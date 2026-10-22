"""部门空间（namespace）管理：项目级空间注册表 CRUD。

约定：
- 默认空间 id 恒等于 project_id，不可删除、不可重命名；
- 空间 ID 创建后不可变（PUT 仅更新显示名/描述，与项目 ID 语义一致）；
- 非空空间（仍含本体）不可删除，需先迁移或删除其本体；
- 列表自动回填"有本体但注册表缺失"的空间（兼容旧数据，标记 derived=true）。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from api.dependencies import project_id_param
from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from logging_setup import get_logger
from schemas.namespace import NamespaceCreate, NamespaceUpdate

router = APIRouter(prefix="/api/namespaces", tags=["namespaces"])
logger = get_logger("api.namespaces")

NS_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _ns_store(project_id: str) -> JsonStore:
    return JsonStore("namespaces", project_id=project_id)


def _onto_store(project_id: str) -> JsonStore:
    return JsonStore("ontologies", project_id=project_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ontology_counts(project_id: str) -> dict[str, int]:
    """按空间统计本体数（本体未显式指定 namespace 时继承 project_id）。"""
    counts: dict[str, int] = {}
    for o in _onto_store(project_id).all():
        ns = o.get("namespace") or project_id
        counts[ns] = counts.get(ns, 0) + 1
    return counts


def _backfill(project_id: str, store: JsonStore) -> None:
    """把有本体但注册表缺失的空间补录进注册表（旧数据兼容）。"""
    existing = {e.get("id") for e in store.all()}
    to_add = [
        {"id": ns, "name": ns, "description": None, "created_at": _now(), "derived": True}
        for ns in _ontology_counts(project_id)
        if ns != project_id and ns not in existing
    ]
    for rec in to_add:
        store.insert(rec)
        logger.info("namespace backfilled", extra={"ns": rec["id"], "project_id": project_id})


def _to_item(entry: dict, counts: dict[str, int], project_id: str) -> dict:
    ns_id = entry.get("id")
    return {
        "id": ns_id,
        "name": entry.get("name") or ns_id,
        "description": entry.get("description"),
        "ontology_count": counts.get(ns_id, 0),
        "is_default": ns_id == project_id,
        "derived": bool(entry.get("derived")),
        "created_at": entry.get("created_at"),
    }


@router.get("")
def list_namespaces(
    project_id: str = Depends(project_id_param),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
):
    """空间列表（注册表 ∪ 已占用空间），按创建时间排序。"""
    store = _ns_store(project_id)
    _backfill(project_id, store)
    # 默认空间不在注册表中也恒定存在
    if not store.get(project_id):
        store.insert({"id": project_id, "name": "默认空间", "description": None, "created_at": _now(), "derived": False})
    counts = _ontology_counts(project_id)
    items = [_to_item(e, counts, project_id) for e in store.all()]
    items.sort(key=lambda x: (not x["is_default"], x["created_at"] or "", x["id"]))
    p, ps = parse_pagination(page, page_size)
    total = len(items)
    start = (p - 1) * ps
    return success(list_payload(items[start:start + ps], total, p, ps))


@router.post("", status_code=201)
def create_namespace(payload: NamespaceCreate, project_id: str = Depends(project_id_param)):
    """新建空间；id 缺省自动生成，冲突返回 409。"""
    store = _ns_store(project_id)
    _backfill(project_id, store)
    ns_id = (payload.id or "").strip()
    if not ns_id:
        ns_id = "ns_" + uuid.uuid4().hex[:8]
    if not NS_ID_RE.match(ns_id):
        raise ApiError("NAMESPACE_INVALID_ID", "空间 ID 仅允许字母开头，字母/数字/_/-，最长 64 位", http_status=400)
    if ns_id == project_id:
        raise ApiError("NAMESPACE_DEFAULT_RESERVED", "默认空间 ID 不可新建", http_status=409)
    if store.get(ns_id) or ns_id in _ontology_counts(project_id):
        raise ApiError("NAMESPACE_CONFLICT", f"空间 {ns_id} 已存在", http_status=409)
    rec = {"id": ns_id, "name": payload.name.strip(), "description": payload.description, "created_at": _now(), "derived": False}
    store.insert(rec)
    return success(_to_item(rec, _ontology_counts(project_id), project_id), http_status=201)


@router.put("/{ns_id}")
def update_namespace(ns_id: str, payload: NamespaceUpdate, project_id: str = Depends(project_id_param)):
    """更新空间显示名/描述；ID 不可变；默认空间不可改。"""
    if ns_id == project_id:
        raise ApiError("NAMESPACE_DEFAULT_PROTECTED", "默认空间不可重命名", http_status=409)
    store = _ns_store(project_id)
    if not store.get(ns_id):
        raise ApiError("NAMESPACE_NOT_FOUND", f"空间 {ns_id} 不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    if "name" in patch:
        patch["name"] = patch["name"].strip()
    if not patch:
        raise ApiError("NAMESPACE_NOOP", "无更新字段", http_status=400)
    rec = store.update(ns_id, patch)
    return success(_to_item(rec, _ontology_counts(project_id), project_id))


@router.delete("/{ns_id}")
def delete_namespace(ns_id: str, project_id: str = Depends(project_id_param)):
    """删除空间；默认空间与仍含本体的空间不可删除。"""
    if ns_id == project_id:
        raise ApiError("NAMESPACE_DEFAULT_PROTECTED", "默认空间不可删除", http_status=409)
    store = _ns_store(project_id)
    if not store.get(ns_id):
        raise ApiError("NAMESPACE_NOT_FOUND", f"空间 {ns_id} 不存在", http_status=404)
    count = _ontology_counts(project_id).get(ns_id, 0)
    if count > 0:
        raise ApiError(
            "NAMESPACE_NOT_EMPTY",
            f"空间 {ns_id} 下仍有 {count} 个本体，请先迁移或删除后再删除空间",
            http_status=409,
            details={"ontology_count": count},
        )
    store.delete(ns_id)
    return success({"id": ns_id, "deleted": True})
