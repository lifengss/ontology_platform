"""VS2 测试：真实 SHACL 校验（pyshacl）+ OWL2-RL 风格推理 trace。"""
from modeling_translator import build_instance_graph, translate_to_shacl
from jena_adapter.client import shacl_validate
from reasoning import inference_trace


def _ontology():
    return {
        "name": "客户本体",
        "objects": [
            {"id": "obj_customer",
            "stable_key": "attr_name",
            "business_definition": "测试用的对象", "name": "客户",
             "attributes": [{"id": "attr_name", "name": "名称", "data_type": "string",
                             "required": True, "constraints": {"min_length": 1, "max_length": 128}}],
             "relations": [{"id": "rel_belongs",
            "stable_key": "id",
            "business_definition": "测试用的对象", "name": "归属部门", "target_object": "obj_dept",
                            "cardinality": "many_to_one", "inverse_name": "拥有客户"}]},
            {"id": "obj_dept", "name": "部门",
             "business_definition": "测试部门对象", "stable_key": "id",
             "attributes": [], "relations": []},
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
    s = next(x for x in steps if x["rule"] == "owl:inverseOf 对称性")
    # 业务中文文案：从 RDFS.label 取中文名（归属部门 / 拥有客户），而非裸 ID
    assert s["rule_zh"] == "逆向关系补全"
    assert "归属部门" in s["premises_zh"][0] and "拥有客户" in s["premises_zh"][0]
    assert "拥有客户" in s["conclusion_zh"] and "归属部门" in s["conclusion_zh"]
    # 原始 OWL2-RL 形式保留
    assert "inverseOf" in s["premises"][0] and "inverseOf" in s["conclusion"]

def test_inference_trace_zh_uses_chinese_object_names():
    onto = _ontology()
    steps = inference_trace(onto)
    for s in steps:
        assert s["rule_zh"] and s["premises_zh"] and s["conclusion_zh"]
        # 中文文案不应包含裸属性/对象 ID（labels 缺失时才回退 ID，该本体全部有中文名）
        assert "attr_name" not in " ".join(s["premises_zh"] + [s["conclusion_zh"]])

def test_inference_trace_subclass_transitivity():
    onto = {
        "objects": [
            {"id": "o_a", "name": "A", "parent": "o_b",
             "business_definition": "测试对象 A", "stable_key": "attr_id",
             "attributes": [{"id": "attr_id", "name": "ID", "data_type": "string"}], "relations": []},
            {"id": "o_b", "name": "B", "parent": "o_c",
             "business_definition": "测试对象 B", "stable_key": "attr_id",
             "attributes": [{"id": "attr_id", "name": "ID", "data_type": "string"}], "relations": []},
            {"id": "o_c", "name": "C",
             "business_definition": "测试对象 C", "stable_key": "attr_id",
             "attributes": [{"id": "attr_id", "name": "ID", "data_type": "string"}], "relations": []},
        ],
        "constraints": [],
    }
    steps = inference_trace(onto)
    s = next(x for x in steps if x["rule"] == "RDFS subClassOf 传递闭包" and x["conclusion"] == "o_a⊆o_c")
    # 传递推断的业务化表述：A 也归属于 C，且实例自动适用 C 的约束
    assert s["rule_zh"] == "对象归属的传递"
    assert "A" in s["conclusion_zh"] and "C" in s["conclusion_zh"] and "自动适用" in s["conclusion_zh"]