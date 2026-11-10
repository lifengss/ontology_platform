"""mcp_server：由本体 Schema 按项目自动生成工具，双传输挂载 /mcp/{project_id}（VS6）。

工具契约（对外 Agent 可读）：
- ontology_query: 读本体 TBox，无副作用
- inference_trace: 读推理链路，无副作用
- instance_get: 读实例 ABox，无副作用（limit 默认 50 / 上限 200，offset 翻页，fields 投影）
- term_lookup: 数据字典联邦查询（词条解析→多外部系统并行查询→语义归一合并），无副作用；
  歧义/成本确认以结构化 JSON 返回（status=needs_disambiguation / cost_confirmation_required），
  词条不存在或全部系统失败 raise ToolError（TERM_NOT_FOUND / FEDERATED_QUERY_FAILED）
- action_invoke: 写外部系统，需确认令牌，有副作用

MCP 接入层修复（2026-10-28，docs/平台修改建议-MCP接入层修复.md）：
- P0-1 双传输：Streamable HTTP（2025-03-26/2025-06-18，推荐）+ SSE（2024-11-05 兼容）。
  Streamable 走原始 ASGI（McpStreamableMiddleware 拦截 POST/DELETE /mcp/{pid}、
  GET /mcp/{pid} 带 Mcp-Session-Id）；SSE 兼容路径 GET（Accept: text/event-stream 且无
  session 头）+ POST /mcp/{pid}/messages/ 保留。
- P0-2 连接即注册修复：只有"已发布"项目可连接，未发布 404 NOT_PUBLISHED（不再凭空注册）；
  Bearer token 或 ?token= 鉴权，缺失/无效 401 UNAUTHORIZED；内存驻留项目上限
  MCP_MAX_PROJECTS + 30 分钟空闲回收；Streamable 会话内置 30 分钟空闲超时（SDK 内建，
  过期后旧 session id 404，客户端重走 initialize）。
- P0-3 发布态持久化：JsonStore("mcp_published", project_id) 落盘，启动自动恢复，
  重启后发布态不丢；工具层校验发布态——未发布的 ontology_id 返回 isError NOT_PUBLISHED。
- P1-5 错误语义：业务错误一律 raise ToolError → MCP result isError=true，
  文本 "CODE: message"（NOT_FOUND/NOT_PUBLISHED/AMBIGUOUS/UNAUTHORIZED/INVALID_ARGUMENT/TOO_LARGE）。

依赖 mcp>=2（MCPServer，即原 FastMCP；低层 Server 属性 _lowlevel_server）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from collections import deque
from typing import Any
from urllib.parse import parse_qs

from action_engine import ActionEngine
from config import get_settings
from core.storage import JsonStore
from fastapi.responses import JSONResponse
from logging_setup import get_logger

logger = get_logger("mcp_server")

# ---- 接入层参数（P0-2 / P1-4） ----
MCP_MAX_PROJECTS = 100              # 内存驻留项目端点上限（超限淘汰最久空闲）
MCP_IDLE_TIMEOUT = 30 * 60          # 项目端点空闲回收（秒）；发布态在磁盘，随时可再建
MCP_SESSION_IDLE_TIMEOUT = 30 * 60  # Streamable 会话空闲超时（SDK 内建，过期 404）
MCP_MAX_SESSIONS = 100              # 单项目 Streamable 并发会话上限
MAX_SSE_SESSIONS = 200              # SSE 兼容路径全局会话引用上限（超了丢最旧引用）
INSTANCE_DEFAULT_LIMIT = 50         # instance_get 默认条数（P1-4）
INSTANCE_MAX_LIMIT = 200            # instance_get 单页上限（P1-4）

# ---- 错误码（P1-5）：工具错误文本统一 "CODE: message" ----
ERR_NOT_FOUND = "NOT_FOUND"
ERR_NOT_PUBLISHED = "NOT_PUBLISHED"
ERR_AMBIGUOUS = "AMBIGUOUS"
ERR_UNAUTHORIZED = "UNAUTHORIZED"
ERR_INVALID_ARGUMENT = "INVALID_ARGUMENT"
ERR_TOO_LARGE = "TOO_LARGE"
ERR_TERM_NOT_FOUND = "TERM_NOT_FOUND"            # term_lookup：数据字典无匹配词条（不猜测）
ERR_FEDERATED_FAILED = "FEDERATED_QUERY_FAILED"  # term_lookup：全部覆盖系统查询失败
ERR_CAPABILITY_FAILED = "CAPABILITY_FAILED"      # capability_call：能力执行/定义业务失败

SESSION_ID_HEADER = "mcp-session-id"

# P-02 借鉴补丁：对外工具契约元数据（外部 Agent 可静态判断读写/副作用）
TOOLS_CONTRACT = {
    "ontology_query": {
        "reads": ["本体TBox", "实例ABox"], "writes": [],
        "preconditions": ["本体已加载且非空", "本体已发布"],
        "postconditions": ["仅返回被授权命名图内的结果"], "side_effect": False,
    },
    "inference_trace": {
        "reads": ["推理结论", "推导路径"], "writes": [],
        "preconditions": ["已执行过推理快照", "本体已发布"],
        "postconditions": ["返回可追溯的推导链"], "side_effect": False,
    },
    "instance_get": {
        "reads": ["实例ABox"], "writes": [],
        "preconditions": ["本体已发布"],
        "postconditions": ["返回实例及其溯源标记（含外部MCP来源着色）"], "side_effect": False,
    },
    "action_invoke": {
        "reads": ["Action定义", "目标外部系统"], "writes": ["外部系统", "本地执行日志"],
        "preconditions": ["Action 已定义", "本次执行已人工确认"],
        "postconditions": ["幂等键已写入", "执行结果已留痕"], "side_effect": True,
    },
    "term_lookup": {
        "reads": ["数据字典", "外部系统（经已配置 MCP 连接并行只读查询）"], "writes": [],
        "preconditions": ["数据字典存在可用词条（或同义词）"],
        "postconditions": ["查询审计已留痕（term_lookup_audit）"], "side_effect": False,
    },
    "capability_call": {
        "reads": ["能力注册表", "实例ABox"],
        "writes": ["实例ABox（output_attribute + __capability__ 元数据，dry_run 时不写）"],
        "preconditions": ["能力已发布(active)；实例存在且对象类型与能力目标一致"],
        "postconditions": ["返回四要素溯源（能力版本+输入实例+lookup命中原始值+计算值）"],
        "side_effect": True,
    },
}


def _err(code: str, message: str):
    """构造 ToolError（mcp SDK 捕获后转 CallToolResult(isError=true)，文本 "CODE: message"）。"""
    from mcp.server.mcpserver.exceptions import ToolError
    return ToolError(f"{code}: {message}")


def _asgi_send(request):
    """从 Starlette Request 取原始 ASGI send 可调用对象（供 mcp SDK 传输层使用）。

    uvicorn/hypercorn 将 send 放在 scope["asgi"]["send"]；
    部分 ASGI 栈（如 TestClient）改挂在 Request._send 上，做兜底。
    """
    asgi = request.scope.get("asgi") or {}
    send = asgi.get("send")
    if send is None:
        send = getattr(request, "_send", None)
    if send is None:
        raise RuntimeError("无法获取 ASGI send（SSE 传输不可用）")
    return send


def _scope_headers(scope: dict) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


async def _asgi_json(scope, send, status: int, payload: dict) -> None:
    """原始 ASGI 直接回一个 JSON 响应（鉴权/未发布等 4xx，P0-2）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class _McpEndpoint:
    """单项目的 MCP 端点：MCPServer 实例 + Streamable HTTP manager（常驻任务，P0-1）。"""

    def __init__(self, owner: "McpServer", project_id: str) -> None:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        self.owner = owner
        self.project_id = project_id
        self.last_seen = time.monotonic()
        self.mcp = MCPServer(
            f"EnterpriseOntologyMCP_{project_id}",
            instructions=f"企业业务本体平台 MCP Server（项目 {project_id}）：可查询本体/实例/推理链路，或在确认后调用 Action 回写外部系统。",
        )
        self.manager = StreamableHTTPSessionManager(
            app=self.mcp._lowlevel_server,
            stateless=False,
            session_idle_timeout=MCP_SESSION_IDLE_TIMEOUT,
            max_sessions=MCP_MAX_SESSIONS,
        )
        self._register_tools(self.mcp)
        self._task: asyncio.Task | None = None
        self.started = False

    def _register_tools(self, m) -> None:
        _register_tools(self.owner, self.project_id, m)

    def start(self) -> None:
        async def _run() -> None:
            try:
                async with self.manager.run():
                    self.started = True
                    await asyncio.Event().wait()  # 常驻直至 stop()
            except asyncio.CancelledError:
                self.started = False
                raise
            except Exception:  # noqa: BLE001
                self.started = False
                logger.exception("MCP streamable manager 启动失败 project_id=%s", self.project_id)
        self._task = asyncio.create_task(_run(), name=f"mcp-streamable-{self.project_id}")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        self.started = False


