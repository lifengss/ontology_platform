"""VS5-VS8 测试：模板、MCP Server、外部接入、Action 引擎。"""
from fastapi.testclient import TestClient

from main import app

c = TestClient(app)


def _create_ontology():
    r = c.post("/api/ontologies", json={
        "name": "外部接入测试",
        "objects": [
            {
                "id": "obj_account",
                "name": "账户",
                "attributes": [{"id": "attr_aname", "name": "账户名", "data_type": "string", "required": True, "constraints": {}}],
                "relations": [],
            },
        ],
        "constraints": [],
    })
    assert r.status_code == 201
    return r.json()["data"]


def test_templates_builtin_and_instantiate():
    r = c.get("/api/templates")
    assert r.status_code == 200
    assert any(t["id"] == "tpl_customer_basic" for t in r.json()["data"]["items"])
    r2 = c.post("/api/templates/tpl_customer_basic/instantiate")
    assert r2.status_code == 201
    assert "id" in r2.json()["data"]


def test_mcp_server_status():
    r = c.get("/mcp/status")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["endpoint"] == "/mcp"
    assert "ontology_query" in data["tools"]


def test_mcp_connection_crud_and_test():
    onto = _create_ontology()
    oid = onto["id"]
    body = {
        "display_name": "测试 CRM",
        "transport": "http",
        "endpoint": "http://localhost:9999/read",
        "auth": {"type": "api_key", "api_key_ref": "TEST_KEY"},
        "tool_allowlist": ["read"],
        "maps_to_ontology": {"ontology_id": oid, "object_type": "obj_account", "field_map": {"account_name": "attr_aname"}},
    }
    r = c.post("/api/mcp/connections", json=body)
    assert r.status_code == 201
    cid = r.json()["data"]["connection_id"]
    assert r.json()["data"]["id"] == cid

    r2 = c.get(f"/api/mcp/connections/{cid}")
    assert r2.status_code == 200
    assert r2.json()["data"]["display_name"] == "测试 CRM"

    r3 = c.post(f"/api/mcp/connections/{cid}/test")
    assert r3.status_code == 200
    assert r3.json()["data"]["reachable"] is False  # 9999 未监听

    r4 = c.delete(f"/api/mcp/connections/{cid}")
    assert r4.status_code == 200


def test_action_execute_and_confirm():
    action = {
        "display_name": "同步账户",
        "description": "",
        "target_connection": "",
        "input_map": {"name": "$.account.name"},
        "idempotency_key": "account::{name}",
        "invoke": {"kind": "webhook", "url": "http://localhost:9999/sync", "method": "POST"},
    }
    r = c.post("/api/actions", json=action)
    assert r.status_code == 201
    aid = r.json()["data"]["action_id"]

    # dry_run
    r2 = c.post(f"/api/actions/{aid}/execute", json={"input": {"account": {"name": "腾讯"}}, "dry_run": True})
    assert r2.status_code == 200
    assert r2.json()["data"]["dry_run"] is True
    assert r2.json()["data"]["planned_payload"]["name"] == "腾讯"

    # execute
    r3 = c.post(f"/api/actions/{aid}/execute", json={"input": {"account": {"name": "腾讯"}}})
    assert r3.status_code == 200
    token = r3.json()["data"]["confirmation_token"]
    assert token
    assert r3.json()["data"]["idempotency_key"] == "account::腾讯"

    # confirm
    r4 = c.post(f"/api/actions/{aid}/confirm", json={"confirmation_token": token})
    assert r4.status_code == 200
    assert r4.json()["data"]["executed"] is True
    assert "log_id" in r4.json()["data"]

    # 重复 token 应失效
    r5 = c.post(f"/api/actions/{aid}/confirm", json={"confirmation_token": token})
    assert r5.status_code == 422


def test_action_idempotency():
    action = {
        "display_name": "幂等测试",
        "description": "",
        "target_connection": "",
        "input_map": {"name": "$.name"},
        "idempotency_key": "idem::{name}",
        "invoke": {"kind": "webhook", "url": "http://localhost:9999/sync", "method": "POST"},
    }
    aid = c.post("/api/actions", json=action).json()["data"]["action_id"]

    t1 = c.post(f"/api/actions/{aid}/execute", json={"input": {"name": "A"}}).json()["data"]["confirmation_token"]
    c.post(f"/api/actions/{aid}/confirm", json={"confirmation_token": t1})

    # 第二次相同幂等键应命中
    t2 = c.post(f"/api/actions/{aid}/execute", json={"input": {"name": "A"}}).json()["data"]["confirmation_token"]
    r = c.post(f"/api/actions/{aid}/confirm", json={"confirmation_token": t2})
    assert r.json()["data"]["idempotency_hit"] is True
