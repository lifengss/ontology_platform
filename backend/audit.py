"""V2-09 完整审计（M2-D）：平台统一 append-only 审计日志。

设计（API 文档 §25）：
- 存储：``data/audit/audit.jsonl`` 单一日志文件（JSON Lines），按行追加。
- INV-4：本模块**不提供**任何修改/删除接口；``/api/audit`` 仅有读与导出，
  任何写尝试返回 405 AUDIT_IMMUTABLE 并记录 ``audit_tamper_attempt``。
- 每条记录含 who（actor/actor_role）/when（ts）/what（action/target_*）/
  前值后值（before/after）/项目与租户（project_id/tenant_id）。
- 统一入口 ``record()``：auth 域事件经 ``auth.audit()`` 镜像进入本日志，
  各业务路由（建模/入库/删除/写回/审批/权限变更）直接调用 ``record()``。
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import config as cfg

logger = None  # 延迟导入，避免循环依赖


def _log() -> None:
    global logger
    if logger is None:
        try:
            from logging_setup import get_logger
            logger = get_logger("audit")
        except Exception:  # noqa: BLE001
            logger = None


def _log_path() -> Path:
    p = Path(cfg.get_settings().data_dir) / "audit" / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_LOCK = threading.Lock()


def record(
    action: str,
    actor: str = "-",
    actor_role: str = "",
    project_id: str = "",
    tenant_id: str = "",
    target_type: str = "",
    target_id: str = "",
    target_name: str = "",
    before: Any = None,
    after: Any = None,
    detail: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """追加一条审计记录（永不修改/删除既有行）。"""
    rec = {
        "id": uuid.uuid4().hex[:12],
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "actor": actor or "-",
        "actor_role": actor_role or "",
        "action": action,
        "project_id": project_id or "",
        "tenant_id": tenant_id or "",
        "target_type": target_type or "",
        "target_id": str(target_id or "")[:120],
        "target_name": str(target_name or "")[:120],
        "before": before,
        "after": after,
        "detail": detail or {},
    }
    line = json.dumps(rec, ensure_ascii=False)
    with _LOCK:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return rec


def actor_from_request(request) -> tuple[str, str]:
    """从鉴权中间件注入的 request.state.auth_user 提取（who）。"""
    try:
        user = getattr(request.state, "auth_user", None) or {}
        return user.get("username", "-"), user.get("role", "") or ""
    except Exception:  # noqa: BLE001
        return "-", ""


def record_from_request(
    request,
    action: str,
    project_id: str = "",
    tenant_id: str = "",
    target_type: str = "",
    target_id: str = "",
    target_name: str = "",
    before: Any = None,
    after: Any = None,
    detail: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    actor, role = actor_from_request(request)
    return record(action, actor=actor, actor_role=role, project_id=project_id,
                  tenant_id=tenant_id, target_type=target_type, target_id=target_id,
                  target_name=target_name, before=before, after=after, detail=detail)


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
                _log() and logger.warning("audit: 跳过损坏行")
    return out


def _parse_ts(value: str) -> Optional[datetime]:
    """ISO 时间解析（兼容 'Z' 后缀与无时区视为本地时间）；解析失败返回 None。"""
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
        dt = dt.astimezone()  # 无时区按本地时区解释（与 record() 落盘一致）
    return dt


def _ts_key(value: str) -> tuple:
    """时间排序/比较键：可解析走 datetime（跨时区正确），否则回退字符串（保持原序）。"""
    dt = _parse_ts(value)
    if dt is not None:
        return (1, dt.timestamp(), "")
    return (0, 0.0, value)


def _match(r: dict[str, Any], actor: Optional[str], action: Optional[str],
           project_id: Optional[str], tenant_id: Optional[str],
           target_type: Optional[str], target_id: Optional[str],
           target: Optional[str], start: Optional[str], end: Optional[str]) -> bool:
    if actor and r.get("actor") != actor:
        return False
    if action and r.get("action") != action:
        return False
    if project_id and r.get("project_id") != project_id:
        return False
    if tenant_id and r.get("tenant_id") != tenant_id:
        return False
    if target_type and r.get("target_type") != target_type:
        return False
    if target_id and r.get("target_id") != target_id:
        return False
    if target:
        hay = f"{r.get('target_id', '')} {r.get('target_name', '')}".lower()
        if target.lower() not in hay:
            return False
    # 时间过滤：按绝对时间比较（ISO 带时区，跨时区安全）；任一侧不可解析时回退字符串比较
    ts = r.get("ts", "")
    ts_dt = _parse_ts(ts)
    if start:
        start_dt = _parse_ts(start)
        if start_dt is not None and ts_dt is not None:
            if ts_dt < start_dt:
                return False
        elif ts < start:
            return False
    if end:
        end_dt = _parse_ts(end)
        if end_dt is not None and ts_dt is not None:
            if ts_dt > end_dt:
                return False
        elif ts > end:
            return False
    return True


def query(actor: Optional[str] = None, action: Optional[str] = None,
          project_id: Optional[str] = None, tenant_id: Optional[str] = None,
          target_type: Optional[str] = None, target_id: Optional[str] = None,
          target: Optional[str] = None, start: Optional[str] = None,
          end: Optional[str] = None, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """组合检索（操作者＋时间范围＋对象），默认倒序（最新在前）。"""
    items = [r for r in _iter_all() if _match(
        r, actor, action, project_id, tenant_id, target_type, target_id, target, start, end)]
    items.sort(key=lambda r: (_ts_key(r.get("ts", "")), r.get("id", "")), reverse=True)
    total = len(items)
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    s = (page - 1) * page_size
    return {"items": items[s:s + page_size], "total": total, "page": page, "page_size": page_size}


def export_rows(actor: Optional[str] = None, action: Optional[str] = None,
                project_id: Optional[str] = None, tenant_id: Optional[str] = None,
                target_type: Optional[str] = None, target_id: Optional[str] = None,
                target: Optional[str] = None, start: Optional[str] = None,
                end: Optional[str] = None) -> list[dict[str, Any]]:
    """导出（与检索同条件，全量倒序；CSV/JSONL 由调用方序列化）。"""
    items = [r for r in _iter_all() if _match(
        r, actor, action, project_id, tenant_id, target_type, target_id, target, start, end)]
    items.sort(key=lambda r: (_ts_key(r.get("ts", "")), r.get("id", "")), reverse=True)
    return items
