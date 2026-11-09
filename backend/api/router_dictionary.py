"""V2-01 数据字典路由（M1-A）：词条 CRUD + 同义词晋升 + 联邦查询（预览/执行）
+ 审计查询 + field_map 收敛（来源盘点/从连接导入）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.dependencies import project_id_param
from core.responses import ApiError, list_payload, parse_pagination, success
from dictionary import (
    create_term,
    delete_term,
    field_map_sources,
    federated_lookup,
    get_term,
    import_from_connections,
    list_audit,
    list_terms,
    promote_synonym,
    update_term,
)
from logging_setup import get_logger
from schemas.dictionary import (
    ImportFromConnectionsReq,
    SynonymAdd,
    TermCreate,
    TermLookupReq,
    TermUpdate,
)

router = APIRouter(prefix="/api/dictionary", tags=["dictionary"])
logger = get_logger("api.dictionary")


@router.get("/terms")
def api_list_terms(project_id: str = Depends(project_id_param)):
    items = list_terms(project_id)
    return success(list_payload(items, len(items), 1, len(items)))


@router.post("/terms", status_code=201)
def api_create_term(payload: TermCreate, project_id: str = Depends(project_id_param)):
    rec = create_term(project_id, payload.model_dump())
    return success(rec, http_status=201)


@router.get("/terms/{term_id}")
def api_get_term(term_id: str, project_id: str = Depends(project_id_param)):
    rec = get_term(project_id, term_id)
    if rec is None:
        raise ApiError("NOT_FOUND", f"词条 {term_id} 不存在", 404)
    return success(rec)


@router.put("/terms/{term_id}")
def api_update_term(term_id: str, payload: TermUpdate, project_id: str = Depends(project_id_param)):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    rec = update_term(project_id, term_id, data)
    return success(rec)


@router.delete("/terms/{term_id}")
def api_delete_term(term_id: str, project_id: str = Depends(project_id_param)):
    delete_term(project_id, term_id)
    return success({"deleted": term_id})


@router.post("/terms/{term_id}/synonyms")
def api_promote_synonym(term_id: str, payload: SynonymAdd, project_id: str = Depends(project_id_param)):
    rec = promote_synonym(project_id, term_id, payload.synonym)
    return success(rec)


@router.post("/term-lookup")
def api_term_lookup(payload: TermLookupReq, project_id: str = Depends(project_id_param)):
    """联邦查询：dry_run=true 返回成本预览；confirm_cost 控制全量拉取确认。"""
    res = federated_lookup(
        project_id, payload.term, value=payload.value, systems=payload.systems,
        limit=payload.limit, dry_run=payload.dry_run, confirm_cost=payload.confirm_cost,
        actor="rest",
    )
    logger.info("api.term_lookup project=%s term='%s' status=%s",
                project_id, payload.term, res.get("status"))
    return success(res)


@router.get("/term-lookup-audit")
def api_term_lookup_audit(
    project_id: str = Depends(project_id_param),
    page: int = Query(1, ge=1),
    pageSize: int = Query(50, ge=1, le=200),
):
    data = list_audit(project_id, page=page, page_size=pageSize)
    return success(list_payload(data["items"], total=data["total"], page=page, page_size=pageSize))


@router.post("/import-from-connections")
def api_import_from_connections(payload: ImportFromConnectionsReq,
                                project_id: str = Depends(project_id_param)):
    res = import_from_connections(project_id, payload.connection_ids)
    return success(res)


@router.get("/field-map-sources")
def api_field_map_sources(project_id: str = Depends(project_id_param)):
    """field_map 配置来源盘点：每个连接的 field_map 来自 explicit 还是 dictionary。"""
    items = field_map_sources(project_id)
    return success(list_payload(items, len(items), 1, len(items)))
