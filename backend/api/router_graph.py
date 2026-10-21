"""可视化路由（UC6 / VS4）：图谱数据、推理链路与自然语言查询。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ai_helper import nl_to_sparql
from api.dependencies import project_id_param
from core.responses import ApiError, success
from core.storage import JsonStore
from jena_adapter.client import sparql_query
from logging_setup import get_logger
from reasoning import inference_trace
from viz import Viz

router = APIRouter(prefix="/api", tags=["viz"])
logger = get_logger("api.viz")
_viz = Viz()


def _store_for(project_id: str) -> JsonStore:
    return JsonStore("ontologies", project_id=project_id)


@router.get("/graph/{ontology_id}")
def graph_data(
    ontology_id: str,
    mode: str = Query(default="schema", pattern="^(schema|knowledge)$"),
    project_id: str = Depends(project_id_param),
):
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return success(_viz.graph_data(ontology_id, rec, mode=mode, project_id=project_id))


def _inference_trace_response(ontology_id: str, rec: dict[str, Any], focus: str | None = None) -> dict[str, Any]:
    steps = inference_trace(rec, focus)
    return {"ontology_id": ontology_id, "focus": focus, "steps": steps}


@router.get("/ontologies/{ontology_id}/inference")
def inference_trace_endpoint(ontology_id: str, focus: str | None = None, project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return success(_inference_trace_response(ontology_id, rec, focus))


@router.get("/inference/trace/{ontology_id}")
def inference_trace_by_id(ontology_id: str, project_id: str = Depends(project_id_param)):
    """推理链路接口别名，兼容 /api/inference/trace/{ontology_id} 调用。"""
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return success(_inference_trace_response(ontology_id, rec, focus=None))


@router.post("/graph/{ontology_id}/nl-query")
def natural_language_query(
    ontology_id: str,
    payload: dict,
    project_id: str = Depends(project_id_param),
):
    """自然语言 → SPARQL → Jena 查询（O-05）。"""
    question = (payload.get("question") or "").strip()
    if not question:
        raise ApiError("VALIDATION_ERROR", "question 不能为空", http_status=422)
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    try:
        sparql = nl_to_sparql(question, rec)
    except Exception as e:  # noqa: BLE001
        logger.warning("nl_to_sparql failed: %s", e)
        raise ApiError("LLM_ERROR", f"自然语言转 SPARQL 失败: {e}", http_status=502) from e
    try:
        rows = sparql_query(sparql, project_id=project_id, ontology_id=ontology_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("sparql_query failed: %s", e)
        raise ApiError("UPSTREAM_UNAVAILABLE", f"SPARQL 执行失败: {e}", http_status=502) from e
    logger.info("nl_query ontology_id=%s project_id=%s rows=%d", ontology_id, project_id, len(rows))
    return success({"question": question, "sparql": sparql, "rows": rows, "count": len(rows)})


@router.post("/graph/{ontology_id}/rdf-sync")
def sync_ontology_rdf(ontology_id: str, project_id: str = Depends(project_id_param)):
    """手动重建本体全量图（schema+实例）并写入 RDF 存储（回填/修复入口）。"""
    from jena_adapter import rdf_sync

    if not _store_for(project_id).get(ontology_id):
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    try:
        result = rdf_sync.sync_ontology(project_id, ontology_id)
    except Exception as e:  # noqa: BLE001
        raise ApiError("BAD_GATEWAY", f"RDF 同步失败: {e}", http_status=502)
    logger.info("rdf_sync manual ontology_id=%s project_id=%s mode=%s", ontology_id, project_id, result.get("mode"))
    return success(result)
