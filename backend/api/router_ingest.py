"""实例归化流水线路由（UC2 / VS3）。

铁律：LLM 输出禁止直落库。预览生成候选（LLM/字段映射），
审核通过 → SHACL 校验 → 落 Neo4j（不可用时降级本地 JSONL）。
"""
from __future__ import annotations

import copy
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from api.dependencies import project_id_param
import audit as audit_svc
from core.instance_store import get_instance_store
from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from ai_helper.client import call_llm
from tenant import (
    audit_content_open,
    bypass,
    content_visible,
    current_user,
    ensure_project_access,
    ensure_schema_visible,
    ensure_schema_write,
    instance_read_allowed,
    instance_uploader,
    is_admin,
    mask_instance_content,
    merge_open_targets,
    remove_open_targets,
    validate_open_targets,
)


def _request_username(request: Request) -> str:
    """V2-06：上传者归属（鉴权关闭时 dev）。"""
    user = current_user(request) or {}
    return user.get("username") or "dev"


def _ensure_instance_write(request: Request, project_id: str, rec: dict) -> None:
    """V2-06：实例写权限 = 实例可见（同租户＋schema 可见）且 schema 写权限（共享只授读）。"""
    if bypass():
        return
    user = current_user(request)
    ont = _ontologies_for(project_id).get(rec.get("ontology_id"))
    if not ont or not instance_read_allowed(user, project_id, ont):
        raise ApiError("NOT_FOUND", "实例不存在", http_status=404)
    ensure_schema_write(request, project_id, ont)
from ingestion import approve as approve_candidate_logic
from ingestion import preview as preview_candidate
from ingestion import validate_instance
from jena_adapter import rdf_sync
from logging_setup import get_logger
from modeling_translator import blocking_violations
from schemas.ingest import ApproveResult, IngestPreviewRequest

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
logger = get_logger("api.ingest")


def _store_for(project_id: str) -> JsonStore:
    return JsonStore("ingest_candidates", project_id=project_id)


