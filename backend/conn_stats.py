"""conn_stats：V2-12（M3-D）连接调用统计——成功率/时延/最近成功，三类通道统一口径。

三类 kind：
- read：手工拉取/读取（router_mcp.read_connection → mcp_client.read）
- cdc：CDC 消费轮询（backend/cdc 每轮 poll 成功/失败）
- federation：联邦查询（dictionary.federated_lookup 逐系统回源查询）

写回 mcp_connections 记录的 stats 子对象（每类一个 50 槽滚动窗 recent + 计数）。
跨线程安全：模块级 RLock（JsonStore update 为读改写，CDC 后台线程与 API 线程并发）。
永不抛错：统计失败只告警日志，不阻断调用主流程。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from core.storage import JsonStore
from logging_setup import get_logger

logger = get_logger("conn_stats")

_LOCK = threading.RLock()
_WINDOW = 50
KINDS = ("read", "cdc", "federation")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn_store(project_id: str) -> JsonStore:
    return JsonStore("mcp_connections", project_id)


def _bucket(rec: dict[str, Any], kind: str) -> dict[str, Any]:
    stats = rec.setdefault("stats", {})
    if not isinstance(stats, dict):
        stats = {}
        rec["stats"] = stats
    b = stats.get(kind)
    if not isinstance(b, dict):
        b = {"ok": 0, "fail": 0, "last_ms": None, "last_at": None, "last_error": None, "recent": []}
        stats[kind] = b
    return b


def record_conn_stat(
    project_id: str,
    connection_id: str,
    kind: str,
    ok: bool,
    ms: int | None = None,
    error: str | None = None,
) -> None:
    """记录一次调用结果（滚动窗 50 槽）。永不抛错。"""
    if kind not in KINDS:
        return
    try:
        with _LOCK:
            store = _conn_store(project_id)
            rec = store.get(connection_id)
            if not rec:
                return
            b = _bucket(rec, kind)
            if ok:
                b["ok"] = int(b.get("ok", 0)) + 1
            else:
                b["fail"] = int(b.get("fail", 0)) + 1
            if ms is not None:
                b["last_ms"] = int(ms)
            b["last_at"] = _now()
            b["last_error"] = (error or "")[:300] if not ok else None
            recent = b.get("recent") if isinstance(b.get("recent"), list) else []
            recent.append({"ok": bool(ok), "ms": int(ms) if ms is not None else None, "at": b["last_at"]})
            b["recent"] = recent[-_WINDOW:]
            store.update(connection_id, {"stats": rec["stats"]})
    except Exception:  # noqa: BLE001——统计永不阻断主流程
        logger.warning("conn_stats 记录失败 kind=%s conn=%s（不阻断）", kind, connection_id, exc_info=True)


def _summarize_bucket(b: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(b, dict):
        return {"calls": 0, "success_rate": None, "last_ms": None, "last_at": None, "last_error": None}
    recent = b.get("recent") if isinstance(b.get("recent"), list) else []
    n = len(recent)
    ok_n = sum(1 for r in recent if isinstance(r, dict) and r.get("ok"))
    total = int(b.get("ok", 0)) + int(b.get("fail", 0))
    return {
        "calls": total,
        "window_calls": n,
        "success_rate": round(ok_n / n, 4) if n else None,
        "last_ms": b.get("last_ms"),
        "last_at": b.get("last_at"),
        "last_error": b.get("last_error"),
    }


def summarize_conn_stats(conn: dict[str, Any]) -> dict[str, Any]:
    """连接健康视图用：三类调用的成功率/时延/最近成功（口径=累计计数 + 最近 50 次窗口成功率）。"""
    stats = conn.get("stats") if isinstance(conn.get("stats"), dict) else {}
    return {k: _summarize_bucket(stats.get(k)) for k in KINDS}


def human_age(seconds: int | None) -> str | None:
    """V2-12 TP-12-07：距上次(秒) → 「xx小时xx分xx秒」。"""
    if seconds is None:
        return None
    s = max(0, int(seconds))
    return f"{s // 3600}小时{(s % 3600) // 60}分{s % 60}秒"
