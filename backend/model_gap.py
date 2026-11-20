"""GAP-05：导入校验失败的结构化模型缺口 + 一键修复（兼容变更）。

缺口类型（gap["kind"]）：
- missing_attribute   缺字段：数据里有模型没有的列（上传表头未匹配）
- enum_value_missing  枚举缺值：数据取值不在枚举内（SHACL sh:in 违规）
- constraint_too_strict 约束过紧：required/pattern/长度/数值区间与数据不符
- type_mismatch       类型不符：取值无法按属性 data_type 解析（需人工处理）
- unknown             其他（对象级 SHACL 违规等，需人工处理）

一键修复只执行【兼容变更】（与 schemadiff 判定一致）：
新增可选属性 / 补枚举值 / 解除约束（pattern、长度、数值区间、required）
→ 经 changesets 兼容变更管线自动升小版本并直接发布；
类型变更等不兼容项不自动执行，标记 auto_fixable=False 提示人工处理。
"""
from __future__ import annotations

import copy
import re
from typing import Any, Optional

from modeling_translator import _local  # 与 SHACL 违规 path 的缩写规则保持一致

GAP_KINDS = (
    "missing_attribute",
    "enum_value_missing",
    "constraint_too_strict",
    "type_mismatch",
    "unknown",
)

# 修复动作 → 中文描述（前端展示）
FIX_ACTION_LABELS = {
    "add_attribute": "新增可选属性",
    "add_enum_values": "补枚举值",
    "remove_pattern": "解除正则约束",
    "relax_max_length": "上调最大长度",
    "relax_min_length": "下调最小长度",
    "relax_maximum": "上调最大值",
    "relax_minimum": "下调最小值",
    "unset_required": "必填改为可选",
}


def _infer_dtype(values: list[Any]) -> str:
    """复用 GAP-04 宽表列类型推断（router_ontologies._infer_column_dtype）。"""
    try:
        from api.router_ontologies import _infer_column_dtype
        return _infer_column_dtype([str(v) for v in values])
    except Exception:
        return "string"


def _slug_attr_id(name: str, existing: set[str]) -> str:
    base = "".join(ch for ch in re.sub(r"[^0-9A-Za-z_]+", "_", (name or "").strip()) if ch.isalnum() or ch == "_")
    base = base or "attr"
    if base[0].isdigit():
        base = "a_" + base
    if base not in existing:
        return base
    i = 1
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. 缺字段（上传表头未匹配）
# ---------------------------------------------------------------------------

def missing_attribute_gaps(headers: list[str], records: list[dict], obj: dict) -> list[dict]:
    """headers：未匹配到任何属性的表头列表；records：原始行（键=表头）。

    返回 missing_attribute 缺口（含推断类型，供一键新增可选属性）。
    """
    attrs = obj.get("attributes") or []
    known_names = {a.get("name") for a in attrs}
    gaps: list[dict] = []
    for h in headers:
        if not h or h in known_names:
            continue
        samples = [str(r.get(h)) for r in records if r.get(h) not in (None, "")][:200]
        dtype = _infer_dtype(samples) if samples else "string"
        gaps.append({
            "kind": "missing_attribute",
            "attribute_id": None,
            "attribute_name": h,
            "detail": f"数据列「{h}」在本体中不存在（按样本推断类型为 {dtype}），该列数据不会被导入",
            "auto_fixable": True,
            "fix": {"action": "add_attribute",
                    "params": {"name": h, "data_type": dtype, "required": False}},
        })
    return gaps


# ---------------------------------------------------------------------------
# 2. SHACL 违规 → 结构化缺口
# ---------------------------------------------------------------------------

