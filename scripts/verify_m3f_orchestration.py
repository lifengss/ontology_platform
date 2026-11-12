#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M3-F（V2-14 复杂 Action 编排）验收脚本——覆盖 TC-14-01~13 / TCE-14-01~05 / TCEE-04。

验收点（与 docs/V2-测试用例.md 第十六章逐条对应）：
  TC-14-01  编排引擎执行（串行＋条件分支，逐步状态可查；事件/cron 触发）
  TC-14-02  失败策略（retry 耗尽 / skip 跳过继续 / abort 中止，现场保留）
  TC-14-03  no-code 规格（参数仅三来源；条件仅白名单算子＋AND/OR；触发 manual/event/cron）
  TC-14-04  无代码红线（API 负向传代码/表达式字段 → 422 CODE_REJECTED；缺口不生成代码节点）
  TC-14-05  发布前 dry-run（逐节点输入/输出预览；风险节点展示待确认态；无外部调用）
  TC-14-06  SOP/skill→草稿（逐节点 grounding 依据+置信度；无对应能力步骤标能力缺口）
  TC-14-07  草稿高风险自动挂确认（生成通道不绕过签署护栏）
  TC-14-08  草稿与手动编排同通道（同引擎/同审计/同发布通道/执行行为一致）
  TC-14-09  AI 局部生成（结构化片段插入；越界输出拒绝；代码提示词拒绝）
  TC-14-10  AI 执行侧边界（执行路径零 LLM 调用；同触发多次执行路径可复现）
  TC-14-12  golden dataset 准确度（四类指标达冻结阈值）
  TCE-14-01 节点失败重试耗尽（按策略跳过/终止；下游依赖节点不执行）
  TCE-14-02 条件分支字段异常（节点错误标记＋原因，不静默走默认分支）
  TCE-14-03 触发 payload 非法（缺 required_ctx 字段 → 拒绝启动并报缺失项，不半启动）
  TCE-14-05 能力缺口未处理发布（发布被拦）
  TCEE-04   运行中断重启恢复（running→interrupted→resume 确定性重入，幂等键去重）
（TC-14-11 身份联邦属 M2-C 凭据映射；TC-14-13 反向暴露 P2 可选，均不在本切片。）

