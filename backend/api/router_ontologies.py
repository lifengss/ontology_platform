"""本体建模路由（UC1 / VS1 + VS5/逆向）。完整闭环：创建→翻译落 Jena(降级文件)→读取→导出→校验→快照。"""
from __future__ import annotations

import copy
import io
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from rdflib import Graph

from ai_helper import call_llm, extract_json, generate_ontology_from_document, generate_ontology_from_text
from api.dependencies import namespace_param, project_id_param
import draft_align as align_mod
import audit as audit_svc
import dictionary as dict_svc
import sidecar_log as sidecar_svc  # V2-10 旁路操作日志（M4-G）
from core.instance_store import get_instance_store
from core.project_store import ProjectStore
from core.responses import (
    ApiError,
    fail,
    list_payload,
    parse_pagination,
    success,
)
from core.storage import JsonStore
from jena_adapter import rdf_sync
from jena_adapter.client import shacl_validate
from logging_setup import get_logger
from modeling_translator import (
    blocking_violations,
    build_instance_graph,
    translate_to_shacl,
    translate_to_turtle,
    validate_business_layer,
)
import schemadiff as sd
from schemas.ontology import (
    ExportRequest,
    Ontology,
    OntologyCreate,
    OntologyEnrichRequest,
    OntologyEnrichSummary,
    OntologyFromDocResponse,
    OntologyFromTextRequest,
    OntologyObjectPatch,
    PrimaryKeyMigrationRequest,
    ValidateRequest,
    ValidateResult,
)
from tenant import (
    audit_sharing_change,
    bypass,
    can_manage_sharing,
    current_user,
    effective_sharing,
    ensure_schema_visible,
    ensure_schema_write,
    is_admin,
    schema_visible,
    SHARING_LEVELS,
)

JOBS: dict[str, dict[str, Any]] = {}
# GAP-11：任务进度/取消/重试——重试参数（含文件字节）与取消标志不序列化进响应，单独存放
_JOBS_PARAMS: dict[str, dict[str, Any]] = {}
_JOBS_CANCEL: dict[str, threading.Event] = {}


def _job_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_job(job_id: str, **fields: Any) -> None:
    """就地更新 job 状态/进度（单 dict update 原子，跨线程安全）。"""
    job = JOBS.get(job_id)
    if job is None:
        return
    job.update(fields)
    job["updated_at"] = _job_now()


def _new_job_record(job_id: str, project_id: str, file_infos: list[tuple[str, bytes]]) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": "pending",
        "stage": "排队中",
        "progress": 0,
        "kind": "from-doc",
        "file_names": [f[0] for f in file_infos],
        "draft_id": None,
        "error": None,
        "project_id": project_id,
        "created_at": _job_now(),
        "updated_at": _job_now(),
    }


router = APIRouter(prefix="/api/ontologies", tags=["ontologies"])
logger = get_logger("api.ontologies")
_project_store = ProjectStore()
_store: JsonStore | None = None
_snapshots: JsonStore | None = None


def _store_for(project_id: str) -> JsonStore:
    return JsonStore("ontologies", project_id=project_id)


def _snapshots_for(project_id: str) -> JsonStore:
    return JsonStore("ontology_snapshots", project_id=project_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_view(rec: dict) -> dict:
    """审计前值/后值：模型本体（name/version/status/objects/constraints/functions）。"""
    return {k: rec.get(k) for k in ("name", "description", "version", "status",
                                    "objects", "constraints", "functions") if k in rec}


def _ensure_editable(rec: dict) -> None:
    """V2-07（TCE-07-02）：已发布本体不可直编——一切 schema 变更须经 /changesets 提案
    （兼容升小版本 / 不兼容升大版本＋审批）。"""
    if rec.get("status") == "active":
        raise ApiError(
            "PUBLISHED_IMMUTABLE",
            "已发布本体不可直接修改，请通过变更提案（POST /changesets）提交：兼容变更自动升小版本，"
            "不兼容变更须升大版本并审批（V2-07）",
            http_status=409,
        )


def _sync_rdf(project_id: str, ontology_id: str) -> None:
    """重建本体全量图（schema+实例）写入 RDF 存储；失败仅告警，不阻断主流程。"""
    try:
        rdf_sync.sync_ontology(project_id, ontology_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("RDF 同步失败（不影响主流程） ontology_id=%s: %s", ontology_id, e)


def _backfill_object_fields(obj: dict, namespace: str) -> None:
    """对象级字段自动补齐（create/update 共用）。

    前端表单不采集 business_definition/stable_key，缺失时按同一规则补齐：
    业务语义定义回退 description→name 派生；稳定主键取首个 required string 属性
    （回退首个 string 属性→任意属性）。update 路径若不做此补齐，PUT 整体替换
    objects 会丢掉库中原有值，必被业务层校验门拦截（422）。
    """
    if not obj.get("namespace"):
        obj["namespace"] = namespace
    biz_def = (obj.get("business_definition") or obj.get("description") or f"{obj.get('name', '对象')}的业务语义").strip()
    if len(biz_def) < 5:
        biz_def = f"{obj.get('name', '对象')}的业务语义定义"
    obj["business_definition"] = biz_def
    attrs = obj.get("attributes") or []
    if not obj.get("stable_key") and attrs:
        candidates = [a for a in attrs if a.get("required") and a.get("data_type", "string") == "string"]
        if not candidates:
            candidates = [a for a in attrs if a.get("data_type", "string") == "string"]
        if not candidates:
            candidates = attrs
        if candidates:
            obj["stable_key"] = candidates[0]["id"]
            obj["stable_key_explicit"] = False  # 自动推断；用户显式指定时由 PATCH/PUT 置 True


@router.post("", status_code=201)
def create_ontology(
    payload: OntologyCreate,
    request: Request,
    project_id: str = Depends(project_id_param),
    namespace: str | None = Depends(namespace_param),
):
    user = current_user(request) or {}
    record = payload.model_dump()
    record["id"] = uuid.uuid4().hex
    record["status"] = "draft"
    record["project_id"] = project_id
    record["namespace"] = namespace or project_id
    record["owner"] = user.get("username") or "dev"  # V2-06：建模人归属
    record["sharing"] = "private"  # V2-06：新建本体默认私有（仅本人可见）
    record["shared_with"] = []
    record["created_at"] = _now()
    record["updated_at"] = record["created_at"]
    # 对象级 namespace 默认继承本体 namespace；并自动补齐业务语义定义与稳定主键
    for obj in record.get("objects", []):
        _backfill_object_fields(obj, record["namespace"])
    biz_errors = blocking_violations(record)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "业务层校验未通过", http_status=422,
                       details={"violations": biz_errors})
    store = _store_for(project_id)
    store.insert(record)
    _sync_rdf(project_id, record["id"])
    audit_svc.record_from_request(request, "ontology_create", project_id=project_id,
                                  target_type="ontology", target_id=record["id"],
                                  target_name=record.get("name", ""), after=_model_view(record))
    sidecar_svc.record_from_request(request, sidecar_svc.TYPE_ONTOLOGY_CHANGE, project_id=project_id,
                                    target_type="ontology", target_id=record["id"],
                                    payload={"action": "create", "name": record.get("name", "")})
    logger.info("create_ontology id=%s project_id=%s name=%s", record["id"], project_id, record["name"])
    return success(record, http_status=201)


@router.get("")
def list_ontologies(
    request: Request,
    project_id: str = Depends(project_id_param),
    namespace: str | None = Depends(namespace_param),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = None,
):
    page, page_size = parse_pagination(page, page_size)
    store = _store_for(project_id)
    items = store.all()
    user = current_user(request)
    project = _project_store.get(project_id)
    if not bypass() and not is_admin(user):
        # V2-06 TC-06-03：列表仅含 schema 可见本体（私有=owner，部门=同租户，项目组=指定成员，公开=全员）
        items = [o for o in items if schema_visible(user, project, o)]
    # 按 namespace 过滤；未显式指定则继承 project_id
    ns = namespace or project_id
    items = [o for o in items if (o.get("namespace") or project_id) == ns]
    if keyword:
        items = [o for o in items if keyword.lower() in (o.get("name", "") or "").lower()]
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/jobs")
def list_jobs(project_id: str = Depends(project_id_param)):
    """当前项目全部异步任务（created_at 倒序，GAP-11 任务面板）。

    注意：本路由必须先注册于 GET /{ontology_id} 之前——单段路径 /jobs 会被其通配吞掉。
    """
    items = [j for j in JOBS.values() if j.get("project_id") == project_id]
    items.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return success(list_payload(items, total=len(items), page=1, page_size=len(items) or 1))


@router.get("/{ontology_id}")
def get_ontology(ontology_id: str, request: Request,
                 version: Optional[str] = Query(default=None,
                                                description="V2-07 固定版本引用（TC-07-06）；缺省=当前"),
                 project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, rec)  # V2-06：不可见=404（不泄露存在性）
    rec = dict(rec)
    rec.setdefault("sharing", effective_sharing(rec))
    if version:  # V2-07：旧版本持续可用，按快照覆盖模型字段
        from api.router_changesets import resolve_versioned
        _rec, override = resolve_versioned(rec, version)
        rec = {**rec, **override, "version": version}
    return success(rec)


@router.patch("/{ontology_id}/sharing")
def update_sharing(
    ontology_id: str,
    payload: dict,
    request: Request,
    project_id: str = Depends(project_id_param),
):
    """V2-06 / TC-06-03·04：调整本体共享等级（仅 owner/admin）。

    body: {"sharing": "private|project|department|public", "shared_with": ["username", ...]}
    - project 级必须给出 shared_with（可跨租户指定成员）；指定无效账号 422（TCE-06-01）；
    - 变更即时生效（每次请求实时计算，降级立即收口，TCE-06-03）；
    - 审计 sharing_change：谁/何时/从哪级到哪级（TC-06-04）。
    """
    store = _store_for(project_id)
    rec = store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    user = current_user(request) or {}
    if not bypass():
        ensure_schema_visible(request, project_id, rec)
        if not can_manage_sharing(user, project_id, rec):
            raise ApiError("FORBIDDEN", "仅建模人本人或管理员可调整共享等级", http_status=403)
    new_level = payload.get("sharing")
    if new_level not in SHARING_LEVELS:
        raise ApiError("VALIDATION_ERROR", f"sharing 必须是 {sorted(SHARING_LEVELS)}", http_status=422)
    shared_with = payload.get("shared_with")
    if new_level == "project":
        if not isinstance(shared_with, list) or not shared_with:
            raise ApiError("VALIDATION_ERROR", "项目组共享必须给出 shared_with 指定成员列表", http_status=422)
        import auth as auth_svc
        existing = {u["username"] for u in auth_svc.list_users()}
        invalid = [u for u in shared_with if u not in existing]
        if invalid:
            raise ApiError("VALIDATION_ERROR", "共享指定了无效账号", http_status=422,
                           details={"invalid_users": invalid})
    else:
        shared_with = []
    old_level = effective_sharing(rec)
    updated = store.update(ontology_id, {
        "sharing": new_level,
        "shared_with": shared_with,
        "updated_at": _now(),
    })
    audit_sharing_change(
        (user.get("username") or "dev"), project_id, ontology_id,
        old_level, new_level, shared_with,
    )
    logger.info("sharing_change ontology=%s project=%s %s->%s members=%d",
                ontology_id, project_id, old_level, new_level, len(shared_with))
    return success(updated)


@router.put("/{ontology_id}")
def update_ontology(
    ontology_id: str,
    payload: Ontology,
    request: Request,
    project_id: str = Depends(project_id_param),
    namespace: str | None = Depends(namespace_param),
):
    store = _store_for(project_id)
    existing = store.get(ontology_id)
    if not existing:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, existing)
    ensure_schema_write(request, project_id, existing)  # V2-06：共享只授 schema 只读（TC-06-08）
    _ensure_editable(existing)  # V2-07（TCE-07-02）：已发布本体须经变更提案
    old_stable_keys = {o.get("id"): o.get("stable_key") for o in existing.get("objects", [])}
    patch = payload.model_dump(exclude_unset=True)
    patch.pop("id", None)
    patch["updated_at"] = _now()
    if namespace:
        patch["namespace"] = namespace
    # 2.2：更新路径同样执行业务层校验（与 create 一致），失败时返回具体 violations
    if "objects" in patch:
        # 与 create 对齐：自动补齐 business_definition/stable_key（表单不采集，PUT 整体替换会丢原值）
        ns = patch.get("namespace") or existing.get("namespace") or project_id
        for obj in patch["objects"]:
            _backfill_object_fields(obj, ns)
        merged = dict(existing)
        merged.update(patch)
        biz_errors = blocking_violations(merged)
        if biz_errors:
            raise ApiError("VALIDATION_ERROR", "业务层校验未通过", http_status=422,
                           details={"violations": biz_errors})
    before_view = _model_view(existing)
    updated = store.update(ontology_id, patch)
    _sync_rdf(project_id, ontology_id)
    audit_svc.record_from_request(request, "ontology_update", project_id=project_id,
                                  target_type="ontology", target_id=ontology_id,
                                  target_name=existing.get("name", ""),
                                  before=before_view, after=_model_view(updated))
    sidecar_svc.record_from_request(request, sidecar_svc.TYPE_ONTOLOGY_CHANGE, project_id=project_id,
                                    target_type="ontology", target_id=ontology_id,
                                    payload={"action": "update", "name": existing.get("name", "")})
    logger.info("update_ontology id=%s project_id=%s", ontology_id, project_id)
    # V2-03 EP-03-01：前端表单保存（PUT）走这条路，主键变更也要给出存量实例影响提示
    if "objects" in patch:
        for obj in patch["objects"]:
            if old_stable_keys.get(obj.get("id")) != obj.get("stable_key"):
                impact = _primary_key_impact(project_id, ontology_id, obj.get("id"),
                                             old_stable_keys.get(obj.get("id")), obj.get("stable_key"))
                return success({**updated, "primary_key_change": impact})
    return success(updated)


