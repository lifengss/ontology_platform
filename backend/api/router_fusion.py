"""V2-02 跨本体对齐与融合 — 融合 API（M4-A③）：融合申请/会签/执行/虚拟融合查询。

端点：
- POST /api/fusion/requests                发起融合申请（源本体≥2，已发布；等价映射自动推导或显式提供；会签=除发起人外全部源本体 owner）
- GET  /api/fusion/requests                融合申请列表（?status= 过滤；分页）
- GET  /api/fusion/requests/{fusion_id}    融合申请详情（含等价映射与会签进度）
- POST /api/fusion/requests/{fusion_id}/sign        会签（approve/reject；一票否决；owner 缺位 admin 代行）
- POST /api/fusion/requests/{fusion_id}/withdraw    撤回（发起人/管理员，pending/approved）
- POST /api/fusion/requests/{fusion_id}/execute     执行融合（默认虚拟执行；body.materialize=true 显式物化派生本体；无变化则不变版本）
- PUT  /api/fusion/requests/{fusion_id}/mappings    调整等价映射（已物化则自动产生新版本）
- POST /api/fusion/query                   虚拟融合查询（只读；同键合并双来源标记/冲突双值并存或权威源取值）
"""
from __future__ import annotations

import fusion
from fastapi import APIRouter, Depends, Request
from fastapi import status as http_status
from pydantic import BaseModel, Field

from api.dependencies import project_id_param
from core.responses import ApiError, list_payload, success
from tenant import current_user, ensure_project_access

router = APIRouter(prefix="/api/fusion", tags=["fusion"])


class _ObjectRef(BaseModel):
    ontology_id: str
    object_id: str


class _AttrRef(BaseModel):
    ontology_id: str
    attr_id: str


class _GroupIn(BaseModel):
    canonical_name: str = ""
    objects: list[_ObjectRef] = Field(..., min_length=2)
    attribute_pairs: list[list[_AttrRef]] = []


class FusionCreateIn(BaseModel):
    name: str | None = None
    source_ontologies: list[str] = Field(..., min_length=2)
    object_groups: list[_GroupIn] | None = None


class FusionSignIn(BaseModel):
    decision: str
    comment: str | None = None
    proxy_reason: str | None = None


class FusionMappingsIn(BaseModel):
    object_groups: list[_GroupIn] = Field(..., min_length=1)


class FusionExecuteIn(BaseModel):
    note: str | None = None
    # TP-02-12：物化快照默认关闭；显式 true 才物化派生本体（派生数据，非权威）
    materialize: bool = False


class FusionQueryIn(BaseModel):
    fusion_id: str | None = None
    fusion_ontology_id: str | None = None
    object_type: str | None = None
    stable_key_value: str | None = None
    version: str | None = None
    limit: int = 100


def _err(e: fusion.FusionError) -> ApiError:
    return ApiError(e.code, e.message, e.http_status)


@router.post("/requests")
def api_create_fusion(request: Request, body: FusionCreateIn,
                      project_id: str = Depends(project_id_param)):
    """发起融合申请：等价映射默认由已生效对齐声明推导，可显式提供覆盖。"""
    user = current_user(request)
    try:
        rec = fusion.create_fusion_request(
            request, project_id, user,
            body.name, body.source_ontologies,
            [g.model_dump() for g in (body.object_groups or [])] or None)
    except fusion.FusionError as e:
        raise _err(e)
    return success(rec)


@router.get("/requests")
def api_list_fusion(request: Request,
                    status: str | None = None,
                    page: int = 1, page_size: int = 50,
                    project_id: str = Depends(project_id_param)):
    user = current_user(request)
    items = fusion.list_fusion_requests(project_id, user, status)
    total = len(items)
    p = max(1, page)
    start = (p - 1) * page_size
    return success(list_payload(items[start:start + page_size], total, p, page_size))


@router.get("/requests/{fusion_id}")
def api_get_fusion(request: Request, fusion_id: str,
                   project_id: str = Depends(project_id_param)):
    user = current_user(request)
    try:
        rec = fusion.get_fusion_request(project_id, user, fusion_id)
    except fusion.FusionError as e:
        raise _err(e)
    return success(rec)


@router.post("/requests/{fusion_id}/sign")
def api_sign_fusion(request: Request, fusion_id: str, body: FusionSignIn,
                    project_id: str = Depends(project_id_param)):
    """会签：approve/reject，一票否决；owner 缺位时租户管理员可代行（须给理由）。"""
    user = current_user(request)
    try:
        rec = fusion.sign_fusion_request(request, project_id, user, fusion_id,
                                         body.decision, body.comment, body.proxy_reason)
    except fusion.FusionError as e:
        raise _err(e)
    return success(rec)


@router.post("/requests/{fusion_id}/withdraw")
def api_withdraw_fusion(request: Request, fusion_id: str,
                        project_id: str = Depends(project_id_param)):
    user = current_user(request)
    try:
        rec = fusion.withdraw_fusion_request(request, project_id, user, fusion_id)
    except fusion.FusionError as e:
        raise _err(e)
    return success(rec)


@router.post("/requests/{fusion_id}/execute")
def api_execute_fusion(request: Request, fusion_id: str, body: FusionExecuteIn,
                       project_id: str = Depends(project_id_param)):
    """执行融合：默认虚拟执行（校验模型＋变更预演，不物化，TP-02-12）；
    materialize=true 显式物化：首次创建派生融合本体（1.0，标注"派生数据，非权威"），
    重执行按源当前状态重建并产生新版本。"""
    user = current_user(request)
    try:
        out = fusion.execute_fusion(request, project_id, user, fusion_id,
                                    body.note, materialize=body.materialize)
    except fusion.FusionError as e:
        raise _err(e)
    return success(out)


@router.put("/requests/{fusion_id}/mappings")
def api_update_mappings(request: Request, fusion_id: str, body: FusionMappingsIn,
                        project_id: str = Depends(project_id_param)):
    """调整等价映射（映射演化即版本演化；已物化则立即产生融合本体新版本）。"""
    user = current_user(request)
    try:
        out = fusion.update_fusion_mappings(
            request, project_id, user, fusion_id,
            [g.model_dump() for g in body.object_groups])
    except fusion.FusionError as e:
        raise _err(e)
    return success(out)


@router.post("/query")
def api_fusion_query(request: Request, body: FusionQueryIn,
                     project_id: str = Depends(project_id_param)):
    """虚拟融合查询（只读）：等价映射 join＋键对齐合并＋分渠道取数；实例/快照不动。"""
    user = current_user(request)
    try:
        out = fusion.fusion_query(
            project_id, user,
            fusion_id=body.fusion_id,
            fusion_ontology_id=body.fusion_ontology_id,
            object_type=body.object_type,
            stable_key_value=body.stable_key_value,
            version=body.version,
            limit=body.limit)
    except fusion.FusionError as e:
        raise _err(e)
    return success(out)
