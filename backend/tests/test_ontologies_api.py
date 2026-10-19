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
        {"id": "obj_customer", "name": "客户",
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
