"""V2-07 schema 语义 diff（M2-E）：变更兼容性判定。

规则（与 docs/V2-测试用例.md 八、TC-07-04 对齐）：
- **兼容（升小版本，直接发布）**：新增对象；新增*可选*属性；取值范围放宽
  （下限降低/上限抬高/枚举增补/约束移除）；必填放宽；新增关系/函数/业务约束的移除。
- **不兼容（须升大版本＋审批）**：删除对象；删除属性；属性类型变化；
  主键(stable_key)变化；父对象变化；必填收紧；取值范围收窄（含枚举删值/新增下限）；
  删除关系；新增/修改业务约束（可能使存量实例违规）；删除函数。

版本表示："M" 或 "M.m"，统一规范化为 "M.m"。
"""
from __future__ import annotations

import re
from typing import Any

_VER_RE = re.compile(r"^(\d+)(?:\.(\d+))?$")

# 约束字段 → (方向, kind 放宽, kind 收窄)：lower=下限（越小越宽），upper=上限（越大越宽）
_CONSTRAINT_FIELDS = (
    ("minimum", "lower"),
    ("min_length", "lower"),
    ("maximum", "upper"),
    ("max_length", "upper"),
    ("enum", "enum"),
    ("pattern", "pattern"),
)


class VersionError(ValueError):
    pass


def parse_version(v: str | int | None) -> tuple[int, int]:
    s = str(v or "1").strip()
    m = _VER_RE.match(s)
    if not m:
        raise VersionError(f"非法版本号: {v!r}（应为 M 或 M.m）")
    return int(m.group(1)), int(m.group(2) or 0)


def fmt_version(major: int, minor: int = 0) -> str:
    return f"{major}.{minor}"


def norm_version(v: str | int | None) -> str:
    major, minor = parse_version(v)
    return fmt_version(major, minor)


def bump_minor(v: str | int | None) -> str:
    major, minor = parse_version(v)
    return fmt_version(major, minor + 1)


def bump_major(v: str | int | None) -> str:
    major, _ = parse_version(v)
    return fmt_version(major + 1, 0)


def _ch(kind: str, compatible: bool, detail: str = "", **ids: Any) -> dict[str, Any]:
    c: dict[str, Any] = {"kind": kind, "compatible": compatible, "detail": detail}
    for k, val in ids.items():
        if val:
            c[k] = val
    return c


def _constraints_of(attr: dict[str, Any]) -> dict[str, Any]:
    c = attr.get("constraints") or {}
    return {k: c.get(k) for k, _ in _CONSTRAINT_FIELDS}


def _diff_constraints(old_a: dict[str, Any], new_a: dict[str, Any], object_id: str,
                      attribute_id: str) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    co = _constraints_of(old_a)
    cn = _constraints_of(new_a)
    for key, direction in _CONSTRAINT_FIELDS:
        if key not in co and key not in cn:
            continue
        o, n = co.get(key), cn.get(key)
        if o == n:
            continue
        if direction == "enum":
            so = set(map(str, o if isinstance(o, list) else []))
            sn = set(map(str, n if isinstance(n, list) else []))
            if sn - so:
                changes.append(_ch("constraint_widened", True,
                                   f"枚举增补 {sorted(sn - so)}", object_id=object_id,
                                   attribute_id=attribute_id))
            if so - sn:
                changes.append(_ch("constraint_narrowed", False,
                                   f"枚举删值 {sorted(so - sn)}", object_id=object_id,
                                   attribute_id=attribute_id))
            continue
        if direction == "pattern":
            # 正则无法安全判定收窄方向：保守视为不兼容
            changes.append(_ch("constraint_narrowed", False,
                               f"pattern 变化 {o!r} → {n!r}", object_id=object_id,
                               attribute_id=attribute_id))
            continue
        # lower / upper
        if n is None:
            compat, kind = True, "constraint_widened"  # 解除界限 = 放宽
            detail = f"{key} 解除（{o} → 无限制）"
        elif o is None:
            compat, kind = False, "constraint_narrowed"  # 新增界限 = 收窄
            detail = f"{key} 新增 {n}"
        else:
            if direction == "lower":
                compat = n <= o
            else:
                compat = n >= o
            kind = "constraint_widened" if compat else "constraint_narrowed"
            detail = f"{key}: {o} → {n}"
        changes.append(_ch(kind, compat, detail, object_id=object_id, attribute_id=attribute_id))
    return changes


def _diff_attribute(old_a: dict[str, Any], new_a: dict[str, Any], object_id: str) -> list[dict[str, Any]]:
    aid = new_a.get("id") or old_a.get("id")
    changes: list[dict[str, Any]] = []
    if old_a.get("data_type") != new_a.get("data_type"):
        changes.append(_ch("attribute_type_changed", False,
                           f"{old_a.get('data_type')} → {new_a.get('data_type')}",
                           object_id=object_id, attribute_id=aid))
    req_o = bool(old_a.get("required"))
    req_n = bool(new_a.get("required"))
    if req_o != req_n:
        # 放宽（required→optional）= 兼容；收紧 = 不兼容
        changes.append(_ch("attribute_required_changed", not req_n,
                           "必填→可选" if not req_n else "可选→必填",
                           object_id=object_id, attribute_id=aid))
    changes.extend(_diff_constraints(old_a, new_a, object_id, aid))
    return changes


