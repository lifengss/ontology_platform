"""VS2 补丁（P-01/P-03）测试：业务层轻量校验门 + enum 约束（sh:in）
+ SHACL 属性形状 URI 按对象隔离（跨对象复用属性 ID 不再合并约束）。"""
import pytest
from rdflib import BNode, Graph, SH, URIRef

from modeling_translator import (
    assert_shapes_well_formed,
    blocking_violations,
    build_instance_graph,
    translate_to_shacl,
    translate_to_turtle,
    validate_business_layer,
)
from jena_adapter.client import shacl_validate


def _base():
    return {"name": "t", "objects": [
        {"id": "o1", "name": "O1",
         "business_definition": "测试对象 O1，用于业务层校验门",
         "stable_key": "a1",
         "attributes": [{"id": "a1", "name": "A1", "data_type": "string", "required": True, "constraints": {}}],
         "relations": []},
    ], "constraints": []}


def test_duplicate_object_id():
    o = _base()
    o["objects"].append({"id": "o1",
            "stable_key": "id",
            "business_definition": "测试用的对象", "name": "dup", "attributes": [], "relations": []})
    errs = validate_business_layer(o)
    assert any("重复" in e["message"] for e in errs)


def test_forbidden_property_name():
    o = _base()
    o["objects"][0]["attributes"][0]["id"] = "password"
    errs = validate_business_layer(o)
    assert any(e["severity"] == "error" and "敏感" in e["message"] for e in errs)


def test_invalid_data_type_and_cardinality():
    o = _base()
    o["objects"][0]["attributes"][0]["data_type"] = "blob"
    o["objects"][0]["relations"].append(
        {"id": "r1", "name": "R1", "target_object": "o1", "cardinality": "bogus"})
    errs = validate_business_layer(o)
    assert any("数据类型" in e["message"] for e in errs)
    assert any("基数" in e["message"] for e in errs)


def test_enum_definition_must_be_nonempty_list():
    o = _base()
    o["objects"][0]["attributes"][0]["constraints"] = {"enum": []}
    errs = validate_business_layer(o)
    assert any("enum" in e["message"] for e in errs)


def test_parent_cycle_detected():
    o = {"name": "t", "objects": [
        {"id": "a", "name": "A", "parent": "b",
         "business_definition": "测试对象 A", "stable_key": "id",
         "attributes": [], "relations": []},
        {"id": "b", "name": "B", "parent": "a",
         "business_definition": "测试对象 B", "stable_key": "id",
         "attributes": [], "relations": []},
    ], "constraints": []}
    errs = validate_business_layer(o)
    assert any("环" in e["message"] for e in errs)


def test_valid_model_passes_gate():
    assert validate_business_layer(_base()) == []


def test_enum_generates_sh_in_and_validates():
    onto = {"name": "t", "objects": [
        {"id": "o1",
            "stable_key": "a1",
            "business_definition": "测试用的对象", "name": "O1", "attributes": [
            {"id": "a1", "name": "A1", "data_type": "string", "required": False,
             "constraints": {"enum": ["A", "B"]}}], "relations": []},
    ], "constraints": []}
    ttl = translate_to_turtle(onto)
    assert "sh:in" in ttl

    # 合法取值
    data_ok = build_instance_graph(onto, [{"object_type": "o1", "a1": "A"}])
    res_ok = shacl_validate(data_ok, translate_to_shacl(onto))
    assert res_ok["valid"] is True

    # 越界取值 → sh:in 违规
    data_bad = build_instance_graph(onto, [{"object_type": "o1", "a1": "C"}])
    res_bad = shacl_validate(data_bad, translate_to_shacl(onto))
    assert res_bad["valid"] is False
    assert any("a1" in (v.get("path") or "") for v in res_bad["violations"])


# ===== SHACL 属性形状 URI 按对象隔离（跨对象复用属性 ID）=====

def _reused_enum_onto():
    """两个对象复用同一属性 ID（均带 enum，约束不同）——修复前会合并到同一 PS 节点，
    产生两条 sh:in → 形状图非法、SHACL 引擎拒绝执行。"""
    def obj(oid, enum):
        return {"id": oid, "name": oid.upper(),
                "business_definition": f"测试对象 {oid}，跨对象复用属性",
                "stable_key": "a1",
                "attributes": [
                    {"id": "a1", "name": "A1", "data_type": "string", "required": True, "constraints": {}},
                    {"id": "shared", "name": "共享", "data_type": "string", "required": False,
                     "constraints": {"enum": enum}},
                ],
                "relations": []}
    return {"name": "t", "objects": [obj("o1", ["A", "B"]), obj("o2", ["C", "D"])], "constraints": []}


def test_reused_enum_attr_shapes_isolated_and_engine_runs():
    onto = _reused_enum_onto()
    shapes = translate_to_shacl(onto)
    # 两个独立 PS 主体，各自 sh:in 出度恰为 1（修复前是 1 个主体 ×2 → 引擎报 InConstraintComponent 错误）
    in_counts: dict = {}
    for s, p, _o in shapes:
        if p == SH["in"]:
            in_counts[s] = in_counts.get(s, 0) + 1
    assert len(in_counts) == 2
    assert all(n == 1 for n in in_counts.values())
    # 引擎可执行：各对象按自己的 enum 判违和（o1 取 D 越界、o2 取 A 越界）
    data = build_instance_graph(onto, [
        {"object_type": "o1", "a1": "x", "shared": "D"},
        {"object_type": "o2", "a1": "y", "shared": "A"},
    ])
    res = shacl_validate(data, shapes)
    assert res is not None and res["valid"] is False
    assert len(res["violations"]) == 2
    data_ok = build_instance_graph(onto, [
        {"object_type": "o1", "a1": "x", "shared": "A"},
        {"object_type": "o2", "a1": "y", "shared": "C"},
    ])
    assert shacl_validate(data_ok, shapes)["valid"] is True


def test_reused_attr_id_same_type_is_warning_only():
    o = _reused_enum_onto()
    errs = validate_business_layer(o)
    # 复用本身合理：仅 warning，blocking_violations 为空 → 保存/发布不阻断
    assert blocking_violations(o) == []
    assert any(e["severity"] == "warning" and "shared" in e["path"] for e in errs)


def test_reused_attr_id_different_data_type_is_error():
    o = _reused_enum_onto()
    o["objects"][1]["attributes"][1]["data_type"] = "integer"
    errs = validate_business_layer(o)
    # 跨对象冲突 error + 既有 enum/integer 不匹配 error，共两条阻塞项
    assert any(e["severity"] == "error" and "data_type 不一致" in e["message"] for e in errs)
    assert len(blocking_violations(o)) == 2


def test_assert_shapes_well_formed_rejects_double_sh_in():
    g = Graph()
    ps = URIRef("http://ontology.local/xPS")
    g.add((ps, SH["in"], BNode()))
    g.add((ps, SH["in"], BNode()))
    with pytest.raises(ValueError, match="单值约束谓词出现多值"):
        assert_shapes_well_formed(g)