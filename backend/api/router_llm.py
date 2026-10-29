"""LLM 设置与连通性测试路由。"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ai_helper import reload_llm_config, test_connection
from ai_helper.config import get_llm_config
from core.responses import success
from credential_store import save_secret
from logging_setup import get_logger

router = APIRouter(prefix="/api/llm", tags=["llm"])
logger = get_logger("api.llm")


class LLMSettingsUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    timeout: int | None = Field(default=None, ge=1, le=600)
    max_tokens: int | None = Field(default=None, ge=1, le=65536)
    api_key: str | None = None
    prompts: dict[str, str] | None = None


class LLMTestRequest(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    timeout: int | None = 30


@router.get("/settings")
def get_llm_settings():
    """读取当前 LLM 非敏感运行时配置（API key 不暴露，只返回来源）。"""
    cfg = get_llm_config()
    data = dict(cfg.raw)
    data["base_url"] = cfg.base_url()
    has_env = bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"))
    has_file = bool(cfg.api_key()) and not has_env
    data["api_key_configured"] = has_env or has_file
    data["api_key_source"] = "env" if has_env else ("file" if has_file else "none")
    return success(data)


@router.get("/settings/reveal")
def reveal_llm_api_key():
    """返回当前生效 API key 明文（供设置页以 ······· 回显、点眼睛图标显示明文）。

    本机单用户模式（平台自身无鉴权层），且 key 本就落盘本机 secrets 文件，
    回给本机前端无新增暴露面；严禁把 key 写入日志。
    """
    cfg = get_llm_config()
    return success({"api_key": cfg.api_key() or ""})


@router.put("/settings")
def update_llm_settings(payload: LLMSettingsUpdate):
    """更新 LLM 运行时配置（保存到 data/llm_config.json）；api_key 单独落到 secrets。"""
    cfg = get_llm_config()
    data = cfg.raw
    for key in ("provider", "model", "base_url", "temperature", "timeout", "max_tokens"):
        value = getattr(payload, key)
        if value is not None:
            data[key] = value
    if payload.prompts is not None:
        data.setdefault("prompts", {})
        data["prompts"].update(payload.prompts)
    cfg.save(data)

    # 空串 / null = 不修改（与前端"保持不变表示不修改"契约一致）。
    # 修复：旧版把空串当"清除"，改非 key 设置保存时会删掉已配置的 key。
    if payload.api_key is not None and payload.api_key.strip():
        save_secret("LLM_API_KEY", payload.api_key.strip())

    reload_llm_config()
    logger.info("LLM settings updated")
    return success({"saved": True})


@router.post("/test")
def test_llm_connection(payload: LLMTestRequest | None = None):
    """测试 LLM 连通性。优先使用请求体中的参数，否则使用当前配置。"""
    req = payload or LLMTestRequest()
    cfg = get_llm_config()
    result = test_connection(
        base_url=req.base_url or cfg.base_url(),
        model=req.model or cfg.model,
        api_key=req.api_key or cfg.api_key(),
        timeout=req.timeout or cfg.timeout,
    )
    return success(result)


@router.get("/env-hints")
def get_env_hints():
    """返回当前 .env 中读取到的非敏感 LLM 配置（用于前端回显 base_url 与 model，不返回 key）。"""
    return success({
        "LLM_BASE_URL": os.getenv("LLM_BASE_URL", ""),
        "LLM_ENDPOINT": os.getenv("LLM_ENDPOINT", ""),
        "LLM_MODEL": os.getenv("LLM_MODEL", ""),
        "LLM_API_KEY": bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")),
    })
