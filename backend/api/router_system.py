"""系统 / 凭据路由：健康检查、平台元信息、凭据状态、日志查看（VS9）。"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from config import get_settings
from core.responses import success
from credential_store import list_configured
from logging_setup import get_logger, log_event, read_recent_logs

router = APIRouter(tags=["system"])
logger = get_logger("api.system")
_settings = get_settings()


@router.get("/api/system/info")
def system_info():
    return success({
        "app_name": _settings.app_name,
        "version": _settings.version,
        "modules": {
            "modeling": "ready",
            "jena_adapter": "file-mode",
            "neo4j_sync": "pending",
            "ingestion": "pending",
            "reasoning": "pending",
            "mcp_server": "pending",
            "mcp_client": "pending",
            "action_engine": "ready",
            "credential_store": "ready",
            "template": "pending",
            "viz": "ready",
        },
    })


@router.get("/api/credentials/status")
def credentials_status():
    # 仅返回引用名，绝不返回明文
    return success(list_configured())


class ClientLogPayload(BaseModel):
    level: str = Field(default="info")
    message: str
    stack: str | None = None
    url: str | None = None
    user_agent: str | None = None


@router.get("/api/logs")
def list_logs(
    days: int = Query(default=2, ge=1, le=7),
    limit: int = Query(default=2000, ge=1, le=10000),
):
    """返回最近 N 天的结构化日志，从新到旧排列。"""
    return success({"items": read_recent_logs(days=days, limit=limit), "days": days, "limit": limit})


@router.post("/api/client-logs")
def receive_client_log(payload: ClientLogPayload):
    """接收前端上报的错误/操作日志，统一落到后端日志文件。"""
    level_name = payload.level.upper()
    level = getattr(__import__("logging"), level_name, 20)
    fields = {"client": True, "url": payload.url, "user_agent": payload.user_agent}
    if payload.stack:
        fields["stack"] = payload.stack
    log_event(logger, level, payload.message, **fields)
    return success({"received": True})
