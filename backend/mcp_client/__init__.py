"""mcp_client：多连接配置管理 + transport(stdio/http) + 鉴权 + 健壮性（VS7）。

健壮性（PRD §6.5）：健康检查 + 超时(10s) + 指数退避重试(≤3)；401/403 阻断告警不降级；schema 漂移停库转人工。
MVP 对 HTTP transport 做通用工具调用封装；真实 MCP stdio JSON-RPC 协议在需要时扩展。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # noqa: S404
import time
import urllib.error
import urllib.request
from typing import Any

from config import get_settings
from core.responses import ApiError
from ingestion import apply_field_map
from logging_setup import get_logger

logger = get_logger("mcp_client")


def _resolve_ref(ref: str | None) -> str | None:
    """解析凭据引用：支持 ${ENV_NAME} 或直接 ENV_NAME。"""
    if not ref:
        return None
    m = re.match(r"\$\{(.+)\}", ref.strip())
    name = m.group(1) if m else ref.strip()
    return os.getenv(name)


def _auth_header(auth: dict[str, Any]) -> dict[str, str]:
    """根据 auth 配置生成请求头；不暴露 secret。"""
    headers: dict[str, str] = {"Accept": "application/json", "Content-Type": "application/json"}
    atype = auth.get("type")
    if atype == "bearer_token":
        token = _resolve_ref(auth.get("bearer_token_ref"))
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif atype == "api_key":
        key = _resolve_ref(auth.get("api_key_ref"))
        if key:
            headers["X-API-Key"] = key
    elif atype == "oauth2_client_credentials":
        # MVP 简化：token_url + client_id/secret 取 token（标准 client_credentials）
        token = _fetch_oauth_token(auth)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_oauth_token(auth: dict[str, Any]) -> str | None:
    token_url = auth.get("token_url")
    client_id = _resolve_ref(auth.get("client_id_ref"))
    client_secret = _resolve_ref(auth.get("client_secret_ref"))
    if not token_url or not client_id or not client_secret:
        return None
    data = urllib.parse.urlencode({  # type: ignore[attr-defined]
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": " ".join(auth.get("scopes", [])),
    }).encode("utf-8")
    req = urllib.request.Request(token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8")).get("access_token")
    except Exception as e:  # noqa: BLE001
        logger.warning("OAuth token 获取失败: %s", e)
        return None


def _http_probe(url: str, headers: dict[str, str], timeout: int) -> tuple[bool, bool, int, str | None]:
    """返回 (reachable, auth_ok, latency_ms, error)。"""
    req = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            latency_ms = int((time.time() - start) * 1000)
            return True, True, latency_ms, None
    except urllib.error.HTTPError as e:
        latency_ms = int((time.time() - start) * 1000)
        if e.code in (401, 403):
            return True, False, latency_ms, f"HTTP {e.code}"
        # 其他 4xx/5xx 视为可连通但业务错误，reachable 仍算 true
        return True, True, latency_ms, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        latency_ms = int((time.time() - start) * 1000)
        return False, False, latency_ms, str(e)


class McpClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    def test_connection(self, config: dict[str, Any]) -> dict[str, Any]:
        """连通性测试（含鉴权握手）。"""
        transport = config.get("transport")
        if transport in ("streamable_http", "http"):
            endpoint = config.get("endpoint")
            if not endpoint:
                return {"reachable": False, "auth_ok": False, "error": "缺少 endpoint"}
            headers = _auth_header(config.get("auth") or {})
            reachable, auth_ok, latency_ms, error = _http_probe(endpoint, headers, self.settings.mcp_timeout_seconds)
            return {"reachable": reachable, "auth_ok": auth_ok, "latency_ms": latency_ms, "error": error}
        if transport == "stdio":
            command = config.get("endpoint") or config.get("command")
            if not command:
                return {"reachable": False, "auth_ok": False, "error": "stdio 缺少 command"}
            exists = shutil.which(command.split()[0]) is not None
            return {"reachable": exists, "auth_ok": exists, "error": None if exists else "命令未找到"}
        return {"reachable": False, "auth_ok": False, "error": f"未知 transport: {transport}"}

    def read(self, config: dict[str, Any], tool: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """触发读取 → 返回结构化记录（按 maps_to_ontology 映射为候选实例）。"""
        records = self.invoke_tool(config, tool, params or {})
        maps = config.get("maps_to_ontology") or {}
        object_type = maps.get("object_type")
        field_map: dict[str, str] = maps.get("field_map") or {}
        if not object_type:
            raise ApiError("VALIDATION_ERROR", "连接缺少 maps_to_ontology.object_type", http_status=422)
        if not field_map:
            raise ApiError("VALIDATION_ERROR", "连接缺少 maps_to_ontology.field_map", http_status=422)

        return {
            "object_type": object_type,
            "field_map": field_map,
            "records": records,
            "record_count": len(records),
        }

    def invoke_tool(self, config: dict[str, Any], tool: str, params: dict[str, Any]) -> Any:
        """通用 MCP/HTTP 工具调用（读写均可），返回原始结果。"""
        transport = config.get("transport")
        if transport in ("streamable_http", "http"):
            return self._http_invoke(config, tool, params)
        if transport == "stdio":
            return self._stdio_invoke(config, tool, params)
        raise ApiError("VALIDATION_ERROR", f"未知 transport: {transport}", http_status=422)

    def _http_invoke(self, config: dict[str, Any], tool: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        endpoint = config.get("endpoint")
        if not endpoint:
            raise ApiError("VALIDATION_ERROR", "HTTP transport 缺少 endpoint", http_status=422)
        headers = _auth_header(config.get("auth") or {})
        body = json.dumps({"tool": tool, "params": params}).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")  # noqa: S310

        def call():
            try:
                with urllib.request.urlopen(req, timeout=self.settings.mcp_timeout_seconds) as resp:  # noqa: S310
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise ApiError("MCP_AUTH_ERROR", f"MCP 鉴权失败: HTTP {e.code}", http_status=403)
                raise ApiError("MCP_READ_ERROR", f"MCP 读取失败: HTTP {e.code}", http_status=502)
            except urllib.error.URLError as e:
                raise ApiError("MCP_UNREACHABLE", f"MCP 端点不可达: {e}", http_status=503)
            except Exception as e:  # noqa: BLE001
                raise ApiError("MCP_READ_ERROR", f"MCP 读取异常: {e}", http_status=502)

        result = self._with_retry(call)
        # 兼容多种返回结构：{"records": [...]} / {"data": [...]} / [...]
        if isinstance(result, list):
            return result
        for key in ("records", "data", "items", "results"):
            if key in result and isinstance(result[key], list):
                return result[key]
        raise ApiError("MCP_READ_ERROR", "MCP 返回格式无法识别 records", http_status=502)

    def _stdio_invoke(self, config: dict[str, Any], tool: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        command = config.get("endpoint") or config.get("command")
        if not command:
            raise ApiError("VALIDATION_ERROR", "stdio 缺少 command", http_status=422)
        # MVP 占位：真实 MCP stdio 需要 JSON-RPC session，先不支持
        raise ApiError("NOT_IMPLEMENTED", "stdio MCP 读取在 MVP 中未实现", http_status=501)

    def _backoff(self, attempt: int) -> float:
        return min(2 ** attempt, 8)  # 指数退避，≤8s

    def _with_retry(self, fn):
        last = None
        for attempt in range(self.settings.mcp_max_retries + 1):
            try:
                return fn()
            except ApiError as e:
                # 401/403 不重试；直接抛
                if e.code in ("MCP_AUTH_ERROR", "FORBIDDEN_WRITE"):
                    raise
                last = e
                if attempt < self.settings.mcp_max_retries:
                    time.sleep(self._backoff(attempt))
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < self.settings.mcp_max_retries:
                    time.sleep(self._backoff(attempt))
        raise last  # type: ignore[misc]