def _instance_records(project_id: str, ontology_id: str, object_id: str) -> list[dict[str, Any]]:
    """该本体下某对象类型的全部实例（无 ontology_id 的历史记录默认归属当前本体）。"""
    return [r for r in get_instance_store(project_id=project_id).find(object_type=object_id, limit=None)
            if (r.get("ontology_id") or ontology_id) == ontology_id]


def _primary_key_impact(project_id: str, ontology_id: str, object_id: str,
                        old_key: str | None, new_key: str | None) -> dict[str, Any]:
    """V2-03 / EP-03-01：主键变更影响提示；存量实例 stable_key 不静默改写。"""
    recs = _instance_records(project_id, ontology_id, object_id)
    legacy = [r for r in recs if r.get("stable_key") != new_key]
    return {
        "object_id": object_id,
        "previous_stable_key": old_key,
        "current_stable_key": new_key,
        "existing_instances": len(recs),
        "legacy_instances": len(legacy),
        "migration_required": bool(recs) and bool(legacy),
        "hint": (f"已有 {len(recs)} 条实例；存量 stable_key 不随本次变更静默改写，"
                 "新入库实例按新主键匹配/去重。如需迁移存量，请显式调用 "
                 "primary-key/migrate 并 confirm=true（缺失新主键值的实例会跳过，不产生键断裂）。"),
    }


@router.patch("/{ontology_id}/objects/{object_id}")
def patch_ontology_object(
    ontology_id: str,
    object_id: str,
    payload: OntologyObjectPatch,
    request: Request,
    project_id: str = Depends(project_id_param),
    namespace: str | None = Depends(namespace_param),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """就地更新本体内单个对象（对象元数据/约束/父级/显式业务主键）。

    V2-03：传 `stable_key` 字符串即显式指定（标记 stable_key_explicit=true，
    校验属性存在且 data_type=string，失败 422）；显式传 null 回退自动推断；
    不传保持旧值。变更主键时返回 `data.primary_key_change` 影响提示，不改写存量实例。
    `If-Match` / 原子乐观锁保证并发两请求中后到者不会基于旧状态覆盖最新结果（409）。
    """
    store = _store_for(project_id)
    existing = store.get(ontology_id)
    if not existing:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, existing)
    ensure_schema_write(request, project_id, existing)
    _ensure_editable(existing)  # V2-07（TCE-07-02）：已发布本体须经变更提案
    expected_updated_at = existing.get("updated_at")
    if if_match and if_match != expected_updated_at:
        raise ApiError("STALE_ONTOLOGY", "本体已被其它请求更新，请刷新后重试（后到者须基于最新状态提交）",
                       http_status=409)
    objects = existing.get("objects", [])
    idx = next((i for i, o in enumerate(objects) if o.get("id") == object_id), None)
    if idx is None:
        raise ApiError("NOT_FOUND", "对象不存在", http_status=404)
    old_key = objects[idx].get("stable_key")
    old_obj = dict(objects[idx])  # M2-D：审计前值

    patch = payload.model_dump(exclude_unset=True)
    patch["id"] = object_id
    pk_supplied = "stable_key" in patch
    # 局部更新语义：请求体未传的字段保持旧值（显式传 null 清除 parent），
    # 否则仅改 parent 会把 name/attributes 等冲掉，导致业务门误报 stable_key 不属于本对象
    for key in ("name", "description", "namespace", "stable_key", "stable_key_explicit",
                "business_definition", "parent"):
        if key not in patch:
            patch[key] = objects[idx].get(key)
    if pk_supplied:
        patch["stable_key_explicit"] = bool(patch.get("stable_key"))
    patch["stable_key_explicit"] = bool(patch.get("stable_key_explicit"))
    if "attributes" not in patch:
        patch["attributes"] = objects[idx].get("attributes") or []
    if "relations" not in patch:
        patch["relations"] = objects[idx].get("relations") or []
    # 对象级 namespace 默认继承本体 namespace
    if not patch.get("namespace"):
        patch["namespace"] = namespace or existing.get("namespace", project_id)
    # 补全属性/关系缺失的默认业务语义
    for attr in patch.get("attributes") or []:
        attr.setdefault("constraints", {})
    # 刷新对象级稳定主键（未显式指定且无值时按三级回退自动推断）
    if not patch.get("stable_key"):
        attrs = patch.get("attributes") or []
        candidates = [a for a in attrs if a.get("required") and a.get("data_type", "string") == "string"]
        if not candidates and attrs:
            candidates = [a for a in attrs if a.get("data_type", "string") == "string"]
        if candidates:
            patch["stable_key"] = candidates[0]["id"]
        patch["stable_key_explicit"] = False

    objects[idx] = patch
    existing["objects"] = objects
    existing["updated_at"] = _now()
    # 业务层校验（与 PUT 路径一致）：主键须存在且显式值为 string、父存在/继承环等，失败不落库
    biz_errors = blocking_violations(existing)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "业务层校验未通过", http_status=422,
                       details={"violations": biz_errors})
    updated = store.update_if_unchanged(
        ontology_id, expected_updated_at,
        {"objects": objects, "updated_at": existing["updated_at"]},
    )
    if updated is None:
        raise ApiError("STALE_ONTOLOGY", "本体已被其它请求更新，请刷新后重试（并发主键变更未覆盖最新状态）",
                       http_status=409)
    _sync_rdf(project_id, ontology_id)
    audit_svc.record_from_request(request, "ontology_object_update", project_id=project_id,
                                  target_type="object", target_id=object_id,
                                  target_name=patch.get("name", ""),
                                  before=old_obj, after=dict(patch),
                                  detail={"ontology_id": ontology_id})
    sidecar_svc.record_from_request(request, sidecar_svc.TYPE_ONTOLOGY_CHANGE, project_id=project_id,
                                    target_type="object", target_id=object_id,
                                    payload={"action": "patch_object", "ontology_id": ontology_id,
                                             "name": patch.get("name", "")})
    logger.info("patch_object id=%s object_id=%s project_id=%s stable_key=%s explicit=%s",
                ontology_id, object_id, project_id, patch.get("stable_key"),
                patch.get("stable_key_explicit"))
    if old_key != patch.get("stable_key"):
        impact = _primary_key_impact(project_id, ontology_id, object_id,
                                     old_key, patch.get("stable_key"))
        return success({**updated, "primary_key_change": impact})
    return success(updated)


@router.get("/{ontology_id}/objects/{object_id}/primary-key/impact")
def primary_key_impact(
    ontology_id: str,
    object_id: str,
    stable_key: str | None = Query(default=None, description="拟设置的新主键；缺省用对象当前值"),
    project_id: str = Depends(project_id_param),
):
    """V2-03 / TCE-03-01：主键变更影响提示（存量实例数 / 是否需显式迁移）。"""
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    obj = next((o for o in rec.get("objects", []) if o.get("id") == object_id), None)
    if obj is None:
        raise ApiError("NOT_FOUND", "对象不存在", http_status=404)
    return success(_primary_key_impact(project_id, ontology_id, object_id,
                                       obj.get("stable_key"), stable_key or obj.get("stable_key")))


