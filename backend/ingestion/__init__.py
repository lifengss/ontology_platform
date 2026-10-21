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
from datetime import datetime, timezone
from typing import Any

from core.responses import ApiError
from jena_adapter.client import shacl_validate
from logging_setup import get_logger
from modeling_translator import build_instance_graph, translate_to_shacl

logger = get_logger("ingestion")


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
        raise ApiError("VALIDATION_ERROR", "field_map 来源必须提供 mapping.field_map", http_status=422)
    records = payload.source.records
    if not records:
        raise ApiError("VALIDATION_ERROR", "field_map 来源必须提供 source.records", http_status=422)

    obj = next((o for o in ontology.get("objects", []) if o["id"] == object_type), None)
    if obj is None:
        raise ApiError("VALIDATION_ERROR", f"对象类型 {object_type} 不存在", http_status=422)
    valid_attrs = {a["id"] for a in obj.get("attributes", [])}
    for target in field_map.values():
        if target not in valid_attrs:
            raise ApiError("VALIDATION_ERROR", f"field_map 目标字段 {target} 不在对象 {object_type} 的属性列表中", http_status=422)

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
    data = build_instance_graph(ontology, instances)
    shacl_result = shacl_validate(data, shapes)

    if shacl_result is None:
        raise ApiError("INTERNAL_ERROR", "SHACL 校验引擎不可用", http_status=500)

    if not shacl_result["valid"]:
        return {
            "imported": 0,
            "rejected": 0,
            "violations": shacl_result["violations"],
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
    return {
        "imported": len(instances),
        "rejected": 0,
        "violations": [],
        "neo_tx_id": tx_id,
        "relation_tx_id": relation_tx_id,
    }