def analyze_model_gaps(ontology: dict, object_type: str,
                       violations: list[dict],
                       instances: Optional[list[dict]] = None) -> list[dict]:
    """把 SHACL 门禁违规（+ 候选实例样本）归纳为结构化模型缺口。

    violations: jena_adapter 违规 {severity, path, focus_node, message}，path 为属性 IRI 缩写（= _local(attr_id)）。
    instances:  候选实例（键=属性 id），用于定位具体违规取值与放宽幅度。
    """
    obj = next((o for o in (ontology.get("objects") or []) if o.get("id") == object_type), None)
    if not obj:
        return []
    attrs = obj.get("attributes") or []
    local_map = {_local(a.get("id") or ""): a for a in attrs if a.get("id")}
    instances = instances or []

    violated_attrs: dict[str, list[dict]] = {}  # attr id -> 相关违规
    unknown: dict[str, str] = {}                # path -> 示例 message
    for v in violations or []:
        path = (v.get("path") or "").strip()
        a = local_map.get(path)
        if a:
            violated_attrs.setdefault(a["id"], []).append(v)
        elif path:
            unknown.setdefault(path, v.get("message") or "")

    gaps: list[dict] = []
    seen: set[tuple] = set()

    def _add(gap: dict) -> None:
        key = (gap["kind"], gap.get("attribute_id"), (gap.get("fix") or {}).get("action") if gap.get("fix") else None)
        if key in seen:
            return
        seen.add(key)
        gaps.append(gap)

    for attr_id, vlist in violated_attrs.items():
        attr = local_map[_local(attr_id)]
        name = attr.get("name") or attr_id
        c = attr.get("constraints") or {}
        obs = [inst.get(attr_id) for inst in instances if isinstance(inst, dict)]
        non_empty = [v for v in obs if v not in (None, "")]
        messages = "；".join(m for m in (v.get("message") or "" for v in vlist) if m)[:200]

        # 枚举缺值
        if isinstance(c.get("enum"), list) and c.get("enum"):
            allowed = {str(e) for e in c["enum"]}
            bad = [v for v in non_empty if str(v) not in allowed]
            if bad:
                values = sorted({str(v) for v in bad})[:20]
                _add({
                    "kind": "enum_value_missing",
                    "attribute_id": attr_id,
                    "attribute_name": name,
                    "detail": f"属性「{name}」出现枚举外取值：{'、'.join(values)}",
                    "auto_fixable": True,
                    "fix": {"action": "add_enum_values", "params": {"values": values}},
                    "values": values,
                })

        # 必填过严
        if attr.get("required") and any(v in (None, "") for v in obs):
            _add({
                "kind": "constraint_too_strict",
                "attribute_id": attr_id,
                "attribute_name": name,
                "detail": f"属性「{name}」为必填，但存在空值实例",
                "auto_fixable": True,
                "fix": {"action": "unset_required", "params": {}},
            })

        # 正则过紧
        if c.get("pattern") and non_empty:
            try:
                pat = re.compile(c["pattern"])
            except re.error:
                pat = None
            bad = [v for v in non_empty if pat is None or not pat.search(str(v))]
            if bad:
                _add({
                    "kind": "constraint_too_strict",
                    "attribute_id": attr_id,
                    "attribute_name": name,
                    "detail": f"属性「{name}」正则 {c['pattern']!r} 无法覆盖数据（如 {bad[0]!r}）",
                    "auto_fixable": True,
                    "fix": {"action": "remove_pattern", "params": {}},
                })

        # 长度上下限
        if isinstance(c.get("max_length"), (int, float)) and non_empty:
            maxlen = max(len(str(v)) for v in non_empty)
            if maxlen > c["max_length"]:
                _add({
                    "kind": "constraint_too_strict",
                    "attribute_id": attr_id,
                    "attribute_name": name,
                    "detail": f"属性「{name}」最大长度 {c['max_length']} 小于数据实际 {maxlen}",
                    "auto_fixable": True,
                    "fix": {"action": "relax_max_length", "params": {"max_length": int(maxlen)}},
                })
        if isinstance(c.get("min_length"), (int, float)) and non_empty:
            minlen = min(len(str(v)) for v in non_empty)
            if minlen < c["min_length"]:
                _add({
                    "kind": "constraint_too_strict",
                    "attribute_id": attr_id,
                    "attribute_name": name,
                    "detail": f"属性「{name}」最小长度 {c['min_length']} 大于数据实际 {minlen}",
                    "auto_fixable": True,
                    "fix": {"action": "relax_min_length", "params": {"min_length": int(minlen)}},
                })

        # 数值区间
        nums = [n for n in (_num(v) for v in non_empty) if n is not None]
        if isinstance(c.get("maximum"), (int, float)) and nums and max(nums) > c["maximum"]:
            _add({
                "kind": "constraint_too_strict",
                "attribute_id": attr_id,
                "attribute_name": name,
                "detail": f"属性「{name}」最大值 {c['maximum']} 小于数据实际 {max(nums):g}",
                "auto_fixable": True,
                "fix": {"action": "relax_maximum", "params": {"maximum": max(nums)}},
            })
        if isinstance(c.get("minimum"), (int, float)) and nums and min(nums) < c["minimum"]:
            _add({
                "kind": "constraint_too_strict",
                "attribute_id": attr_id,
                "attribute_name": name,
                "detail": f"属性「{name}」最小值 {c['minimum']} 大于数据实际 {min(nums):g}",
                "auto_fixable": True,
                "fix": {"action": "relax_minimum", "params": {"minimum": min(nums)}},
            })

        # 其余违规（多为类型不符或无法自动放宽）→ 人工处理
        if not any(g.get("attribute_id") == attr_id for g in gaps):
            _add({
                "kind": "type_mismatch",
                "attribute_id": attr_id,
                "attribute_name": name,
                "detail": f"属性「{name}」取值与类型 {attr.get('data_type')} 不符（{messages}）；"
                          f"类型变更属不兼容变更，需人工在本体建模页处理或升大版本审批",
                "auto_fixable": False,
                "fix": None,
            })

    for path, msg in unknown.items():
        _add({
            "kind": "unknown",
            "attribute_id": None,
            "attribute_name": path,
            "detail": f"对象级/未识别违规（{path}）：{msg[:120]}",
            "auto_fixable": False,
            "fix": None,
        })
    return gaps


