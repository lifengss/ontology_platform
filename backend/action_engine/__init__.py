"""action_engine：Action 受控执行引擎（VS8）。

护栏（PRD §6.6 / 决策）：每次确认；幂等键防重；dry-run 优先；失败指数退避+熔断；外部不可回滚→幂等 upsert。
执行流：触发 → 渲染 input_map → 弹确认令牌 → 确认后调外部写工具/webhook → 回填+日志。
"""
from __future__ import annotations

import json
import threading
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


def _http_call(url: str, method: str, payload: dict[str, Any], timeout: int, idem: str | None = None) -> dict[str, Any]:
    """单次外呼（webhook/rest）。idem 非空时携带 Idempotency-Key，供外部幂等系统重试去重。"""
    body = json.dumps(payload).encode("utf-8") if payload else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if idem:
        headers["Idempotency-Key"] = idem  # 重试幂等：支持幂等的外部系统可据此键去重
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return {"status": resp.status, "body": resp.read().decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode("utf-8", errors="replace")}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "error": str(e)}


def _backoff(attempt: int) -> float:
    """指数退避延迟：1s、2s、4s…，封顶 8s。"""
    return float(min(2 ** attempt, 8))


def _is_success(resp: Any) -> bool:
    """外部返回 status 为 2xx/3xx 视为成功。"""
    if not isinstance(resp, dict):
        return False
    try:
        status = int(resp.get("status", 0))
    except (TypeError, ValueError):
        return False
    return 200 <= status < 400


def _is_retryable(resp: Any) -> bool:
    """可重试失败：网络/超时（status=0）与 5xx。
    4xx（含 401/403 鉴权失败）是确定性失败，重试无意义，直接返回。"""
    if not isinstance(resp, dict):
        return False
    try:
        status = int(resp.get("status", 0))
    except (TypeError, ValueError):
        return False
    return status == 0 or status >= 500


def _http_call_resilient(url: str, method: str, payload: dict[str, Any], timeout: int, idem: str | None = None) -> dict[str, Any]:
    """webhook/rest 回写：指数退避重试（PRD §6.6 护栏 4）。

    可重试失败（网络/超时/5xx）按指数退避重试（最多 action_max_retries 次）；4xx 直接返回不重试。
    重试携带 Idempotency-Key，外部系统若支持幂等则不会重复写。
    """
    max_retries = get_settings().action_max_retries
    attempt = 0
    while True:
        result = _http_call(url, method, payload, timeout, idem)
        if _is_retryable(result) and attempt < max_retries:
            delay = _backoff(attempt)
            logger.warning("action webhook retry url=%s status=%s delay=%.1fs attempt=%d/%d",
                           url, result.get("status"), delay, attempt + 1, max_retries)
            _sleep_fn(delay)
            attempt += 1
            continue
        return result


# --- 熔断器（PRD §6.6 护栏 4：失败指数退避 + 熔断） ---

# 模块级 sleep / 时钟函数，便于测试覆盖（免真实等待）
_sleep_fn = time.sleep


def _breaker_clock() -> float:
    return time.monotonic()


class CircuitBreaker:
    """按外部端点维度的熔断器。

    状态机：CLOSED --(连续失败达阈值)--> OPEN --(冷却期结束)--> HALF_OPEN
            HALF_OPEN 试探成功 --> CLOSED；试探失败 --> OPEN（重新冷却）。
    计数口径：以「一次 confirm 逻辑操作」计一次（重试循环内多次请求合并为一次结果），
    避免瞬时抖动与熔断互相干扰；HALF_OPEN 仅放行一次试探。
    """

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, key: str, failure_threshold: int = 3, cooldown_seconds: float = 30.0) -> None:
        self.key = key
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._state = self.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False
        self._lock = threading.Lock()

    def _now(self) -> float:
        return _breaker_clock()

    def state(self) -> str:
        with self._lock:
            self._maybe_transition()
            return self._state

    def _maybe_transition(self) -> None:
        if self._state == self.OPEN and (self._now() - self._opened_at) >= self.cooldown_seconds:
            self._state = self.HALF_OPEN
            self._trial_in_flight = False

    def allow(self) -> bool:
        """返回本次 confirm 是否放行（HALF_OPEN 仅放行一次试探）。"""
        with self._lock:
            self._maybe_transition()
            if self._state == self.CLOSED:
                return True
            if self._state == self.HALF_OPEN and not self._trial_in_flight:
                self._trial_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._trial_in_flight = False
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._trial_in_flight = False
            if self._state == self.HALF_OPEN or self._consecutive_failures >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = self._now()

    def retry_after(self) -> float:
        with self._lock:
            if self._state != self.OPEN:
                return 0.0
            return max(0.0, self.cooldown_seconds - (self._now() - self._opened_at))


_breakers: dict[str, CircuitBreaker] = {}


def _get_breaker(key: str) -> CircuitBreaker:
    s = get_settings()
    b = _breakers.get(key)
    if b is None:
        b = CircuitBreaker(key,
                           failure_threshold=s.action_breaker_failure_threshold,
                           cooldown_seconds=s.action_breaker_cooldown_seconds)
        _breakers[key] = b
    return b


def _reset_breakers() -> None:
    """测试用：清空熔断状态，防止跨用例泄漏。"""
    _breakers.clear()


def _breaker_key(action: dict, invoke: dict, kind: str | None) -> str:
    """熔断键：按外部端点（webhook 用 url，mcp_tool 用 connection），一个端点故障不影响其它端点。"""
    if kind in ("webhook", "rest"):
        return invoke.get("url", "") or "webhook"
    if kind == "mcp_tool":
        return f"mcp:{action.get('target_connection') or 'unknown'}"
    return f"action:{kind}"


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
        """人工确认后真正回写：HTTP/webhook 或 MCP tool。

        护栏（PRD §6.6，见模块 docstring）：
        - 指数退避重试：webhook/rest 可重试失败（网络/超时/5xx）重试，4xx 不重试，携带 Idempotency-Key；
        - 熔断器：按外部端点，连续失败达阈值即 OPEN（冷却期），期间快速失败、不外呼、不落日志（不污染幂等键）。
        """
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
        else:
            breaker = _get_breaker(_breaker_key(action, invoke, kind))
            if not breaker.allow():
                retry_after = breaker.retry_after()
                logger.warning("confirm action_id=%s token=%s 熔断器OPEN key=%s retry_after=%.1fs（快速失败，不外呼，不落日志）",
                               action_id, token, breaker.key, retry_after)
                return {"executed": False, "circuit_open": True, "retry_after": round(retry_after, 1),
                        "error": f"熔断器开启：外部端点 {breaker.key} 持续失败，{retry_after:.1f}s 后再试（未回写；本次不计入幂等）"}
            if kind in ("webhook", "rest"):
                external_response = _http_call_resilient(invoke.get("url", ""), invoke.get("method", "POST"),
                                                         planned, get_settings().mcp_timeout_seconds, idem)
            elif kind == "mcp_tool":
                external_response = self._invoke_mcp_tool(action, invoke, planned)
            else:
                # 不支持的 kind 属配置错误，不是外部系统故障 → 不记入熔断
                external_response = {"status": 0, "error": f"不支持的 invoke.kind: {kind}"}
            if kind in ("webhook", "rest", "mcp_tool"):
                if _is_success(external_response):
                    breaker.record_success()
                else:
                    breaker.record_failure()

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
