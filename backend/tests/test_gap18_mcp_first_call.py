"""GAP-18 MCP 发布后首调就绪 — 端点刚创建（manager.run() 任务未调度）不应假 503。

模拟：直接调用 McpServer.handle_streamable_request（端点刚 ensure，started 尚 False），
断言响应不再立即 503（等待就绪后正常走 manager 处理），仅真实启动失败才 503。
"""
import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _authenv import auth_module_guard
import main as m
from core.storage import JsonStore
from fastapi.testclient import TestClient

_guard = auth_module_guard()
app = m.app
client = TestClient(app)

_PID = f"gap18-{uuid.uuid4().hex[:8]}"


def _setup_project_with_publish():
    r = client.post("/api/projects", json={"name": f"GAP18-{int(time.time())}", "description": ""})
    global _PID
    _PID = r.json()["data"]["id"]
    body = {"name": "G18 本体", "modeling_type": "manual", "version": "1",
            "objects": [{"id": "obj_x", "name": "X", "stable_key": "attr_code",
                         "attributes": [{"id": "attr_code", "name": "编码", "data_type": "string",
                                         "required": True, "constraints": {}}],
                         "relations": []}],
            "constraints": [], "functions": []}
    r = client.post("/api/ontologies", params={"project_id": _PID}, json=body)
    assert r.status_code == 201, r.text
    oid = r.json()["data"]["id"]
    r = client.post(f"/api/mcp/server/publish", params={"project_id": _PID, "ontology_id": oid})
    assert r.status_code == 200 and r.json()["data"]["published"], r.text
    r = client.post("/api/mcp/server/token", params={"project_id": _PID})
    assert r.status_code == 201, r.text
    return oid


async def _call_streamable(server, pid: str) -> list[dict]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": f"/mcp/{pid}",
             "headers": [], "query_string": b"", "state": {}}
    await server.handle_streamable_request(pid, scope, receive, send)
    return sent


def test_first_streamable_call_not_503():
    """发布后首调：端点刚创建（started 尚 False）应等待就绪而非立即 503。"""
    from mcp_server import get_mcp_server

    _setup_project_with_publish()
    server = get_mcp_server()
    # token：取项目 token 记录（MCP 接入 token 在发布/接入页生成）
    from mcp_server import McpServer
    rec = server._token_record(_PID)
    assert rec and rec.get("token"), "项目应有 MCP 接入 token"
    sent = asyncio.run(_call_streamable(server, _PID))
    assert sent, "应有响应"
    status = sent[0].get("status")
    assert status != 503, f"首调不应 503（GAP-18），实际 {sent[0]}"


def test_evicted_endpoint_recreated_no_503():
    """端点被淘汰（空闲回收）后重建同样不 503。"""
    from mcp_server import get_mcp_server

    server = get_mcp_server()
    ep = server._endpoints.get(_PID)
    if ep:
        asyncio.run(ep.stop())
        server._endpoints.pop(_PID, None)
    sent = asyncio.run(_call_streamable(server, _PID))
    assert sent and sent[0].get("status") != 503


def teardown_module():
    try:
        client.delete(f"/api/projects/{_PID}")
    except Exception:
        pass