def _attrs_of(obj: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {a.get("id"): a for a in (obj.get("attributes") or []) if a.get("id")}


def _rels_of(obj: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r.get("id"): r for r in (obj.get("relations") or []) if r.get("id")}


def diff_ontology(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """语义 diff：返回 {compatible, version_bump, changes[]}。空变更视为兼容。"""
    changes: list[dict[str, Any]] = []
    old_objs = {o.get("id"): o for o in (old.get("objects") or []) if o.get("id")}
    new_objs = {o.get("id"): o for o in (new.get("objects") or []) if o.get("id")}

    for oid, o in new_objs.items():
        if oid not in old_objs:
            changes.append(_ch("object_added", True, f"新增对象 {o.get('name') or oid}", object_id=oid))
    for oid, o in old_objs.items():
        if oid not in new_objs:
            changes.append(_ch("object_removed", False, f"删除对象 {o.get('name') or oid}", object_id=oid))

    for oid, o_old in old_objs.items():
        o_new = new_objs.get(oid)
        if o_new is None:
            continue
        if (o_old.get("stable_key") or None) != (o_new.get("stable_key") or None):
            changes.append(_ch("stable_key_changed", False,
                               f"{o_old.get('stable_key')!r} → {o_new.get('stable_key')!r}",
                               object_id=oid))
        if (o_old.get("parent") or None) != (o_new.get("parent") or None):
            changes.append(_ch("parent_changed", False,
                               f"{o_old.get('parent')!r} → {o_new.get('parent')!r}", object_id=oid))
        # 属性
        ao, an = _attrs_of(o_old), _attrs_of(o_new)
        for aid, a in an.items():
            if aid not in ao:
                # 新增必填属性 = 收窄（存量实例无法满足）；可选 = 兼容
                compat = not a.get("required")
                changes.append(_ch("attribute_added", compat,
                                   "新增必填属性" if not compat else "新增可选属性",
                                   object_id=oid, attribute_id=aid))
        for aid, a in ao.items():
            if aid not in an:
                changes.append(_ch("attribute_removed", False,
                                   f"删除属性 {a.get('data_type') or ''}".strip(),
                                   object_id=oid, attribute_id=aid))
        for aid in set(ao) & set(an):
            changes.extend(_diff_attribute(ao[aid], an[aid], oid))
        # 关系
        ro, rn = _rels_of(o_old), _rels_of(o_new)
        for rid, r in rn.items():
            if rid not in ro:
                changes.append(_ch("relation_added", True,
                                   f"新增关系 → {r.get('target_object')}", object_id=oid, relation_id=rid))
        for rid, r in ro.items():
            if rid not in rn:
                changes.append(_ch("relation_removed", False,
                                   f"删除关系 → {r.get('target_object')}", object_id=oid, relation_id=rid))
        for rid in set(ro) & set(rn):
            if ro[rid] != rn[rid]:
                changes.append(_ch("relation_changed", False, "关系定义变化",
                                   object_id=oid, relation_id=rid))

    # 业务约束：新增/修改 = 可能使存量实例违规 → 不兼容；移除 = 兼容
    co = {c.get("id"): c for c in (old.get("constraints") or []) if c.get("id")}
    cn = {c.get("id"): c for c in (new.get("constraints") or []) if c.get("id")}
    for cid, c in cn.items():
        if cid not in co:
            changes.append(_ch("constraint_added", False, f"新增业务约束 {c.get('name') or cid}",
                               constraint_id=cid))
        elif co[cid] != c:
            changes.append(_ch("constraint_modified", False,
                               f"业务约束修改 {c.get('name') or cid}", constraint_id=cid))
    for cid, c in co.items():
        if cid not in cn:
            changes.append(_ch("constraint_removed", True, f"移除业务约束 {c.get('name') or cid}",
                               constraint_id=cid))

    # 函数（元模型预留）
    fo = {f.get("function_id") or f.get("name"): f for f in (old.get("functions") or [])}
    fn = {f.get("function_id") or f.get("name"): f for f in (new.get("functions") or [])}
    for fid, f in fn.items():
        if fid not in fo:
            changes.append(_ch("function_added", True, f"新增函数 {f.get('name') or fid}", function_id=fid))
        elif fo[fid] != f:
            changes.append(_ch("function_modified", False, f"函数修改 {f.get('name') or fid}", function_id=fid))
    for fid, f in fo.items():
        if fid not in fn:
            changes.append(_ch("function_removed", False, f"删除函数 {f.get('name') or fid}", function_id=fid))

    compatible = all(c["compatible"] for c in changes)
    return {
        "compatible": compatible,
        "version_bump": "minor" if compatible else "major",
        "changes": changes,
    }
