"""企业业务本体平台 — FastAPI 应用入口（VS0 运行基座）。

- 挂载所有 /api 路由
- 统一响应信封与 ApiError 异常处理器（强制规则 7）
- /health 健康检查（后端 + 依赖可达性）
- 结构化请求日志（JSON 行）
- /mcp（MCP Server 挂载点）在 VS6 切片由 mcp_server 模块接入
"""
from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.router_actions import router as actions_router
from api.router_graph import router as graph_router
from api.router_ingest import router as ingest_router
from api.router_llm import router as llm_router
from api.router_mcp import router as mcp_router
from api.router_namespaces import router as namespaces_router
from api.router_ontologies import router as ontologies_router
from api.router_projects import router as projects_router
from api.router_system import router as system_router
from api.router_templates import router as templates_router
from config import get_settings
from core.responses import ApiError, fail, success
from jena_adapter.client import health as jena_health
from logging_setup import get_logger, log_event
from mcp_server import McpStreamableMiddleware, get_mcp_server
from neo4j_sync import Neo4jSync

settings = get_settings()
logger = get_logger("api.main")

app = FastAPI(title=settings.app_name, version=settings.version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    start = time.time()
    req_id = uuid.uuid4().hex[:8]
    log_event(logger, 20, "request_start", req_id=req_id, method=request.method, path=request.url.path)
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001
        log_event(logger, 40, "request_error", req_id=req_id, error=str(exc))
        raise
    elapsed_ms = int((time.time() - start) * 1000)
    log_event(logger, 20, "request_end", req_id=req_id, status=response.status_code, elapsed_ms=elapsed_ms)
    return response


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    log_event(logger, 30, "api_error", code=exc.code, detail=exc.message, path=request.url.path)
    return fail(exc)


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log_event(logger, 50, "unhandled_exception", error=str(exc), path=request.url.path)
    return fail(ApiError("INTERNAL_ERROR", "未预期的内部错误", http_status=500))


def _postgres_health() -> str:
    try:
        import psycopg2
        conn = psycopg2.connect(settings.postgres_dsn, connect_timeout=3)
        conn.close()
        return "healthy"
    except Exception:  # noqa: BLE001
        return "unavailable"


@app.get("/health")
def health():
    deps = jena_health()
    deps.update(Neo4jSync().health())
    deps["postgres"] = _postgres_health()
    all_ok = all(v == "healthy" for v in deps.values())
    return success({"status": "ok" if all_ok else "degraded", "version": settings.version, "dependencies": deps})


# 挂载业务路由
app.include_router(projects_router)
app.include_router(ontologies_router)
app.include_router(ingest_router)
app.include_router(graph_router)
app.include_router(mcp_router)
app.include_router(namespaces_router)
app.include_router(actions_router)
app.include_router(templates_router)
app.include_router(llm_router)
app.include_router(system_router)


# VS6: 由 mcp_server 模块按项目挂载 /mcp/{project_id}，双传输（2026-10-28 MCP 接入层修复）：
# - Streamable HTTP（2025-03-26/2025-06-18，推荐）：POST/DELETE /mcp/{pid} 与
#   GET /mcp/{pid}（带 Mcp-Session-Id）走原始 ASGI 中间件 McpStreamableMiddleware；
# - SSE（2024-11-05 兼容）：GET /mcp/{pid}（Accept: text/event-stream 且无 session 头）
#   + POST /mcp/{pid}/messages/。
# 必须使用 get_mcp_server() 单例：发布态（内存缓存 + 磁盘 JsonStore）与端点缓存
# 都在该实例上，直接 McpServer() 会造出第二个实例，导致端点/状态端点互相不可见。
_mcp_server = get_mcp_server()
_mcp_server.mount(app)
# 最后 add_middleware → 最外层：Streamable 流量不经过 BaseHTTPMiddleware（避免响应体被管道缓冲）
app.add_middleware(McpStreamableMiddleware, mcp_server=_mcp_server)


@app.on_event("startup")
async def _start_mcp_sweeper():
    _mcp_server.start_sweeper()


@app.get("/mcp/status")
def mcp_status(project_id: str | None = Query(default=None)):
    return success(_mcp_server.status(project_id=project_id))


@app.get("/mcp/{project_id}")
async def mcp_sse_endpoint(request: Request):
    # 带 Mcp-Session-Id 的 GET 已被 McpStreamableMiddleware 拦截（Streamable 会话流）。
    # 此处只处理无 session 头的请求：SSE 兼容路径（Accept: text/event-stream）或 405。
    accept = request.headers.get("accept", "")
    if "text/event-stream" not in accept.lower():
        return JSONResponse(
            {"error": "METHOD_NOT_ALLOWED",
             "detail": "Streamable HTTP GET 需携带 Mcp-Session-Id；旧版 SSE 客户端请带 Accept: text/event-stream"},
            status_code=405,
        )
    return await _mcp_server.handle_sse(request)


@app.post("/mcp/{project_id}/messages/")
async def mcp_messages_endpoint(request: Request):
    return await _mcp_server.handle_messages(request)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
