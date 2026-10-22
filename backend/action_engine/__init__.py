"""action_engine：Action 受控执行引擎（VS8）。

护栏（PRD §6.6 / 决策）：每次确认；幂等键防重；dry-run 优先；失败指数退避+熔断；外部不可回滚→幂等 upsert。
执行流：触发 → 渲染 input_map → 弹确认令牌 → 确认后调外部写工具/webhook → 回填+日志。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from config import get_settings
from core.storage import JsonStore
from logging_setup import get_logger

logger = get_logger("action_engine")

# 4.1：业务动作页当前为"仅展示、未开发"状态。
# True 时 confirm 不会真正回写业务系统（webhook/MCP 外呼被拦截），仅记录日志；
# 待业务回写需求明确后改为 False 即可启用下方保留的完整回写模板代码。
DISPLAY_ONLY = True

_pending_tokens: dict[str, dict[str, Any]] = {}


def _log_store_for(project_id: str | None = None) -> JsonStore:
    return JsonStore("action_logs", project_id=project_id)


def render_input_map(input_map: dict[str, str], data: dict[str, Any]) -> dict[str, Any]:
    """把 input_map（JSONPath 风格 $.Customer.name）从 input 数据渲染为实际调用参数。"""
    result: dict[str, Any] = {}
    for key, path in input_map.items():
        parts = path.lstrip("$.").split(".")
        val: Any = data
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                val = None
                break
        result[key] = val
    return result


def idempotency_resolved(idempotency_key: str, planned: dict[str, Any], input_data: dict[str, Any]) -> str:
    """解析幂等键模板（如 customer::{name}）：优先用 planned_payload，再回退 input 数据。"""
    rendered = idempotency_key
    # 支持 {key} 占位与 JSONPath $.path 两种写法
    for src in (planned, input_data):
        if not isinstance(src, dict):
            continue
        for key, val in src.items():
            if isinstance(val, (str, int, float, bool)):
                rendered = rendered.replace("{" + key + "}", str(val))
    # 仍未解析的 JSONPath 用 render_input_map
    if rendered.startswith("$."):
        rendered = render_input_map({"_": rendered}, input_data).get("_", rendered)
    return rendered


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _http_call(url: str, method: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return {"status": resp.status, "body": resp.read().decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode("utf-8", errors="replace")}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "error": str(e)}


class ActionEngine:
    def execute(self, action: dict, request: dict, project_id: str | None = None) -> dict:
        """触发执行：渲染 payload，返回 confirmation_token（未确认不真正回写）。
        dry_run=true 时直接返回 planned_payload 且 confirmation_token=None。
        """
        data = request.get("input", {})
        dry_run = request.get("dry_run", False)
        planned = render_input_map(action.get("input_map", {}), data)
        idem = idempotency_resolved(action.get("idempotency_key", ""), planned, data)
        if dry_run:
            logger.info("dry_run action_id=%s planned=%s", action.get("action_id"), planned)
            return {"confirmation_token": None, "dry_run": True, "planned_payload": planned, "idempotency_key": idem}
        token = uuid.uuid4().hex
        _pending_tokens[token] = {
            "action": action,
            "input": data,
            "planned_payload": planned,
            "idempotency_key": idem,
            "project_id": project_id,
            "created_at": _now(),
        }
        logger.info("execute action_id=%s token=%s (待确认)", action.get("action_id"), token)
        return {"confirmation_token": token, "dry_run": False, "planned_payload": planned, "idempotency_key": idem}

    def confirm(self, token: str, executed_log: dict) -> dict:
        """人工确认后真正回写：HTTP/webhook 或 MCP tool。"""
        pending = _pending_tokens.pop(token, None)
        if not pending:
            logger.warning("confirm token=%s 无效或已过期", token)
            raise _validation_error("确认令牌无效或已过期")

        action = pending["action"]
        planned = pending["planned_payload"]
        idem = pending["idempotency_key"]
        action_id = action.get("action_id", "unknown")
        project_id = pending.get("project_id") or executed_log.get("project_id")

        # 幂等键防重
        if idem and _already_executed(idem, project_id):
            prev = _find_log_by_idem(idem, project_id)
            logger.info("confirm action_id=%s token=%s idempotency_hit=%s", action_id, token, idem)
            return {"executed": True, "idempotency_hit": True, "external_response": prev.get("external_response", {}), "log_id": prev.get("log_id")}

        invoke = action.get("invoke", {})
        kind = invoke.get("kind")
        external_response: dict[str, Any]
        if DISPLAY_ONLY:
            logger.info("confirm action_id=%s token=%s display_only=True（未回写业务系统）", action_id, token)
            external_response = {"status": 0, "display_only": True,
                                 "error": "display_only: 业务动作页为展示模式，未回写业务系统"}
        elif kind in ("webhook", "rest"):
            external_response = _http_call(invoke.get("url", ""), invoke.get("method", "POST"), planned, get_settings().mcp_timeout_seconds)
        elif kind == "mcp_tool":
            external_response = self._invoke_mcp_tool(action, invoke, planned)
        else:
            external_response = {"status": 0, "error": f"不支持的 invoke.kind: {kind}"}

        log_id = uuid.uuid4().hex
        log_record = {
            "log_id": log_id,
            "action_id": action_id,
            "confirmation_token": token,
            "idempotency_key": idem,
            "planned_payload": planned,
            "external_response": external_response,
            "executed_at": _now(),
        }
        _log_store_for(project_id).insert(log_record)
        logger.info("confirm action_id=%s token=%s log_id=%s kind=%s", action_id, token, log_id, kind)
        return {"executed": True, "idempotency_hit": False, "external_response": external_response, "log_id": log_id}

    def _invoke_mcp_tool(self, action: dict, invoke: dict, planned: dict[str, Any]) -> dict[str, Any]:
        from mcp_client import McpClient
        from core.responses import ApiError as _ApiError

        connection_id = action.get("target_connection")
        tool = invoke.get("tool")
        if not connection_id or not tool:
            return {"status": 0, "error": "mcp_tool 缺少 target_connection 或 tool"}
        config = JsonStore("mcp_connections").get(connection_id)
        if not config:
            return {"status": 0, "error": f"连接 {connection_id} 不存在"}
        try:
            result = McpClient().invoke_tool(config, tool, planned)
            return {"status": 200, "result": result}
        except _ApiError as e:
            return {"status": e.http_status or 502, "error": e.message, "code": e.code}
        except Exception as e:  # noqa: BLE001
            return {"status": 0, "error": str(e)}


def list_logs(action_id: str, project_id: str | None = None) -> list[dict[str, Any]]:
    return _log_store_for(project_id).find(lambda r: r.get("action_id") == action_id)


def _already_executed(idempotency_key: str, project_id: str | None = None) -> bool:
    return _find_log_by_idem(idempotency_key, project_id) is not None


def _find_log_by_idem(idempotency_key: str, project_id: str | None = None) -> dict[str, Any] | None:
    for log in _log_store_for(project_id).all():
        if log.get("idempotency_key") == idempotency_key:
            return log
    return None


def _validation_error(message: str):
    from core.responses import ApiError
    return ApiError("VALIDATION_ERROR", message, http_status=422)
