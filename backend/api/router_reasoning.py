"""V2-16 推理引擎配置接口（M4-C）。

- GET  /api/reasoning/engine：当前生效引擎（运行时配置 > 环境变量 > 缺省 local）＋Fuseki 连通状态；
- PUT  /api/reasoning/engine：运行时切换引擎（body {"engine": "local"|"fuseki"}；write 权限）；
- GET  /api/reasoning/scope/{ontology_id}：推理图范围检查（TC-16-04 负向断言）。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from api.dependencies import project_id_param
from core.responses import ApiError, success
from core.storage import JsonStore
from reasoning import engine as rengine
from tenant import ensure_schema_visible

router = APIRouter(prefix="/api/reasoning", tags=["reasoning"])


class EngineIn(BaseModel):
    engine: str


def _engine_state() -> dict[str, Any]:
    engine, source = rengine.get_configured_engine()
    from config import get_settings
    settings = get_settings()
    try:
        from jena_adapter.client import _is_reachable
        reachable = bool(_is_reachable())
    except Exception:  # noqa: BLE001 - 连通探测失败按不可达处理
        reachable = False
    return {
        "engine": engine,
        "source": source,
        "env_engine": os.environ.get("ONTOLOGY_REASONING_ENGINE"),
        "fuseki": {
            "base_url": settings.jena_base_url,
            "dataset": settings.jena_dataset,
            "reachable": reachable,
        },
    }


@router.get("/engine")
def get_engine(request: Request):
    """当前推理引擎状态（V2-16）。"""
    return success(_engine_state())


@router.put("/engine")
def set_engine(payload: EngineIn, request: Request):
    """运行时切换推理引擎（V2-16 TC-16-01）。"""
    if payload.engine not in rengine._VALID_ENGINES:
        raise ApiError("VALIDATION_ERROR", "engine 必须是 local 或 fuseki", http_status=422)
    rengine.set_engine(payload.engine)
    return success(_engine_state())


@router.get("/scope/{ontology_id}")
def reasoning_scope(
    ontology_id: str,
    request: Request,
    project_id: str = Depends(project_id_param),
):
    """推理图范围检查（V2-16 TC-16-04）：源连接型实例仅键/类型/关系快照在图内。"""
    rec = JsonStore("ontologies", project_id=project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, rec)
    return success(rengine.check_reasoning_scope(rec, project_id, ontology_id))
