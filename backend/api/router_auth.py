"""V2-05 登录鉴权与 RBAC 路由（M2-A）。

端点契约见 docs/API-INTERFACE-DOC.md §21。所有响应走 core.responses 统一信封。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

import auth as auth_svc
from api.dependencies import project_id_param
from core.responses import ApiError, fail, list_payload, parse_pagination, success
from schemas.auth import GrantIn, LoginReq, UserCreate, UserPatch

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _current_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "auth_user", None)
    if user is None:
        raise ApiError("UNAUTHORIZED", "未登录或会话已失效", http_status=401)
    return user


def _dev_identity() -> dict[str, Any]:
    return {
        "id": "dev",
        "username": "dev",
        "role": "admin",
        "grants": [],
        "disabled": False,
        "locked": False,
        "created_at": None,
        "updated_at": None,
    }


def _cookie_secure() -> bool:
    return os.getenv("ONTOLOGY_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on")


@router.post("/login")
def api_login(payload: LoginReq, request: Request, mode: str | None = Query(default=None)):
    """账号密码登录。默认只下发 httpOnly Cookie；`?mode=bearer` 额外返回 token 供非浏览器客户端。"""
    auth_svc.ensure_bootstrap()
    user, err = auth_svc.authenticate(payload.username.strip(), payload.password)
    if user is None:
        return fail(ApiError("INVALID_CREDENTIALS", auth_svc.UNIFORM_LOGIN_ERROR, http_status=401))
    token, exp = auth_svc.issue_token(user)
    data: dict[str, Any] = {
        "authenticated": True,
        "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat(),
        "user": auth_svc._public_user(user),
        "permissions": auth_svc.permission_matrix(user),
        "cookie_session": mode != "bearer",
    }
    if mode == "bearer":
        data["token"] = token
    resp = success(data)
    if mode != "bearer":
        resp.set_cookie(
            auth_svc.COOKIE_NAME,
            token,
            max_age=auth_svc.TOKEN_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(),
            path="/",
        )
    return resp


@router.post("/logout")
def api_logout(request: Request):
    cookie_token = request.cookies.get(auth_svc.COOKIE_NAME)
    token = auth_svc.extract_token(cookie_token, request.headers.get("authorization"))
    claims = auth_svc.decode_jwt(token) if token else None
    if claims:
        auth_svc.revoke_jti(claims["jti"], int(claims["exp"]))
        auth_svc.audit("logout", actor=claims.get("sub", "-"), target=claims["jti"])
    resp = success({"logged_out": True})
    resp.delete_cookie(auth_svc.COOKIE_NAME, path="/")
    return resp


@router.get("/me")
def api_me(request: Request, project_id: str = Depends(project_id_param)):
    """实时返回当前身份与权限矩阵（角色调整免重登即时生效）。"""
    if not auth_svc.auth_enabled():
        dev = _dev_identity()
        return success({
            "authenticated": True,
            "dev_mode": True,
            "user": dev,
            "permissions": auth_svc.permission_matrix(dev),
        })
    user = _current_user(request)
    actions = auth_svc.effective_actions(user, project_id)
    permissions = auth_svc.permission_matrix(user)
    permissions["project_actions"] = actions
    permissions["project_id"] = project_id
    return success({"authenticated": True, "dev_mode": False,
                    "user": auth_svc._public_user(user), "permissions": permissions})


@router.get("/roles")
def api_roles():
    return success({"items": [
        {"id": rid, "label": cfg["label"], "actions": cfg["actions"]}
        for rid, cfg in auth_svc.ROLES.items()
    ], "total": len(auth_svc.ROLES)})


@router.get("/users")
def api_list_users(page: int | None = Query(default=None),
                   pageSize: int | None = Query(default=None)):
    items = auth_svc.list_users()
    p, ps = parse_pagination(page, pageSize)
    sliced = items[(p - 1) * ps: p * ps]
    return success(list_payload(sliced, len(items), p, ps))


@router.post("/users", status_code=201)
def api_create_user(payload: UserCreate, request: Request):
    actor = _current_user(request)
    try:
        rec = auth_svc.create_user(
            payload.username.strip(), payload.password, payload.role,
            [g.model_dump() for g in payload.grants],
            tenant_id=payload.tenant_id,
        )
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc), http_status=422) from exc
    auth_svc.audit("user_created", actor=actor.get("username", "-"),
                   target=rec["id"], detail={"username": rec["username"], "role": rec["role"]})
    return success(rec, http_status=201)


@router.put("/users/{user_id}")
def api_update_user(user_id: str, payload: UserPatch, request: Request):
    actor = _current_user(request)
    before = auth_svc.get_user(user_id)
    if before is None:
        raise ApiError("NOT_FOUND", f"用户 {user_id} 不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    if "grants" in patch and patch["grants"] is not None:
        patch["grants"] = [g.model_dump() if isinstance(g, GrantIn) else g for g in patch["grants"]]
    try:
        rec = auth_svc.update_user(user_id, patch)
    except ValueError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc), http_status=422) from exc
    auth_svc.audit(
        "permission_change", actor=actor.get("username", "-"), target=user_id,
        detail={
            "username": before.get("username"),
            "before": {"role": before.get("role"), "grants": before.get("grants"), "disabled": before.get("disabled")},
            "after": {"role": rec.get("role"), "grants": rec.get("grants"), "disabled": rec.get("disabled")},
        },
    )
    return success(rec)


@router.get("/audit")
def api_audit(request: Request, event: str | None = Query(default=None),
              actor: str | None = Query(default=None),
              page: int | None = Query(default=None),
              pageSize: int | None = Query(default=None)):
    items = auth_svc.list_audit(event=event, actor=actor, limit=500)
    p, ps = parse_pagination(page, pageSize)
    sliced = items[(p - 1) * ps: p * ps]
    return success(list_payload(sliced, len(items), p, ps))


@router.get("/sso/config")
def api_sso_config():
    """SSO 预留接口：存在但未启用（TP-05-05）。"""
    return success(auth_svc.sso_config())


@router.post("/sso/start")
def api_sso_start():
    """SSO 预留接口：未启用时显式拒绝，不回落匿名登录。"""
    return fail(ApiError("SSO_NOT_ENABLED", "SSO 对接尚未启用，请使用账号密码登录", http_status=403))
