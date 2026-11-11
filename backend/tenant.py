"""tenant：V2-06 多租户、部门隔离与本体共享等级（M2-C）。

口径（与 docs/API-INTERFACE-DOC.md §24、docs/V2-功能需求描述.md V2-06 一致）：
- **项目（project）即租户边界**：每项目属于一个租户（project.tenant_id，缺省 default）；
  用户绑定租户（user.tenant_id，缺省 default）。跨租户访问项目级资源一律 404（不泄露存在性）。
- **本体共享等级只作用于 schema（对象/属性/关系/定义），只读**：
  - private（默认，新建本体）：仅 owner（+平台 admin）可见；
  - project（项目组共享）：仅 shared_with 指定成员可见（支持跨租户）；
  - department（部门共享）：本租户全员可见；
  - public（公开）：跨租户全员可见。
  - 存量本体无 sharing 字段时视为 department（不收紧既有同租户协作），新本体一律默认 private。
- **实例永不共享（INV-2）**：跨共享范围（跨租户）用户仅可见 schema，实例列表/详情不可见。
- **上传型实例内容**：默认仅上传者可读；上传者可显式开放到人/部门，上限=本体共享等级可见范围；
  开放/收回留审计（content_open_grant / content_open_revoke）。
- **源连接型实例内容**：以访问者身份代查源系统（identity_federation.federated_read，TC-06-06 联验
  通过）；当前版本实例仍按 M4-D3 前的存量口径存储，同租户读取不变（边界如实标注）。
- **共享不授写**：schema 写操作要求 owner / admin / 本租户且 RBAC（M2-A grants）允许 write。
- 等级变更即时生效（每次请求实时计算，无缓存授权），降级立即收口（TCE-06-03）。
- 共享等级变更审计 sharing_change（谁/何时/从哪级到哪级，TC-06-04）。
- ONTOLOGY_AUTH_ENABLED=false（测试态豁免）时全部检查放行，与 M2-A 同口径。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Query, Request

from core.responses import ApiError
from logging_setup import get_logger

logger = get_logger("tenant")

SHARING_LEVELS = ("private", "project", "department", "public")

EVENT_SHARING_CHANGE = "sharing_change"
EVENT_CONTENT_OPEN = "content_open_grant"
EVENT_CONTENT_REVOKE = "content_open_revoke"


# --------------------------------------------------------------------------- #
# 基础判定
# --------------------------------------------------------------------------- #
def current_user(request: Request) -> Optional[dict[str, Any]]:
    """请求身份：鉴权关闭时返回 dev 管理员（测试态豁免）；未登录返回 None。"""
    user = getattr(request.state, "auth_user", None)
    if user is None:
        import auth as auth_svc
        if not auth_svc.auth_enabled():
            return {"id": "dev", "username": "dev", "role": "admin",
                    "grants": [], "tenant_id": "default"}
        return None
    return user


def bypass() -> bool:
    """测试态豁免（ONTOLOGY_AUTH_ENABLED=false）：与 M2-A 同口径全部放行。"""
    import auth as auth_svc
    return not auth_svc.auth_enabled()


def is_admin(user: Optional[dict[str, Any]]) -> bool:
    return bool(user) and user.get("role") == "admin"


def user_tenant(user: Optional[dict[str, Any]]) -> str:
    return (user or {}).get("tenant_id") or "default"


def project_tenant(project: Optional[dict[str, Any]]) -> str:
    """项目所属租户：优先 tenant_id，历史数据回退 department（缺省 default）。"""
    if not project:
        return "default"
    return project.get("tenant_id") or "default"


def same_tenant(user: Optional[dict[str, Any]], project: Optional[dict[str, Any]]) -> bool:
    if is_admin(user):
        return True
    return user_tenant(user) == project_tenant(project)


# --------------------------------------------------------------------------- #
# 项目级租户隔离（TC-06-01）
# --------------------------------------------------------------------------- #
def ensure_project_access(request: Request, project_id: str) -> None:
    """跨租户访问项目级资源（连接/字典/候选/审批等无共享语义的资源）一律 404，不泄露存在性。

    本体/实例走资源级判定（schema 可跨租户共享：TC-06-03/05），不经本函数。
    admin（平台治理角色，操作留审计）可跨租户；测试态豁免放行。
    """
    if bypass():
        return
    user = current_user(request)
    if user is None or is_admin(user):
        return
    from api.router_projects import get_project_store
    project = get_project_store().get(project_id)
    if not project or not same_tenant(user, project):
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)


# --------------------------------------------------------------------------- #
# 本体 schema 可见性（TC-06-02/03）
# --------------------------------------------------------------------------- #
def ontology_owner(ont: dict[str, Any]) -> Optional[str]:
    return ont.get("owner") or ont.get("created_by")


def effective_sharing(ont: dict[str, Any]) -> str:
    """存量本体无 sharing 字段时视为 department（不收紧既有同租户协作）。"""
    return ont.get("sharing") if ont.get("sharing") in SHARING_LEVELS else "department"


def schema_visible(user: Optional[dict[str, Any]], project: Optional[dict[str, Any]],
                   ont: dict[str, Any]) -> bool:
    """schema 可见性：owner / admin / 按共享等级。跨租户项目已在项目级拦截。"""
    if user is None:
        return False
    if is_admin(user):
        return True
    if ontology_owner(ont) == user.get("username"):
        return True
    if not same_tenant(user, project):
        # 跨租户仅 public / project（指定成员）可见
        sharing = effective_sharing(ont)
        if sharing == "public":
            return True
        if sharing == "project" and user.get("username") in (ont.get("shared_with") or []):
            return True
        return False
    # 同租户：owner 已命中；其余按等级
    sharing = effective_sharing(ont)
    if sharing in ("department", "public"):
        return True
    if sharing == "project" and user.get("username") in (ont.get("shared_with") or []):
        return True
    return False


def ensure_schema_visible(request: Request, project_id: str, ont: dict[str, Any]) -> dict[str, Any]:
    """不可见一律 404「本体不存在」，与资源不存在同文案（TC-06-02 不泄露存在性）。返回当前用户。"""
    user = current_user(request)
    if bypass() or schema_visible(user, _project(project_id), ont):
        return user or {}
    raise ApiError("NOT_FOUND", "本体不存在", http_status=404)


def _project(project_id: str) -> Optional[dict[str, Any]]:
    try:
        from api.router_projects import get_project_store
        return get_project_store().get(project_id)
    except Exception:  # noqa: BLE001
        return None


def can_write_schema(user: Optional[dict[str, Any]], project_id: str,
                     ont: dict[str, Any]) -> bool:
    """schema 写权限：admin / owner / 同租户且 RBAC（M2-A grants）允许 write。

    共享只授读：跨租户（含 public/project 共享可见）非 owner 一律拒绝（TC-06-08）。
    """
    if user is None:
        return False
    if is_admin(user):
        return True
    if ontology_owner(ont) == user.get("username"):
        return True
    if not same_tenant(user, _project(project_id)):
        return False
    import auth as auth_svc
    return "write" in auth_svc.effective_actions(user, project_id, ont.get("id"))


def ensure_schema_write(request: Request, project_id: str, ont: dict[str, Any]) -> dict[str, Any]:
    user = current_user(request)
    if bypass() or can_write_schema(user, project_id, ont):
        return user or {}
    raise ApiError("FORBIDDEN", "共享仅授 schema 只读，写操作被拒", http_status=403)


def can_manage_sharing(user: Optional[dict[str, Any]], project_id: str, ont: dict[str, Any]) -> bool:
    """共享等级调整：仅 owner 或 admin（等级变更属治理操作）。"""
    if user is None:
        return False
    return is_admin(user) or ontology_owner(ont) == user.get("username")


# --------------------------------------------------------------------------- #
# 实例可见性（TC-06-05 / TC-06-07）
# --------------------------------------------------------------------------- #
def instance_read_allowed(user: Optional[dict[str, Any]], project_id: str,
                          ont: dict[str, Any]) -> bool:
    """实例可见 = schema 可见 且 属本租户（跨共享范围仅可见 schema，INV-2）。"""
    if user is None:
        return False
    if is_admin(user):
        return True
    if not same_tenant(user, _project(project_id)):
        return False
    return schema_visible(user, _project(project_id), ont)


def instance_uploader(rec: dict[str, Any]) -> Optional[str]:
    return rec.get("uploaded_by") or rec.get("created_by")


def instance_is_connection_type(project_id: str, rec: dict[str, Any]) -> bool:
    """源连接型实例判定：经候选的 source_type=external_mcp 判定；无候选元数据按上传型处理。"""
    cid = rec.get("candidate_id")
    if not cid:
        return False
    try:
        from core.storage import JsonStore
        cand = JsonStore("ingest_candidates", project_id=project_id).get(cid)
    except Exception:  # noqa: BLE001
        return False
    return bool(cand) and cand.get("source_type") == "external_mcp"


def content_open_cap(project_id: str, ont: dict[str, Any]) -> tuple[set[str], set[str]]:
    """开放上限 = 共享等级可见范围，返回 (允许的用户名集合, 允许的租户集合)。"""
    sharing = effective_sharing(ont)
    if sharing == "private":
        owner = ontology_owner(ont)
        return ({owner} if owner else set()), set()
    if sharing == "project":
        return set(ont.get("shared_with") or []), set()
    if sharing == "department":
        import auth as auth_svc
        tenant = project_tenant(_project(project_id))
        users = {u["username"] for u in auth_svc.list_users()
                 if (u.get("tenant_id") or "default") == tenant}
        return users, {tenant}
    # public：全部用户 / 全部租户
    import auth as auth_svc
    all_users = {u["username"] for u in auth_svc.list_users()}
    all_tenants = {_tenant_of_project(p) for p in _all_projects()}
    return all_users, all_tenants


def _tenant_of_project(p: dict[str, Any]) -> str:
    return project_tenant(p)


def _all_projects() -> list[dict[str, Any]]:
    try:
        from api.router_projects import get_project_store
        return get_project_store().list()
    except Exception:  # noqa: BLE001
        return []


def content_visible(user: Optional[dict[str, Any]], project_id: str,
                    rec: dict[str, Any], ont: dict[str, Any]) -> bool:
    """上传型实例字段内容可见性：上传者/admin/显式开放范围内；存量无上传者按同租户放行。

    源连接型实例：内容以访问者身份联邦代查（TC-06-06），平台侧存量读取同租户不收紧
    （V2-02 实例数据模型随 M4/D3 落地，边界如实标注）。
    """
    if user is None:
        return False
    if is_admin(user):
        return True
    if instance_is_connection_type(project_id, rec):
        return True
    uploader = instance_uploader(rec)
    if not uploader:
        return True  # 存量实例无上传者元数据：不收紧既有同租户行为
    if uploader == user.get("username"):
        return True
    open_rec = rec.get("content_open") or {}
    if user.get("username") in (open_rec.get("users") or []):
        return True
    if user_tenant(user) in (open_rec.get("departments") or []):
        return True
    return False


def mask_instance_content(rec: dict[str, Any]) -> dict[str, Any]:
    """内容受限时保留元数据（键/类型/关系），字段值置空并显式标记（不静默）。"""
    masked = dict(rec)
    masked["instance"] = {}
    masked["content_restricted"] = True
    return masked


# --------------------------------------------------------------------------- #
# 开放范围校验（TCE-06-01 / TCE-06-02）
# --------------------------------------------------------------------------- #
def validate_open_targets(project_id: str, ont: dict[str, Any],
                          users: list[str], departments: list[str]) -> dict[str, list[str]]:
    """开放目标合法性：账号必须存在（TCE-06-01）、必须在共享等级可见范围内（TCE-06-02）。"""
    import auth as auth_svc
    cap_users, cap_tenants = content_open_cap(project_id, ont)
    existing = {u["username"] for u in auth_svc.list_users()}
    invalid_users = [u for u in users if u not in existing]
    out_of_cap_users = [u for u in users if u in existing and u not in cap_users]
    all_tenants = {_tenant_of_project(p) for p in _all_projects()}
    invalid_depts = [d for d in departments if d not in all_tenants]
    out_of_cap_depts = [d for d in departments if d in all_tenants and d not in cap_tenants]
    return {
        "invalid_users": invalid_users,
        "out_of_cap_users": out_of_cap_users,
        "invalid_departments": invalid_depts,
        "out_of_cap_departments": out_of_cap_depts,
    }


def merge_open_targets(rec: dict[str, Any], users: list[str],
                       departments: list[str]) -> dict[str, list[str]]:
    open_rec = dict(rec.get("content_open") or {})
    open_rec["users"] = sorted(set((open_rec.get("users") or []) ) | set(users))
    open_rec["departments"] = sorted(set((open_rec.get("departments") or [])) | set(departments))
    return open_rec


def remove_open_targets(rec: dict[str, Any], users: list[str],
                        departments: list[str]) -> dict[str, list[str]]:
    open_rec = dict(rec.get("content_open") or {})
    open_rec["users"] = [u for u in (open_rec.get("users") or []) if u not in set(users)]
    open_rec["departments"] = [d for d in (open_rec.get("departments") or []) if d not in set(departments)]
    return open_rec


def project_tenant_guard(request: Request, project_id: str | None = Query(default="default")) -> None:
    """FastAPI 路由级依赖：连接/字典/MCP 管理等无共享语义资源统一租户拦截（TC-06-01）。"""
    ensure_project_access(request, project_id or "default")


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #
def _audit(event: str, actor: str, target: str, detail: dict[str, Any]) -> None:
    try:
        import auth as auth_svc
        auth_svc.audit(event, actor=actor, target=target, detail=detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("tenant audit failed for %s: %s", event, e)


def audit_sharing_change(actor: str, project_id: str, ont_id: str,
                         old: str, new: str, shared_with: list[str]) -> None:
    _audit(EVENT_SHARING_CHANGE, actor, ont_id, {
        "project_id": project_id, "from": old, "to": new, "shared_with": shared_with,
    })


def audit_content_open(actor: str, project_id: str, instance_id: str,
                       users: list[str], departments: list[str], revoked: bool = False) -> None:
    _audit(EVENT_CONTENT_REVOKE if revoked else EVENT_CONTENT_OPEN, actor, instance_id, {
        "project_id": project_id, "users": users, "departments": departments,
    })
