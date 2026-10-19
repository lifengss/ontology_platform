"""VS4 测试：图谱数据（TBox+ABox 合并、来源着色、tab 切换）。"""
from fastapi.testclient import TestClient
from main import app

c = TestClient(app)


def _make_ontology_with_relations():
    body = {
        "name": "部门客户本体",
        "objects": [
            {
                "id": "obj_dept",
                "name": "部门",
                "attributes": [{"id": "attr_dname", "name": "部门名", "data_type": "string", "required": True, "constraints": {}}],
                "relations": [],
            },
            {
                "id": "obj_customer",
                "name": "客户",
                "attributes": [
                    {"id": "attr_name", "name": "名称", "data_type": "string", "required": True, "constraints": {}},
                ],
                "relations": [{"id": "rel_belong", "name": "归属部门", "target_object": "obj_dept", "cardinality": "many_to_one"}],
            },
        ],
        "constraints": [],
    }
    r = c.post("/api/ontologies", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_schema_graph_tab():
    onto = _make_ontology_with_relations()
    r = c.get(f"/api/graph/{onto['id']}")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert "tabs" in data
    schema = data["tabs"]["schema"]
    node_ids = {n["id"] for n in schema["nodes"]}
    assert "obj_customer" in node_ids
    assert "obj_dept" in node_ids
    # schema 边：对象关系
    assert any(e["source"] == "obj_customer" and e["target"] == "obj_dept" for e in schema["edges"])
    # 所有 schema 节点来源为 internal
    assert all(n["source"] == "internal" for n in schema["nodes"])


def test_knowledge_graph_includes_instances_after_approve():
    onto = _make_ontology_with_relations()
    oid = onto["id"]
    # field_map preview & approve
    r = c.post("/api/ingest/preview", json={
        "ontology_id": oid,
        "object_type": "obj_customer",
        "source": {"kind": "field_map", "records": [{"name": "腾讯"}]},
        "mapping": {"field_map": {"name": "attr_name"}},
    })
    cand = r.json()["data"]
    c.post(f"/api/ingest/candidates/{cand['id']}/approve")

    r2 = c.get(f"/api/graph/{oid}")
    data = r2.json()["data"]
    kg = data["tabs"]["knowledge"]
    inst_nodes = [n for n in kg["nodes"] if n["kind"] == "instance"]
    assert len(inst_nodes) == 1
    assert inst_nodes[0]["label"] == "腾讯"
    assert inst_nodes[0]["source"] == "external_mcp"
    assert inst_nodes[0]["object_type"] == "obj_customer"

    # instance_of 边
    assert any(
        e["kind"] == "instance_of" and e["source"] == inst_nodes[0]["id"] and e["target"] == "obj_customer"
        for e in kg["edges"]
    )


def test_top_level_graph_is_knowledge_graph():
    onto = _make_ontology_with_relations()
    r = c.get(f"/api/graph/{onto['id']}")
    data = r.json()["data"]
    assert any(n["kind"] == "object" for n in data["nodes"])
    assert "source_legend" in data


def test_inference_trace_endpoint():
    onto = {
        "name": "继承推理",
        "objects": [
            {"id": "A", "name": "A", "parent": "B", "attributes": [], "relations": []},
            {"id": "B", "name": "B", "parent": "C", "attributes": [], "relations": []},
            {"id": "C", "name": "C", "attributes": [], "relations": []},
        ],
        "constraints": [],
    }
    r = c.post("/api/ontologies", json=onto)
    oid = r.json()["data"]["id"]
    r2 = c.get(f"/api/inference/trace/{oid}")
    assert r2.status_code == 200
    steps = r2.json()["data"]["steps"]
    assert any("传递闭包" in s["rule"] for s in steps)


def test_graph_not_found():
    r = c.get("/api/graph/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
