"""mcp_server：由本体 Schema 自动生成工具，通过 SSE 挂载 /mcp（VS6）。

工具契约（对外 Agent 可读）：
- ontology_query: 读本体 TBox，无副作用
- inference_trace: 读推理链路，无副作用
- instance_get: 读实例 ABox，无副作用
- action_invoke: 写外部系统，需确认令牌，有副作用
"""
from __future__ import annotations

import json
import os
from typing import Any

from action_engine import ActionEngine
from config import get_settings
from core.storage import JsonStore
from logging_setup import get_logger
from reasoning import inference_trace

logger = get_logger("mcp_server")

# P-02 借鉴补丁：对外工具契约元数据（外部 Agent 可静态判断读写/副作用）
TOOLS_CONTRACT = {
    "ontology_query": {
        "reads": ["本体TBox", "实例ABox"], "writes": [],
        "preconditions": ["本体已加载且非空"],
        "postconditions": ["仅返回被授权命名图内的结果"], "side_effect": False,
    },
    "inference_trace": {
        "reads": ["推理结论", "推导路径"], "writes": [],
        "preconditions": ["已执行过推理快照"],
        "postconditions": ["返回可追溯的推导链"], "side_effect": False,
    },
    "instance_get": {
        "reads": ["实例ABox"], "writes": [],
        "preconditions": ["实例存在"],
        "postconditions": ["返回实例及其溯源标记（含外部MCP来源着色）"], "side_effect": False,
    },
    "action_invoke": {
        "reads": ["Action定义", "目标外部系统"], "writes": ["外部系统", "本地执行日志"],
        "preconditions": ["Action 已定义", "本次执行已人工确认"],
        "postconditions": ["幂等键已写入", "执行结果已留痕"], "side_effect": True,
    },
}


class McpServer:
    def __init__(self) -> None:
        self._ontologies = JsonStore("ontologies")
        self._actions = JsonStore("actions")
        self._action_engine = ActionEngine()
        self._mcp: Any | None = None
        self._transport: Any | None = None

    def status(self) -> dict:
        return {"published": bool(self._mcp), "endpoint": "/mcp", "tools": list(TOOLS_CONTRACT.keys())}

    def publish(self, ontology_id: str) -> dict:
        rec = self._ontologies.get(ontology_id)
        if not rec:
            return {"published": False, "error": "本体不存在"}
        logger.info("publish MCP server for ontology_id=%s tools=%s", ontology_id, list(TOOLS_CONTRACT.keys()))
        return {"published": True, "ontology_id": ontology_id, "endpoint": "/mcp", "tools": list(TOOLS_CONTRACT.keys())}

    def mount(self, app):
        """将 MCP SSE 端点挂载到 FastAPI app。"""
        try:
            from mcp.server.fastmcp import FastMCP
            from mcp.server.sse import SseServerTransport
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp 库未安装，跳过 MCP Server 挂载: %s", e)
            return

        mcp = FastMCP("EnterpriseOntologyMCP", instructions="企业业务本体平台 MCP Server：可查询本体/实例/推理链路，或在确认后调用 Action 回写外部系统。")
        self._mcp = mcp
        self._transport = SseServerTransport("/mcp/messages/")

        @mcp.tool()
        def ontology_query(ontology_id: str, query: str = "") -> str:
            """查询本体结构：返回对象、属性、关系摘要。"""
            rec = self._ontologies.get(ontology_id)
            if not rec:
                return json.dumps({"error": "NOT_FOUND"}, ensure_ascii=False)
            summary = {
                "ontology_id": ontology_id,
                "name": rec.get("name"),
                "objects": [
                    {
                        "id": o["id"],
                        "name": o.get("name"),
                        "attribute_count": len(o.get("attributes", [])),
                        "relation_count": len(o.get("relations", [])),
                    }
                    for o in rec.get("objects", [])
                ],
            }
            return json.dumps(summary, ensure_ascii=False)

        @mcp.tool()
        def inference_trace(ontology_id: str) -> str:
            """返回 OWL2-RL 风格推理链路。"""
            rec = self._ontologies.get(ontology_id)
            if not rec:
                return json.dumps({"error": "NOT_FOUND"}, ensure_ascii=False)
            try:
                steps = _run_inference(rec)
                return json.dumps({"ontology_id": ontology_id, "steps": steps}, ensure_ascii=False)
            except Exception as e:  # noqa: BLE001
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        @mcp.tool()
        def instance_get(ontology_id: str, object_type: str = "", instance_id: str = "") -> str:
            """查询已落库实例（支持按 object_type 过滤或按 id 精确匹配）。"""
            instances = _load_instances(ontology_id)
            if object_type:
                instances = [i for i in instances if i.get("object_type") == object_type]
            if instance_id:
                instances = [i for i in instances if i.get("instance", {}).get("id") == instance_id or i.get("instance", {}).get("node_id") == instance_id]
            return json.dumps({"ontology_id": ontology_id, "count": len(instances), "instances": instances}, ensure_ascii=False)

        @mcp.tool()
        def action_invoke(action_id: str, confirmation_token: str = "") -> str:
            """确认执行 Action（需先通过 /api/actions/{id}/execute 获取 confirmation_token）。"""
            rec = self._actions.get(action_id)
            if not rec:
                return json.dumps({"error": "NOT_FOUND"}, ensure_ascii=False)
            result = self._action_engine.confirm(confirmation_token, {"action": rec})
            return json.dumps(result, ensure_ascii=False)

        @app.get("/mcp")
        async def handle_sse(request):
            async with self._transport.connect_sse(request.scope, request.receive, request.send) as streams:
                await mcp._mcp_server.run(
                    streams[0], streams[1],
                    mcp._mcp_server.create_initialization_options(),
                )

        @app.post("/mcp/messages/")
        async def handle_messages(request):
            await self._transport.handle_post_message(request.scope, request.receive, request.send)

        logger.info("MCP Server 已挂载到 /mcp (SSE)，工具: %s", list(TOOLS_CONTRACT.keys()))


def _run_inference(ontology: dict[str, Any]) -> list[dict[str, Any]]:
    from reasoning import inference_trace as _inf
    return _inf(ontology)


def _load_instances(ontology_id: str) -> list[dict[str, Any]]:
    path = os.path.join(get_settings().data_dir, "instances.jsonl")
    if not os.path.exists(path):
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("ontology_id") == ontology_id:
                    records.append(rec)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 instances.jsonl 失败: %s", e)
    return records