@router.post("/{ontology_id}/objects/{object_id}/primary-key/migrate")
def migrate_primary_key(
    ontology_id: str,
    object_id: str,
    payload: PrimaryKeyMigrationRequest,
    project_id: str = Depends(project_id_param),
):
    """V2-03 / EP-03-01：存量实例主键元数据迁移（必须 confirm=true）。

    只补写实例记录的 stable_key / stable_key_value 元数据，不改动 instance 内容；
    新主键值为空的实例跳过，避免键断裂。默认调用（confirm=false）一律 422 拒绝。
    """
    if not payload.confirm:
        raise ApiError("MIGRATION_NOT_CONFIRMED",
                       "存量实例主键迁移须显式确认（confirm=true）；默认不改写任何存量实例",
                       http_status=422)
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    obj = next((o for o in rec.get("objects", []) if o.get("id") == object_id), None)
    if obj is None:
        raise ApiError("NOT_FOUND", "对象不存在", http_status=404)
    key = obj.get("stable_key")
    if not key:
        raise ApiError("VALIDATION_ERROR", "对象未声明业务主键，无法迁移", http_status=422)
    store = get_instance_store(project_id=project_id)
    recs = _instance_records(project_id, ontology_id, object_id)
    n_updated = n_skipped = 0
    for r in recs:
        if r.get("stable_key") == key:
            continue
        value = (r.get("instance") or {}).get(key)
        if value is None or value == "":
            n_skipped += 1
            continue
        store.update(r["id"], {"stable_key": key, "stable_key_value": value,
                               "key_migrated_at": _now()})
        n_updated += 1
    return success({"object_id": object_id, "stable_key": key, "total": len(recs),
                    "updated": n_updated, "skipped": n_skipped,
                    "hint": "存量实例未静默改写：仅在本次显式确认后补写主键元数据；缺失新主键值的实例已跳过。"})


@router.delete("/{ontology_id}")
def delete_ontology(
    ontology_id: str,
    request: Request,
    force: bool = Query(default=False),
    project_id: str = Depends(project_id_param),
):
    """删除本体，并级联清理其 MCP 发布状态、实例数据与 RDF 图。

    - 若该本体处于 MCP 已发布状态：自动取消发布（内存态，随删除一并清除）；
    - 若存在实例数据：默认 409 返回数量，需 force=true 二次确认才连带删除；
    - force=true 时同时删除该本体的待审核导入候选（本体删除后已不可审批）。
    """
    store = _store_for(project_id)
    rec = store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, rec)
    ensure_schema_write(request, project_id, rec)

    # 1) MCP 发布状态：自动取消发布（内存态，随删除清除）
    unpublished = False
    from mcp_server import get_mcp_server
    mcp = get_mcp_server()
    if ontology_id in mcp.published_ids(project_id):
        mcp.unpublish(project_id, ontology_id)
        unpublished = True

    # 2) 实例数据：存在时需 force 二次确认
    from core.instance_store import JsonlInstanceStore
    inst_store = JsonlInstanceStore(project_id=project_id)
    # limit=None：全量语义。此前硬编码 limit=100000，超限时 force 删除会静默漏删
    instances = inst_store.find(filters={"ontology_id": ontology_id}, limit=None)
    if instances and not force:
        raise ApiError(
            "CONFLICT",
            f"该本体下还有 {len(instances)} 条实例数据，如需连同删除请用 force=true 再次确认",
            http_status=409,
            details={"instance_count": len(instances)},
        )
    instances_deleted = 0
    if force and instances:
        for r in instances:
            if r.get("id") and inst_store.delete(r["id"]):
                instances_deleted += 1

    # 3) 该本体的待审核导入候选：本体删除后不可再审批，一并清理
    candidates_deleted = 0
    cand_store = JsonStore("ingest_candidates", project_id=project_id)
    for c in cand_store.find(lambda r: r.get("ontology_id") == ontology_id):
        cid = c.get("id")
        if cid and cand_store.delete(cid):
            candidates_deleted += 1

    # 4) RDF 图（命名图/本地 TTL），失败不阻断主流程
    try:
        rdf_sync.delete_ontology(project_id, ontology_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("删除 RDF 图失败（不影响本体删除） ontology_id=%s: %s", ontology_id, e)

    # 5) 本体记录
    store.delete(ontology_id)
    audit_svc.record_from_request(request, "ontology_delete", project_id=project_id,
                                  target_type="ontology", target_id=ontology_id,
                                  target_name=rec.get("name", ""), before=_model_view(rec),
                                  detail={"unpublished": unpublished, "force": force,
                                          "instances_deleted": instances_deleted,
                                          "candidates_deleted": candidates_deleted})
    sidecar_svc.record_from_request(request, sidecar_svc.TYPE_ONTOLOGY_CHANGE, project_id=project_id,
                                    target_type="ontology", target_id=ontology_id,
                                    payload={"action": "delete", "name": rec.get("name", ""),
                                             "instances_deleted": instances_deleted})
    logger.info(
        "delete_ontology id=%s project_id=%s force=%s unpublished=%s instances_deleted=%d candidates_deleted=%d",
        ontology_id, project_id, force, unpublished, instances_deleted, candidates_deleted,
    )
    return success({
        "deleted": True,
        "unpublished": unpublished,
        "instances_deleted": instances_deleted,
        "candidates_deleted": candidates_deleted,
    })


@router.post("/{ontology_id}/enrich")
def enrich_ontology(
    ontology_id: str,
    file: UploadFile = File(...),
    instructions: str | None = Form(default=None),
    llm_endpoint: str | None = Form(default=None),
    model: str | None = Form(default=None),
    auto_save: bool = Form(default=False),
    project_id: str = Depends(project_id_param),
    namespace: str | None = Depends(namespace_param),
):
    """基于现有本体，上传 docx/xlsx/csv/txt 材料做增量增强。

    流程：
    1. 读取现有本体；若状态为 active 且未提供 snapshot 参数，自动做 snapshot 备份。
    2. 提取文档文本。
    3. 调用 LLM 做增量抽取：保留现有对象，在此基础上新增对象/属性/关系。
    4. 合并去重（按 id，id 冲突时以新增覆盖但保留现有业务定义）。
    5. 业务层校验。
    6. 默认仅返回预览（preview）和变更摘要；auto_save=true 时更新原本体并落 Jena。
    """
    store = _store_for(project_id)
    existing = store.get(ontology_id)
    if not existing:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    # V2-07（TCE-07-02）：已发布本体不可被 enrich 直接落库（可预览，须走变更提案）
    if auto_save and existing.get("status") == "active":
        raise ApiError("PUBLISHED_IMMUTABLE",
                       "已发布本体不可直接增强落库，请通过变更提案（POST /changesets）提交（V2-07）",
                       http_status=409)

    filename = (file.filename or "").lower()
    content = file.file.read()
    if not content:
        raise ApiError("VALIDATION_ERROR", "文件为空", http_status=422)

    # 1. 若当前是 active 状态，自动 snapshot 备份
    snapshot = None
    if existing.get("status") == "active":
        snapshot = {
            "snapshot_id": uuid.uuid4().hex,
            "ontology_id": ontology_id,
            "project_id": project_id,
            "note": f"enrich 自动备份: {filename}",
            "created_at": _now(),
            "model": existing,
        }
        _snapshots_for(project_id).insert(snapshot)

    # 2. 提取材料文本
    try:
        doc_text = _extract_document_text(filename, io.BytesIO(content))
    except ApiError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ApiError("PARSE_ERROR", f"文档解析失败: {e}", http_status=422) from e

    # 3. 调用 LLM 做增量抽取
    try:
        generated = _generate_ontology_enrichment(existing, doc_text, instructions or "", llm_endpoint, model)
        delta = _normalize_generated(generated)
    except ApiError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("enrich LLM 解析失败: %s", e)
        raise ApiError("LLM_ERROR", f"LLM 输出无法解析为合法本体增量: {e}", http_status=422)

    # 4. 合并去重
    merged, summary = _merge_ontology(existing, delta, namespace or existing.get("namespace") or project_id)

    # 5. 业务层校验
    biz_errors = blocking_violations(merged)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "增强后的本体未通过业务层校验",
                       http_status=422, details={"violations": biz_errors})

    # 6. 默认只返回预览
    if not auto_save:
        logger.info("enrich preview ontology_id=%s project_id=%s added_objects=%d added_attrs=%d added_rels=%d",
                    ontology_id, project_id, len(summary.added_objects), len(summary.added_attributes), len(summary.added_relations))
        return success({
            "ontology_id": ontology_id,
            "status": "preview",
            "snapshot_id": snapshot["snapshot_id"] if snapshot else None,
            "preview": merged,
            "summary": summary.model_dump(),
        })

    # auto_save=true：更新原本体并落 Jena
    patch = {
        "name": merged.get("name"),
        "description": merged.get("description"),
        "objects": merged.get("objects", []),
        "constraints": merged.get("constraints", []),
        "status": "draft",
        "updated_at": _now(),
    }
    updated = store.update(ontology_id, patch)
    _sync_rdf(project_id, ontology_id)
    logger.info("enrich saved ontology_id=%s project_id=%s added_objects=%d added_attrs=%d added_rels=%d",
                ontology_id, project_id, len(summary.added_objects), len(summary.added_attributes), len(summary.added_relations))
    return success({
        "ontology_id": ontology_id,
        "status": updated.get("status"),
        "snapshot_id": snapshot["snapshot_id"] if snapshot else None,
        "ontology": updated,
        "summary": summary.model_dump(),
    })


@router.post("/{ontology_id}/publish")
def publish_ontology(ontology_id: str, request: Request, project_id: str = Depends(project_id_param)):
    store = _store_for(project_id)
    rec = store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    if rec.get("status") != "draft":
        raise ApiError("VALIDATION_ERROR", "只有 draft 状态的本体可以发布", http_status=422)
    # V2-07（TC-07-06）：首次发布写入基线版本快照——旧版本持续可用（REST ?version=1.0 /
    # MCP version=1.0 / 版本清单），后续变更单以该基线做语义 diff
    from api import router_changesets as _cs
    import schemadiff as _sd
    now = _now()
    base_version = _sd.norm_version(rec.get("version"))
    versions = list(rec.get("versions") or [])
    if not any(v.get("version") == base_version for v in versions):
        user = current_user(request) or {}
        versions.append({
            "version": base_version,
            "published_at": now,
            "published_by": user.get("username") or "dev",
            "note": "初次发布（基线版本）",
            "changes": [],
            "model": _cs._model_of(rec),
        })
    updated = store.update(ontology_id, {
        "status": "active",
        "version": base_version,
        "published_version": base_version,
        "versions": versions,
        "updated_at": now,
    })
    audit_svc.record_from_request(request, "ontology_publish", project_id=project_id,
                                  target_type="ontology", target_id=ontology_id,
                                  target_name=rec.get("name", ""),
                                  before={"status": "draft", "version": rec.get("version")},
                                  after={"status": "active", "version": base_version})
    logger.info("publish_ontology id=%s project_id=%s", ontology_id, project_id)
    return success(updated)


# ---- GAP-09：生成草稿 vs 当前模型 差异审核（逐项采纳/拒绝、一键采纳全部） ----

class DraftDiffRequest(BaseModel):
    source_draft_id: str
    # 多文档增量建模：按名称/字典同义词/相似度把源草稿对象 ID 对齐到目标（默认开；歧义不对齐）
    align: bool = True