class McpStreamableMiddleware:
    """原始 ASGI 中间件（P0-1）：把 Streamable HTTP 流量分流到对应项目的 manager。

    拦截规则（其余全部透传给 FastAPI）：
    - POST /mcp/{pid}、DELETE /mcp/{pid} → Streamable（旧 SSE 客户端只 POST /mcp/{pid}/messages/，不冲突）
    - GET /mcp/{pid} 且带 Mcp-Session-Id 头 → Streamable 会话 SSE 流
    - GET /mcp/{pid} 无 session 头 → 透传（FastAPI 走 SSE 兼容路径或 405）
    """

    _PATH = re.compile(r"^/mcp/([^/]+)/?$")
    _MSG_PATH = re.compile(r"^/mcp/([^/]+)/messages/?$")

    def __init__(self, app, mcp_server: "McpServer") -> None:
        self.app = app
        self.mcp_server = mcp_server

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http":
            path = scope.get("path", "")
            method = scope.get("method", "")
            # legacy SSE 的 POST 消息端点：SDK 自行通过原始 send 回 202/4xx，
            # 必须在本层直通，避免 FastAPI 函数路由再补发一次隐式响应。
            mm = self._MSG_PATH.match(path)
            if mm and method == "POST":
                await self.mcp_server.handle_messages_asgi(mm.group(1), scope, receive, send)
                return
            m = self._PATH.match(path)
            if m:
                headers = _scope_headers(scope)
                streamable = method in ("POST", "DELETE") or (
                    method == "GET" and SESSION_ID_HEADER in headers)
                if streamable:
                    await self.mcp_server.handle_streamable_request(m.group(1), scope, receive, send)
                    return
                # legacy SSE GET：长连接响应由 SDK 直接发，绕开 FastAPI/BaseHTTPMiddleware
                if method == "GET" and "text/event-stream" in headers.get("accept", ""):
                    await self.mcp_server.handle_sse_asgi(m.group(1), scope, receive, send)
                    return
        await self.app(scope, receive, send)


