"""业务实例影响推理（3.1：实例值变化 → 下游业务影响）。

确定性规则引擎（不依赖 LLM），在实例数据上直接推理：

规则 R1（直接引用回溯）：
    对对象 X 存在指向目标对象类型的 many_to_one 关系时，若其他 X 实例的任一字段值
    （关系 ID 字段或普通属性字段，兼容"引用值存于属性 ID"的建模约定）等于目标实例
    的业务主键（stable_key）值，则该实例被"直接影响"（列出命中的字段路径）。

规则 R2（时间链传导）：
    目标实例的某属性为日期值且发生变化（Δ 天）时，对每个直接引用的实例，
    其日期类型属性（属性 type=date 或属性名含 日期/时间/截止/开始/结束/due/date 等语义）
    推断同向平移 Δ 天，标记为"可能受影响"；属性名显式含时间语义的置信度高，否则中等。

仅做一跳传导（可解释优先），输出含推理依据（reason）供前端展示与人工确认。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from logging_setup import get_logger

logger = get_logger("reasoning.impact")

# 属性名含以下片段即视为"时间语义"（用于提高置信度）
DATE_NAME_HINTS = ("日期", "时间", "截止", "开始", "结束", "到期", "due", "date", "time", "deadline")

_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S")


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:  # ISO 8601（含时区/秒）
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _is_date_value(value: Any) -> bool:
    return _parse_date(value) is not None


def _shift_date(value: Any, delta_days: int) -> str:
    d = _parse_date(value)
    if d is None:
        return str(value)
    return (d + timedelta(days=delta_days)).isoformat()


def _date_delta_days(old: Any, new: Any) -> int | None:
    old_d, new_d = _parse_date(old), _parse_date(new)
    if old_d is None or new_d is None:
        return None
    return (new_d - old_d).days


def _instance_data(inst: dict[str, Any]) -> dict[str, Any]:
    return inst.get("instance") or inst.get("props") or inst


def _attr_type(obj_def: dict[str, Any] | None, attr_id: str) -> str:
    if not obj_def:
        return ""
    for a in obj_def.get("attributes") or []:
        if a.get("id") == attr_id:
            return str(a.get("type") or "")
    return ""


def _name_has_time_hint(attr_id: str) -> bool:
    low = str(attr_id).lower()
    return any(h in low for h in DATE_NAME_HINTS)


def _label(inst: dict[str, Any], obj_def: dict[str, Any] | None = None) -> str:
    """实例展示名：业务名称属性（id 含 name）→ 通用键 → stable_key 值 → 实例 id。"""
    data = _instance_data(inst)
    if obj_def:
        for a in obj_def.get("attributes") or []:
            aid = str(a.get("id") or "")
            if "name" in aid and data.get(aid):
                return str(data[aid])
    for key in ("name", "title", "code"):
        if data.get(key):
            return str(data[key])
    sk = (obj_def or {}).get("stable_key")
    if sk and data.get(sk):
        return str(data[sk])
    return str(inst.get("id", ""))


def infer_impact(
    ontology: dict[str, Any],
    instances: list[dict[str, Any]],
    instance_id: str,
    attribute: str,
    new_value: str,
) -> dict[str, Any]:
    """对目标实例的属性值变化做一跳影响推理。

    instances: 实例存储中的记录列表（{id, object_type, instance/props: {...}}）。
    返回 { source, impacted, explanation }。
    """
    target = next((i for i in instances if i.get("id") == instance_id), None)
    if target is None:
        raise KeyError(instance_id)

    target_data = _instance_data(target)
    old_value = target_data.get(attribute)
    delta = _date_delta_days(old_value, new_value)

    objects = ontology.get("objects") or []
    obj_by_id = {o.get("id"): o for o in objects}
    target_obj = obj_by_id.get(target.get("object_type"))
    stable_key = (target_obj or {}).get("stable_key")
    target_key_value = target_data.get(stable_key) if stable_key else None

    impacted: list[dict[str, Any]] = []
    for inst in instances:
        if inst.get("id") == target.get("id"):
            continue
        data = _instance_data(inst)
        inst_obj = obj_by_id.get(inst.get("object_type"))

        # R1 直接引用回溯：X 存在指向目标类型的 N:1 关系（模式门），
        # 且实例任一字段值（关系 ID 字段或普通属性字段）等于目标实例主键值（值匹配）
        ref_paths: list[str] = []
        has_rel_to_target = any(
            r.get("cardinality") == "many_to_one" and r.get("target_object") == target.get("object_type")
            for r in inst_obj.get("relations") or []
        )
        if has_rel_to_target and target_key_value is not None and str(target_key_value) != "":
            own_key = inst_obj.get("stable_key")
            for k, v in data.items():
                if k.startswith("_") or v is None or k == own_key:
                    continue
                if str(v) == str(target_key_value):
                    ref_paths.append(f"{inst.get('object_type')}::{k} → {target.get('object_type')}:{target_key_value}")
        if not ref_paths:
            continue

        # R2 时间链传导
        inferred: list[dict[str, Any]] = []
        if delta is not None and delta != 0:
            for attr_id, val in data.items():
                if str(attr_id).startswith("_") or not _is_date_value(val):
                    continue
                if _attr_type(inst_obj, attr_id) != "date" and not _name_has_time_hint(attr_id):
                    continue
                inferred.append({
                    "attribute": attr_id,
                    "current": val,
                    "inferred": _shift_date(val, delta),
                    "confidence": "high" if _name_has_time_hint(attr_id) else "medium",
                })

        reason = "直接引用（" + "；".join(ref_paths) + "）"
        if delta is not None and delta != 0:
            direction = "推迟" if delta > 0 else "提前"
            reason += f"；{attribute} 由 {old_value} 变为 {new_value}（{direction} {abs(delta)} 天），时间属性联动推断"
        impacted.append({
            "instance_id": inst.get("id"),
            "object_type": inst.get("object_type"),
            "label": _label(inst, inst_obj),
            "direct_reference": ref_paths,
            "inferred_changes": inferred,
            "reason": reason,
        })

    impacted.sort(key=lambda x: (len(x["inferred_changes"]), x["instance_id"]), reverse=True)
    n_direct = len(impacted)
    n_inferred = len([i for i in impacted if i["inferred_changes"]])
    if not impacted:
        explanation = f"实例 {instance_id}（{target.get('object_type')}）的 {attribute} 变化未发现直接引用方，无下游影响。"
    else:
        explanation = (
            f"实例 {instance_id}（{target.get('object_type')}）的 {attribute} 由 {old_value} 变为 {new_value}"
            + (f"（{delta:+d} 天）" if delta is not None else "")
            + f"。共 {n_direct} 个实例直接引用该实例，其中 {n_inferred} 个存在时间属性联动推断（可能受影响，需人工确认）。"
        )

    logger.info("impact_inference instance=%s attr=%s impacted=%d inferred=%d",
                instance_id, attribute, n_direct, n_inferred)
    return {
        "source": {
            "instance_id": instance_id,
            "object_type": target.get("object_type"),
            "label": _label(target, target_obj),
            "attribute": attribute,
            "old_value": old_value,
            "new_value": new_value,
            "delta_days": delta,
        },
        "impacted": impacted,
        "explanation": explanation,
        "rules": [
            "R1 直接引用回溯：N:1 关系属性引用目标实例主键 → 直接影响",
            "R2 时间链传导：目标日期属性变化 Δ 天 → 引用方日期属性推断平移 Δ 天（一跳）",
        ],
    }
