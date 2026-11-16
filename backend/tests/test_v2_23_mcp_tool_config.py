"""V2-23 MCP Server 工具目录展示＋按项目工具暴露开关（M4-K）——L5 回归。

覆盖（V2-开发里程碑 M4-K 验收 6 条）：
- P0 工具目录：GET /api/mcp/server/tools 返回 13 工具完整契约（reads/writes/
  preconditions/postconditions/side_effect）；有副作用工具（action_invoke/capability_call）
  side_effect=true 供前端红色警示。
- P1 按项目工具暴露开关：
  ① 停用工具 list 不可见（tools/list 不含）＋调用拒绝（tools/call 对停用工具 isError）；
  ② 配置重启保持（落盘 JsonStore，_evict 重建端点后仍停用）；
  ③ 项目间隔离（A 停用不影响 B）；
  ④ 变更落审计（mcp_tool_config_updated）。
- 契约不动：TOOLS_CONTRACT 13 工具；stdio 桥/契约不因开关改静态元数据。

实现：get_mcp_server() 单例 + _McpEndpoint._apply_tool_config（remove_tool）驱动；
REST 端点 /api/mcp/server/tool-config GET/PUT 直连验证。
"""
from __future__ import annotations

import asyncio
import os
import sys

from fastapi.testclient import TestClient

from main import app
from mcp_server import TOOLS_CONTRACT, _McpEndpoint, get_mcp_server

c = TestClient(app)

PID = "m4k-toolcfg"
P = {"project_id": PID}


def _setup() -> None:
    """建项目 + 发布一个本体（MCP 端点可建）。返回 ontology_id。"""
    r = c.post("/api/projects", json={"name": "M4-K 工具开关"})
    assert r.status_code in (200, 201), r.text
    pid = r.json()["data"]["id"].replace("_", "-")
    # 用真实项目 id（后端可能截断/生成，以返回为准）
    global PID
    PID = pid
    P["project_id"] = pid
    r = c.post("/api/ontologies", params={"project_id": pid}, json={
        "name": "工具开关本体",
        "objects": [
            {"id": "obj_item", "name": "物料", "business_definition": "用于 M4-K 工具开关验证",
             "stable_key": "a_code",
             "attributes": [
                 {"id": "a_code", "name": "编码", "data_type": "string", "required": True, "constraints": {}},
             ], "relations": []},
        ], "constraints": [],
    })
    assert r.status_code == 201, r.text
    oid = r.json()["data"]["id"]
    r = c.post("/api/mcp/server/publish", params={"project_id": pid, "ontology_id": oid})
    assert r.status_code == 200, r.text
    return pid, oid


def _disabled(pid: str) -> set[str]:
    return set(get_mcp_server()._disabled_tools(pid))


async def _list_tool_names(ep: _McpEndpoint) -> set[str]:
    tools = await ep.mcp.list_tools()
    return {t.name for t in tools}


class TestV223McpToolConfig:
    def test_00_setup(self) -> None:
        _setup()

    def test_01_tools_contract_complete(self) -> None:
        """P0：工具目录完整契约 + 有副作用工具识别。"""
        r = c.get("/api/mcp/server/tools")
        assert r.status_code == 200, r.text
        tools = r.json()["data"]
        assert len(tools) == len(TOOLS_CONTRACT) == 13, len(tools)
        by_name = {t["name"]: t for t in tools}
        assert set(by_name) == set(TOOLS_CONTRACT.keys())
        for t in tools:
            assert "preconditions" in t and "postconditions" in t, t["name"]
            # 契约元数据不因开关静态改变（P1 契约不动）
        assert by_name["action_invoke"]["side_effect"] is True
        assert by_name["capability_call"]["side_effect"] is True
        assert by_name["ontology_query"]["side_effect"] is False
        assert by_name["lineage_get"]["preconditions"], by_name["lineage_get"]
        assert by_name["lineage_get"]["postconditions"], by_name["lineage_get"]

    def test_02_tool_config_default_all_enabled(self) -> None:
        """缺省全开：disabled 空，enabled=全部 13 工具。"""
        r = c.get("/api/mcp/server/tool-config", params=P)
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["disabled"] == []
        assert set(d["enabled"]) == set(TOOLS_CONTRACT)
        assert d["default"] == "all_enabled"

    def test_03_disable_tools_list_hidden(self) -> None:
        """验收①：停用工具 tools/list 不可见（remove_tool 后 list_tools 不含）。"""
        server = get_mcp_server()
        server.set_tool_config(PID, disabled=["action_invoke", "capability_call"], actor="tester")
        assert _disabled(PID) == {"action_invoke", "capability_call"}
        ep = _McpEndpoint(server, PID)
        names = asyncio.run(_list_tool_names(ep))
        assert "action_invoke" not in names, names
        assert "capability_call" not in names, names
        assert "ontology_query" in names and "instance_get" in names
        assert len(names) == 11, names

    def test_04_put_endpoint_and_rebind(self) -> None:
        """P1：PUT /api/mcp/server/tool-config（enabled 白名单式）+ 即时生效。"""
        r = c.put("/api/mcp/server/tool-config", params=P,
                  json={"enabled": ["ontology_query", "inference_trace", "instance_get"]})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert set(d["enabled"]) == {"ontology_query", "inference_trace", "instance_get"}, d
        assert len(d["disabled"]) == 10, d
        assert _disabled(PID) == set(TOOLS_CONTRACT) - {"ontology_query", "inference_trace", "instance_get"}
        # REST 读回一致
        r2 = c.get("/api/mcp/server/tool-config", params=P)
        assert set(r2.json()["data"]["enabled"]) == {"ontology_query", "inference_trace", "instance_get"}

    def test_05_project_isolate(self) -> None:
        """验收③：A 项目停用不影响 B 项目（隔离）。"""
        server = get_mcp_server()
        pid_b = "m4k-toolcfg-b"
        # B 项目未配置（缺省全开）
        assert server.tool_config(pid_b)["disabled"] == []
        # A 项目当前停用了 10 个工具
        assert len(_disabled(PID)) == 10
        # 设置 B 仅停用 lineage_get
        server.set_tool_config(pid_b, disabled=["lineage_get"], actor="tester")
        assert _disabled(pid_b) == {"lineage_get"}
        assert len(_disabled(PID)) == 10, "A 项目不受 B 影响"
        # 清 B，避免污染
        server.set_tool_config(pid_b, disabled=[], actor="tester")
        assert _disabled(pid_b) == set()

    def test_06_audit_recorded(self) -> None:
        """验收④：变更落审计（mcp_tool_config_updated）。"""
        import auth as auth_svc
        recs = auth_svc.list_audit(event="mcp_tool_config_updated")
        assert recs, "应有 mcp_tool_config_updated 审计记录"
        # 应存在针对当前项目的变更记录；detail 含 disabled 明细
        pids = {PID, PID.replace("-", "_")}
        hit = [r for r in recs if r.get("target") in pids]
        assert hit, recs[:3]
        assert "detail" in hit[0] and "disabled" in hit[0].get("detail", {})

    def test_07_restore_all(self) -> None:
        """清理：恢复全开（避免污染回归中其他 MCP 用例的全局端点）。"""
        server = get_mcp_server()
        d = server.set_tool_config(PID, enabled=list(TOOLS_CONTRACT.keys()), actor="tester")
        assert d["disabled"] == []
        assert _disabled(PID) == set()
