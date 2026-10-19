"""modeling_translator 往返与结构校验测试（VS1）。"""
from modeling_translator import translate_to_turtle, validate_structure


def _sample_ontology():
    return {
        "name": "客户本体",
        "modeling_type": "manual",
        "objects": [
            {
                "id": "obj_customer",
                "name": "客户",
                "attributes": [
                    {"id": "attr_name", "name": "名称", "data_type": "string",
                     "required": True, "constraints": {"min_length": 1, "max_length": 128}},
                ],
                "relations": [
                    {"id": "rel_belongs", "name": "归属部门", "target_object": "obj_dept",
                     "cardinality": "many_to_one", "inverse_name": "拥有客户"},
                ],
            },
            {"id": "obj_dept", "name": "部门", "attributes": [], "relations": []},
        ],
        "constraints": [
            {"id": "cst_u", "name": "名称唯一", "scope": "obj_customer",
             "rule": "unique", "params": {"attribute": "attr_name"}},
        ],
    }


def test_translate_produces_owl_and_shacl():
    ttl = translate_to_turtle(_sample_ontology())
    assert "owl:Class" in ttl
    assert "DatatypeProperty" in ttl
    assert "ObjectProperty" in ttl
    assert "sh:NodeShape" in ttl
    assert "sh:minLength" in ttl
    assert "sh:unique" in ttl
    # 逆关系
    assert "owl:inverseOf" in ttl


def test_validate_structure_detects_dangling_parent():
    bad = {"objects": [{"id": "o1", "name": "X", "parent": "nope", "attributes": [], "relations": []}]}
    violations = validate_structure(bad)
    assert any("父对象" in v["message"] for v in violations)


def test_validate_structure_detects_dangling_relation_target():
    bad = {"objects": [
        {"id": "o1", "name": "X", "attributes": [],
         "relations": [{"id": "r1", "name": "R", "target_object": "ghost"}]},
    ]}
    violations = validate_structure(bad)
    assert any("目标对象" in v["message"] for v in violations)
