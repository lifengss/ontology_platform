"""VS3 测试：实例归化流水线（preview / approve / SHACL 拦截 / 文件降级落库）。"""
from fastapi.testclient import TestClient
from ingestion import _call_llm
from main import app

c = TestClient(app)


def _make_ontology():
    body = {
        "name": "客户本体",
        "objects": [
            {
                "id": "obj_customer",
                "name": "客户",
                "attributes": [
                    {"id": "attr_name", "name": "名称", "data_type": "string", "required": True, "constraints": {}},
                    {"id": "attr_industry", "name": "行业", "data_type": "string", "required": False,
                     "constraints": {"enum": ["制造", "互联网"]}},
                ],
                "relations": [],
            }
        ],
        "constraints": [],
    }
    r = c.post("/api/ontologies", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_field_map_preview_and_approve():
    onto = _make_ontology()
    oid = onto["id"]
    r = c.post("/api/ingest/preview", json={
        "ontology_id": oid,
        "object_type": "obj_customer",
        "source": {
            "kind": "field_map",
            "records": [
                {"account_name": "腾讯", "industry": "互联网"},
                {"account_name": "宝钢", "industry": "制造"},
            ],
        },
        "mapping": {
            "field_map": {"account_name": "attr_name", "industry": "attr_industry"},
            "source_ref": "manual-csv",
        },
    })
    assert r.status_code == 201, r.text
    cand = r.json()["data"]
    assert cand["status"] == "pending"
    assert cand["source_type"] == "field_map"
    assert len(cand["instances"]) == 2
    assert cand["instances"][0]["attr_name"] == "腾讯"

    # approve → SHACL 通过 → imported
    r2 = c.post(f"/api/ingest/candidates/{cand['id']}/approve")
    assert r2.status_code == 200, r2.text
    res = r2.json()["data"]
    assert res["imported"] == 2
    assert res["violations"] == []
    assert res["neo_tx_id"] is not None

    # 详情状态变为 imported
    r3 = c.get(f"/api/ingest/candidates/{cand['id']}")
    assert r3.json()["data"]["status"] == "imported"


def test_field_map_preview_rejects_bad_attribute():
    onto = _make_ontology()
    r = c.post("/api/ingest/preview", json={
        "ontology_id": onto["id"],
        "object_type": "obj_customer",
        "source": {"kind": "field_map", "records": [{"x": 1}]},
        "mapping": {"field_map": {"x": "attr_not_exist"}},
    })
    assert r.status_code == 422, r.text


def test_approve_with_shacl_violation_stays_pending():
    onto = _make_ontology()
    oid = onto["id"]
    # attr_name required，但实例缺失
    r = c.post("/api/ingest/preview", json={
        "ontology_id": oid,
        "object_type": "obj_customer",
        "source": {"kind": "field_map", "records": [{"industry": "互联网"}]},
        "mapping": {"field_map": {"industry": "attr_industry"}},
    })
    cand = r.json()["data"]
    r2 = c.post(f"/api/ingest/candidates/{cand['id']}/approve")
    assert r2.status_code == 200
    res = r2.json()["data"]
    assert res["imported"] == 0
    assert any(v["path"] == "attr_name" for v in res["violations"])
    # 状态仍为 pending，可修正后重审
    r3 = c.get(f"/api/ingest/candidates/{cand['id']}")
    assert r3.json()["data"]["status"] == "pending"


def test_enum_violation_in_approve():
    onto = _make_ontology()
    oid = onto["id"]
    r = c.post("/api/ingest/preview", json={
        "ontology_id": oid,
        "object_type": "obj_customer",
        "source": {"kind": "field_map", "records": [{"account_name": "A", "industry": "金融"}]},
        "mapping": {"field_map": {"account_name": "attr_name", "industry": "attr_industry"}},
    })
    cand = r.json()["data"]
    r2 = c.post(f"/api/ingest/candidates/{cand['id']}/approve")
    res = r2.json()["data"]
    assert res["imported"] == 0
    assert any("attr_industry" in (v.get("path") or "") for v in res["violations"])


def test_reject_candidate():
    onto = _make_ontology()
    r = c.post("/api/ingest/preview", json={
        "ontology_id": onto["id"],
        "object_type": "obj_customer",
        "source": {"kind": "field_map", "records": [{"account_name": "X"}]},
        "mapping": {"field_map": {"account_name": "attr_name"}},
    })
    cid = r.json()["data"]["id"]
    r2 = c.post(f"/api/ingest/candidates/{cid}/reject")
    assert r2.status_code == 200
    assert r2.json()["data"]["status"] == "rejected"


def test_llm_preview_with_mocked_endpoint(monkeypatch):
    onto = _make_ontology()

    def fake_call(endpoint, model, prompt):
        return {"choices": [{"message": {"content": '{"instances":[{"attr_name":"MockCo","attr_industry":"制造"}]}'}}]}

    monkeypatch.setattr("ingestion._call_llm", fake_call)
    r = c.post("/api/ingest/preview", json={
        "ontology_id": onto["id"],
        "object_type": "obj_customer",
        "source": {"kind": "llm", "text": "某制造企业客户", "llm_endpoint": "http://mock/v1"},
    })
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["instances"][0]["attr_name"] == "MockCo"


def test_list_candidates_paginated():
    onto = _make_ontology()
    oid = onto["id"]
    for i in range(3):
        c.post("/api/ingest/preview", json={
            "ontology_id": oid,
            "object_type": "obj_customer",
            "source": {"kind": "field_map", "records": [{"account_name": f"C{i}"}]},
            "mapping": {"field_map": {"account_name": "attr_name"}},
        })
    r = c.get(f"/api/ingest/candidates?ontology_id={oid}&page=1&pageSize=2")
    assert r.status_code == 200
    payload = r.json()["data"]
    assert payload["total"] >= 3
    assert len(payload["items"]) <= 2
    assert payload["page_size"] == 2
