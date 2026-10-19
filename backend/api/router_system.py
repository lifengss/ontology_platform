"""系统 / 凭据路由：健康检查、平台元信息、凭据状态（VS9）。"""
from __future__ import annotations

from fastapi import APIRouter

from config import get_settings
from core.responses import success
from credential_store import list_configured
from logging_setup import get_logger

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
