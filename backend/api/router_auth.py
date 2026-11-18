"""V2-05 登录鉴权与 RBAC 路由（M2-A）。

端点契约见 docs/API-INTERFACE-DOC.md §21。所有响应走 core.responses 统一信封。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

import auth as auth_svc
from api.dependencies import project_id_param
from core.responses import ApiError, fail, list_payload, parse_pagination, success
from schemas.auth import ChangePasswordReq, GrantIn, LoginReq, UserCreate, UserPatch

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


@router.post("/change-password")
def api_change_password(payload: ChangePasswordReq, request: Request,
                        mode: str | None = Query(default=None)):
    """修改本人密码（须验证当前密码）。成功后 must_change_password 清除，
    改密前签发的旧会话立即失效；为免当前请求被弹回登录页，成功响应中
    直接签发新会话（cookie 模式覆写 cookie，?mode=bearer 额外返回 token）。"""
    user = _current_user(request)
    err = auth_svc.change_password(user, payload.old_password, payload.new_password)
    if err:
        raise ApiError("INVALID_CREDENTIALS", err, http_status=400)
    # 重读改密后的最新记录（pwd_epoch 已递增），否则新 token 会带着旧版本号被立即判失效
    token, exp = auth_svc.issue_token(auth_svc.get_user(user["id"]))
    data: dict[str, Any] = {
        "changed": True,
        "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat(),
        "user": auth_svc._public_user(auth_svc.get_user(user["id"])),
    }
    if mode == "bearer":
        data["token"] = token
    resp = success(data)
    if mode != "bearer":
        resp.set_cookie(
            auth_svc.COOKIE_NAME, token, max_age=auth_svc.TOKEN_TTL_SECONDS,
            httponly=True, samesite="lax", secure=_cookie_secure(), path="/",
        )
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
            department=payload.department,
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
            "before": {"role": before.get("role"), "grants": before.get("grants"),
                       "disabled": before.get("disabled"), "department": before.get("department")},
            "after": {"role": rec.get("role"), "grants": rec.get("grants"),
                      "disabled": rec.get("disabled"), "department": rec.get("department")},
        },
    )
    return success(rec)


@router.delete("/users/{user_id}")
def api_delete_user(user_id: str, request: Request):
    """物理删除用户（仅 admin）。删除前提示不可恢复，删除后留审计。"""
    actor = _current_user(request)
    before = auth_svc.get_user(user_id)
    if before is None:
        raise ApiError("NOT_FOUND", f"用户 {user_id} 不存在", http_status=404)
    auth_svc.delete_user(user_id)
    auth_svc.audit(
        "user_deleted", actor=actor.get("username", "-"), target=user_id,
        detail={"username": before.get("username"), "role": before.get("role"),
                "department": before.get("department")},
    )
    return success({"deleted": True})


@router.post("/users/import")
async def api_import_users(file: UploadFile = File(...), request: Request = None):
    """Excel 批量添加用户：逐行解析（账号/密码/角色/部门/租户/权限），失败行返回明细。"""
    actor = _current_user(request)

    # 仅支持 .xlsx / .xls
    filename = (file.filename or "").lower()
    if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
        raise ApiError("BAD_REQUEST", "仅支持 .xlsx 文件", http_status=400)

    try:
        import io
        from openpyxl import load_workbook
        content = await file.read()
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", f"Excel 解析失败: {exc}", http_status=400) from exc

    if not rows:
        raise ApiError("BAD_REQUEST", "文件内容为空", http_status=400)

    header = rows[0]
    # 期望表头：账号/密码/角色/部门/租户/权限（密码列可缺省：该行使用系统默认初始密码）
    col = {h.strip(): i for i, h in enumerate(header) if h is not None}
    required = ("账号",)
    missing = [k for k in required if k not in col]
    if missing:
        raise ApiError("BAD_REQUEST", f"缺少必填列: {'、'.join(missing)}", http_status=400)

    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for idx, row in enumerate(rows[1:], start=2):
        if row is None or all(c is None or str(c).strip() == "" for c in row):
            continue  # 跳过空行
        def _cell(key: str) -> str:
            # 空单元格是 None：须先判 None 再转字符串（str(None)="None" 会污染取值）
            v = row[col[key]] if col.get(key) is not None else None
            return str(v).strip() if v is not None else ""

        username = _cell("账号")
        password = _cell("密码")
        role = _cell("角色") or "viewer"
        department = _cell("部门")
        tenant_id = _cell("租户") or None
        try:
            if password and len(password) < 8:
                raise ValueError("密码长度至少 8 位（留空则用系统默认初始密码）")
            if not username:
                raise ValueError("账号不能为空")
            rec = auth_svc.create_user(
                username, password or None, role, [],
                tenant_id=tenant_id or None, department=department or None,
            )
            created.append(rec)
        except ValueError as exc:
            failed.append({"row": idx, "reason": str(exc)})

    auth_svc.audit(
        "users_import", actor=actor.get("username", "-"),
        detail={"created": len(created), "failed": len(failed),
                "failed_rows": [f["row"] for f in failed]},
    )
    return success({"created": len(created), "failed": failed, "total": len(created) + len(failed)},
                   http_status=201 if created else 400)


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