class AdoptChangesRequest(BaseModel):
    source_draft_id: str
    adopt: list[int] | Literal["all"]
    note: str | None = None
    align: bool = True  # 与 draft-diff 同一对齐口径，保证变更序号一致


def _model_core(rec: dict[str, Any]) -> dict[str, Any]:
    """模型核心三字段（objects/constraints/functions）深拷贝视图，供 diff/合并互不污染。"""
    return {k: copy.deepcopy(rec.get(k) or []) for k in ("objects", "constraints", "functions")}


def _diff_models(target: dict[str, Any], source: dict[str, Any],
                 project_id: str, align: bool) -> tuple[dict[str, Any], dict[str, Any], list[dict]]:
    """draft-diff 与 adopt 共用的取模＋对齐入口：两端口径必须一致（变更序号同源）。"""
    t, s = _model_core(target), _model_core(source)
    if align:
        s, alignment = align_mod.align_model(t, s, project_id)
    else:
        alignment = []
    return t, s, alignment


def _record_alignment_synonyms(project_id: str, alignment: list[dict]) -> int:
    """采纳后沉淀字典：语义对齐（名称不同但判定同一对象）的对象名对 → 词条同义词。

    人工确认合并即同义词晋升的输入来源（多文档建模与数据字典互哺闭环）；
    失败不影响采纳主链路，返回成功沉淀条数。
    """
    recorded = 0
    for e in alignment or []:
        if e.get("method") != "semantic":
            continue  # 精确/同义词通道无新增信息
        src_name, tgt_name = e.get("source_name") or "", e.get("target_name") or ""
        if not src_name or not tgt_name or dict_svc.normalize_text(src_name) == dict_svc.normalize_text(tgt_name):
            continue
        try:
            existing = next((t for t in dict_svc.list_terms(project_id)
                             if (t.get("object_type") or "") == (e.get("target_object_id") or "")
                             and not (t.get("property_id") or "")
                             and dict_svc.normalize_text(t.get("canonical")) == dict_svc.normalize_text(tgt_name)),
                            None)
            if existing:
                if src_name not in (existing.get("synonyms") or []):
                    dict_svc.promote_synonym(project_id, existing["id"], src_name)
            else:
                dict_svc.create_term(project_id, {
                    "canonical": tgt_name, "object_type": e.get("target_object_id") or "",
                    "synonyms": [src_name],
                })
            recorded += 1
        except Exception as exc:  # noqa: BLE001  沉淀失败不阻断采纳
            logger.warning("alignment synonym record failed (%s→%s): %s", tgt_name, src_name, exc)
    return recorded


def _find_obj(model: dict[str, Any], object_id: str | None) -> dict[str, Any] | None:
    return next((o for o in model.get("objects") or [] if o.get("id") == object_id), None)


def _fn_key(f: dict[str, Any]) -> str:
    return str(f.get("function_id") or f.get("name") or "")


def _apply_change(m: dict[str, Any], s: dict[str, Any], c: dict[str, Any]) -> bool:
    """把一条 diff 变更应用到目标模型 m（值取自源模型 s）；幂等，生效返回 True。"""
    kind = c.get("kind")
    oid, aid, rid = c.get("object_id"), c.get("attribute_id"), c.get("relation_id")
    if kind == "object_added":
        so = _find_obj(s, oid)
        if so and _find_obj(m, oid) is None:
            m.setdefault("objects", []).append(copy.deepcopy(so))
            return True
        return False
    if kind == "object_removed":
        m["objects"] = [o for o in m.get("objects") or [] if o.get("id") != oid]
        return True
    if kind in ("stable_key_changed", "parent_changed"):
        field = "stable_key" if kind == "stable_key_changed" else "parent"
        to, so = _find_obj(m, oid), _find_obj(s, oid)
        if to is None or so is None:
            return False
        if field in so:
            to[field] = copy.deepcopy(so[field])
        else:
            to.pop(field, None)
        return True
    if kind == "attribute_added":
        to, so = _find_obj(m, oid), _find_obj(s, oid)
        sa = next((a for a in (so or {}).get("attributes") or [] if a.get("id") == aid), None)
        if to is None or sa is None:
            return False
        if not any(a.get("id") == aid for a in to.get("attributes") or []):
            to.setdefault("attributes", []).append(copy.deepcopy(sa))
            return True
        return False
    if kind == "attribute_removed":
        to = _find_obj(m, oid)
        if to is None:
            return False
        to["attributes"] = [a for a in to.get("attributes") or [] if a.get("id") != aid]
        return True
    if kind in ("attribute_type_changed", "attribute_required_changed",
                "constraint_widened", "constraint_narrowed"):
        to, so = _find_obj(m, oid), _find_obj(s, oid)
        ta = next((a for a in (to or {}).get("attributes") or [] if a.get("id") == aid), None)
        sa = next((a for a in (so or {}).get("attributes") or [] if a.get("id") == aid), None)
        if ta is None or sa is None:
            return False
        if kind == "attribute_type_changed":
            ta["data_type"] = sa.get("data_type")
            return True
        if kind == "attribute_required_changed":
            ta["required"] = bool(sa.get("required"))
            return True
        key = c.get("constraint_key")
        if not key:
            return False
        tcons = ta.setdefault("constraints", {})
        if key in (sa.get("constraints") or {}):
            tcons[key] = copy.deepcopy(sa["constraints"][key])
        else:
            tcons.pop(key, None)
        return True
    if kind == "relation_added":
        to, so = _find_obj(m, oid), _find_obj(s, oid)
        sr = next((r for r in (so or {}).get("relations") or [] if r.get("id") == rid), None)
        if to is None or sr is None:
            return False
        if not any(r.get("id") == rid for r in to.get("relations") or []):
            to.setdefault("relations", []).append(copy.deepcopy(sr))
            return True
        return False
    if kind == "relation_removed":
        to = _find_obj(m, oid)
        if to is None:
            return False
        to["relations"] = [r for r in to.get("relations") or [] if r.get("id") != rid]
        return True
    if kind == "relation_changed":
        to, so = _find_obj(m, oid), _find_obj(s, oid)
        sr = next((r for r in (so or {}).get("relations") or [] if r.get("id") == rid), None)
        if to is None or sr is None:
            return False
        for i, r in enumerate(to.get("relations") or []):
            if r.get("id") == rid:
                to["relations"][i] = copy.deepcopy(sr)
                return True
        return False
    if kind in ("constraint_added", "constraint_modified", "constraint_removed"):
        cid = c.get("constraint_id")
        if kind == "constraint_removed":
            m["constraints"] = [x for x in m.get("constraints") or [] if x.get("id") != cid]
            return True
        sc = next((x for x in s.get("constraints") or [] if x.get("id") == cid), None)
        if sc is None:
            return False
        cons = m.setdefault("constraints", [])
        if kind == "constraint_added":
            if not any(x.get("id") == cid for x in cons):
                cons.append(copy.deepcopy(sc))
                return True
            return False
        for i, x in enumerate(cons):
            if x.get("id") == cid:
                cons[i] = copy.deepcopy(sc)
                return True
        cons.append(copy.deepcopy(sc))
        return True
    if kind in ("function_added", "function_modified", "function_removed"):
        fid = c.get("function_id")
        if kind == "function_removed":
            m["functions"] = [f for f in m.get("functions") or [] if _fn_key(f) != fid]
            return True
        sf = next((f for f in s.get("functions") or [] if _fn_key(f) == fid), None)
        if sf is None:
            return False
        fns = m.setdefault("functions", [])
        if kind == "function_added":
            if not any(_fn_key(f) == fid for f in fns):
                fns.append(copy.deepcopy(sf))
                return True
            return False
        for i, f in enumerate(fns):
            if _fn_key(f) == fid:
                fns[i] = copy.deepcopy(sf)
                return True
        fns.append(copy.deepcopy(sf))
        return True
    return False


@router.post("/{ontology_id}/draft-diff")
def draft_diff(
    ontology_id: str,
    payload: DraftDiffRequest,
    request: Request,
    project_id: str = Depends(project_id_param),
):
    """GAP-09：生成结果（来源草稿）vs 当前模型（本本体）的结构化差异。

    返回 schemadiff 变更列表（每项带 index，供逐项采纳）＋双方摘要；只读，不做任何修改。
    """
    store = _store_for(project_id)
    target = store.get(ontology_id)
    if not target:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, target)
    source = store.get(payload.source_draft_id)
    if not source:
        raise ApiError("NOT_FOUND", "来源草稿不存在", http_status=404)
    ensure_schema_visible(request, project_id, source)
    if source.get("status") != "draft":
        raise ApiError("VALIDATION_ERROR", "来源必须是草稿（AI/文档/表格生成未发布）", http_status=422)
    t, s, alignment = _diff_models(target, source, project_id, payload.align)
    diff = sd.diff_ontology(t, s)
    for i, ch in enumerate(diff["changes"]):
        ch["index"] = i
    return success({
        "target": {
            "id": target["id"], "name": target.get("name"), "status": target.get("status"),
            "version": target.get("version"), "object_count": len(target.get("objects") or []),
        },
        "source": {
            "id": source["id"], "name": source.get("name"), "status": source.get("status"),
            "draft_source": source.get("draft_source"), "generation_mode": source.get("generation_mode"),
            "object_count": len(source.get("objects") or []),
        },
        "diff": diff,
        "change_count": len(diff["changes"]),
        "alignment": alignment,
        "aligned_count": len(alignment),
    })


