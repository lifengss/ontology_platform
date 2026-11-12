# -*- coding: utf-8 -*-
"""
M3-G 里程碑退出——V2-13（回写风险分级）+ V2-14（Action 编排）端到端联验（验收脚本）
====================================================================================
用例（docs/V2-测试用例.md）：
  TCE2E-03 [P1/功能] 备件更换完整回写链路：
      ① 库存查询 → ② 缺货创建采购单 → ③ 实例更新 → ④ 通知 CRM
      预期：V2-13（回写风险分级）+ V2-14（Action 编排）组合；风险分级准确、签核证据链完整。
  TCE2E-07 [P1/功能] 浏览器端回写编排全链路（本脚本验 API 侧闭环；浏览器侧由
      agent-browser 单独验证：拖拽编排/干跑/发布/事件触发/状态·审计·血缘页）：
      ① 编排 → ② 发布前干跑预览（零外呼）→ ③ 发布 → ④ 事件触发（自动启动）
      → ⑤ 节点执行状态/审计（workflow_* + action_writeback 签核证据链）
  TCEE-04 [P1/故障演练] 流程中途打断-恢复（本脚本验 API 侧；浏览器侧单独验证刷新续接）：
      中断运行 → 模拟重新登录（新 token）→ 从正确状态续接（中断+待确认节点仍在）→ 确认完成
      负断言：无半写（节点状态一致）、无悬挂确认（run 终态后 confirm→409；reject 留操作者+原因）。

实现口径：
  - ① 库存查询=低危 webhook Action（wh mock，输出 stock=0）+ branch 判 stock==0 → 缺货分支；
  - ② 缺货创建采购单=高危 Action（人工确认，signer 留痕）；
  - ③ 实例更新=低危 Action 自动回写仓库系统（库存实例更新，零人工）；
  - ④ 通知 CRM=中危 Action（LLM 不可用→undecidable→升级人工，确认后才回写）；
  - 平台侧实例更新通道（PATCH /api/ingest/instances/{id}）已由 M2/HF-01 验收覆盖，不重复。

运行：cd d:\\self_coding\\ontology_platform && python scripts/verify_m3g_exit.py
L5 回归另跑：cd backend && python -m pytest tests/ -q
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- 环境变量（在导入 backend 之前设置，同 M3-F 模式）----
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.join(PROJECT_ROOT, "backend")
sys.path.insert(0, BACKEND_ROOT)

import config  # noqa: E402

_TEMP_DIR = tempfile.mkdtemp(prefix="m3g_")
DATA_DIR = os.path.join(_TEMP_DIR, "data")
os.environ.update({
    "DATA_DIR": DATA_DIR,
    "ONTOLOGY_WRITEBACK_ENABLED": "true",
    "ONTOLOGY_WRITEBACK_CONFIRM_TTL_SECONDS": "3600",
    "ONTOLOGY_ORCH_CONFIRM_TTL_SECONDS": "3600",
    "ONTOLOGY_ACTION_MAX_RETRIES": "1",
    "ONTOLOGY_ACTION_BREAKER_FAILURE_THRESHOLD": "50",
    "ONTOLOGY_ORCH_SCHEDULER_INTERVAL_SECONDS": "0",
    # LLM 强制"不可用"（中风险三条件评估 undecidable → 升级人工；不触达真实端点）
    "LLM_BASE_URL": "http://127.0.0.1:9/v1",
    "LLM_API_KEY": "test-disabled",
    "ONTOLOGY_AUTH_ENABLED": "true",
    "ONTOLOGY_AUTH_SECRET": "m3g-verify-secret",
    "ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD": "Admin@12345",
    "ONTOLOGY_PBKDF2_ITERATIONS": "10000",
})
config.get_settings.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

# ---- 结果收集 ----
PASS, FAIL = 0, 0
FAILS = []
SEEN = set()


def check(name, ok, detail="", expect=""):
    global PASS, FAIL
    if name in SEEN:
        return
    SEEN.add(name)
    if ok:
        PASS += 1
        print(f"[PASS] {name}  {expect}".rstrip())
    else:
        FAIL += 1
        FAILS.append((name, detail, expect))
        print(f"[FAIL] {name}  期望: {expect}  实际: {detail[:300]}")


# ---- mock 外部系统（wh=仓库 / pur=采购 / crm=CRM）----
class _Mock:
    def __init__(self, name, body):
        self.name = name
        self.body = body
        self.calls = []
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}"

    def call_count(self):
        with self.lock:
            return len(self.calls)

    def _make_handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                with outer.lock:
                    outer.calls.append({"path": self.path, "body": body,
                                        "idem": self.headers.get("Idempotency-Key")})
                payload = json.dumps(outer.body, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return H


def seed_actions(store, wh, pur, crm, pid):
    """白盒种子 4 个 Action（覆盖 低/中/高 三档风险；记录结构同 M3-F 路由同构写入）。"""
    from datetime import datetime, timezone
    items = [
        ("act_query_stock", "查询库存", "low", wh, "idem-qstock::{pn}", "查询备件库存"),
        ("act_stockout", "创建采购单", "high", pur, "idem-po::{pn}", "缺货时创建采购单"),
        ("act_updstock", "更新库存实例", "low", wh, "idem-stock::{pn}", "更换后更新库存实例"),
        ("act_notify_crm", "通知CRM", "medium", crm, "idem-crm::{pn}", "通知CRM备件更换"),
    ]
    for aid, name, risk, mock, idem, desc in items:
        now = datetime.now(timezone.utc).isoformat()
        store.insert({
            "action_id": aid, "id": aid, "project_id": pid,
            "name": name, "description": desc, "category": "验证",
            "target_connection": f"ext-{mock.name}",
            "invoke": {"kind": "webhook", "url": mock.url, "method": "POST"},
            "input_map": {}, "idempotency_key": idem,
            "confirmation": "human_required", "risk_level": risk,
            "status": "active", "dry_run_supported": True, "side_effect": True,
            "created_at": now, "updated_at": now,
        })


def main():
    from core.storage import JsonStore
    import main as backend_main

    wh = _Mock("wh", {"stock": 0, "pn": "PN-5001", "op": "ok"})
    pur = _Mock("pur", {"po_id": "PO-8801", "status": "created"})
    crm = _Mock("crm", {"notified": True, "channel": "crm"})

    client = TestClient(backend_main.app)
    with client:
        r = client.post("/api/auth/login?mode=bearer",
                        json={"username": "admin", "password": "Admin@12345"})
        token = (r.json().get("data") or {}).get("token")
        check("env.admin.login", bool(token), f"body={r.text[:200]}", "admin 登录成功")
        r = client.post("/api/projects", headers={"Authorization": f"Bearer {token}"},
                        json={"name": "M3G 验收项目"})
        pid = (r.json().get("data") or {}).get("project_id") or (r.json().get("data") or {}).get("id")
        check("env.project", bool(pid), f"body={r.text[:200]}", "项目创建成功")
        seed_actions(JsonStore("actions", project_id=pid), wh, pur, crm, pid)

        def req(method, path, body=None, params=None, tok=token):
            h = {"Authorization": f"Bearer {tok}"}
            p = dict(params or {})
            p.setdefault("project_id", pid)
            return client.request(method, path, json=body, params=p, headers=h)

        def data_of(r):
            return r.json().get("data") or {}

        def wf_id_of(r):
            d = data_of(r) or {}
            return d.get("id") or d.get("workflow_id") or ""

        def mk_wf(spec):
            return req("POST", "/api/orchestration/workflows", spec)

        def publish(wf_id):
            return req("POST", f"/api/orchestration/workflows/{wf_id}/publish")

        def get_run(run_id):
            return data_of(req("GET", f"/api/orchestration/runs/{run_id}"))

        def run_wf(wf_id, payload=None):
            r = req("POST", f"/api/orchestration/workflows/{wf_id}/run", {"payload": payload or {}})
            return r, data_of(r)

        def confirm(run_id, node_id, reason="ok", tok=token):
            return req("POST", f"/api/orchestration/runs/{run_id}/confirm",
                       {"node_id": node_id, "reason": reason}, tok=tok)

        def audit(action, page_size=200):
            return data_of(req("GET", "/api/audit",
                               params={"action": action, "page_size": page_size}))

        # ===================== Phase A：TCE2E-03 备件更换完整回写链路 =====================
        # ① 库存查询(低危自动) → branch(stock==0 缺货) → ② 创建采购单(高危 confirm)
        # → 人工签核(confirm 控制节点) → ③ 更新库存实例(低危自动) → ④ 通知 CRM(中危 AI 升级人工)
        PN = "PN-5001"
        spec = {
            "name": "备件更换回写链", "trigger": {"type": "manual"},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "a_query", "kind": "action", "label": "① 库存查询",
                 "action_id": "act_query_stock",
                 "params": {"pn": {"value_type": "literal", "value": PN},
                            "msg": {"value_type": "literal", "value": "query"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "br", "kind": "branch", "label": "缺货判断",
                 "branches": {
                     "short": {"label": "缺货", "condition": {"logic": "AND",
                                  "rules": [{"field": "node:a_query.body.stock", "op": "eq", "value": 0}]}},
                     "ok": {"is_default": True, "label": "充足"}}},
                {"id": "a_po", "kind": "action", "label": "② 创建采购单",
                 "action_id": "act_stockout",
                 "params": {"pn": {"value_type": "field_ref", "value": "node:a_query.body.pn"},
                            "msg": {"value_type": "literal", "value": "stockout→PO"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "c_sign", "kind": "confirm", "label": "采购签核", "wait_timeout": 600},
                {"id": "a_stock", "kind": "action", "label": "③ 更新库存实例",
                 "action_id": "act_updstock",
                 "params": {"pn": {"value_type": "field_ref", "value": "node:a_query.body.pn"},
                            "msg": {"value_type": "literal", "value": "replace-1"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "a_crm", "kind": "action", "label": "④ 通知CRM",
                 "action_id": "act_notify_crm",
                 "params": {"pn": {"value_type": "field_ref", "value": "node:a_query.body.pn"},
                            "msg": {"value_type": "literal", "value": "replaced"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [
                {"id": "e1", "from": "start", "to": "a_query"},
                {"id": "e2", "from": "a_query", "to": "br"},
                {"id": "e3", "from": "br", "to": "a_po", "from_port": "short"},
                {"id": "e4", "from": "br", "to": "end", "from_port": "ok"},
                {"id": "e5", "from": "a_po", "to": "c_sign"},
                {"id": "e6", "from": "c_sign", "to": "a_stock"},
                {"id": "e7", "from": "a_stock", "to": "a_crm"},
                {"id": "e8", "from": "a_crm", "to": "end"},
            ],
        }
        r = mk_wf(spec)
        wf_id = wf_id_of(r)
        check("A.spec.create", r.status_code in (200, 201),
              f"code={r.status_code} body={r.text[:200]}", "组合链工作流创建（4 步 + 分支 + 签核）")
        check("A.publish", publish(wf_id).status_code == 200, "", "工作流发布")

        wh0, pur0, crm0 = wh.call_count(), pur.call_count(), crm.call_count()
        r, run = run_wf(wf_id)
        run_id = run.get("run_id") or run.get("id")
        if os.environ.get("M3G_DUMP"):
            print("RUN_DUMP:", json.dumps(run, ensure_ascii=False)[:4000], "\n")
        ns = run.get("node_states", {})
        check("A.step1.query.ok", run.get("status") == "awaiting_confirmation"
              and ns.get("a_query", {}).get("status") == "success"
              and wh.call_count() == wh0 + 1,
              f"status={run.get('status')} a_query={ns.get('a_query', {}).get('status')} "
              f"wh_delta={wh.call_count() - wh0}",
              "① 库存查询：低危自动回写（wh +1），无人工介入")
        check("A.step2.branch.short", run.get("pending_node") == "a_po"
              and pur.call_count() == pur0,
              f"pending={run.get('pending_node')}",
              "branch 判 stock==0 走缺货分支，停在高危采购单节点（未外呼）")
        pa = (ns.get("a_po") or {}).get("pending_action") or {}
        check("A.risk.high", pa.get("risk_level") == "high" and pa.get("confirmation_token"),
              f"pending_action={str(pa)[:150]}", "高危分级准确（risk_level=high + 确认令牌）")
        # 负断言：a_po 待确认期间确认别的节点 → 409
        r = confirm(run_id, "c_sign", "not-pending")
        check("A.neg.not_pending", r.status_code == 409, f"code={r.status_code} body={r.text[:150]}",
              "非待确认节点确认 → 409（不越序）")
        # ② 高危人工确认（签核）
        r = confirm(run_id, "a_po", "采购审批通过")
        run = data_of(r)
        check("A.step2.po.confirmed", run.get("status") == "awaiting_confirmation"
              and run.get("pending_node") == "c_sign",
              f"status={run.get('status')} pending={run.get('pending_node')}",
              "② 采购单：高危人工签核确认后回写（pur +1），继续到签核控制节点")
        check("A.step2.po.external", pur.call_count() == pur0 + 1,
              f"pur_delta={pur.call_count() - pur0}", "采购系统收到创建采购单外呼")
        # 人工签核控制节点
        r = confirm(run_id, "c_sign", "签核")
        run = data_of(r)
        ns = run.get("node_states", {})
        check("A.step3.stock.paused_crm", run.get("status") == "awaiting_confirmation"
              and run.get("pending_node") == "a_crm"
              and ns.get("a_stock", {}).get("status") == "success"
              and wh.call_count() == wh0 + 2,
              f"pending={run.get('pending_node')} a_stock={ns.get('a_stock', {}).get('status')} "
              f"wh_delta={wh.call_count() - wh0}",
              "③ 实例更新：低危自动回写（wh +1，零人工），继续到 CRM 通知")
        pa = (ns.get("a_crm") or {}).get("pending_action") or {}
        ev = pa.get("evaluation") or {}
        check("A.risk.medium.escalated", pa.get("risk_level") == "medium"
              and (pa.get("escalated") is True or (ev or {}).get("verdict") == "undecidable"),
              f"pending_action={str(pa)[:200]} ev={str(ev)[:200]}",
              "④ CRM 通知：中危 AI 判定不可用（undecidable）→ 升级人工")
        r = confirm(run_id, "a_crm", "已核实通知内容")
        run = data_of(r)
        ns = run.get("node_states", {})
        check("A.step4.crm.ok", run.get("status") == "succeeded" and crm.call_count() == crm0 + 1
              and all(ns.get(i, {}).get("status") == "success"
                      for i in ("a_query", "a_po", "c_sign", "a_stock", "a_crm")),
              f"status={run.get('status')} crm_delta={crm.call_count() - crm0} "
              f"states={[ns.get(i, {}).get('status') for i in ('a_query', 'a_po', 'c_sign', 'a_stock', 'a_crm')]}",
              "④ CRM 通知确认后回写；整链 4 步全 success（succeeded）")
        # 签核证据链
        wb = audit("action_writeback").get("items", [])
        wb_po = [x for x in wb if x.get("target_id") == "act_stockout"]
        wb_crm = [x for x in wb if x.get("target_id") == "act_notify_crm"]
        wb_low = [x for x in wb if x.get("target_id") in ("act_query_stock", "act_updstock")]
        check("A.chain.wb_count", len(wb_po) == 1 and len(wb_crm) == 1 and len(wb_low) >= 2,
              f"po={len(wb_po)} crm={len(wb_crm)} low={len(wb_low)}",
              "action_writeback 审计 4 条（4 次回写各 1 条）")
        check("A.chain.signer.high", wb_po and wb_po[0].get("detail", {}).get("signer") == "admin"
              and (wb_po[0].get("detail", {}).get("signer_at") or ""),
              f"detail={str(wb_po[:1])[:200]}", "高危回写审计含签核人身份+时间（signer=admin, signer_at）")
        check("A.chain.signer.medium", wb_crm and wb_crm[0].get("detail", {}).get("signer") == "admin",
              f"detail={str(wb_crm[:1])[:200]}", "中危（升级人工后）回写审计含签核人")
        check("A.chain.auto.low", wb_low and all(x.get("detail", {}).get("auto") is True
              and not x.get("detail", {}).get("signer") for x in wb_low),
              f"detail={str(wb_low[:2])[:200]}", "低危回写审计 auto=true 且无 signer（自动，零人工）")
        cf = audit("workflow_run_confirmed").get("items", [])
        cf_nodes = [x.get("after", {}).get("node_id") for x in cf]
        check("A.chain.confirmed.audit", len(cf) == 3
              and all(x.get("actor") == "admin" and (x.get("after", {}).get("reason")) for x in cf)
              and set(cf_nodes) == {"a_po", "c_sign", "a_crm"},
              f"n={len(cf)} nodes={cf_nodes}",
              "workflow_run_confirmed ×3（采购单/签核/CRM），操作者+原因留痕")
        st = audit("workflow_run_started").get("items", [])
        fin = audit("workflow_run_finished").get("items", [])
        check("A.chain.run_lifecycle", st and fin and any(
            x.get("target_id") == run_id for x in fin) and any(
            x.get("target_id") == run_id for x in st),
              f"started={len(st)} finished={len(fin)}", "运行生命周期审计 started/finished 齐全")

        # ===================== Phase B：TCE2E-07 事件触发回写编排闭环（API 侧） =====================
        spec2 = {
            "name": "事件-库存告警", "trigger": {"type": "event", "event_topic": "stock.low",
                                                 "required_ctx": ["pn"]},
            "nodes": [
                {"id": "start", "kind": "start"},
                {"id": "a_po2", "kind": "action", "label": "创建采购单",
                 "action_id": "act_stockout",
                 "params": {"pn": {"value_type": "trigger_ctx", "value": "trigger.pn"},
                            "msg": {"value_type": "literal", "value": "event-po"}},
                 "failure_policy": "retry", "retry_max": 1},
                {"id": "end", "kind": "end"},
            ],
            "edges": [{"id": "e1", "from": "start", "to": "a_po2"},
                      {"id": "e2", "from": "a_po2", "to": "end"}],
        }
        r = mk_wf(spec2)
        wf2 = wf_id_of(r)
        check("B.create", r.status_code in (200, 201), f"code={r.status_code}", "事件触发工作流创建")
        # ② 发布前干跑预览（零外呼）
        pur_b0 = pur.call_count()
        r = req("POST", f"/api/orchestration/workflows/{wf2}/dry-run", {"payload": {"pn": "PN-7001"}})
        dry = data_of(r)
        if os.environ.get("M3G_DUMP"):
            print("DRY_DUMP:", json.dumps(dry, ensure_ascii=False)[:2000], "\n")
        preview = dry.get("preview") or dry
        p_nodes = {n.get("node_id") or n.get("id"): n for n in (preview.get("nodes") or [])}
        check("B.dryrun", r.status_code == 200 and dry.get("dry_run") is True
              and p_nodes.get("a_po2", {}).get("status") == "awaiting_confirmation"
              and p_nodes.get("a_po2", {}).get("risk_level") == "high"
              and pur.call_count() == pur_b0,
              f"code={r.status_code} preview={str(preview)[:200]} pur_delta={pur.call_count() - pur_b0}",
              "② 干跑预览：高危节点标 awaiting_confirmation，零外呼")
        # ③ 发布
        check("B.publish", publish(wf2).status_code == 200, "", "③ 发布")
        # ④ 事件触发
        r = req("POST", "/api/orchestration/events",
                {"topic": "stock.low", "payload": {"pn": "PN-7001"}})
        res = data_of(r)
        rr = (res.get("results") or [{}])[0]
        run_b = get_run(rr.get("run_id") or "")
        check("B.event.start", res.get("matched_workflows") == 1 and rr.get("started") is True
              and run_b.get("status") == "awaiting_confirmation"
              and run_b.get("trigger", {}).get("source") == "event",
              f"matched={res.get('matched_workflows')} rr={str(rr)[:150]} "
              f"status={run_b.get('status')} trigger={run_b.get('trigger')}",
              "④ 事件分发→自动启动（trigger source=event），停在高危确认点")
        # 负断言：缺 required_ctx → 拒绝启动且不半启动
        pur_b1 = pur.call_count()
        r = req("POST", "/api/orchestration/events", {"topic": "stock.low", "payload": {}})
        res = data_of(r)
        rr = (res.get("results") or [{}])[0]
        check("B.neg.ctx_missing", rr.get("started") is False
              and rr.get("error_code") == "TRIGGER_CTX_MISSING" and pur.call_count() == pur_b1,
              f"rr={str(rr)[:200]} pur_delta={pur.call_count() - pur_b1}",
              "缺 required_ctx → TRIGGER_CTX_MISSING 拒绝（不半启动、零外呼）")
        # ⑤ 节点状态/审计
        run_b = get_run(run_b.get("run_id") or run_b.get("id"))
        r = confirm(run_b.get("run_id") or run_b.get("id"), "a_po2", "事件链采购审批")
        run_b = data_of(r)
        check("B.run.succeeded", run_b.get("status") == "succeeded"
              and pur.call_count() == pur_b1 + 1,
              f"status={run_b.get('status')} pur_delta={pur.call_count() - pur_b1}",
              "⑤ 确认后执行完成（pur +1），节点状态 success")
        ev_a = audit("workflow_event_dispatched").get("items", [])
        check("B.audit.event", ev_a and any("stock.low" in str(x) for x in ev_a),
              f"n={len(ev_a)} first={str(ev_a[:1])[:200]}", "⑤ 审计含 workflow_event_dispatched（事件留痕）")
        st_items = audit("workflow_run_started").get("items", [])
        check("B.audit.run_started_event", any(
            (x.get("after") or {}).get("source") == "event"
            and (x.get("after") or {}).get("topic") == "stock.low"
            for x in st_items),
            f"started={str(st_items[:2])[:300]}",
            "⑤ run 启动审计可区分事件触发来源（after.source=event + topic）")

        # ===================== Phase C：TCEE-04 流程中途打断-重新登录-续接 =====================
        # ① 再派事件 → 新 run 中断于高危确认点
        r = req("POST", "/api/orchestration/events",
                {"topic": "stock.low", "payload": {"pn": "PN-7002"}})
        rr = ((data_of(r)).get("results") or [{}])[0]
        rid_c = rr.get("run_id")
        run_c = get_run(rid_c or "")
        rid_c = run_c.get("run_id") or rid_c
        check("C.interrupted", run_c.get("status") == "awaiting_confirmation"
              and run_c.get("pending_node") == "a_po2",
              f"status={run_c.get('status')} pending={run_c.get('pending_node')}",
              "① 运行中断于确认点（awaiting_confirmation + 待确认节点）")
        # ② 模拟重新登录（新 token）→ 从正确状态续接
        token2 = (client.post("/api/auth/login?mode=bearer",
                              json={"username": "admin", "password": "Admin@12345"}).json()
                  .get("data") or {}).get("token")
        check("C.relogin", bool(token2) and token2 != token, "", "② 重新登录获取新 token")
        run_c2 = data_of(client.request(
            "GET", f"/api/orchestration/runs/{rid_c}",
            params={"project_id": pid},
            headers={"Authorization": f"Bearer {token2}"}))
        n2 = run_c2.get("node_states", {})
        check("C.resume.state", run_c2.get("status") == "awaiting_confirmation"
              and run_c2.get("pending_node") == "a_po2"
              and n2.get("a_po2", {}).get("status") == "awaiting_confirmation"
              and n2.get("end", {}).get("status") in ("not_reached", "not_executed"),
              f"status={run_c2.get('status')} pending={run_c2.get('pending_node')} "
              f"a_po2={n2.get('a_po2', {}).get('status')} end={n2.get('end', {}).get('status')}",
              "② 重新登录后状态续接正确（中断态+待确认节点，无半写）")
        # ③ 新 token 确认 → 完成（此前未外呼，确认后正常外呼 1 次）
        pur_c0 = pur.call_count()
        r = confirm(rid_c, "a_po2", "重新登录后确认", tok=token2)
        run_c3 = data_of(r)
        check("C.resume.done", run_c3.get("status") == "succeeded"
              and pur.call_count() == pur_c0 + 1,
              f"status={run_c3.get('status')} pur_delta={pur.call_count() - pur_c0}",
              "③ 新会话确认后运行完成，回写正常执行 1 次（不重复）")
        # 负断言：终态后确认 → 409（无悬挂确认）
        r = confirm(rid_c, "a_po2", "again", tok=token2)
        check("C.neg.no_hang", r.status_code == 409,
              f"code={r.status_code} body={r.text[:150]}", "终态后再次确认 → 409（无悬挂确认）")
        # 负断言：reject 留操作者+原因且不产生外呼
        r = req("POST", "/api/orchestration/events",
                {"topic": "stock.low", "payload": {"pn": "PN-7003"}})
        rid_r = ((data_of(r)).get("results") or [{}])[0].get("run_id")
        run_r = get_run(rid_r or "")
        rid_r = run_r.get("run_id") or rid_r
        pur_r0 = pur.call_count()
        r = req("POST", f"/api/orchestration/runs/{rid_r}/reject",
                {"node_id": "a_po2", "reason": "库存数据过期"})
        run_rj = data_of(r)
        check("C.reject", run_rj.get("status") == "cancelled" and pur.call_count() == pur_r0,
              f"status={run_rj.get('status')} pur_delta={pur.call_count() - pur_r0}",
              "拒绝待确认节点 → cancelled 且零外呼")
        rj = audit("workflow_run_rejected").get("items", [])
        check("C.reject.audit", rj and rj[-1].get("actor") == "admin"
              and "库存数据过期" in str(rj[-1].get("after")),
              f"last={str(rj[-1:])[:250]}", "reject 审计留操作者+原因")
    return FAIL


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        traceback.print_exc()
        rc = 1
    print()
    print(f"==== M3-G 验收结果: PASS={PASS} FAIL={FAIL} ====")
    if FAILS:
        for n, d, e in FAILS:
            print(f"  [FAIL] {n}\n    期望: {e}\n    实际: {d[:300]}")
    if rc or FAIL:
        print(f"(失败现场保留: {_TEMP_DIR})")
    else:
        shutil.rmtree(_TEMP_DIR, ignore_errors=True)
    sys.exit(1 if (rc or FAIL) else 0)
