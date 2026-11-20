"""可视化路由（UC6 / VS4）：图谱数据、推理链路与自然语言查询。

V2-06：本体级读路径统一执行 schema 可见性（不可见=404，不泄露存在性）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from ai_helper import nl_to_sparql
from api.dependencies import project_id_param
from core.responses import ApiError, success
from core.storage import JsonStore
from jena_adapter.client import sparql_query
from logging_setup import get_logger
from tenant import ensure_schema_visible
from viz import Viz

router = APIRouter(prefix="/api", tags=["viz"])
logger = get_logger("api.viz")
_viz = Viz()


def _store_for(project_id: str) -> JsonStore:
    return JsonStore("ontologies", project_id=project_id)


def _instance_store_for(project_id: str):
    from core.instance_store import get_instance_store

    return get_instance_store(project_id=project_id)


# GAP-17/19：图谱响应缓存（见 graph_data 注释）
_GRAPH_CACHE: dict[tuple, dict] = {}


@router.get("/graph/{ontology_id}")
def graph_data(
    ontology_id: str,
    request: Request,
    mode: str = Query(default="schema", pattern="^(schema|knowledge)$"),
    max_instances: int = Query(default=300, ge=0, description="knowledge 视图实例抽样上限，0=不限制（大图性能差）"),
    project_id: str = Depends(project_id_param),
):
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, rec)
    # GAP-17/19：图谱响应缓存——实例数据版本（快照聚合签名）或本体 updated_at 变化即失效；
    # 大图（5 万实例）graph_data 全量构造 ~秒级，缓存命中 O(1)（数据 dict 只读，序列化不改写）
    _ver, _snap = _instance_store_for(project_id).snapshot()
    gkey = (project_id, ontology_id, mode, max_instances, _ver, rec.get("updated_at"))
    hit = _GRAPH_CACHE.get(gkey)
    if hit is not None:
        return success(hit)
    data = _viz.graph_data(ontology_id, rec, mode=mode, project_id=project_id, max_instances=max_instances)
    if len(_GRAPH_CACHE) > 8:
        _GRAPH_CACHE.clear()
    _GRAPH_CACHE[gkey] = data
    return success(data)


@router.get("/graph/{ontology_id}/neighbors")
def graph_neighbors(
    ontology_id: str,
    request: Request,
    node_id: str = Query(..., min_length=1, description="焦点节点 id（对象 id 或实例 id）"),
    mode: str = Query(default="schema", pattern="^(schema|knowledge)$"),
    depth: int = Query(default=1, ge=1, le=2, description="对象关系 BFS 层级（1~2）"),
    limit: int = Query(default=50, ge=0, le=200, description="实例邻居抽样上限，0=不截断"),
    project_id: str = Depends(project_id_param),
):
    """按需邻居查询（GAP-12）：大图不一次拉全图，前端双击节点增量展开。"""
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, rec)
    data = _viz.neighbors(ontology_id, rec, node_id, mode=mode, project_id=project_id, depth=depth, limit=limit)
    if data is None:
        raise ApiError("NOT_FOUND", "节点不存在", http_status=404)
    return success(data)


@router.get("/graph/{ontology_id}/consistency")
def graph_consistency(ontology_id: str, request: Request, project_id: str = Depends(project_id_param)):
    """GAP-15：本地 TTL 与模型/实例数据一致性对账（轻量 mtime 对比；后台对账循环自动修复漂移）。"""
    from jena_adapter import rdf_sync

    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, rec)
    return success(rdf_sync.check_consistency(project_id, ontology_id))


def _inference_trace_response(ontology_id: str, rec: dict[str, Any], focus: str | None = None,
                              project_id: str = "", engine: str | None = None) -> dict[str, Any]:
    from reasoning import engine as rengine
    # V2-16：双引擎推理（local / fuseki），Fuseki 失败自动降级本地
    data = rengine.run_inference(rec, project_id, ontology_id, focus=focus, engine=engine)
    return {"ontology_id": ontology_id, "focus": focus, **data}


@router.get("/ontologies/{ontology_id}/inference")
def inference_trace_endpoint(ontology_id: str, request: Request, focus: str | None = None,
                             engine: str | None = Query(default=None, pattern="^(local|fuseki)$",
                                                       description="V2-16 推理引擎：local / fuseki，缺省按运行时配置"),
                             project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, rec)
    return success(_inference_trace_response(ontology_id, rec, focus, project_id, engine))


@router.get("/inference/trace/{ontology_id}")
def inference_trace_by_id(ontology_id: str, engine: str | None = Query(default=None, pattern="^(local|fuseki)$"),
                          project_id: str = Depends(project_id_param)):
    """推理链路接口别名，兼容 /api/inference/trace/{ontology_id} 调用。"""
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return success(_inference_trace_response(ontology_id, rec, focus=None, project_id=project_id, engine=engine))


@router.post("/inference/impact")
def impact_inference_endpoint(
    payload: dict,
    project_id: str = Depends(project_id_param),
):
    """3.1 业务影响推理：实例值变化 → 下游实例影响（确定性规则 R1 直接引用 + R2 时间链传导）。

    body: { ontology_id, instance_id, attribute, new_value }
    """
    from core.instance_store import get_instance_store
    from reasoning.impact_inference import infer_impact

    ontology_id = (payload.get("ontology_id") or "").strip()
    instance_id = (payload.get("instance_id") or "").strip()
    attribute = (payload.get("attribute") or "").strip()
    new_value = payload.get("new_value")
    if not (ontology_id and instance_id and attribute):
        raise ApiError("VALIDATION_ERROR", "ontology_id / instance_id / attribute 均不能为空", http_status=422)
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    instances = get_instance_store(project_id).all()
    try:
        result = infer_impact(rec, instances, instance_id, attribute, str(new_value))
    except KeyError as e:
        raise ApiError("NOT_FOUND", "实例不存在", http_status=404) from e
    logger.info("impact_inference ontology_id=%s instance_id=%s project_id=%s", ontology_id, instance_id, project_id)
    return success(result)


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
        sparql, source = nl_to_sparql(question, rec)
    except Exception as e:  # noqa: BLE001
        logger.warning("nl_to_sparql failed: %s", e)
        raise ApiError("LLM_ERROR", f"自然语言转 SPARQL 失败: {e}", http_status=502) from e
    try:
        rows = sparql_query(sparql, project_id=project_id, ontology_id=ontology_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("sparql_query failed: %s", e)
        raise ApiError("UPSTREAM_UNAVAILABLE", f"SPARQL 执行失败: {e}", http_status=502) from e
    logger.info("nl_query ontology_id=%s project_id=%s source=%s question=%r rows=%d sparql=%.200s",
                ontology_id, project_id, source, question, len(rows), sparql)
    return success({"question": question, "sparql": sparql, "source": source, "rows": rows, "count": len(rows)})


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
