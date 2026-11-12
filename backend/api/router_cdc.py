"""V2-12 流式 CDC 路由（M3-D）：通道配置 / 手动轮询 / 告警查询（连接健康视图数据源）。

与连接健康视图（GET /api/mcp/connections/{id}/health）、MCP 工具 connection_health、
前端连接健康面板共用 cdc 模块同一口径。健康视图聚合在 schema_drift.health_info。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi import status as http_status
from pydantic import BaseModel

import audit
from api.dependencies import project_id_param
from core.responses import ApiError, success
from core.storage import JsonStore
from cdc import (
    CHANNELS,
    cdc_status_of,
    fallback_pull,
    poll_connection,
    summarize_cdc,
)
from logging_setup import get_logger
from tenant import current_user, project_tenant_guard  # V2-06 租户隔离 / V2-12 审计 actor

router = APIRouter(prefix="/api/cdc", tags=["cdc"], dependencies=[Depends(project_tenant_guard)])
logger = get_logger("api.cdc")


def _actor(request: Request) -> str:
    return (current_user(request) or {}).get("username", "-")


def _conn_store(project_id: str) -> JsonStore:
    return JsonStore("mcp_connections", project_id)


class ChannelConfig(BaseModel):
    channel: str | None = None  # auto | binlog | mq | cursor | none
    enabled: bool | None = None


@router.get("/channels")
def api_list_channels(project_id: str = Depends(project_id_param)):
    """CDC 通道列表：每连接的状态摘要（running/not_supported/disabled/stopped_drift/circuit_open）。"""
    items = []
    for conn in _conn_store(project_id).all():
        cdc = conn.get("cdc") or {}
        items.append({
            "connection_id": conn.get("id"),
            "display_name": conn.get("display_name"),
            "transport": conn.get("transport"),
            "cdc_status": cdc_status_of(conn),
            **summarize_cdc(cdc),
        })
    return success({"items": items, "total": len(items)})


@router.post("/channels/{connection_id}/config")
def api_configure(request: Request, connection_id: str, body: ChannelConfig,
                  project_id: str = Depends(project_id_param)):
    """通道配置：channel（auto/binlog/mq/cursor/none）与 enabled 开关。

    重新配置通道后 supported 复位（下次轮询重新探测源端支持性）；
    显式 channel="none" → 直接标记 CDC 未支持（走降级周期拉取 + 联邦读）。
    """
    actor = _actor(request)
    store = _conn_store(project_id)
    conn = store.get(connection_id)
    if not conn:
        raise ApiError("NOT_FOUND", "连接不存在", http_status.HTTP_404_NOT_FOUND)
    old = dict(conn.get("cdc") or {})
    new = dict(old)
    if body.channel is not None:
        if body.channel not in ("auto", "none", *CHANNELS):
            raise ApiError("VALIDATION_ERROR",
                           f"channel 必须为 auto/none/{'/'.join(CHANNELS)}",
                           http_status.HTTP_422_UNPROCESSABLE_ENTITY)
        new["channel"] = body.channel
        # 通道变更后重新探测支持性（不静默沿用旧探测结论）
        new.pop("supported", None)
        new["supported"] = False if body.channel == "none" else None
        new["supported_channel"] = None
    if body.enabled is not None:
        new["enabled"] = bool(body.enabled)
        if body.enabled and not old.get("enabled"):
            from datetime import datetime, timezone
            new.setdefault("enabled_at", datetime.now(timezone.utc).isoformat())
    store.update(connection_id, {"cdc": new})
    audit.record(action="cdc_channel_config", actor=actor, project_id=project_id,
                 target_type="mcp_connection", target_id=connection_id,
                 detail={"old": {"channel": old.get("channel"), "enabled": old.get("enabled")},
                         "new": {"channel": new.get("channel"), "enabled": new.get("enabled")}})
    return success({"connection_id": connection_id, "cdc": summarize_cdc(new),
                    "cdc_status": cdc_status_of({**conn, "cdc": new,
                                                 "snapshot_auto_update": conn.get("snapshot_auto_update")})})


@router.post("/channels/{connection_id}/poll")
def api_poll(request: Request, connection_id: str, project_id: str = Depends(project_id_param)):
    """手动单轮消费（与后台循环同一入口）：返回轮统计（applied/skipped_dup/stale/review/error）。"""
    actor = _actor(request)
    st = poll_connection(project_id, connection_id, actor=actor)
    return success(st)


@router.post("/channels/{connection_id}/fallback-pull")
def api_fallback_pull(request: Request, connection_id: str, project_id: str = Depends(project_id_param)):
    """手动触发降级周期拉取（仅对 CDC 未支持的连接生效）。"""
    actor = _actor(request)
    st = fallback_pull(project_id, connection_id, actor=actor)
    return success(st)


@router.get("/channels/{connection_id}/alerts")
def api_alerts(connection_id: str, limit: int = 20, project_id: str = Depends(project_id_param)):
    """最近告警（lag/stalled/circuit_open/恢复事件，连接内 20 条滚动窗）。"""
    conn = _conn_store(project_id).get(connection_id)
    if not conn:
        raise ApiError("NOT_FOUND", "连接不存在", http_status.HTTP_404_NOT_FOUND)
    limit = max(1, min(int(limit or 20), 100))
    alerts = (conn.get("cdc") or {}).get("alerts") or []
    return success({"items": alerts[-limit:], "total": len(alerts)})