@router.post("/{ontology_id}/adopt-changes")
def adopt_changes(
    ontology_id: str,
    payload: AdoptChangesRequest,
    request: Request,
    project_id: str = Depends(project_id_param),
):
    """GAP-09：把差异审核中勾选的变更合并进当前模型（目标必须是 draft）。

    adopt：变更 index 列表或 "all"。先重新 diff 再按 index 逐项应用（幂等，已生效的计入 skipped）；
    合并结果与 PUT 编辑一样过 blocking_violations 业务层校验。active 目标不可直接采纳（409，走变更提案）。
    """
    store = _store_for(project_id)
    target = store.get(ontology_id)
    if not target:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ensure_schema_visible(request, project_id, target)
    ensure_schema_write(request, project_id, target)
    _ensure_editable(target)
    source = store.get(payload.source_draft_id)
    if not source:
        raise ApiError("NOT_FOUND", "来源草稿不存在", http_status=404)
    ensure_schema_visible(request, project_id, source)
    if source.get("status") != "draft":
        raise ApiError("VALIDATION_ERROR", "来源必须是草稿（AI/文档/表格生成未发布）", http_status=422)
    if source["id"] == target["id"]:
        raise ApiError("VALIDATION_ERROR", "来源草稿不能是目标本体自身", http_status=422)

    t, source_model, alignment = _diff_models(target, source, project_id, payload.align)
    diff = sd.diff_ontology(t, source_model)
    changes = diff["changes"]
    if payload.adopt == "all":
        chosen = list(range(len(changes)))
    else:
        bad = [i for i in payload.adopt if not isinstance(i, int) or i < 0 or i >= len(changes)]
        if bad:
            raise ApiError("VALIDATION_ERROR", "包含无效的变更序号（请先重新对比）", http_status=422,
                           details={"invalid_indexes": bad, "count": len(changes)})
        chosen = sorted(set(payload.adopt))

    merged = copy.deepcopy(t)
    applied = skipped = 0
    for i in chosen:
        if _apply_change(merged, source_model, changes[i]):
            applied += 1
        else:
            skipped += 1
    if applied == 0:
        return success({**target, "adopted": 0, "skipped": skipped, "source_id": source["id"],
                        "note": "所选变更均无实际变化"})

    patch = {
        "objects": merged["objects"],
        "constraints": merged["constraints"],
        "functions": merged["functions"],
        "updated_at": _now(),
    }
    merged_rec = dict(target)
    merged_rec.update(patch)
    biz_errors = blocking_violations(merged_rec)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "采纳后的模型未通过业务层校验（未写入）", http_status=422,
                       details={"violations": biz_errors})
    updated = store.update(ontology_id, patch)
    _sync_rdf(project_id, ontology_id)
    synonyms_recorded = _record_alignment_synonyms(project_id, alignment)
    audit_svc.record_from_request(request, "ontology_adopt_changes", project_id=project_id,
                                  target_type="ontology", target_id=ontology_id,
                                  target_name=target.get("name", ""),
                                  before=_model_view(target), after=_model_view(updated),
                                  detail={"source_id": source["id"], "adopted": applied,
                                          "skipped": skipped, "note": payload.note or "",
                                          "aligned_objects": len(alignment),
                                          "synonyms_recorded": synonyms_recorded})
    sidecar_svc.record_from_request(request, sidecar_svc.TYPE_ONTOLOGY_CHANGE, project_id=project_id,
                                    target_type="ontology", target_id=ontology_id,
                                    payload={"action": "adopt_changes", "name": target.get("name", ""),
                                             "source_id": source["id"], "adopted": applied})
    logger.info("adopt_changes target=%s source=%s applied=%d skipped=%d aligned=%d synonyms=%d project=%s",
                ontology_id, source["id"], applied, skipped, len(alignment), synonyms_recorded, project_id)
    note = f"已采纳 {applied} 项变更" + (f"（{skipped} 项无变化跳过）" if skipped else "")
    if len(alignment):
        note += f"；自动对齐 {len(alignment)} 个同名对象"
    if synonyms_recorded:
        note += f"；沉淀字典同义词 {synonyms_recorded} 条"
    return success({**updated, "adopted": applied, "skipped": skipped, "source_id": source["id"],
                    "aligned_count": len(alignment), "synonyms_recorded": synonyms_recorded, "note": note})


