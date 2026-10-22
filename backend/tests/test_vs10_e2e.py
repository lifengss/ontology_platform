"""VS10 集成验收：P0 切片端到端串联测试（PRD §9.1 验收项 1-4 的测试证据）。

完整链路（单一独立项目 vs10-e2e，互不污染）：
  (1) 手工建模（表单/JSON）→ SHACL 校验（合法样本通过 / 违规样本拦截）
  (2) 发布 MCP Server → 真实 MCP SDK 客户端经 SSE 查询
      ontology_query / inference_trace / instance_get（PRD §9.1-1：外部 Agent 可查询）
  (3) 连接外部 MCP（mock http）读取 → 候选 pending → 未审核不落库（卡点）
      → 人工 approve 入库 source=external_mcp（PRD §9.1-2/3）
  (4) 定义 Action → dry-run（零外呼）→ execute+confirm 真实回写（mock 恰好收到 1 次）
      → 相同幂等键重复触发 → idempotency_hit，不重复回写（PRD §9.1-4）

依赖 mcp SDK 与 uvicorn（requirements.txt 已含）；mock 外部服务见 mock_mcp_http。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from main import app

c = TestClient(app)
PID = "vs10-e2e"
P = {"project_id": PID}

_ctx: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# 基础设施：同进程 uvicorn（真实 socket，供 MCP SSE 客户端连接）
# ---------------------------------------------------------------------------

def _pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_server():
    """同进程启动 uvicorn（与 TestClient 共享 app 单例：mcp_server 发布状态 + JsonStore 一致）。"""
    port = _pick_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="vs10-uvicorn")
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("uvicorn 线程意外退出")
        time.sleep(0.05)
    assert server.started, "uvicorn 20s 内未就绪"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _mcp_sse_query(base_url: str, ontology_id: str) -> dict[str, Any]:
    """真实 MCP SDK 客户端经 SSE 连接 /mcp/{PID}，调用三个只读工具。"""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async def _run() -> dict[str, Any]:
        results: dict[str, Any] = {}
        async with sse_client(f"{base_url}/mcp/{PID}") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                results["tools"] = sorted(t.name for t in tools.tools)
                rq = await session.call_tool("ontology_query", {"ontology_id": ontology_id, "query": ""})
                results["ontology_query"] = json.loads(rq.content[0].text)
                rt = await session.call_tool("inference_trace", {"ontology_id": ontology_id})
                results["inference_trace"] = json.loads(rt.content[0].text)
                ri = await session.call_tool(
                    "instance_get", {"ontology_id": ontology_id, "object_type": "Order"}
                )
                results["instance_get"] = json.loads(ri.content[0].text)
        return results

    return asyncio.run(asyncio.wait_for(_run(), timeout=30))


# ---------------------------------------------------------------------------
# (1) 手工建模 + SHACL 校验
# ---------------------------------------------------------------------------

def _department_ontology() -> dict[str, Any]:
    """某部门客户-订单域本体（PRD §9.1-1：ITBP 手工建模）。"""
    return {
        "name": "VS10验收-客户订单域",
        "description": "VS10 集成验收用部门本体",
        "modeling_type": "manual",
        "objects": [
            {
                "id": "Customer",
                "name": "客户",
                "business_definition": "部门服务的客户",
                "stable_key": "customerCode",
                "attributes": [
                    {"id": "customerCode", "name": "客户编码", "data_type": "string", "required": True, "constraints": {}},
                    {"id": "customerName", "name": "客户名称", "data_type": "string", "required": True, "constraints": {}},
                ],
                "relations": [],
            },
            {
                "id": "Order",
                "name": "订单",
                "business_definition": "客户订单",
                "stable_key": "orderCode",
                "attributes": [
                    {"id": "orderCode", "name": "订单编码", "data_type": "string", "required": True, "constraints": {}},
                    {"id": "amount", "name": "金额", "data_type": "float", "required": True,
                     "constraints": {"minimum": 0}},
                ],
                "relations": [
                    {"id": "belongsToCustomer", "name": "归属客户", "target_object": "Customer",
                     "cardinality": "many_to_one"},
                ],
            },
        ],
        "constraints": [],
    }


def test_01_modeling_and_shacl_gate():
    r = c.post("/api/ontologies", params=P, json=_department_ontology())
    assert r.status_code == 201, r.text
    _ctx["ontology_id"] = r.json()["data"]["id"]
    oid = _ctx["ontology_id"]

    # 合法样本：SHACL 通过
    r2 = c.post(f"/api/ontologies/{oid}/validate", params=P, json={
        "sample_instances": [
            {"object_type": "Customer", "customerCode": "C001", "customerName": "甲客户"},
            {"object_type": "Order", "orderCode": "O001", "amount": 100.5, "belongsToCustomer": "C001"},
        ],
    })
    assert r2.status_code == 200
    assert r2.json()["data"]["valid"] is True, r2.text

    # 违规样本：amount=-5 违反 minimum=0 → 必须拦截（PRD §9.1-1：模型可信）
    r3 = c.post(f"/api/ontologies/{oid}/validate", params=P, json={
        "sample_instances": [
            {"object_type": "Order", "orderCode": "O-BAD", "amount": -5},
        ],
    })
    assert r3.status_code == 200
    data = r3.json()["data"]
    assert data["valid"] is False
    # 违和应落在 amount 属性上（minInclusive）：SHACL 报告里属性在 path 字段，不在 message
    assert any(v.get("path") == "amount" for v in data["violations"]), data["violations"]


# ---------------------------------------------------------------------------
# (2) 发布 MCP Server + 真实 SDK 客户端 SSE 查询
# ---------------------------------------------------------------------------

def test_02_mcp_publish_and_sse_query(live_server):
    oid = _ctx["ontology_id"]
    r = c.post("/api/mcp/server/publish", params={**P, "ontology_id": oid})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["published"] is True
    assert d["endpoint"] == f"/mcp/{PID}"

    r2 = c.get("/mcp/status", params=P)
    assert r2.json()["data"]["published"] is True
    assert any(o["ontology_id"] == oid for o in r2.json()["data"]["published_ontologies"])

    # PRD §9.1-1：外部 Agent（MCP SDK 客户端）经 SSE 成功查询
    res = _mcp_sse_query(live_server, oid)
    assert {"ontology_query", "inference_trace", "instance_get", "action_invoke"} <= set(res["tools"])
    assert {o["id"] for o in res["ontology_query"]["objects"]} == {"Customer", "Order"}
    assert "steps" in res["inference_trace"]
    assert res["instance_get"]["count"] == 0  # 尚未读入实例
    _ctx["mcp_base"] = live_server


# ---------------------------------------------------------------------------
# (3) 外部 MCP 读取 + 人工审核卡点 + 落库
# ---------------------------------------------------------------------------

def test_03_external_mcp_read_review_gate():
    from tests.mock_mcp_http import start_mock

    mock = start_mock(read_records=[
        {"order_code": "EXT-O1", "amount": "250.5", "customer_code": "C001"},
        {"order_code": "EXT-O2", "amount": "88", "customer_code": "C002"},
    ])
    _ctx["mock"] = mock
    os.environ["VS10_MOCK_TOKEN"] = "secret-123"
    oid = _ctx["ontology_id"]
    try:
        body = {
            "display_name": "外部ERP（mock）",
            "transport": "http",
            "endpoint": mock.read_url,
            "auth": {"type": "bearer_token", "bearer_token_ref": "${VS10_MOCK_TOKEN}"},
            "tool_allowlist": ["read"],
            "maps_to_ontology": {
                "ontology_id": oid,
                "object_type": "Order",
                "field_map": {"order_code": "orderCode", "amount": "amount"},
                "read_tool": "read",
            },
        }
        r = c.post("/api/mcp/connections", params=P, json=body)
        assert r.status_code == 201, r.text
        _ctx["connection_id"] = r.json()["data"]["connection_id"]
        cid = _ctx["connection_id"]

        # 连通性探测（GET + Bearer 头）
        r2 = c.post(f"/api/mcp/connections/{cid}/test", params=P)
        assert r2.json()["data"]["reachable"] is True
        assert r2.json()["data"]["auth_ok"] is True
        assert len(mock.read_calls) == 0  # 探测只走 GET，不触发读取工具

        # 读取 → 生成 pending 候选
        r3 = c.post(f"/api/mcp/connections/{cid}/read", params=P)
        assert r3.status_code == 200, r3.text
        d3 = r3.json()["data"]
        assert d3["record_count"] == 2
        assert len(mock.read_calls) == 1  # 读取工具确实被调用了 1 次
        _ctx["candidate_id"] = d3["candidate_id"]

        # PRD §9.1-2 卡点：未审核候选不得直接落库
        r4 = c.get("/api/ingest/instances", params={**P, "ontology_id": oid})
        assert r4.json()["data"]["total"] == 0, "未审核候选直接落库，审核卡点失效"

        # 人工审核批准 → 落库
        r5 = c.post(f"/api/ingest/candidates/{_ctx['candidate_id']}/approve", params=P)
        assert r5.status_code == 200, r5.text

        r6 = c.get("/api/ingest/instances", params={**P, "ontology_id": oid, "object_type": "Order"})
        data = r6.json()["data"]
        assert data["total"] == 2
        codes = sorted(i["instance"].get("orderCode") for i in data["items"])
        assert codes == ["EXT-O1", "EXT-O2"]
        # 实例记录不带 source_type（来源标记在候选上）：所有实例应来自本次批准的候选
        assert all(i.get("candidate_id") == _ctx["candidate_id"] for i in data["items"])
        # PRD §9.1-3：人工批准入库的候选来源为 external_mcp，状态 imported
        rc = c.get(f"/api/ingest/candidates/{_ctx['candidate_id']}", params=P)
        assert rc.status_code == 200
        assert rc.json()["data"]["source_type"] == "external_mcp"
        assert rc.json()["data"]["status"] == "imported"
        _ctx["approved"] = True
    finally:
        mock.stop()
        os.environ.pop("VS10_MOCK_TOKEN", None)


# ---------------------------------------------------------------------------
# (4) Action：dry-run 零外呼 / 确认回写 / 幂等防重
# ---------------------------------------------------------------------------

def test_04_action_dryrun_confirm_idempotent(monkeypatch):
    # 该测试需要真实外呼以证明"恰好写一次"：临时关闭展示门（测试结束自动恢复）
    import action_engine
    monkeypatch.setattr(action_engine, "DISPLAY_ONLY", False)

    from tests.mock_mcp_http import start_mock

    mock = start_mock()
    _ctx["mock_write"] = mock
    try:
        r = c.post("/api/actions", params=P, json={
            "name": "回写ERP订单确认",
            "category": "数据同步",
            "target_connection": _ctx["connection_id"],
            "invoke": {"kind": "webhook", "url": mock.write_url, "method": "POST"},
            # input_map 契约为纯 JSONPath（字面量需放 input 里再映射）
            "input_map": {"orderCode": "$.order_code", "note": "$.note"},
            "idempotency_key": "order-{orderCode}",
            "confirmation": "human_required",
            "dry_run_supported": True,
        })
        assert r.status_code == 201, r.text
        _ctx["action_id"] = r.json()["data"]["action_id"]
        aid = _ctx["action_id"]

        # dry-run：只出计划载荷，零外呼（PRD §9.1-4：每次确认）
        r2 = c.post(f"/api/actions/{aid}/execute", params=P, json={
            "input": {"order_code": "EXT-O1", "note": "VS10 验收回写"}, "dry_run": True,
        })
        d2 = r2.json()["data"]
        assert d2["dry_run"] is True
        assert d2["confirmation_token"] is None
        assert d2["planned_payload"] == {"orderCode": "EXT-O1", "note": "VS10 验收回写"}
        assert len(mock.write_calls) == 0, "dry-run 不应外呼业务系统"

        # 正式触发 → 仅拿确认令牌，未确认前不写
        r3 = c.post(f"/api/actions/{aid}/execute", params=P,
                    json={"input": {"order_code": "EXT-O1", "note": "VS10 验收回写"}})
        token = r3.json()["data"]["confirmation_token"]
        assert token
        assert len(mock.write_calls) == 0, "未确认不应回写"

        # 人工确认 → 真实回写（mock 恰好收到 1 次）
        r4 = c.post(f"/api/actions/{aid}/confirm", params=P, json={"confirmation_token": token})
        d4 = r4.json()["data"]
        assert d4["executed"] is True
        assert d4["idempotency_hit"] is False
        assert d4["external_response"]["status"] == 200
        assert len(mock.write_calls) == 1
        assert mock.write_calls[0]["payload"] == {"orderCode": "EXT-O1", "note": "VS10 验收回写"}

        # 重复触发（相同输入 → 相同幂等键）→ 命中幂等，不再重复回写
        r5 = c.post(f"/api/actions/{aid}/execute", params=P,
                    json={"input": {"order_code": "EXT-O1", "note": "VS10 验收回写"}})
        token2 = r5.json()["data"]["confirmation_token"]
        assert token2 and token2 != token
        r6 = c.post(f"/api/actions/{aid}/confirm", params=P, json={"confirmation_token": token2})
        d6 = r6.json()["data"]
        assert d6["executed"] is True
        assert d6["idempotency_hit"] is True, "相同幂等键重复触发应命中幂等"
        assert len(mock.write_calls) == 1, "重复触发导致重复回写"

        # 执行日志可查
        r7 = c.get(f"/api/actions/{aid}/logs", params=P)
        assert r7.json()["data"]["total"] == 1
    finally:
        mock.stop()


def test_05_unpublish_cleanup():
    oid = _ctx["ontology_id"]
    r = c.post(f"/api/mcp/server/unpublish?project_id={PID}&ontology_id={oid}")
    assert r.status_code == 200
    r2 = c.get("/mcp/status", params=P)
    assert r2.json()["data"]["published"] is False, "取消发布后项目 Server 应下线"
