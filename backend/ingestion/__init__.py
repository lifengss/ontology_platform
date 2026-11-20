"""ingestion：实例归化流水线编排（LLM 候选 / 字段映射 → 人工审核卡 → SHACL → 落 Neo4j）。

铁律（PRD §6.3）：LLM 输出禁止直落库；必须经人工审核 + SHACL 校验。

VS3 设计：
- preview：生成候选实例（LLM/字段映射/外部 MCP），**不入库**。
- approve：SHACL 校验 → 通过则写 Neo4j；Neo4j 不可用时降级为本地 JSONL 文件（与 Jena 文件降级一致）。
"""
from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from core.instance_store import get_instance_store
from core.responses import ApiError
from jena_adapter.client import shacl_validate
from logging_setup import get_logger
from modeling_translator import build_instance_graph, translate_to_shacl

logger = get_logger("ingestion")

# GAP-19：approve SHACL 并行分块大小（每块实例数）；>该值走线程池分块校验
_APPROVE_SHACL_CHUNK = 500


def _existing_records_for_composite(candidate: dict[str, Any],
                                    project_id: str | None) -> list[dict[str, Any]]:
    """HF-03：取同对象、同本体（含历史无 ontology_id）的已入库实例，供复合唯一兜底。"""
    ontology_id = candidate.get("ontology_id")
    pid = project_id or candidate.get("project_id")
    rows = get_instance_store(project_id=pid).find(
        object_type=candidate.get("object_type"), limit=None,
    )
    if not ontology_id:
        return rows
    return [r for r in rows
            if r.get("ontology_id") in (None, "", ontology_id)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_meta(source_type: str, source_ref: str, raw_ref: str = "") -> dict[str, Any]:
    """构建结构化 instance_source（O-07）。

    kind: internal | doc | external_mcp | field_map | llm
    connection_id: 外部连接/端点/文档名标识
    fetched_at: ISO 时间戳
    raw_ref: 源记录主键或文档段落引用
    """
    return {
        "kind": source_type,
        "connection_id": source_ref,
        "fetched_at": _now(),
        "raw_ref": raw_ref,
    }


def preview(payload, ontology: dict[str, Any]) -> dict[str, Any]:
    """根据来源生成候选实例，返回候选 dict（不含 id/status/created_at，由路由回填）。"""
    kind = payload.source.kind
    if kind == "llm":
        return _preview_llm(payload, ontology)
    if kind == "field_map":
        return _preview_field_map(payload, ontology)
    if kind == "external_mcp":
        raise ApiError("VALIDATION_ERROR", "外部 MCP 读取在 VS7 切片实现，请改用 field_map 或直接提供 records", http_status=422)
    raise ApiError("VALIDATION_ERROR", f"未知 source kind: {kind}", http_status=422)


def _resolve_object_type(payload, ontology: dict[str, Any]) -> str:
    """确定目标对象类型：请求显式指定 > mapping.object_type > 本体首个对象。"""
    ot = payload.object_type or (payload.mapping or {}).get("object_type")
    objects = ontology.get("objects", [])
    obj_by_id = {o["id"]: o for o in objects}
    if ot and ot in obj_by_id:
        return ot
    if objects:
        return objects[0]["id"]
    raise ApiError("VALIDATION_ERROR", "本体为空，无法推断目标对象类型", http_status=422)


def _llm_prompt(object_type: str, ontology: dict[str, Any], text: str) -> str:
    obj = next((o for o in ontology.get("objects", []) if o["id"] == object_type), None)
    if obj is None:
        raise ApiError("VALIDATION_ERROR", f"对象类型 {object_type} 不存在", http_status=422)
    attrs = obj.get("attributes", [])
    attr_desc = "\n".join(
        f"- {a['id']} ({a.get('name', a['id'])}, 类型={a.get('data_type','string')}, 必填={a.get('required',False)})"
        for a in attrs
    )
    return (
        "你是企业本体平台的实例抽取助手。请根据以下本体定义和文档内容，抽取实例。\n\n"
        f"目标对象类型: {object_type}\n"
        f"属性列表:\n{attr_desc}\n\n"
        "文档内容:\n" + text + "\n\n"
        "请只返回 JSON，格式如下（不要返回 markdown 代码块外的解释）：\n"
        '{"object_type": "' + object_type + '", "instances": [{"attr_id": "value", ...}, ...]}'
    )


def _extract_json(text: str) -> Any:
    """从可能包含 markdown 的文本中提取 JSON 对象/数组。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    # 也兼容直接返回 JSON 数组/对象
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 尝试查找第一个 {} 或 [] 块
        match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        raise


def _call_llm(endpoint: str, model: str | None, prompt: str) -> dict[str, Any]:
    """调用 OpenAI 兼容 /chat/completions 端点，返回完整响应对象。"""
    from ai_helper.client import call_llm

    result = call_llm(prompt, base_url=endpoint, model=model, temperature=0.0, timeout=30)
    choices = result.get("choices", [])
    if not choices:
        raise ApiError("LLM_UNAVAILABLE", "LLM 返回空 choices", http_status=503)
    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise ApiError("LLM_UNAVAILABLE", "LLM 返回空内容", http_status=503)
    return result


def _preview_llm(payload, ontology: dict[str, Any]) -> dict[str, Any]:
    text = payload.source.text
    if not text:
        raise ApiError("VALIDATION_ERROR", "LLM 来源必须提供 source.text", http_status=422)
    endpoint = payload.source.llm_endpoint
    if not endpoint:
        raise ApiError("VALIDATION_ERROR", "LLM 来源必须提供 source.llm_endpoint", http_status=422)

    object_type = _resolve_object_type(payload, ontology)
    prompt = _llm_prompt(object_type, ontology, text)
    result = _call_llm(endpoint, payload.source.model, prompt)
    content = result["choices"][0]["message"]["content"]
    parsed = _extract_json(content)

    # 支持两种返回：{"object_type": ..., "instances": [...]} 或 直接数组
    if isinstance(parsed, dict):
        instances = parsed.get("instances", [])
        object_type = parsed.get("object_type") or object_type
    elif isinstance(parsed, list):
        instances = parsed
    else:
        raise ApiError("VALIDATION_ERROR", "LLM 返回不是合法 JSON 实例数组", http_status=422)

    if not isinstance(instances, list):
        raise ApiError("VALIDATION_ERROR", "LLM 返回的 instances 不是数组", http_status=422)

    # 给每个实例补 object_type 和溯源
    for inst in instances:
        inst["object_type"] = object_type
        raw_ref = str(inst.get("_raw_ref", text[:200]))
        inst.setdefault("_source", _source_meta("llm", endpoint, raw_ref=raw_ref))

    return {
        "ontology_id": payload.ontology_id,
        "source_type": "llm",
        "source_ref": endpoint,
        "object_type": object_type,
        "instances": instances,
    }


def _clean_value(v: Any) -> Any:
    if v is None:
        return None
    # GAP-06：空串/纯空白视为「缺失」——CSV/Excel 空单元格不再以 "" 落入实例，
    # 避免触发枚举 Literal("") 不符、必填误报等 SHACL 报错；可选属性直接缺键，
    # 必填属性走清晰的「必填缺失」路径。
    if isinstance(v, str) and v.strip() == "":
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def apply_field_map(
    records: list[dict[str, Any]],
    field_map: dict[str, str],
    object_type: str,
    source_ref: str,
    valid_attrs: set[str] | None = None,
    raw_ref_field: str | None = None,
) -> list[dict[str, Any]]:
    """通用字段映射：records × field_map → 候选实例列表。"""
    instances: list[dict[str, Any]] = []
    for rec in records:
        raw_ref = str(rec.get(raw_ref_field, "")) if raw_ref_field else ""
        inst: dict[str, Any] = {
            "object_type": object_type,
            "_source": _source_meta("field_map", source_ref, raw_ref=raw_ref),
        }
        for src_field, target_attr in field_map.items():
            if valid_attrs and target_attr not in valid_attrs:
                continue
            if src_field in rec:
                val = _clean_value(rec[src_field])
                if val is not None:
                    inst[target_attr] = val
        instances.append(inst)
    return instances


def _coerce_value(value: Any, data_type: str) -> Any:
    if value is None or value == "":
        return None
    try:
        if data_type == "integer":
            return int(value)
        if data_type == "float":
            return float(value)
        if data_type == "boolean":
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("true", "1", "yes", "是"):
                return True
            if str(value).lower() in ("false", "0", "no", "否"):
                return False
            raise ValueError
        if data_type == "date":
            return str(value)
        return str(value)
    except (ValueError, TypeError) as exc:
        raise ApiError("VALIDATION_ERROR", f"值 '{value}' 无法转换为类型 {data_type}", http_status=422) from exc


def _validate_record_against_object(record: dict[str, Any], field_map: dict[str, str], obj: dict[str, Any]) -> list[str]:
    """校验源记录是否满足对象属性约束（必填 + 类型），返回 violations 列表；preview 阶段不强制阻断。"""
    errors: list[str] = []
    for attr in obj.get("attributes", []):
        attr_id = attr["id"]
        src_field = next((s for s, t in field_map.items() if t == attr_id), None)
        value = record.get(src_field) if src_field else None
        if attr.get("required") and (value is None or value == ""):
            errors.append(f"属性 {attr.get('name') or attr_id} 必填，但源记录缺少映射字段")
            continue
        if value is not None and value != "":
            try:
                _coerce_value(value, attr.get("data_type", "string"))
            except ApiError as e:
                errors.append(f"属性 {attr.get('name') or attr_id} 类型错误: {e.message}")
    return errors


def _preview_field_map(payload, ontology: dict[str, Any]) -> dict[str, Any]:
    object_type = _resolve_object_type(payload, ontology)
    mapping = payload.mapping or {}
    field_map: dict[str, str] = mapping.get("field_map") or {}
    if not field_map:
        raise ApiError(
            "VALIDATION_ERROR",
            "mapping.field_map 不能为空：请提供 {源表头: 属性id}；源表头与属性 id 相同时可用 identity map"
            '（如 {"orderCode": "orderCode"}）。注意 preview 端点必须显式传 field_map；'
            "若走 upload 端点可不传（按表头自动匹配）",
            http_status=422,
            message_zh="预览接口需要告诉系统「源数据的每一列对应模型哪个属性」，本次请求没有提供这份对应关系",
            suggestion=(
                "curl -X POST '/api/ingest/preview?project_id=<项目>' -H 'Content-Type: application/json' -d "
                '\'{"ontology_id":"<本体id>","object_type":"<对象id>",'
                '"source":{"kind":"field_map","records":[{"orderCode":"O-1"}]},'
                '"mapping":{"field_map":{"orderCode":"orderCode"}}}\''
            ),
        )
    records = payload.source.records
    if not records:
        raise ApiError("VALIDATION_ERROR", "field_map 来源必须提供 source.records", http_status=422)

    obj = next((o for o in ontology.get("objects", []) if o["id"] == object_type), None)
    if obj is None:
        raise ApiError("VALIDATION_ERROR", f"对象类型 {object_type} 不存在", http_status=422)
    # 映射目标允许 属性 ID 或 关系 ID（关系引用值按 rdf_sync 约定存于关系 ID 键）
    valid_attrs = {a["id"] for a in obj.get("attributes", [])} | {r["id"] for r in obj.get("relations", [])}
    for target in field_map.values():
        if target not in valid_attrs:
            raise ApiError(
                "VALIDATION_ERROR",
                f"field_map 目标字段 {target} 不在对象 {object_type} 的属性/关系字段列表中"
                "（若未显式指定 object_type，系统会回退到本体首个对象，可能并非你的目标对象）",
                http_status=422,
                suggestion="在请求 body 加 \"object_type\":\"<对象id>\"，或对 preview 端点用查询参数 ?object_type=<对象id> 显式指定",
            )

    preview_violations: list[str] = []
    for i, rec in enumerate(records):
        record_errors = _validate_record_against_object(rec, field_map, obj)
        for err in record_errors:
            preview_violations.append(f"记录 #{i}: {err}")

    instances = apply_field_map(records, field_map, object_type, mapping.get("source_ref", ""), valid_attrs)

    return {
        "ontology_id": payload.ontology_id,
        "source_type": "field_map",
        "source_ref": mapping.get("source_ref", ""),
        "object_type": object_type,
        "instances": instances,
        "violations": preview_violations,
    }


def approve(candidate: dict[str, Any], ontology: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
    """审核通过：SHACL 校验 → 落库。返回 ApproveResult 结构。"""
    if candidate.get("status") == "imported":
        return candidate.get("approve_result", {"imported": 0, "rejected": 0, "violations": [], "neo_tx_id": None})
    if candidate.get("status") == "rejected":
        raise ApiError("VALIDATION_ERROR", "已拒绝的候选不能重新审批", http_status=422)

    instances = candidate.get("instances", [])
    object_type = candidate.get("object_type")
    if not instances:
        raise ApiError("VALIDATION_ERROR", "候选实例为空", http_status=422)
    if not object_type:
        raise ApiError("VALIDATION_ERROR", "候选缺少 object_type", http_status=422)

    shapes = translate_to_shacl(ontology)
    # GAP-19：大候选 SHACL 校验并行分块——pyshacl 对全部实例×形状单次校验是 CPU 长尾
    #（数千实例候选可达分钟级）；按 _APPROVE_SHACL_CHUNK 分块后线程池并行，
    # 违规按块序拼接（与全量校验同口径）。小候选走原单次路径。
    _CHUNK = _APPROVE_SHACL_CHUNK
    if len(instances) > _CHUNK:
        chunks = [instances[i:i + _CHUNK] for i in range(0, len(instances), _CHUNK)]

        def _validate_chunk(chunk):
            return shacl_validate(build_instance_graph(ontology, chunk), shapes)

        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as ex:
            chunk_results = list(ex.map(_validate_chunk, chunks))
        if any(r is None for r in chunk_results):
            raise ApiError("INTERNAL_ERROR", "SHACL 校验引擎不可用", http_status=500)
        merged_violations: list[dict[str, Any]] = []
        for r in chunk_results:
            merged_violations.extend(r.get("violations") or [])
        shacl_result = {
            "valid": all(r.get("valid") for r in chunk_results),
            "violations": merged_violations,
            "engine": chunk_results[0].get("engine"),
            "degraded": any(r.get("degraded") for r in chunk_results),
            "degrade_reason": next((r.get("degrade_reason") for r in chunk_results
                                    if r.get("degrade_reason")), None),
            "chunked": True,
            "chunks": len(chunks),
        }
    else:
        data = build_instance_graph(ontology, instances)
        shacl_result = shacl_validate(data, shapes)

    if shacl_result is None:
        raise ApiError("INTERNAL_ERROR", "SHACL 校验引擎不可用", http_status=500)

    if not shacl_result["valid"]:
        from model_gap import analyze_model_gaps  # GAP-05：违规→结构化模型缺口
        return {
            "imported": 0,
            "rejected": 0,
            "violations": shacl_result["violations"],
            "model_gaps": analyze_model_gaps(ontology, object_type,
                                             shacl_result["violations"], instances),
            "neo_tx_id": None,
        }

    # HF-03：sh:uniqueKey 非标准 SHACL 约束组件，pyshacl 不执行；复合唯一在业务层兜底。
    # 仅当存在 scope 匹配当前对象且 params.attributes 的复合约束时才生效，
    # 历史单属性 unique 用例与其他对象类型不受影响。
    composite: list[dict[str, Any]] = []
    has_composite = any(
        isinstance(c, dict) and c.get("rule") == "unique"
        and c.get("scope") == object_type
        and (c.get("params") or {}).get("attributes")
        for c in (ontology.get("constraints") or [])
    )
    if has_composite:
        composite = composite_unique_violations(
            ontology, instances, object_type,
            existing=_existing_records_for_composite(candidate, project_id),
        )
    if composite:
        return {
            "imported": 0,
            "rejected": 0,
            "violations": composite,
            "neo_tx_id": None,
        }

    # SHACL 通过 → 落库（Neo4j 或本地 JSONL 降级）
    from neo4j_sync import Neo4jSync

    sync = Neo4jSync()
    instance_source = instances[0].get("_source") if instances else None
    if not instance_source:
        instance_source = _source_meta(candidate.get("source_type", "internal"), candidate.get("source_ref", ""))

    tx_id = sync.write_instances(
        object_type, instances,
        source=instance_source,
        ontology_id=candidate.get("ontology_id"),
        project_id=project_id or candidate.get("project_id"),
    )
    # O-13: 关系物化建边
    relation_tx_id = sync.write_relations(
        object_type, instances, ontology,
        ontology_id=candidate.get("ontology_id"),
        project_id=project_id or candidate.get("project_id"),
    )
    # GAP-15：落库后自动 RDF 同步（本地 TTL 始终写＋Fuseki 可达时双写），
    # 导入审批后图谱即时反映最新数据，无需手动 POST /rdf-sync；失败仅告警不阻断审批结果
    try:
        from jena_adapter import rdf_sync as _rdf_sync

        _rdf_sync.sync_ontology(project_id or candidate.get("project_id"), candidate.get("ontology_id"))
    except Exception as e:  # noqa: BLE001
        logger.warning("approve 后 RDF 同步失败 ontology_id=%s: %s", candidate.get("ontology_id"), e)
    return {
        "imported": len(instances),
        "rejected": 0,
        "violations": [],
        "neo_tx_id": tx_id,
        "relation_tx_id": relation_tx_id,
    }


# ---------------------------------------------------------------------------
# HF-01 / HF-03：实例更新校验（复用 approve 的 SHACL 三段式，不改 approve 返回结构）
# ---------------------------------------------------------------------------

_PLATFORM_INSTANCE_KEYS = {
    "object_type", "_source", "__capability__", "id", "updated_at",
    "imported_at", "ontology_id", "project_id", "key_migrated_at",
}


def _object_of(ontology: dict[str, Any], object_type: str) -> dict[str, Any] | None:
    return next((o for o in ontology.get("objects", [])
                 if isinstance(o, dict) and o.get("id") == object_type), None)


def composite_unique_violations(
    ontology: dict[str, Any],
    instances: list[dict[str, Any]],
    object_type: str,
    existing: list[dict[str, Any]] | None = None,
    exclude_id: str | None = None,
) -> list[dict[str, Any]]:
    """按 constraints[].params.attributes 做真正的组合键去重。

    生成器（modeling_translator）已能把 attributes 编译进 sh:uniqueKey RDF list，
    但该谓词不是 SHACL 标准约束组件，pyshacl/Fuseki 不会执行；故在业务层补上。
    """
    constraints = [c for c in (ontology.get("constraints") or [])
                   if isinstance(c, dict) and c.get("rule") == "unique"
                   and c.get("scope") == object_type
                   and (c.get("params") or {}).get("attributes")]
    if not constraints:
        return []
    obj = _object_of(ontology, object_type)
    if not obj:
        return []
    valid_attrs = {a.get("id") for a in obj.get("attributes", []) if isinstance(a, dict)}
    rows: list[tuple[str, dict]] = []
    for src in (existing or []):
        if src.get("object_type") and src.get("object_type") != object_type:
            continue
        if exclude_id and src.get("id") == exclude_id:
            continue
        rows.append((str(src.get("id")), src.get("instance") or {}))
    for i, inst in enumerate(instances or []):
        rows.append((f"new#{i}", inst if isinstance(inst, dict) else {}))

    violations: list[dict[str, Any]] = []
    for cst in constraints:
        attrs = list((cst.get("params") or {}).get("attributes") or [])
        missing = [a for a in attrs if a not in valid_attrs]
        if missing:
            violations.append({
                "severity": "error", "path": ",".join(attrs),
                "message": f"复合唯一约束引用未定义属性: {missing}",
            })
            continue
        seen: dict[tuple, str] = {}
        for rid, inst in rows:
            vals = tuple(inst.get(a) for a in attrs)
            if any(v is None for v in vals):
                continue
            if vals in seen:
                violations.append({
                    "severity": "error", "path": ",".join(attrs), "focus_node": rid,
                    "message": f"复合唯一冲突 ({','.join(attrs)})={vals}，已存在 {seen[vals]}",
                })
            else:
                seen[vals] = rid
    return violations


def validate_instance(
    ontology: dict[str, Any],
    instance: dict[str, Any],
    object_type: str,
    existing: list[dict[str, Any]] | None = None,
    exclude_id: str | None = None,
    strict_fields: bool = False,
) -> dict[str, Any]:
    """实例更新前的统一校验：SHACL + 字段白名单 + 复合唯一。

    返回 {"violations", "warnings", "unknown_fields"}。
    默认对未知字段只告警（兼容模型层派生维度字段）；strict_fields=True 才 422。
    """
    violations: list[dict[str, Any]] = []
    shapes = translate_to_shacl(ontology)
    data = build_instance_graph(ontology, [instance])
    shacl_result = shacl_validate(data, shapes)
    if shacl_result is None:
        violations.append({"severity": "error", "message": "SHACL 校验引擎不可用"})
    elif not shacl_result["valid"]:
        violations.extend(shacl_result["violations"])

    obj = _object_of(ontology, object_type)
    defined = {a.get("id") for a in (obj or {}).get("attributes", []) if isinstance(a, dict)}
    rels = {r.get("id") for r in (obj or {}).get("relations", []) if isinstance(r, dict)}
    allowed = defined | rels | _PLATFORM_INSTANCE_KEYS
    unknown = [k for k in instance if k not in allowed]
    warnings = [f"未知字段 {k}（不在本体属性定义中，已保留）" for k in unknown]
    if strict_fields and unknown:
        violations.append({
            "severity": "error", "message": f"字段白名单拒绝未知字段: {unknown}",
        })
    violations.extend(composite_unique_violations(
        ontology, [instance], object_type, existing=existing, exclude_id=exclude_id,
    ))
    return {"violations": violations, "warnings": warnings, "unknown_fields": unknown}
