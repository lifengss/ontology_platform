"""V2-17 SHACL 执行路径引擎配置接口（M4-D）。

- GET /api/shacl/engine：当前生效引擎（运行时配置 > 环境变量 > 缺省 local）＋Jena 执行器可用性；
- PUT /api/shacl/engine：运行时切换引擎（body {"engine": "local"|"jena"}；write 权限）。

诚实口径（依赖事实，2026-11-16 实测）：Fuseki standalone fat jar 无 SHACL HTTP 服务端点，
Jena 侧 = Java 子进程执行 fat jar 内置 Jena SHACL 引擎（同一 Jena/RDF 栈、同一形状文件）；
每次校验含 JVM 启动＋文件序列化开销，时延基线如实记录（TC-17-05），不断言 Jena 快于本地。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.responses import ApiError, success
from jena_adapter import shacl_engine
from jena_adapter.shacl_jena import java_shacl_available

router = APIRouter(prefix="/api/shacl", tags=["shacl"])


class EngineIn(BaseModel):
    engine: str


def _engine_state() -> dict[str, Any]:
    engine, source = shacl_engine.get_configured_engine()
    ok, why = java_shacl_available()
    return {
        "engine": engine,
        "source": source,
        "env_engine": os.environ.get(shacl_engine.ENV_VAR),
        "jena": {"available": ok, "detail": why},
    }


@router.get("/engine")
def get_engine(request: Request):
    """当前 SHACL 执行路径状态（V2-17）。"""
    return success(_engine_state())


@router.put("/engine")
def set_engine(payload: EngineIn, request: Request):
    """运行时切换 SHACL 执行路径（V2-17 正常流步骤 1）。"""
    try:
        shacl_engine.set_engine(payload.engine.strip().lower())
    except ValueError as e:
        raise ApiError("VALIDATION_ERROR", str(e), http_status=422)
    return success(_engine_state())
