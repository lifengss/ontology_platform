"""本体建模路由（UC1 / VS1 + VS5/逆向）。完整闭环：创建→翻译落 Jena(降级文件)→读取→导出→校验→快照。"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from rdflib import Graph

from ai_helper import call_llm, generate_ontology_from_document, generate_ontology_from_text
from core.responses import (
    ApiError,
    fail,
    list_payload,
    parse_pagination,
    success,
)
from core.storage import JsonStore
from jena_adapter.client import shacl_validate, upload_ttl
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
    OntologyFromDocResponse,
    OntologyFromTextRequest,
    ValidateRequest,
    ValidateResult,
)

JOBS: dict[str, dict[str, Any]] = {}

router = APIRouter(prefix="/api/ontologies", tags=["ontologies"])
logger = get_logger("api.ontologies")
_store = JsonStore("ontologies")
_snapshots = JsonStore("ontology_snapshots")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("", status_code=201)
def create_ontology(payload: OntologyCreate):
    record = payload.model_dump()
    record["id"] = uuid.uuid4().hex
    record["created_at"] = _now()
    record["updated_at"] = record["created_at"]
    # 翻译前轻量校验门（P-01/P-03）：模型级完整性，省一次 Jena/SHACL 调用
    biz_errors = validate_business_layer(record)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "业务层校验未通过", http_status=422,
                       details={"violations": biz_errors})
    _store.insert(record)
    # 翻译并落 Jena（不可达时落本地 TTL 文件）
    ttl = translate_to_turtle(record)
    upload_ttl(record["id"], ttl)
    logger.info("create_ontology id=%s name=%s", record["id"], record["name"])
    return success(record, http_status=201)


@router.get("")
def list_ontologies(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = None,
):
    page, page_size = parse_pagination(page, page_size)
    items = _store.all()
    if keyword:
        items = [o for o in items if keyword.lower() in (o.get("name", "") or "").lower()]
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/{ontology_id}")
def get_ontology(ontology_id: str):
    rec = _store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return success(rec)


@router.put("/{ontology_id}")
def update_ontology(ontology_id: str, payload: Ontology):
    existing = _store.get(ontology_id)
    if not existing:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    patch.pop("id", None)
    patch["updated_at"] = _now()
    updated = _store.update(ontology_id, patch)
    ttl = translate_to_turtle(updated)
    upload_ttl(ontology_id, ttl)
    logger.info("update_ontology id=%s", ontology_id)
    return success(updated)


@router.delete("/{ontology_id}")
def delete_ontology(ontology_id: str):
    if not _store.delete(ontology_id):
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    logger.info("delete_ontology id=%s", ontology_id)
    return success({"deleted": True})


@router.post("/{ontology_id}/validate")
def validate_ontology(ontology_id: str, req: ValidateRequest | None = None):
    rec = _store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    # 结构校验（业务层轻量门，P-01/P-03） + 真实 SHACL 校验（pyshacl，离线可用）
    structural = validate_business_layer(rec)
    shapes = translate_to_shacl(rec)
    data = build_instance_graph(rec, (req.sample_instances or [])) if req else Graph()
    shacl = shacl_validate(data, shapes)
    if shacl is None:
        # pyshacl 不可用 → 仅结构校验
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
def export_ontology(ontology_id: str, _req: ExportRequest | None = None):
    rec = _store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    ttl = translate_to_turtle(rec)
    filename = f"{rec.get('name', ontology_id)}.ttl".replace(" ", "_")
    return success({"format": "turtle", "content": ttl, "filename": filename})


@router.post("/{ontology_id}/snapshot")
def snapshot_ontology(ontology_id: str, req: dict | None = None):
    rec = _store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    snap = {
        "snapshot_id": uuid.uuid4().hex,
        "ontology_id": ontology_id,
        "note": (req or {}).get("note"),
        "created_at": _now(),
        "model": rec,
    }
    _snapshots.insert(snap)
    logger.info("snapshot ontology_id=%s", ontology_id)
    return success(snap, http_status=201)


@router.get("/{ontology_id}/snapshots")
def list_snapshots(
    ontology_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    page, page_size = parse_pagination(page, page_size)
    items = _snapshots.find(lambda r: r.get("ontology_id") == ontology_id)
    items.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


# ---- 用户友好的逆向/辅助生成入口（VS5+） ----


def _save_draft(record: dict[str, Any], source: str) -> dict[str, Any]:
    record["id"] = uuid.uuid4().hex
    record["modeling_type"] = "reverse"
    record["status"] = "draft"
    record["draft_source"] = source
    record["created_at"] = _now()
    record["updated_at"] = record["created_at"]
    _store.insert(record)
    return record


def _normalize_generated(generated: dict[str, Any]) -> dict[str, Any]:
    """清洗 LLM/Excel 输出，使其符合业务层校验。"""
    # 确保每个对象有 id/name/attributes/relations
    objects: list[dict[str, Any]] = generated.get("objects") or []
    for obj in objects:
        obj.setdefault("attributes", [])
        obj.setdefault("relations", [])
        for attr in obj["attributes"]:
            attr.setdefault("required", False)
            attr.setdefault("constraints", {})
        for rel in obj.get("relations", []):
            rel.setdefault("cardinality", "many_to_one")
    return {
        "name": generated.get("name", "未命名本体"),
        "description": generated.get("description", ""),
        "objects": objects,
        "constraints": generated.get("constraints", []),
        "version": "1",
    }


@router.post("/from-text", status_code=201)
def create_from_text(payload: OntologyFromTextRequest):
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
    draft = _save_draft(record, "text")
    logger.info("from-text draft_id=%s", draft["id"])
    return success(draft, http_status=201)


@router.post("/from-excel", status_code=201)
def create_from_excel(file: UploadFile = File(...)):
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
    draft = _save_draft(record, "excel")
    logger.info("from-excel draft_id=%s", draft["id"])
    return success(draft, http_status=201)


@router.post("/from-doc", status_code=202)
def create_from_doc(
    file: UploadFile = File(...),
    llm_endpoint: str | None = Form(default=None),
    model: str | None = Form(default=None),
):
    """上传 PRD/Word/Excel 文档 → 后台解析为文本 → LLM 抽取本体草稿（异步任务，返回 job_id）。"""
    filename = (file.filename or "").lower()
    content = file.file.read()
    if not content:
        raise ApiError("VALIDATION_ERROR", "文件为空", http_status=422)
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "pending", "draft_id": None, "error": None}

    def _run() -> None:
        try:
            text = _extract_document_text(filename, io.BytesIO(content))
            generated = generate_ontology_from_document(text, llm_endpoint, model)
            record = _normalize_generated(generated)
            biz_errors = validate_business_layer(record)
            if biz_errors:
                JOBS[job_id] = {"status": "error", "draft_id": None, "error": f"业务层校验未通过: {biz_errors}"}
                return
            draft = _save_draft(record, "document")
            JOBS[job_id] = {"status": "done", "draft_id": draft["id"], "error": None}
        except Exception as e:  # noqa: BLE001
            logger.warning("from-doc job=%s 失败: %s", job_id, e)
            JOBS[job_id] = {"status": "error", "draft_id": None, "error": str(e)}

    import threading
    threading.Thread(target=_run, daemon=True).start()
    logger.info("from-doc job=%s filename=%s", job_id, filename)
    return success({"job_id": job_id, "status": "pending"}, http_status=202)


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise ApiError("NOT_FOUND", "任务不存在", http_status=404)
    return success(job)


@router.post("/{ontology_id}/publish")
def publish_ontology(ontology_id: str):
    """将 draft 状态的本体发布为 active（真正落库并翻译到 Jena）。"""
    rec = _store.get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    if rec.get("status") != "draft":
        raise ApiError("VALIDATION_ERROR", "只能发布 draft 状态的本体", http_status=422)
    rec["status"] = "active"
    rec["updated_at"] = _now()
    _store.update(ontology_id, rec)
    ttl = translate_to_turtle(rec)
    upload_ttl(ontology_id, ttl)
    logger.info("publish_ontology id=%s", ontology_id)
    return success(rec)


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
    约定：sheet 或 CSV 中第一列为 对象名，其后列依次为 属性名/类型/必填/关系目标。
    简单 heuristics：表头行识别 object/attribute/type/required/target 等关键字。
    """
    import csv
    rows: list[list[str]] = []
    if filename.endswith(".csv"):
        text = stream.read().decode("utf-8", errors="replace")
        rows = list(csv.reader(text.splitlines()))
    else:
        try:
            import openpyxl
        except ImportError as e:  # noqa: F841
            raise ApiError("NOT_IMPLEMENTED", "未安装 openpyxl", http_status=501)
        wb = openpyxl.load_workbook(stream, data_only=True)
        ws = wb.active
        rows = [[str(c) if c is not None else "" for c in row] for row in ws.iter_rows(values_only=True)]
    if not rows:
        raise ValueError("空表格")
    header = [h.strip().lower() for h in rows[0]]
    # 识别列索引
    def idx(*names):
        for name in names:
            if name in header:
                return header.index(name)
        return None
    obj_col = idx("对象", "object", "object_name", "对象名") or 0
    attr_col = idx("属性", "attribute", "attr", "属性名") or 1
    type_col = idx("类型", "type", "data_type", "数据类型") or 2
    required_col = idx("必填", "required", "是否必填") or 3
    target_col = idx("关系目标", "target", "target_object", "目标对象") or 4
    card_col = idx("基数", "cardinality") or 5

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


def _slug(text: str) -> str:
    import re
    s = re.sub(r"[^\w\u4e00-\u9fff]", "_", text).strip("_")
    return s[:40] or "x"
