"""action_engine：Action 受控执行引擎（MVP 展示模式 + V2-13 回写风险分级 M3-E）。

护栏（PRD §6.6 / 决策）：每次确认；幂等键防重；dry-run 优先；失败指数退避+熔断；外部不可回滚→幂等 upsert。

V2-13（M3-E）回写风险分级执行流（ONTOLOGY_WRITEBACK_ENABLED=true 时真实外呼）：
- low（低风险）：dry-run/映射校验门禁（mapping_gate）通过 → 自动回写，无人工介入；
  门禁失败 → 不回写，失败原因落日志（EP-13-01）。
- medium（中风险）：AI 三条件评估（无预期外外部影响/原子性/完整性），
  评估报告随回写日志与审计留存；三条件全过 → 自动执行；
  评估失败/超时/输出非法/无法判定 → 升级人工确认（失败≠通过，EP-13-02）。
- high（高风险，默认）：必须人工确认才回写；确认记录签核标识（身份+时间戳）；
  确认超时（TTL）→ 回写取消，记录取消原因与操作者（EP-13-03）；拒绝同理。
- 幂等/退避/熔断三类护栏对三档同等生效；幂等仅以「已生效」回写挡重，
  失败/取消/门禁失败不挡重试（失败场景保留可重放，EP-13-04）。
- 跨系统写回优先用操作者身份联邦映射身份（M2-B），无映射回退连接默认身份并留痕。
- 熔断 OPEN 瞬间产生 action_circuit_open 审计告警。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional

from config import get_settings
from core.storage import JsonStore
from logging_setup import get_logger

logger = get_logger("action_engine")

# 4.1：业务动作页 MVP 为"仅展示、未开发"状态。
# 保留模块级属性（既有测试 monkeypatch 点）；V2-13 生效展示模式 =
# DISPLAY_ONLY 且未开启 ONTOLOGY_WRITEBACK_ENABLED（验收/生产真实回写开关）。
DISPLAY_ONLY = True

RISK_LEVELS = ("low", "medium", "high")
_RISK_ORDER = {lv: i for i, lv in enumerate(RISK_LEVELS)}

# V2-13 中危 AI 三条件
_EVAL_CONDITIONS = ("no_unexpected_external_impact", "atomicity", "completeness")

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


def _display_only() -> bool:
    """生效展示模式：模块开关（测试可 monkeypatch）且未启用 V2-13 真实回写。"""
    return bool(DISPLAY_ONLY) and not get_settings().writeback_enabled


def _risk_level(action: dict) -> str:
    rl = (action or {}).get("risk_level")
    return rl if rl in RISK_LEVELS else "high"


def _risk_downgraded(old: str, new: str) -> bool:
    return _RISK_ORDER.get(new, 2) < _RISK_ORDER.get(old, 2)


def _signer_info(actor: Optional[dict]) -> Optional[dict]:
    """签核标识 = 身份 + 时间戳（V2-13：人工确认/升级确认/拒绝/重放操作者留痕）。"""
    if not actor:
        return None
    return {
        "user_id": actor.get("id") or actor.get("user_id"),
        "username": actor.get("username"),
        "role": actor.get("role"),
        "signed_at": _now(),
    }


def _actor_username(actor: Optional[dict]) -> Optional[str]:
    return (actor or {}).get("username")


def _tenant_id_of(actor: Optional[dict]) -> str:
    return (actor or {}).get("tenant_id") or ""


# --------------------------------------------------------------------------- #
# V2-13 低风险映射校验门禁（dry-run 预校验）
# --------------------------------------------------------------------------- #
def mapping_gate(action: dict, planned: dict[str, Any], idem: str) -> list[str]:
    """低风险自动回写前的映射校验门禁；返回失败原因列表（空 = 通过）。

    校验项：回写方式配置完整（webhook/rest 需 url；mcp_tool 需连接存在＋tool＋白名单）、
    input_map 渲染值无未解析（None）、幂等键可解析且非空。
    """
    failures: list[str] = []
    invoke = action.get("invoke") or {}
    kind = invoke.get("kind")
    target = action.get("target_connection")
    if kind in ("webhook", "rest"):
        if not invoke.get("url"):
            failures.append("invoke.url 未设置")
    elif kind == "mcp_tool":
        if not target:
            failures.append("target_connection 未设置")
        elif not invoke.get("tool"):
            failures.append("invoke.tool 未设置")
        else:
            config = JsonStore("mcp_connections").get(target)
            if not config:
                failures.append(f"目标连接 {target} 不存在")
            else:
                allow = config.get("tool_allowlist") or []
                if allow and invoke.get("tool") not in allow:
                    failures.append(f"工具 {invoke.get('tool')} 不在连接工具白名单内")
    else:
        failures.append(f"不支持的回写方式 invoke.kind={kind!r}")
    for k, v in (planned or {}).items():
        if v is None:
            failures.append(f"映射字段 {k} 在 input 中无对应值（路径未解析）")
    if not (idem or "").strip():
        failures.append("幂等键为空（低风险自动回写要求可解析的幂等键）")
    elif "{" in idem or idem.startswith("$."):
        failures.append("幂等键模板未解析")
    return failures


# --------------------------------------------------------------------------- #
# V2-13 中风险 AI 三条件评估（失败≠通过）
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> Optional[dict]:
    """从 AI 响应抽取首个 JSON 对象（兼容 ```json 围栏与前后噪声）；失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if "```" in text:
        parts = [p.strip() for p in text.split("```") if p.strip()]
        for p in parts:
            if p.lower().startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                text = p
                break
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


