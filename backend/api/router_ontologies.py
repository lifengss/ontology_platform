"""本体建模路由（UC1 / VS1 + VS5/逆向）。完整闭环：创建→翻译落 Jena(降级文件)→读取→导出→校验→快照。"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from rdflib import Graph

from ai_helper import call_llm, extract_json, generate_ontology_from_document, generate_ontology_from_text
from api.dependencies import namespace_param, project_id_param
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
    build_instance_graph,
    translate_to_shacl,
    translate_to_turtle,
    validate_business_layer,
)
from schemas.ontology import (
    ExportRequest,
    Ontology,
    OntologyCreate,
    OntologyEnrichRequest,
    OntologyEnrichSummary,
    OntologyFromDocResponse,
    OntologyFromTextRequest,
    OntologyObjectPatch,
    ValidateRequest,
    ValidateResult,
)

JOBS: dict[str, dict[str, Any]] = {}

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


def _sync_rdf(project_id: str, ontology_id: str) -> None:
    """重建本体全量图（schema+实例）写入 RDF 存储；失败仅告警，不阻断主流程。"""
    try:
        rdf_sync.sync_ontology(project_id, ontology_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("RDF 同步失败（不影响主流程） ontology_id=%s: %s", ontology_id, e)


@router.post("", status_code=201)
def create_ontology(
    payload: OntologyCreate,
    project_id: str = Depends(project_id_param),
    namespace: str | None = Depends(namespace_param),
):
    record = payload.model_dump()
    record["id"] = uuid.uuid4().hex
    record["status"] = "draft"
    record["project_id"] = project_id
    record["namespace"] = namespace or project_id
    record["created_at"] = _now()
    record["updated_at"] = record["created_at"]
    # 对象级 namespace 默认继承本体 namespace；并自动补齐业务语义定义与稳定主键
    for obj in record.get("objects", []):
        if not obj.get("namespace"):
            obj["namespace"] = record["namespace"]
        biz_def = (obj.get("business_definition") or obj.get("description") or f"{obj.get('name', '对象')}的业务语义").strip()
        if len(biz_def) < 5:
            biz_def = f"{obj.get('name', '对象')}的业务语义定义"
        obj["business_definition"] = biz_def
        attrs = obj.get("attributes", [])
        if not obj.get("stable_key") and attrs:
            candidates = [a for a in attrs if a.get("required") and a.get("data_type", "string") == "string"]
            if not candidates:
                candidates = [a for a in attrs if a.get("data_type", "string") == "string"]
            if not candidates:
                candidates = attrs
            if candidates:
                obj["stable_key"] = candidates[0]["id"]
    biz_errors = validate_business_layer(record)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "业务层校验未通过", http_status=422,
                       details={"violations": biz_errors})
    store = _store_for(project_id)
    store.insert(record)
    _sync_rdf(project_id, record["id"])
    logger.info("create_ontology id=%s project_id=%s name=%s", record["id"], project_id, record["name"])
    return success(record, http_status=201)


@router.get("")
def list_ontologies(
    project_id: str = Depends(project_id_param),
    namespace: str | None = Depends(namespace_param),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = None,
):
    page, page_size = parse_pagination(page, page_size)
    store = _store_for(project_id)
    items = store.all()
    # 按 namespace 过滤；未显式指定则继承 project_id
    ns = namespace or project_id
    items = [o for o in items if (o.get("namespace") or project_id) == ns]
    if keyword:
        items = [o for o in items if keyword.lower() in (o.get("name", "") or "").lower()]
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/{ontology_id}")
def get_ontology(ontology_id: str, project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return success(rec)


@router.put("/{ontology_id}")
def update_ontology(
    ontology_id: str,
    payload: Ontology,
    project_id: str = Depends(project_id_param),
    namespace: str | None = Depends(namespace_param),
):
    store = _store_for(project_id)
    existing = store.get(ontology_id)
    if not existing:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    patch.pop("id", None)
    patch["updated_at"] = _now()
    if namespace:
        patch["namespace"] = namespace
    for obj in patch.get("objects", []):
        if not obj.get("namespace"):
            obj["namespace"] = patch.get("namespace", existing.get("namespace", project_id))
    updated = store.update(ontology_id, patch)
    _sync_rdf(project_id, ontology_id)
    logger.info("update_ontology id=%s project_id=%s", ontology_id, project_id)
    return success(updated)


@router.patch("/{ontology_id}/objects/{object_id}")
def patch_ontology_object(
    ontology_id: str,
    object_id: str,
    payload: OntologyObjectPatch,
    project_id: str = Depends(project_id_param),
    namespace: str | None = Depends(namespace_param),
):
    """就地更新本体内单个对象（对象约束编辑）。

    仅当对象存在时更新；保留 id/namespace/stable_key/business_definition 等元数据，
    用请求体覆盖 name/description/attributes/relations。更新后重翻译 TTL 落盘。
    """
    store = _store_for(project_id)
    existing = store.get(ontology_id)
    if not existing:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    objects = existing.get("objects", [])
    idx = next((i for i, o in enumerate(objects) if o.get("id") == object_id), None)
    if idx is None:
        raise ApiError("NOT_FOUND", "对象不存在", http_status=404)

    patch = payload.model_dump(exclude_unset=True)
    patch["id"] = object_id
    # 保留既有元数据（namespace/stable_key/business_definition 等继承原值）
    for key in ("namespace", "stable_key", "business_definition", "parent"):
        if key not in patch:
            patch[key] = objects[idx].get(key)
    # 对象级 namespace 默认继承本体 namespace
    if not patch.get("namespace"):
        patch["namespace"] = namespace or existing.get("namespace", project_id)
    # 补全属性/关系缺失的默认业务语义
    for attr in patch.get("attributes") or []:
        attr.setdefault("constraints", {})
    # 刷新对象级稳定主键（若对象无 stable_key 且改为有 required 字符串属性）
    if not patch.get("stable_key"):
        attrs = patch.get("attributes") or []
        candidates = [a for a in attrs if a.get("required") and a.get("data_type", "string") == "string"]
        if not candidates and attrs:
            candidates = [a for a in attrs if a.get("data_type", "string") == "string"]
        if candidates:
            patch["stable_key"] = candidates[0]["id"]

    objects[idx] = patch
    existing["objects"] = objects
    existing["updated_at"] = _now()
    updated = store.update(ontology_id, {"objects": objects, "updated_at": existing["updated_at"]})
    if updated is None:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    _sync_rdf(project_id, ontology_id)
    logger.info("patch_object id=%s object_id=%s project_id=%s", ontology_id, object_id, project_id)
    return success(updated)


@router.delete("/{ontology_id}")
def delete_ontology(ontology_id: str, project_id: str = Depends(project_id_param)):
    if not _store_for(project_id).delete(ontology_id):
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    logger.info("delete_ontology id=%s project_id=%s", ontology_id, project_id)
    return success({"deleted": True})


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
    biz_errors = validate_business_layer(merged)
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
def publish_ontology(ontology_id: str, project_id: str = Depends(project_id_param)):
    store = _store_for(project_id)
    rec = store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    if rec.get("status") != "draft":
        raise ApiError("VALIDATION_ERROR", "只有 draft 状态的本体可以发布", http_status=422)
    updated = store.update(ontology_id, {"status": "active", "updated_at": _now()})
    logger.info("publish_ontology id=%s project_id=%s", ontology_id, project_id)
    return success(updated)


@router.post("/{ontology_id}/validate")
def validate_ontology(ontology_id: str, req: ValidateRequest | None = None, project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    structural = validate_business_layer(rec)
    shapes = translate_to_shacl(rec)
    data = build_instance_graph(rec, (req.sample_instances or [])) if req else Graph()
    shacl = shacl_validate(data, shapes)
    if shacl is None:
        violations = [{"severity": v["severity"], "path": v.get("path"),
                       "focus_node": None, "message": v["message"]} for v in structural]
    else:
        violations = shacl["violations"] + [
            {"severity": v["severity"], "path": v.get("path"),
             "focus_node": None, "message": v["message"]} for v in structural
        ]
    result = ValidateResult(valid=len(violations) == 0, violations=violations)
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


def _save_draft(record: dict[str, Any], source: str, project_id: str) -> dict[str, Any]:
    record["id"] = uuid.uuid4().hex
    record["modeling_type"] = "reverse"
    record["status"] = "draft"
    record["project_id"] = project_id
    record["draft_source"] = source
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
        generated = generate_ontology_from_text(payload.text, payload.llm_endpoint, payload.model)
        record = _normalize_generated(generated)
    except ApiError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("from-text LLM 解析失败: %s", e)
        raise ApiError("LLM_ERROR", f"LLM 输出无法解析为合法本体: {e}", http_status=422)
    biz_errors = validate_business_layer(record)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "生成的本体未通过业务层校验", http_status=422, details={"violations": biz_errors})
    draft = _save_draft(record, "text", project_id)
    logger.info("from-text draft_id=%s project_id=%s", draft["id"], project_id)
    return success(draft, http_status=201)


@router.post("/from-excel", status_code=201)
def create_from_excel(file: UploadFile = File(...), project_id: str = Depends(project_id_param)):
    """上传 CSV/Excel（对象、属性、关系表）→ 生成本体草稿。"""
    filename = (file.filename or "").lower()
    content = file.file.read()
    if not content:
        raise ApiError("VALIDATION_ERROR", "文件为空", http_status=422)
    try:
        generated = _parse_spreadsheet(filename, io.BytesIO(content))
        record = _normalize_generated(generated)
    except Exception as e:  # noqa: BLE001
        logger.warning("from-excel 解析失败: %s", e)
        raise ApiError("PARSE_ERROR", f"文件解析失败: {e}", http_status=422)
    biz_errors = validate_business_layer(record)
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
    project_id: str = Depends(project_id_param),
):
    """上传 PRD/Word/Excel 文档 → 后台解析为文本 → LLM 抽取本体草稿（异步任务，返回 job_id）。支持多文件。"""
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

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "pending", "draft_id": None, "error": None, "project_id": project_id}

    background_tasks.add_task(_run_doc_extraction, job_id, file_infos, llm_endpoint, model, project_id)
    logger.info("from-doc job=%s project_id=%s files=%s", job_id, project_id, [f[0] for f in file_infos])
    return success({"job_id": job_id, "status": "pending", "project_id": project_id}, http_status=202)


def _run_doc_extraction(
    job_id: str,
    file_infos: list[tuple[str, bytes]],
    llm_endpoint: str | None,
    model: str | None,
    project_id: str,
) -> None:
    """from-doc 后台任务：解析文档、调用 LLM、保存草稿并更新 JOBS。"""
    try:
        texts: list[str] = []
        for filename, content in file_infos:
            texts.append(f"--- 文档: {filename} ---\n" + _extract_document_text(filename, io.BytesIO(content)))
        combined_text = "\n\n".join(texts)
        generated = generate_ontology_from_document(combined_text, llm_endpoint, model)
        record = _normalize_generated(generated)
        biz_errors = validate_business_layer(record)
        if biz_errors:
            JOBS[job_id] = {"status": "error", "draft_id": None, "error": f"业务层校验未通过: {biz_errors}"}
            logger.warning("from-doc job=%s 业务层校验未通过: %s", job_id, biz_errors)
            return
        draft = _save_draft(record, "document", project_id)
        JOBS[job_id] = {"status": "done", "draft_id": draft["id"], "error": None}
        logger.info("from-doc job=%s done draft_id=%s", job_id, draft["id"])
    except Exception as e:  # noqa: BLE001
        logger.warning("from-doc job=%s 失败: %s", job_id, e)
        JOBS[job_id] = {"status": "error", "draft_id": None, "error": f"{type(e).__name__}: {e}"}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, project_id: str = Depends(project_id_param)):
    job = JOBS.get(job_id)
    if not job:
        raise ApiError("NOT_FOUND", "任务不存在", http_status=404)
    # 简单项目归属校验
    if job.get("project_id") and job.get("project_id") != project_id:
        raise ApiError("FORBIDDEN", "任务不属于当前项目", http_status=403)
    return success(job)


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


def _parse_spreadsheet(filename: str, stream: io.BytesIO) -> dict[str, Any]:
    """解析 CSV/Excel 为 {name, objects:[...]}。
    优先识别 B0 三 sheet 模板；否则按单 sheet 约定列名解析。
    """
    if filename.endswith(".csv"):
        import csv
        text = stream.read().decode("utf-8", errors="replace")
        rows = list(csv.reader(text.splitlines()))
        return _parse_flat_rows(filename, rows)

    try:
        import openpyxl
    except ImportError as e:  # noqa: F841
        raise ApiError("NOT_IMPLEMENTED", "未安装 openpyxl", http_status=501)

    wb = openpyxl.load_workbook(stream, data_only=True)
    sheet_names = set(wb.sheetnames)
    b0_sheets = {"1-类层次", "2-对象属性(关系)", "3-数据属性"}
    if b0_sheets.issubset(sheet_names):
        return _parse_b0_template(wb)

    ws = wb.active
    rows = [[str(c) if c is not None else "" for c in row] for row in ws.iter_rows(values_only=True)]
    return _parse_flat_rows(filename, rows)


def _parse_flat_rows(filename: str, rows: list[list[str]]) -> dict[str, Any]:
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
            obj["attributes"].append({"id": attr_id, "name": attr_name, "data_type": dtype, "required": required, "constraints": {}})
        target_name = row[target_col].strip() if target_col is not None and target_col < len(row) else ""
        if target_name and target_name in objects:
            rel_id = "rel_" + _slug(obj_name) + "_" + _slug(target_name)
            cardinality = row[card_col].strip() if card_col is not None and card_col < len(row) else "many_to_one"
            if cardinality not in ("one_to_one", "one_to_many", "many_to_one", "many_to_many"):
                cardinality = "many_to_one"
            obj["relations"].append({"id": rel_id, "name": f"{obj_name}-{target_name}", "target_object": objects[target_name]["id"], "cardinality": cardinality})

    return {"name": "表格导入本体", "description": f"来自 {filename}", "objects": list(objects.values())}


def _parse_b0_template(wb: Any) -> dict[str, Any]:
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
                "constraints": {},
            })

    objects = []
    for cls_id, obj in classes.items():
        obj["relations"] = rels_by_domain.get(cls_id, [])
        obj["attributes"] = attrs_by_class.get(cls_id, [])
        if obj["parent"] and obj["parent"] not in classes:
            obj["parent"] = None
        objects.append(obj)

    return {"name": "B0参考本体导入", "description": "从 B0 三 sheet 模板导入", "objects": objects}


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
4. data_type 只能是 string/integer/float/boolean/date；cardinality 只能是 one_to_one/one_to_many/many_to_one/many_to_many。
5. target_object 必须指向 objects 中真实存在的 id。
6. id 使用小写下划线，全局唯一，建议以 obj_/attr_/rel_ 前缀区分。
"""
    result = call_llm(prompt, base_url=endpoint, model=model, temperature=0.1, max_tokens=8192, system_message=system_msg)
    content = result["choices"][0]["message"]["content"]
    return extract_json(content)


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
