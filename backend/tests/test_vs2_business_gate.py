"""VS2 补丁（P-01/P-03）测试：业务层轻量校验门 + enum 约束（sh:in）。"""
from modeling_translator import (
    build_instance_graph,
    translate_to_shacl,
    translate_to_turtle,
    validate_business_layer,
)
from jena_adapter.client import shacl_validate


def _base():
    return {"name": "t", "objects": [
        {"id": "o1", "name": "O1",
         "attributes": [{"id": "a1", "name": "A1", "data_type": "string", "required": True, "constraints": {}}],
         "relations": []},
    ], "constraints": []}


def test_duplicate_object_id():
    o = _base()
    o["objects"].append({"id": "o1", "name": "dup", "attributes": [], "relations": []})
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
        {"id": "a", "name": "A", "parent": "b", "attributes": [], "relations": []},
        {"id": "b", "name": "B", "parent": "a", "attributes": [], "relations": []},
    ], "constraints": []}
    errs = validate_business_layer(o)
    assert any("环" in e["message"] for e in errs)


def test_valid_model_passes_gate():
    assert validate_business_layer(_base()) == []


def test_enum_generates_sh_in_and_validates():
    onto = {"name": "t", "objects": [
        {"id": "o1", "name": "O1", "attributes": [
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
