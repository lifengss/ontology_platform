"""V2-05 全局鉴权过滤器（M2-A）。

覆盖 /api/* 与后端页面路径；/mcp/* 沿用 mcp_server 自己的项目静态 Bearer token 体系，
/health、静态资源、登录/SSO 预留、client-logs 与 OPTIONS 预检放行。

注册为 BaseHTTPMiddleware，且不改变 main.py 的中间件层级：最外层仍是
McpStreamableMiddleware（M1-B 修复 legacy SSE 双响应的关键，勿动）。
"""
from __future__ import annotations

from fastapi import Request

import auth as auth_svc
from core.responses import ApiError, fail


def _deny(request: Request, status: int, code: str, message: str):
    resp = fail(ApiError(code, message, http_status=status))
    # 该响应未经过最内层的 CORSMiddleware，需回显 CORS 头，否则浏览器拿不到 401/403 正文
    for key, value in auth_svc.cors_headers(request.headers.get("origin")).items():
        resp.headers[key] = value
    return resp


async def auth_filter(request: Request, call_next):
    if not auth_svc.auth_enabled():
        return await call_next(request)

    path, method = request.url.path, request.method
    if auth_svc.is_public_request(method, path):
        return await call_next(request)

    auth_svc.ensure_bootstrap()
    token = auth_svc.extract_token(
        request.cookies.get(auth_svc.COOKIE_NAME),
        request.headers.get("authorization"),
    )
    if not token:
        auth_svc.audit("auth_missing", target=path, detail={"method": method})
        return _deny(request, 401, "UNAUTHORIZED", "未登录或会话已失效")

    user = auth_svc.resolve_request_user(token)
    if user is None:
        auth_svc.audit("auth_invalid", target=path, detail={"method": method})
        return _deny(request, 401, "UNAUTHORIZED", "未登录或会话已失效")

    action = auth_svc.required_action(method, path)
    project_id, ontology_id = auth_svc.resource_keys(
        method, path, request.query_params.get("project_id")
    )
    if action not in auth_svc.effective_actions(user, project_id, ontology_id):
        auth_svc.audit(
            "permission_denied",
            actor=user.get("username", "-"),
            target=path,
            detail={
                "method": method, "action": action, "role": user.get("role"),
                "project_id": project_id, "ontology_id": ontology_id,
            },
        )
        return _deny(request, 403, "FORBIDDEN", f"当前角色无权执行 {action} 操作")

    request.state.auth_user = user
    return await call_next(request)
