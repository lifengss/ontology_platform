"""实例存储抽象（默认本地 JSONL，预留 Jena/图数据库扩展）。

存储布局：
    data_dir/projects/<project_id>/instances/<object_id>.jsonl
每个文件为独立 JSONL，便于按对象类型追加/读取，并与 Neo4j 文件降级解耦。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any

from config import get_settings
from logging_setup import get_logger

logger = get_logger("core.instance_store")


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---- GAP-14：文件级记录缓存（跨 store 实例共享）----
# 背景：get_instance_store 每次调用都新建 JsonlInstanceStore，实例级缓存会失效，
# 因此缓存挂在模块级、以文件绝对路径为键（path → ((mtime_ns, size), records)）。
# 失效策略：mtime_ns/size 任一变化即不命中（外部/他进程写入同样安全）；
# 写侧（_append_file/_write_file）成功后主动失效兜底。
# 读侧返回外层记录浅拷贝：调用方 update() 的 rec.update 只改拷贝，不污染缓存。
# 内存权衡：单条记录 ~数百字节，5 万实例文件约数十 MB 量级，换取读路径免磁盘解析。
_FILE_CACHE: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
_FILE_CACHE_LOCK = threading.Lock()


def _invalidate_file_cache(path: pathlib.Path) -> None:
    with _FILE_CACHE_LOCK:
        _FILE_CACHE.pop(str(path), None)


def _copy_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """缓存读出副本：外层记录＋嵌套 instance 各浅拷贝一层，调用方就地修改不污染缓存。"""
    out: list[dict[str, Any]] = []
    for r in records:
        c = dict(r)
        inst = c.get("instance")
        if isinstance(inst, dict):
            c["instance"] = dict(inst)
        out.append(c)
    return out


# ---- GAP-17 容量基准发现：列表/统计/图谱等读路径每请求全量物化（copy 5 万条 ~百 ms 级），
# 高并发下 GIL 排队把 P50 拖到秒级。增加全量只读快照缓存：按"各文件 (mtime_ns,size) 聚合签名"
# 失效，签名未变的请求共享同一份只读列表（零拷贝）。调用方只读约定：不得修改快照内记录
#（列表过滤/排序/分页均为只读操作；annotate_rows/mask_instance_content 返回新 dict 已核实）。
_SNAPSHOT_CACHE: dict[str, list[dict[str, Any]]] = {}
_SNAPSHOT_CACHE_LOCK = threading.Lock()
_SNAPSHOT_CACHE_KEEP = 4  # 最多保留 4 个版本（写频场景新旧版本交替）


class InstanceStore(ABC):
    """实例存储抽象接口。"""

    @abstractmethod
    def insert(self, record: dict[str, Any]) -> dict[str, Any]:
        """插入一条实例记录并返回完整记录（含 id）。"""
        raise NotImplementedError

    @abstractmethod
    def get(self, record_id: str) -> dict[str, Any] | None:
        """按记录 id 读取单条实例。"""
        raise NotImplementedError

    @abstractmethod
    def find(
        self,
        object_type: str | None = None,
        filters: dict[str, Any] | None = None,
        limit: int | None = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按对象类型和/或字段过滤查询实例。limit=None 表示不截断（返回全部匹配记录）。"""
        raise NotImplementedError

    @abstractmethod
    def update(self, record_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        """按记录 id 局部更新实例。"""
        raise NotImplementedError

    @abstractmethod
    def update_many(self, object_type: str, patches: dict[str, dict[str, Any]]) -> int:
        """HF-11：同对象类型批量局部更新（一次读+一次写，消除逐实例全量重写 O(n²)）。

        仅更新列在 patches 中的记录 id；更新后 rec 增加 updated_at。
        返回实际更新的记录数。
        """
        raise NotImplementedError

    @abstractmethod
    def delete(self, record_id: str) -> bool:
        """按记录 id 删除实例。"""
        raise NotImplementedError

    @abstractmethod
    def count(self, object_type: str | None = None) -> int:
        """统计实例总数（可按对象类型）。"""
        raise NotImplementedError

    @abstractmethod
    def all(self) -> list[dict[str, Any]]:
        """返回所有实例记录（向后兼容 JsonStore 接口）。"""
        raise NotImplementedError


class JsonlInstanceStore(InstanceStore):
    """本地 JSONL 实现：每个 object_type 一个 .jsonl 文件。"""

    # 旧版 Neo4j 文件降级产物（项目级单文件），历史数据需一次性迁入本存储
    _LEGACY_NAMES = ("instances.jsonl", "instances.json")

    def __init__(self, project_id: str | None = None, data_dir: str | None = None) -> None:
        settings = get_settings()
        self._project_id = project_id or "default"
        self._dir = pathlib.Path(data_dir or settings.data_dir) / "projects" / self._project_id / "instances"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        """将旧版项目级单文件实例（instances.jsonl / instances.json）迁入按对象分文件的新存储。

        迁移后旧文件改名为 <原文件名>.migrated 作为幂等标记，重复访问不再迁移。
        """
        base = self._dir.parent
        for name in self._LEGACY_NAMES:
            legacy = base / name
            marker = base / f"{name}.migrated"
            if not legacy.exists() or marker.exists():
                continue
            raw_records: list[dict[str, Any]] = []
            try:
                text = legacy.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("读取旧版实例文件失败，跳过迁移: %s %s", legacy, e)
                continue
            if name.endswith(".json"):
                try:
                    batch = json.loads(text)
                    if isinstance(batch, list):
                        raw_records = [r for r in batch if isinstance(r, dict)]
                except json.JSONDecodeError as e:
                    logger.warning("旧版实例文件解析失败，跳过迁移: %s %s", legacy, e)
                    continue
            else:
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        raw_records.append(rec)
            migrated = 0
            with self._lock:
                # 按内容签名去重（tx_id + object_type + instance），
                # 避免同一批数据在多个旧文件（instances.jsonl / instances.json）中重复迁入
                seen_signatures = {
                    self._record_signature(r) for r in self._read_all_records()
                }
                for rec in raw_records:
                    object_type = rec.get("object_type")
                    if not object_type:
                        continue
                    inst = rec.get("instance") if isinstance(rec.get("instance"), dict) else {}
                    signature = self._record_signature(rec)
                    if signature in seen_signatures:
                        continue
                    rid = rec.get("id") or (
                        "legacy_"
                        + hashlib.sha1(
                            json.dumps(
                                {"tx": rec.get("tx_id"), "ot": object_type, "inst": inst},
                                sort_keys=True,
                                ensure_ascii=False,
                            ).encode("utf-8")
                        ).hexdigest()[:16]
                    )
                    seen_signatures.add(signature)
                    new_rec = {
                        "id": rid,
                        "project_id": rec.get("project_id") or self._project_id,
                        "ontology_id": rec.get("ontology_id"),
                        "object_type": object_type,
                        "candidate_id": rec.get("candidate_id"),
                        "instance": inst,
                        "source": rec.get("source"),
                        "tx_id": rec.get("tx_id"),
                        "imported_at": rec.get("imported_at") or _now(),
                    }
                    self._append_file(object_type, [new_rec])
                    migrated += 1
                try:
                    legacy.rename(marker)
                except OSError as e:
                    logger.warning("旧版实例文件改名失败（数据已迁入）: %s %s", legacy, e)
            logger.info("旧版实例文件迁移完成: %s migrated=%d", legacy, migrated)

    def _read_all_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self._dir.glob("*.jsonl")):
            records.extend(self._read_file(path.stem))
        return records

    def snapshot(self) -> tuple[str, list[dict[str, Any]]]:
        """全量只读快照（GAP-17）：跨文件聚合＋按聚合签名缓存，免每请求物化。

        返回 (version, records)。**只读约定**：records 不得被调用方修改（外层/嵌套均不得）；
        需要可变副本时用 find()/all()（返回独立拷贝）。version 为各文件 (mtime_ns, size)
        聚合签名——任何写入（含外部进程）都会改变签名从而失效。
        """
        sigs = []
        for p in sorted(self._dir.glob("*.jsonl")):
            try:
                st = p.stat()
                sigs.append((p.stem, st.st_mtime_ns, st.st_size))
            except OSError:
                continue
        vkey = json.dumps(sigs, ensure_ascii=False, default=str)
        with _SNAPSHOT_CACHE_LOCK:
            hit = _SNAPSHOT_CACHE.get(vkey)
        if hit is not None:
            return vkey, hit
        records: list[dict[str, Any]] = []
        for stem, _mt, _sz in sigs:
            records.extend(self._read_file(stem))  # _read_file 返回独立副本 → 快照自成一体
        with _SNAPSHOT_CACHE_LOCK:
            if len(_SNAPSHOT_CACHE) >= _SNAPSHOT_CACHE_KEEP:
                _SNAPSHOT_CACHE.clear()
            _SNAPSHOT_CACHE[vkey] = records
        return vkey, records

    @staticmethod
    def _record_signature(rec: dict[str, Any]) -> str:
        """实例内容签名：tx_id + object_type + instance，用于跨文件去重。"""
        return hashlib.sha1(
            json.dumps(
                {"tx": rec.get("tx_id"), "ot": rec.get("object_type"), "inst": rec.get("instance")},
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    def _path_for(self, object_type: str) -> pathlib.Path:
        return self._dir / f"{object_type}.jsonl"

    def _read_file(self, object_type: str) -> list[dict[str, Any]]:
        path = self._path_for(object_type)
        if not path.exists():
            return []
        # GAP-14：文件级缓存命中判定（mtime_ns + size 任一变化即失效，外部/他进程写入同样安全）
        key = str(path)
        sig = None
        try:
            st = path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        if sig is not None:
            with _FILE_CACHE_LOCK:
                hit = _FILE_CACHE.get(key)
            if hit and hit[0] == sig:
                return _copy_records(hit[1])
        records: list[dict[str, Any]] = []
        with self._lock, open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    logger.warning("JSONL 解析跳过: %s line=%s", e, line[:200])
        if sig is not None:
            with _FILE_CACHE_LOCK:
                _FILE_CACHE[key] = (sig, records)
        return _copy_records(records)

    def _append_file(self, object_type: str, records: list[dict[str, Any]]) -> None:
        path = self._path_for(object_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(path, "a", encoding="utf-8", newline="\n") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _invalidate_file_cache(path)  # GAP-14：写后主动失效（兜底＝mtime 变化）

    # HF-11：Windows 下 os.replace 会因瞬时文件锁抛 PermissionError(WinError 5)
    # 或 FileNotFoundError，重写为退避重试；一次性批量写回（供 update_many 消除 O(n²)）。
    # HF-13：approve 落库也走 update→_write_file，偶发 WinError 3 次重试未兜住，
    # 将重试次数增至 5 次、退避略增（读侧 _read_file 已对损坏 JSONL 行容错跳过，
    # 写回时因坏行已被过滤而顺带清理；不影响写入正确性）。
    _REPLACE_RETRIES = 5
    _REPLACE_BACKOFF_SEC = 0.08

    def _write_file(self, object_type: str, records: list[dict[str, Any]]) -> None:
        path = self._path_for(object_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp"
        with self._lock, open(tmp, "w", encoding="utf-8", newline="\n") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        last_err: Exception | None = None
        for attempt in range(1, self._REPLACE_RETRIES + 1):
            try:
                os.replace(tmp, path)
                _invalidate_file_cache(path)  # GAP-14：写后主动失效
                return
            except (PermissionError, FileNotFoundError) as e:
                # 瞬时锁：重试前等待；末次失败上抛，由上层决定是否降级/重试。
                last_err = e
                if attempt == self._REPLACE_RETRIES:
                    break
                time.sleep(self._REPLACE_BACKOFF_SEC * attempt)
        logger.warning("os.replace 重试 %d 次仍失败: object_type=%s err=%s",
                       self._REPLACE_RETRIES, object_type, last_err)
        raise last_err  # type: ignore[misc]

    def insert(self, record: dict[str, Any]) -> dict[str, Any]:
        record.setdefault("id", uuid.uuid4().hex)
        record.setdefault("project_id", self._project_id)
        record.setdefault("created_at", _now())
        object_type = record.get("object_type")
        if not object_type:
            raise ValueError("实例记录必须包含 object_type")
        self._append_file(object_type, [record])
        return record

    def get(self, record_id: str) -> dict[str, Any] | None:
        for path in self._dir.glob("*.jsonl"):
            for rec in self._read_file(path.stem):
                if rec.get("id") == record_id:
                    return rec
        return None

    def find(
        self,
        object_type: str | None = None,
        filters: dict[str, Any] | None = None,
        limit: int | None = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters = filters or {}
        candidates: list[dict[str, Any]] = []
        if object_type:
            candidates = self._read_file(object_type)
        else:
            for path in sorted(self._dir.glob("*.jsonl")):
                candidates.extend(self._read_file(path.stem))

        def _match(rec: dict[str, Any]) -> bool:
            for k, v in filters.items():
                if rec.get(k) != v:
                    return False
            return True

        matched = [r for r in candidates if _match(r)]
        if limit is None:
            return matched[offset:]
        return matched[offset : offset + limit]

    def update(self, record_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        for path in self._dir.glob("*.jsonl"):
            object_type = path.stem
            records = self._read_file(object_type)
            for i, rec in enumerate(records):
                if rec.get("id") == record_id:
                    rec.update({k: v for k, v in patch.items() if k != "id"})
                    rec["updated_at"] = _now()
                    records[i] = rec
                    self._write_file(object_type, records)
                    return rec
        return None

    def update_many(self, object_type: str, patches: dict[str, dict[str, Any]]) -> int:
        """HF-11：同对象类型批量局部更新（一次读 + 一次写）。

        避免 execute_batch 里对每个实例单独 update()——那会把同一 jsonl 文件
        反复全量读改写 N 次（O(n²) IO）。这里是按对象类型聚合补丁后仅读写一次。
        """
        if not patches:
            return 0
        object_type = str(object_type)
        records = self._read_file(object_type)
        if not records:
            return 0
        updated = 0
        for rec in records:
            rid = rec.get("id")
            patch = patches.get(rid)
            if patch is None:
                continue
            rec.update({k: v for k, v in patch.items() if k != "id"})
            rec["updated_at"] = _now()
            updated += 1
        if updated:
            self._write_file(object_type, records)
        return updated

    def delete(self, record_id: str) -> bool:
        for path in self._dir.glob("*.jsonl"):
            object_type = path.stem
            records = self._read_file(object_type)
            new_records = [r for r in records if r.get("id") != record_id]
            if len(new_records) != len(records):
                self._write_file(object_type, new_records)
                return True
        return False

    def count(self, object_type: str | None = None) -> int:
        if object_type:
            return len(self._read_file(object_type))
        total = 0
        for path in self._dir.glob("*.jsonl"):
            total += len(self._read_file(path.stem))
        return total

    def all(self) -> list[dict[str, Any]]:
        # 「全量」语义：不受 find() 默认 limit=1000 截断。
        # 历史 bug：all() 退化为「前 1000 条」，实例数超限后按字母序命中的同一对象占满前 1000，
        # 其余对象在截断快照上过滤恒为 0，导致树计数/图谱节点/血缘全面失真。
        return self.find(limit=None)


class JenaInstanceStore(InstanceStore):
    """Jena 图数据库实例存储预留实现（待接入 jena_adapter/client.py 写入/查询接口）。"""

    def __init__(self, project_id: str | None = None) -> None:
        self._project_id = project_id or "default"

    def insert(self, record: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Jena 实例写入尚未实现")

    def get(self, record_id: str) -> dict[str, Any] | None:
        raise NotImplementedError("Jena 实例读取尚未实现")

    def find(
        self,
        object_type: str | None = None,
        filters: dict[str, Any] | None = None,
        limit: int | None = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("Jena 实例查询尚未实现")

    def update(self, record_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError("Jena 实例更新尚未实现")

    def update_many(self, object_type: str, patches: dict[str, dict[str, Any]]) -> int:
        raise NotImplementedError("Jena 实例批量更新尚未实现")

    def delete(self, record_id: str) -> bool:
        raise NotImplementedError("Jena 实例删除尚未实现")

    def count(self, object_type: str | None = None) -> int:
        raise NotImplementedError("Jena 实例统计尚未实现")

    def all(self) -> list[dict[str, Any]]:
        raise NotImplementedError("Jena 实例全量读取尚未实现")


def get_instance_store(project_id: str | None = None, backend: str | None = None) -> InstanceStore:
    """获取默认实例存储。

    backend 显式传入时优先（'jena'→预留实现，'pg'→Postgres，'jsonl'→本地文件）；
    未传则按 settings.instance_store_backend（环境变量 INSTANCE_STORE_BACKEND）选择，
    默认 'jsonl'（MVP 本地文件）。K8s 多副本部署设 INSTANCE_STORE_BACKEND=pg 收敛共享一致。
    """
    if backend and backend != "auto":
        if backend == "jena":
            return JenaInstanceStore(project_id=project_id)
        if backend == "pg":
            from core.pg_instance_store import PgInstanceStore

            return PgInstanceStore(project_id=project_id)
        return JsonlInstanceStore(project_id=project_id)
    settings = get_settings()
    chosen = settings.instance_store_backend
    if chosen == "pg":
        from core.pg_instance_store import PgInstanceStore

        return PgInstanceStore(project_id=project_id)
    if chosen == "jena":
        return JenaInstanceStore(project_id=project_id)
    return JsonlInstanceStore(project_id=project_id)
