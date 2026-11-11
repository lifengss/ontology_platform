"""V2-04 能力层路由（M1-B 首片）：能力 CRUD + 定义校验 + 版本化发布 + 依赖 + 单实例/批量执行。

所有接口遵循统一信封（core.responses）；业务错误经 CapabilityApiError → ApiError。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status

from api.dependencies import project_id_param
from capabilities import CapabilityApiError, CapabilityService
from core.responses import ApiError, list_payload, success
from logging_setup import get_logger
from schemas.capability import (
    CapabilityCreate,
    CapabilityExecute,
    CapabilityExecuteBatch,
    CapabilityUpdate,
)

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])
logger = get_logger("api.capabilities")


def _svc(project_id: str) -> CapabilityService:
    return CapabilityService(project_id)


def _raise_api(err: CapabilityApiError) -> None:
    """业务错误 → ApiError（保留稳定错误码与 HTTP 状态）。"""
    details = err.details if isinstance(err.details, dict) else None
    raise ApiError(err.code, err.message, err.http_status, details)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
@router.get("")
def api_list_capabilities(
    status: Optional[str] = Query(default=None, pattern="^(draft|active|deprecated)$"),
    object_type: Optional[str] = Query(default=None, max_length=64),
    q: Optional[str] = Query(default=None, max_length=128),
    project_id: str = Depends(project_id_param),
):
    items = _svc(project_id).list_capabilities(status=status, object_type=object_type, q=q)
    return success(list_payload(items, len(items), 1, len(items)))


@router.post("", status_code=201)
def api_create_capability(payload: CapabilityCreate, project_id: str = Depends(project_id_param)):
    try:
        rec = _svc(project_id).create_capability(payload.model_dump())
    except CapabilityApiError as e:
        _raise_api(e)
    return success(rec, http_status=201)


@router.get("/{cap_id}")
def api_get_capability(cap_id: str, project_id: str = Depends(project_id_param)):
    rec = _svc(project_id).get_capability(cap_id)
    if not rec:
        raise ApiError("NOT_FOUND", "能力不存在", http_status.HTTP_404_NOT_FOUND)
    return success(rec)


@router.put("/{cap_id}")
def api_update_capability(cap_id: str, payload: CapabilityUpdate, project_id: str = Depends(project_id_param)):
    try:
        rec = _svc(project_id).update_capability(cap_id, payload.model_dump(exclude_unset=True))
    except CapabilityApiError as e:
        _raise_api(e)
    return success(rec)


@router.delete("/{cap_id}")
def api_delete_capability(cap_id: str, project_id: str = Depends(project_id_param)):
    try:
        _svc(project_id).delete_capability(cap_id)
    except CapabilityApiError as e:
        _raise_api(e)
    return success({"deleted": cap_id})


# --------------------------------------------------------------------------- #
# 校验 / 版本化 / 依赖
# --------------------------------------------------------------------------- #
@router.post("/{cap_id}/validate")
def api_validate_capability(
    cap_id: str,
    draft_expression: Optional[str] = Query(default=None, max_length=8192),
    project_id: str = Depends(project_id_param),
):
    try:
        res = _svc(project_id).validate_capability(cap_id, draft_expression)
    except CapabilityApiError as e:
        _raise_api(e)
    return success(res)


@router.get("/{cap_id}/validate")
def api_validate_capability_get(
    cap_id: str,
    draft_expression: Optional[str] = Query(default=None, max_length=8192),
    project_id: str = Depends(project_id_param),
):
    try:
        res = _svc(project_id).validate_capability(cap_id, draft_expression)
    except CapabilityApiError as e:
        _raise_api(e)
    return success(res)


@router.post("/{cap_id}/publish")
def api_publish_capability(cap_id: str, project_id: str = Depends(project_id_param)):
    try:
        rec = _svc(project_id).publish_capability(cap_id)
    except CapabilityApiError as e:
        _raise_api(e)
    return success(rec)


@router.get("/{cap_id}/versions")
def api_list_versions(cap_id: str, project_id: str = Depends(project_id_param)):
    rec = _svc(project_id).get_capability(cap_id)
    if not rec:
        raise ApiError("NOT_FOUND", "能力不存在", http_status.HTTP_404_NOT_FOUND)
    items = _svc(project_id).list_versions(cap_id)
    return success(list_payload(items, len(items), 1, len(items)))


@router.get("/{cap_id}/dependents")
def api_list_dependents(cap_id: str, project_id: str = Depends(project_id_param)):
    items = _svc(project_id).list_dependents(cap_id)
    return success(list_payload(items, len(items), 1, len(items)))


@router.get("/{cap_id}/affected-instances")
def api_affected_instances(
    cap_id: str,
    instance_id: str = Query(..., description="被修改的基线实例 ID"),
    project_id: str = Depends(project_id_param),
):
    """HF-04：反查哪些结果实例的计算曾命中这条基线，并标记 stale（基线变更后未重算）。"""
    try:
        res = _svc(project_id).affected_instances(cap_id, instance_id)
    except CapabilityApiError as e:
        _raise_api(e)
    return success(res)


# --------------------------------------------------------------------------- #
# 执行（单实例 / 批量 + 四要素溯源 + 写回）
# --------------------------------------------------------------------------- #
@router.post("/{cap_id}/execute")
def api_execute_capability(cap_id: str, payload: CapabilityExecute, project_id: str = Depends(project_id_param)):
    try:
        res = _svc(project_id).execute(
            cap_id, payload.instance_id, channel=payload.channel,
            trigger_source=payload.trigger_source, dry_run=payload.dry_run,
            inputs=payload.inputs,
        )
    except CapabilityApiError as e:
        _raise_api(e)
    return success(res)


@router.post("/{cap_id}/execute-batch")
def api_execute_batch_capability(
    cap_id: str, payload: CapabilityExecuteBatch, project_id: str = Depends(project_id_param),
):
    try:
        res = _svc(project_id).execute_batch(
            cap_id,
            limit=payload.limit,
            offset=payload.offset,
            instance_ids=payload.instance_ids,
            channel=payload.channel,
            trigger_source=payload.trigger_source,
            dry_run=payload.dry_run,
        )
    except CapabilityApiError as e:
        _raise_api(e)
    return success(res)