# ---------------------------------------------------------------------------
# 3. 缺口 → 模型（仅兼容修复）
# ---------------------------------------------------------------------------

def apply_gaps_to_model(model: dict, object_type: str, gaps: list[dict]):
    """把可自动修复的缺口应用到模型副本（只产生兼容变更）。

    返回 (new_model, applied, skipped)：
    - new_model: 修复后的完整模型（dict，含 name/objects/constraints/functions 等）
    - applied:   已执行的修复说明（list[str]）
    - skipped:   未执行项（[{attribute_name, reason}]，含不兼容与已生效）
    """
    new_model = copy.deepcopy(dict(model or {}))
    obj = next((o for o in (new_model.get("objects") or []) if o.get("id") == object_type), None)
    applied: list[str] = []
    skipped: list[dict] = []
    if obj is None:
        return new_model, applied, [{"attribute_name": object_type, "reason": "对象类型不存在"}]

    attrs: list[dict] = obj.setdefault("attributes", [])

    for gap in gaps or []:
        fix = gap.get("fix") or {}
        action = fix.get("action")
        label = gap.get("attribute_name") or (gap.get("attribute_id") or "")
        if not gap.get("auto_fixable") or not action:
            skipped.append({"attribute_name": label,
                            "reason": gap.get("detail") or "需人工处理"})
            continue

        if action == "add_attribute":
            p = fix.get("params") or {}
            name = p.get("name") or label
            if any(a.get("name") == name for a in attrs):
                skipped.append({"attribute_name": label, "reason": "同名属性已存在"})
                continue
            aid = _slug_attr_id(name, {a.get("id") for a in attrs})
            attrs.append({"id": aid, "name": name,
                          "data_type": p.get("data_type") or "string",
                          "required": False, "constraints": {}})
            applied.append(f"新增可选属性「{name}」({p.get('data_type') or 'string'})")
            continue

        attr = next((a for a in attrs if a.get("id") == gap.get("attribute_id")
                     or a.get("name") == label), None)
        if attr is None:
            skipped.append({"attribute_name": label, "reason": "属性未找到"})
            continue
        c = attr.setdefault("constraints", {})
        p = fix.get("params") or {}

        if action == "add_enum_values":
            existing = c.get("enum")
            existing = existing if isinstance(existing, list) else []
            have = {str(e) for e in existing}
            newv = [v for v in (p.get("values") or []) if str(v) not in have]
            if not newv:
                skipped.append({"attribute_name": label, "reason": "枚举值已存在"})
                continue
            c["enum"] = existing + newv
            applied.append(f"「{label}」补枚举值 {'、'.join(str(v) for v in newv)}")
        elif action == "remove_pattern":
            if not c.get("pattern"):
                skipped.append({"attribute_name": label, "reason": "已无正则约束"})
                continue
            c.pop("pattern", None)
            applied.append(f"「{label}」解除正则约束")
        elif action == "unset_required":
            if not attr.get("required"):
                skipped.append({"attribute_name": label, "reason": "已是可选属性"})
                continue
            attr["required"] = False
            applied.append(f"「{label}」必填改为可选")
        elif action in ("relax_max_length", "relax_minimum"):
            key = "max_length" if action == "relax_max_length" else "minimum"
            cur, new = c.get(key), p.get(key)
            if new is None or not isinstance(cur, (int, float)) or new <= cur:
                skipped.append({"attribute_name": label, "reason": "约束已满足或无法兼容放宽"})
                continue
            c[key] = new
            applied.append(f"「{label}」{'最大长度' if key == 'max_length' else '最小值'} {cur} → {new:g}")
        elif action in ("relax_min_length", "relax_maximum"):
            key = "min_length" if action == "relax_min_length" else "maximum"
            cur, new = c.get(key), p.get(key)
            if new is None or not isinstance(cur, (int, float)) or new >= cur:
                skipped.append({"attribute_name": label, "reason": "约束已满足或无法兼容放宽"})
                continue
            c[key] = new
            applied.append(f"「{label}」{'最小长度' if key == 'min_length' else '最大值'} {cur} → {new:g}")
        else:
            skipped.append({"attribute_name": label, "reason": f"未知修复动作 {action}"})

    return new_model, applied, skipped
