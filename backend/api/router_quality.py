"""V2-21 数据健康度路由（M3-C）：四维评估 / 当前健康 / 趋势快照（质量看板数据源）。

与 MCP 工具 quality_health、前端「数据健康度」tab 共用 data_quality.evaluate_ontology()，
保证 API / MCP / UI 三通道同一指标口径。指标观测性、不阻断；恒伴随规模（scale）展示。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi import status as http_status

import audit
from api.dependencies import project_id_param
from core.responses import ApiError, success
from data_quality import (
    QualityNotFound,
    evaluate_ontology,
    latest_snapshot,
    list_snapshots,
)
from logging_setup import get_logger
from tenant import current_user, project_tenant_guard  # V2-06 租户隔离 / V2-21 审计 actor

router = APIRouter(prefix="/api/quality", tags=["quality"], dependencies=[Depends(project_tenant_guard)])
logger = get_logger("api.quality")


def _actor(request: Request) -> str:
    return (current_user(request) or {}).get("username", "-")


@router.post("/ontologies/{ontology_id}/evaluate")
def api_evaluate(request: Request, ontology_id: str, project_id: str = Depends(project_id_param)):
    """即时评估四维并落快照（趋势线新增一个点）。"""
    actor = _actor(request)
    try:
        snap = evaluate_ontology(project_id, ontology_id, actor=actor, save=True)
    except QualityNotFound:
        raise ApiError("NOT_FOUND", "本体不存在", http_status.HTTP_404_NOT_FOUND)
    audit.record(action="quality_evaluated", actor=actor, project_id=project_id,
                 target_type="ontology", target_id=ontology_id,
                 detail={"snapshot_id": snap["id"], "scale": snap["scale"]["instance_count"]})
    return success(snap)


@router.get("/ontologies/{ontology_id}/health")
def api_health(ontology_id: str, project_id: str = Depends(project_id_param)):
    """当前健康度：最新快照；无快照时即时计算（不落快照，is_snapshot=false）。"""
    snap = latest_snapshot(project_id, ontology_id)
    if snap is not None:
        snap = {**snap, "is_snapshot": True}
        return success(snap)
    try:
        live = evaluate_ontology(project_id, ontology_id, actor="-", save=False)
    except QualityNotFound:
        raise ApiError("NOT_FOUND", "本体不存在", http_status.HTTP_404_NOT_FOUND)
    live = {**live, "is_snapshot": False}
    return success(live)


@router.get("/ontologies/{ontology_id}/snapshots")
def api_snapshots(ontology_id: str, limit: int = 50, project_id: str = Depends(project_id_param)):
    """质量趋势线：快照序列（旧→新，附 caliber_changed/scale_changed/is_baseline 标注）。"""
    if get_ontology_safe(project_id, ontology_id) is None:
        raise ApiError("NOT_FOUND", "本体不存在", http_status.HTTP_404_NOT_FOUND)
    data = list_snapshots(project_id, ontology_id, limit=limit)
    if not data["items"]:
        data["baseline_hint"] = "尚无快照：POST /api/quality/ontologies/{id}/evaluate 建立基线（首次全量入库）"
    return success(data)


def get_ontology_safe(project_id: str, ontology_id: str):
    from data_quality import get_ontology
    return get_ontology(project_id, ontology_id)
