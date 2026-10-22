"""实例归化流水线路由（UC2 / VS3）。

铁律：LLM 输出禁止直落库。预览生成候选（LLM/字段映射），
审核通过 → SHACL 校验 → 落 Neo4j（不可用时降级本地 JSONL）。
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from api.dependencies import project_id_param
from core.instance_store import get_instance_store
from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from ai_helper.client import call_llm
from ingestion import approve as approve_candidate_logic
from ingestion import preview as preview_candidate
from jena_adapter import rdf_sync
from logging_setup import get_logger
from modeling_translator import validate_business_layer
from schemas.ingest import ApproveResult, IngestPreviewRequest

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
logger = get_logger("api.ingest")


def _store_for(project_id: str) -> JsonStore:
    return JsonStore("ingest_candidates", project_id=project_id)


def _sync_rdf(project_id: str, ontology_id: str | None) -> None:
    """重建本体全量图（schema+实例）写入 RDF 存储；失败仅告警，不阻断主流程。"""
    if not ontology_id:
        return
    try:
        rdf_sync.sync_ontology(project_id, ontology_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("RDF 同步失败（不影响主流程） ontology_id=%s: %s", ontology_id, e)


def _instance_store_for(project_id: str):
    return get_instance_store(project_id=project_id)


def _ontologies_for(project_id: str) -> JsonStore:
    return JsonStore("ontologies", project_id=project_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_ontology(ontology_id: str, project_id: str) -> dict:
    rec = _ontologies_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return rec


def _match_object_type(token: str, ontology: dict[str, Any]) -> str | None:
    """按 sheet/文件名 token 启发式匹配对象 id（支持 obj_ 前缀与中文名）。"""
    token_lower = token.lower().replace(" ", "").replace("_", "").replace("-", "")
    objects = ontology.get("objects", [])
    best: tuple[str, int] = ("", -1)
    for obj in objects:
        oid = obj.get("id", "").lower()
        candidates = [
            oid,
            oid.replace("obj_", ""),
            oid.replace("obj", ""),
            obj.get("name", "").lower().replace(" ", ""),
        ]
        for c in candidates:
            if not c:
                continue
            # 精确包含
            if token_lower in c or c in token_lower:
                score = len(c)
                if score > best[1]:
                    best = (obj["id"], score)
    return best[0] if best[0] else None


def _parse_excel_sheet(
    sheet,
    object_type: str,
    source_ref: str,
    valid_attrs: set[str],
    explicit_field_map: dict[str, str] | None = None,
    obj: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """解析 openpyxl sheet，按表头做字段映射生成实例；显式映射优先，其余自动匹配。"""
    headers = [str(cell.value or "").strip() for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    field_map = _build_field_map(headers, valid_attrs, obj)
    if explicit_field_map:
        # 显式映射覆盖自动匹配；过滤掉不属于当前对象的 target
        filtered_explicit = {k: v for k, v in explicit_field_map.items() if v in valid_attrs}
        field_map.update(filtered_explicit)
    records: list[dict[str, Any]] = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if all(v is None or str(v).strip() == "" for v in row):
            continue
        rec: dict[str, Any] = {}
        for h, v in zip(headers, row):
            if h and v is not None:
                rec[h] = v
        records.append(rec)
    return records, field_map


def _parse_csv(
    content: str,
    object_type: str,
    source_ref: str,
    valid_attrs: set[str],
    explicit_field_map: dict[str, str] | None = None,
    obj: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """解析 CSV 文本，按表头做字段映射生成实例；显式映射优先，其余自动匹配。"""
    import csv
    lines = content.splitlines()
    if not lines:
        return [], {}
    reader = csv.DictReader(lines)
    headers = reader.fieldnames or []
    field_map = _build_field_map(headers, valid_attrs, obj)
    if explicit_field_map:
        filtered_explicit = {k: v for k, v in explicit_field_map.items() if v in valid_attrs}
        field_map.update(filtered_explicit)
    records = [row for row in reader if any(v is not None and str(v).strip() != "" for v in row.values())]
    return records, field_map


def _normalize_token(text: str) -> str:
    return text.lower().replace(" ", "").replace("_", "").replace("-", "")


def _build_field_map(headers: list[str], valid_attrs: set[str], obj: dict[str, Any] | None = None) -> dict[str, str]:
    """表头与属性 id/name 模糊匹配（忽略大小写、下划线、obj_/attr_ 前缀）。"""
    field_map: dict[str, str] = {}
    attrs_by_id = {a["id"]: a for a in (obj.get("attributes", []) if obj else [])}
    # 构建属性 id/name 的归一化查找表
    norm_to_attr: dict[str, str] = {}
    for attr_id, attr in attrs_by_id.items():
        norm_to_attr[_normalize_token(attr_id)] = attr_id
        norm_to_attr[_normalize_token(attr_id).replace("attr", "")] = attr_id
        name = attr.get("name", "")
        if name:
            norm_to_attr[_normalize_token(name)] = attr_id

    for h in headers:
        if not h:
            continue
        if h in valid_attrs:
            field_map[h] = h
            continue
        # 去掉 attr_/obj_ 等前缀再试
        norm_h = _normalize_token(h)
        if norm_h in norm_to_attr:
            field_map[h] = norm_to_attr[norm_h]
            continue
        # 去掉常见前缀
        for prefix in ("attr_", "obj_"):
            if norm_h.startswith(prefix):
                stripped = norm_h[len(prefix):]
                if stripped in norm_to_attr:
                    field_map[h] = norm_to_attr[stripped]
                    break
    return field_map


def _coerce_record_types(records: list[dict[str, Any]], field_map: dict[str, str], obj: dict[str, Any]) -> list[dict[str, Any]]:
    """按对象属性类型将记录值做类型转换。"""
    attr_types = {a["id"]: a.get("data_type", "string") for a in obj.get("attributes", [])}
    out: list[dict[str, Any]] = []
    for rec in records:
        new_rec: dict[str, Any] = dict(rec)
        for src, target in field_map.items():
            if target not in attr_types or src not in new_rec:
                continue
            val = new_rec[src]
            if val is None or val == "":
                continue
            dt = attr_types[target]
            try:
                if dt == "integer":
                    new_rec[src] = int(val)
                elif dt == "float":
                    new_rec[src] = float(val)
                elif dt == "boolean":
                    s = str(val).lower()
                    new_rec[src] = s in ("true", "1", "yes", "是")
                elif dt == "date":
                    new_rec[src] = str(val)
                else:
                    new_rec[src] = str(val)
            except (ValueError, TypeError):
                pass  # 保留原值，后续 SHACL 会报错
        out.append(new_rec)
    return out


def _build_candidate_records(
    ontology_id: str,
    ontology: dict[str, Any],
    object_type: str,
    source_ref: str,
    records: list[dict[str, Any]],
    field_map: dict[str, str],
    project_id: str,
) -> dict[str, Any]:
    obj_by_id = {o["id"]: o for o in ontology.get("objects", [])}
    obj = obj_by_id[object_type]
    records = _coerce_record_types(records, field_map, obj)
    payload = IngestPreviewRequest(
        ontology_id=ontology_id,
        object_type=obj["id"],
        source={"kind": "field_map", "records": records},
        mapping={"field_map": field_map, "source_ref": source_ref},
    )
    candidate = preview_candidate(payload, ontology)
    return candidate


def _save_candidate(
    candidate: dict[str, Any],
    project_id: str,
    source_ref: str,
) -> dict[str, Any]:
    rec = {
        "id": uuid.uuid4().hex,
        "project_id": project_id,
        "ontology_id": candidate["ontology_id"],
        "source_type": candidate["source_type"],
        "source_ref": source_ref,
        "object_type": candidate["object_type"],
        "instances": candidate["instances"],
        "status": "pending",
        "created_at": _now(),
        "approve_result": None,
    }
    _store_for(project_id).insert(rec)
    return rec


@router.post("/upload")
async def upload_instances(
    ontology_id: str,
    object_type: str | None = None,
    field_map: str | None = Form(default=None),
    file: UploadFile = File(...),
    project_id: str = Depends(project_id_param),
):
    """上传 Excel/CSV 生成候选实例。xlsx 支持按 sheet 名/文件名推断对象类型；
    未指定 object_type 时遍历所有 sheet，每个合法 sheet 生成一个候选。csv 必须指定 object_type。
    field_map 为 JSON 字符串，{源表头: 属性id}，显式覆盖自动匹配。"""
    ontology = _load_ontology(ontology_id, project_id)
    biz_errors = validate_business_layer(ontology)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "本体模型未通过业务层校验，无法生成候选", http_status=422,
                       details={"violations": biz_errors})

    objects = ontology.get("objects", [])
    obj_by_id = {o["id"]: o for o in objects}
    filename = file.filename or ""
    ext = filename.split(".")[-1].lower() if "." in filename else ""

    explicit_field_map: dict[str, str] | None = None
    if field_map:
        try:
            explicit_field_map = json.loads(field_map)
            if not isinstance(explicit_field_map, dict):
                raise ApiError("VALIDATION_ERROR", "field_map 必须是 JSON 对象", http_status=422)
        except json.JSONDecodeError as e:
            raise ApiError("VALIDATION_ERROR", f"field_map JSON 解析失败: {e}", http_status=422) from e

    candidates: list[dict[str, Any]] = []
    if ext == "xlsx":
        try:
            from openpyxl import load_workbook
        except Exception as e:  # noqa: BLE001
            raise ApiError("SERVICE_UNAVAILABLE", f"openpyxl 未安装: {e}", http_status=503) from e
        content = await file.read()
        wb = load_workbook(io.BytesIO(content))
        if not wb.sheetnames:
            raise ApiError("VALIDATION_ERROR", "Excel 文件无 sheet", http_status=422)

        if object_type and object_type in obj_by_id:
            target_sheets = [(object_type, object_type)]
        else:
            target_sheets = []
            for sheet_name in wb.sheetnames:
                matched = _match_object_type(sheet_name, ontology) if not object_type else object_type
                if matched and matched in obj_by_id:
                    target_sheets.append((sheet_name, matched))
                elif sheet_name in obj_by_id:
                    target_sheets.append((sheet_name, sheet_name))

        for sheet_name, matched_object_type in target_sheets:
            obj = obj_by_id[matched_object_type]
            valid_attrs = {a["id"] for a in obj.get("attributes", [])} | {r["id"] for r in obj.get("relations", [])}
            # 全局 field_map 可能只针对部分对象；过滤出当前对象可用属性，无法映射的表头走自动匹配
            sheet_field_map = None
            if explicit_field_map:
                sheet_field_map = {k: v for k, v in explicit_field_map.items() if v in valid_attrs}
            records, parsed_field_map = _parse_excel_sheet(
                wb[sheet_name], matched_object_type, filename, valid_attrs, sheet_field_map, obj
            )
            if not records:
                continue
            candidate = _build_candidate_records(ontology_id, ontology, matched_object_type, filename, records, parsed_field_map, project_id)
            rec = _save_candidate(candidate, project_id, filename)
            candidates.append(rec)
    elif ext == "csv":
        if not object_type:
            # 尝试从文件名推断
            inferred = _match_object_type(filename.rsplit(".", 1)[0], ontology)
            if inferred:
                object_type = inferred
        if not object_type:
            raise ApiError("VALIDATION_ERROR", "CSV 上传必须指定 object_type 或文件名可推断对象类型", http_status=422)
        if object_type not in obj_by_id:
            raise ApiError("VALIDATION_ERROR", f"对象类型 {object_type} 不在本体中", http_status=422)
        content = (await file.read()).decode("utf-8-sig")
        obj = obj_by_id[object_type]
        valid_attrs = {a["id"] for a in obj.get("attributes", [])} | {r["id"] for r in obj.get("relations", [])}
        csv_field_map = None
        if explicit_field_map:
            csv_field_map = {k: v for k, v in explicit_field_map.items() if v in valid_attrs}
        records, parsed_field_map = _parse_csv(content, object_type, filename, valid_attrs, csv_field_map, obj)
        candidate = _build_candidate_records(ontology_id, ontology, object_type, filename, records, parsed_field_map, project_id)
        rec = _save_candidate(candidate, project_id, filename)
        candidates.append(rec)
    else:
        raise ApiError("VALIDATION_ERROR", "仅支持 .xlsx 或 .csv 文件", http_status=422)

    if not candidates:
        raise ApiError("VALIDATION_ERROR", "未找到可导入的对象类型或 sheet 为空", http_status=422)

    for rec in candidates:
        logger.info("upload_instances candidate_id=%s project_id=%s ontology_id=%s object_type=%s count=%d",
                    rec["id"], project_id, ontology_id, rec["object_type"], len(rec["instances"]))
    return success({"candidates": candidates, "total": len(candidates)}, http_status=201)


def _infer_field_map_with_llm(
    headers: list[str],
    object_type: str,
    obj: dict[str, Any],
    llm_endpoint: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    """调用 LLM 把中文/英文表头映射到对象属性 id。"""
    attrs = obj.get("attributes", [])
    attr_lines = [f"- {a['id']} (名称={a.get('name', '')}, 类型={a.get('data_type', 'string')}, 必填={a.get('required', False)})" for a in attrs]
    prompt = (
        "你是企业数据集成专家。请根据表头和目标对象的属性定义，推断每个表头应映射到哪个属性 id。\n\n"
        f"目标对象类型: {object_type}\n"
        f"目标对象名称: {obj.get('name', '')}\n"
        "可用属性:\n" + "\n".join(attr_lines) + "\n\n"
        f"表头: {headers}\n\n"
        "请只返回 JSON 对象，key 为表头，value 为最匹配的可用属性 id。"
        "如果某个表头无法映射到任何属性，value 设为 null。不要返回解释。"
    )
    result = call_llm(prompt, base_url=llm_endpoint, model=model, temperature=0.0, max_tokens=2048)
    content = result["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM 返回的不是对象")
    valid_attrs = {a["id"] for a in attrs}
    field_map: dict[str, str] = {}
    for src, target in parsed.items():
        if target and target in valid_attrs:
            field_map[src] = target
    return field_map


@router.post("/upload-smart")
async def upload_instances_smart(
    ontology_id: str,
    object_type: str | None = None,
    auto_apply: bool = Form(default=False),
    llm_endpoint: str | None = Form(default=None),
    model: str | None = Form(default=None),
    file: UploadFile = File(...),
    project_id: str = Depends(project_id_param),
):
    """上传 Excel/CSV，由 LLM 自动推断表头到属性的映射。

    - 默认返回每个 sheet/object 的推断映射供用户确认。
    - auto_apply=true 时直接生成 candidate 记录。
    """
    ontology = _load_ontology(ontology_id, project_id)
    biz_errors = validate_business_layer(ontology)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "本体模型未通过业务层校验", http_status=422,
                       details={"violations": biz_errors})

    objects = ontology.get("objects", [])
    obj_by_id = {o["id"]: o for o in objects}
    filename = file.filename or ""
    ext = filename.split(".")[-1].lower() if "." in filename else ""

    candidates: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []

    if ext == "xlsx":
        try:
            from openpyxl import load_workbook
        except Exception as e:  # noqa: BLE001
            raise ApiError("SERVICE_UNAVAILABLE", f"openpyxl 未安装: {e}", http_status=503) from e
        content = await file.read()
        wb = load_workbook(io.BytesIO(content))
        for sheet_name in wb.sheetnames:
            matched_object_type = _match_object_type(sheet_name, ontology) if not object_type else object_type
            if matched_object_type not in obj_by_id:
                continue
            obj = obj_by_id[matched_object_type]
            headers = [str(cell.value or "").strip() for cell in next(wb[sheet_name].iter_rows(min_row=1, max_row=1))]
            try:
                field_map = _infer_field_map_with_llm(headers, matched_object_type, obj, llm_endpoint, model)
            except Exception as e:  # noqa: BLE001
                logger.warning("upload-smart LLM 推断失败 sheet=%s: %s", sheet_name, e)
                raise ApiError("LLM_ERROR", f"字段映射推断失败: {e}", http_status=502) from e
            mappings.append({"sheet": sheet_name, "object_type": matched_object_type, "field_map": field_map})
            if auto_apply and field_map:
                records, _ = _parse_excel_sheet(wb[sheet_name], matched_object_type, filename, set(field_map.values()), field_map, obj)
                if records:
                    candidate = _build_candidate_records(ontology_id, ontology, matched_object_type, filename, records, field_map, project_id)
                    rec = _save_candidate(candidate, project_id, filename)
                    candidates.append(rec)
    elif ext == "csv":
        if not object_type:
            object_type = _match_object_type(filename.rsplit(".", 1)[0], ontology)
        if not object_type or object_type not in obj_by_id:
            raise ApiError("VALIDATION_ERROR", "CSV 上传必须指定 object_type 或文件名可推断对象类型", http_status=422)
        content = (await file.read()).decode("utf-8-sig")
        obj = obj_by_id[object_type]
        import csv
        lines = content.splitlines()
        reader = csv.DictReader(lines)
        headers = reader.fieldnames or []
        try:
            field_map = _infer_field_map_with_llm(headers, object_type, obj, llm_endpoint, model)
        except Exception as e:  # noqa: BLE001
            raise ApiError("LLM_ERROR", f"字段映射推断失败: {e}", http_status=502) from e
        mappings.append({"sheet": "csv", "object_type": object_type, "field_map": field_map})
        if auto_apply and field_map:
            records, _ = _parse_csv(content, object_type, filename, set(field_map.values()), field_map, obj)
            if records:
                candidate = _build_candidate_records(ontology_id, ontology, object_type, filename, records, field_map, project_id)
                rec = _save_candidate(candidate, project_id, filename)
                candidates.append(rec)
    else:
        raise ApiError("VALIDATION_ERROR", "仅支持 .xlsx 或 .csv 文件", http_status=422)

    if auto_apply:
        if not candidates:
            raise ApiError("VALIDATION_ERROR", "未找到可导入的对象类型或 sheet 为空", http_status=422)
        return success({"candidates": candidates, "total": len(candidates)}, http_status=201)
    return success({"mappings": mappings, "total": len(mappings)})


@router.post("/preview")
def preview(payload: IngestPreviewRequest, project_id: str = Depends(project_id_param)):
    ontology = _load_ontology(payload.ontology_id, project_id)
    biz_errors = validate_business_layer(ontology)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "本体模型未通过业务层校验，无法生成候选", http_status=422,
                       details={"violations": biz_errors})

    candidate = preview_candidate(payload, ontology)
    rec = {
        "id": uuid.uuid4().hex,
        "project_id": project_id,
        "ontology_id": candidate["ontology_id"],
        "source_type": candidate["source_type"],
        "source_ref": candidate["source_ref"],
        "object_type": candidate["object_type"],
        "instances": candidate["instances"],
        "status": "pending",
        "created_at": _now(),
        "approve_result": None,
    }
    _store_for(project_id).insert(rec)
    logger.info("ingest preview candidate_id=%s project_id=%s source=%s instances=%d",
                rec["id"], project_id, rec["source_type"], len(rec["instances"]))
    return success(rec, http_status=201)


@router.get("/candidates")
def list_candidates(
    project_id: str = Depends(project_id_param),
    ontology_id: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
):
    page, page_size = parse_pagination(page, page_size)
    items = _store_for(project_id).all()
    if ontology_id:
        items = [c for c in items if c.get("ontology_id") == ontology_id]
    if status:
        items = [c for c in items if c.get("status") == status]
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    return success(rec)


def _approve_one(rec: dict, candidate_id: str, project_id: str) -> dict:
    """单个候选批准：校验 + 落库 + 状态回写（供单条/批量批准复用）。"""
    store = _store_for(project_id)
    ontology = _load_ontology(rec["ontology_id"], project_id)
    result = approve_candidate_logic(rec, ontology, project_id=project_id)

    if result["imported"] > 0:
        rec["status"] = "imported"
        instance_store = _instance_store_for(project_id)
        for i, inst in enumerate(rec.get("instances", [])):
            instance_record = {
                "id": inst.get("id") or f"{rec['object_type']}_{uuid.uuid4().hex}",
                "project_id": project_id,
                "ontology_id": rec.get("ontology_id"),
                "object_type": rec.get("object_type"),
                "candidate_id": candidate_id,
                "instance": inst,
                "imported_at": _now(),
                "tx_id": result.get("neo_tx_id"),
            }
            instance_store.insert(instance_record)
        _sync_rdf(project_id, rec.get("ontology_id"))
    rec["approve_result"] = result
    store.update(candidate_id, rec)
    logger.info("approve candidate_id=%s project_id=%s imported=%d violations=%d",
                candidate_id, project_id, result["imported"], len(result["violations"]))
    return result


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: str, project_id: str = Depends(project_id_param)):
    store = _store_for(project_id)
    rec = store.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    if rec.get("status") == "imported":
        return success(rec.get("approve_result") or ApproveResult(imported=0, rejected=0, violations=[], neo_tx_id=None).model_dump())
    if rec.get("status") == "rejected":
        raise ApiError("VALIDATION_ERROR", "已拒绝的候选不能重新审批", http_status=422)
    return success(_approve_one(rec, candidate_id, project_id))


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: str, project_id: str = Depends(project_id_param)):
    store = _store_for(project_id)
    rec = store.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    if rec.get("status") == "imported":
        raise ApiError("FORBIDDEN_WRITE", "已落库的候选不能拒绝", http_status=403)
    store.update(candidate_id, {"status": "rejected"})
    logger.info("reject candidate_id=%s project_id=%s", candidate_id, project_id)
    return success({"status": "rejected"})


@router.post("/candidates/{candidate_id}/revert")
def revert_candidate(candidate_id: str, project_id: str = Depends(project_id_param)):
    """1.4：已驳回候选恢复为待审核。"""
    store = _store_for(project_id)
    rec = store.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    if rec.get("status") != "rejected":
        raise ApiError("FORBIDDEN_WRITE", "仅已驳回的候选可恢复为待审核", http_status=403)
    store.update(candidate_id, {"status": "pending"})
    logger.info("revert candidate_id=%s project_id=%s", candidate_id, project_id)
    return success({"candidate_id": candidate_id, "status": "pending"})


@router.delete("/candidates/batch")
def delete_candidates_batch(payload: dict, project_id: str = Depends(project_id_param)):
    """1.4：批量删除候选（仅限 已驳回/已入库）。"""
    store = _store_for(project_id)
    ids = payload.get("candidate_ids")
    if not isinstance(ids, list) or not ids:
        raise ApiError("VALIDATION_ERROR", "candidate_ids 必须是非空数组", http_status=422)
    deleted, skipped = [], []
    for cid in ids:
        rec = store.get(cid)
        if not rec:
            skipped.append({"id": cid, "reason": "not_found"})
            continue
        if rec.get("status") == "pending":
            skipped.append({"id": cid, "reason": "pending 候选需先批准或驳回"})
            continue
        store.delete(cid)
        deleted.append(cid)
    logger.info("delete_candidates_batch deleted=%d skipped=%d project_id=%s", len(deleted), len(skipped), project_id)
    return success({"deleted": deleted, "skipped": skipped})


@router.delete("/candidates/{candidate_id}")
def delete_candidate(candidate_id: str, project_id: str = Depends(project_id_param)):
    """1.4：删除单个候选（仅限 已驳回/已入库；待审核需先批准或驳回）。"""
    store = _store_for(project_id)
    rec = store.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    if rec.get("status") == "pending":
        raise ApiError("FORBIDDEN_WRITE", "待审核候选不能删除，请先批准或驳回", http_status=403)
    if not store.delete(candidate_id):
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    logger.info("delete_candidate candidate_id=%s project_id=%s", candidate_id, project_id)
    return success({"deleted": True})


@router.post("/candidates/batch-approve")
def batch_approve_candidates(payload: dict, project_id: str = Depends(project_id_param)):
    """1.4：批量批准（仅对 pending 候选生效，逐组执行与单个批准相同的校验+落库）。"""
    store = _store_for(project_id)
    ids = payload.get("candidate_ids")
    if not isinstance(ids, list) or not ids:
        raise ApiError("VALIDATION_ERROR", "candidate_ids 必须是非空数组", http_status=422)
    affected, skipped = 0, 0
    total_imported, all_violations = 0, []
    for cid in ids:
        rec = store.get(cid)
        if not rec or rec.get("status") != "pending":
            skipped += 1
            continue
        result = _approve_one(rec, cid, project_id)
        total_imported += result.get("imported", 0)
        all_violations.extend(result.get("violations", []))
        affected += 1
    logger.info("batch_approve affected=%d skipped=%d project_id=%s", affected, skipped, project_id)
    return success({
        "affected": affected,
        "skipped": skipped,
        "imported": total_imported,
        "violations": all_violations,
    })


@router.post("/candidates/batch-reject")
def batch_reject_candidates(payload: dict, project_id: str = Depends(project_id_param)):
    """1.4：批量驳回（仅对 pending 候选生效）。"""
    store = _store_for(project_id)
    ids = payload.get("candidate_ids")
    if not isinstance(ids, list) or not ids:
        raise ApiError("VALIDATION_ERROR", "candidate_ids 必须是非空数组", http_status=422)
    affected, skipped = 0, 0
    for cid in ids:
        rec = store.get(cid)
        if not rec or rec.get("status") != "pending":
            skipped += 1
            continue
        store.update(cid, {"status": "rejected"})
        affected += 1
    logger.info("batch_reject affected=%d skipped=%d project_id=%s", affected, skipped, project_id)
    return success({"affected": affected, "skipped": skipped})


@router.put("/candidates/{candidate_id}/instances")
def update_candidate_instances(
    candidate_id: str,
    payload: dict,
    project_id: str = Depends(project_id_param),
):
    store = _store_for(project_id)
    rec = store.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    if rec.get("status") == "imported":
        raise ApiError("FORBIDDEN_WRITE", "已入库的候选不能修改实例", http_status=403)
    instances = payload.get("instances")
    if not isinstance(instances, list):
        raise ApiError("VALIDATION_ERROR", "instances 必须是数组", http_status=422)
    store.update(candidate_id, {"instances": instances})
    logger.info("update_candidate_instances candidate_id=%s project_id=%s count=%d", candidate_id, project_id, len(instances))
    return success({"updated": True, "count": len(instances)})


@router.delete("/candidates/{candidate_id}/instances/{idx}")
def delete_candidate_instance(
    candidate_id: str,
    idx: int,
    project_id: str = Depends(project_id_param),
):
    store = _store_for(project_id)
    rec = store.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    if rec.get("status") == "imported":
        raise ApiError("FORBIDDEN_WRITE", "已入库的候选不能删除实例", http_status=403)
    instances = list(rec.get("instances", []))
    if idx < 0 or idx >= len(instances):
        raise ApiError("VALIDATION_ERROR", "实例索引越界", http_status=422)
    instances.pop(idx)
    store.update(candidate_id, {"instances": instances})
    logger.info("delete_candidate_instance candidate_id=%s idx=%d project_id=%s", candidate_id, idx, project_id)
    return success({"deleted": True, "count": len(instances)})


@router.get("/instances")
def list_instances(
    project_id: str = Depends(project_id_param),
    ontology_id: str | None = None,
    object_type: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
):
    page, page_size = parse_pagination(page, page_size)
    items = _instance_store_for(project_id).all()
    if ontology_id:
        items = [it for it in items if it.get("ontology_id") == ontology_id]
    if object_type:
        items = [it for it in items if it.get("object_type") == object_type]
    items.sort(key=lambda x: x.get("imported_at") or "", reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/instances/{instance_id}")
def get_instance(instance_id: str, project_id: str = Depends(project_id_param)):
    store = _instance_store_for(project_id)
    for rec in store.all():
        if rec.get("id") == instance_id:
            return success(rec)
    raise ApiError("NOT_FOUND", "实例不存在", http_status=404)


@router.put("/instances/{instance_id}")
def update_instance(
    instance_id: str,
    payload: dict,
    project_id: str = Depends(project_id_param),
):
    store = _instance_store_for(project_id)
    rec = None
    for it in store.all():
        if it.get("id") == instance_id:
            rec = it
            break
    if not rec:
        raise ApiError("NOT_FOUND", "实例不存在", http_status=404)

    new_instance = payload.get("instance")
    if not isinstance(new_instance, dict):
        raise ApiError("VALIDATION_ERROR", "instance 必须是对象", http_status=422)

    rec["instance"] = new_instance
    rec["updated_at"] = _now()
    store.update(instance_id, rec)
    _sync_rdf(project_id, rec.get("ontology_id"))
    logger.info("update_instance id=%s project_id=%s", instance_id, project_id)
    return success(rec)


@router.delete("/instances/{instance_id}")
def delete_instance(instance_id: str, project_id: str = Depends(project_id_param)):
    store = _instance_store_for(project_id)
    rec = next((it for it in store.all() if it.get("id") == instance_id), None)
    if rec is None or not store.delete(instance_id):
        raise ApiError("NOT_FOUND", "实例不存在", http_status=404)
    _sync_rdf(project_id, rec.get("ontology_id"))
    logger.info("delete_instance id=%s project_id=%s", instance_id, project_id)
    return success({"deleted": True})
