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
import uuid
from abc import ABC, abstractmethod
from typing import Any

from config import get_settings
from logging_setup import get_logger

logger = get_logger("core.instance_store")


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


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
        return records

    def _append_file(self, object_type: str, records: list[dict[str, Any]]) -> None:
        path = self._path_for(object_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(path, "a", encoding="utf-8", newline="\n") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _write_file(self, object_type: str, records: list[dict[str, Any]]) -> None:
        path = self._path_for(object_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp"
        with self._lock, open(tmp, "w", encoding="utf-8", newline="\n") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

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

    def delete(self, record_id: str) -> bool:
        raise NotImplementedError("Jena 实例删除尚未实现")

    def count(self, object_type: str | None = None) -> int:
        raise NotImplementedError("Jena 实例统计尚未实现")

    def all(self) -> list[dict[str, Any]]:
        raise NotImplementedError("Jena 实例全量读取尚未实现")


def get_instance_store(project_id: str | None = None, backend: str | None = None) -> InstanceStore:
    """获取默认实例存储。backend 为 'jena' 时返回预留实现，否则返回本地 JSONL。"""
    if backend == "jena":
        return JenaInstanceStore(project_id=project_id)
    return JsonlInstanceStore(project_id=project_id)
