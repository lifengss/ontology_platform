"""mcp_server：由本体 Schema 按项目自动生成工具，通过 SSE 挂载 /mcp/{project_id}（VS6）。

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
    """按项目维护独立的 FastMCP 实例，挂载到 /mcp/{project_id}。"""

    def __init__(self) -> None:
        self._projects: dict[str, Any] = {}
        self._transports: dict[str, Any] = {}
        self._published_ontologies: dict[str, list[str]] = {}  # project_id -> [ontology_id, ...]（5.1：按本体记录发布清单）
        self._action_engine = ActionEngine()

    def _ontologies(self, project_id: str) -> JsonStore:
        return JsonStore("ontologies", project_id=project_id)

    def _actions(self, project_id: str) -> JsonStore:
        return JsonStore("actions", project_id=project_id)

    def _ontology_names(self, project_id: str, ontology_ids: list[str]) -> list[dict]:
        store = self._ontologies(project_id)
        items = []
        for oid in ontology_ids:
            rec = store.get(oid)
            items.append({"ontology_id": oid, "name": (rec or {}).get("name") or oid})
        return items

    def status(self, project_id: str | None = None) -> dict:
        if project_id:
            # 5.1：项目级状态含"已发布本体清单"（多本体共享同一项目端点）
            return {
                "published": project_id in self._projects,
                "endpoint": f"/mcp/{project_id}",
                "messages_endpoint": f"/mcp/{project_id}/messages/",
                "tools": list(TOOLS_CONTRACT.keys()),
                "published_ontologies": self._ontology_names(project_id, self._published_ontologies.get(project_id, [])),
            }
        # 5.2：全局状态=多项目共存视图（各项目独立端点互不影响）
        return {
            "published_projects": [
                {
                    "project_id": pid,
                    "endpoint": f"/mcp/{pid}",
                    "published_ontologies": self._ontology_names(pid, self._published_ontologies.get(pid, [])),
                }
                for pid in self._projects
            ],
            "tools": list(TOOLS_CONTRACT.keys()),
        }

    def published_ids(self, project_id: str) -> list[str]:
        """已发布本体清单（公开只读，供 MCP 列表接口使用）。"""
        return list(self._published_ontologies.get(project_id, []))

    def server_active(self, project_id: str) -> bool:
        """项目 MCP Server 是否在线（首个本体发布时上线，清单清空时下线）。"""
        return project_id in self._projects

    def publish(self, project_id: str, ontology_id: str) -> dict:
        rec = self._ontologies(project_id).get(ontology_id)
        if not rec:
            return {"published": False, "error": "本体不存在"}
        self._ensure_mcp(project_id)
        ids = self._published_ontologies.setdefault(project_id, [])
        if ontology_id not in ids:
            ids.append(ontology_id)
        logger.info("publish MCP server project_id=%s ontology_id=%s ontologies=%d tools=%s",
                    project_id, ontology_id, len(ids), list(TOOLS_CONTRACT.keys()))
        return {
            "published": True,
            "project_id": project_id,
            "ontology_id": ontology_id,
            "endpoint": f"/mcp/{project_id}",
            "tools": list(TOOLS_CONTRACT.keys()),
            "published_ontologies": self._ontology_names(project_id, ids),
        }

    def unpublish(self, project_id: str, ontology_id: str | None = None) -> dict:
        if project_id not in self._projects:
            return {"published": False, "error": "MCP Server 未发布"}
        if ontology_id:
            # 5.1：按本体取消发布；清单清空后下线整个项目 Server
            ids = self._published_ontologies.get(project_id, [])
            if ontology_id in ids:
                ids.remove(ontology_id)
            if not ids:
                self._projects.pop(project_id, None)
                self._transports.pop(project_id, None)
            logger.info("unpublish MCP ontology project_id=%s ontology_id=%s remaining=%d", project_id, ontology_id, len(ids))
            return {"published": bool(self._projects.get(project_id)), "project_id": project_id,
                    "ontology_id": ontology_id, "message": "已取消发布该本体"}
        self._projects.pop(project_id, None)
        self._transports.pop(project_id, None)
        self._published_ontologies.pop(project_id, None)
        logger.info("unpublish MCP server project_id=%s", project_id)
        return {"published": False, "project_id": project_id, "message": "已取消发布"}

    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": meta.get("description", ""),
                "side_effect": meta.get("side_effect", False),
                "reads": meta.get("reads", []),
                "writes": meta.get("writes", []),
                "example": self._tool_example(name),
            }
            for name, meta in TOOLS_CONTRACT.items()
        ]

    def _tool_example(self, tool_name: str) -> dict[str, Any]:
        examples = {
            "ontology_query": {"method": "tools/call", "params": {"name": "ontology_query", "arguments": {"ontology_id": "<ontology_id>"}}},
            "inference_trace": {"method": "tools/call", "params": {"name": "inference_trace", "arguments": {"ontology_id": "<ontology_id>"}}},
            "instance_get": {"method": "tools/call", "params": {"name": "instance_get", "arguments": {"ontology_id": "<ontology_id>", "object_type": "Activity"}}},
            "action_invoke": {"method": "tools/call", "params": {"name": "action_invoke", "arguments": {"action_id": "<action_id>", "confirmation_token": "<token>"}}},
        }
        return examples.get(tool_name, {})

    def _ensure_mcp(self, project_id: str) -> Any:
        try:
            from mcp.server.fastmcp import FastMCP
            from mcp.server.sse import SseServerTransport
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp 库未安装，跳过 MCP Server 挂载: %s", e)
            return None

        if project_id in self._projects:
            return self._projects[project_id]

        mcp = FastMCP(
            f"EnterpriseOntologyMCP_{project_id}",
            instructions=f"企业业务本体平台 MCP Server（项目 {project_id}）：可查询本体/实例/推理链路，或在确认后调用 Action 回写外部系统。",
        )
        transport = SseServerTransport(f"/mcp/{project_id}/messages/")
        self._projects[project_id] = mcp
        self._transports[project_id] = transport

        ontologies = self._ontologies(project_id)
        actions = self._actions(project_id)

        @mcp.tool()
        def ontology_query(ontology_id: str, query: str = "") -> str:
            """查询本体结构：返回对象、属性、关系摘要。"""
            rec = ontologies.get(ontology_id)
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
            rec = ontologies.get(ontology_id)
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
            instances = _load_instances(project_id, ontology_id)
            if object_type:
                instances = [i for i in instances if i.get("object_type") == object_type]
            if instance_id:
                instances = [i for i in instances if i.get("instance", {}).get("id") == instance_id or i.get("instance", {}).get("node_id") == instance_id]
            return json.dumps({"ontology_id": ontology_id, "count": len(instances), "instances": instances}, ensure_ascii=False)

        @mcp.tool()
        def action_invoke(action_id: str, confirmation_token: str = "") -> str:
            """确认执行 Action（需先通过 /api/actions/{id}/execute 获取 confirmation_token）。"""
            rec = actions.get(action_id)
            if not rec:
                return json.dumps({"error": "NOT_FOUND"}, ensure_ascii=False)
            result = self._action_engine.confirm(confirmation_token, {"action": rec})
            return json.dumps(result, ensure_ascii=False)

        return mcp

    def mount(self, app):
        """将 MCP SSE 端点按项目挂载到 FastAPI app（路由定义在 main.py）。"""
        logger.info("MCP Server 已挂载到 /mcp/{project_id} (SSE)，工具: %s", list(TOOLS_CONTRACT.keys()))

    async def handle_sse(self, request):
        """供 main.py /mcp/{project_id} GET 路由调用。"""
        project_id = request.path_params["project_id"]
        self._ensure_mcp(project_id)
        transport = self._transports.get(project_id)
        mcp = self._projects.get(project_id)
        if not transport or not mcp:
            body = json.dumps({"error": "MCP Server 未发布"}).encode("utf-8")
            await request.send({"type": "http.response.start", "status": 404, "headers": [[b"content-type", b"application/json"]]})
            return await request.send({"type": "http.response.body", "body": body})
        async with transport.connect_sse(request.scope, request.receive, request.send) as streams:
            await mcp._mcp_server.run(
                streams[0], streams[1],
                mcp._mcp_server.create_initialization_options(),
            )

    async def handle_messages(self, request):
        """供 main.py /mcp/{project_id}/messages/ POST 路由调用。"""
        project_id = request.path_params["project_id"]
        transport = self._transports.get(project_id)
        if not transport:
            body = json.dumps({"error": "MCP Server 未发布"}).encode("utf-8")
            await request.send({"type": "http.response.start", "status": 404, "headers": [[b"content-type", b"application/json"]]})
            await request.send({"type": "http.response.body", "body": body})
            return
        await transport.handle_post_message(request.scope, request.receive, request.send)


def _run_inference(ontology: dict[str, Any]) -> list[dict[str, Any]]:
    from reasoning import inference_trace as _inf
    return _inf(ontology)


def _load_instances(project_id: str, ontology_id: str) -> list[dict[str, Any]]:
    """读取该项目下指定本体的全部实例（数据源与审批入库一致：core.instance_store，按对象分文件）。"""
    from core.instance_store import get_instance_store
    try:
        records = get_instance_store(project_id).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取实例存储失败: project=%s err=%s", project_id, e)
        return []
    return [r for r in records if (r or {}).get("ontology_id") == ontology_id]


_mcp_server: "McpServer | None" = None


def get_mcp_server() -> McpServer:
    """进程内唯一 McpServer 实例（发布状态在内存中，必须单例共享）。"""
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = McpServer()
    return _mcp_server