@router.post("/{ontology_id}/validate")
def validate_ontology(ontology_id: str, req: ValidateRequest | None = None, project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    # structural 含 warning 级条目（如跨对象属性 ID 复用提示）：展示给用户但不影响 valid；
    # valid 仅由 error 级结构问题 + SHACL 违规决定
    structural = validate_business_layer(rec)
    structural_blocking = [v for v in structural if v.get("severity") == "error"]
    shapes = translate_to_shacl(rec)
    data = build_instance_graph(rec, (req.sample_instances or [])) if req else Graph()
    shacl = shacl_validate(data, shapes)
    if shacl is None:
        violations = [{"severity": v["severity"], "path": v.get("path"),
                       "focus_node": None, "message": v["message"]} for v in structural]
        valid = len(structural_blocking) == 0
    else:
        violations = shacl["violations"] + [
            {"severity": v["severity"], "path": v.get("path"),
             "focus_node": None, "message": v["message"]} for v in structural
        ]
        valid = len(shacl["violations"]) == 0 and len(structural_blocking) == 0
    result = ValidateResult(valid=valid, violations=violations)
    return success(result.model_dump())


@router.post("/{ontology_id}/export")
def export_ontology(ontology_id: str, _req: ExportRequest | None = None, project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ttl = translate_to_turtle(rec)
    filename = f"{rec.get('name', ontology_id)}.ttl".replace(" ", "_")
    return success({"format": "turtle", "content": ttl, "filename": filename})


@router.post("/{ontology_id}/snapshot")
def snapshot_ontology(ontology_id: str, req: dict | None = None, project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    snap = {
        "snapshot_id": uuid.uuid4().hex,
        "ontology_id": ontology_id,
        "project_id": project_id,
        "note": (req or {}).get("note"),
        "created_at": _now(),
        "model": rec,
    }
    _snapshots_for(project_id).insert(snap)
    logger.info("snapshot ontology_id=%s project_id=%s", ontology_id, project_id)
    return success(snap, http_status=201)


@router.get("/{ontology_id}/snapshots")
def list_snapshots(
    ontology_id: str,
    project_id: str = Depends(project_id_param),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    page, page_size = parse_pagination(page, page_size)
    items = _snapshots_for(project_id).find(lambda r: r.get("ontology_id") == ontology_id)
    items.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


# ---- 用户友好的逆向/辅助生成入口（VS5+） ----


def _save_draft(record: dict[str, Any], source: str, project_id: str, generation_mode: str | None = None) -> dict[str, Any]:
    record["id"] = uuid.uuid4().hex
    record["modeling_type"] = "reverse"
    record["status"] = "draft"
    record["project_id"] = project_id
    record["draft_source"] = source
    # generation_mode: "llm"=模型真实产出 / "fallback"=确定性兜底（LLM 失败降级），供 UI 明确提示
    if generation_mode:
        record["generation_mode"] = generation_mode
    record["created_at"] = _now()
    record["updated_at"] = record["created_at"]
    _store_for(project_id).insert(record)
    return record


def _normalize_generated(generated: dict[str, Any]) -> dict[str, Any]:
    """清洗 LLM/Excel 输出，使其符合业务层校验。"""
    # 兼容 LLM 偶尔返回的 OWL/类图风格结构：ontology/classes/objectProperties/dataProperties
    objects = _convert_to_object_schema(generated)

    for obj in objects:
        obj.setdefault("attributes", [])
        obj.setdefault("relations", [])
        for attr in obj["attributes"]:
            attr.setdefault("required", False)
            attr.setdefault("constraints", {})
        for rel in obj.get("relations", []):
            rel.setdefault("cardinality", "many_to_one")

        # O-01: 自动补齐业务语义定义与稳定主键
        biz_def = (obj.get("business_definition") or obj.get("description") or f"{obj.get('name', '对象')}的业务语义").strip()
        if len(biz_def) < 5:
            biz_def = f"{obj.get('name', '对象')}的业务语义定义"
        obj["business_definition"] = biz_def

        attrs = obj.get("attributes", [])
        if not obj.get("stable_key") and attrs:
            # 优先选 required string 属性作为业务主键
            candidates = [a for a in attrs if a.get("required") and a.get("data_type", "string") == "string"]
            if not candidates:
                candidates = [a for a in attrs if a.get("data_type", "string") == "string"]
            if not candidates:
                candidates = attrs
            obj["stable_key"] = candidates[0]["id"]
    return {
        "name": generated.get("name") or generated.get("ontology", {}).get("name") or generated.get("ontology", {}).get("label") or "未命名本体",
        "description": generated.get("description") or generated.get("ontology", {}).get("description", ""),
        "objects": objects,
        "constraints": generated.get("constraints", []),
        # O-02/V2-19: 透传 LLM 生成的 functions（能力/计算函数），供本体函数元模型存储；
        # blocking_violations 不校验 functions，故此处不会引入拦截。
        "functions": generated.get("functions", []),
        "version": "1",
    }


def _convert_to_object_schema(generated: dict[str, Any]) -> list[dict[str, Any]]:
    """把可能的 OWL/classes 风格输出转成系统要求的 objects/attributes/relations 结构。"""
    objects: list[dict[str, Any]] = generated.get("objects") or []
    if objects:
        return objects

    classes: list[dict[str, Any]] = generated.get("classes") or []
    if not classes:
        return []

    # 建立 class id -> object 的映射
    obj_map: dict[str, dict[str, Any]] = {}
    for cls in classes:
        cid = cls.get("id") or cls.get("classId") or cls.get("name")
        if not cid:
            continue
        obj_map[cid] = {
            "id": cid,
            "name": cls.get("name") or cls.get("label") or cls.get("id"),
            "business_definition": cls.get("description") or cls.get("definition") or cls.get("comment", ""),
            "attributes": [],
            "relations": [],
        }

    # dataProperties -> attributes
    for prop in generated.get("dataProperties", generated.get("datatype_properties", [])):
        pid = prop.get("id") or prop.get("propertyId") or prop.get("name")
        pname = prop.get("name") or prop.get("label") or pid
        domain = prop.get("domain") or prop.get("domainClass")
        dtype = (prop.get("data_type") or prop.get("dataType") or prop.get("range") or "string").lower()
        if dtype not in ("string", "integer", "float", "boolean", "date"):
            dtype = "string"
        if domain and domain in obj_map:
            obj_map[domain]["attributes"].append({
                "id": pid,
                "name": pname,
                "data_type": dtype,
                "required": False,
                "constraints": {},
            })

    # objectProperties -> relations
    for prop in generated.get("objectProperties", generated.get("object_properties", [])):
        pid = prop.get("id") or prop.get("propertyId") or prop.get("name")
        pname = prop.get("name") or prop.get("label") or pid
        domain = prop.get("domain") or prop.get("domainClass")
        range_ = prop.get("range") or prop.get("rangeClass") or prop.get("target")
        if domain and domain in obj_map and range_ and range_ in obj_map:
            obj_map[domain]["relations"].append({
                "id": pid,
                "name": pname,
                "target_object": range_,
                "cardinality": "many_to_one",
            })

    return list(obj_map.values())


@router.post("/from-text", status_code=201)
def create_from_text(payload: OntologyFromTextRequest, project_id: str = Depends(project_id_param)):
    """自然语言描述 → 生成本体草稿（待确认落库）。"""
    if not payload.text.strip():
        raise ApiError("VALIDATION_ERROR", "text 不能为空", http_status=422)
    try:
        generated, gen_mode = generate_ontology_from_text(
            payload.text, payload.llm_endpoint, payload.model, max_tokens=payload.max_tokens
        )
        record = _normalize_generated(generated)
    except ApiError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("from-text LLM 解析失败: %s", e)
        raise ApiError("LLM_ERROR", f"LLM 输出无法解析为合法本体: {e}", http_status=422)
    biz_errors = blocking_violations(record)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "生成的本体未通过业务层校验", http_status=422, details={"violations": biz_errors})
    draft = _save_draft(record, "text", project_id, gen_mode)
    logger.info("from-text draft_id=%s project_id=%s generation_mode=%s", draft["id"], project_id, gen_mode)
    return success(draft, http_status=201)


@router.post("/from-excel", status_code=201)
def create_from_excel(file: UploadFile = File(...), name: str | None = Form(default=None), project_id: str = Depends(project_id_param)):
    """上传 CSV/Excel 数据表 → 生成可编辑本体草稿（GAP-04）。

    支持三种格式（自动识别）：
    1) 宽数据表（最常用）：首行列名（列=属性）、其余为数据行——单对象草稿，
       类型/必填/业务主键/约束（min/max、长度、枚举）由样本自动推断；
    2) 长表（属性清单）：列含 对象/属性/类型/必填/关系目标/基数，可选约束列
       （最小长度/最大长度/正则/最小值/最大值/枚举）；
    3) B0 三 sheet 模板（1-类层次/2-对象属性(关系)/3-数据属性）。
    `name` 可覆盖本体名（缺省：宽表取文件名主干）。
    """
    filename = (file.filename or "").lower()
    content = file.file.read()
    if not content:
        raise ApiError("VALIDATION_ERROR", "文件为空", http_status=422)
    try:
        generated = _parse_spreadsheet(filename, io.BytesIO(content), name=name)
        record = _normalize_generated(generated)
    except Exception as e:  # noqa: BLE001
        logger.warning("from-excel 解析失败: %s", e)
        raise ApiError("PARSE_ERROR", f"文件解析失败: {e}", http_status=422)
    biz_errors = blocking_violations(record)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "表格生成的本体未通过业务层校验", http_status=422, details={"violations": biz_errors})
    draft = _save_draft(record, "excel", project_id)
    logger.info("from-excel draft_id=%s project_id=%s", draft["id"], project_id)
    return success(draft, http_status=201)


@router.post("/from-doc", status_code=202)
async def create_from_doc(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(default=[]),
    file: UploadFile | None = File(default=None),
    llm_endpoint: str | None = Form(default=None),
    model: str | None = Form(default=None),
    ontology_id: str | None = Form(default=None),
    project_id: str = Depends(project_id_param),
):
    """上传 PRD/Word/Excel 文档 → 后台解析为文本 → LLM 抽取本体草稿（异步任务，返回 job_id）。支持多文件。

    多文档增量建模：可选 ontology_id 指定已有本体作为合并目标——文档仍独立抽取成新草稿，
    完成后前端自动进入「草稿→目标」差异审核；diff/采纳侧按名称/字典同义词/相似度
    确定性对齐同名对象（歧义不对齐、不猜测），逐项人工确认后合并进目标。
    """
    all_files: list[UploadFile] = list(files) if files else []
    if file:
        all_files.append(file)
    if not all_files:
        raise ApiError("VALIDATION_ERROR", "文件不能为空", http_status=422)
    file_infos: list[tuple[str, bytes]] = []
    for f in all_files:
        filename = (f.filename or "").lower()
        content = await f.read()
        if not content:
            raise ApiError("VALIDATION_ERROR", f"文件 {filename} 为空", http_status=422)
        file_infos.append((filename, content))

    merge_target: dict[str, Any] | None = None
    if ontology_id:
        merge_target = _store_for(project_id).get(ontology_id)
        if not merge_target:
            raise ApiError("NOT_FOUND", "合并目标本体不存在", http_status=404)

    job_id = uuid.uuid4().hex
    job = _new_job_record(job_id, project_id, file_infos)
    if merge_target:
        job["merge_target_id"] = merge_target["id"]
        job["merge_target_name"] = merge_target.get("name") or ""
    JOBS[job_id] = job
    _JOBS_PARAMS[job_id] = {"file_infos": file_infos, "llm_endpoint": llm_endpoint, "model": model,
                            "ontology_id": merge_target["id"] if merge_target else None}
    _JOBS_CANCEL[job_id] = threading.Event()

    background_tasks.add_task(_run_doc_extraction, job_id, file_infos, llm_endpoint, model, project_id)
    logger.info("from-doc job=%s project_id=%s files=%s merge_target=%s",
                job_id, project_id, [f[0] for f in file_infos],
                merge_target.get("id") if merge_target else None)
    return success({"job_id": job_id, "status": "pending", "project_id": project_id,
                    "merge_target_id": merge_target.get("id") if merge_target else None}, http_status=202)


def _run_doc_extraction(
    job_id: str,
    file_infos: list[tuple[str, bytes]],
    llm_endpoint: str | None,
    model: str | None,
    project_id: str,
) -> None:
    """from-doc 后台任务：解析文档、调用 LLM、保存草稿并更新 JOBS（GAP-11：进度上报＋协作式取消）。"""
    cancel_evt = _JOBS_CANCEL.get(job_id)
    total = max(len(file_infos), 1)

    def _cancelled() -> bool:
        if cancel_evt is not None and cancel_evt.is_set():
            _update_job(job_id, status="cancelled", stage="已取消")
            logger.info("from-doc job=%s cancelled", job_id)
            return True
        return False

    try:
        _update_job(job_id, status="running", stage="解析文档", progress=1)
        texts: list[str] = []
        for idx, (filename, content) in enumerate(file_infos):
            if _cancelled():
                return
            _update_job(job_id, stage=f"解析文档 {idx + 1}/{total}：{filename}",
                        progress=5 + int((idx / total) * 30))
            texts.append(f"--- 文档: {filename} ---\n" + _extract_document_text(filename, io.BytesIO(content)))
        if _cancelled():
            return
        combined_text = "\n\n".join(texts)
        _update_job(job_id, stage="LLM 抽取本体", progress=40)
        generated, gen_mode = generate_ontology_from_document(combined_text, llm_endpoint, model)
        if _cancelled():
            return
        _update_job(job_id, stage="业务层校验", progress=70)
        record = _normalize_generated(generated)
        biz_errors = blocking_violations(record)
        if biz_errors:
            _update_job(job_id, status="error", stage="业务层校验未通过",
                        error=f"业务层校验未通过: {biz_errors}")
            logger.warning("from-doc job=%s 业务层校验未通过: %s", job_id, biz_errors)
            return
        _update_job(job_id, stage="保存草稿", progress=85)
        draft = _save_draft(record, "document", project_id, gen_mode)
        _update_job(job_id, status="done", stage="完成", progress=100, draft_id=draft["id"])
        logger.info("from-doc job=%s done draft_id=%s generation_mode=%s", job_id, draft["id"], gen_mode)
    except Exception as e:  # noqa: BLE001
        logger.warning("from-doc job=%s 失败: %s", job_id, e)
        _update_job(job_id, status="error", stage="执行失败", error=f"{type(e).__name__}: {e}")


def _get_job_or_404(job_id: str, project_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise ApiError("NOT_FOUND", "任务不存在", http_status=404)
    # 简单项目归属校验
    if job.get("project_id") and job.get("project_id") != project_id:
        raise ApiError("FORBIDDEN", "任务不属于当前项目", http_status=403)
    return job


@router.get("/jobs/{job_id}")
def get_job(job_id: str, project_id: str = Depends(project_id_param)):
    return success(_get_job_or_404(job_id, project_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, project_id: str = Depends(project_id_param)):
    """取消进行中的异步任务（GAP-11：协作式，在下一个检查点生效；进行中的 LLM 调用不会被强制中断）。"""
    job = _get_job_or_404(job_id, project_id)
    if job.get("status") not in ("pending", "running"):
        raise ApiError("VALIDATION_ERROR", f"任务处于 {job.get('status')} 状态，无法取消", http_status=409)
    evt = _JOBS_CANCEL.get(job_id)
    if evt is not None:
        evt.set()
    _update_job(job_id, status="cancelled", stage="已取消")
    logger.info("from-doc job=%s cancelled by user", job_id)
    return success(job)


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    project_id: str = Depends(project_id_param),
):
    """重试失败/已取消的异步任务（GAP-11：复用原上传参数，新建 job 并返回新 job_id）。"""
    job = _get_job_or_404(job_id, project_id)
    if job.get("status") not in ("error", "cancelled"):
        raise ApiError("VALIDATION_ERROR",
                       f"仅失败/已取消任务可重试，当前状态 {job.get('status')}", http_status=409)
    params = _JOBS_PARAMS.get(job_id)
    if params is None:
        raise ApiError("VALIDATION_ERROR",
                       "原任务参数已不可用（服务可能已重启），请重新上传文档", http_status=409)

    new_id = uuid.uuid4().hex
    file_infos = params["file_infos"]
    record = _new_job_record(new_id, project_id, file_infos)
    record["retried_from"] = job_id
    # 多文档增量建模：重试沿用原合并目标（目标已被删除则退化为普通 from-doc，不阻断重试）
    if params.get("ontology_id"):
        mt = _store_for(project_id).get(params["ontology_id"])
        if mt:
            record["merge_target_id"] = mt["id"]
            record["merge_target_name"] = mt.get("name") or ""
        else:
            logger.warning("from-doc retry job=%s: merge target %s 不存在，退化为普通解析",
                           new_id, params.get("ontology_id"))
    JOBS[new_id] = record
    _JOBS_PARAMS[new_id] = params
    _JOBS_CANCEL[new_id] = threading.Event()

    background_tasks.add_task(
        _run_doc_extraction, new_id, file_infos,
        params.get("llm_endpoint"), params.get("model"), project_id,
    )
    logger.info("from-doc job=%s retried as new job=%s project_id=%s", job_id, new_id, project_id)
    return success({"job_id": new_id, "status": "pending", "retried_from": job_id}, http_status=202)


def _extract_document_text(filename: str, stream: io.BytesIO) -> str:
    if filename.endswith(".txt") or filename.endswith(".md"):
        return stream.read().decode("utf-8", errors="replace")
    if filename.endswith(".docx"):
        try:
            import docx
        except ImportError as e:  # noqa: F841
            raise ApiError("NOT_IMPLEMENTED", "未安装 python-docx，无法解析 Word", http_status=501)
        doc = docx.Document(stream)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        try:
            import openpyxl
        except ImportError as e:  # noqa: F841
            raise ApiError("NOT_IMPLEMENTED", "未安装 openpyxl，无法解析 Excel", http_status=501)
        wb = openpyxl.load_workbook(stream, data_only=True)
        lines: list[str] = []
        for sheet in wb.worksheets:
            for row in sheet.iter_rows(values_only=True):
                line = " ".join(str(c) for c in row if c is not None)
                if line.strip():
                    lines.append(line)
        return "\n".join(lines)
    if filename.endswith(".pdf"):
        try:
            import PyPDF2
        except ImportError as e:  # noqa: F841
            raise ApiError("NOT_IMPLEMENTED", "未安装 PyPDF2，无法解析 PDF", http_status=501)
        reader = PyPDF2.PdfReader(stream)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ApiError("VALIDATION_ERROR", f"不支持的文件类型: {filename}", http_status=415)


def _parse_spreadsheet(filename: str, stream: io.BytesIO, name: str | None = None) -> dict[str, Any]:
    """解析 CSV/Excel 为 {name, objects:[...]}。

    GAP-04 格式分派（优先级）：
    1) B0 三 sheet 模板（Excel sheet 名含 1-类层次/2-对象属性(关系)/3-数据属性）；
    2) 长表（属性清单）：表头显式含 对象/对象名/object 列——一行一属性、可多对象；
    3) 宽数据表（普通表头，列=属性、行=实例）：单对象草稿，
       类型/必填/稳定主键/约束全部由样本行自动推断，生成后可编辑。
    """
    if filename.endswith(".csv"):
        import csv
        text = stream.read().decode("utf-8", errors="replace")
        rows = list(csv.reader(text.splitlines()))
        return _parse_table_rows(filename, rows, name)

    try:
        import openpyxl
    except ImportError as e:  # noqa: F841
        raise ApiError("NOT_IMPLEMENTED", "未安装 openpyxl", http_status=501)

    wb = openpyxl.load_workbook(stream, data_only=True)
    sheet_names = set(wb.sheetnames)
    b0_sheets = {"1-类层次", "2-对象属性(关系)", "3-数据属性"}
    if b0_sheets.issubset(sheet_names):
        return _parse_b0_template(wb, name)

    ws = wb.active
    rows = [[str(c) if c is not None else "" for c in row] for row in ws.iter_rows(values_only=True)]
    return _parse_table_rows(filename, rows, name)


# GAP-04 宽表推断：样本行数上限（防大表拖慢解析；推断只用样本，草稿可编辑）
_WIDE_SAMPLE_LIMIT = 500
# 稳定主键候选列名关键词（命中即优先作为业务主键）
_KEY_HINTS = ("id", "编号", "编码", "code", "identifier", "单号", "key")
_BOOL_WORDS = {"true", "false", "yes", "no", "是", "否"}
_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$")


def _parse_table_rows(filename: str, rows: list[list[str]], name: str | None = None) -> dict[str, Any]:
    """按表头分派长表（属性清单）与宽数据表。

    仅当表头显式含「对象」式列（对象/对象名/object/object_name）才走长表；
    普通表头走宽表——修复此前宽数据表无「对象」列时被按长表误解析
    （每行数据被当成一个对象）的问题。
    """
    if rows and any(h.strip().lower() in ("对象", "对象名", "object", "object_name") for h in rows[0]):
        return _parse_flat_rows(filename, rows, name)
    return _parse_wide_rows(filename, rows, name)


def _infer_column_dtype(values: list[str]) -> str:
    """由样本值推断属性 data_type（全部样本命中才取更严格类型，否则 string）。"""
    nonempty = [v.strip() for v in values if v.strip()]
    if not nonempty:
        return "string"
    if all(re.fullmatch(r"-?\d+", v) for v in nonempty):
        return "integer"
    if all(re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", v) for v in nonempty):
        return "float"
    if all(v.lower() in _BOOL_WORDS for v in nonempty):
        return "boolean"
    if all(_DATE_RE.match(v) for v in nonempty):
        return "date"
    return "string"


def _infer_column_constraints(values: list[str], dtype: str, is_key: bool) -> dict[str, Any]:
    """从样本值抽取属性 constraints：数值 min/max、字符串长度区间、低基数 → enum。

    稳定主键列不推断 enum（唯一键不应是枚举白名单）。
    """
    nonempty = [v.strip() for v in values if v.strip()]
    c: dict[str, Any] = {}
    if dtype in ("integer", "float") and nonempty:
        nums = [float(v) for v in nonempty]
        if dtype == "integer":
            c["minimum"] = int(min(nums))
            c["maximum"] = int(max(nums))
        else:
            c["minimum"] = min(nums)
            c["maximum"] = max(nums)
    elif dtype == "string" and nonempty:
        c["min_length"] = min(len(v) for v in nonempty)
        c["max_length"] = max(len(v) for v in nonempty)
        if not is_key and len(nonempty) >= 4:
            distinct = list(dict.fromkeys(nonempty))
            if len(distinct) <= 8 and len(distinct) < len(nonempty):
                c["enum"] = distinct[:8]
    return c


def _parse_wide_rows(filename: str, rows: list[list[str]], name: str | None = None) -> dict[str, Any]:
    """GAP-04 宽数据表：表头=属性清单、数据行=实例 → 单对象草稿本体。

    - 对象名：优先 `name` 参数，否则取文件名主干；
    - 类型/必填/稳定主键/约束全部由样本行推断（最多 _WIDE_SAMPLE_LIMIT 行）；
    - 仅表头无数据行时退化为骨架草稿（各列 string、非必填、无约束）；
    - 重复列名自动加后缀保证属性 id 唯一；草稿可编辑，推断有误可在编辑器修正。
    """
    if not rows:
        raise ValueError("空表格")
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    obj_name = (name or "").strip() or stem or "导入对象"
    header = [h.strip() for h in rows[0]]
    data_rows = [r for r in rows[1:] if any(str(x).strip() for x in r)]
    sample = data_rows[:_WIDE_SAMPLE_LIMIT]

    cols: list[tuple[int, str, list[str]]] = []
    for i, h in enumerate(header):
        if not h:
            continue
        vals = [str(r[i]).strip() if i < len(r) else "" for r in sample]
        if data_rows and not any(vals):
            continue  # 数据行存在但该列全空：不成属性
        cols.append((i, h, vals))
    if not cols:
        raise ValueError("表格没有有效列（表头为空）")

    used: dict[str, int] = {}
    attributes: list[dict[str, Any]] = []
    for _, h, vals in cols:
        base = "attr_" + _slug(h)
        if base in used:
            used[base] += 1
            aid = f"{base}_{used[base]}"
        else:
            used[base] = 0
            aid = base
        attributes.append({
            "id": aid,
            "name": h,
            "data_type": _infer_column_dtype(vals),
            "required": bool(sample) and all(v for v in vals),
            "constraints": {},
            "_vals": vals,
        })

    # 稳定主键：1) 列名命中 id 式关键词 2) 首个 string 列 3) 首列
    def _hint_rank(a: dict[str, Any]) -> int:
        low = a["name"].lower()
        return next((idx for idx, kw in enumerate(_KEY_HINTS) if kw in low), 99)

    hint_attrs = [a for a in attributes if _hint_rank(a) < 99]
    if hint_attrs:
        key_attr = min(hint_attrs, key=_hint_rank)
    else:
        str_attrs = [a for a in attributes if a["data_type"] == "string"]
        key_attr = str_attrs[0] if str_attrs else attributes[0]
    key_id = key_attr["id"]
    for a in attributes:
        vals = a.pop("_vals")
        a["constraints"] = _infer_column_constraints(vals, a["data_type"], a["id"] == key_id)

    sample_n = min(len(data_rows), _WIDE_SAMPLE_LIMIT)
    return {
        "name": obj_name,
        "description": (
            f"来自 {filename} 的宽表导入（样本 {sample_n} 行 × {len(attributes)} 列）；"
            "类型/必填/主键/约束为自动推断，请在编辑器中确认"
        ),
        "objects": [{
            "id": "obj_" + _slug(obj_name),
            "name": obj_name,
            "stable_key": key_id,
            "attributes": attributes,
            "relations": [],
        }],
    }


def _enum_cell(s: str):
    """枚举单元格：数值形态转 int/float（与 data_type 数值型对齐），否则保持字符串。"""
    s = s.strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    try:
        f = float(s)
    except ValueError:
        return s
    return f if f == f and f not in (float("inf"), float("-inf")) else s


def _constraint_from_values(vals: dict[str, str | None]) -> dict[str, Any]:
    """由约束列的原始单元格文本组装属性 constraints（各列均可选，全部为空返回 {}）。

    vals 键：min_length / max_length / pattern / minimum / maximum / enum（逗号分隔）。
    数字列解析失败（非数字）时忽略该列而非报错，保持导入宽容度。
    """
    def _to_num(v: str | None) -> float | None:
        v = (v or "").strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    c: dict[str, Any] = {}
    mn = _to_num(vals.get("min_length"))
    if mn is not None:
        c["min_length"] = int(mn)
    mx = _to_num(vals.get("max_length"))
    if mx is not None:
        c["max_length"] = int(mx)
    pat = (vals.get("pattern") or "").strip()
    if pat:
        c["pattern"] = pat
    vmin = _to_num(vals.get("minimum"))
    if vmin is not None:
        c["minimum"] = float(vmin)
    vmax = _to_num(vals.get("maximum"))
    if vmax is not None:
        c["maximum"] = float(vmax)
    en_raw = (vals.get("enum") or "").strip()
    if en_raw:
        c["enum"] = [_enum_cell(x) for x in re.split(r"[,，]", en_raw) if x.strip()]
    return c


def _parse_flat_rows(filename: str, rows: list[list[str]], name: str | None = None) -> dict[str, Any]:
    if not rows:
        raise ValueError("空表格")
    header = [h.strip().lower() for h in rows[0]]
    def col_idx(*names):
        for name in names:
            if name in header:
                return header.index(name)
        return None
    obj_col = col_idx("对象", "object", "object_name", "对象名") or 0
    attr_col = col_idx("属性", "attribute", "attr", "属性名") or 1
    type_col = col_idx("类型", "type", "data_type", "数据类型") or 2
    required_col = col_idx("必填", "required", "是否必填") or 3
    target_col = col_idx("关系目标", "target", "target_object", "目标对象") or 4
    card_col = col_idx("基数", "cardinality") or 5
    # 可选约束列（缺省列时属性 constraints 为空）
    c_minlen_col = col_idx("最小长度", "min_length")
    c_maxlen_col = col_idx("最大长度", "max_length")
    c_pattern_col = col_idx("正则", "pattern")
    c_min_col = col_idx("最小值", "minimum")
    c_max_col = col_idx("最大值", "maximum")
    c_enum_col = col_idx("枚举", "enum")

    objects: dict[str, dict[str, Any]] = {}
    for row in rows[1:]:
        if not any(row):
            continue
        obj_name = row[obj_col].strip()
        if not obj_name:
            continue
        if obj_name not in objects:
            obj_id = "obj_" + _slug(obj_name)
            objects[obj_name] = {"id": obj_id, "name": obj_name, "attributes": [], "relations": []}
        obj = objects[obj_name]
        attr_name = row[attr_col].strip() if attr_col is not None and attr_col < len(row) else ""
        if attr_name:
            attr_id = "attr_" + _slug(attr_name)
            dtype = row[type_col].strip().lower() if type_col is not None and type_col < len(row) else "string"
            if dtype not in ("string", "integer", "float", "boolean", "date"):
                dtype = "string"
            req_text = row[required_col].strip().lower() if required_col is not None and required_col < len(row) else ""
            required = req_text in ("是", "true", "yes", "1")

            def _at(col: int | None) -> str:
                return row[col].strip() if col is not None and col < len(row) else ""

            obj["attributes"].append({
                "id": attr_id,
                "name": attr_name,
                "data_type": dtype,
                "required": required,
                "constraints": _constraint_from_values({
                    "min_length": _at(c_minlen_col),
                    "max_length": _at(c_maxlen_col),
                    "pattern": _at(c_pattern_col),
                    "minimum": _at(c_min_col),
                    "maximum": _at(c_max_col),
                    "enum": _at(c_enum_col),
                }),
            })
        target_name = row[target_col].strip() if target_col is not None and target_col < len(row) else ""
        if target_name and target_name in objects:
            rel_id = "rel_" + _slug(obj_name) + "_" + _slug(target_name)
            cardinality = row[card_col].strip() if card_col is not None and card_col < len(row) else "many_to_one"
            if cardinality not in ("one_to_one", "one_to_many", "many_to_one", "many_to_many"):
                cardinality = "many_to_one"
            obj["relations"].append({"id": rel_id, "name": f"{obj_name}-{target_name}", "target_object": objects[target_name]["id"], "cardinality": cardinality})

    return {
        "name": (name or "").strip() or "表格导入本体",
        "description": f"来自 {filename}",
        "objects": list(objects.values()),
    }


def _parse_b0_template(wb: Any, name: str | None = None) -> dict[str, Any]:
    """解析 B0 参考本体的三 sheet 模板（类层次、对象属性、数据属性）。"""
    def sheet_rows(name: str) -> list[list[str]]:
        ws = wb[name]
        return [[str(c) if c is not None else "" for c in row] for row in ws.iter_rows(values_only=True)]

    def cell(row: list[str], header: list[str], col: str) -> str | None:
        if col not in header:
            return None
        v = row[header.index(col)].strip()
        return v or None

    cls_rows = sheet_rows("1-类层次")
    rel_rows = sheet_rows("2-对象属性(关系)")
    attr_rows = sheet_rows("3-数据属性")

    classes: dict[str, dict[str, Any]] = {}
    if cls_rows:
        cls_header = [h.strip() for h in cls_rows[0]]
        for r in cls_rows[1:]:
            cls_id = cell(r, cls_header, "类名")
            if not cls_id:
                continue
            classes[cls_id] = {
                "id": cls_id,
                "name": cell(r, cls_header, "中文名") or cls_id,
                "description": cell(r, cls_header, "备注/取值域") or "",
                "parent": cell(r, cls_header, "父类"),
                "attributes": [],
                "relations": [],
            }

    rels_by_domain: dict[str, list[dict[str, Any]]] = {}
    if rel_rows:
        rel_header = [h.strip() for h in rel_rows[0]]
        for r in rel_rows[1:]:
            rel_id = cell(r, rel_header, "关系名")
            domain = cell(r, rel_header, "定义域")
            range_ = cell(r, rel_header, "值域")
            if not rel_id or not domain:
                continue
            rels_by_domain.setdefault(domain, []).append({
                "id": rel_id,
                "name": cell(r, rel_header, "中文名") or rel_id,
                "target_object": range_,
                "cardinality": "many_to_one",
            })

    attr_type_map = {
        "string": "string", "str": "string", "integer": "integer", "int": "integer",
        "float": "float", "decimal": "float", "boolean": "boolean", "bool": "boolean",
        "date": "date", "datetime": "date",
    }
    attrs_by_class: dict[str, list[dict[str, Any]]] = {}
    if attr_rows:
        attr_header = [h.strip() for h in attr_rows[0]]
        for r in attr_rows[1:]:
            attr_id = cell(r, attr_header, "属性名")
            cls_id = cell(r, attr_header, "所属类")
            dtype_raw = (cell(r, attr_header, "数据类型") or "string").strip().lower()
            if not attr_id or not cls_id:
                continue
            attrs_by_class.setdefault(cls_id, []).append({
                "id": attr_id,
                "name": cell(r, attr_header, "中文名") or attr_id,
                "data_type": attr_type_map.get(dtype_raw, "string"),
                "required": False,
                # 可选约束列：最小长度/最大长度/正则/最小值/最大值/枚举（逗号分隔）
                "constraints": _constraint_from_values({
                    "min_length": cell(r, attr_header, "最小长度"),
                    "max_length": cell(r, attr_header, "最大长度"),
                    "pattern": cell(r, attr_header, "正则"),
                    "minimum": cell(r, attr_header, "最小值"),
                    "maximum": cell(r, attr_header, "最大值"),
                    "enum": cell(r, attr_header, "枚举"),
                }),
            })

    objects = []
    for cls_id, obj in classes.items():
        obj["relations"] = rels_by_domain.get(cls_id, [])
        obj["attributes"] = attrs_by_class.get(cls_id, [])
        # 父类必须在本模板内显式定义，与 API 路径"父对象不存在报错"口径一致（不再静默抹掉）
        if obj["parent"] and obj["parent"] not in classes:
            raise ValueError(
                f"类层次：父类 '{obj['parent']}' 不存在（'1-类层次' sheet 缺少该类名），请修正模板后重新导入"
            )
        objects.append(obj)

    return {
        "name": (name or "").strip() or "B0参考本体导入",
        "description": "从 B0 三 sheet 模板导入",
        "objects": objects,
    }


def _slug(text: str) -> str:
    import re
    s = re.sub(r"[^\w\u4e00-\u9fff]", "_", text).strip("_")
    return s[:40] or "x"


def _serialize_ontology(ontology: dict[str, Any]) -> str:
    """把现有本体序列化为给 LLM 的紧凑描述，不含内部 id/时间戳。"""
    lines = [
        f"本体名称: {ontology.get('name', '')}",
        f"描述: {ontology.get('description', '')}",
        "现有业务对象:",
    ]
    for obj in ontology.get("objects", []):
        lines.append(f"- 对象 id={obj.get('id')} name={obj.get('name')} definition={obj.get('business_definition', '')}")
        for a in obj.get("attributes", []):
            lines.append(f"  - 属性 id={a.get('id')} name={a.get('name')} type={a.get('data_type')} required={a.get('required', False)}")
        for r in obj.get("relations", []):
            lines.append(f"  - 关系 id={r.get('id')} name={r.get('name')} target={r.get('target_object')} cardinality={r.get('cardinality')}")
    return "\n".join(lines)


def _generate_ontology_enrichment(
    ontology: dict[str, Any],
    doc_text: str,
    instructions: str,
    endpoint: str | None,
    model: str | None,
) -> dict[str, Any]:
    """调用 LLM 在现有本体基础上做增量抽取。"""
    system_msg = "你是企业业务本体建模专家。任务：基于已有本体，从新材料中增量抽取缺失的对象、属性、关系。"
    prompt = f"""{_serialize_ontology(ontology)}

补充材料：
{doc_text[:12000]}

{"额外指示：" + instructions if instructions else ""}

请严格返回如下 JSON，禁止返回任何解释或额外字段：
{{
  "name": "增强后本体名称（可保持原名）",
  "description": "增强后本体描述",
  "objects": [
    {{
      "id": "obj_xxx",
      "name": "对象中文名",
      "business_definition": "业务语义定义",
      "attributes": [
        {{"id": "attr_xxx", "name": "属性中文名", "data_type": "string", "required": false, "constraints": {{}}}}
      ],
      "relations": [
        {{"id": "rel_xxx", "name": "关系中文名", "target_object": "目标对象id", "cardinality": "many_to_one"}}
      ]
    }}
  ],
  "constraints": []
}}

约束：
1. 顶层字段只能是 name、description、objects、constraints。
2. 必须保留并复用已有对象 id；只有新材料中确实出现的新业务实体才新增对象。
3. 新增属性应放在最相关的已有对象或新增对象上，避免重复。
4. data_type 只能是 string/integer/float/boolean/date/datetime；cardinality 只能是 one_to_one/one_to_many/many_to_one/many_to_many。
5. target_object 必须指向 objects 中真实存在的 id。
6. id 使用小写下划线，全局唯一，建议以 obj_/attr_/rel_ 前缀区分。
"""
    # 不硬编码 max_tokens：思考型模型的 reasoning 计入 completion_tokens，小值会被吃光导致 content 为空
    result = call_llm(prompt, base_url=endpoint, model=model, temperature=0.1, system_message=system_msg)
    message = result.get("choices", [{}])[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise ApiError("LLM_ERROR", "LLM 返回内容为空（reasoning 耗尽 max_tokens 或未输出正文），请重试或调大 LLM 设置中的 max_tokens", http_status=422)
    return extract_json(str(content))


def _merge_ontology(
    existing: dict[str, Any],
    delta: dict[str, Any],
    namespace: str,
) -> tuple[dict[str, Any], OntologyEnrichSummary]:
    """合并现有本体与增强增量，按 id 去重；返回合并后本体和变更摘要。"""
    merged = dict(existing)
    merged["name"] = delta.get("name") or existing.get("name")
    merged["description"] = delta.get("description") or existing.get("description")
    merged["updated_at"] = _now()

    old_objs = {o["id"]: o for o in existing.get("objects", [])}
    new_objs: list[dict[str, Any]] = []
    added_objects: list[str] = []
    updated_objects: list[str] = []
    added_attributes: list[str] = []
    added_relations: list[str] = []

    for obj in delta.get("objects", []):
        oid = obj.get("id")
        if not oid:
            continue
        obj["namespace"] = obj.get("namespace") or namespace
        if oid not in old_objs:
            new_objs.append(obj)
            added_objects.append(oid)
            continue

        old_obj = old_objs[oid]
        # 属性按 id 合并
        old_attrs = {a["id"]: a for a in old_obj.get("attributes", [])}
        merged_attrs = list(old_obj.get("attributes", []))
        for a in obj.get("attributes", []):
            if a["id"] not in old_attrs:
                merged_attrs.append(a)
                added_attributes.append(f"{oid}.{a['id']}")

        # 关系按 id 合并
        old_rels = {r["id"]: r for r in old_obj.get("relations", [])}
        merged_rels = list(old_obj.get("relations", []))
        for r in obj.get("relations", []):
            if r["id"] not in old_rels:
                merged_rels.append(r)
                added_relations.append(f"{oid}.{r['id']}")

        updated_obj = dict(old_obj)
        updated_obj.update({k: v for k, v in obj.items() if k not in ("attributes", "relations")})
        updated_obj["attributes"] = merged_attrs
        updated_obj["relations"] = merged_rels
        if updated_obj != old_obj:
            updated_objects.append(oid)
        new_objs.append(updated_obj)

    # 保留增量中未触及的已有对象
    for oid, old_obj in old_objs.items():
        if not any(o["id"] == oid for o in new_objs):
            new_objs.append(old_obj)

    merged["objects"] = new_objs
    merged["constraints"] = existing.get("constraints", []) + delta.get("constraints", [])
    return merged, OntologyEnrichSummary(
        added_objects=added_objects,
        added_attributes=added_attributes,
        added_relations=added_relations,
        updated_objects=updated_objects,
    )
