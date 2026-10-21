"""本体 API 信封/分页一致性测试（script-check-skill 规则 7 的端到端自检）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

client = TestClient(app)

SAMPLE = {
    "name": "测试客户本体",
    "modeling_type": "manual",
    "objects": [
        {"id": "obj_customer",
            "stable_key": "attr_name",
            "business_definition": "测试用的对象", "name": "客户",
         "attributes": [{"id": "attr_name", "name": "名称", "data_type": "string",
                         "required": True, "constraints": {}}],
         "relations": []},
    ],
    "constraints": [],
}


def _create():
    r = client.post("/api/ontologies", json=SAMPLE)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_envelope_shape():
    r = client.get("/health")
    body = r.json()
    assert set(body.keys()) == {"success", "data", "error"}
    assert body["success"] is True
    assert body["error"] is None


def test_list_is_paginated_object_not_array():
    _create()
    r = client.get("/api/ontologies?page=1&pageSize=20")
    body = r.json()
    assert body["success"] is True
    d = body["data"]
    assert isinstance(d, dict)
    assert "items" in d and "total" in d and "page" in d and "page_size" in d
    assert isinstance(d["items"], list)


def test_create_read_validate_export():
    created = _create()
    oid = created["id"]
    # 读
    r = client.get(f"/api/ontologies/{oid}")
    assert r.status_code == 200 and r.json()["data"]["name"] == SAMPLE["name"]
    # 校验（结构校验应通过）
    r = client.post(f"/api/ontologies/{oid}/validate")
    assert r.status_code == 200
    assert r.json()["data"]["valid"] is True
    # 导出 TTL
    r = client.post(f"/api/ontologies/{oid}/export")
    assert r.status_code == 200
    assert "owl:Class" in r.json()["data"]["content"]
    # 快照
    r = client.post(f"/api/ontologies/{oid}/snapshot", json={"note": "ci"})
    assert r.status_code == 201
    r = client.get(f"/api/ontologies/{oid}/snapshots")
    assert r.json()["data"]["total"] >= 1


def test_not_found_returns_envelope():
    r = client.get("/api/ontologies/does_not_exist")
    assert r.status_code == 404
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "NOT_FOUND"


def test_namespace_filter_on_list():
    created = _create()
    assert created.get("namespace") == "default"
    # 显式指定不同 namespace 应过滤掉
    r = client.get("/api/ontologies?page=1&pageSize=20&namespace=sales")
    body = r.json()
    ids = [o["id"] for o in body["data"]["items"]]
    assert created["id"] not in ids
    # 显式指定 default 应包含
    r = client.get("/api/ontologies?page=1&pageSize=20&namespace=default")
    body = r.json()
    ids = [o["id"] for o in body["data"]["items"]]
    assert created["id"] in ids


def test_nl_query_shape_and_sparql():
    created = _create()
    oid = created["id"]
    r = client.post(f"/api/graph/{oid}/nl-query", json={"question": "列出所有客户"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert "sparql" in d
    assert "rows" in d
    assert d["count"] == len(d["rows"])
    assert d["sparql"].upper().startswith("SELECT")


def test_nl_query_requires_question():
    created = _create()
    oid = created["id"]
    r = client.post(f"/api/graph/{oid}/nl-query", json={})
    assert r.status_code == 422


def test_patch_ontology_object_updates_single_object():
    """就地更新本体内单个对象（对象约束编辑），应保留元数据且重翻译 TTL。"""
    created = _create()
    oid = created["id"]
    obj = created["objects"][0]
    payload = {
        "name": "客户-改名",
        "description": "更新后的描述",
        "attributes": [
            {"id": "attr_name", "name": "客户名称", "data_type": "string", "required": True, "constraints": {}},
            {"id": "attr_code", "name": "客户编码", "data_type": "string", "required": False, "constraints": {}},
        ],
        "relations": [],
    }
    r = client.patch(f"/api/ontologies/{oid}/objects/{obj['id']}", json=payload)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    target = next(o for o in d["objects"] if o["id"] == obj["id"])
    assert target["name"] == "客户-改名"
    assert target["description"] == "更新后的描述"
    assert len(target["attributes"]) == 2
    # 保留元数据：stable_key / business_definition / namespace
    assert target["stable_key"] == obj["stable_key"]
    assert target["business_definition"] == obj["business_definition"]
    assert target["namespace"] == obj.get("namespace", "default")


def test_patch_ontology_object_not_found():
    created = _create()
    oid = created["id"]
    r = client.patch(f"/api/ontologies/{oid}/objects/non_existent",
                     json={"name": "x", "attributes": [], "relations": []})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"