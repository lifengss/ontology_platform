"""实例归化流水线路由（UC2 / VS3）。

铁律：LLM 输出禁止直落库。预览生成候选（LLM/字段映射），
审核通过 → SHACL 校验 → 落 Neo4j（不可用时降级本地 JSONL）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query

from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from ingestion import approve as approve_candidate_logic
from ingestion import preview as preview_candidate
from logging_setup import get_logger
from modeling_translator import validate_business_layer
from schemas.ingest import ApproveResult, IngestPreviewRequest

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
logger = get_logger("api.ingest")
_candidates = JsonStore("ingest_candidates")
_ontologies = JsonStore("ontologies")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_ontology(ontology_id: str) -> dict:
    rec = _ontologies.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return rec


@router.post("/preview")
def preview(payload: IngestPreviewRequest):
    ontology = _load_ontology(payload.ontology_id)
    # 候选生成前先确认本体模型自身合法（防御脏数据进入归化）
    biz_errors = validate_business_layer(ontology)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "本体模型未通过业务层校验，无法生成候选", http_status=422,
                       details={"violations": biz_errors})

    candidate = preview_candidate(payload, ontology)
    rec = {
        "id": uuid.uuid4().hex,
        "ontology_id": candidate["ontology_id"],
        "source_type": candidate["source_type"],
        "source_ref": candidate["source_ref"],
        "object_type": candidate["object_type"],
        "instances": candidate["instances"],
        "status": "pending",
        "created_at": _now(),
        "approve_result": None,
    }
    _candidates.insert(rec)
    logger.info("ingest preview candidate_id=%s source=%s instances=%d",
                rec["id"], rec["source_type"], len(rec["instances"]))
    return success(rec, http_status=201)


@router.get("/candidates")
def list_candidates(
    ontology_id: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
):
    page, page_size = parse_pagination(page, page_size)
    items = _candidates.all()
    if ontology_id:
        items = [c for c in items if c.get("ontology_id") == ontology_id]
    if status:
        items = [c for c in items if c.get("status") == status]
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str):
    rec = _candidates.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    return success(rec)


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: str):
    rec = _candidates.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    if rec.get("status") == "imported":
        return success(rec.get("approve_result") or ApproveResult(imported=0, rejected=0, violations=[], neo_tx_id=None).model_dump())
    if rec.get("status") == "rejected":
        raise ApiError("VALIDATION_ERROR", "已拒绝的候选不能重新审批", http_status=422)

    ontology = _load_ontology(rec["ontology_id"])
    result = approve_candidate_logic(rec, ontology)

    if result["imported"] > 0:
        rec["status"] = "imported"
    # 若 SHACL 失败，保持 pending 状态，便于用户修正后重新审批
    rec["approve_result"] = result
    _candidates.update(candidate_id, rec)
    logger.info("approve candidate_id=%s imported=%d violations=%d",
                candidate_id, result["imported"], len(result["violations"]))
    return success(result)


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: str):
    rec = _candidates.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    if rec.get("status") == "imported":
        raise ApiError("FORBIDDEN_WRITE", "已落库的候选不能拒绝", http_status=403)
    _candidates.update(candidate_id, {"status": "rejected"})
    logger.info("reject candidate_id=%s", candidate_id)
    return success({"status": "rejected"})
