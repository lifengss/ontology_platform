#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M3-E（V2-13 回写风险分级）验收脚本——覆盖 10 点：TP-13-01~06 / TCE-13-01~04。

分级矩阵（action_engine）：
  low   低风险 → 映射/幂等门（dry-run 可先跑）→ 自动回写，无人工
  medium 中风险 → AI 三条件评估（无误伤/可原子/可完整；报告随记录留痕）
               → 通过自动，失败/超时/无法判断 → 升级人工（失败≠通过）
  high  高风险 → 必须人工确认（签核标识=身份+时间戳），未确认不回写
横切：幂等键/指数退避/熔断（三级同等生效）；三类回写统一入审计（action_writeback，
      detail 含 risk_level/trigger_source/signer，可按等级/触发来源/签核人检索）；
风险等级仅管理员可调整（常规更新端点锁死 risk_level，防降级绕过）。

验收点（与 docs/V2-测试用例.md 第十三章逐条对应）：
  TC-13-01  低风险自动回写（无人工干预）
  TC-13-02  中风险 AI 三条件评估（通过自动+报告留痕；未通过升级人工）
  TC-13-03  高风险人工确认+签核标识（身份+时间戳）
  TC-13-04  风险等级仅管理员调整（非管理员 403+审计；管理员成功+变更留痕）
  TC-13-05  幂等/退避/熔断在三级同等生效
  TC-13-06  三级回写统一审计，可按等级/触发来源/签核人检索
  TCE-13-01 低风险 dry-run/门失败 → 不回写，记录失败原因
  TCE-13-02 中风险评估失败/超时/无法判断 → 升级人工，不自动执行
  TCE-13-03 高风险确认拒绝/超时 → 取消+记录原因与操作人
  TCE-13-04 外部失败 → 退避重试 → 熔断；失败场景可重放

运行：backend/ 下  python scripts/verify_m3e_writeback_risk.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.join(PROJECT_ROOT, "backend")
sys.path.insert(0, BACKEND_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))  # scripts/ 同目录

import config  # noqa: E402

_TEMP_DIR = tempfile.mkdtemp(prefix="m3e_verify_")
DATA_DIR = os.path.join(_TEMP_DIR, "data")


def _setup_env() -> None:
    os.environ.update({
        "DATA_DIR": DATA_DIR,
        "ONTOLOGY_WRITEBACK_ENABLED": "true",
        "ONTOLOGY_WRITEBACK_CONFIRM_TTL_SECONDS": "600",
        "ONTOLOGY_WRITEBACK_AI_TIMEOUT_SECONDS": "30",
        "ONTOLOGY_ACTION_MAX_RETRIES": "2",
        "ONTOLOGY_ACTION_BREAKER_FAILURE_THRESHOLD": "3",
        "ONTOLOGY_ACTION_BREAKER_COOLDOWN_SECONDS": "3600",  # 冷却期长 → 熔断后保持 OPEN
        # M2-A 身份认证（TP-13-04 需 admin/modeler 两角色）
        "ONTOLOGY_AUTH_ENABLED": "true",
        "ONTOLOGY_AUTH_SECRET": "m3e-verify-secret",
        "ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD": "Admin@12345",
        "ONTOLOGY_PBKDF2_ITERATIONS": "10000",  # 加速哈希
    })
    config.get_settings.cache_clear()


# ===================== 结果记录 =====================
RESULTS: list[tuple[str, bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "", expected: str = "") -> None:
    RESULTS.append((name, bool(cond), detail, expected))
    if not cond:
        print(f"  ✗ {name} :: {detail} || 期望: {expected}")


# ===================== 外部系统 mock（可控返回码/头/体） =====================
class _FakeExternal:
    def __init__(self, name: str, status: int = 200):
        self.name = name
        self.status = status
        self.script: list[int] = []  # 逐次返回码序列（耗尽后沿用最后值）
        self._idx = 0
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}"

    def set_script(self, statuses: list[int]) -> None:
        with self.lock:
            self.script = list(statuses)
            self._idx = 0

    def _make_handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静默
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                with outer.lock:
                    if outer.script:
                        st = outer.script[min(outer._idx, len(outer.script) - 1)]
                        outer._idx += 1
                    else:
                        st = outer.status
                    outer.calls.append({
                        "idem": self.headers.get("Idempotency-Key"),
                        "body": body.decode("utf-8", "replace"),
                    })
                payload = json.dumps(
                    {"echo": True, "status": st, "ts": time.time()}).encode()
                self.send_response(st)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return H

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


# ===================== 中风险 AI 三条件评估的确定性替身 =====================
# action_engine.ai_three_condition_evaluate 内部 lazy import ai_helper.client.call_llm，
# 故替换模块属性即可控制 verdict（pass/fail/null/error 四种行为）。
LLM_STATE = {"behavior": "pass", "calls": 0}