def ai_three_condition_evaluate(action: dict, planned: dict[str, Any], input_data: dict[str, Any]) -> dict:
    """中风险 AI 三条件评估：无预期外外部影响 / 原子性 / 完整性。

    返回评估报告（随回写日志 evaluation 字段与审计留存）。
    LLM 不可用/超时/输出非法/条件无法判定 → verdict=undecidable（失败≠通过 → 升级人工）。
    verdict 由三条件取值规则推导（全 true→pass；任一 false→fail；其余→undecidable），
    不信任模型自报 verdict，结构性防止"失败被判为通过"。
    """
    report: dict[str, Any] = {
        "verdict": "undecidable",
        "conditions": {k: None for k in _EVAL_CONDITIONS},
        "rationale": "",
        "source": "llm",
        "model": None,
        "evaluated_at": _now(),
        "error": None,
    }
    prompt = (
        "你是数据治理回写风险评估器。评估以下跨系统回写操作是否可自动执行，严格按三条件判断：\n"
        "1) no_unexpected_external_impact：无预期外外部影响（仅变更目标系统中业务明确请求的数据，"
        "无级联/删除/批量变更等超出范围的副作用）；\n"
        "2) atomicity：原子性（单次写入或幂等写入，失败不产生部分写入残留）；\n"
        "3) completeness：完整性（目标系统所需字段完整非空，无关键缺失）。\n\n"
        f"目标连接：{action.get('target_connection') or 'N/A'}\n"
        f"回写方式：{json.dumps(action.get('invoke') or {}, ensure_ascii=False)}\n"
        f"计划 payload：{json.dumps(planned or {}, ensure_ascii=False, default=str)}\n"
        f"业务说明：{action.get('description') or action.get('name') or 'N/A'}\n\n"
        "只返回一个 JSON（不要输出其它文字），字段：\n"
        '{"no_unexpected_external_impact": true|false|null, "atomicity": true|false|null, '
        '"completeness": true|false|null, "rationale": "简要依据"}\n'
        "信息不足以判断某条件时，该条件置 null。"
    )
    try:
        from ai_helper.client import call_llm
        from ai_helper.config import get_llm_config
        cfg = get_llm_config()
        report["model"] = cfg.model
        result = call_llm(prompt, timeout=get_settings().writeback_ai_timeout_seconds)
        content = ((result.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        obj = _extract_json(content)
        if obj is None:
            report["error"] = "AI 未返回可解析的 JSON 结构，降级为无法判定"
            return report
        conds: dict[str, Any] = {}
        for k in _EVAL_CONDITIONS:
            v = obj.get(k)
            conds[k] = v if v in (True, False, None) else None
        report["conditions"] = conds
        report["rationale"] = str(obj.get("rationale") or "")[:2000]
        vals = list(conds.values())
        if all(v is True for v in vals):
            report["verdict"] = "pass"
        elif any(v is False for v in vals):
            report["verdict"] = "fail"
        else:
            report["verdict"] = "undecidable"
    except Exception as e:  # noqa: BLE001
        report["source"] = "unavailable"
        report["error"] = f"AI 评估不可用/失败/超时：{e}"
        report["verdict"] = "undecidable"
    return report


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


def _validation_error(message: str):
    from core.responses import ApiError
    return ApiError("VALIDATION_ERROR", message, http_status=422)


def _counts_as_executed(log: dict) -> bool:
    """幂等口径（V2-13）：只有「已生效」的回写挡重；
    失败/取消/门禁失败不挡重试与重放（失败场景保留可重放）。
    存量 MVP 日志无 executed 字段（均为展示模式成功）→ 视为已生效。"""
    if log.get("executed") is False:
        return False
    if log.get("outcome") in ("failed", "cancelled", "gate_failed"):
        return False
    return True


def _find_log_by_idem(idempotency_key: str, project_id: str | None = None) -> dict[str, Any] | None:
    """返回同一幂等键最近一条「已生效」回写日志（无则 None）。"""
    for log in reversed(_log_store_for(project_id).all()):
        if log.get("idempotency_key") == idempotency_key and _counts_as_executed(log):
            return log
    return None


def _already_executed(idempotency_key: str, project_id: str | None = None) -> bool:
    return _find_log_by_idem(idempotency_key, project_id) is not None


def list_logs(action_id: str, project_id: str | None = None) -> list[dict[str, Any]]:
    return _log_store_for(project_id).find(lambda r: r.get("action_id") == action_id)


class ActionEngine:
    def execute(self, action: dict, request: dict, project_id: str | None = None,
                actor: dict | None = None, trigger_source: str = "api") -> dict:
        """触发执行（V2-13 按风险分级分流）：

        - dry_run：返回 planned_payload + 映射门禁预览（gate），不产生确认令牌/不回写；
        - low：门禁通过 → 自动回写（auto_executed）；失败 → 不回写并记录原因（EP-13-01）；
        - medium：AI 三条件评估通过 → 自动回写；否则升级人工确认（EP-13-02）；
        - high（默认）：返回 confirmation_token，人工确认后才回写。
        """
        data = request.get("input", {})
        dry_run = request.get("dry_run", False)
        planned = render_input_map(action.get("input_map", {}), data)
        idem = idempotency_resolved(action.get("idempotency_key", ""), planned, data)
        risk = _risk_level(action)
        action_id = action.get("action_id", "unknown")

        if dry_run:
            gate_failures = mapping_gate(action, planned, idem)
            logger.info("dry_run action_id=%s planned=%s gate_passed=%s", action_id, planned, not gate_failures)
            return {"confirmation_token": None, "dry_run": True, "planned_payload": planned,
                    "idempotency_key": idem, "risk_level": risk,
                    "gate": {"passed": not gate_failures, "failures": gate_failures}}

        if risk == "low":
            gate_failures = mapping_gate(action, planned, idem)
            if gate_failures:
                # EP-13-01：预校验失败 → 不回写，失败原因落日志
                log_id = uuid.uuid4().hex
                _log_store_for(project_id).insert({
                    "log_id": log_id, "action_id": action_id, "confirmation_token": None,
                    "idempotency_key": idem, "planned_payload": planned, "input": data,
                    "external_response": None, "executed_at": _now(),
                    "risk_level": "low", "trigger_source": trigger_source,
                    "actor": _actor_username(actor), "signer": None, "evaluation": None,
                    "auto": False, "identity": "none", "executed": False,
                    "outcome": "gate_failed", "gate_failures": gate_failures,
                })
                logger.warning("execute action_id=%s low-risk 门禁失败：%s", action_id, gate_failures)
                return {"executed": False, "auto_executed": False, "gate_failed": True,
                        "gate_failures": gate_failures, "log_id": log_id,
                        "planned_payload": planned, "idempotency_key": idem,
                        "risk_level": "low", "outcome": "gate_failed"}
            result = self._auto_writeback(action, data, planned, idem, project_id, actor, trigger_source,
                                          evaluation=None, auto_reason="low_gate_passed")
            logger.info("execute action_id=%s low-risk 门禁通过 → 自动回写 outcome=%s", action_id, result.get("outcome"))
            return result

        if risk == "medium":
            report = ai_three_condition_evaluate(action, planned, data)
            if report["verdict"] == "pass":
                result = self._auto_writeback(action, data, planned, idem, project_id, actor, trigger_source,
                                              evaluation=report, auto_reason="medium_ai_passed")
                logger.info("execute action_id=%s medium-risk AI 评估通过 → 自动回写 outcome=%s",
                            action_id, result.get("outcome"))
                return result
            # fail / undecidable → 升级人工确认（失败≠通过，EP-13-02）
            token = uuid.uuid4().hex
            _pending_tokens[token] = {
                "action": action, "input": data, "planned_payload": planned, "idempotency_key": idem,
                "project_id": project_id, "created_at": _now(), "created_ts": time.time(),
                "risk_level": "medium", "evaluation": report, "escalated": True,
            }
            logger.warning("execute action_id=%s medium-risk AI 评估未通过（%s）→ 升级人工确认 token=%s",
                           action_id, report["verdict"], token)
            return {"confirmation_token": token, "dry_run": False, "planned_payload": planned,
                    "idempotency_key": idem, "risk_level": "medium", "escalated": True,
                    "evaluation": report, "outcome": "escalated_to_human",
                    "note": "中风险 AI 三条件评估未通过（fail/超时/无法判定），已升级人工确认"}

        # high（默认，最保守）
        token = uuid.uuid4().hex
        _pending_tokens[token] = {
            "action": action, "input": data, "planned_payload": planned, "idempotency_key": idem,
            "project_id": project_id, "created_at": _now(), "created_ts": time.time(),
            "risk_level": "high",
        }
        logger.info("execute action_id=%s token=%s (高危待人工确认)", action_id, token)
        return {"confirmation_token": token, "dry_run": False, "planned_payload": planned,
                "idempotency_key": idem, "risk_level": "high", "outcome": "awaiting_confirmation"}

    def _auto_writeback(self, action: dict, data: dict, planned: dict, idem: str,
                        project_id: str | None, actor: dict | None, trigger_source: str,
                        evaluation: dict | None = None, auto_reason: str = "") -> dict:
        """低/中危评估通过后的自动回写（与人工确认共用同一执行管线与护栏）。"""
        pending = {
            "action": action, "input": data, "planned_payload": planned, "idempotency_key": idem,
            "project_id": project_id, "created_at": _now(), "created_ts": time.time(),
            "risk_level": _risk_level(action), "evaluation": evaluation,
            "auto": True, "auto_reason": auto_reason,
        }
        result = self._do_writeback(pending, actor=actor, trigger_source=trigger_source)
        result["auto_executed"] = result.get("outcome") in ("success", "simulated", "idempotency_hit")
        return result

    def confirm(self, token: str, executed_log: dict,
                actor: dict | None = None, trigger_source: str = "api") -> dict:
        """人工确认后真正回写（高危 / 中危升级 / 存量确认令牌）。

        护栏（PRD §6.6，见模块 docstring）：
        - 确认令牌 TTL：超时未确认 → 回写取消，记录取消原因与操作者（EP-13-03）；
        - 指数退避重试：webhook/rest 可重试失败（网络/超时/5xx）重试，4xx 不重试，携带 Idempotency-Key；
        - 熔断器：按外部端点，连续失败达阈值即 OPEN（冷却期），期间快速失败、不外呼、不落日志（不污染幂等键）；
        - 签核标识（身份+时间戳）随日志与审计留存（V2-13）。
        """
        pending = _pending_tokens.pop(token, None)
        if not pending:
            logger.warning("confirm token=%s 无效或已过期", token)
            raise _validation_error("确认令牌无效或已过期")

        action = pending["action"]
        project_id = pending.get("project_id") or executed_log.get("project_id")
        action_id = action.get("action_id", "unknown")
        signed = _signer_info(actor)
        pending["confirmation_token"] = token

        # TTL：确认超时 → 回写取消（EP-13-03：取消原因 + 操作者留痕）
        ttl = get_settings().writeback_confirm_ttl_seconds
        if ttl and ttl > 0 and (time.time() - pending.get("created_ts", time.time())) > ttl:
            log_id = uuid.uuid4().hex
            _log_store_for(project_id).insert({
                "log_id": log_id, "action_id": action_id, "confirmation_token": token,
                "idempotency_key": pending.get("idempotency_key"),
                "planned_payload": pending.get("planned_payload"), "input": pending.get("input"),
                "external_response": None, "executed_at": _now(),
                "risk_level": pending.get("risk_level"), "trigger_source": trigger_source,
                "actor": _actor_username(actor), "signer": signed, "evaluation": pending.get("evaluation"),
                "auto": False, "identity": "none", "executed": False,
                "outcome": "cancelled", "cancel_reason": "confirmation_timeout",
            })
            logger.warning("confirm action_id=%s token=%s 超时取消（TTL=%ss）", action_id, token, ttl)
            self._record_writeback_audit(project_id, action, trigger_source, actor,
                                         after={"executed": False, "cancelled": True,
                                                "cancel_reason": "confirmation_timeout", "log_id": log_id},
                                         detail={"risk_level": pending.get("risk_level"),
                                                 "trigger_source": trigger_source,
                                                 "signer": (signed or {}).get("username"),
                                                 "signer_at": (signed or {}).get("signed_at"),
                                                 "cancel_reason": "confirmation_timeout"})
            return {"executed": False, "cancelled": True, "cancel_reason": "confirmation_timeout",
                    "log_id": log_id, "signer": signed, "risk_level": pending.get("risk_level"),
                    "outcome": "cancelled"}

        return self._do_writeback(pending, actor=actor, trigger_source=trigger_source, signed=signed)

    def reject(self, token: str, reason: str, actor: dict | None = None,
               project_id: str | None = None, expected_action_id: str | None = None) -> dict:
        """拒绝待确认回写（EP-13-03：取消原因与操作者留痕，不产生外部调用）。"""
        pending = _pending_tokens.pop(token, None)
        if not pending:
            raise _validation_error("确认令牌无效或已过期")
        paction = pending.get("action") or {}
        if expected_action_id and paction.get("action_id") not in (None, expected_action_id):
            raise _validation_error("确认令牌与该 Action 不匹配")
        pid = pending.get("project_id") or project_id
        signed = _signer_info(actor)
        log_id = uuid.uuid4().hex
        _log_store_for(pid).insert({
            "log_id": log_id, "action_id": paction.get("action_id"), "confirmation_token": token,
            "idempotency_key": pending.get("idempotency_key"),
            "planned_payload": pending.get("planned_payload"), "input": pending.get("input"),
            "external_response": None, "executed_at": _now(),
            "risk_level": pending.get("risk_level"), "trigger_source": "reject",
            "actor": _actor_username(actor), "signer": signed, "evaluation": pending.get("evaluation"),
            "auto": False, "identity": "none", "executed": False,
            "outcome": "cancelled", "cancel_reason": "rejected", "reject_reason": reason,
        })
        logger.info("reject action_id=%s token=%s reason=%s", paction.get("action_id"), token, reason)
        self._record_writeback_audit(pid, paction, "reject", actor,
                                     after={"executed": False, "cancelled": True,
                                            "cancel_reason": "rejected", "reason": reason, "log_id": log_id},
                                     detail={"risk_level": pending.get("risk_level"),
                                             "trigger_source": "reject",
                                             "signer": (signed or {}).get("username"),
                                             "signer_at": (signed or {}).get("signed_at"),
                                             "cancel_reason": "rejected"})
        return {"cancelled": True, "cancel_reason": "rejected", "reject_reason": reason,
                "log_id": log_id, "signer": signed, "risk_level": pending.get("risk_level"),
                "outcome": "cancelled"}

    def replay(self, action: dict, log_id: str, project_id: str | None,
               actor: dict | None = None, trigger_source: str = "replay") -> dict:
        """失败场景重放（EP-13-04）：以原 planned_payload/幂等键重新执行。

        已生效的回写不可重放（幂等键防重）；仅 failed/cancelled/gate_failed 可重放。
        """
        src = next((l for l in _log_store_for(project_id).all() if l.get("log_id") == log_id), None)
        if src is None or src.get("action_id") != action.get("action_id"):
            raise _validation_error("回写日志不存在")
        if _counts_as_executed(src):
            from core.responses import ApiError
            raise ApiError("REPLAY_NOT_ALLOWED", "已生效的回写不可重放（幂等键防重）", http_status=409)
        pending = {
            "action": action, "input": src.get("input"),
            "planned_payload": src.get("planned_payload") or {},
            "idempotency_key": src.get("idempotency_key") or "",
            "project_id": project_id, "created_at": _now(), "created_ts": time.time(),
            "risk_level": src.get("risk_level") or _risk_level(action),
            "evaluation": src.get("evaluation"), "auto": False, "replay_of": log_id,
        }
        result = self._do_writeback(pending, actor=actor, trigger_source=trigger_source,
                                    signed=_signer_info(actor))
        logger.info("replay action_id=%s log_id=%s outcome=%s", action.get("action_id"), log_id, result.get("outcome"))
        return result

    def _do_writeback(self, pending: dict, actor: dict | None = None,
                      trigger_source: str = "api", signed: dict | None = None) -> dict:
        """统一回写执行管线（低/中危自动、人工确认、重放共用）：
        幂等防重 → 熔断快速失败 → 外呼（展示模式模拟/真实）→ 日志+签核 → 熔断记账+告警。"""
        action = pending["action"]
        planned = pending["planned_payload"]
        idem = pending.get("idempotency_key") or ""
        project_id = pending.get("project_id")
        action_id = action.get("action_id", "unknown")
        risk = pending.get("risk_level") or _risk_level(action)
        invoke = action.get("invoke", {})
        kind = invoke.get("kind")

        # 幂等键防重（仅「已生效」回写挡重）
        if idem and _already_executed(idem, project_id):
            prev = _find_log_by_idem(idem, project_id)
            logger.info("writeback action_id=%s idempotency_hit=%s", action_id, idem)
            self._record_writeback_audit(project_id, action, trigger_source, actor,
                                         after={"executed": True, "idempotency_hit": True,
                                                "outcome": "idempotency_hit",
                                                "log_id": (prev or {}).get("log_id")},
                                         detail={"risk_level": risk, "trigger_source": trigger_source,
                                                 "signer": (signed or {}).get("username"),
                                                 "signer_at": (signed or {}).get("signed_at"),
                                                 "idempotency_hit": True,
                                                 "auto": bool(pending.get("auto")),
                                                 "replay_of": pending.get("replay_of")})
            return {"executed": True, "idempotency_hit": True,
                    "external_response": (prev or {}).get("external_response", {}),
                    "log_id": (prev or {}).get("log_id"), "outcome": "idempotency_hit", "risk_level": risk}

        external_response: dict[str, Any]
        identity = "not_applicable"
        if _display_only():
            logger.info("writeback action_id=%s display_only=True（未回写业务系统）", action_id)
            external_response = {"status": 0, "display_only": True,
                                 "error": "display_only: 展示模式（MVP）或未启用 ONTOLOGY_WRITEBACK_ENABLED，未真实回写"}
        else:
            breaker = _get_breaker(_breaker_key(action, invoke, kind))
            if not breaker.allow():
                retry_after = breaker.retry_after()
                logger.warning("writeback action_id=%s 熔断器OPEN key=%s retry_after=%.1fs（快速失败）",
                               action_id, breaker.key, retry_after)
                return {"executed": False, "circuit_open": True, "retry_after": round(retry_after, 1),
                        "outcome": "circuit_open", "risk_level": risk,
                        "error": f"熔断器开启：外部端点 {breaker.key} 持续失败，{retry_after:.1f}s 后再试（未回写；本次不计入幂等）"}
            pre_state = breaker.state()
            if kind in ("webhook", "rest"):
                external_response = _http_call_resilient(invoke.get("url", ""), invoke.get("method", "POST"),
                                                         planned, get_settings().mcp_timeout_seconds, idem)
                identity = "none"
            elif kind == "mcp_tool":
                external_response, identity = self._invoke_mcp_tool(action, invoke, planned,
                                                                    actor=actor, project_id=project_id)
            else:
                # 不支持的 kind 属配置错误，不是外部系统故障 → 不记入熔断
                external_response = {"status": 0, "error": f"不支持的 invoke.kind: {kind}"}

        success = _is_success(external_response)
        simulated = external_response.get("display_only") is True
        executed = success or simulated

        log_id = uuid.uuid4().hex
        log_record = {
            "log_id": log_id,
            "action_id": action_id,
            "confirmation_token": pending.get("confirmation_token"),
            "idempotency_key": idem,
            "planned_payload": planned,
            "input": pending.get("input"),
            "external_response": external_response,
            "executed_at": _now(),
            # V2-13 回写风险分级字段（三类回写统一留痕）
            "risk_level": risk,
            "trigger_source": trigger_source,
            "actor": _actor_username(actor),
            "signer": signed,
            "evaluation": pending.get("evaluation"),
            "auto": bool(pending.get("auto")),
            "identity": identity,
            "replay_of": pending.get("replay_of"),
            "executed": executed,
            "outcome": "simulated" if simulated else ("success" if success else "failed"),
        }
        _log_store_for(project_id).insert(log_record)
        logger.info("writeback action_id=%s log_id=%s kind=%s outcome=%s identity=%s",
                    action_id, log_id, kind, log_record["outcome"], identity)

        # V2-13（TC-13-06）：三类回写统一审计——自动（low/medium）/人工确认/重放共用本管线，
        # 不依赖入口路由，API 与 MCP 触发均留痕；detail 含 risk_level/trigger_source/signer 供组合检索。
        self._record_writeback_audit(project_id, action, trigger_source, actor,
                                     after={"executed": executed, "outcome": log_record["outcome"],
                                            "log_id": log_id},
                                     detail={"risk_level": risk, "trigger_source": trigger_source,
                                             "signer": (signed or {}).get("username"),
                                             "signer_at": (signed or {}).get("signed_at"),
                                             "evaluation_verdict": (pending.get("evaluation") or {}).get("verdict"),
                                             "identity": identity,
                                             "auto": bool(pending.get("auto")),
                                             "replay_of": pending.get("replay_of")})

        # 熔断记账 + OPEN 瞬间告警（V2-13：熔断 + 告警，EP-13-04）
        if not _display_only() and kind in ("webhook", "rest", "mcp_tool"):
            if success or simulated:
                breaker.record_success()
            else:
                breaker.record_failure()
                if pre_state != CircuitBreaker.OPEN and breaker.state() == CircuitBreaker.OPEN:
                    self._breaker_open_alert(breaker, action, project_id, risk, trigger_source, actor)

        result: dict[str, Any] = {
            "executed": executed, "idempotency_hit": False,
            "external_response": external_response, "log_id": log_id,
            "outcome": log_record["outcome"], "risk_level": risk, "identity": identity,
            "signer": signed,
        }
        if pending.get("auto"):
            result["auto"] = True
            result["auto_reason"] = pending.get("auto_reason")
        if pending.get("evaluation"):
            result["evaluation"] = pending["evaluation"]
        if pending.get("replay_of"):
            result["replay_of"] = pending["replay_of"]
        return result

    def _record_writeback_audit(self, project_id: str | None, action: dict, trigger_source: str,
                                actor: dict | None, after: dict, detail: dict) -> None:
        """V2-13（TC-13-06）：回写生命周期统一审计事件（action_writeback）。

        自动（low/medium）/人工确认/重放/拒绝/超时取消均由引擎记录，不依赖入口路由，
        API 与 MCP 触发均留痕；detail 固定含 risk_level/trigger_source/signer，
        供 /api/audit 按等级、触发来源、签核人组合检索。
        """
        try:
            import audit as audit_svc
            audit_svc.record(
                "action_writeback",
                actor=(actor or {}).get("username") or "system",
                actor_role=(actor or {}).get("role") or "",
                project_id=project_id or "",
                tenant_id=_tenant_id_of(actor),
                target_type="action", target_id=action.get("action_id") or "",
                target_name=action.get("name") or "",
                before={"target": action.get("target_connection")},
                after=after,
                detail=detail,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("action_writeback 审计记录失败：%s", e)

    def _breaker_open_alert(self, breaker: CircuitBreaker, action: dict, project_id: str | None,
                            risk: str, trigger_source: str, actor: dict | None) -> None:
        """熔断 OPEN 瞬间产生审计告警（V2-13 / EP-13-04「熔断+告警」）。"""
        try:
            import audit as audit_svc
            audit_svc.record(
                "action_circuit_open",
                actor=_actor_username(actor) or "system",
                actor_role=(actor or {}).get("role") or "",
                project_id=project_id or "",
                tenant_id=_tenant_id_of(actor),
                target_type="action", target_id=action.get("action_id") or "",
                target_name=action.get("name") or "",
                before={"breaker_state": "closed/half_open"},
                after={"breaker_state": "open", "retry_after": round(breaker.retry_after(), 1)},
                detail={"breaker_key": breaker.key, "risk_level": risk,
                        "trigger_source": trigger_source,
                        "alert": f"外部端点 {breaker.key} 连续失败达阈值，熔断开启（冷却期内快速失败）"},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("breaker alert 记录失败：%s", e)

    def _invoke_mcp_tool(self, action: dict, invoke: dict, planned: dict[str, Any],
                         actor: dict | None = None,
                         project_id: str | None = None) -> tuple[dict[str, Any], str]:
        """MCP 写工具外呼（V2-13 + M2-B）：跨系统写回优先用操作者身份联邦映射身份，
        无映射回退连接默认身份；返回 (响应, identity)。"""
        from mcp_client import McpClient
        from core.responses import ApiError as _ApiError

        connection_id = action.get("target_connection")
        tool = invoke.get("tool")
        if not connection_id or not tool:
            return {"status": 0, "error": "mcp_tool 缺少 target_connection 或 tool"}, "none"
        config = JsonStore("mcp_connections").get(connection_id)
        if not config:
            return {"status": 0, "error": f"连接 {connection_id} 不存在"}, "none"
        # 身份联邦（M2-B）：映射身份优先；缺失映射回退连接默认身份（留痕 identity 字段）
        auth_override: dict | None = None
        identity = "connection_default"
        if actor and project_id and actor.get("id"):
            try:
                from identity_federation import resolve_credential
                override = resolve_credential(project_id, user_id=actor.get("id"),
                                              connection_id=connection_id)
                if override:
                    auth_override = override
                    identity = "mapped"
            except Exception:  # noqa: BLE001
                auth_override = None
        try:
            result = McpClient().invoke_tool(config, tool, planned, auth_override=auth_override)
            return {"status": 200, "result": result, "identity": identity}, identity
        except _ApiError as e:
            return {"status": e.http_status or 502, "error": e.message, "code": e.code,
                    "identity": identity}, identity
        except Exception as e:  # noqa: BLE001
            return {"status": 0, "error": str(e), "identity": identity}, identity
