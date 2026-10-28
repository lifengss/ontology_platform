# -*- coding: utf-8 -*-
"""
mcp_stdio_bridge.py
企业本体平台 -> WorkBuddy 的本地 stdio MCP bridge（绕开连接器体系的 OAuth 认证）。

原理：WorkBuddy 以 stdio 子进程方式拉起本脚本，通过标准输入输出做 MCP 协议交互，
不经过连接器 UI 的「连接/授权（OAuth）」。本脚本内部用 HTTP 把工具调用透传到平台
`POST /mcp/{project_id}?token=...`（平台自带鉴权）。

用法（WorkBuddy mcp.json）：
{
  "mcpServers": {
    "成本核算模型": {
      "type": "stdio",
      "command": "C:\\\\Users\\\\lif_lc\\\\AppData\\\\Local\\\\Programs\\\\Python\\\\Python310\\\\python.exe",
      "args": ["-X", "utf8", "D:\\\\self_coding\\\\ontology_platform\\\\backend\\\\mcp_stdio_bridge.py",
               "--project", "<project_id>"],
      "timeout": 120000,
      "disabled": false
    }
  }
}
注意：不要写 env 字段（Node spawn env 为整体替换，写 env 会丢 SystemRoot，Windows 下
Python import asyncio 会抛 WinError 10106，宿主只显示"连接失败"无日志）。
本脚本 token 从平台 mcp_tokens.json 读，不依赖环境变量。
项目 ID 经 --project 参数传入（兜底环境变量 ONTOLOGY_PROJECT_ID），换项目只改 mcp.json 的 args。
"""
import os
import sys
import json
import http.client


def _parse_project_id() -> str:
    """项目 ID 从命令行读取（--project=<id> 或 --project <id>），
    其次环境变量 ONTOLOGY_PROJECT_ID，最后用默认值兜底。
    这样换项目只需改 WorkBuddy mcp.json 的 args，不必改本脚本。"""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a.startswith("--project="):
            return a.split("=", 1)[1]
        if a == "--project" and i + 1 < len(argv):
            return argv[i + 1]
    return os.environ.get("ONTOLOGY_PROJECT_ID", "5cd5ccc06b604ed7829f99983f52e963")


PROJECT_ID = _parse_project_id()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOK_PATH = os.path.join(BASE_DIR, "backend", "data", "projects", PROJECT_ID, "mcp_tokens.json")
HOST, PORT = "127.0.0.1", 8000


def _get_token() -> str:
    if not os.path.exists(TOK_PATH):
        raise RuntimeError("未找到平台 token 文件: %s" % TOK_PATH)
    d = json.load(open(TOK_PATH, encoding="utf-8"))
    rec = d[0] if isinstance(d, list) else d
    return rec.get("token", "")


class _PlatformClient:
    """对平台 Streamable HTTP MCP 的轻量客户端（每个调用内部完成握手）。"""

    def __init__(self):
        self.sid = None
        self.initialized = False

    def _post(self, body, first=False):
        token = _get_token()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.sid and not first:
            headers["mcp-session-id"] = self.sid
        c = http.client.HTTPConnection(HOST, PORT, timeout=60)
        try:
            c.request("POST", "/mcp/%s?token=%s" % (PROJECT_ID, token),
                      body=json.dumps(body).encode(), headers=headers)
            r = c.getresponse()
            raw = r.read().decode("utf-8", "ignore")
            sid = r.getheader("mcp-session-id") or r.getheader("Mcp-Session-Id")
            ct = r.getheader("content-type", "")
            status = r.status
        finally:
            c.close()
        if sid:
            self.sid = sid
        return status, self._parse(raw, ct)

    @staticmethod
    def _parse(raw, ct):
        if "text/event-stream" in ct.lower() or ("data:" in raw and "jsonrpc" in raw):
            data = "".join(l[5:].strip() for l in raw.replace("\r\n", "\n").split("\n")
                           if l.startswith("data:"))
            data = data or raw
            try:
                return json.loads(data)
            except Exception:
                return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _ensure_init(self):
        if self.initialized:
            return
        st, j = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "wb-stdio-bridge", "version": "1"}}}, first=True)
        if st != 200:
            raise RuntimeError("平台 initialize 失败 HTTP %s: %s" % (st, json.dumps(j, ensure_ascii=False)))
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialized = True

    def call(self, tool, args):
        self._ensure_init()
        st, j = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": tool, "arguments": args}})
        try:
            return j["result"]["content"][0]["text"]
        except Exception:
            return json.dumps(j, ensure_ascii=False)


from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    name="Enterprise Ontology Stdio Bridge",
    instructions="企业业务本体平台（维保服务产品成本模型）的本地 stdio 桥：可查询本体/实例/推理链路。",
)
_client = _PlatformClient()


@mcp.tool()
def ontology_query(ontology_id: str = "", query: str = "") -> str:
    """查询本体结构：返回对象、属性、关系摘要。ontology_id 接受本体名称或 32 位 ID；
    留空返回本项目已发布本体清单（ontology_id/name/object_count）。query 可填对象 id 或名称做子串过滤。"""
    return _client.call("ontology_query", {"ontology_id": ontology_id, "query": query})


@mcp.tool()
def instance_get(ontology_id: str, object_type: str = "", instance_id: str = "",
                 limit: int = 50, offset: int = 0, fields: str = "") -> str:
    """查询已落库实例（ABox）。ontology_id 接受本体名称或 32 位 ID。limit 默认 50/上限 200，offset 翻页，
    fields 指定属性名（逗号分隔，如 "attr_name,attr_age"）。"""
    return _client.call("instance_get", {
        "ontology_id": ontology_id, "object_type": object_type, "instance_id": instance_id,
        "limit": limit, "offset": offset, "fields": fields})


@mcp.tool()
def inference_trace(ontology_id: str) -> str:
    """返回 OWL2-RL 风格推理链路。ontology_id 接受本体名称或 32 位 ID。"""
    return _client.call("inference_trace", {"ontology_id": ontology_id})


@mcp.tool()
def action_invoke(action_id: str, confirmation_token: str = "") -> str:
    """确认执行 Action（需先通过 /api/actions/{id}/execute 获取 confirmation_token）。"""
    return _client.call("action_invoke", {"action_id": action_id, "confirmation_token": confirmation_token})


if __name__ == "__main__":
    if "--check" in sys.argv:
        import asyncio
        tools = asyncio.run(mcp.list_tools())
        print("CHECK OK, tools:", [t.name for t in tools])
        sys.exit(0)
    mcp.run(transport="stdio")
