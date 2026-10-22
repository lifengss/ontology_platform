"""项目管理路由：项目 CRUD + 共享库提示 + 项目级数据访问入口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from core.project_store import ProjectStore
from core.responses import ApiError, list_payload, parse_pagination, success
from logging_setup import get_logger
from schemas.project import ProjectCreate, ProjectUpdate

router = APIRouter(prefix="/api/projects", tags=["projects"])
logger = get_logger("api.projects")
_store = ProjectStore()


@router.post("", status_code=201)
def create_project(payload: ProjectCreate):
    rec = _store.create(payload)
    logger.info("create_project id=%s name=%s", rec["id"], rec["name"])
    return success(rec, http_status=201)


@router.get("")
def list_projects(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
    with_stats: bool = Query(default=False, description="附带项目级统计（本体数/实例数/数据量），首页总览用"),
):
    page, page_size = parse_pagination(page, page_size)
    items = _store.list()
    for rec in items:
        rec.setdefault("status", "active")  # 历史数据无 status 字段，视为 active
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
    return success(list_payload(page_items, total, page, page_size))


@router.get("/{project_id}")
def get_project(project_id: str):
    rec = _store.get(project_id)
    if not rec:
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)
    return success(rec)


@router.put("/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate):
    if not _store.get(project_id):
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    updated = _store.update(project_id, patch)
    return success(updated)


@router.delete("/{project_id}")
def delete_project(project_id: str):
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


def get_project_store() -> ProjectStore:
    return _store