_ALL_PASS = {"no_unexpected_external_impact": True, "atomicity": True, "completeness": True,
             "rationale": "验证桩：三条件均满足"}


def _llm_response(obj: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}}]}


def _fake_call_llm(prompt: str, **kwargs) -> dict:
    LLM_STATE["calls"] += 1
    b = LLM_STATE["behavior"]
    if b == "pass":
        return _llm_response(_ALL_PASS)
    if b == "fail":
        return _llm_response({**_ALL_PASS, "atomicity": False, "rationale": "验证桩：原子性不满足"})
    if b == "null":
        return _llm_response({**_ALL_PASS, "no_unexpected_external_impact": None,
                              "rationale": "验证桩：无法判断副作用"})
    raise RuntimeError("验证桩：AI 网关超时/不可用")


# ===================== 请求助手 =====================
def req(client, method: str, path: str, token: str | None = None,
        json_body: dict | None = None, params: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        return client.request(method, path, json=json_body, headers=headers, params=params)
    return client.request(method, path, headers=headers, params=params)


def base_checks(name: str, resp, code: int = 200) -> dict:
    """返回 data；校验 HTTP code + success 信封（TP-09-01）。"""
    check(f"{name}.code=={code}", resp.status_code == code, f"got {resp.status_code}")
    try:
        body = resp.json()
    except Exception:
        check(f"{name}.json", False, "非 JSON 响应")
        return {}
    check(f"{name}.success", isinstance(body, dict) and body.get("success") is True,
          f"body={body}")
    return body.get("data") or {}


# ===================== 审计助手 =====================
def audit_items(client, token: str, pid: str, **filters) -> list[dict]:
    params = {"project_id": pid, "page_size": 500}
    params.update(filters)
    r = req(client, "GET", "/api/audit", token, params=params)
    return (r.json().get("data") or {}).get("items") or []


def find_audit(items: list[dict], action: str, target_id: str = "",
               detail_pred=None) -> dict | None:
    for a in items:
        if a.get("action") != action:
            continue
        if target_id and a.get("target_id") != target_id:
            continue
        if detail_pred and not detail_pred(a.get("detail") or {}):
            continue
        return a
    return None

# ===================== 环境与对象准备 =====================
def build_env():
    _setup_env()
    import action_engine
    import ai_helper.client as _aich
    from fastapi.testclient import TestClient
    import main as m

    action_engine._sleep_fn = lambda s: None  # 退避等待不真实 sleep（确定性/提速）
    _aich.call_llm = _fake_call_llm

    client = TestClient(m.app)

    # --- 用户：admin（bootstrap）+ modeler（非管理员，验证 403） ---
    r = req(client, "POST", "/api/auth/login?mode=bearer",
            json_body={"username": "admin", "password": "Admin@12345"})
    admin_token = (r.json().get("data") or {}).get("token")
    check("env.admin.login", bool(admin_token), f"body={r.text[:200]}", "admin 登录成功")
    r = req(client, "POST", "/api/auth/users", admin_token,
            json_body={"username": "modeler1", "password": "Modeler@12345", "role": "modeler"})
    check("env.modeler.create", r.status_code in (200, 201), f"body={r.text[:200]}")
    r = req(client, "POST", "/api/auth/login?mode=bearer",
            json_body={"username": "modeler1", "password": "Modeler@12345"})
    modeler_token = (r.json().get("data") or {}).get("token")
    check("env.modeler.login", bool(modeler_token), f"body={r.text[:200]}")

    # --- 项目 ---
    r = req(client, "POST", "/api/projects", admin_token,
            json_body={"name": "M3E 验证项目"})
    pdata = r.json().get("data") or {}
    pid = pdata.get("project_id") or pdata.get("id")
    check("env.project", bool(pid), f"body={r.text[:200]}")

    # --- 外部系统 mock（多实例 → 熔断按 URL 独立） ---
    mock_ok = _FakeExternal("ok", 200)
    mock_resil = _FakeExternal("resil", 200)
    mock_cb = _FakeExternal("cb", 500)
    mock_replay = _FakeExternal("replay", 500)

    # --- Action 定义（全部 webhook 目标；target_connection 为外部系统标识） ---
    def mk_action(name: str, risk: str, idem: str, mock: _FakeExternal,
                  input_map: dict | None = None) -> str:
        r = req(client, "POST", "/api/actions", admin_token,
                json_body={
                    "name": name, "description": f"M3E 验收-{name}",
                    "risk_level": risk,
                    "invoke": {"kind": "webhook", "method": "POST", "url": mock.url},
                    "input_map": input_map or {"cust": "$.customer"},
                    "idempotency_key": idem,
                    "target_connection": "ext-crm",
                },
                params={"project_id": pid})
        aid = (r.json().get("data") or {}).get("action_id")
        check(f"env.action.{name}", bool(aid), f"body={r.text[:200]}")
        return aid

    A = {
        "low": mk_action("低风险-客户状态同步", "low", "M3E-IDEM-LOW-1", mock_ok),
        "low_bad": mk_action("低风险-映射缺字段", "low", "M3E-IDEM-LOWBAD-1", mock_ok,
                             input_map={"cust": "$.customer", "missing": "$.absent"}),
        "med": mk_action("中风险-工单升级", "medium", "M3E-IDEM-MED-1", mock_ok),
        "med_fail": mk_action("中风险-原子性不满足", "medium", "M3E-IDEM-MEDFAIL-1", mock_ok),
        "med_err": mk_action("中风险-AI不可用", "medium", "M3E-IDEM-MEDERR-1", mock_ok),
        "med_null": mk_action("中风险-无法判断", "medium", "M3E-IDEM-MEDNULL-1", mock_ok),
        "high": mk_action("高风险-对外退款", "high", "M3E-IDEM-HIGH-1", mock_ok),
        "high_rej": mk_action("高风险-拒绝场景", "high", "M3E-IDEM-HIGHREJ-1", mock_ok),
        "high_ttl": mk_action("高风险-超时场景", "high", "M3E-IDEM-HIGHTTL-1", mock_ok),
        "riskctl": mk_action("风险等级管控", "high", "M3E-IDEM-RISKCTL-1", mock_ok),
        "resil": mk_action("低风险-退避重试", "low", "M3E-IDEM-RESIL-1", mock_resil),
        "cb": mk_action("低风险-熔断", "low", "M3E-IDEM-CB-{seq}", mock_cb),
        "replay": mk_action("低风险-失败重放", "low", "M3E-IDEM-REPLAY-1", mock_replay),
    }
    return client, admin_token, modeler_token, pid, A, mock_ok, mock_resil, mock_cb, mock_replay


def _logs(client, token: str, aid: str, pid: str) -> list[dict]:
    r = req(client, "GET", f"/api/actions/{aid}/logs", token, params={"project_id": pid, "limit": 100})
    return (r.json().get("data") or {}).get("items") or []

# ===================== S1 TP-13-01 低风险自动回写（无人工干预） =====================
def s1_low_auto(client, token, pid, A, mock_ok):
    aid = A["low"]
    r = req(client, "POST", f"/api/actions/{aid}/execute", token,
            json_body={"input": {"customer": "CUST-A"}, "dry_run": True},
            params={"project_id": pid})
    d = base_checks("S1.dry_run", r)
    check("S1.dry_run.gate.passed", (d.get("gate") or {}).get("passed") is True,
          f"gate={d.get('gate')}", "dry-run 门通过")
    check("S1.dry_run.no_token", d.get("confirmation_token") is None,
          "dry-run 不产生待确认 token")
    check("S1.dry_run.no_external", len(mock_ok.calls) == 0,
          f"calls={len(mock_ok.calls)}", "dry-run 不产生真实回写")

    r = req(client, "POST", f"/api/actions/{aid}/execute", token,
            json_body={"input": {"customer": "CUST-A"}}, params={"project_id": pid})
    d = base_checks("S1.execute", r)
    check("S1.executed", d.get("executed") is True, f"data={d}")
    check("S1.auto_executed", d.get("auto_executed") is True,
          f"risk={d.get('risk_level')}", "低风险自动回写")
    check("S1.outcome.success", d.get("outcome") == "success", f"outcome={d.get('outcome')}")
    check("S1.no_human", d.get("confirmation_token") is None and not d.get("escalated"),
          "低风险全程无人工干预")
    check("S1.external_called", len(mock_ok.calls) == 1, f"calls={len(mock_ok.calls)}")
    check("S1.idem_key_sent", mock_ok.calls[0]["idem"] == "M3E-IDEM-LOW-1"
          if mock_ok.calls else False, f"idem={mock_ok.calls and mock_ok.calls[0]['idem']}")
    check("S1.payload_rendered", '"cust": "CUST-A"' in (mock_ok.calls[0]["body"] if mock_ok.calls else ""),
          f"body={mock_ok.calls and mock_ok.calls[0]['body']}", "映射渲染正确")

    logs = _logs(client, token, aid, pid)
    check("S1.log_recorded", len(logs) == 1 and logs[0].get("outcome") == "success"
          and logs[0].get("auto") is True, f"logs={logs}", "日志留痕（auto=true）")


# ===================== S2 TP-13-02 中风险 AI 三条件评估 =====================
def s2_medium_ai(client, token, pid, A, mock_ok):
    aid = A["med"]
    LLM_STATE["behavior"] = "pass"
    r = req(client, "POST", f"/api/actions/{aid}/execute", token,
            json_body={"input": {"customer": "CUST-B"}}, params={"project_id": pid})
    d = base_checks("S2.pass.execute", r)
    ev = d.get("evaluation") or {}
    check("S2.pass.auto", d.get("executed") is True and d.get("auto_executed") is True,
          f"data={d}", "三条件通过 → 自动回写")
    check("S2.pass.verdict", ev.get("verdict") == "pass" and ev.get("source") == "llm",
          f"evaluation={ev}")
    conds = ev.get("conditions") or {}
    check("S2.pass.conditions", all(conds.get(k) is True for k in
                                    ("no_unexpected_external_impact", "atomicity", "completeness")),
          f"conditions={conds}")

    logs = _logs(client, token, aid, pid)
    log_ev = (logs[-1].get("evaluation") or {}) if logs else {}
    check("S2.report_retained", log_ev.get("verdict") == "pass" and log_ev.get("conditions"),
          f"log.evaluation={log_ev}", "评估报告随日志留痕（GET /logs 可查）")

    aid_f = A["med_fail"]
    before = len(mock_ok.calls)
    LLM_STATE["behavior"] = "fail"
    r = req(client, "POST", f"/api/actions/{aid_f}/execute", token,
            json_body={"input": {"customer": "CUST-C"}}, params={"project_id": pid})
    d = base_checks("S2.fail.execute", r)
    ev = d.get("evaluation") or {}
    check("S2.fail.escalated", d.get("escalated") is True and d.get("confirmation_token"),
          f"data={d}", "原子性不满足 → 升级人工")
    check("S2.fail.verdict", ev.get("verdict") == "fail"
          and (ev.get("conditions") or {}).get("atomicity") is False, f"evaluation={ev}")
    check("S2.fail.no_external", len(mock_ok.calls) == before,
          f"calls {before}->{len(mock_ok.calls)}", "未通过不自动回写")
    return d.get("confirmation_token")


# ===================== S3 TP-13-03 高风险人工确认+签核标识 =====================
def s3_high_confirm(client, admin_token, pid, A, mock_ok):
    aid = A["high"]
    before = len(mock_ok.calls)
    r = req(client, "POST", f"/api/actions/{aid}/execute", admin_token,
            json_body={"input": {"customer": "CUST-D"}}, params={"project_id": pid})
    d = base_checks("S3.execute", r)
    tok = d.get("confirmation_token")
    check("S3.awaiting", d.get("outcome") == "awaiting_confirmation" and bool(tok),
          f"data={d}", "高风险 → 生成待确认 token")
    check("S3.no_auto_writeback", len(mock_ok.calls) == before,
          f"calls {before}->{len(mock_ok.calls)}", "未确认不回写")

    r = req(client, "POST", f"/api/actions/{aid}/confirm", admin_token,
            json_body={"confirmation_token": tok}, params={"project_id": pid})
    d = base_checks("S3.confirm", r)
    check("S3.confirmed.executed", d.get("executed") is True and d.get("outcome") == "success",
          f"data={d}")
    sig = d.get("signer") or {}
    check("S3.signer.identity", sig.get("username") == "admin" and sig.get("role") == "admin"
          and bool(sig.get("user_id")), f"signer={sig}", "签核标识=身份")
    check("S3.signer.timestamp", bool(sig.get("signed_at")), f"signed_at={sig.get('signed_at')}")
    check("S3.external_called", len(mock_ok.calls) == before + 1,
          f"calls={len(mock_ok.calls)}")

    logs = _logs(client, admin_token, aid, pid)
    log_sig = (logs[-1].get("signer") or {}) if logs else {}
    check("S3.log.signer", log_sig.get("username") == "admin" and bool(log_sig.get("signed_at")),
          f"log.signer={log_sig}", "签核标识随日志留痕")


# ===================== S4 TP-13-04 风险等级仅管理员调整 =====================
def s4_risk_admin_only(client, admin_token, modeler_token, pid, A):
    aid = A["riskctl"]
    r = req(client, "PUT", f"/api/actions/{aid}/risk-level", modeler_token,
            json_body={"risk_level": "low", "reason": "验证-非管理员降级"},
            params={"project_id": pid})
    body = r.json()
    check("S4.modeler.403", r.status_code == 403 and body.get("error", {}).get("code") == "FORBIDDEN",
          f"code={r.status_code} body={body}")
    den = find_audit(audit_items(client, admin_token, pid, action="action_risk_level_denied"),
                     "action_risk_level_denied")
    check("S4.denied.audited", den is not None and den.get("actor") == "modeler1",
          f"den={den}", "非管理员调整留审计（actor=modeler1）")

    r = req(client, "PUT", f"/api/actions/{aid}/risk-level", admin_token,
            json_body={"risk_level": "low", "reason": "验证-管理员降级"},
            params={"project_id": pid})
    d = base_checks("S4.admin.update", r)
    check("S4.admin.risk_low", d.get("risk_level") == "low", f"data={d}")
    hist = (d.get("risk_level_history") or [{}])[-1]
    check("S4.history", hist.get("from") == "high" and hist.get("to") == "low"
          and hist.get("downgrade") is True and hist.get("by") == "admin",
          f"history={hist}", "变更留痕（含降级标记/操作人）")
    chg = find_audit(audit_items(client, admin_token, pid, action="action_risk_level_change"),
                     "action_risk_level_change", target_id=aid)
    check("S4.change.audited", chg is not None and (chg.get("detail") or {}).get("downgrade") is True,
          f"chg={chg}")

    r = req(client, "PUT", f"/api/actions/{aid}/risk-level", admin_token,
            json_body={"risk_level": "low", "reason": "重复"}, params={"project_id": pid})
    check("S4.no_change.400", r.status_code == 400
          and r.json().get("error", {}).get("code") == "NO_CHANGE", f"body={r.text[:200]}")

    r = req(client, "PUT", f"/api/actions/{aid}", admin_token,
            json_body={"name": "风险等级管控", "description": "M3E 验收-风险等级管控",
                       "target_connection": "ext-crm",
                       "invoke": {"kind": "webhook", "method": "POST", "url": "http://127.0.0.1:1/none"},
                       "idempotency_key": "M3E-IDEM-RISKCTL-1",
                       "risk_level": "high"},
            params={"project_id": pid})
    check("S4.normal_update.locked", r.status_code == 403
          and r.json().get("error", {}).get("code") == "RISK_LEVEL_LOCKED",
          f"body={r.text[:200]}", "常规更新端点锁死 risk_level（含管理员）")

# ===================== S5 TP-13-05 幂等/退避/熔断（三级同等生效） =====================
def s5_guards(client, token, pid, A, mock_ok, mock_resil, mock_cb):
    import config
    import action_engine
    settings = config.get_settings()

    # --- 幂等（三级） ---
    before = len(mock_ok.calls)
    r = req(client, "POST", f"/api/actions/{A['low']}/execute", token,
            json_body={"input": {"customer": "CUST-A"}}, params={"project_id": pid})
    d = base_checks("S5.idem.low", r)
    check("S5.idem.low.hit", d.get("idempotency_hit") is True and d.get("executed") is True,
          f"data={d}")
    LLM_STATE["behavior"] = "pass"
    r = req(client, "POST", f"/api/actions/{A['med']}/execute", token,
            json_body={"input": {"customer": "CUST-B"}}, params={"project_id": pid})
    d = base_checks("S5.idem.medium", r)
    check("S5.idem.medium.hit", d.get("idempotency_hit") is True, f"data={d}",
          "中风险幂等同等生效")
    r = req(client, "POST", f"/api/actions/{A['high']}/execute", token,
            json_body={"input": {"customer": "CUST-D"}}, params={"project_id": pid})
    d = base_checks("S5.idem.high.exec", r)
    tok = d.get("confirmation_token")
    r = req(client, "POST", f"/api/actions/{A['high']}/confirm", token,
            json_body={"confirmation_token": tok}, params={"project_id": pid})
    d = base_checks("S5.idem.high.confirm", r)
    check("S5.idem.high.hit", d.get("idempotency_hit") is True, f"data={d}",
          "高风险确认幂等同等生效")
    check("S5.idem.no_redundant_call", len(mock_ok.calls) == before,
          f"calls {before}->{len(mock_ok.calls)}", "幂等命中不产生新外部调用")

    # --- 指数退避重试（低危真实回写） ---
    settings.action_max_retries = 2
    mock_resil.set_script([500, 500, 200])
    r = req(client, "POST", f"/api/actions/{A['resil']}/execute", token,
            json_body={"input": {"customer": "CUST-R1"}}, params={"project_id": pid})
    d = base_checks("S5.backoff.execute", r)
    check("S5.backoff.success", d.get("executed") is True and d.get("outcome") == "success",
          f"data={d}", "5xx,5xx,200 → 退避重试后成功")
    check("S5.backoff.three_calls", len(mock_resil.calls) == 3,
          f"calls={len(mock_resil.calls)}", "2 次重试 = 3 次外部调用")
    check("S5.backoff.same_idem", mock_resil.calls and all(
        c["idem"] == "M3E-IDEM-RESIL-1" for c in mock_resil.calls),
          f"ids={[c['idem'] for c in mock_resil.calls]}", "重试复用同一幂等键")

    # --- 熔断（低危；retries=0 每次确认=1 次外部调用） ---
    settings.action_max_retries = 0
    mock_cb.set_script([500])
    for seq in ("1", "2", "3"):
        r = req(client, "POST", f"/api/actions/{A['cb']}/execute", token,
                json_body={"input": {"customer": f"CUST-CB{seq}", "seq": seq}},
                params={"project_id": pid})
        base_checks(f"S5.breaker.exec{seq}", r)
    d3 = r.json()["data"]
    check("S5.breaker.third_failed", d3.get("outcome") == "failed", f"data={d3}")
    breaker = action_engine._get_breaker(mock_cb.url)
    check("S5.breaker.open", breaker.state() == "open",
          f"state={breaker.state()}", "连续 3 次失败 → OPEN")
    r = req(client, "POST", f"/api/actions/{A['cb']}/execute", token,
            json_body={"input": {"customer": "CUST-CB4", "seq": "4"}},
            params={"project_id": pid})
    d = base_checks("S5.breaker.fastfail", r)
    check("S5.breaker.fast_fail", d.get("circuit_open") is True and d.get("executed") is False,
          f"data={d}", "OPEN 期间直接失败、不发起外部调用")
    check("S5.breaker.no_call", len(mock_cb.calls) == 3,
          f"calls={len(mock_cb.calls)}")
    al = find_audit(audit_items(client, token, pid, action="action_circuit_open"),
                    "action_circuit_open")
    check("S5.breaker.alert", al is not None and (al.get("detail") or {}).get("alert"),
          f"al={al}", "熔断告警入审计")
    settings.action_max_retries = 2  # 还原


# ===================== S7 TCE-13-01 低风险门失败 → 不回写 =====================
def s7_gate_fail(client, token, pid, A, mock_ok):
    aid = A["low_bad"]
    before = len(mock_ok.calls)
    r = req(client, "POST", f"/api/actions/{aid}/execute", token,
            json_body={"input": {"customer": "CUST-A"}}, params={"project_id": pid})
    d = base_checks("S7.execute", r)
    check("S7.gate_failed", d.get("gate_failed") is True and d.get("executed") is False
          and d.get("outcome") == "gate_failed", f"data={d}")
    check("S7.gate_failures", any("missing" in str(f) for f in (d.get("gate_failures") or [])),
          f"failures={d.get('gate_failures')}", "失败原因=映射字段 missing（路径未解析）")
    check("S7.no_external", len(mock_ok.calls) == before, "门失败不回写")

    r = req(client, "POST", f"/api/actions/{aid}/execute", token,
            json_body={"input": {"customer": "CUST-A"}, "dry_run": True},
            params={"project_id": pid})
    d = base_checks("S7.dry_run", r)
    check("S7.dry_run.gate_fail", (d.get("gate") or {}).get("passed") is False
          and (d.get("gate") or {}).get("failures"), f"gate={d.get('gate')}",
          "dry-run 亦暴露门失败（可先验）")

    logs = _logs(client, token, aid, pid)
    check("S7.log.gate_failures", any(l.get("outcome") == "gate_failed"
          and l.get("gate_failures") for l in logs), f"logs={logs}", "失败原因随日志留痕")


# ===================== S8 TCE-13-02 中风险评估失败/无法判断 → 升级人工 =====================
def s8_ai_fail_escalate(client, token, pid, A, mock_ok, med_fail_token: str):
    before = len(mock_ok.calls)
    LLM_STATE["behavior"] = "error"
    r = req(client, "POST", f"/api/actions/{A['med_err']}/execute", token,
            json_body={"input": {"customer": "CUST-E"}}, params={"project_id": pid})
    d = base_checks("S8.error.execute", r)
    ev = d.get("evaluation") or {}
    check("S8.error.escalated", d.get("escalated") is True and d.get("confirmation_token"),
          f"data={d}", "AI 异常 → 升级人工（失败≠通过）")
    check("S8.error.undecidable", ev.get("verdict") == "undecidable"
          and ev.get("source") == "unavailable" and ev.get("error"), f"ev={ev}")

    LLM_STATE["behavior"] = "null"
    r = req(client, "POST", f"/api/actions/{A['med_null']}/execute", token,
            json_body={"input": {"customer": "CUST-N"}}, params={"project_id": pid})
    d = base_checks("S8.null.execute", r)
    ev = d.get("evaluation") or {}
    check("S8.null.undecidable", d.get("escalated") is True
          and ev.get("verdict") == "undecidable" and ev.get("source") == "llm",
          f"ev={ev}", "条件 null（无法判断）→ 升级人工")
    check("S8.no_auto_writeback", len(mock_ok.calls) == before,
          f"calls {before}->{len(mock_ok.calls)}", "两种情形均不自动执行")

    # 升级后人工接管仍可通过（人工判断优先）
    r = req(client, "POST", f"/api/actions/{A['med_fail']}/confirm", token,
            json_body={"confirmation_token": med_fail_token}, params={"project_id": pid})
    d = base_checks("S8.override.confirm", r)
    check("S8.override.executed", d.get("executed") is True and d.get("outcome") == "success",
          f"data={d}", "人工确认接管 AI 未通过的回写")


# ===================== S9 TCE-13-03 高风险拒绝/超时 → 取消+原因+操作人 =====================
def s9_high_reject_ttl(client, admin_token, pid, A, mock_ok):
    import action_engine
    before = len(mock_ok.calls)

    # --- 拒绝 ---
    aid = A["high_rej"]
    r = req(client, "POST", f"/api/actions/{aid}/execute", admin_token,
            json_body={"input": {"customer": "CUST-X"}}, params={"project_id": pid})
    d = base_checks("S9.rej.execute", r)
    tok = d.get("confirmation_token")
    r = req(client, "POST", f"/api/actions/{aid}/reject", admin_token,
            json_body={"confirmation_token": tok, "reason": "不在业务窗口"},
            params={"project_id": pid})
    d = base_checks("S9.rej.reject", r)
    check("S9.rejected", d.get("cancelled") is True and d.get("cancel_reason") == "rejected"
          and d.get("reject_reason") == "不在业务窗口", f"data={d}")
    check("S9.rej.signer", (d.get("signer") or {}).get("username") == "admin",
          f"signer={d.get('signer')}", "拒绝操作人留痕")
    check("S9.rej.no_external", len(mock_ok.calls) == before, "拒绝后未回写")
    rj = find_audit(audit_items(client, admin_token, pid, action="action_writeback"),
                    "action_writeback", target_id=aid,
                    detail_pred=lambda dt: dt.get("trigger_source") == "reject"
                    and dt.get("signer") == "admin")
    check("S9.rej.audited", rj is not None, f"rj={rj}", "拒绝入审计（signer=操作人）")

    # --- 确认超时（白盒回拨 created_ts 越过 TTL=600s） ---
    aid = A["high_ttl"]
    r = req(client, "POST", f"/api/actions/{aid}/execute", admin_token,
            json_body={"input": {"customer": "CUST-T"}}, params={"project_id": pid})
    d = base_checks("S9.ttl.execute", r)
    tok = d.get("confirmation_token")
    action_engine._pending_tokens[tok]["created_ts"] -= 1000
    r = req(client, "POST", f"/api/actions/{aid}/confirm", admin_token,
            json_body={"confirmation_token": tok}, params={"project_id": pid})
    d = base_checks("S9.ttl.confirm", r)
    check("S9.timeout.cancelled", d.get("cancelled") is True
          and d.get("cancel_reason") == "confirmation_timeout", f"data={d}")
    check("S9.timeout.signer", (d.get("signer") or {}).get("username") == "admin",
          "超时取消仍记录操作人")
    check("S9.timeout.no_external", len(mock_ok.calls) == before, "超时后未回写")
    to = find_audit(audit_items(client, admin_token, pid, action="action_writeback"),
                    "action_writeback", target_id=aid,
                    detail_pred=lambda dt: dt.get("cancel_reason") == "confirmation_timeout")
    check("S9.timeout.audited", to is not None, f"to={to}")


# ===================== S10 TCE-13-04 外部失败 → 可重放 =====================
def s10_replay(client, admin_token, pid, A, mock_replay):
    aid = A["replay"]
    mock_replay.set_script([500])
    r = req(client, "POST", f"/api/actions/{aid}/execute", admin_token,
            json_body={"input": {"customer": "CUST-R"}}, params={"project_id": pid})
    d = base_checks("S10.execute.failed", r)
    check("S10.failed", d.get("outcome") == "failed" and d.get("executed") is False,
          f"data={d}", "外部 5xx（含退避重试）→ failed")
    logs = _logs(client, admin_token, aid, pid)
    failed_log = next((l for l in logs if l.get("outcome") == "failed"), None)
    check("S10.log.failed", failed_log is not None, f"logs={logs}")

    mock_replay.set_script([200])
    r = req(client, "POST", f"/api/actions/{aid}/logs/{failed_log['log_id']}/replay",
            admin_token, params={"project_id": pid})
    d = base_checks("S10.replay", r)
    check("S10.replay.success", d.get("executed") is True and d.get("outcome") == "success"
          and d.get("replay_of") == failed_log["log_id"], f"data={d}")
    check("S10.replay.idem_reused", all(c["idem"] == "M3E-IDEM-REPLAY-1"
          for c in mock_replay.calls), f"ids={[c['idem'] for c in mock_replay.calls]}",
          "重放复用原幂等键（失败不阻塞重放）")
    check("S10.replay.signer", (d.get("signer") or {}).get("username") == "admin",
          "重放操作人留痕")

    # 已生效回写不可重放
    low_logs = _logs(client, admin_token, A["low"], pid)
    ok_log = next((l for l in low_logs if l.get("outcome") == "success"), None)
    r = req(client, "POST", f"/api/actions/{A['low']}/logs/{ok_log['log_id']}/replay",
            admin_token, params={"project_id": pid})
    check("S10.no_replay_success", r.status_code == 409
          and r.json().get("error", {}).get("code") == "REPLAY_NOT_ALLOWED",
          f"body={r.text[:200]}", "已成功（已生效）不可重放")


# ===================== S6 TC-13-06 三类回写统一审计 + 组合检索 =====================
def s6_audit_unified(client, admin_token, modeler_token, pid, A):
    wb = audit_items(client, admin_token, pid, action="action_writeback")
    check("S6.audit.present", len(wb) >= 5, f"n={len(wb)}", "三类回写均有 action_writeback 记录")
    check("S6.audit.detail_keys", all(
        set(("risk_level", "trigger_source", "signer")) <= set((a.get("detail") or {}).keys())
        for a in wb), f"sample={wb[0] if wb else None}", "detail 固定含三级检索字段")

    hi = audit_items(client, admin_token, pid, action="action_writeback", risk_level="high")
    check("S6.filter.risk_high", len(hi) >= 2 and all(
        (a.get("detail") or {}).get("risk_level") == "high" for a in hi), f"n={len(hi)}",
        "按等级检索：high（确认+拒绝+超时）")
    lo = audit_items(client, admin_token, pid, action="action_writeback", risk_level="low")
    check("S6.filter.risk_low", len(lo) >= 2, f"n={len(lo)}", "按等级检索：low（自动+重放）")
    me = audit_items(client, admin_token, pid, action="action_writeback", risk_level="medium")
    check("S6.filter.risk_medium", len(me) >= 2, f"n={len(me)}", "按等级检索：medium（自动+人工接管）")

    src_api = audit_items(client, admin_token, pid, action="action_writeback",
                          trigger_source="api")
    check("S6.filter.src_api", len(src_api) >= 3, f"n={len(src_api)}", "按触发来源检索：api")
    src_replay = audit_items(client, admin_token, pid, action="action_writeback",
                             trigger_source="replay")
    check("S6.filter.src_replay", len(src_replay) == 1 and (
        src_replay[0].get("detail") or {}).get("replay_of"),
        f"items={src_replay}", "按触发来源检索：replay（含 replay_of）")

    sig = audit_items(client, admin_token, pid, action="action_writeback", signer="admin")
    check("S6.filter.signer", len(sig) >= 3, f"n={len(sig)}", "按签核人检索：admin")

    # 组合：高风险 + api 来源 + admin 签核（TC-13-03 的确认事件必须命中）
    combo = audit_items(client, admin_token, pid, action="action_writeback",
                        risk_level="high", trigger_source="api", signer="admin")
    check("S6.filter.combo", any(
        (a.get("detail") or {}).get("signer") == "admin" for a in combo), f"n={len(combo)}",
        "组合检索命中确认事件")

    # 审计仅管理员可读（M2-D 守卫，防签核信息泄漏）
    r = req(client, "GET", "/api/audit", modeler_token,
            params={"project_id": pid, "limit": 10})
    check("S6.audit.admin_only", r.status_code == 403, f"code={r.status_code}")


# ===================== 主流程 =====================
def main() -> int:
    client, admin_token, modeler_token, pid, A, mock_ok, mock_resil, mock_cb, mock_replay = \
        build_env()
    try:
        s1_low_auto(client, admin_token, pid, A, mock_ok)
        med_fail_token = s2_medium_ai(client, admin_token, pid, A, mock_ok)
        s3_high_confirm(client, admin_token, pid, A, mock_ok)
        s4_risk_admin_only(client, admin_token, modeler_token, pid, A)
        s5_guards(client, admin_token, pid, A, mock_ok, mock_resil, mock_cb)
        s7_gate_fail(client, admin_token, pid, A, mock_ok)
        s8_ai_fail_escalate(client, admin_token, pid, A, mock_ok, med_fail_token or "")
        s9_high_reject_ttl(client, admin_token, pid, A, mock_ok)
        s10_replay(client, admin_token, pid, A, mock_replay)
        s6_audit_unified(client, admin_token, modeler_token, pid, A)
    finally:
        for m in (mock_ok, mock_resil, mock_cb, mock_replay):
            m.close()
        shutil.rmtree(_TEMP_DIR, ignore_errors=True)

    passed = sum(1 for _, ok, _, _ in RESULTS if ok)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{'=' * 60}\nM3-E 结果: {passed}/{len(RESULTS)} 通过, {len(failed)} 失败")
    if failed:
        print("失败项:")
        for name, _, detail, expected in failed:
            print(f"  - {name} :: {detail} || 期望: {expected}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

