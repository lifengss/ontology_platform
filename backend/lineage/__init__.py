"""V2-08 细粒度血缘（M3-B）：实例级 / 字段级血缘统一装配（只读，不写任何存储）。

垂直链六要素：
  1. 来源系统   —— 候选优先：候选 source_type=external_mcp → source_ref 查连接显示名；
                   field_map/file_upload → 文件名；无候选才回退 instance._source
                   （ingestion.apply_field_map 恒写 _source.kind="field_map"，
                     connection_id=文件名或连接id，无法区分文件/MCP，故不可单独采信）
  2. 源字段     —— 候选 mapping.field_map（{源字段: 本体属性id}）反查；
                   field_map_source=dictionary 时附字典术语解析路径
                   （canonical + system_bindings[connection_id]）
  3. 导入批次   —— 候选记录（ingest_candidates：source_type/source_ref/created_at/created_by）
  4. fetched_at —— instance._source.fetched_at（拉取时间）
  5. 转换规则   —— instance.__capability__（M1-B 四要素溯源：能力版本＋输入原始值＋
                   lookup 命中明细）+ 能力记录（执行版本 expression）
  6. 上游实例   —— __capability__.trace.lookups[*].matched_record_id → 实例记录

三通道一致：REST（api/router_lineage.py）、MCP（mcp_server lineage_get）、
前端实例详情面板均调用 build_instance_lineage()，返回同一结构。

联邦读（capability_call inputs 覆盖 / 术语联邦查询）不产生新实例、不落持久化血缘：
  本模块只读现有实例与候选/能力记录，inputs_persisted 由能力层保证恒为 False
  （输入覆盖不落盘），联邦读内容不会出现在任何实例的 _source 或 __capability__ 中。
"""
from __future__ import annotations

from typing import Any, Optional

from core.instance_store import get_instance_store
from core.storage import JsonStore
from logging_setup import get_logger

logger = get_logger("lineage")

_STORES = {
    "ontologies": "ontologies",
    "candidates": "ingest_candidates",
    "connections": "mcp_connections",
    "capabilities": "capabilities",
    "terms": "dictionary_terms",
}


def _store(kind: str, project_id: str) -> JsonStore:
    return JsonStore(_STORES[kind], project_id=project_id)


def _object_def(project_id: str, ontology_id: Optional[str], object_type: Optional[str]) -> dict[str, Any]:
    """按 ontology_id + object_type（id 或 name 均可）取对象定义；失败返回 {}。"""
    if not ontology_id or not object_type:
        return {}
    rec = _store("ontologies", project_id).get(ontology_id)
    if not rec:
        return {}
    for obj in rec.get("objects") or []:
        if obj.get("id") == object_type or obj.get("name") == object_type:
            return obj
    return {}


def _term_index(project_id: str, object_type: str) -> dict[str, dict[str, Any]]:
    """(object_type, property_id) → 词条索引（每属性至多一个词条）。"""
    idx: dict[str, dict[str, Any]] = {}
    if not object_type:
        return idx
    for t in _store("terms", project_id).all():
        if t.get("object_type") and t.get("object_type") != object_type:
            continue
        prop = t.get("property_id")
        if prop:
            idx.setdefault(prop, t)
    return idx


