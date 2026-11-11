"""identity_federation：V2-14i 身份联邦（M2-B）。

平台用户 → 各外部系统凭据映射；凭据走 credential_store 本地加密存储（绝不入库、绝不打印）。
不仅用于编排回写，也用于实例内容联邦读（平台以访问者身份代查源系统）。

设计要点：
- 映射记录只存 secret_ref（credential_store 中的引用名）与非敏感 principal；明文 secret 永不落平台存储、永不回显 API。
- 凭据解析 resolve_credential 返回“引用式”auth override（bearer_token_ref / api_key_ref / client_id+client_secret_ref / username+password_ref），由 mcp_client 最终解析为实际值，平台内部不持有明文。
- 缺失映射时显式拒绝（IdentityMappingMissing），不回落匿名/默认账号（TCE-14-04）。
- 所有审计/日志 detail 对 principal 脱敏；secret 明文不进日志/审计。
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from config import get_settings
from core.storage import JsonStore
from credential_store import delete_secret, load_secret, redact, save_secret
from logging_setup import get_logger

logger = get_logger("identity_federation")

# 支持的凭据类型；与 schemas.mcp.ConnectionAuth.type 对齐并补充 basic。
ALLOWED_AUTH_TYPES = {"bearer_token", "api_key", "oauth2_client_credentials", "basic"}

# 审计事件名
EVENT_CREATE = "identity_mapping_create"
EVENT_DELETE = "identity_mapping_delete"
EVENT_RESOLVE = "identity_resolve"


class IdentityError(Exception):
    code = "IDENTITY_ERROR"
    http_status = 400

    def __init__(self, code: str | None = None, message: str = "", http_status: int | None = None):
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status
        super().__init__(message or self.code)


class IdentityMappingMissing(IdentityError):
    code = "IDENTITY_MAPPING_MISSING"
    http_status = 403


def _store(project_id: str) -> JsonStore:
    return JsonStore("identity_mappings", project_id=project_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _secret_ref_for(project_id: str, connection_id: str, user_id: str) -> str:
    safe_pid = re.sub(r"[^A-Za-z0-9_]", "_", project_id)
    safe_cid = re.sub(r"[^A-Za-z0-9_]", "_", connection_id)
    safe_uid = re.sub(r"[^A-Za-z0-9_]", "_", user_id)
    return f"IDFED_{safe_pid}_{safe_cid}_{safe_uid}".upper()


def create_mapping(
    project_id: str,
    *,
    user_id: str,
    connection_id: str,
    auth_type: str,
    principal: Optional[str] = None,
    secret: Optional[str] = None,
    actor: Optional[str] = None,
) -> dict[str, Any]:
    """创建/重建某用户对某连接的凭据映射；secret 一次性写入 credential_store（加密落盘）。"""
    if auth_type not in ALLOWED_AUTH_TYPES:
        raise IdentityError(
            "INVALID_AUTH_TYPE",
            f"auth_type 必须是 {sorted(ALLOWED_AUTH_TYPES)}",
            http_status=422,
        )
    if not secret:
        raise IdentityError("MISSING_SECRET", "secret 为必填（凭据明文仅一次性传入，不入平台存储）", http_status=422)
    if not user_id or not connection_id:
        raise IdentityError("INVALID_MAPPING", "user_id 与 connection_id 均必填", http_status=422)

    ref = _secret_ref_for(project_id, connection_id, user_id)
    # 覆盖写：先删旧 secret 再存新（同一 ref 幂等）
    delete_secret(ref)
    save_secret(ref, secret)  # 启用加密时自动加密 + chmod 600

    rec = {
        "id": uuid.uuid4().hex,
        "project_id": project_id,
        "user_id": user_id,
        "connection_id": connection_id,
        "auth_type": auth_type,
        "principal": principal,
        "secret_ref": ref,
        "created_by": actor or user_id,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _store(project_id).insert(rec)
    _audit(EVENT_CREATE, actor or user_id, rec["id"], {
        "user_id": user_id,
        "connection_id": connection_id,
        "auth_type": auth_type,
        "principal": redact(principal) if principal else None,
    })
    logger.info("identity mapping created user=%s conn=%s type=%s", user_id, connection_id, auth_type)
    return rec


def get_mapping(project_id: str, mapping_id: str) -> Optional[dict[str, Any]]:
    return _store(project_id).get(mapping_id)


def list_mappings(
    project_id: str,
    *,
    user_id: Optional[str] = None,
    connection_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    items = _store(project_id).all()
    if user_id:
        items = [i for i in items if i.get("user_id") == user_id]
    if connection_id:
        items = [i for i in items if i.get("connection_id") == connection_id]
    return items


def delete_mapping(project_id: str, mapping_id: str, *, actor: Optional[str] = None) -> bool:
    rec = _store(project_id).get(mapping_id)
    if not rec:
        return False
    delete_secret(rec.get("secret_ref"))
    _store(project_id).delete(mapping_id)
    _audit(EVENT_DELETE, actor or "-", mapping_id, {
        "user_id": rec.get("user_id"),
        "connection_id": rec.get("connection_id"),
    })
    logger.info("identity mapping deleted id=%s", mapping_id)
    return True


def resolve_credential(project_id: str, *, user_id: str, connection_id: str) -> dict[str, Any]:
    """返回可用于 mcp_client 的“引用式”auth override；缺失映射抛 IdentityMappingMissing（不回落匿名）。

    返回结构（与 schemas.mcp.ConnectionAuth 兼容，凭据用 *_ref 引用，平台内部不持有明文）：
      bearer_token        -> {"type": "bearer_token", "bearer_token_ref": <ref>}
      api_key            -> {"type": "api_key", "api_key_ref": <ref>}
      oauth2_client_credentials -> {"type": "...", "client_id": <principal>, "client_secret_ref": <ref>}
      basic              -> {"type": "basic", "username": <principal>, "password_ref": <ref>}
    """
    rec = next(
        (m for m in _store(project_id).all()
         if m.get("user_id") == user_id and m.get("connection_id") == connection_id),
        None,
    )
    if not rec:
        raise IdentityMappingMissing(
            "IDENTITY_MAPPING_MISSING",
            f"用户 {user_id} 对外部系统 {connection_id} 未配置凭据映射，跨系统调用被拒（不回落匿名/默认账号）",
            http_status=403,
        )
    auth_type = rec["auth_type"]
    principal = rec.get("principal")
    ref = rec["secret_ref"]
    override: dict[str, Any] = {"type": auth_type}
    if auth_type == "bearer_token":
        override["bearer_token_ref"] = ref
    elif auth_type == "api_key":
        override["api_key_ref"] = ref
    elif auth_type == "oauth2_client_credentials":
        override["client_id"] = principal
        override["client_secret_ref"] = ref
    elif auth_type == "basic":
        override["username"] = principal
        override["password_ref"] = ref
    else:  # 防御：allowed 已拦截
        raise IdentityError("UNSUPPORTED_AUTH_TYPE", f"不支持的 auth_type: {auth_type}", http_status=422)

    _audit(EVENT_RESOLVE, user_id, connection_id, {
        "connection_id": connection_id,
        "auth_type": auth_type,
        "principal": redact(principal) if principal else None,
    })
    return override


def delegated_auth_header(connection: dict[str, Any], *, user_id: str, project_id: str) -> dict[str, Any]:
    """联邦读/编排回写时，以访问者身份代查源系统：必须存在映射，否则抛 IdentityMappingMissing。

    返回 mcp_client 可用的 auth override（含 refs）；调用方传给 McpClient.invoke_tool(auth_override=...)。
    """
    cid = connection.get("connection_id") or connection.get("id")
    return resolve_credential(project_id, user_id=user_id, connection_id=cid)


# 审计事件：委托联邦读
EVENT_FEDERATED_READ = "identity_delegated_read"


def federated_read(
    project_id: str,
    *,
    user_id: str,
    connection: dict[str, Any],
    tool: str,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """源连接型内容联邦读调用点（TP-06-06）：以访问者身份代查源系统。

    - 委托身份由该访问者的凭据映射决定（缺失映射即 IdentityMappingMissing，不回落匿名）；
      源系统按该身份返回其权限范围内的数据 → 同一连接/本体，不同访问者返回范围不同。
    - 范围的最终决定方是源系统＋业务端（M2-C 内容共享 / M4 联邦读在其上叠加租户过滤）；
      本函数只保证“真实以访问者身份代查”并携带来源标记。
    - 返回 {"source_connection", "resolved_as", "tool", "result"}；审计不含结果数据与凭据明文。
    """
    from mcp_client import McpClient

    cid = connection.get("connection_id") or connection.get("id")
    override = delegated_auth_header(connection, user_id=user_id, project_id=project_id)
    result = McpClient().invoke_tool(connection, tool, params or {}, auth_override=override)
    _audit(EVENT_FEDERATED_READ, user_id, cid, {
        "connection_id": cid,
        "auth_type": override.get("type"),
        "tool": tool,
        "user_id": user_id,
    })
    logger.info("federated read user=%s conn=%s tool=%s", user_id, cid, tool)
    return {
        "source_connection": cid,
        "resolved_as": {"user_id": user_id, "auth_type": override.get("type")},
        "tool": tool,
        "result": result,
    }


def _audit(event: str, actor: str, target: Any, detail: dict[str, Any]) -> None:
    """审计：detail 中已对 principal 脱敏，secret 明文永不进入审计。失败静默，不影响主流程。"""
    try:
        import auth as auth_svc
        auth_svc.audit(event, actor=actor, target=str(target), detail=detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("identity audit failed for %s: %s", event, e)
