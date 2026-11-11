"""V2-14i 身份联邦路由（M2-B）。

端点契约见 docs/API-INTERFACE-DOC.md §23。凭据明文仅一次性经请求传入，平台只存引用 + 本地加密；
所有响应不回显 secret_ref / secret。鉴权：本人可管理自己的映射；管理员可管理任意用户。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

import auth as auth_svc
from api.dependencies import project_id_param
from core.responses import ApiError, list_payload, parse_pagination, success
from identity_federation import (
    ALLOWED_AUTH_TYPES,
    IdentityError,
    IdentityMappingMissing,
    create_mapping,
    delete_mapping,
    get_mapping,
    list_mappings,
)

router = APIRouter(prefix="/api/identity", tags=["identity"])


def _current_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "auth_user", None)
    if user is None:
        if not auth_svc.auth_enabled():
            return {"id": "dev", "username": "dev", "role": "admin", "grants": [], "disabled": False, "locked": False}
        raise ApiError("UNAUTHORIZED", "未登录或会话已失效", http_status=401)
    return user


def _require_self_or_admin(actor: dict[str, Any], target_user_id: str) -> None:
    if actor.get("role") != "admin" and target_user_id != actor.get("id"):
        raise ApiError("FORBIDDEN", "仅管理员可管理其他用户的凭据映射", http_status=403)


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    """去除敏感字段（secret_ref）后返回；绝不回显凭据引用或明文。"""
    r = dict(rec)
    r.pop("secret_ref", None)
    return r


class MappingIn(BaseModel):
    user_id: str
    connection_id: str
    auth_type: str
    principal: Optional[str] = None
    secret: str  # 一次性传入，绝不回显、绝不入平台存储


@router.post("/mappings", status_code=201)
def api_create_mapping(payload: MappingIn, request: Request, project_id: str = Depends(project_id_param)):
    actor = _current_user(request)
    _require_self_or_admin(actor, payload.user_id)
    try:
        rec = create_mapping(
            project_id,
            user_id=payload.user_id,
            connection_id=payload.connection_id,
            auth_type=payload.auth_type,
            principal=payload.principal,
            secret=payload.secret,
            actor=actor.get("username"),
        )
    except IdentityError as e:
        raise ApiError(e.code, str(e), e.http_status) from e
    return success(_public(rec), http_status=201)


@router.get("/mappings")
def api_list_mappings(
    request: Request,
    project_id: str = Depends(project_id_param),
    user_id: Optional[str] = Query(default=None),
    connection_id: Optional[str] = Query(default=None),
):
    actor = _current_user(request)
    eff_user = user_id or actor.get("id")
    _require_self_or_admin(actor, eff_user)
    items = list_mappings(project_id, user_id=eff_user, connection_id=connection_id)
    items = [_public(i) for i in items]
    page, page_size = parse_pagination(1, max(len(items), 1))
    return success(list_payload(items, len(items), page, page_size))


@router.get("/mappings/{mapping_id}")
def api_get_mapping(mapping_id: str, request: Request, project_id: str = Depends(project_id_param)):
    actor = _current_user(request)
    rec = get_mapping(project_id, mapping_id)
    if not rec:
        raise ApiError("NOT_FOUND", "映射不存在", http_status=404)
    _require_self_or_admin(actor, rec.get("user_id"))
    return success(_public(rec))


@router.get("/mappings/{mapping_id}/resolve-status")
def api_resolve_status(mapping_id: str, request: Request, project_id: str = Depends(project_id_param)):
    """仅返回映射完整性（auth_type/principal/has_secret），不返回任何凭据值，供前端展示。"""
    actor = _current_user(request)
    rec = get_mapping(project_id, mapping_id)
    if not rec:
        raise ApiError("NOT_FOUND", "映射不存在", http_status=404)
    _require_self_or_admin(actor, rec.get("user_id"))
    from credential_store import load_secret
    ref = rec.get("secret_ref")
    return success({
        "mapping_id": mapping_id,
        "auth_type": rec.get("auth_type"),
        "principal": rec.get("principal"),
        "has_secret": bool(ref and load_secret(ref)),
    })


@router.delete("/mappings/{mapping_id}")
def api_delete_mapping(mapping_id: str, request: Request, project_id: str = Depends(project_id_param)):
    actor = _current_user(request)
    rec = get_mapping(project_id, mapping_id)
    if not rec:
        raise ApiError("NOT_FOUND", "映射不存在", http_status=404)
    _require_self_or_admin(actor, rec.get("user_id"))
    ok = delete_mapping(project_id, mapping_id, actor=actor.get("username"))
    return success({"deleted": ok})


@router.get("/auth-types")
def api_auth_types():
    """返回支持的凭据类型清单（供前端下拉）。"""
    return success({"items": sorted(ALLOWED_AUTH_TYPES), "total": len(ALLOWED_AUTH_TYPES)})
