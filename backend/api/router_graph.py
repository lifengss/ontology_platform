"""可视化路由（UC6 / VS4）：图谱数据与推理链路。"""
from __future__ import annotations

from fastapi import APIRouter

from core.responses import ApiError, success
from core.storage import JsonStore
from logging_setup import get_logger
from reasoning import inference_trace
from viz import Viz

router = APIRouter(prefix="/api", tags=["viz"])
logger = get_logger("api.viz")
_store = JsonStore("ontologies")
_viz = Viz()


@router.get("/graph/{ontology_id}")
def graph_data(ontology_id: str):
    rec = _store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return success(_viz.graph_data(ontology_id, rec))


@router.get("/inference/trace/{ontology_id}")
def inference_trace_endpoint(ontology_id: str, focus: str | None = None):
    rec = _store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    steps = inference_trace(rec, focus)
    return success({"ontology_id": ontology_id, "focus": focus, "steps": steps})