运行：项目根下  python scripts/verify_m3f_orchestration.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.join(PROJECT_ROOT, "backend")
sys.path.insert(0, BACKEND_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import config  # noqa: E402

_TEMP_DIR = tempfile.mkdtemp(prefix="m3f_verify_")
DATA_DIR = os.path.join(_TEMP_DIR, "data")


def _setup_env() -> None:
    os.environ.update({
        "DATA_DIR": DATA_DIR,
        "ONTOLOGY_WRITEBACK_ENABLED": "true",
        "ONTOLOGY_WRITEBACK_CONFIRM_TTL_SECONDS": "3600",
        "ONTOLOGY_WRITEBACK_AI_TIMEOUT_SECONDS": "10",
        "ONTOLOGY_ACTION_MAX_RETRIES": "1",  # 加速：Action 层内部重试 1 次
        "ONTOLOGY_ACTION_BREAKER_FAILURE_THRESHOLD": "50",  # 测试期不触发熔断干扰
        "ONTOLOGY_ORCH_CONFIRM_TTL_SECONDS": "3600",
        "ONTOLOGY_ORCH_SCHEDULER_INTERVAL_SECONDS": "0",  # 禁用后台调度（tick 手动驱动）
        # LLM 强制"不可用"：指向不可达本地地址＋假 key（config 读 LLM_BASE_URL/LLM_API_KEY，
        # 且 api_key 会回退凭证库，空 env 无法禁用）→ 生成回退确定性通道、
        # 中风险三条件评估 undecidable → 升级人工；且不触达真实 LLM 端点
        "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_API_KEY": "test-disabled",
        # M2-A 身份认证
        "ONTOLOGY_AUTH_ENABLED": "true",
        "ONTOLOGY_AUTH_SECRET": "m3f-verify-secret",
        "ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD": "Admin@12345",
        "ONTOLOGY_PBKDF2_ITERATIONS": "10000",
    })
    config.get_settings.cache_clear()


# ===================== 结果记录 =====================
RESULTS: list[tuple[str, bool, str, str]] = []


def check(name: str, cond: bool, detail: str = "", expected: str = "") -> None:
    RESULTS.append((name, bool(cond), detail, expected))
    if not cond:
        print(f"  ✗ {name} :: {detail} || 期望: {expected}")


# ===================== 外部系统 mock（可控返回码/体） =====================
class FakeExternal:
    def __init__(self, name: str, status: int = 200, body: dict | None = None):
        self.name = name
        self.status = status
        self.script: list[int] = []
        self._idx = 0
        self.body = body if body is not None else {"ok": True, "name": name, "count": 7}
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}"

    def set_script(self, statuses: list[int]) -> None:
        with self.lock:
            self.script = list(statuses)
            self._idx = 0

    def call_count(self) -> int:
        with self.lock:
            return len(self.calls)

    def _make_handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                with outer.lock:
                    status = outer.script[outer._idx] if outer._idx < len(outer.script) else outer.status
                    outer._idx += 1
                    outer.calls.append({"path": self.path, "body": body.decode("utf-8", "replace"),
                                        "idem": self.headers.get("Idempotency-Key")})
                payload = json.dumps(outer.body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return H


# ===================== HTTP 辅助 =====================
def req(client, method: str, path: str, token: str | None = None,
        json_body: dict | None = None, params: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        return client.request(method, path, json=json_body, headers=headers, params=params)
    return client.request(method, path, headers=headers, params=params)


def data_of(resp) -> dict:
    try:
        return resp.json().get("data") or {}
    except Exception:  # noqa: BLE001
        return {}


# ===================== 环境准备 =====================
def build_env():
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as client:
        r = req(client, "POST", "/api/auth/login?mode=bearer",
                json_body={"username": "admin", "password": "Admin@12345"})
        token = (r.json().get("data") or {}).get("token")
        check("env.admin.login", bool(token), f"body={r.text[:200]}", "admin 登录成功")
        r = req(client, "POST", "/api/projects", token, json_body={"name": "M3F 验证项目"})
        pid = data_of(r).get("project_id") or data_of(r).get("id")
        check("env.project", bool(pid), f"body={r.text[:200]}", "项目创建成功")
        # 生成专用项目（Action 目录独立，golden 匹配无歧义）
        r = req(client, "POST", "/api/projects", token, json_body={"name": "M3F 生成项目"})
        pid_gen = data_of(r).get("project_id") or data_of(r).get("id")
        check("env.project.gen", bool(pid_gen), f"body={r.text[:200]}", "生成项目创建成功")
        mocks = {
            "ok": FakeExternal("ok", 200, {"count": 7, "stage": "done"}),
            "ok2": FakeExternal("ok2", 200, {"count": 2, "stage": "done"}),
            "fail": FakeExternal("fail", 500),
        }

        # Action 白盒种子：ActionCreate 接口不开放 action_id（服务端 uuid 生成），
        # 而编排节点/生成器以可读 action_id 引用（_actions_by_id 以其为键），
        # 故直接以路由同构记录写入存储（Action 创建流程 M3-E 已单独验收）。
        def mk_action(action_id: str, name: str, desc: str, risk: str, mock: FakeExternal,
                      project: str = pid, idem: str | None = None) -> None:
            from core.storage import JsonStore
            now = datetime.now(timezone.utc).isoformat()
            rec = {
                "action_id": action_id, "id": action_id, "project_id": project,
                "name": name, "description": desc, "category": "验证",
                "target_connection": f"ext-{mock.name}",
                "invoke": {"kind": "webhook", "url": mock.url, "method": "POST"},
                "input_map": {}, "idempotency_key": idem or f"idem-{action_id}",
                "confirmation": "human_required", "risk_level": risk,
                "status": "active", "dry_run_supported": True, "side_effect": True,
                "created_at": now, "updated_at": now,
            }
            try:
                JsonStore("actions", project_id=project).insert(rec)
                check(f"env.action.{action_id}", True, "",
                      "Action 就绪（action_id 可读、幂等键输入相关）")
            except Exception as e:  # noqa: BLE001
                check(f"env.action.{action_id}", False, f"err={e}", "Action 就绪")

        # 运行用 Action（三档风险＋必败）：幂等键为输入相关模板，
        # 不同节点/入参 → 不同键，避免无占位键导致同 Action 后续执行全部命中幂等
        mk_action("act_notice", "发送通知", "向指定用户发送系统通知消息", "low", mocks["ok"],
                  idem="idem-notice::{msg}")
        mk_action("act_sync", "数据同步", "将业务数据同步到目标系统", "medium", mocks["ok2"],
                  idem="idem-sync::{stage}")
        mk_action("act_archive", "归档旧数据", "归档超过保留期的旧数据", "high", mocks["ok"],
                  idem="idem-archive::{ts}")
        mk_action("act_fail", "必败操作", "固定 500 的外部操作（失败策略验收）", "low", mocks["fail"],
                  idem="idem-fail::{msg}")
        # 生成用 Action（独立生成项目；名称须与 scripts/golden/orchestration_dataset.json 一致；
        # 生成编排入参恒为空 → 固定幂等键即可解析）
        mk_action("gen_send", "发送通知", "向指定用户发送系统通知消息", "low", mocks["ok"], pid_gen)
        mk_action("gen_datasync", "数据同步", "将业务数据同步到目标系统", "low", mocks["ok2"], pid_gen)
        mk_action("gen_report", "导出报表", "导出业务报表文件到归档系统", "medium", mocks["ok"], pid_gen)
        mk_action("gen_archiveold", "归档旧数据", "归档超过保留期的旧数据", "high", mocks["ok"], pid_gen)
        return client, token, pid, pid_gen, mocks


# ===================== 工作流辅助 =====================
def mk_wf(client, token, pid, spec, expect_code=201):
    r = req(client, "POST", "/api/orchestration/workflows", token,
            json_body=spec, params={"project_id": pid})
    check(f"wf.create[{spec['name']}] code=={expect_code}", r.status_code == expect_code,
          f"got {r.status_code} body={r.text[:300]}", f"HTTP {expect_code}")
    return r


def wf_id_of(resp) -> str:
    return data_of(resp).get("id") or data_of(resp).get("workflow_id") or ""


def publish(client, token, pid, wf_id, expect_code=200):
    r = req(client, "POST", f"/api/orchestration/workflows/{wf_id}/publish", token,
            params={"project_id": pid})
    return r


def get_wf(client, token, pid, wf_id) -> dict:
    r = req(client, "GET", f"/api/orchestration/workflows/{wf_id}", token, params={"project_id": pid})
    return data_of(r)


def run_wf(client, token, pid, wf_id, payload=None, expect_code=200):
    r = req(client, "POST", f"/api/orchestration/workflows/{wf_id}/run", token,
            json_body={"payload": payload or {}}, params={"project_id": pid})
    return r


def get_run(client, token, pid, run_id) -> dict:
    r = req(client, "GET", f"/api/orchestration/runs/{run_id}", token, params={"project_id": pid})
    return data_of(r)


# ===================== TC-14-01 编排引擎执行（串行＋分支＋确认分流） =====================
def test_engine_exec(client, token, pid, mocks):
    # ① 串行：start → a1(low 自动) → a2(low 自动) → end
    spec = {"name": "WF-serial", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start", "label": "开始"},
                {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                 "params": {"msg": {"value_type": "literal", "value": "hello"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "a2", "kind": "action", "label": "通知2", "action_id": "act_notice",
                 "params": {"msg": {"value_type": "literal", "value": "hello2"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end", "label": "结束"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "a1"},
                      {"id": "e2", "from": "a1", "to": "a2"},
                      {"id": "e3", "from": "a2", "to": "end"}]}
    r = mk_wf(client, token, pid, spec)
    wf_id = wf_id_of(r)
    r = publish(client, token, pid, wf_id)
    check("TC-14-01.publish.serial", r.status_code == 200, f"body={r.text[:300]}", "发布成功")
    before = mocks["ok"].call_count()
    r = run_wf(client, token, pid, wf_id)
    run = data_of(r)
    check("TC-14-01.run.succeeded", r.status_code == 200 and run.get("status") == "succeeded",
          f"status={run.get('status')} body={r.text[:300]}", "运行 succeeded")
    ns = run.get("node_states") or {}
    check("TC-14-01.nodes.states", all(ns.get(i, {}).get("status") == "success"
                                       for i in ("start", "a1", "a2", "end")),
          f"states={ {k: v.get('status') for k, v in ns.items()} }", "各节点 success（逐步状态可查）")
    check("TC-14-01.external.calls", mocks["ok"].call_count() - before == 2,
          f"delta={mocks['ok'].call_count() - before}", "低风险自动回写 2 次外部调用")

    # ② 条件分支：count=7 > 3 → hi 分支（默认分支未选 → 其下游 not_reached）
    spec = {"name": "WF-branch", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                 "params": {"msg": {"value_type": "literal", "value": "br1"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "br", "kind": "branch", "label": "分支",
                 "branches": {"hi": {"condition": {
                        "logic": "AND",
                        "rules": [{"field": "node:a1.body.count", "op": "gt", "value": 3}]},
                     "label": "大"},
                     "lo": {"is_default": True, "label": "小"}}},
                {"id": "a2", "kind": "action", "label": "高路径", "action_id": "act_notice",
                 "params": {"v": {"value_type": "field_ref", "value": "node:a1.body.count"},
                            "msg": {"value_type": "literal", "value": "br2"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "a1"},
                      {"id": "e2", "from": "a1", "to": "br"},
                      {"id": "e3", "from": "br", "to": "a2", "from_port": "hi"},
                      {"id": "e4", "from": "br", "to": "end", "from_port": "lo"}]}
    r = mk_wf(client, token, pid, spec)
    wf_br = wf_id_of(r)
    check("TC-14-01.publish.branch", publish(client, token, pid, wf_br).status_code == 200)
    before = mocks["ok"].call_count()
    run = data_of(run_wf(client, token, pid, wf_br))
    ns = run.get("node_states") or {}
    check("TC-14-01.branch.selected", run.get("status") == "succeeded"
          and (ns.get("br") or {}).get("output", {}).get("selected_port") == "hi"
          and ns.get("a2", {}).get("status") == "success",
          f"status={run.get('status')} br_out={(ns.get('br') or {}).get('output')} a2={ns.get('a2', {}).get('status')}",
          "gt 7>3 命中 hi 分支，a2 执行成功")
    check("TC-14-01.branch.fieldref", mocks["ok"].call_count() - before == 2,
          f"delta={mocks['ok'].call_count() - before}", "field_ref node:a1.body.count 解析生效（a1+a2 两次外呼）")

    # ③ 中风险 → AI 不可用 undecidable → 升级人工（暂停）→ 确认后执行（TCE-13-02 编排内闭环）
    spec = {"name": "WF-medium", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "am", "kind": "action", "label": "同步", "action_id": "act_sync",
                 "params": {"stage": {"value_type": "literal", "value": "med1"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "am"}, {"id": "e2", "from": "am", "to": "end"}]}
    r = mk_wf(client, token, pid, spec)
    wf_med = wf_id_of(r)
    check("TC-14-01.publish.medium", publish(client, token, pid, wf_med).status_code == 200)
    before = mocks["ok2"].call_count()
    run = data_of(run_wf(client, token, pid, wf_med))
    ns = run.get("node_states") or {}
    pa = (ns.get("am") or {}).get("pending_action") or {}
    check("TC-14-01.medium.paused", run.get("status") == "awaiting_confirmation"
          and run.get("pending_node") == "am"
          and (pa.get("evaluation") or {}).get("verdict") == "undecidable",
          f"status={run.get('status')} eval={(pa.get('evaluation') or {}).get('verdict')}",
          "中风险 AI 无法判定 → 升级人工暂停（失败≠通过）")
    check("TC-14-01.medium.noexternal", mocks["ok2"].call_count() == before,
          f"delta={mocks['ok2'].call_count() - before}", "升级人工前无外部调用")
    r = req(client, "POST", f"/api/orchestration/runs/{run['run_id']}/confirm", token,
            json_body={"node_id": "am", "reason": "评估通过，人工确认"},
            params={"project_id": pid})
    run2 = data_of(r)
    ns2 = run2.get("node_states") or {}
    check("TC-14-01.medium.confirmed", r.status_code == 200 and run2.get("status") == "succeeded"
          and ns2.get("am", {}).get("status") == "success"
          and (ns2.get("am") or {}).get("confirmed_by"),
          f"status={run2.get('status')} am={ns2.get('am', {}).get('status')} body={r.text[:300]}",
          "确认后执行完成，签核标识（confirmed_by）留存")
    check("TC-14-01.medium.external", mocks["ok2"].call_count() - before == 1,
          f"delta={mocks['ok2'].call_count() - before}", "确认后恰好一次外部回写")

    # ④ 高风险 → 必须人工确认；拒绝 → 取消，无外部调用
    spec = {"name": "WF-high", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "ah", "kind": "action", "label": "归档", "action_id": "act_archive",
                 "params": {"ts": {"value_type": "literal", "value": "hi1"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "ah"}, {"id": "e2", "from": "ah", "to": "end"}]}
    r = mk_wf(client, token, pid, spec)
    wf_hi = wf_id_of(r)
    check("TC-14-01.publish.high", publish(client, token, pid, wf_hi).status_code == 200)
    before = mocks["ok"].call_count()
    run = data_of(run_wf(client, token, pid, wf_hi))
    check("TC-14-01.high.paused", run.get("status") == "awaiting_confirmation"
          and run.get("pending_node") == "ah",
          f"status={run.get('status')}", "高风险运行暂停待人工确认")
    r = req(client, "POST", f"/api/orchestration/runs/{run['run_id']}/reject", token,
            json_body={"node_id": "ah", "reason": "本批数据不准"}, params={"project_id": pid})
    run2 = data_of(r)
    ns2 = run2.get("node_states") or {}
    check("TC-14-01.high.rejected", r.status_code == 200 and run2.get("status") == "cancelled"
          and ns2.get("ah", {}).get("status") == "failed",
          f"status={run2.get('status')} ah={ns2.get('ah', {}).get('status')} body={r.text[:300]}",
          "拒绝 → 运行取消，节点失败留痕")
    check("TC-14-01.high.noexternal", mocks["ok"].call_count() == before,
          f"delta={mocks['ok'].call_count() - before}", "拒绝不产生外部调用")
    return {"serial": wf_id, "branch": wf_br, "medium": wf_med, "high": wf_hi}


# ===================== confirm 控制节点 / TTL =====================
def test_confirm_node(client, token, pid, mocks):
    spec = {"name": "WF-confirm", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "cf", "kind": "confirm", "label": "人工确认点",
                 "confirm_title": "确认继续执行", "failure_policy": "abort"},
                {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                 "params": {"msg": {"value_type": "literal", "value": "cf1"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "cf"},
                      {"id": "e2", "from": "cf", "to": "a1"},
                      {"id": "e3", "from": "a1", "to": "end"}]}
    r = mk_wf(client, token, pid, spec)
    wf_id = wf_id_of(r)
    check("TC-14-01.publish.confirm", publish(client, token, pid, wf_id).status_code == 200)

    # ① 正常确认 → 继续执行完成
    run = data_of(run_wf(client, token, pid, wf_id))
    ns = run.get("node_states") or {}
    check("TC-14-01.confirm.paused", run.get("status") == "awaiting_confirmation"
          and run.get("pending_node") == "cf" and ns.get("a1", {}).get("status") == "not_executed",
          f"status={run.get('status')} a1={ns.get('a1', {}).get('status')}",
          "confirm 控制节点暂停，下游未执行")
    r = req(client, "POST", f"/api/orchestration/runs/{run['run_id']}/confirm", token,
            json_body={"node_id": "cf", "reason": "ok"}, params={"project_id": pid})
    run2 = data_of(r)
    ns2 = run2.get("node_states") or {}
    check("TC-14-01.confirm.done", r.status_code == 200 and run2.get("status") == "succeeded"
          and (ns2.get("cf") or {}).get("output", {}).get("confirmed_by")
          and ns2.get("a1", {}).get("status") == "success",
          f"status={run2.get('status')} cf_out={(ns2.get('cf') or {}).get('output')} body={r.text[:300]}",
          "确认 → 节点成功（签核标识）→ 下游执行完成")

    # ② 拒绝 → 取消，下游 not_reached
    run = data_of(run_wf(client, token, pid, wf_id))
    r = req(client, "POST", f"/api/orchestration/runs/{run['run_id']}/reject", token,
            json_body={"node_id": "cf", "reason": "不同意"}, params={"project_id": pid})
    run2 = data_of(r)
    ns2 = run2.get("node_states") or {}
    check("TC-14-01.confirm.rejected", r.status_code == 200 and run2.get("status") == "cancelled"
          and ns2.get("a1", {}).get("status") == "not_reached",
          f"status={run2.get('status')} a1={ns2.get('a1', {}).get('status')}",
          "拒绝 → 取消，下游 not_reached（不执行）")

    # ③ 确认超时 → 409 CONFIRMATION_TIMEOUT，运行取消留痕（EP-13-03 等价）
    run = data_of(run_wf(client, token, pid, wf_id))
    from core.storage import JsonStore
    store = JsonStore("workflow_runs", project_id=pid)
    rec = store.get(run["run_id"])
    rec["node_states"]["cf"]["awaiting_since"] = "2000-01-01T00:00:00+00:00"
    store.update(run["run_id"], rec)
    r = req(client, "POST", f"/api/orchestration/runs/{run['run_id']}/confirm", token,
            json_body={"node_id": "cf", "reason": "迟到了"}, params={"project_id": pid})
    body = r.text
    run2 = data_of(req(client, "GET", f"/api/orchestration/runs/{run['run_id']}", token,
                       params={"project_id": pid}))
    check("TC-14-01.confirm.timeout", r.status_code == 409 and "CONFIRMATION_TIMEOUT" in body
          and run2.get("status") == "cancelled",
          f"code={r.status_code} body={body[:300]} final={run2.get('status')}",
          "确认超时 → 409，运行取消（操作者+原因留痕）")
    return wf_id


# ===================== TC-14-02 / TCE-14-01 失败策略 =====================
def test_failure_policy(client, token, pid, mocks):
    mocks["fail"].set_script([500, 500, 500, 500, 500, 500, 500, 500, 500, 500])

    def spec_of(policy: str, name: str):
        return {"name": name, "trigger": {"type": "manual"},
                "nodes": [
                    {"id": "start", "kind": "start"},
                    {"id": "af", "kind": "action", "label": "必败", "action_id": "act_fail",
                     "params": {"msg": {"value_type": "literal", "value": "f1"}},
                     "failure_policy": policy, "retry_max": 2},
                    {"id": "b1", "kind": "action", "label": "下游", "action_id": "act_notice",
                     "params": {"msg": {"value_type": "literal", "value": "f2"}},
                     "failure_policy": "retry", "retry_max": 1},
                    {"id": "end", "kind": "end"},
                ],
                "edges": [{"id": "e1", "from": "start", "to": "af"},
                          {"id": "e2", "from": "af", "to": "b1"},
                          {"id": "e3", "from": "b1", "to": "end"}]}

    # retry 耗尽 → 运行 failed，下游 not_reached（现场保留）
    wf_id = wf_id_of(mk_wf(client, token, pid, spec_of("retry", "WF-fail-retry")))
    check("TC-14-02.publish.retry", publish(client, token, pid, wf_id).status_code == 200)
    run = data_of(run_wf(client, token, pid, wf_id))
    ns = run.get("node_states") or {}
    check("TCE-14-01.retry.exhausted", run.get("status") == "failed"
          and ns.get("af", {}).get("status") == "failed"
          and ns.get("af", {}).get("error")
          and ns.get("b1", {}).get("status") == "not_reached",
          f"status={run.get('status')} af={ns.get('af', {}).get('status')!r} "
          f"err={(ns.get('af') or {}).get('error')} b1={ns.get('b1', {}).get('status')}",
          "重试耗尽 → failed，错误现场保留，下游不执行")

    # skip → 跳过继续，运行完成
    wf_id = wf_id_of(mk_wf(client, token, pid, spec_of("skip", "WF-fail-skip")))
    check("TC-14-02.publish.skip", publish(client, token, pid, wf_id).status_code == 200)
    run = data_of(run_wf(client, token, pid, wf_id))
    ns = run.get("node_states") or {}
    check("TC-14-02.skip.continued", run.get("status") == "succeeded"
          and ns.get("af", {}).get("status") == "skipped"
          and ns.get("b1", {}).get("status") == "success",
          f"status={run.get('status')} af={ns.get('af', {}).get('status')} b1={ns.get('b1', {}).get('status')}",
          "skip 策略 → 跳过失败节点继续下游，运行成功")

    # abort → 运行 aborted，下游不执行
    wf_id = wf_id_of(mk_wf(client, token, pid, spec_of("abort", "WF-fail-abort")))
    check("TC-14-02.publish.abort", publish(client, token, pid, wf_id).status_code == 200)
    run = data_of(run_wf(client, token, pid, wf_id))
    ns = run.get("node_states") or {}
    check("TC-14-02.abort.stopped", run.get("status") == "aborted"
          and ns.get("af", {}).get("status") == "failed"
          and ns.get("b1", {}).get("status") == "not_reached",
          f"status={run.get('status')} b1={ns.get('b1', {}).get('status')}",
          "abort 策略 → 运行中止，下游不执行")


# ===================== TCE-14-02 条件字段异常（不静默走默认） =====================
def test_condition_error(client, token, pid, mocks):
    def spec_of(name: str, cond: dict, tag: str):
        return {"name": name, "trigger": {"type": "manual"},
                "nodes": [
                    {"id": "start", "kind": "start"},
                    {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                     "params": {"msg": {"value_type": "literal", "value": f"{tag}a1"}},
                     "failure_policy": "retry", "retry_max": 1},
                    {"id": "br", "kind": "branch",
                     "branches": {"p": {"condition": cond},
                                  "d": {"is_default": True, "label": "默认"}}},
                    {"id": "a2", "kind": "action", "label": "命中才执行", "action_id": "act_notice",
                     "params": {"msg": {"value_type": "literal", "value": f"{tag}a2"}},
                     "failure_policy": "retry", "retry_max": 1},
                    {"id": "end", "kind": "end"},
                ],
                "edges": [{"id": "e1", "from": "start", "to": "a1"},
                          {"id": "e2", "from": "a1", "to": "br"},
                          {"id": "e3", "from": "br", "to": "a2", "from_port": "p"},
                          {"id": "e4", "from": "br", "to": "end", "from_port": "d"}]}

    # ① 引用不存在的字段 → ConditionError → 节点报错，不静默落默认
    wf_id = wf_id_of(mk_wf(client, token, pid, spec_of("WF-cond-missing", {
        "logic": "AND",
        "rules": [{"field": "node:a1.nope", "op": "eq", "value": 1}]}, "cm")))
    check("TCE-14-02.publish.missing", publish(client, token, pid, wf_id).status_code == 200)
    run = data_of(run_wf(client, token, pid, wf_id))
    ns = run.get("node_states") or {}
    check("TCE-14-02.missing.error", run.get("status") == "failed"
          and ns.get("br", {}).get("status") == "failed"
          and ns.get("br", {}).get("error")
          and ns.get("a2", {}).get("status") == "not_reached",
          f"status={run.get('status')} br_err={(ns.get('br') or {}).get('error')} "
          f"a2={ns.get('a2', {}).get('status')}",
          "字段缺失 → 节点错误标记+原因，不静默走默认分支，下游不执行")

    # ② 类型不匹配（str 与 number 比 gt）→ ConditionError
    wf_id = wf_id_of(mk_wf(client, token, pid, spec_of("WF-cond-typemismatch", {
        "logic": "AND",
        "rules": [{"field": "node:a1.body.stage", "op": "gt", "value": 3}]}, "ct")))
    check("TCE-14-02.publish.typemismatch", publish(client, token, pid, wf_id).status_code == 200)
    run = data_of(run_wf(client, token, pid, wf_id))
    ns = run.get("node_states") or {}
    check("TCE-14-02.typemismatch.error", run.get("status") == "failed"
          and ns.get("br", {}).get("status") == "failed"
          and ns.get("a2", {}).get("status") == "not_reached",
          f"status={run.get('status')} br_err={(ns.get('br') or {}).get('error')}",
          "类型不符 → 节点报错（不静默），下游不执行")


# ===================== TC-14-03 no-code 规格（API 层负向） =====================
def test_spec_negatives(client, token, pid, mocks):
    base = {"name": "WF-neg", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                 "params": {}, "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "a1"}, {"id": "e2", "from": "a1", "to": "end"}]}

    def mk(spec: dict):
        return req(client, "POST", "/api/orchestration/workflows", token,
                   json_body=spec, params={"project_id": pid})

    # ① 参数取值来源非三选一 → 拒绝
    spec = json.loads(json.dumps(base))
    spec["nodes"][1]["params"] = {"x": {"value_type": "code", "value": "1+1"}}
    r = mk(spec)
    check("TC-14-03.param.source", r.status_code == 422, f"code={r.status_code} body={r.text[:200]}",
          "参数来源仅 literal/field_ref/trigger_ctx，其它 422")

    # ② 条件算子非白名单 → 拒绝（创建或发布处拦截）
    spec = json.loads(json.dumps(base))
    spec["nodes"].insert(2, {"id": "br", "kind": "branch",
                             "branches": {"p": {"condition": {"logic": "AND",
                                                              "rules": [{"field": "node:a1.body.count",
                                                                         "op": "regex",
                                                                         "value": "^7$"}]}},
                                          "d": {"is_default": True}}})
    spec["edges"] = [{"id": "e1", "from": "start", "to": "a1"},
                     {"id": "e2", "from": "a1", "to": "br"},
                     {"id": "e3", "from": "br", "to": "end", "from_port": "p"},
                     {"id": "e4", "from": "br", "to": "end", "from_port": "d"}]
    r = mk(spec)
    r2 = req(client, "POST", f"/api/orchestration/workflows/{wf_id_of(r)}/publish", token,
             params={"project_id": pid}) if r.status_code == 201 else None
    check("TC-14-03.cond.op", (r.status_code == 422) or (r2 is not None and r2.status_code == 422),
          f"create={r.status_code} publish={(r2.status_code if r2 else '-')}",
          "条件算子仅白名单＋AND/OR，其它 422")

    # ③ 触发类型非 manual/event/cron → 拒绝
    spec = json.loads(json.dumps(base))
    spec["trigger"] = {"type": "schedule"}
    r = mk(spec)
    check("TC-14-03.trigger.type", r.status_code == 422, f"code={r.status_code}",
          "触发类型仅 manual/event/cron")

    # ④ cron 表达式非法（6 段/越界）→ 拒绝
    spec = json.loads(json.dumps(base))
    spec["trigger"] = {"type": "cron", "cron_expr": "61 * * * * *"}
    r = mk(spec)
    check("TC-14-03.trigger.cron", r.status_code == 422, f"code={r.status_code} body={r.text[:200]}",
          "cron 非 5 段/字段越界 → 422")

    # ⑤ 结构负向：双 start / 无 end / 环 / 自环 / 不可达 / 端口重边
    s2 = json.loads(json.dumps(base)); s2["nodes"].append({"id": "start2", "kind": "start"})
    check("TC-14-03.struct.start2", mk(s2).status_code == 422, "", "恰好一个 start")
    s3 = json.loads(json.dumps(base)); s3["nodes"] = [n for n in s3["nodes"] if n["id"] != "end"]
    s3["edges"] = [e for e in s3["edges"] if e["to"] != "end"]
    check("TC-14-03.struct.noend", mk(s3).status_code == 422, "", "至少一个 end")
    s4 = json.loads(json.dumps(base))
    s4["edges"].append({"id": "e3", "from": "end", "to": "start"})
    check("TC-14-03.struct.cycle", mk(s4).status_code == 422, "", "无环")
    s5 = json.loads(json.dumps(base)); s5["edges"].append({"id": "e3", "from": "a1", "to": "a1"})
    check("TC-14-03.struct.selfloop", mk(s5).status_code == 422, "", "无自环")
    s6 = json.loads(json.dumps(base)); s6["nodes"].append({"id": "orphan", "kind": "action",
                                                           "label": "孤儿", "action_id": "act_notice"})
    check("TC-14-03.struct.unreachable", mk(s6).status_code == 422, "", "节点从 start 可达")
    s7 = json.loads(json.dumps(base))
    s7["nodes"].insert(2, {"id": "br", "kind": "branch",
                           "branches": {"p": {"condition": {"logic": "AND",
                                                            "rules": [{"field": "node:a1.body.count",
                                                                       "op": "eq",
                                                                       "value": 7}]}},
                                        "d": {"is_default": True}}})
    s7["edges"] = [{"id": "e1", "from": "start", "to": "a1"}, {"id": "e2", "from": "a1", "to": "br"},
                   {"id": "e3", "from": "br", "to": "end", "from_port": "p"},
                   {"id": "e4", "from": "br", "to": "end", "from_port": "p"}]
    check("TC-14-03.struct.portdup", mk(s7).status_code == 422, "", "同端口唯一出边")

    # ⑥ 节点类型非六类 → 拒绝
    s8 = json.loads(json.dumps(base)); s8["nodes"][1]["kind"] = "script"
    r = mk(s8)
    check("TC-14-03.node.kind", r.status_code == 422, f"code={r.status_code}",
          "节点类型仅 start/end/action/branch/confirm/capability_gap")


# ===================== TC-14-04 无代码红线（API 负向） =====================
def test_redline(client, token, pid, mocks):
    base = {"name": "WF-red", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                 "params": {}, "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "a1"}, {"id": "e2", "from": "a1", "to": "end"}]}

    # ① 节点夹带 code 字段（Pydantic 会静默忽略 → 红线层必须先拦）
    spec = json.loads(json.dumps(base))
    spec["nodes"][1]["code"] = "def hack():\n    import os\n    os.system('rm -rf /')"
    r = req(client, "POST", "/api/orchestration/workflows", token,
            json_body=spec, params={"project_id": pid})
    check("TC-14-04.redline.code", r.status_code == 422 and "CODE_REJECTED" in r.text,
          f"code={r.status_code} body={r.text[:300]}", "代码字段 → 422 CODE_REJECTED")

    # ② 表达式式取值 → 拒绝
    spec = json.loads(json.dumps(base))
    spec["nodes"][1]["params"] = {"x": {"value_type": "literal", "value": "eval(__import__('os'))"}}
    r = req(client, "POST", "/api/orchestration/workflows", token,
            json_body=spec, params={"project_id": pid})
    check("TC-14-04.redline.expr", r.status_code == 422,
          f"code={r.status_code} body={r.text[:300]}", "表达式/脚本 token → 422")

    # ③ 生成接口同样过红线（源文本含代码 → 整段拒绝）
    r = req(client, "POST", "/api/orchestration/workflows/generate", token,
            json_body={"text": "步骤：\n1. 执行 os.system('curl evil')\n2. 发送通知",
                       "source_type": "sop"},
            params={"project_id": pid})
    check("TC-14-04.redline.generate", r.status_code == 422 and "CODE_REJECTED" in r.text,
          f"code={r.status_code} body={r.text[:300]}", "生成源文本含代码 → 整段拒绝")

    # ④ 能力缺口节点允许存在（缺口不生成代码节点，走显式节点）
    spec = json.loads(json.dumps(base))
    spec["nodes"].insert(2, {"id": "g1", "kind": "capability_gap", "label": "缺口",
                             "gap_reason": "待人工补建"})
    spec["edges"] = [{"id": "e1", "from": "start", "to": "a1"},
                     {"id": "e2", "from": "a1", "to": "g1"},
                     {"id": "e3", "from": "g1", "to": "end"}]
    r = mk_wf(client, token, pid, spec)
    check("TC-14-04.gap.allowed", r.status_code == 201, f"body={r.text[:300]}",
          "capability_gap 为显式合法节点（不生成代码节点）")


# ===================== TC-14-05 dry-run =====================
def test_dry_run(client, token, pid, mocks):
    spec = {"name": "WF-dry", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                 "params": {"msg": {"value_type": "literal", "value": "dry"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "a2", "kind": "action", "label": "归档", "action_id": "act_archive",
                 "params": {"ts": {"value_type": "literal", "value": "dry2"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "a1"},
                      {"id": "e2", "from": "a1", "to": "a2"},
                      {"id": "e3", "from": "a2", "to": "end"}]}
    r = mk_wf(client, token, pid, spec)
    wf_id = wf_id_of(r)
    before = mocks["ok"].call_count()
    r = req(client, "POST", f"/api/orchestration/workflows/{wf_id}/dry-run", token,
            json_body={"payload": {}}, params={"project_id": pid})
    res = data_of(r)
    preview = {n.get("node_id") or n.get("id"): n for n in (res.get("nodes") or [])}
    check("TC-14-05.dry.ok", r.status_code == 200 and res.get("dry_run") is True and preview,
          f"body={r.text[:400]}", "dry-run 200 且返回节点预览")
    check("TC-14-05.dry.noexternal", mocks["ok"].call_count() == before,
          f"delta={mocks['ok'].call_count() - before}", "dry-run 无任何外部调用")
    a1p, a2p = preview.get("a1") or {}, preview.get("a2") or {}
    check("TC-14-05.dry.riskpreview",
          a1p.get("status") == "preview_ok" and a2p.get("status") == "awaiting_confirmation",
          f"a1={a1p.get('status')} a2={a2p.get('status')} "
          f"preview_a2={json.dumps(a2p, ensure_ascii=False)[:200]}",
          "低风险预览可自动执行；高风险节点展示待确认态")


# ===================== TCE-14-03 事件触发 / required_ctx =====================
def test_event_trigger(client, token, pid, mocks):
    spec = {"name": "WF-event", "trigger": {"type": "event", "event_topic": "order.created",
                                            "required_ctx": ["order_id"]},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                 "params": {"oid": {"value_type": "trigger_ctx", "value": "trigger.order_id"},
                            "msg": {"value_type": "literal", "value": "ev1"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "a1"}, {"id": "e2", "from": "a1", "to": "end"}]}
    r = mk_wf(client, token, pid, spec)
    wf_id = wf_id_of(r)
    check("TCE-14-03.publish.event", publish(client, token, pid, wf_id).status_code == 200)
    before = mocks["ok"].call_count()

    # ① 缺 required_ctx → 拒绝启动并报缺失项（不半启动）
    r = req(client, "POST", "/api/orchestration/events", token,
            json_body={"topic": "order.created", "payload": {}}, params={"project_id": pid})
    res = data_of(r)
    rr = (res.get("results") or [{}])[0]
    check("TCE-14-03.ctx.missing", r.status_code == 200 and res.get("matched_workflows") == 1
          and rr.get("started") is False and rr.get("error_code") == "TRIGGER_CTX_MISSING",
          f"body={json.dumps(res, ensure_ascii=False)[:300]}",
          "缺 required_ctx 字段 → 拒绝启动并报缺失项（TRIGGER_CTX_MISSING）")
    check("TCE-14-03.ctx.noexternal", mocks["ok"].call_count() == before,
          f"delta={mocks['ok'].call_count() - before}", "被拒工作流无任何节点执行（不半启动）")

    # ② 完整 payload → 启动成功
    r = req(client, "POST", "/api/orchestration/events", token,
            json_body={"topic": "order.created", "payload": {"order_id": "o-1001"}},
            params={"project_id": pid})
    res = data_of(r)
    rr = (res.get("results") or [{}])[0]
    run = get_run(client, token, pid, rr.get("run_id") or "")
    check("TCE-14-03.ctx.ok", r.status_code == 200 and rr.get("started") is True
          and run.get("status") == "succeeded",
          f"rr={json.dumps(rr, ensure_ascii=False)[:200]} run={run.get('status')}",
          "完整 payload → 事件触发启动并完成")

    # ③ 主题不匹配 → 0 匹配
    r = req(client, "POST", "/api/orchestration/events", token,
            json_body={"topic": "other.topic", "payload": {"order_id": "x"}},
            params={"project_id": pid})
    check("TCE-14-03.topic.nomatch", data_of(r).get("matched_workflows") == 0,
          f"body={r.text[:200]}", "非匹配主题不触发")


# ===================== TC-14-01 cron 触发（tick 直驱＋分钟去重） =====================
def test_cron_trigger(client, token, pid, mocks):
    spec = {"name": "WF-cron", "trigger": {"type": "cron", "cron_expr": "* * * * *",
                                           "static_payload": {}},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                 "params": {"msg": {"value_type": "literal", "value": "cr1"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "a1"}, {"id": "e2", "from": "a1", "to": "end"}]}
    r = mk_wf(client, token, pid, spec)
    wf_id = wf_id_of(r)
    check("TC-14-01.publish.cron", publish(client, token, pid, wf_id).status_code == 200)
    from orchestration import orchestrator
    fired1 = orchestrator.tick_cron()
    mine1 = [f for f in fired1 if f.get("workflow_id") == wf_id and f.get("run_id")]
    check("TC-14-01.cron.fire", len(mine1) == 1,
          f"fired={json.dumps(fired1, ensure_ascii=False)[:300]}", "cron 到点触发一次")
    fired2 = orchestrator.tick_cron()
    mine2 = [f for f in fired2 if f.get("workflow_id") == wf_id and f.get("run_id")]
    check("TC-14-01.cron.dedup", len(mine2) == 0,
          f"fired={json.dumps(fired2, ensure_ascii=False)[:300]}", "同分钟重复 tick 去重（不重复触发）")


# ===================== TCE-14-05 能力缺口发布阻断 =====================
def test_gap_publish_block(client, token, pid, mocks):
    spec = {"name": "WF-gap", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "g1", "kind": "capability_gap", "label": "缺口", "gap_reason": "待补建能力"},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "g1"}, {"id": "e2", "from": "g1", "to": "end"}]}
    r = mk_wf(client, token, pid, spec)
    wf_id = wf_id_of(r)
    r = publish(client, token, pid, wf_id)
    check("TCE-14-05.publish.blocked", r.status_code == 409 and "CAPABILITY_GAP_UNRESOLVED" in r.text,
          f"code={r.status_code} body={r.text[:300]}", "存在 capability_gap → 发布被拦 409")
    # 移除缺口 → 可发布
    r = req(client, "PUT", f"/api/orchestration/workflows/{wf_id}", token,
            json_body={"name": "WF-gap", "description": "", "trigger": {"type": "manual"},
                       "nodes": [
                           {"id": "start", "kind": "start"},
                           {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                            "params": {"msg": {"value_type": "literal", "value": "gp1"}},
                            "failure_policy": "retry", "retry_max": 1},
                           {"id": "end", "kind": "end"}],
                       "edges": [{"id": "e1", "from": "start", "to": "a1"},
                                 {"id": "e2", "from": "a1", "to": "end"}]},
            params={"project_id": pid})
    check("TCE-14-05.update.ok", r.status_code == 200, f"body={r.text[:300]}", "更新移除缺口成功")
    r = publish(client, token, pid, wf_id)
    check("TCE-14-05.publish.unblocked", r.status_code == 200, f"body={r.text[:300]}",
          "缺口解除后可发布")
    # 已发布 → 不可再改（409）
    r = req(client, "PUT", f"/api/orchestration/workflows/{wf_id}", token,
            json_body={"name": "WF-gap-x", "description": "", "trigger": {"type": "manual"},
                       "nodes": get_wf(client, token, pid, wf_id)["nodes"],
                       "edges": get_wf(client, token, pid, wf_id)["edges"]},
            params={"project_id": pid})
    check("TC-14-01.published.immutable", r.status_code == 409, f"code={r.status_code}",
          "已发布工作流不可修改（需先下线）")


# ===================== TC-14-10 执行侧 AI 边界（零 LLM＋可复现） =====================
def test_execution_determinism(client, token, pid, mocks):
    import ai_helper.client as aic
    calls: list[str] = []
    orig = aic.call_llm

    def _boom(*a, **k):
        calls.append("called")
        raise RuntimeError("执行路径禁止调用 LLM（TC-14-10）")

    aic.call_llm = _boom
    try:
        spec = {"name": "WF-determ", "trigger": {"type": "manual"},
                "nodes": [
                    {"id": "start", "kind": "start"},
                    {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                     "params": {"msg": {"value_type": "literal", "value": "dt1"}},
                     "failure_policy": "retry", "retry_max": 1},
                    {"id": "br", "kind": "branch",
                     "branches": {"p": {"condition": {"logic": "AND",
                                                      "rules": [{"field": "node:a1.body.count",
                                                                 "op": "gt", "value": 3}]}},
                                  "d": {"is_default": True}}},
                    {"id": "a2", "kind": "action", "label": "高路径", "action_id": "act_notice",
                     "params": {"msg": {"value_type": "literal", "value": "dt2"}},
                     "failure_policy": "retry", "retry_max": 1},
                    {"id": "end", "kind": "end"},
                ],
                "edges": [{"id": "e1", "from": "start", "to": "a1"},
                          {"id": "e2", "from": "a1", "to": "br"},
                          {"id": "e3", "from": "br", "to": "a2", "from_port": "p"},
                          {"id": "e4", "from": "br", "to": "end", "from_port": "d"}]}
        r = mk_wf(client, token, pid, spec)
        wf_id = wf_id_of(r)
        check("TC-14-10.publish", publish(client, token, pid, wf_id).status_code == 200)
        traces = []
        for i in (1, 2):
            run = data_of(run_wf(client, token, pid, wf_id))
            ns = run.get("node_states") or {}
            traces.append(((ns.get("br") or {}).get("output") or {}).get("selected_port"))
            check(f"TC-14-10.run{i}.succeeded", run.get("status") == "succeeded",
                  f"status={run.get('status')}", f"第 {i} 次执行成功")
        check("TC-14-10.zerollm", not calls, f"calls={calls}",
              "执行路径（含中风险分流判断）零 LLM 调用")
        check("TC-14-10.reproducible", traces[0] == traces[1] == "p",
          f"traces={traces}", "同一编排+同一触发多次执行，分支路径可复现（均命中 p 端口）")
    finally:
        aic.call_llm = orig


# ===================== TCEE-04 运行中断 → 重启恢复（确定性重入＋幂等） =====================
def test_interrupt_resume(client, token, pid, mocks):
    from orchestration import orchestrator
    from core.storage import JsonStore

    spec = {"name": "WF-resume", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "cf", "kind": "confirm", "label": "确认点", "confirm_title": "确认继续",
                 "failure_policy": "abort"},
                {"id": "a1", "kind": "action", "label": "通知", "action_id": "act_notice",
                 "params": {"msg": {"value_type": "literal", "value": "rs1"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "cf"},
                      {"id": "e2", "from": "cf", "to": "a1"},
                      {"id": "e3", "from": "a1", "to": "end"}]}
    r = mk_wf(client, token, pid, spec)
    wf_id = wf_id_of(r)
    check("TCEE-04.publish", publish(client, token, pid, wf_id).status_code == 200)
    run = data_of(run_wf(client, token, pid, wf_id))
    rid = run["run_id"]

    # ① 模拟崩溃：recover_interrupted 把在途运行（此处停在确认点）标 interrupted
    recovered = orchestrator.recover_interrupted()
    mine = [x for x in recovered if x.get("run_id") == rid]
    run2 = get_run(client, token, pid, rid)
    check("TCEE-04.recover", bool(mine) and run2.get("status") == "interrupted"
          and run2.get("interrupted_at"),
          f"recovered={len(recovered)} status={run2.get('status')}",
          "启动扫描：在途运行 → interrupted（留痕）")

    # ② resume → 恢复为待确认（确认点保留），确认后继续执行完成
    r = req(client, "POST", f"/api/orchestration/runs/{rid}/resume", token,
            params={"project_id": pid})
    run3 = data_of(r)
    check("TCEE-04.resume.toconfirm", r.status_code == 200
          and run3.get("status") == "awaiting_confirmation" and run3.get("pending_node") == "cf",
          f"status={run3.get('status')} pending={run3.get('pending_node')} body={r.text[:200]}",
          "resume 确定性重入：停在确认点 → 待确认")
    r = req(client, "POST", f"/api/orchestration/runs/{rid}/confirm", token,
            json_body={"node_id": "cf", "reason": "恢复后确认"}, params={"project_id": pid})
    run4 = data_of(r)
    ns4 = run4.get("node_states") or {}
    check("TCEE-04.resume.done", r.status_code == 200 and run4.get("status") == "succeeded"
          and ns4.get("a1", {}).get("status") == "success",
          f"status={run4.get('status')} a1={ns4.get('a1', {}).get('status')} body={r.text[:300]}",
          "确认后恢复执行并跑完")

    # ③ 模拟"执行中崩溃"：白盒把 a1 置 running + 运行 interrupted → resume 确定性重入
    spec2 = json.loads(json.dumps({"name": "WF-resume2", "trigger": {"type": "manual"},
                                   "nodes": [
                                       {"id": "start", "kind": "start"},
                                       {"id": "a1", "kind": "action", "label": "通知",
                                        "action_id": "act_notice",
                                        "params": {"msg": {"value_type": "literal", "value": "rs2"}},
                                        "failure_policy": "retry", "retry_max": 1},
                                       {"id": "end", "kind": "end"}],
                                   "edges": [{"id": "e1", "from": "start", "to": "a1"},
                                             {"id": "e2", "from": "a1", "to": "end"}]}))
    r = mk_wf(client, token, pid, spec2)
    wf2 = wf_id_of(r)
    check("TCEE-04.publish2", publish(client, token, pid, wf2).status_code == 200)
    wf_rec = get_wf(client, token, pid, wf2)
    run5 = orchestrator.start_run(pid, wf_rec, {}, actor={"username": "admin"},
                                  trigger_source="manual")  # 进程内调用，返回运行记录本身
    rid5 = run5["run_id"]
    # start_run 为同步执行（本工作流无确认点 → 已跑完）；此处白盒强制构造
    # "进程死亡时 a1 执行中"现场 → resume 须确定性重入（幂等键防重外呼）
    store = JsonStore("workflow_runs", project_id=pid)
    rec5 = store.get(rid5)
    rec5["status"] = "interrupted"
    rec5["node_states"]["a1"]["status"] = "running"
    rec5["pending_node"] = None
    store.update(rid5, rec5)
    r = req(client, "POST", f"/api/orchestration/runs/{rid5}/resume", token,
            params={"project_id": pid})
    run7 = data_of(r)
    ns7 = run7.get("node_states") or {}
    check("TCEE-04.resume.running", r.status_code == 200 and run7.get("status") == "succeeded"
          and ns7.get("a1", {}).get("status") == "success",
          f"status={run7.get('status')} a1={ns7.get('a1', {}).get('status')} body={r.text[:300]}",
          "执行中节点确定性重入（幂等键防重外呼），运行恢复完成")


# ===================== TC-14-08 草稿与手动编排同通道（审计一致） =====================
def test_draft_same_channel(client, token, pid, pid_gen, mocks):
    import audit as audit_svc
    r = req(client, "POST", "/api/orchestration/workflows/generate", token,
            json_body={"text": "1. 数据同步 到主库\n2. 发送通知 给负责人",
                       "source_type": "sop", "name": "草稿-同通道"},
            params={"project_id": pid_gen})
    rec = data_of(r)
    wf_id = rec.get("id") or ""
    check("TC-14-08.draft.created", r.status_code == 201 and wf_id,
          f"code={r.status_code} body={r.text[:300]}", "生成草稿创建成功")
    check("TC-14-08.draft.draft", get_wf(client, token, pid_gen, wf_id).get("status") == "draft",
          "", "草稿不自动发布（status=draft）")
    # 同通道发布＋执行
    check("TC-14-08.publish", publish(client, token, pid_gen, wf_id).status_code == 200,
          "", "草稿经同一发布通道发布")
    run = data_of(run_wf(client, token, pid_gen, wf_id))
    final = get_run(client, token, pid_gen, run.get("run_id") or "")
    check("TC-14-08.run.ok", final.get("status") in ("succeeded", "awaiting_confirmation"),
          f"status={final.get('status')}", "草稿编排可执行（中/高风险会暂停等确认，属正常分流）")
    started = [x for x in audit_svc.query(action="workflow_run_started")["items"]
               if x.get("target_id") == (final.get("run_id") or "")]
    finished = [x for x in audit_svc.query(action="workflow_run_finished")["items"]
                if x.get("target_id") == (final.get("run_id") or "")]
    check("TC-14-08.audit.same", bool(started) and bool(finished),
          f"started={len(started)} finished={len(finished)}",
          "与手动编排同一审计事件（workflow_run_started/finished）")


# ===================== API 测试总入口 =====================
def run_api_tests(client, token, pid, pid_gen, mocks):
    test_engine_exec(client, token, pid, mocks)
    test_confirm_node(client, token, pid, mocks)
    test_failure_policy(client, token, pid, mocks)
    test_condition_error(client, token, pid, mocks)
    test_spec_negatives(client, token, pid, mocks)
    test_redline(client, token, pid, mocks)
    test_dry_run(client, token, pid, mocks)
    test_event_trigger(client, token, pid, mocks)
    test_cron_trigger(client, token, pid, mocks)
    test_gap_publish_block(client, token, pid, mocks)
    test_execution_determinism(client, token, pid, mocks)
    test_interrupt_resume(client, token, pid, mocks)
    test_draft_same_channel(client, token, pid, pid_gen, mocks)


# ===================== TC-14-06/07 生成：grounding＋缺口＋高危自动确认 =====================
def test_generation(client, token, pid, pid_gen, mocks):
    r = req(client, "POST", "/api/orchestration/workflows/generate", token,
            json_body={"text": "1. 数据同步 到主数据库\n2. 人工打电话回访\n3. 归档旧数据",
                       "source_type": "sop", "name": "草稿-生成验证"},
            params={"project_id": pid_gen})
    rec = data_of(r)
    nodes = rec.get("nodes") or []
    wf_id = rec.get("id") or ""
    check("TC-14-06.gen.ok", r.status_code == 201 and wf_id and len(nodes) >= 4,
          f"code={r.status_code} nodes={len(nodes)} body={r.text[:300]}", "SOP → 草稿（含 start/end）")
    grounded = [n for n in nodes if n.get("kind") == "action"]
    gaps = [n for n in nodes if n.get("kind") == "capability_gap"]
    confirms = [n for n in nodes if n.get("kind") == "confirm"]
    check("TC-14-06.gen.grounded", len(grounded) == 2,
          f"actions={[(n.get('id'), n.get('action_id')) for n in grounded]}",
          "可 grounded 步骤 → action 节点（数据同步/归档旧数据）")
    check("TC-14-06.gen.gap", len(gaps) == 1 and "回访" in (gaps[0].get("gap_reason") or ""),
          f"gaps={[(g.get('id'), g.get('gap_reason')) for g in gaps]}",
          "无对应能力步骤 → capability_gap（不静默丢弃，不生成代码节点）")
    bad_grounding = [n for n in grounded
                     if not (isinstance(n.get("grounding"), dict)
                             and n["grounding"].get("basis")
                             and 0 < float(n["grounding"].get("confidence") or 0) <= 1)]
    check("TC-14-06.gen.grounding", not bad_grounding,
          f"bad={bad_grounding}", "每个节点带 grounding（依据+置信度）")
    # 高危（归档旧数据）前自动挂 confirm
    prev_of = {}
    for e in rec.get("edges") or []:
        prev_of[e.get("to")] = e.get("from")
    gap_node = gaps[0] if gaps else {}
    archive_node = next((n for n in grounded if n.get("action_id") == "gen_archiveold"), {})
    chain_prev = prev_of.get(archive_node.get("id"))
    prev_kind = next((n.get("kind") for n in nodes if n.get("id") == chain_prev), None)
    check("TC-14-07.gen.confirm", len(confirms) >= 1 and prev_kind == "confirm",
          f"confirms={len(confirms)} archive_prev_kind={prev_kind}",
          "高风险节点自动挂人工确认（生成通道不绕过签署护栏）")
    return wf_id


# ===================== TC-14-09 AI 局部生成（边界） =====================
def test_fragment_generation(client, token, pid, pid_gen, mocks):
    # 先造一个含 Action 的草稿作补全宿主
    r = req(client, "POST", "/api/orchestration/workflows/generate", token,
            json_body={"text": "1. 数据同步 全量\n2. 发送通知 完成", "source_type": "sop"},
            params={"project_id": pid_gen})
    host_id = (data_of(r).get("id") or "")
    check("TC-14-09.host", r.status_code == 201 and host_id, f"code={r.status_code}",
          "补全宿主草稿就绪")

    # ① 代码式提示词 → 整段拒绝（红线先行）
    r = req(client, "POST", f"/api/orchestration/workflows/{host_id}/generate-fragment", token,
            json_body={"intent": "加一个节点执行 python -c 'import os; os.system(1)'"},
            params={"project_id": pid_gen})
    check("TC-14-09.fragment.redline", r.status_code == 422 and "CODE_REJECTED" in r.text,
          f"code={r.status_code} body={r.text[:300]}", "代码式提示词 → 422 CODE_REJECTED")

    # ② AI 不可用（本环境未配置 LLM）→ 422 FRAGMENT_GEN_FAILED（确定性回退不自动合并）
    r = req(client, "POST", f"/api/orchestration/workflows/{host_id}/generate-fragment", token,
            json_body={"intent": "在数据同步之后加一步发送通知", "after_node": None},
            params={"project_id": pid_gen})
    check("TC-14-09.fragment.noai", r.status_code == 422 and "FRAGMENT_GEN_FAILED" in r.text,
          f"code={r.status_code} body={r.text[:300]}",
          "AI 不可用 → 拒绝局部生成（不静默合并，不自动发布）")


# ===================== TC-14-12 golden dataset 准确度（冻结阈值） =====================
def _gen_and_score(client, token, pid_gen, sample: dict) -> dict:
    r = req(client, "POST", "/api/orchestration/workflows/generate", token,
            json_body={"text": sample["text"], "source_type": "sop",
                       "name": f"golden-{sample['id']}"},
            params={"project_id": pid_gen})
    rec = data_of(r)
    nodes = rec.get("nodes") or []
    edges = rec.get("edges") or []
    by_id = {n.get("id"): n for n in nodes}
    # 步骤序：按节点 id 中的序号（n*/g*）排序；confirm（cf*）不计入步骤
    step_nodes = [n for n in nodes if re.match(r"^(n|g)\d+$", n.get("id") or "")]
    step_nodes.sort(key=lambda n: int(re.sub(r"\D", "", n.get("id") or "0")))
    expected = sample["expected_steps"]
    tp = fp = fn = 0
    edge_ok = edge_total = 0
    gap_found = 0
    for i, exp in enumerate(expected):
        got = step_nodes[i] if i < len(step_nodes) else None
        kind = (got or {}).get("kind")
        aid = (got or {}).get("action_id")
        if exp == "GAP":
            if kind == "capability_gap":
                gap_found += 1
            else:
                fp += 1  # 期望缺口却生成了 action
        else:
            if kind == "action" and aid == exp:
                tp += 1
            elif kind == "action":
                fp += 1
            else:
                fn += 1  # 期望 action 却成了缺口/缺失
    for i in range(len(step_nodes)):
        if i + 1 >= len(step_nodes):
            break
        cur, nxt = step_nodes[i], step_nodes[i + 1]
        # 允许中间隔着 confirm 节点
        path = {cur["id"]}
        queue = [cur["id"]]
        adj = {}
        for e in edges:
            adj.setdefault(e.get("from"), []).append(e.get("to"))
        while queue:
            x = queue.pop()
            for y in adj.get(x, []):
                if y not in path:
                    path.add(y)
                    queue.append(y)
        edge_total += 1
        if nxt["id"] in path:
            edge_ok += 1
    # 高危确认挂接：expected 中高危 action（gen_archiveold）在真实节点链上的
    # 上游接入节点须为 confirm（cf{seq} 直接前插 n{seq}，不能只看 n/g 序列）
    prev_of: dict[str, str] = {e.get("to"): e.get("from") for e in edges}
    confirm_ok = confirm_total = 0
    for i, exp in enumerate(expected):
        if exp == "gen_archiveold" and i < len(step_nodes):
            confirm_total += 1
            prev = by_id.get(prev_of.get(step_nodes[i]["id"]))
            if prev and prev.get("kind") == "confirm":
                confirm_ok += 1
    return {
        "precision": tp / (tp + fp) if (tp + fp) else 1.0,
        "recall": tp / (tp + fn) if (tp + fn) else 1.0,
        "edge": edge_ok / edge_total if edge_total else 1.0,
        "gap_recall": (gap_found / sum(1 for e in expected if e == "GAP"))
        if any(e == "GAP" for e in expected) else 1.0,  # 无期望缺口的样例：空集记 1.0（阈值不变，仅修正空集语义）
        "confirm": confirm_ok / confirm_total if confirm_total else 1.0,
        "gaps_found": gap_found,
        "steps": len(step_nodes),
    }


def test_golden_dataset(client, token, pid, pid_gen, mocks):
    ds_path = os.path.join(PROJECT_ROOT, "scripts", "golden", "orchestration_dataset.json")
    with open(ds_path, encoding="utf-8") as f:
        ds = json.load(f)
    th = ds["thresholds"]
    agg = {"precision": [], "recall": [], "edge": [], "gap_recall": [], "confirm": []}
    for s in ds["samples"]:
        m = _gen_and_score(client, token, pid_gen, s)
        for k in agg:
            agg[k].append(m[k])
        print(f"  golden[{s['id']}] precision={m['precision']:.2f} recall={m['recall']:.2f} "
              f"edge={m['edge']:.2f} gap_recall={m['gap_recall']:.2f} confirm={m['confirm']:.2f}")
    import statistics
    mean = {k: statistics.mean(v) for k, v in agg.items()}
    check("TC-14-12.precision", mean["precision"] >= th["node_mapping_precision_min"],
          f"mean={mean['precision']:.3f} min={th['node_mapping_precision_min']}", "节点映射精度达标")
    check("TC-14-12.recall", mean["recall"] >= th["node_mapping_recall_min"],
          f"mean={mean['recall']:.3f} min={th['node_mapping_recall_min']}", "节点映射召回达标")
    check("TC-14-12.edge", mean["edge"] >= th["edge_accuracy_min"],
          f"mean={mean['edge']:.3f} min={th['edge_accuracy_min']}", "边结构准确率达标")
    check("TC-14-12.gaprecall", mean["gap_recall"] >= th["gap_recall_min"],
          f"mean={mean['gap_recall']:.3f} min={th['gap_recall_min']}",
          "能力缺口召回 100%（缺口不得静默丢弃）")
    check("TC-14-12.confirm", mean["confirm"] >= th["high_risk_confirm_attach_min"],
          f"mean={mean['confirm']:.3f} min={th['high_risk_confirm_attach_min']}",
          "高风险节点确认挂接率达标")


# ===================== 生成测试总入口 =====================
def run_gen_tests(client, token, pid, pid_gen, mocks):
    test_generation(client, token, pid, pid_gen, mocks)
    test_fragment_generation(client, token, pid, pid_gen, mocks)
    test_golden_dataset(client, token, pid, pid_gen, mocks)


if __name__ == "__main__":
    _setup_env()
    client, token, PID, PID_GEN, MOCKS = build_env()
    run_api_tests(client, token, PID, PID_GEN, MOCKS)  # 本文件下方定义
    run_gen_tests(client, token, PID, PID_GEN, MOCKS)  # 本文件下方定义
    total = len(RESULTS)
    passed = sum(1 for _, ok, _, _ in RESULTS if ok)
    print(f"\n{'=' * 60}\nM3-F 验收：PASS={passed}/{total} FAIL={total - passed}"
          + (f"\nDATA_DIR={DATA_DIR}" if passed < total else ""))
    sys.exit(0 if passed == total else 1)
