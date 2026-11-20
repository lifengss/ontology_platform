"""项目管理路由：项目 CRUD + 共享库提示 + 项目级数据访问入口。

V2-06（M2-C）：项目即租户边界——创建时落租户归属（创建人租户），列表按租户过滤，
跨租户读写/删除/归档一律 404（不泄露存在性）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from core.last_project import get_last_project
from core.project_store import ProjectStore
from core.responses import ApiError, list_payload, parse_pagination, success
from logging_setup import get_logger
from schemas.project import ProjectCreate, ProjectUpdate
from tenant import bypass, current_user, ensure_project_access, is_admin, user_tenant

router = APIRouter(prefix="/api/projects", tags=["projects"])
logger = get_logger("api.projects")
_store = ProjectStore()


@router.post("", status_code=201)
def create_project(payload: ProjectCreate, request: Request):
    user = current_user(request) or {}
    rec = _store.create(payload, tenant_id=user_tenant(user))
    logger.info("create_project id=%s name=%s tenant=%s", rec["id"], rec["name"], rec.get("tenant_id"))
    return success(rec, http_status=201)


@router.get("")
def list_projects(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
    with_stats: bool = Query(default=False, description="附带项目级统计（本体数/实例数/数据量），首页总览用"),
    department: str | None = Query(default=None, description="按所属部门精确过滤；历史数据无 department 字段视为默认部门"),
):
    page, page_size = parse_pagination(page, page_size)
    items = _store.list()
    for rec in items:
        rec.setdefault("status", "active")  # 历史数据无 status 字段，视为 active
        rec.setdefault("department", "默认部门")  # 历史数据无 department 字段，视为默认部门
        rec.setdefault("tenant_id", "default")  # 历史数据无 tenant_id 字段，视为默认租户
    user = current_user(request)
    if not bypass() and not is_admin(user):
        # V2-06 TC-06-01：非管理员仅见本租户项目
        items = [rec for rec in items if (rec.get("tenant_id") or "default") == user_tenant(user)]
    if department:
        items = [rec for rec in items if (rec.get("department") or "默认部门") == department]
    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start: start + page_size]
    if with_stats:
        from mcp_server import get_mcp_server
        mcp = get_mcp_server()
        for rec in page_items:
            st = _store.stats(rec["id"])
            rec["stats"] = st
            rec["mcp_published"] = mcp.server_active(rec["id"])
    data = list_payload(page_items, total, page, page_size)
    # GAP-07：当前身份的默认项目（最近使用且可读 → 该 id；否则 null）
    last = get_last_project((user or {}).get("id"))
    if last:
        from api.dependencies import _project_readable  # 懒导入：依赖层与路由层互不顶层耦合
        if not _project_readable(request, last):
            last = None
    data["default_project"] = last
    return success(data)


@router.get("/{project_id}")
def get_project(request: Request, project_id: str):
    ensure_project_access(request, project_id)
    rec = _store.get(project_id)
    if not rec:
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)
    return success(rec)


@router.put("/{project_id}")
def update_project(request: Request, project_id: str, payload: ProjectUpdate):
    ensure_project_access(request, project_id)
    if not _store.get(project_id):
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    if "department" in patch:
        patch["department"] = (patch["department"] or "").strip() or "默认部门"
    updated = _store.update(project_id, patch)
    return success(updated)


@router.delete("/{project_id}")
def delete_project(request: Request, project_id: str):
    ensure_project_access(request, project_id)
    if project_id in ("default", "_shared"):
        raise ApiError("FORBIDDEN_WRITE", "不能删除默认项目或共享库", http_status=403)
    if not _store.get(project_id):
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)
    # 级联：MCP 发布态下线（尽力而为，未发布则忽略）
    from mcp_server import get_mcp_server
    try:
        get_mcp_server().unpublish(project_id)
    except Exception:  # noqa: BLE001
        logger.warning("delete_project: MCP unpublish failed project_id=%s", project_id)
    _store.delete(project_id)  # 内部级联删除数据目录与归档包
    logger.info("delete_project id=%s", project_id)
    return success({"deleted": True})


@router.post("/{project_id}/archive")
def archive_project(project_id: str):
    """归档项目：只保留索引（本体清单/计数），重数据压缩到 archives/<id>.zip。"""
    if project_id in ("default", "_shared"):
        raise ApiError("FORBIDDEN_WRITE", "不能归档默认项目或共享库", http_status=403)
    rec = _store.get(project_id)
    if not rec:
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)
    if rec.get("status") == "archived":
        raise ApiError("BAD_REQUEST", "项目已处于归档状态", http_status=400)
    # 归档前下线 MCP 发布态（归档后项目不可选，端点必须不可用）
    from mcp_server import get_mcp_server
    try:
        get_mcp_server().unpublish(project_id)
    except Exception:  # noqa: BLE001
        logger.warning("archive_project: MCP unpublish failed project_id=%s", project_id)
    try:
        result = _store.archive(project_id)
    except FileNotFoundError:
        raise ApiError("NOT_FOUND", "项目数据目录不存在，无法归档", http_status=404)
    logger.info("archive_project id=%s size=%s", project_id, (result.get("archive_info") or {}).get("archive_size"))
    return success(result)


@router.post("/{project_id}/restore")
def restore_project(project_id: str):
    """恢复归档项目：解压归档包回原目录，status 置回 active。"""
    rec = _store.get(project_id)
    if not rec:
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)
    if rec.get("status") != "archived":
        raise ApiError("BAD_REQUEST", "项目未处于归档状态，无需恢复", http_status=400)
    try:
        result = _store.restore(project_id)
    except FileNotFoundError:
        raise ApiError("NOT_FOUND", "归档包不存在，无法恢复（数据可能已丢失）", http_status=404)
    logger.info("restore_project id=%s", project_id)
    return success(result)


@router.post("/{project_id}/promote-to-shared")
def promote_to_shared(project_id: str, ontology_id: str = Query(...)):
    """MVP：仅给出功能提示，真正提升到共享库保留到下个版本。"""
    rec = _store.get(project_id)
    if not rec:
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)
    logger.info("promote-to-shared requested project_id=%s ontology_id=%s (MVP: noop)", project_id, ontology_id)
    return success({
        "promoted": False,
        "reason": "共享库功能将在下个版本开放，MVP 仅保留操作入口用于演示。",
        "project_id": project_id,
        "ontology_id": ontology_id,
    })


@router.post("/{project_id}/seed-example", status_code=201)
def seed_example_project(request: Request, project_id: str):
    """GAP-13 示例项目一条龙：为项目预置示例本体（内置约束示范模板）＋示例实例（每对象 3 条）。

    - 本体经 create_ontology 走与人工建模相同通道（业务校验/审计/RDF 同步），落为 draft；
    - 幂等保护：项目已有本体时 409（避免重复种导致用户数据混淆）；
    - 实例经 InstanceStore 直插（与 CSV 入库记录同构，source=example_seed 便于溯源筛选）。
    """
    ensure_project_access(request, project_id)
    rec = _store.get(project_id)
    if not rec:
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)
    if rec.get("status") == "archived":
        raise ApiError("BAD_REQUEST", "归档项目不能填充示例数据", http_status=400)
    from api.router_ontologies import _store_for as _onto_store_for, create_ontology
    from core.instance_store import get_instance_store
    from example_seed import build_example_instances, find_builtin_template

    tpl = find_builtin_template()
    if not tpl:
        raise ApiError("NOT_FOUND", "内置示例模板不存在", http_status=404)
    if _onto_store_for(project_id).all():
        raise ApiError("EXAMPLE_EXISTS", "该项目已存在本体，示例数据仅向空项目提供（新建空项目即可一键体验）", http_status=409)
    from schemas.ontology import OntologyCreate
    response = create_ontology(OntologyCreate(**tpl["model"]), request=request, project_id=project_id, namespace=None)
    import json as _json
    ontology = _json.loads(response.body.decode("utf-8")).get("data") or {}
    records = build_example_instances(ontology)
    store = get_instance_store(project_id)
    for r in records:
        store.insert(r)
    logger.info("seed_example project_id=%s ontology_id=%s instances=%d", project_id, ontology.get("id"), len(records))
    return success({
        "ontology_id": ontology.get("id"),
        "ontology_name": ontology.get("name"),
        "objects": len(ontology.get("objects") or []),
        "instances": len(records),
        "template_id": tpl["id"],
    }, http_status=201)


def get_project_store() -> ProjectStore:
    return _store