def build_instance_lineage(project_id: str, instance_id: str) -> Optional[dict[str, Any]]:
    """装配实例血缘视图；实例不存在返回 None（调用方决定 404 / 工具错误语义）。"""
    inst_store = get_instance_store(project_id)
    rec = inst_store.get(instance_id)
    if not rec:
        return None

    content = rec.get("instance") or {}
    src = content.get("_source") or {}
    kind = src.get("kind") or "manual"
    conn_id = src.get("connection_id") or None
    otype = rec.get("object_type")
    ontology_id = rec.get("ontology_id")

    # ---------- 3) 导入批次（候选）—— 先于来源系统加载（来源判定候选优先） ---------- #
    candidate = None
    if rec.get("candidate_id"):
        candidate = _store("candidates", project_id).get(rec["candidate_id"])
    batch: Optional[dict[str, Any]] = None
    if candidate:
        batch = {
            "candidate_id": candidate.get("id"),
            "source_type": candidate.get("source_type"),
            "source_ref": candidate.get("source_ref"),
            "object_type": candidate.get("object_type"),
            "status": candidate.get("status"),
            "created_at": candidate.get("created_at"),
            "created_by": candidate.get("created_by"),
            "record_count": len(candidate.get("instances") or []),
        }

    # ---------- 1) 来源系统（候选 source_type 优先，无候选回退 _source） ---------- #
    cand_type = (candidate or {}).get("source_type")
    semantic_kind = kind
    system: Optional[dict[str, Any]] = None
    if cand_type == "external_mcp":
        semantic_kind = "external_mcp"
        cid = candidate.get("source_ref")
        conn = _store("connections", project_id).get(cid) if cid else None
        system = {
            "connection_id": cid,
            "display_name": (conn or {}).get("display_name"),
            "name": (conn or {}).get("name"),
            "origin": (conn or {}).get("origin"),
        }
    elif cand_type in ("field_map", "file_upload"):
        # 上传通道候选恒为 source_type="field_map"（映射式导入），语义来源是文件
        semantic_kind = "file_upload"
        system = {"kind": "file_upload", "source_ref": candidate.get("source_ref")}
    if system is None:
        if conn_id:
            conn = _store("connections", project_id).get(conn_id)
            system = {
                "connection_id": conn_id,
                "display_name": conn.get("display_name") if conn else None,
                "name": conn.get("name") if conn else None,
                "origin": conn.get("origin") if conn else None,
            }
        elif kind == "file_upload":
            system = {"kind": "file_upload", "source_ref": src.get("raw_ref")}
        elif kind == "manual":
            system = {"kind": "manual"}
        else:
            system = {"kind": kind}

    # ---------- 5) 转换规则（公式能力） ---------- #
    cap_meta = content.get("__capability__")
    cap_record: Optional[dict[str, Any]] = None
    transformation: Optional[dict[str, Any]] = None
    output_attr: Optional[str] = None
    expression_at_version: Optional[str] = None
    if cap_meta:
        cap_record = _store("capabilities", project_id).get(cap_meta.get("capability_id") or "")
        cap_record = cap_record or None
        if cap_record:
            output_attr = cap_record.get("output_attribute")
            expression_at_version = cap_record.get("expression")
            ver = cap_meta.get("capability_version")
            for h in cap_record.get("history") or []:
                if h.get("version") == ver:
                    expression_at_version = h.get("expression") or expression_at_version
                    break
        trace = cap_meta.get("trace") or {}
        transformation = {
            "capability_id": cap_meta.get("capability_id"),
            "capability_name": cap_meta.get("capability_name"),
            "capability_version": cap_meta.get("capability_version"),
            "expression": expression_at_version,
            "executed_at": cap_meta.get("executed_at"),
            "execution_channel": cap_meta.get("execution_channel"),
            "trigger_source": cap_meta.get("trigger_source"),
            "execution_status": cap_meta.get("execution_status"),
            "value": cap_meta.get("value"),
            "error_code": cap_meta.get("error_code"),
            "stale": cap_meta.get("stale"),
            "result_freshness": cap_meta.get("result_freshness"),
            "inputs": {
                "source_instance_id": cap_meta.get("source_instance_id"),
                "input_overrides": cap_meta.get("input_overrides"),
                "inputs_persisted": cap_meta.get("inputs_persisted"),
                "data_gaps": cap_meta.get("data_gaps"),
                "used_inputs": trace.get("inputs"),
                "null_inputs": trace.get("null_inputs"),
            },
            "lookups": trace.get("lookups"),
        }
    else:
        trace: dict[str, Any] = {}

    # ---------- 2) 源字段 + 字段级血缘 ---------- #
    mapping = (candidate or {}).get("mapping") or {}
    field_map: dict[str, Any] = mapping.get("field_map") or {}
    field_map_source = mapping.get("field_map_source")
    rev_map: dict[str, list[str]] = {}
    for src_field, attr_id in field_map.items():
        if attr_id:
            rev_map.setdefault(attr_id, []).append(src_field)

    # 字典术语解析路径：仅当映射来源为 dictionary 时标注（确定性口径，不猜测）
    terms = _term_index(project_id, otype) if field_map_source == "dictionary" else {}

    obj_def = _object_def(project_id, ontology_id, otype)
    attr_defs = obj_def.get("attributes") or []
    if attr_defs:
        field_ids = [a.get("id") for a in attr_defs if a.get("id")]
        attr_label = {a.get("id"): a.get("name") for a in attr_defs}
    else:
        # 对象定义缺失（历史数据/对象未建模）：回退实例实际字段
        field_ids = sorted(
            k for k in content.keys()
            if not k.startswith("_") and k not in ("__capability__", "id", "project_id", "ontology_id")
        )
        attr_label = {}

    fields: list[dict[str, Any]] = []
    for attr_id in field_ids:
        entry: dict[str, Any] = {
            "attribute": attr_id,
            "name": attr_label.get(attr_id) or attr_id,
            "via": "direct",
            "source_field": rev_map.get(attr_id),
            "term": None,
            "capability": None,
        }
        if (
            transformation
            and output_attr
            and attr_id == output_attr
            and cap_meta.get("execution_status") == "success"
        ):
            entry["via"] = "capability"
            entry["capability"] = {
                "capability_id": transformation["capability_id"],
                "capability_name": transformation["capability_name"],
                "capability_version": transformation["capability_version"],
                "expression": expression_at_version,
                "executed_at": transformation["executed_at"],
                "value": transformation["value"],
            }
        term = terms.get(attr_id)
        if term:
            binding = (term.get("system_bindings") or {}).get(conn_id or "") if conn_id else None
            entry["term"] = {
                "term_id": term.get("id"),
                "canonical": term.get("canonical"),
                "object_type": term.get("object_type") or otype,
                "property_id": term.get("property_id"),
                "system_field": binding,
                "bound": bool(binding),
            }
        fields.append(entry)

    # ---------- 6) 上游实例（公式 lookup 命中的基线实例） ---------- #
    upstream: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lk in trace.get("lookups") or []:
        mid = lk.get("matched_record_id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        up_rec = inst_store.get(mid)
        up_content = (up_rec or {}).get("instance") or {}
        upstream.append({
            "instance_id": mid,
            "object_type": lk.get("object_type"),
            "name": up_content.get("name") or up_content.get("title") or mid,
            "matched_code": lk.get("matched_code"),
            "attribute": lk.get("attr"),
            "dims": lk.get("dims"),
            "value": lk.get("value"),
            "matched_count": lk.get("matched_count"),
            "selection_rule": lk.get("selection_rule"),
            "via": "capability_lookup",
        })

    return {
        "instance_id": rec.get("id"),
        "object_type": otype,
        "instance": {
            "id": rec.get("id"),
            "ontology_id": ontology_id,
            "object_type": otype,
            "name": content.get("name") or content.get("title"),
            "stable_key": rec.get("stable_key"),
            "stable_key_value": rec.get("stable_key_value"),
            "imported_at": rec.get("imported_at"),
            "updated_at": rec.get("updated_at"),
            "tx_id": rec.get("tx_id"),
        },
        "source": {
            "kind": semantic_kind,
            "system": system,
            "fetched_at": src.get("fetched_at"),
            "raw_ref": src.get("raw_ref"),
            "batch": batch,
        },
        "field_map_source": field_map_source,
        "fields": fields,
        "transformation": transformation,
        "upstream": upstream,
    }