class McpServer:
    """按项目维护独立端点（惰性创建），发布态持久化（P0-3），token 鉴权（P0-2）。

    发布态以磁盘 JsonStore("mcp_published") 为准；_endpoints 只是内存缓存，
    30 分钟空闲即回收（sweeper），下次连接按需重建。
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._published_ontologies: dict[str, list[str]] = {}  # project_id -> [ontology_id, ...]
        self._endpoints: dict[str, _McpEndpoint] = {}
        self._sse_sessions: deque = deque()  # (project_id, SseServerTransport)，有界引用
        self._action_engine = ActionEngine()
        self._sweep_task: asyncio.Task | None = None
        self._data_dir = Path(settings.data_dir) / "projects"
        self._load_published()

    # ---------- 发布态持久化（P0-3） ----------

    def _published_store(self, project_id: str) -> JsonStore:
        return JsonStore("mcp_published", project_id=project_id)

    def _load_published(self) -> None:
        """启动时从 data/projects/*/mcp_published.json 恢复发布态（重启不丢）。"""
        if not self._data_dir.exists():
            return
        for pdir in sorted(self._data_dir.iterdir()):
            if not pdir.is_dir():
                continue
            try:
                recs = [r for r in JsonStore("mcp_published", project_id=pdir.name).all() if r.get("ontology_id")]
            except Exception as e:  # noqa: BLE001
                logger.warning("恢复 MCP 发布态失败 project=%s: %s", pdir.name, e)
                continue
            if recs:
                self._published_ontologies[pdir.name] = [r["ontology_id"] for r in recs]
                logger.info("恢复 MCP 发布态 project=%s ontologies=%d", pdir.name, len(recs))

    def _persist_published(self, project_id: str) -> None:
        store = self._published_store(project_id)
        for rec in store.all():
            store.delete(rec["id"])
        for oid in self._published_ontologies.get(project_id, []):
            orec = self._ontologies(project_id).get(oid) or {}
            store.insert({"id": oid, "ontology_id": oid, "name": orec.get("name") or oid,
                          "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    # ---------- token 鉴权（P0-2） ----------

    def _token_store(self, project_id: str) -> JsonStore:
        return JsonStore("mcp_tokens", project_id=project_id)

    def _token_record(self, project_id: str) -> dict | None:
        return self._token_store(project_id).get("default")

    def generate_token(self, project_id: str) -> dict:
        """生成/轮换项目 MCP 接入 token（旧 token 立即失效）。"""
        token = secrets.token_urlsafe(32)
        rec = {"id": "default", "token": token, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if self._token_store(project_id).get("default"):
            self._token_store(project_id).update("default", rec)
        else:
            self._token_store(project_id).insert(rec)
        logger.info("generate MCP token project_id=%s", project_id)
        return rec

    def revoke_token(self, project_id: str) -> dict:
        deleted = self._token_store(project_id).delete("default")
        logger.info("revoke MCP token project_id=%s deleted=%s", project_id, deleted)
        return {"revoked": deleted}

    def token_status(self, project_id: str) -> dict:
        rec = self._token_record(project_id)
        return {"configured": bool(rec), "token": rec.get("token") if rec else None,
                "created_at": rec.get("created_at") if rec else None}

    def _token_ok(self, project_id: str, headers: dict[str, str], query_string: bytes) -> bool:
        rec = self._token_record(project_id)
        if not rec:
            return False
        expected = rec.get("token", "")
        candidate = None
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            candidate = auth[7:].strip()
        if not candidate and query_string:
            q = parse_qs(query_string.decode("utf-8", "replace"))
            if q.get("token"):
                candidate = q["token"][0]
        return bool(candidate) and secrets.compare_digest(candidate, expected)

    # ---------- 端点生命周期（P0-1/P0-2） ----------

    def _ensure_endpoint(self, project_id: str) -> _McpEndpoint:
        ep = self._endpoints.get(project_id)
        if ep is not None:
            ep.last_seen = time.monotonic()
            return ep
        # 超限淘汰最久空闲（发布态在磁盘，淘汰无副作用）
        if len(self._endpoints) >= MCP_MAX_PROJECTS:
            oldest = min(self._endpoints.values(), key=lambda e: e.last_seen)
            if oldest is not ep:
                self._evict(oldest.project_id)
        ep = _McpEndpoint(self, project_id)
        ep.start()
        self._endpoints[project_id] = ep
        return ep

    def _evict(self, project_id: str) -> None:
        ep = self._endpoints.pop(project_id, None)
        if ep is not None:
            logger.info("evict MCP endpoint project_id=%s", project_id)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # 同步上下文（同步路由 handler 跑在线程池，无运行中事件循环）：
                # task.cancel() 线程安全、取消在其所属循环上生效，
                # 避免 create_task 抛 "no running event loop"、stop 协程永不 await。
                task, ep._task = ep._task, None
                if task is not None and not task.done():
                    task.cancel()
                ep.started = False
                return
            asyncio.create_task(ep.stop())

    def start_sweeper(self) -> None:
        """启动空闲回收后台任务（main.py startup 调用）。"""
        if self._sweep_task and not self._sweep_task.done():
            return
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="mcp-idle-sweeper")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for pid, ep in list(self._endpoints.items()):
                if now - ep.last_seen > MCP_IDLE_TIMEOUT:
                    self._evict(pid)

    # ---------- 连接处理（P0-1/P0-2） ----------

    def _ontologies(self, project_id: str) -> JsonStore:
        return JsonStore("ontologies", project_id=project_id)

    def _actions(self, project_id: str) -> JsonStore:
        return JsonStore("actions", project_id=project_id)

    def _not_published(self) -> JSONResponse:
        return JSONResponse({"error": ERR_NOT_PUBLISHED,
                             "detail": "项目未发布。请先在 MCP 接入页发布本体"}, status_code=404)

    def _unauthorized(self) -> JSONResponse:
        return JSONResponse({"error": ERR_UNAUTHORIZED,
                             "detail": "缺少或无效的 MCP 接入 token（Authorization: Bearer <token> 或 ?token=）"},
                            status_code=401)

    async def handle_sse(self, request):
        """SSE 兼容路径（2024-11-05）：FastAPI 路由入口（保留兼容，正常流量由中间件直通）。"""
        await self._run_sse(request.path_params["project_id"], request.scope,
                            request.receive, _asgi_send(request))

    async def handle_sse_asgi(self, project_id: str, scope, receive, send) -> None:
        """SSE GET 原始 ASGI 单响应入口：SDK connect_sse 自行发响应，框架不补发。"""
        await self._run_sse(project_id, scope, receive, send)

    async def _run_sse(self, project_id: str, scope, receive, send) -> None:
        """P0-2：不再"连接即注册"——未发布 404 NOT_PUBLISHED，未鉴权 401 UNAUTHORIZED。"""
        if not self._token_ok(project_id, _scope_headers(scope), scope.get("query_string", b"")):
            await self._unauthorized()(scope, receive, send)
            return
        if project_id not in self._published_ontologies:
            await self._not_published()(scope, receive, send)
            return
        self._ensure_endpoint(project_id)
        from mcp.server.sse import SseServerTransport
        transport = SseServerTransport(f"/mcp/{project_id}/messages/")
        # 有界引用：超限丢最旧（连接本身在客户端断开时由 SDK 自行收尾）
        self._sse_sessions.append((project_id, transport))
        while len(self._sse_sessions) > MAX_SSE_SESSIONS:
            self._sse_sessions.popleft()
        ep = self._endpoints[project_id]
        async with transport.connect_sse(scope, receive, send) as streams:
            await ep.mcp._lowlevel_server.run(
                streams[0], streams[1],
                ep.mcp._lowlevel_server.create_initialization_options(),
            )

    async def handle_messages(self, request):
        """SSE 兼容路径：POST /mcp/{project_id}/messages/（P0-2 同样先鉴权+发布校验）。"""
        project_id = request.path_params["project_id"]
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not self._token_ok(project_id, headers, request.url.query.encode()):
            return self._unauthorized()
        if project_id not in self._published_ontologies:
            return self._not_published()
        self._ensure_endpoint(project_id)
        # SSE 消息路由到对应 session：遍历该项目仍存活的 transport（session 由 SDK 按
        # session_id query 参数路由；旧 transport 引用已丢的会自然 404，客户端重连即可）
        from mcp.server.sse import SseServerTransport  # noqa: F401  (类型语义)
        candidates = [t for (pid, t) in self._sse_sessions if pid == project_id]
        if not candidates:
            return self._not_published()
        for transport in reversed(candidates):
            try:
                await transport.handle_post_message(request.scope, request.receive, _asgi_send(request))
                return
            except Exception:  # noqa: BLE001  (session 不匹配/已关闭 → 试下一个)
                continue
        return JSONResponse({"error": ERR_NOT_FOUND, "detail": "SSE 会话不存在或已过期，请重新连接"}, status_code=404)

    async def handle_messages_asgi(self, project_id: str, scope, receive, send) -> None:
        """SSE 兼容 POST 消息端点（原始 ASGI 单响应路径）。

        SDK ``handle_post_message`` 会自行经 ``send`` 发 202/4xx；若再落到 FastAPI
        函数路由，框架会补发一次隐式响应 → ASGI 双响应（BaseHTTPMiddleware
        "Response content longer than Content-Length" / body_stream AssertionError）。
        本方法由最外层 McpStreamableMiddleware 直通调用，响应仅一份。
        """
        query = scope.get("query_string", b"")
        if not self._token_ok(project_id, _scope_headers(scope), query):
            await _asgi_json(scope, send, 401, {"error": ERR_UNAUTHORIZED,
                                                "detail": "缺少或无效的 MCP 接入 token"})
            return
        if project_id not in self._published_ontologies:
            await _asgi_json(scope, send, 404, {"error": ERR_NOT_PUBLISHED,
                                                "detail": "项目未发布。请先在 MCP 接入页发布本体"})
            return
        self._ensure_endpoint(project_id)
        candidates = [t for (pid, t) in self._sse_sessions if pid == project_id]
        if not candidates:
            await _asgi_json(scope, send, 404, {"error": ERR_NOT_FOUND,
                                                "detail": "SSE 会话不存在或已过期，请重新连接"})
            return
        # SDK 按 query 中的 session_id 自行路由；响应由 SDK 发出，本层不再补发。
        try:
            await candidates[-1].handle_post_message(scope, receive, send)
        except Exception:  # noqa: BLE001
            # 多数异常发生在 SDK 已发响应之后，再发一次会构成双响应，仅记录日志。
            logger.exception("SSE messages 端点处理失败 project_id=%s", project_id)

    async def handle_streamable_request(self, project_id: str, scope, receive, send) -> None:
        """Streamable HTTP 分发（P0-1）：POST/DELETE /mcp/{pid}、GET /mcp/{pid}（带 session 头）。

        原始 ASGI 直通 manager（保留 send 可调用对象，FastAPI 函数路由拿不到）。
        """
        if not self._token_ok(project_id, _scope_headers(scope), scope.get("query_string", b"")):
            await _asgi_json(scope, send, 401, {"error": ERR_UNAUTHORIZED,
                                                "detail": "缺少或无效的 MCP 接入 token"})
            return
        if project_id not in self._published_ontologies:
            await _asgi_json(scope, send, 404, {"error": ERR_NOT_PUBLISHED,
                                                "detail": "项目未发布。请先在 MCP 接入页发布本体"})
            return
        ep = self._ensure_endpoint(project_id)
        if not ep.started:
            await _asgi_json(scope, send, 503, {"error": "MCP 端点启动中，请稍后重试"})
            return
        try:
            await ep.manager.handle_request(scope, receive, send)
        except Exception as e:  # noqa: BLE001
            logger.exception("streamable request 处理失败 project_id=%s path=%s", project_id, scope.get("path"))
            if scope.get("type") == "http":
                import contextlib
                with contextlib.suppress(Exception):
                    await _asgi_json(scope, send, 500, {"error": "INTERNAL", "detail": str(e)})

    # ---------- 发布 / 取消发布（P0-3：落盘） ----------

    def _ontology_names(self, project_id: str, ontology_ids: list[str]) -> list[dict]:
        store = self._ontologies(project_id)
        items = []
        for oid in ontology_ids:
            rec = store.get(oid)
            items.append({"ontology_id": oid, "name": (rec or {}).get("name") or oid})
        return items

    def publish(self, project_id: str, ontology_id: str) -> dict:
        rec = self._ontologies(project_id).get(ontology_id)
        if not rec:
            return {"published": False, "error": "本体不存在"}
        ids = self._published_ontologies.setdefault(project_id, [])
        if ontology_id not in ids:
            ids.append(ontology_id)
        self._persist_published(project_id)  # P0-3：落盘，重启不丢
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
        ids = self._published_ontologies.get(project_id, [])
        if ontology_id:
            if project_id not in self._published_ontologies or not ids:
                return {"published": False, "error": "MCP Server 未发布"}
            if ontology_id in ids:
                ids.remove(ontology_id)
            if not ids:
                self._published_ontologies.pop(project_id, None)
                self._persist_published(project_id)
                self._evict(project_id)
            else:
                self._persist_published(project_id)
            logger.info("unpublish MCP ontology project_id=%s ontology_id=%s remaining=%d", project_id, ontology_id, len(ids))
            return {"published": bool(self._published_ontologies.get(project_id)), "project_id": project_id,
                    "ontology_id": ontology_id, "message": "已取消发布该本体"}
        if project_id not in self._published_ontologies:
            return {"published": False, "error": "MCP Server 未发布"}
        self._published_ontologies.pop(project_id, None)
        self._persist_published(project_id)
        self._evict(project_id)
        logger.info("unpublish MCP server project_id=%s", project_id)
        return {"published": False, "project_id": project_id, "message": "已取消发布"}

    # ---------- 状态 / 工具元数据（P0-1：transports 与协议版本可见） ----------

    TRANSPORTS = ["streamable-http", "sse"]
    PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

    def status(self, project_id: str | None = None) -> dict:
        if project_id:
            return {
                "published": project_id in self._published_ontologies,
                "endpoint": f"/mcp/{project_id}",
                "messages_endpoint": f"/mcp/{project_id}/messages/",
                "transports": self.TRANSPORTS,
                "protocol_versions": self.PROTOCOL_VERSIONS,
                "tools": list(TOOLS_CONTRACT.keys()),
                "published_ontologies": self._ontology_names(project_id, self._published_ontologies.get(project_id, [])),
                "token": self.token_status(project_id),
                "resident": project_id in self._endpoints,
            }
        return {
            "published_projects": [
                {
                    "project_id": pid,
                    "endpoint": f"/mcp/{pid}",
                    "published_ontologies": self._ontology_names(pid, self._published_ontologies.get(pid, [])),
                    "token_configured": bool(self._token_record(pid)),
                }
                for pid in self._published_ontologies
            ],
            "transports": self.TRANSPORTS,
            "protocol_versions": self.PROTOCOL_VERSIONS,
            "tools": list(TOOLS_CONTRACT.keys()),
        }

    def published_ids(self, project_id: str) -> list[str]:
        """已发布本体清单（公开只读，供 MCP 列表接口使用）。"""
        return list(self._published_ontologies.get(project_id, []))

    def server_active(self, project_id: str) -> bool:
        """项目 MCP 是否已发布（P0-3：以持久化清单为准，不再等于"内存里有实例"）。"""
        return project_id in self._published_ontologies

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
            "term_lookup": {"method": "tools/call", "params": {"name": "term_lookup", "arguments": {"term": "客户", "value": "C0001", "dry_run": True}}},
            "capability_call": {"method": "tools/call", "params": {"name": "capability_call", "arguments": {"capability": "cap_onsite_rate_d3", "instance_id": "<record_id>", "dry_run": True}}},
        }
        return examples.get(tool_name, {})

    def mount(self, app):
        """将 MCP 端点挂载到 FastAPI app（路由定义在 main.py；Streamable 走 McpStreamableMiddleware）。"""
        logger.info("MCP Server 已挂载 /mcp/{project_id}（streamable-http + sse 兼容），工具: %s", list(TOOLS_CONTRACT.keys()))


def _resolve_ontology_id(owner: "McpServer", project_id: str, identifier: str) -> str:
    """P0-1：把调用方传入的 identifier（精确本体 ID / 本体名称）解析成真实 ontology_id。

    规则：先精确 ID 匹配；失败再按本体 name 匹配（去首尾空格 + 小写归一）；
    都失败抛 NOT_FOUND（语义：没有这个本体/名称，区别于 NOT_PUBLISHED=本体存在但未发布）。
    """
    identifier = (identifier or "").strip()
    records = owner._ontologies(project_id).all()
    by_id = {o.get("id"): o for o in records}
    if identifier in by_id:
        return identifier
    norm = identifier.lower()
    for o in records:
        if (o.get("name") or "").strip().lower() == norm:
            return o.get("id")
    avail = "、".join(f"{o.get('id')}（{o.get('name') or ''}）" for o in records[:10])
    more = f" 等 {len(records)} 个" if len(records) > 10 else ""
    raise _err(ERR_NOT_FOUND,
               f"本体 '{identifier}' 不存在。本项目本体：{avail or '（无）'}{more}。"
               "可用本体名称或 32 位 ID 重试；ontology_query 不传 ontology_id 可列出已发布本体。")


def _normalize_fields(fields) -> list[str]:
    """P1：fields 容错双形态——逗号分隔字符串（与 stdio bridge 一致）或数组；空串/None → []。

    背景：旧 `list[str] | None` 签名下，客户端把未提供/空的 fields 序列化成 ''（str），
    pydantic 校验直接报 list_type，工具一调用就不可用；统一为字符串优先 + 数组兼容。
    """
    if fields is None:
        return []
    if isinstance(fields, (list, tuple, set)):
        return [str(f).strip() for f in fields if str(f).strip()]
    return [f.strip() for f in str(fields).split(",") if f.strip()]


def _require_published(owner: "McpServer", project_id: str, ontology_id: str) -> None:
    """P0-3：工具层发布态校验——未发布的 ontology_id 拒绝为 isError NOT_PUBLISHED。

    调用前置 _resolve_ontology_id，故此处 ontology_id 必为已存在的本体；
    文案带出已发布清单（id+名称），避免"参数传错"被误读为"项目未发布"。
    """
    if ontology_id not in owner._published_ontologies.get(project_id, ()):
        published = owner._published_ontologies.get(project_id, [])
        names = {o.get("id"): (o.get("name") or "") for o in owner._ontologies(project_id).all()}
        listing = "；".join(f"{oid}（{names.get(oid, '')}）" for oid in published) or "（无，本项目未发布任何本体）"
        raise _err(ERR_NOT_PUBLISHED,
                   f"本体 {ontology_id}（{names.get(ontology_id, '')}）未发布。"
                   f"本项目已发布本体：{listing}。请先在 MCP 接入页发布，或改用上述已发布本体。")


def _register_tools(owner: "McpServer", project_id: str, m) -> None:
    """注册对外工具（契约签名保持稳定；P0-3 发布校验 + P1-4 分页 + P1-5 错误语义）。"""
    ontologies = owner._ontologies(project_id)
    actions = owner._actions(project_id)

    @m.tool()
    def ontology_query(ontology_id: str = "", query: str = "") -> str:
        """查询本体结构：返回对象、属性、关系摘要。ontology_id 接受本体名称或 32 位 ID（先名称解析再校验发布态）；
        留空则返回本项目已发布本体清单（ontology_id/name/object_count），供先枚举再精确查询。
        query 可填对象 id 或名称做子串过滤（可选）。
        """
        oid = (ontology_id or "").strip()
        if not oid:
            # P2-①：空 id 枚举已发布本体（防外部 Agent 猜 ID）
            published = owner._published_ontologies.get(project_id, [])
            by_id = {o.get("id"): o for o in owner._ontologies(project_id).all()}
            items = [
                {
                    "ontology_id": poid,
                    "name": (by_id.get(poid) or {}).get("name") or poid,
                    "object_count": len((by_id.get(poid) or {}).get("objects") or []),
                }
                for poid in published
            ]
            return json.dumps({
                "published": bool(published),
                "count": len(items),
                "ontologies": items,
                "hint": "从清单取 ontology_id（或本体名称）后可精确查询结构/实例。",
            }, ensure_ascii=False)
        oid = _resolve_ontology_id(owner, project_id, oid)
        _require_published(owner, project_id, oid)
        rec = ontologies.get(oid) or {}
        objects = rec.get("objects", [])
        if query:
            objects = [o for o in objects if query in (o.get("id") or "") or query in (o.get("name") or "")]
        summary = {
            "ontology_id": oid,
            "name": rec.get("name"),
            "objects": [
                {
                    "id": o["id"],
                    "name": o.get("name"),
                    "attribute_count": len(o.get("attributes", [])),
                    "relation_count": len(o.get("relations", [])),
                }
                for o in objects
            ],
        }
        return json.dumps(summary, ensure_ascii=False)

    @m.tool()
    def inference_trace(ontology_id: str) -> str:
        """返回 OWL2-RL 风格推理链路。ontology_id 接受本体名称或 32 位 ID。"""
        oid = _resolve_ontology_id(owner, project_id, ontology_id)
        _require_published(owner, project_id, oid)
        rec = ontologies.get(oid) or {}
        try:
            steps = _run_inference(rec)
            return json.dumps({"ontology_id": oid, "steps": steps}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            raise _err(ERR_NOT_FOUND, f"推理链路生成失败: {e}")

    @m.tool()
    def instance_get(ontology_id: str, object_type: str = "", instance_id: str = "",
                     limit: int = 50, offset: int = 0, fields: str | list[str] = "") -> str:
        """查询已落库实例（ABox）。ontology_id 接受本体名称或 32 位 ID。
        limit 默认 50/上限 200，offset 翻页，fields 为逗号分隔字符串或数组的属性名，做投影
        （如 "attr_name,attr_age"）。

        返回 total/has_more；结果被截断时会提示用 object_type 过滤或 offset 取余下数据。
        """
        oid = _resolve_ontology_id(owner, project_id, ontology_id)
        _require_published(owner, project_id, oid)
        lim = max(1, min(int(limit or INSTANCE_DEFAULT_LIMIT), INSTANCE_MAX_LIMIT))
        off = max(0, int(offset or 0))
        field_list = _normalize_fields(fields)
        records = _load_instances(project_id, oid)
        if object_type:
            records = [r for r in records if (r or {}).get("object_type") == object_type]
        if instance_id:
            records = [r for r in records
                       if instance_id in (((r or {}).get("instance") or {}).get("id"),
                                           ((r or {}).get("instance") or {}).get("node_id"))]
        total = len(records)
        page = records[off: off + lim]
        if field_list:
            page = [dict(r) for r in page]
            for r in page:
                inst = r.get("instance") or {}
                r["instance"] = {k: inst[k] for k in field_list if k in inst}
        has_more = off + len(page) < total
        payload: dict[str, Any] = {
            "ontology_id": oid,
            "total": total,
            "offset": off,
            "limit": lim,
            "has_more": has_more,
            "count": len(page),
            "instances": page,
        }
        if has_more and not (object_type or instance_id):
            payload["hint"] = (f"共 {total} 条，本次返回 {off + 1}-{off + len(page)} 条。"
                               "建议加 object_type 缩小范围，或用 offset 取余下数据。")
        return json.dumps(payload, ensure_ascii=False)

    @m.tool()
    def action_invoke(action_id: str, confirmation_token: str = "") -> str:
        """确认执行 Action（需先通过 /api/actions/{id}/execute 获取 confirmation_token）。"""
        rec = actions.get(action_id)
        if not rec:
            raise _err(ERR_NOT_FOUND, f"Action {action_id} 不存在")
        result = owner._action_engine.confirm(confirmation_token, {"action": rec})
        return json.dumps(result, ensure_ascii=False)

    @m.tool()
    def term_lookup(term: str, value: str = "", systems: str = "", limit: int = 50,
                   dry_run: bool = False, confirm_cost: bool = False) -> str:
        """数据字典联邦查询（M1-A / V2-01）。把输入词解析成标准词条（精确→同义词→确定性
        语义近似，不猜测），再按各外部系统自己的字段名并行只读查询，语义归一后合并。

        term 业务词或同义词（如"客户"）；value 待查键值（如客户编码）；
        systems 可选，逗号分隔的连接 id 或连接显示名（缺省=全部覆盖系统）；
        limit 每系统最多返回行数（默认 50 / 上限 500）；
        dry_run=true 仅预览成本（cost_estimate），不发起外呼；
        结果 status=cost_confirmation_required 时，确认后以 confirm_cost=true 重调（或放弃）；
        结果 status=needs_disambiguation 时，从 candidates（含置信度）交人工选择，
        确认后可将所选晋升为同义词再查（届时走精确匹配）。
        同 key 值冲突并存并附来源标记，权威来源取权威值；未覆盖系统显式列入 uncovered；
        全过程写入审计（term_lookup_audit）。
        """
        from dictionary import federated_lookup
        res = federated_lookup(project_id, term, value=value, systems=systems,
                               limit=limit, dry_run=dry_run, confirm_cost=confirm_cost,
                               actor="mcp")
        status = res.get("status")
        if status == "term_not_found":
            raise _err(ERR_TERM_NOT_FOUND,
                       res.get("hint") or "数据字典中未找到匹配词条（显式未覆盖，不猜测）")
        if status == "error":
            per = res.get("per_system") or {}
            detail = "；".join(f"{s}: {r.get('error') or r.get('status')}"
                               for s, r in per.items()) or res.get("error") or "无已覆盖系统"
            raise _err(ERR_FEDERATED_FAILED, f"全部覆盖系统查询失败：{detail}")
        return json.dumps(res, ensure_ascii=False)

    @m.tool()
    def capability_call(capability: str, instance_id: str = "", dry_run: bool = False) -> str:
        """执行已发布(active)能力（M1-B / V2-04 首片，派生型 expression 实现）。
        结果含四要素溯源：能力版本 + 输入实例 + lookup 命中（record_id/编码/原始值）+ 计算值。

        capability 能力 name 或 id；留空则返回本项目已发布能力清单（供先枚举再执行）。
        instance_id 目标实例 record_id（能力 object_type 下的实例）；
        dry_run=true 不写回实例（试算）。
        仅 active 能力可经 MCP 执行，draft 返回 FORBIDDEN_DRAFT 错误。
        """
        from capabilities import CapabilityApiError, CapabilityService

        svc = CapabilityService(project_id)
        name_or_id = (capability or "").strip()
        if not name_or_id:
            items = [
                {"name": r["name"], "id": r["id"], "version": r.get("version"),
                 "object_type": r.get("object_type"), "output_attribute": r.get("output_attribute")}
                for r in svc.list_capabilities(status="active")
            ]
            return json.dumps({"published": bool(items), "count": len(items),
                               "capabilities": items,
                               "hint": "从清单取 name 后以 capability_call(name, instance_id) 执行。"},
                              ensure_ascii=False)
        rec = svc.get_by_name(name_or_id)
        if rec is None:
            rec = svc.get_capability(name_or_id)  # 按 id 查
        if rec is None:
            raise _err(ERR_NOT_FOUND, f"能力不存在: {name_or_id}")
        try:
            res = svc.execute(rec["id"], instance_id, channel="mcp",
                              trigger_source="mcp", mcp_only=True, dry_run=dry_run)
        except CapabilityApiError as e:
            raise _err(ERR_CAPABILITY_FAILED,
                       f"{e.code}: {e.message} (http_status={e.http_status})")
        return json.dumps(res, ensure_ascii=False)


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
    """进程内唯一 McpServer 实例（发布态单例共享；内存端点可随时回收重建，P0-3 持久化兜底）。"""
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = McpServer()
    return _mcp_server
