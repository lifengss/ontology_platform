"""VS2 测试：真实 SHACL 校验（pyshacl）+ OWL2-RL 风格推理 trace。"""
from modeling_translator import build_instance_graph, translate_to_shacl
from jena_adapter.client import shacl_validate
from reasoning import inference_trace


def _ontology():
    return {
        "name": "客户本体",
        "objects": [
            {"id": "obj_customer", "name": "客户",
             "attributes": [{"id": "attr_name", "name": "名称", "data_type": "string",
                             "required": True, "constraints": {"min_length": 1, "max_length": 128}}],
             "relations": [{"id": "rel_belongs", "name": "归属部门", "target_object": "obj_dept",
                            "cardinality": "many_to_one", "inverse_name": "拥有客户"}]},
            {"id": "obj_dept", "name": "部门", "attributes": [], "relations": []},
        ],
        "constraints": [{"id": "cst_u", "name": "名称唯一", "scope": "obj_customer",
                         "rule": "unique", "params": {"attribute": "attr_name"}}],
    }


def test_shacl_catches_missing_required():
    onto = _ontology()
    # 缺 attr_name（required）→ minCount 违规
    data = build_instance_graph(onto, [{"object_type": "obj_customer"}])
    shapes = translate_to_shacl(onto)
    res = shacl_validate(data, shapes)
    assert res is not None
    assert res["valid"] is False
    assert any("attr_name" in (v.get("path") or "") for v in res["violations"])


def test_shacl_passes_valid_instance():
    onto = _ontology()
    data = build_instance_graph(onto, [{"object_type": "obj_customer", "attr_name": "张三"}])
    shapes = translate_to_shacl(onto)
    res = shacl_validate(data, shapes)
    assert res["valid"] is True


def test_inference_trace_inverse_symmetry():
    onto = _ontology()
    steps = inference_trace(onto)
    # rel_belongs inverseOf rel_belongsInv ⇒ 推导出反向
    assert any(s["rule"] == "owl:inverseOf 对称性" for s in steps)


def test_inference_trace_subclass_transitivity():
    onto = {
        "objects": [
            {"id": "o_a", "name": "A", "parent": "o_b", "attributes": [], "relations": []},
            {"id": "o_b", "name": "B", "parent": "o_c", "attributes": [], "relations": []},
            {"id": "o_c", "name": "C", "attributes": [], "relations": []},
        ],
        "constraints": [],
    }
    steps = inference_trace(onto)
    assert any(s["rule"] == "RDFS subClassOf 传递闭包" and "o_a⊆o_c" == s["conclusion"] for s in steps)