def _sync_rdf(project_id: str, ontology_id: str | None, *,
              instance_id: str | None = None, deleted: bool = False) -> None:
    """写入 RDF 存储；失败仅告警，不阻断主流程（L5 依赖此语义）。

    HF-02：传入 instance_id 时走单实例增量同步，否则保持全量重建。
    """
    if not ontology_id:
        return
    try:
        if instance_id:
            rdf_sync.sync_instance(project_id, ontology_id, instance_id, deleted=deleted)
        else:
            rdf_sync.sync_ontology(project_id, ontology_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("RDF 同步失败（不影响主流程） ontology_id=%s: %s", ontology_id, e)


def _change_log_store(project_id: str) -> JsonStore:
    return JsonStore("instance_change_logs", project_id=project_id)


def _write_change_log(project_id: str, rec: dict, before: dict, after: dict,
                      reason: str | None, operator: str, operation: str,
                      warnings: list[str] | None = None) -> None:
    """HF-01：字段级变更审计（before/after/diff/操作人/原因）。"""
    diff = {
        k: {"before": before.get(k), "after": after.get(k)}
        for k in sorted(set(before) | set(after))
        if before.get(k) != after.get(k)
    }
    _change_log_store(project_id).insert({
        "id": uuid.uuid4().hex,
        "ts": _now(),
        "instance_id": rec.get("id"),
        "object_type": rec.get("object_type"),
        "operation": operation,
        "operator": operator,
        "reason": reason or "",
        "before": before,
        "after": after,
        "diff": diff,
        "changed_fields": sorted(diff),
        "warnings": warnings or [],
    })


def _validate_for_update(store, project_id: str, rec: dict, new_instance: dict,
                         strict_fields: bool) -> list[str]:
    """HF-01：PUT/PATCH/批量共用同一校验链路（与 approve 同源，避免逻辑分叉）。"""
    ontology_id = rec.get("ontology_id")
    if not ontology_id:
        return []
    ontology = _load_ontology(ontology_id, project_id)
    rows = store.find(object_type=rec.get("object_type"), limit=None)
    existing = [r for r in rows
                if r.get("ontology_id") in (None, "", ontology_id)]
    checked = validate_instance(
        ontology, new_instance, rec.get("object_type"),
        existing=existing, exclude_id=rec.get("id"), strict_fields=strict_fields,
    )
    if checked["violations"]:
        raise ApiError("VALIDATION_ERROR", "实例更新校验未通过", http_status=422,
                       details={"violations": checked["violations"]})
    return checked["warnings"]


def _invalidate_capability_results(project_id: str,
                                   baseline_instance_id: str) -> dict:
    """HF-04：基线实例变更后，把命中的能力写回结果显式标记为 stale。"""
    try:
        from capabilities import CapabilityService
        return CapabilityService(project_id).invalidate_for_baseline(baseline_instance_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("能力结果失效标记失败（不影响实例更新） instance_id=%s: %s",
                       baseline_instance_id, e)
        return {"marked": 0, "capabilities": [], "instance_ids": [], "error": str(e)}


def _instance_store_for(project_id: str):
    return get_instance_store(project_id=project_id)


def _ontologies_for(project_id: str) -> JsonStore:
    return JsonStore("ontologies", project_id=project_id)


def _namespace_ontology_ids(project_id: str, namespace: str) -> set[str]:
    """指定部门空间内的本体 id 集合（无 namespace 字段的本体归属默认空间=项目 id）。"""
    ns = namespace or project_id
    return {
        o["id"]
        for o in _ontologies_for(project_id).all()
        if (o.get("namespace") or project_id) == ns
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_ontology(ontology_id: str, project_id: str) -> dict:
    rec = _ontologies_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return rec


def _match_object_type(token: str, ontology: dict[str, Any]) -> str | None:
    """按 sheet/文件名 token 启发式匹配对象 id（支持 obj_ 前缀与中文名）。
    精确匹配优先，其次包含匹配且候选串更长者胜（避免 sheet=Supplier 被 VmiSupplier 抢占）。"""
    token_lower = token.lower().replace(" ", "").replace("_", "").replace("-", "")
    objects = ontology.get("objects", [])
    exact: str | None = None
    best: tuple[str, int] = ("", -1)
    for obj in objects:
        oid = obj.get("id", "").lower()
        candidates = [
            oid,
            oid.replace("obj_", ""),
            oid.replace("obj", ""),
            obj.get("name", "").lower().replace(" ", ""),
        ]
        for c in list(candidates) + [c.replace("_", "") for c in candidates]:
            if not c:
                continue
            if c == token_lower:
                exact = obj["id"]
            elif token_lower in c or c in token_lower:
                score = len(c)
                if score > best[1]:
                    best = (obj["id"], score)
    if exact:
        return exact
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
    created_by: str | None = None,
    field_map: dict[str, str] | None = None,
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
    if field_map:
        # V2-08（M3-B）：持久化字段映射，使血缘「源字段」要素可回溯（与 external_mcp 路径一致）
        rec["mapping"] = {"field_map": field_map, "field_map_source": "file"}
    if created_by:
        rec["created_by"] = created_by  # V2-06：上传者归属（上传型内容默认仅本人可读）
    # V2-21（M3-C）：批次健康卡——入库时点观测指标（键重复/缺键/枚举违规分布/必填填充率/新鲜度），
    # 供人工确认卡展示；观测性，不阻断审批
    try:
        from data_quality import compute_batch_health
        rec["health"] = compute_batch_health(project_id, rec)
    except Exception as e:  # 健康卡失败不阻断候选创建
        rec["health"] = {"error": f"批次健康卡计算失败: {e}"}
        logger.warning("批次健康卡计算失败 candidate=%s: %s", rec["id"], e)
    _store_for(project_id).insert(rec)
    return rec


@router.post("/upload")
async def upload_instances(
    request: Request,
    ontology_id: str,
    object_type: str | None = None,
    field_map: str | None = Form(default=None),
    file: UploadFile = File(...),
    project_id: str = Depends(project_id_param),
):
    ensure_project_access(request, project_id)  # V2-06：租户隔离
    """上传 Excel/CSV 生成候选实例。xlsx 支持按 sheet 名/文件名推断对象类型；
    未指定 object_type 时遍历所有 sheet，每个合法 sheet 生成一个候选。csv 必须指定 object_type。
    field_map 为 JSON 字符串，{源表头: 属性id}，显式覆盖自动匹配。"""
    ontology = _load_ontology(ontology_id, project_id)
    biz_errors = blocking_violations(ontology)
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
            if not parsed_field_map:
                headers = [str(cell.value or "").strip() for cell in next(wb[sheet_name].iter_rows(min_row=1, max_row=1))]
                raise ApiError("VALIDATION_ERROR",
                               f"sheet「{sheet_name}」的表头 {headers} 无法匹配对象「{obj.get('name') or matched_object_type}」的任何属性，"
                               f"请检查表头（应使用属性 id 或名称）或显式传 field_map", http_status=422)
            candidate = _build_candidate_records(ontology_id, ontology, matched_object_type, filename, records, parsed_field_map, project_id)
            rec = _save_candidate(candidate, project_id, filename, created_by=_request_username(request),
                                  field_map=parsed_field_map)  # V2-08：血缘源字段要素
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
        if not records:
            raise ApiError("VALIDATION_ERROR", "CSV 无数据行", http_status=422)
        if not parsed_field_map:
            raise ApiError("VALIDATION_ERROR",
                           f"CSV 表头无法匹配对象「{obj.get('name') or object_type}」的任何属性，"
                           f"请检查表头（应使用属性 id 或名称）或显式传 field_map", http_status=422)
        candidate = _build_candidate_records(ontology_id, ontology, object_type, filename, records, parsed_field_map, project_id)
        rec = _save_candidate(candidate, project_id, filename, created_by=_request_username(request),
                              field_map=parsed_field_map)  # V2-08：血缘源字段要素
        candidates.append(rec)
    else:
        raise ApiError("VALIDATION_ERROR", "仅支持 .xlsx 或 .csv 文件", http_status=422)

    if not candidates:
        raise ApiError("VALIDATION_ERROR", "未找到可导入的对象类型或 sheet 为空", http_status=422)

    for rec in candidates:
        logger.info("upload_instances candidate_id=%s project_id=%s ontology_id=%s object_type=%s count=%d",
                    rec["id"], project_id, ontology_id, rec["object_type"], len(rec["instances"]))
        audit_svc.record_from_request(request, "ingest_upload", project_id=project_id,
                                      target_type="ingest_candidate", target_id=rec["id"],
                                      target_name=rec.get("object_type", ""),
                                      after={"ontology_id": ontology_id, "object_type": rec.get("object_type"),
                                             "records": len(rec["instances"]), "filename": filename})
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
    request: Request,
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
    ensure_project_access(request, project_id)  # V2-06：租户隔离
    ontology = _load_ontology(ontology_id, project_id)
    biz_errors = blocking_violations(ontology)
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
                    rec = _save_candidate(candidate, project_id, filename, created_by=_request_username(request),
                                          field_map=field_map)  # V2-08：血缘源字段要素
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
                rec = _save_candidate(candidate, project_id, filename, created_by=_request_username(request),
                                      field_map=field_map)  # V2-08：血缘源字段要素
                candidates.append(rec)
    else:
        raise ApiError("VALIDATION_ERROR", "仅支持 .xlsx 或 .csv 文件", http_status=422)

    if auto_apply:
        if not candidates:
            raise ApiError("VALIDATION_ERROR", "未找到可导入的对象类型或 sheet 为空", http_status=422)
        return success({"candidates": candidates, "total": len(candidates)}, http_status=201)
    return success({"mappings": mappings, "total": len(mappings)})


@router.post("/preview")
def preview(payload: IngestPreviewRequest, request: Request, project_id: str = Depends(project_id_param)):
    ensure_project_access(request, project_id)  # V2-06：租户隔离
    ontology = _load_ontology(payload.ontology_id, project_id)
    ensure_schema_visible(request, project_id, ontology)
    biz_errors = blocking_violations(ontology)
    if biz_errors:
        raise ApiError("VALIDATION_ERROR", "本体模型未通过业务层校验，无法生成候选", http_status=422,
                       details={"violations": biz_errors})

    candidate = preview_candidate(payload, ontology)
    preview_field_map = (payload.mapping or {}).get("field_map") if payload.mapping else None
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
        "created_by": _request_username(request),  # V2-06：上传者归属
    }
    if preview_field_map:
        # V2-08（M3-B）：持久化字段映射，使血缘「源字段」要素可回溯
        rec["mapping"] = {"field_map": preview_field_map, "field_map_source": "file"}
    _store_for(project_id).insert(rec)
    logger.info("ingest preview candidate_id=%s project_id=%s source=%s instances=%d",
                rec["id"], project_id, rec["source_type"], len(rec["instances"]))
    return success(rec, http_status=201)


@router.get("/candidates")
def list_candidates(
    project_id: str = Depends(project_id_param),
    namespace: str | None = None,
    ontology_id: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
):
    page, page_size = parse_pagination(page, page_size)
    items = _store_for(project_id).all()
    if namespace:
        items = [c for c in items if c.get("ontology_id") in _namespace_ontology_ids(project_id, namespace)]
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


@router.get("/candidates/{candidate_id}/health")
def get_candidate_health(candidate_id: str, project_id: str = Depends(project_id_param)):
    """V2-21（M3-C）：批次健康卡即时重算（确认卡刷新/存量候选补算）。"""
    rec = _store_for(project_id).get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    from data_quality import compute_batch_health
    try:
        # EP-21-01：健康卡计算失败返回显式 error（200），不 500、不阻断人工确认主流程
        return success(compute_batch_health(project_id, rec))
    except Exception as e:
        logger.warning("批次健康卡计算失败 candidate=%s: %s", candidate_id, e)
        return success({"error": f"批次健康卡计算失败: {e}"})


def _stable_key_for(ontology: dict, object_type: str) -> str | None:
    """V2-03：取对象当前（可能已显式指定）的业务主键属性 id。"""
    obj = next((o for o in (ontology or {}).get("objects", []) if o.get("id") == object_type), None)
    return (obj or {}).get("stable_key")


def _match_by_stable_key(instance_store, ontology_id: str, object_type: str,
                         key: str, value: Any) -> dict | None:
    """按 (本体, 对象类型, 主键字段, 主键值) 命中已有实例；无该元数据的存量实例不参与。"""
    for r in instance_store.find(object_type=object_type, limit=None):
        if (r.get("ontology_id") or ontology_id) != ontology_id:
            continue
        if r.get("stable_key") != key:
            continue
        if str(r.get("stable_key_value")) != str(value):
            continue
        return r
    return None


def _approve_one(rec: dict, candidate_id: str, project_id: str) -> dict:
    """单个候选批准：校验 + 按业务主键匹配/去重落库 + 状态回写（供单条/批量批准复用）。

    V2-03 / TP-03-04：新入库实例写入 stable_key / stable_key_value 元数据；
    同主键已存在时更新该实例（保留原 id、合并字段）而非追加，批次内重复同样收敛。
    无主键元数据的存量实例不被静默改写，也不参与匹配。
    """
    store = _store_for(project_id)
    ontology = _load_ontology(rec["ontology_id"], project_id)
    result = approve_candidate_logic(rec, ontology, project_id=project_id)

    if result["imported"] > 0:
        rec["status"] = "imported"
        instance_store = _instance_store_for(project_id)
        key = _stable_key_for(ontology, rec.get("object_type"))
        uploader = rec.get("created_by")  # V2-06：上传型内容默认仅上传者可读
        n_inserted = n_deduped = 0
        for inst in rec.get("instances", []):
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
            if uploader:
                instance_record["uploaded_by"] = uploader
                instance_record["content_open"] = {"users": [], "departments": []}
            key_value = inst.get(key) if key else None
            if key and key_value is not None and key_value != "":
                instance_record["stable_key"] = key
                instance_record["stable_key_value"] = key_value
                matched = _match_by_stable_key(instance_store, rec.get("ontology_id"),
                                               rec.get("object_type"), key, key_value)
                if matched:
                    merged = dict(matched.get("instance") or {})
                    merged.update(inst)
                    instance_store.update(matched["id"], {
                        "instance": merged,
                        "stable_key": key,
                        "stable_key_value": key_value,
                        "candidate_id": candidate_id,
                        "imported_at": _now(),
                        "tx_id": result.get("neo_tx_id"),
                    })
                    n_deduped += 1
                    continue
            instance_store.insert(instance_record)
            n_inserted += 1
        result["inserted"] = n_inserted
        result["dedup_updated"] = n_deduped
        result["stable_key"] = key
        _sync_rdf(project_id, rec.get("ontology_id"))
    rec["approve_result"] = result
    store.update(candidate_id, rec)
    logger.info("approve candidate_id=%s project_id=%s imported=%d inserted=%d dedup=%d violations=%d",
                candidate_id, project_id, result["imported"], result.get("inserted", 0),
                result.get("dedup_updated", 0), len(result["violations"]))
    return result


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(request: Request, candidate_id: str, project_id: str = Depends(project_id_param)):
    ensure_project_access(request, project_id)  # V2-06：租户隔离
    store = _store_for(project_id)
    rec = store.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    if rec.get("status") == "imported":
        return success(rec.get("approve_result") or ApproveResult(imported=0, rejected=0, violations=[], neo_tx_id=None).model_dump())
    if rec.get("status") == "rejected":
        raise ApiError("VALIDATION_ERROR", "已拒绝的候选不能重新审批", http_status=422)
    # V2-09（TC-09-02）：_approve_one 会原地改写 rec（status→imported 并整对象写回），
    # before 必须在调用前捕获，否则审计前值被污染
    before_status = rec.get("status")
    result = _approve_one(rec, candidate_id, project_id)
    audit_svc.record_from_request(request, "ingest_approve", project_id=project_id,
                                  target_type="ingest_candidate", target_id=candidate_id,
                                  target_name=rec.get("object_type", ""),
                                  before={"status": before_status},
                                  after={"status": "imported", "imported": result.get("imported"),
                                         "violations": len(result.get("violations", []))})
    return success(result)


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(request: Request, candidate_id: str, project_id: str = Depends(project_id_param)):
    ensure_project_access(request, project_id)  # V2-06：租户隔离
    store = _store_for(project_id)
    rec = store.get(candidate_id)
    if not rec:
        raise ApiError("NOT_FOUND", "候选不存在", http_status=404)
    if rec.get("status") == "imported":
        raise ApiError("FORBIDDEN_WRITE", "已落库的候选不能拒绝", http_status=403)
    store.update(candidate_id, {"status": "rejected"})
    audit_svc.record_from_request(request, "ingest_reject", project_id=project_id,
                                  target_type="ingest_candidate", target_id=candidate_id,
                                  target_name=rec.get("object_type", ""),
                                  before={"status": rec.get("status")}, after={"status": "rejected"})
    logger.info("reject candidate_id=%s project_id=%s", candidate_id, project_id)
    return success({"status": "rejected"})


@router.post("/candidates/{candidate_id}/revert")
def revert_candidate(request: Request, candidate_id: str, project_id: str = Depends(project_id_param)):
    """1.4：已驳回候选恢复为待审核。"""
    ensure_project_access(request, project_id)  # V2-06：租户隔离
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
def batch_approve_candidates(request: Request, payload: dict, project_id: str = Depends(project_id_param)):
    """1.4：批量批准（仅对 pending 候选生效，逐组执行与单个批准相同的校验+落库）。"""
    ensure_project_access(request, project_id)  # V2-06：租户隔离
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
        # V2-09（TC-09-01）：批量审批逐条留痕；before 须在 _approve_one 前捕获（其原地改写 rec）
        before_status = rec.get("status")
        result = _approve_one(rec, cid, project_id)
        audit_svc.record_from_request(request, "ingest_approve", project_id=project_id,
                                      target_type="ingest_candidate", target_id=cid,
                                      target_name=rec.get("object_type", ""),
                                      before={"status": before_status, "batch": True},
                                      after={"status": "imported", "imported": result.get("imported"),
                                             "violations": len(result.get("violations", []))})
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
def batch_reject_candidates(request: Request, payload: dict, project_id: str = Depends(project_id_param)):
    """1.4：批量驳回（仅对 pending 候选生效）。"""
    ensure_project_access(request, project_id)  # V2-06：租户隔离
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
    request: Request,
    project_id: str = Depends(project_id_param),
    namespace: str | None = None,
    ontology_id: str | None = None,
    object_type: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
):
    page, page_size = parse_pagination(page, page_size)
    # 过滤下推到存储层并在【全量】结果上统计 total：
    # 不再先 all()（历史 bug：截断到前 1000 条）再本地过滤——那样 object_type 命中的记录若不在
    # 截断快照里就恒为 0。object_type 走专用参数只读对应 jsonl 文件；ontology_id 走 filters 精确匹配。
    store = _instance_store_for(project_id)
    filters = {"ontology_id": ontology_id} if ontology_id else None
    items = store.find(object_type=object_type, filters=filters, limit=None)
    if namespace:
        allowed = set(_namespace_ontology_ids(project_id, namespace))
        items = [it for it in items if it.get("ontology_id") in allowed]
    # V2-06：实例永不共享（INV-2）——仅同租户且 schema 可见的本体实例可见；
    # 上传型内容默认仅上传者可读，未开放者只见元数据（键/类型/关系），字段值置空并标记。
    user = current_user(request)
    if not bypass():
        from core.project_store import ProjectStore
        project = ProjectStore().get(project_id)
        onto_cache: dict[str, dict | None] = {}
        visible_items: list[dict] = []
        for it in items:
            oid = it.get("ontology_id")
            if oid not in onto_cache:
                onto_cache[oid] = _ontologies_for(project_id).get(oid)
            ont = onto_cache[oid]
            if not ont or not instance_read_allowed(user, project_id, ont):
                continue
            if not content_visible(user, project_id, it, ont):
                it = mask_instance_content(it)
            visible_items.append(it)
        items = visible_items
    items.sort(key=lambda x: x.get("imported_at") or "", reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/instances/{instance_id}")
def get_instance(instance_id: str, request: Request, project_id: str = Depends(project_id_param)):
    rec = _instance_store_for(project_id).get(instance_id)
    if rec:
        user = current_user(request)
        if not bypass():
            from core.project_store import ProjectStore
            project = ProjectStore().get(project_id)
            ont = _ontologies_for(project_id).get(rec.get("ontology_id"))
            if not ont or not instance_read_allowed(user, project_id, ont):
                raise ApiError("NOT_FOUND", "实例不存在", http_status=404)  # 跨共享范围：实例不可见（INV-2）
            if not content_visible(user, project_id, rec, ont):
                return success(mask_instance_content(rec))  # 上传型内容未开放：仅元数据
        return success(rec)
    raise ApiError("NOT_FOUND", "实例不存在", http_status=404)


@router.post("/instances/{instance_id}/content-open")
def open_instance_content(
    instance_id: str,
    payload: dict,
    request: Request,
    project_id: str = Depends(project_id_param),
):
    """V2-06 / TC-06-07：上传型实例内容显式开放到人/部门（仅上传者/admin）。

    body: {"users": ["username"...], "departments": ["tenant_id"...]}
    - 指定无效账号/部门 422（TCE-06-01）；
    - 开放上限=本体共享等级可见范围，范围外 422（TCE-06-02）；
    - 审计 content_open_grant。
    """
    rec = _instance_store_for(project_id).get(instance_id)
    if not rec:
        raise ApiError("NOT_FOUND", "实例不存在", http_status=404)
    user = current_user(request) or {}
    ont = _ontologies_for(project_id).get(rec.get("ontology_id"))
    if not ont:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    if not bypass():
        ensure_schema_visible(request, project_id, ont)
        if not (is_admin(user) or instance_uploader(rec) == user.get("username")):
            raise ApiError("FORBIDDEN", "仅上传者本人或管理员可开放实例内容", http_status=403)
    users = [u for u in (payload.get("users") or []) if u]
    departments = [d for d in (payload.get("departments") or []) if d]
    if not users and not departments:
        raise ApiError("VALIDATION_ERROR", "users/departments 至少指定一项", http_status=422)
    errors = validate_open_targets(project_id, ont, users, departments)
    invalid = errors["invalid_users"] + errors["invalid_departments"]
    out_of_cap = errors["out_of_cap_users"] + errors["out_of_cap_departments"]
    if invalid:
        raise ApiError("VALIDATION_ERROR", "开放指定了无效账号/部门", http_status=422,
                       details={"invalid_users": errors["invalid_users"],
                                "invalid_departments": errors["invalid_departments"]})
    if out_of_cap:
        raise ApiError("VALIDATION_ERROR", "开放范围超出本体共享等级可见范围（上限=schema 可见范围）",
                       http_status=422,
                       details={"out_of_cap_users": errors["out_of_cap_users"],
                                "out_of_cap_departments": errors["out_of_cap_departments"]})
    rec["content_open"] = merge_open_targets(rec, users, departments)
    _instance_store_for(project_id).update(instance_id, {"content_open": rec["content_open"]})
    audit_content_open(user.get("username") or "dev", project_id, instance_id, users, departments)
    logger.info("content_open instance=%s project=%s users=%d departments=%d",
                instance_id, project_id, len(users), len(departments))
    return success({"id": instance_id, "content_open": rec["content_open"]})


@router.post("/instances/{instance_id}/content-revoke")
def revoke_instance_content(
    instance_id: str,
    payload: dict,
    request: Request,
    project_id: str = Depends(project_id_param),
):
    """V2-06 / TC-06-07：收回实例内容开放范围（仅上传者/admin），审计 content_open_revoke。"""
    rec = _instance_store_for(project_id).get(instance_id)
    if not rec:
        raise ApiError("NOT_FOUND", "实例不存在", http_status=404)
    user = current_user(request) or {}
    ont = _ontologies_for(project_id).get(rec.get("ontology_id"))
    if not ont:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    if not bypass():
        ensure_schema_visible(request, project_id, ont)
        if not (is_admin(user) or instance_uploader(rec) == user.get("username")):
            raise ApiError("FORBIDDEN", "仅上传者本人或管理员可收回实例内容开放", http_status=403)
    users = [u for u in (payload.get("users") or []) if u]
    departments = [d for d in (payload.get("departments") or []) if d]
    if not users and not departments:
        raise ApiError("VALIDATION_ERROR", "users/departments 至少指定一项", http_status=422)
    rec["content_open"] = remove_open_targets(rec, users, departments)
    _instance_store_for(project_id).update(instance_id, {"content_open": rec["content_open"]})
    audit_content_open(user.get("username") or "dev", project_id, instance_id, users, departments,
                       revoked=True)
    logger.info("content_revoke instance=%s project=%s users=%d departments=%d",
                instance_id, project_id, len(users), len(departments))
    return success({"id": instance_id, "content_open": rec["content_open"]})


@router.put("/instances/batch")
def update_instances_batch(
    payload: dict,
    request: Request,
    project_id: str = Depends(project_id_param),
    reason: str | None = Query(default=None),
    strict_fields: bool = Query(default=False),
    defer_rdf_sync: bool = Query(default=False),
):
    """HF-02：批量更新，末尾只做一次 RDF 同步；defer_rdf_sync=true 时交给
    `POST /api/graph/{ontology_id}/rdf-sync` 显式触发（该端点已存在）。"""
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError("VALIDATION_ERROR", "items 必须是非空数组", http_status=422)
    store = _instance_store_for(project_id)
    operator = (getattr(request.state, "auth_user", None) or {}).get("username", "api")
    updated: list[str] = []
    errors: list[dict] = []
    touched: set[str] = set()
    stale_marked = 0
    stale_by_instance: dict[str, dict] = {}
    for item in items:
        iid = item.get("id") if isinstance(item, dict) else None
        new_instance = item.get("instance") if isinstance(item, dict) else None
        if not iid or not isinstance(new_instance, dict):
            errors.append({"id": iid, "error": "id/instance 无效"})
            continue
        rec = store.get(iid)
        if not rec:
            errors.append({"id": iid, "error": "实例不存在"})
            continue
        try:
            _ensure_instance_write(request, project_id, rec)
            warnings = _validate_for_update(store, project_id, rec, new_instance, strict_fields)
        except ApiError as e:
            errors.append({"id": iid, "error": e.message})
            continue
        before = copy.deepcopy(rec.get("instance") or {})
        rec["instance"] = new_instance
        rec["updated_at"] = _now()
        store.update(iid, rec)
        _write_change_log(project_id, rec, before, new_instance,
                          item.get("reason") or reason, operator, "BATCH", warnings)
        stale_info = _invalidate_capability_results(project_id, iid)
        stale_marked += int(stale_info.get("marked") or 0)
        if stale_info.get("marked"):
            stale_by_instance[iid] = stale_info
        updated.append(iid)
        if rec.get("ontology_id"):
            touched.add(rec["ontology_id"])
    if not defer_rdf_sync:
        for oid in touched:
            _sync_rdf(project_id, oid)
    return success({
        "updated": len(updated),
        "ids": updated,
        "errors": errors,
        "rdf_sync_deferred": defer_rdf_sync,
        "ontologies_synced": [] if defer_rdf_sync else sorted(touched),
        "stale_marked": stale_marked,
        "stale_by_instance": stale_by_instance,
    })


@router.put("/instances/{instance_id}")
def update_instance(
    instance_id: str,
    payload: dict,
    request: Request,
    project_id: str = Depends(project_id_param),
    reason: str | None = Query(default=None),
    strict_fields: bool = Query(default=False),
    defer_rdf_sync: bool = Query(default=False),
):
    """HF-01/02：整体替换前走统一校验 + 字段审计；默认单实例增量 RDF 同步。"""
    store = _instance_store_for(project_id)
    rec = store.get(instance_id)
    if not rec:
        raise ApiError("NOT_FOUND", "实例不存在", http_status=404)
    _ensure_instance_write(request, project_id, rec)

    new_instance = payload.get("instance")
    if not isinstance(new_instance, dict):
        raise ApiError("VALIDATION_ERROR", "instance 必须是对象", http_status=422)
    warnings = _validate_for_update(store, project_id, rec, new_instance, strict_fields)

    before = copy.deepcopy(rec.get("instance") or {})
    rec["instance"] = new_instance
    rec["updated_at"] = _now()
    store.update(instance_id, rec)
    stale_info = _invalidate_capability_results(project_id, instance_id)
    if not defer_rdf_sync:
        _sync_rdf(project_id, rec.get("ontology_id"), instance_id=instance_id)
    operator = (getattr(request.state, "auth_user", None) or {}).get("username", "api")
    _write_change_log(project_id, rec, before, new_instance, reason, operator, "PUT", warnings)
    logger.info("update_instance id=%s project_id=%s", instance_id, project_id)
    return success({
        **rec,
        "warnings": warnings,
        "rdf_sync_deferred": defer_rdf_sync,
        "stale_marked": stale_info.get("marked", 0),
        "affected_capabilities": stale_info.get("capabilities", []),
    })


@router.patch("/instances/{instance_id}")
def patch_instance(
    instance_id: str,
    payload: dict,
    request: Request,
    project_id: str = Depends(project_id_param),
    reason: str | None = Query(default=None),
    strict_fields: bool = Query(default=False),
    defer_rdf_sync: bool = Query(default=False),
):
    """HF-01：字段级局部更新，避免整体替换丢失 `_source` / `__capability__` 血缘。"""
    patch = payload.get("patch")
    if patch is None:
        patch = payload.get("instance")
    if not isinstance(patch, dict) or not patch:
        raise ApiError("VALIDATION_ERROR", "patch 必须是非空对象", http_status=422)
    store = _instance_store_for(project_id)
    rec = store.get(instance_id)
    if not rec:
        raise ApiError("NOT_FOUND", "实例不存在", http_status=404)
    _ensure_instance_write(request, project_id, rec)
    before = copy.deepcopy(rec.get("instance") or {})
    merged = dict(before)
    merged.update(patch)
    warnings = _validate_for_update(store, project_id, rec, merged, strict_fields)

    rec["instance"] = merged
    rec["updated_at"] = _now()
    store.update(instance_id, rec)
    stale_info = _invalidate_capability_results(project_id, instance_id)
    if not defer_rdf_sync:
        _sync_rdf(project_id, rec.get("ontology_id"), instance_id=instance_id)
    operator = (getattr(request.state, "auth_user", None) or {}).get("username", "api")
    _write_change_log(project_id, rec, before, merged, reason, operator, "PATCH", warnings)
    return success({
        **rec,
        "warnings": warnings,
        "rdf_sync_deferred": defer_rdf_sync,
        "stale_marked": stale_info.get("marked", 0),
        "affected_capabilities": stale_info.get("capabilities", []),
    })


@router.get("/instances/{instance_id}/change-logs")
def list_instance_change_logs(
    instance_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
    project_id: str = Depends(project_id_param),
):
    """HF-01：查询某实例的字段级变更审计。"""
    rows = [r for r in _change_log_store(project_id).all() if r.get("instance_id") == instance_id]
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    page, page_size = parse_pagination(page, page_size)
    start = (page - 1) * page_size
    return success(list_payload(rows[start: start + page_size], len(rows), page, page_size))


@router.delete("/instances/{instance_id}")
def delete_instance(instance_id: str, request: Request, project_id: str = Depends(project_id_param)):
    store = _instance_store_for(project_id)
    rec = store.get(instance_id)
    if rec is None:
        raise ApiError("NOT_FOUND", "实例不存在", http_status=404)
    _ensure_instance_write(request, project_id, rec)
    if not store.delete(instance_id):
        raise ApiError("NOT_FOUND", "实例不存在", http_status=404)
    stale_info = _invalidate_capability_results(project_id, instance_id)
    _sync_rdf(project_id, rec.get("ontology_id"), instance_id=instance_id, deleted=True)
    audit_svc.record_from_request(request, "instance_delete", project_id=project_id,
                                  target_type="instance", target_id=instance_id,
                                  target_name=rec.get("object_type", ""), before=rec,
                                  detail={"stale_marked": stale_info.get("marked", 0)})
    logger.info("delete_instance id=%s project_id=%s", instance_id, project_id)
    return success({
        "deleted": True,
        "stale_marked": stale_info.get("marked", 0),
        "affected_capabilities": stale_info.get("capabilities", []),
    })
