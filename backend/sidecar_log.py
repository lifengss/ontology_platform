"""V2-10 JSONL append-only 旁路操作日志（M4-G）：本体变更 / Action 执行 / 外部读取统一旁路。

设计（API 文档 §26）：
- 存储：``data/sidecar/sidecar.jsonl`` 单文件（JSON Lines），按行追加，永不修改/删除。
- **旁路日志**（非主存储）：与 V2-09 审计（``audit.record``）不同——审计回答"谁在何时改了什么"，
  旁路日志回答"当天实际打到外部系统的所有写/读操作"，供运维按天回放对账（TC-10-02）。
- 统一 schema：``id / time / actor / type / project_id / target_type / target_id / payload / result / prev_hash``。
  ``type`` 三类：``ontology_change`` / ``action_execution`` / ``external_read``。
- **哈希链**（TC-10-03）：每行 ``prev_hash`` = 前一行整行的 SHA-256（首行的 ``prev_hash="0"*64``）；
  任何一行被篡改都会导致该行后续哈希链断裂，``verify_chain`` 可检测。
- **写失败不影响主流程**（TCE-10-01）：``record()`` 内部 try/except 吞掉写失败并告警，
  不补写不伪造；主存储（Jena/Neo4j/PG）路径不依赖本日志（TP-10-04）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import config as cfg

logger = None  # 延迟导入，避免循环依赖

# 事件类型
TYPE_ONTOLOGY_CHANGE = "ontology_change"
TYPE_ACTION_EXECUTION = "action_execution"
TYPE_EXTERNAL_READ = "external_read"

# 哈希链首行前驱
_CHAIN_GENESIS = "0" * 64


def _log() -> None:
    global logger
    if logger is None:
        try:
            from logging_setup import get_logger
            logger = get_logger("sidecar")
        except Exception:  # noqa: BLE001
            logger = None


def _log_path() -> Path:
    p = Path(cfg.get_settings().data_dir) / "sidecar" / "sidecar.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_LOCK = threading.Lock()


def _line_hash(line: str) -> str:
    """对单行（不含换行）求 SHA-256。"""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _head(lines: list[str]) -> int:
    """返回已有行数（含损坏行，损坏行视为不可信但计入位置）。"""
    return len(lines)


def record(
    type_: str,
    actor: str = "-",
    project_id: str = "",
    target_type: str = "",
    target_id: str = "",
    payload: Any = None,
    result: Any = None,
) -> dict[str, Any]:
    """追加一条旁路日志（append-only；写失败仅告警，不影响主流程）。

    ``prev_hash`` 为上一行的哈希；本行哈希由调用方后续计算（读回 verify_chain 时准确定位）。
    """
    rec = {
        "id": uuid.uuid4().hex[:12],
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "actor": actor or "-",
        "type": type_,
        "project_id": project_id or "",
        "target_type": target_type or "",
        "target_id": str(target_id or "")[:120],
        "payload": payload if payload is not None else {},
        "result": result if result is not None else {},
        "prev_hash": _CHAIN_GENESIS,
    }
    try:
        with _LOCK:
            p = _log_path()
            prev_hash = _CHAIN_GENESIS
            if p.exists():
                # 取最后一行算出"前一行哈希"（损坏行视为不可信，重置链头）
                last_line = None
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            last_line = line
                if last_line:
                    prev_hash = _line_hash(last_line)
            rec["prev_hash"] = prev_hash
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001  旁路非主存储：写失败不影响主流程（TCE-10-01）
        _log() and logger.warning("sidecar: 旁路日志写失败（不影响主流程）: %s", e)
    return rec


def record_from_request(
    request,
    type_: str,
    project_id: str = "",
    target_type: str = "",
    target_id: str = "",
    payload: Any = None,
    result: Any = None,
) -> dict[str, Any]:
    """从请求提取 actor/role 后调用 ``record``。"""
    try:
        user = getattr(request.state, "auth_user", None) or {}
        actor = user.get("username", "-")
    except Exception:  # noqa: BLE001
        actor = "-"
    return record(type_, actor=actor, project_id=project_id,
                  target_type=target_type, target_id=target_id,
                  payload=payload, result=result)


def _iter_all() -> list[dict[str, Any]]:
    p = _log_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                _log() and logger.warning("sidecar: 跳过损坏行")
    return out


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _day_bounds(day: str) -> tuple[datetime, datetime]:
    """day='YYYY-MM-DD' 的本日 [开始, 结束)（本地时区）。"""
    d = datetime.strptime(day, "%Y-%m-%d")
    start = d.astimezone()
    end = start + timedelta(days=1)
    return start, end


def _in_day(ts: str, day: str) -> bool:
    dt = _parse_ts(ts)
    if dt is None:
        return False
    start, end = _day_bounds(day)
    return start <= dt < end


def _match(r: dict[str, Any], type_: Optional[str], actor: Optional[str],
           project_id: Optional[str], target_type: Optional[str],
           target_id: Optional[str], target: Optional[str],
           day: Optional[str]) -> bool:
    if type_ and r.get("type") != type_:
        return False
    if actor and r.get("actor") != actor:
        return False
    if project_id and r.get("project_id") != project_id:
        return False
    if target_type and r.get("target_type") != target_type:
        return False
    if target_id and r.get("target_id") != target_id:
        return False
    if target:
        hay = f"{r.get('target_id', '')} {r.get('target_type', '')}".lower()
        if target.lower() not in hay:
            return False
    if day and not _in_day(r.get("time", ""), day):
        return False
    return True


def query(type_: Optional[str] = None, actor: Optional[str] = None,
          project_id: Optional[str] = None, target_type: Optional[str] = None,
          target_id: Optional[str] = None, target: Optional[str] = None,
          day: Optional[str] = None, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """组合检索（类型/操作者/项目/对象/按天），默认正序（追加序，便于回放）。"""
    items = [r for r in _iter_all() if _match(
        r, type_, actor, project_id, target_type, target_id, target, day)]
    # 稳定排序（仅按 time）：同时间戳保持文件追加序（正序，便于按序回放）。
    items.sort(key=lambda r: r.get("time", ""))
    total = len(items)
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    s = (page - 1) * page_size
    return {"items": items[s:s + page_size], "total": total, "page": page, "page_size": page_size}


def stats(type_: Optional[str] = None, actor: Optional[str] = None,
          project_id: Optional[str] = None, day: Optional[str] = None) -> dict[str, Any]:
    """统计：按类型/操作者计数 + 总数。"""
    items = [r for r in _iter_all() if _match(
        r, type_, actor, project_id, None, None, None, day)]
    by_type: dict[str, int] = {}
    by_actor: dict[str, int] = {}
    for r in items:
        by_type[r.get("type", "unknown")] = by_type.get(r.get("type", "unknown"), 0) + 1
        by_actor[r.get("actor", "-")] = by_actor.get(r.get("actor", "-"), 0) + 1
    return {"total": len(items), "by_type": by_type, "by_actor": by_actor}


def replay_day(day: str, type_: str = TYPE_ACTION_EXECUTION) -> list[dict[str, Any]]:
    """按天回放：重建当日 Action 回写序列（TC-10-02），追加序。

    返回该日所有指定类型事件；调用方对账时按序重放并比对外部系统实际状态。
    """
    items = [r for r in _iter_all() if _match(
        r, type_, None, None, None, None, None, day)]
    items.sort(key=lambda r: r.get("time", ""))
    return items


def verify_chain() -> dict[str, Any]:
    """哈希链完整性校验（TC-10-03）：逐行校验 prev_hash == 前一行哈希。

    返回 {ok, checked, broken_at?, message}；任何行被篡改都会链断裂。
    """
    p = _log_path()
    if not p.exists():
        return {"ok": True, "checked": 0, "message": "日志为空（无记录可校验）"}
    prev_hash = _CHAIN_GENESIS
    checked = 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return {"ok": False, "checked": checked, "broken_at": checked + 1,
                        "message": f"第 {checked + 1} 行为损坏行（非合法 JSON）"}
            if rec.get("prev_hash") != prev_hash:
                return {"ok": False, "checked": checked, "broken_at": checked + 1,
                        "message": f"第 {checked + 1} 行哈希链断裂（prev_hash 与上一行不一致）"}
            prev_hash = _line_hash(line)
            checked += 1
    return {"ok": True, "checked": checked, "message": "哈希链完整"}
