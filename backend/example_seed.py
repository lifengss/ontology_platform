"""GAP-13 示例项目一条龙：为空项目预置示例本体（内置模板）＋少量示例实例。

- 本体：复用内置约束示范模板 `tpl_order_sample`（订单/客户，含 pattern/enum/数值区间/长度区间），
  经 `create_ontology` 走与人工建模完全相同的业务校验/审计/RDF 同步通道，落为 draft。
- 实例：按本体对象确定性生成（每对象默认 3 条）——enum 取枚举值轮换、数值取约束区间内值、
  date 递增、pattern 属性用特化样例表生成合法值；实例经 InstanceStore 直插（与 CSV 入库同构）。
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any, Callable

from templates import BUILTIN_TEMPLATES

EXAMPLE_TEMPLATE_ID = "tpl_order_sample"

# pattern 约束无法通用生成合法值 → 按属性 id 特化（覆盖内置模板全部 pattern 属性）
_PATTERN_SAMPLES: dict[str, Callable[[int], str]] = {
    "attr_order_no": lambda i: f"SO{i + 1:08d}",      # ^[A-Z]{2}[0-9]{8,12}$
    "attr_cust_code": lambda i: f"CUST_{i + 1:03d}",  # ^[A-Z0-9_]+$
    "attr_phone": lambda i: f"138{i + 1:08d}",        # ^1[3-9][0-9]{9}$
}


def _attr_value(attr: dict[str, Any], i: int) -> Any:
    """按属性类型与约束生成第 i 条确定性示例值（必须落在约束区间内）。"""
    c = attr.get("constraints") or {}
    if attr["id"] in _PATTERN_SAMPLES:
        return _PATTERN_SAMPLES[attr["id"]](i)
    if c.get("enum"):
        return c["enum"][i % len(c["enum"])]
    dt = attr.get("data_type") or "string"
    if dt == "integer":
        lo, hi = c.get("minimum"), c.get("maximum")
        v = i + 1
        if lo is not None and v < lo:
            v = int(lo)
        if hi is not None and v > hi:
            v = max(int(lo or 0), int(hi) // 2)
        return v
    if dt == "float":
        lo, hi = c.get("minimum"), c.get("maximum")
        v = round((i + 1) * 128.5, 2)
        if lo is not None and v < lo:
            v = float(lo)
        if hi is not None and v > hi:
            v = round(float(hi) / (i + 2), 2)
        return v
    if dt == "boolean":
        return i % 2 == 0
    if dt == "date":
        return (date(2026, 1, 1) + timedelta(days=i)).isoformat()
    # string：尊重 min_length / max_length
    base = f"示例{attr.get('name') or attr['id']}{i + 1}"
    mn = c.get("min_length")
    if mn and len(base) < mn:
        base += "样" * (mn - len(base))
    mx = c.get("max_length")
    if mx and len(base) > mx:
        base = base[:mx]
    return base


def build_example_instances(ontology: dict[str, Any], per_object: int = 3) -> list[dict[str, Any]]:
    """为本体每个对象生成 per_object 条示例实例记录（与 CSV 入库记录同构）。"""
    records: list[dict[str, Any]] = []
    for obj in ontology.get("objects", []):
        obj_id = obj.get("id")
        if not obj_id:
            continue
        for i in range(max(1, per_object)):
            inst: dict[str, Any] = {
                "id": f"{obj_id}_demo_{i + 1}",
                "name": f"{obj.get('name') or obj_id}示例{i + 1}",
            }
            for attr in obj.get("attributes", []):
                inst[attr["id"]] = _attr_value(attr, i)
            records.append({
                "id": uuid.uuid4().hex,
                "ontology_id": ontology.get("id"),
                "object_type": obj_id,
                "instance": inst,
                "source": {"kind": "example_seed"},
            })
    return records


def find_builtin_template(template_id: str = EXAMPLE_TEMPLATE_ID) -> dict[str, Any] | None:
    for tpl in BUILTIN_TEMPLATES:
        if tpl["id"] == template_id:
            return tpl
    return None
