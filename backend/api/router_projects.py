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
):
    page, page_size = parse_pagination(page, page_size)
    items = _store.list()
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


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
    if not _store.delete(project_id):
        raise ApiError("NOT_FOUND", "项目不存在", http_status=404)
    logger.info("delete_project id=%s", project_id)
    return success({"deleted": True})


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
