# -*- coding: utf-8 -*-
"""Action 受控回写健壮性测试（PRD §6.6 护栏 4：失败指数退避 + 熔断）。

覆盖：
- webhook 回写：5xx 指数退避重试后成功 / 4xx 不重试 / 重试携带 Idempotency-Key
- 幂等恰好一次：同一幂等键 confirm 两次，外部系统仅被调用一次
- 熔断器：连续失败达阈值 → OPEN（快速失败、不外呼、不落日志）
- 熔断恢复：冷却期后 HALF_OPEN 试探，成功 → CLOSED / 失败 → 重新 OPEN
- CircuitBreaker 状态机单元测试（CLOSED→OPEN→HALF_OPEN→CLOSED/OPEN）
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import action_engine
from action_engine import ActionEngine, CircuitBreaker
from config import get_settings


class _FakeExternal:
    """可脚本化返回的假外部业务系统（webhook 端点）。

    script: 按请求次序返回的状态码列表，耗尽后重复最后一项。
    """

    def __init__(self) -> None:
        self.script = [200]
        self.calls = 0
        self.idem_keys = []
        self.bodies = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静音
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                outer.calls += 1
                outer.idem_keys.append(self.headers.get("Idempotency-Key"))
                try:
                    outer.bodies.append(json.loads(body) if body else None)
                except (json.JSONDecodeError, ValueError):
                    outer.bodies.append(body)
                status = outer.script[min(outer.calls - 1, len(outer.script) - 1)]
                payload = json.dumps({"ok": status < 400, "status": status}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def set_script(self, statuses) -> None:
        self.script = list(statuses)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def fake_external():
    fe = _FakeExternal()
    yield fe
    fe.stop()


@pytest.fixture
def env(monkeypatch, fake_external):
    """关闭 DISPLAY_ONLY、免真实退避等待、重置熔断；默认 重试2/阈值3/冷却30s。"""
    monkeypatch.setattr(action_engine, "DISPLAY_ONLY", False)
    monkeypatch.setattr(action_engine, "_sleep_fn", lambda s: None)
    s = get_settings()
    monkeypatch.setattr(s, "action_max_retries", 2)
    monkeypatch.setattr(s, "action_breaker_failure_threshold", 3)
    monkeypatch.setattr(s, "action_breaker_cooldown_seconds", 30.0)
    action_engine._reset_breakers()
    yield action_engine
    action_engine._reset_breakers()


def _action(url: str, idem: str) -> dict:
    return {
        "action_id": "act_resilience",
        "input_map": {"amount": "$.amount"},
        "idempotency_key": idem,
        "invoke": {"kind": "webhook", "url": url, "method": "POST"},
    }


def _confirm(ae, action: dict, input_data: dict, project_id: str) -> dict:
    engine = ActionEngine()
    r = engine.execute(action, {"input": input_data, "dry_run": False}, project_id)
    return engine.confirm(r["confirmation_token"], {"project_id": project_id})


def test_retry_on_5xx_then_success(env, fake_external):
    """5xx 指数退避重试后成功：3 次外呼、每次携带 Idempotency-Key、熔断保持 CLOSED。"""
    pid = f"resil_{uuid.uuid4().hex[:8]}"
    idem = f"K-{uuid.uuid4().hex[:8]}"
    fake_external.set_script([500, 500, 200])
    result = _confirm(env, _action(fake_external.base_url, idem), {"amount": 88}, pid)

    assert result["executed"] is True
    assert result["external_response"]["status"] == 200
    assert fake_external.calls == 3, "500,500,200 应恰好外呼 3 次"
    assert fake_external.idem_keys == [idem, idem, idem], "每次重试都应携带同一 Idempotency-Key"
    assert env._get_breaker(fake_external.base_url).state() == CircuitBreaker.CLOSED


def test_no_retry_on_4xx(env, fake_external):
    """4xx 是确定性失败：不重试，仅 1 次外呼；熔断只记 1 次失败仍 CLOSED。"""
    pid = f"resil_{uuid.uuid4().hex[:8]}"
    idem = f"K-{uuid.uuid4().hex[:8]}"
    fake_external.set_script([400])
    result = _confirm(env, _action(fake_external.base_url, idem), {"amount": 1}, pid)

    assert result["external_response"]["status"] == 400
    assert fake_external.calls == 1, "4xx 不应重试"
    assert env._get_breaker(fake_external.base_url).state() == CircuitBreaker.CLOSED


def test_idempotency_exactly_once(env, fake_external):
    """同一幂等键 confirm 两次：外部系统仅被调用一次（第二次命中防重）。"""
    pid = f"resil_{uuid.uuid4().hex[:8]}"
    idem = f"K-{uuid.uuid4().hex[:8]}"
    fake_external.set_script([200])
    first = _confirm(env, _action(fake_external.base_url, idem), {"amount": 5}, pid)
    assert first["executed"] is True
    assert first.get("idempotency_hit") is False
    assert fake_external.calls == 1

    second = _confirm(env, _action(fake_external.base_url, idem), {"amount": 5}, pid)
    assert second.get("idempotency_hit") is True
    assert fake_external.calls == 1, "幂等防重命中时不得再次外呼"


def test_circuit_opens_after_threshold(env, fake_external, monkeypatch):
    """连续失败达阈值 → OPEN：第 4 次快速失败，不外呼、不落执行日志。"""
    s = get_settings()
    monkeypatch.setattr(s, "action_max_retries", 0)   # 每次 confirm = 1 次外呼 = 1 次熔断失败
    monkeypatch.setattr(s, "action_breaker_cooldown_seconds", 3600.0)  # 冷却拉长，保证测试期内保持 OPEN
    pid = f"resil_{uuid.uuid4().hex[:8]}"
    fake_external.set_script([500])
    action = _action(fake_external.base_url, "")  # 空幂等键：不参与防重，每次 confirm 都外呼

    r1 = _confirm(env, action, {"amount": 1}, pid)
    r2 = _confirm(env, action, {"amount": 2}, pid)
    r3 = _confirm(env, action, {"amount": 3}, pid)
    assert r1["external_response"]["status"] == 500
    assert r2["external_response"]["status"] == 500
    assert r3["external_response"]["status"] == 500
    assert fake_external.calls == 3

    breaker = env._get_breaker(fake_external.base_url)
    assert breaker.state() == CircuitBreaker.OPEN

    r4 = _confirm(env, action, {"amount": 4}, pid)
    assert r4["executed"] is False
    assert r4["circuit_open"] is True
    assert r4["retry_after"] > 0
    assert fake_external.calls == 3, "熔断 OPEN 期间不得外呼"


def test_half_open_success_recovery(env, fake_external, monkeypatch):
    """冷却期后 HALF_OPEN 试探成功 → CLOSED。"""
    clock = FakeClock()
    monkeypatch.setattr(env, "_breaker_clock", clock)
    s = get_settings()
    monkeypatch.setattr(s, "action_max_retries", 0)
    pid = f"resil_{uuid.uuid4().hex[:8]}"
    fake_external.set_script([500])

    action = _action(fake_external.base_url, "")
    for _ in range(3):
        _confirm(env, action, {"amount": 1}, pid)
    breaker = env._get_breaker(fake_external.base_url)
    assert breaker.state() == CircuitBreaker.OPEN

    # 冷却期内：快速失败
    assert breaker.allow() is False

    # 冷却期结束 → 试探成功
    clock.advance(30)
    fake_external.set_script([200])
    result = _confirm(env, action, {"amount": 2}, pid)
    assert result["executed"] is True
    assert result["external_response"]["status"] == 200
    assert fake_external.calls == 4, "HALF_OPEN 试探恰好放行一次外呼"
    assert breaker.state() == CircuitBreaker.CLOSED, "试探成功应恢复 CLOSED"


def test_half_open_failure_reopens(env, fake_external, monkeypatch):
    """冷却期后 HALF_OPEN 试探失败 → 重新 OPEN（重新冷却），期间继续快速失败。"""
    clock = FakeClock()
    monkeypatch.setattr(env, "_breaker_clock", clock)
    s = get_settings()
    monkeypatch.setattr(s, "action_max_retries", 0)
    pid = f"resil_{uuid.uuid4().hex[:8]}"
    fake_external.set_script([500])

    action = _action(fake_external.base_url, "")
    for _ in range(3):
        _confirm(env, action, {"amount": 1}, pid)
    breaker = env._get_breaker(fake_external.base_url)
    assert breaker.state() == CircuitBreaker.OPEN

    clock.advance(30)  # 进入 HALF_OPEN
    r = _confirm(env, action, {"amount": 2}, pid)  # 试探失败（仍 500）
    assert r["external_response"]["status"] == 500
    assert fake_external.calls == 4
    assert breaker.state() == CircuitBreaker.OPEN, "试探失败应重新 OPEN"

    r2 = _confirm(env, action, {"amount": 3}, pid)  # 重新冷却期内：快速失败
    assert r2["circuit_open"] is True
    assert fake_external.calls == 4, "重新 OPEN 后冷却期内不得外呼"


def test_circuit_breaker_state_machine(monkeypatch):
    """CircuitBreaker 状态机：CLOSED→(连续失败)→OPEN→(冷却)→HALF_OPEN→成功CLOSED/失败OPEN。"""
    clock = FakeClock()
    monkeypatch.setattr(action_engine, "_breaker_clock", clock)
    b = CircuitBreaker("unit", failure_threshold=2, cooldown_seconds=10)

    assert b.state() == CircuitBreaker.CLOSED
    b.record_failure()
    assert b.state() == CircuitBreaker.CLOSED, "未达阈值不应 OPEN"
    assert b.allow() is True

    b.record_failure()
    assert b.state() == CircuitBreaker.OPEN, "达到阈值应 OPEN"
    assert b.allow() is False
    assert b.retry_after() > 0

    clock.advance(10)
    assert b.allow() is True, "冷却期结束应进入 HALF_OPEN 并放行一次试探"
    b.record_success()
    assert b.state() == CircuitBreaker.CLOSED, "试探成功应恢复 CLOSED"

    # 再打满阈值 → OPEN → HALF_OPEN 试探失败 → 重新 OPEN
    b.record_failure()
    b.record_failure()
    assert b.state() == CircuitBreaker.OPEN
    clock.advance(10)
    assert b.allow() is True
    b.record_failure()
    assert b.state() == CircuitBreaker.OPEN, "HALF_OPEN 试探失败应重新 OPEN"
