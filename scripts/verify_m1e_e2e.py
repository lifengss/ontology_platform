# -*- coding: utf-8 -*-
"""M1-E 里程碑退出 / TCE2E-01（PN 码更换备件全链路）验收脚本。

用法：
    python scripts/verify_m1e_e2e.py

全链（按 docs/V2-功能需求描述.md 附录 E2E 流，组合 V2-01＋V2-04＋V2-15）：
  用户自然语言提问 → [外部 AI 平台经 MCP] term_lookup 三系统合并
  → [外部 AI 平台经 MCP] capability_call 以求「更换提前期」（库存判断＋采购提前期公式）
  → [外部 AI 平台] 综合回答（每个数字带来源标记/术语解析路径/能力版本/基线记录），链路完整。

INV-3：平台只做工具提供方，不新增 answer/ask 编排端点、运行时不做工具选择或分支决策；
本脚本本身扮演「外部 Agent 客户端」，负责解析问题、组装 inputs 与生成带溯源回答。

同时覆盖：
  EP-E2E-01 / TCEE-01 水库系统不可用 → 答案显式说明缺失系统与数据缺口，不给出错误提前期
  V2-15 开放接口：REST 与 MCP 同口径；字典审计与能力版本历史可供事后核查
  M1 前置：字典就绪、6 个成本公式已发布、3 mock 在线
  INV-3 负向：/api/ask|qa|chat|answer 不存在，MCP 工具清单无回答类工具

数据在临时 DATA_DIR 隔离；3 个 mock 与 uvicorn 在脚本内启停。
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TMP_DATA = tempfile.mkdtemp(prefix="m1e_data_")
os.environ["DATA_DIR"] = _TMP_DATA
os.environ["MCP_MAX_RETRIES"] = "0"
os.environ["MCP_TIMEOUT_SECONDS"] = "1"   # 外部系统故障快速失败（零重试）
os.environ["M1E_MOCK_TOKEN"] = "tok-full"  # mock 默认全量 token（build_default_tokens）
# V2-05：M1 全链回归基线不面向鉴权；M2-A 验收脚本在启用态单独验证 TP-05/EP-05
os.environ.setdefault("ONTOLOGY_AUTH_ENABLED", "false")
sys.path.insert(0, os.path.join(REPO, "backend"))

import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import dictionary  # noqa: E402
from capabilities import CapabilityService  # noqa: E402
from core.instance_store import get_instance_store  # noqa: E402
from core.storage import JsonStore  # noqa: E402
from main import app  # noqa: E402
from mocks import build_three_system_seed, make_all  # noqa: E402

PID = "m1e-verify"
ONT = "ont-m1e-spare"
PART_OBJ = "obj_spare_part"
BASE_OBJ = "obj_spare_lead_time_baseline"
PART_ID = "part-pn-e2e"
LEAD_CAP = "cap_spare_lead_time"
ADMIN = {"X-Mock-Admin": "mock-admin-2026"}
P = {"project_id": PID}

CRM_FM = {"PN": "pn", "product_name": "name", "customer_name": "customer",
          "region": "region", "contract_amount": "amount", "contract_status": "status"}
PLM_FM = {"PN码": "pn", "产品名称": "name", "product_model": "model",
          "version": "version", "life_cycle": "lifecycle"}
# 字典把水库系统异构字段映射为业务属性：capacity_mw(数值) → stock_qty（备件可用量）
RSR_FM = {"PN_Num": "pn", "PN名称": "name", "capacity_mw": "stock_qty",
          "reservoir_level": "stock_level", "design_year": "design_year"}
BINDINGS = {"conn-crm": "PN", "conn-plm": "PN码", "conn-rsr": "PN_Num"}

LEAD_EXPR = """stock = this.stock_qty
need = this.required_qty
incoming = if_null(this.in_transit_qty, 0)
if(stock == null, null,
  if(stock >= need,
    lookup('obj_spare_lead_time_baseline', {'attr_pn': this.pn}, 'attr_instock_days'),
    if(stock + incoming >= need,
      lookup('obj_spare_lead_time_baseline', {'attr_pn': this.pn}, 'attr_transit_days'),
      lookup('obj_spare_lead_time_baseline', {'attr_pn': this.pn}, 'attr_purchase_lead_days')
        + lookup('obj_spare_lead_time_baseline', {'attr_pn': this.pn}, 'attr_transit_days'))))"""

PASS = FAIL = 0
FAILS: list[str] = []
SKIPS: list[str] = []
CTX: dict = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def admin_cfg(port: int, **kv) -> None:
    requests.post(f"http://127.0.0.1:{port}/_admin/config", json=kv, headers=ADMIN, timeout=5)


def conn_rec(cid: str, name: str, port: int, fmap: dict) -> dict:
    ts = now_iso()
    return {
        "id": cid, "connection_id": cid, "display_name": name,
        "transport": "http", "endpoint": f"http://127.0.0.1:{port}/read",
        "auth": {"type": "bearer_token", "bearer_token_ref": "${M1E_MOCK_TOKEN}"},
        "read_tool": "read", "fetch_mode": "paged", "fetch_limit": 0,
        "field_map_source": "explicit",
        "maps_to_ontology": {"ontology_id": ONT, "object_type": PART_OBJ,
                             "field_map": dict(fmap)},
        "created_at": ts, "updated_at": ts,
    }


def load_m1b_exprs() -> tuple[dict, dict]:
    """从 M1-B 脚本源码只读提取 6 个成本公式（避免 import 覆盖 DATA_DIR，保持单一事实来源）。"""
    path = os.path.join(REPO, "scripts", "verify_m1b_capabilities.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    consts: dict[str, dict] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) in ("EXPRS", "OUTPUT_ATTRS"):
                    consts[t.id] = ast.literal_eval(node.value)
    return consts["EXPRS"], consts["OUTPUT_ATTRS"]


def setup() -> None:
    """前置：3 mock + 字典 + 2297 无关的本地基线/备件实例 + 6 成本公式 + 提前期能力 + MCP 发布。"""
    section("前置：字典就绪 / 3 mock 在线 / 6 成本公式已发布 / 提前期能力就绪")
    seed = build_three_system_seed(42)
    mocks = make_all(seed=42)
    crm, plm, rsr = mocks["crm"], mocks["plm"], mocks["reservoir"]
    for m in (crm, plm, rsr):
        m.start()
    time.sleep(0.3)
    conns = JsonStore("mcp_connections", PID)
    conns.insert(conn_rec("conn-crm", "CRM系统", crm.port, CRM_FM))
    conns.insert(conn_rec("conn-plm", "PLM系统", plm.port, PLM_FM))
    conns.insert(conn_rec("conn-rsr", "水库系统", rsr.port, RSR_FM))
    term = dictionary.create_term(PID, {
        "canonical": "PN码", "object_type": PART_OBJ, "property_id": "pn",
        "synonyms": ["PN号", "PN"], "system_bindings": dict(BINDINGS),
    })
    check("TC-01-01 前置：PN码词条绑定三系统字段", bool(term.get("id"))
          and term.get("system_bindings") == BINDINGS)

    rsr_rows = {r["PN_Num"]: r for r in seed.reservoir_records}
    conflicted = set(seed.conflict_keys)
    pn = next(r["PN"] for r in seed.crm_records
              if r["PN"] in rsr_rows and r["PN"] not in conflicted
              and rsr_rows[r["PN"]]["capacity_mw"] >= 1.0)
    stock = float(rsr_rows[pn]["capacity_mw"])
    CTX.update({"mocks": (crm, plm, rsr), "seed": seed, "pn": pn,
                "stock": stock, "term": term})

    store = get_instance_store(project_id=PID)
    store.insert({
        "id": PART_ID, "project_id": PID, "ontology_id": ONT, "object_type": PART_OBJ,
        "instance": {"pn": pn, "attr_pn": pn, "part_name": "更换备件",
                     "required_qty": 1, "stock_qty": 0, "in_transit_qty": 0},
        "source": {"kind": "upload", "raw_ref": "verify_m1e:spare_part"},
    })
    store.insert({
        "id": "lead-base-pn", "project_id": PID, "ontology_id": ONT,
        "object_type": BASE_OBJ,
        "instance": {"attr_baseline_code": f"LEAD-{pn}", "attr_pn": pn,
                     "attr_purchase_lead_days": 5, "attr_transit_days": 2,
                     "attr_instock_days": 1},
        "source": {"kind": "upload", "raw_ref": "verify_m1e:lead_baseline"},
    })
    check("本地上传型基线/备件实例落 InstanceStore",
          len(store.find(object_type=BASE_OBJ, limit=None)) == 1
          and store.get(PART_ID) is not None)

    svc = CapabilityService(PID)
    exprs, outs = load_m1b_exprs()
    cost_caps = {}
    for name, expr in exprs.items():
        cost_caps[name] = svc.create_capability({
            "name": name, "object_type": "obj_maintenance_product",
            "expression": expr, "output_attribute": outs[name],
        })
    for name in exprs:  # 依赖顺序 D → E/K/N → O → S
        svc.publish_capability(cost_caps[name]["id"])
    active = {r["name"] for r in svc.list_capabilities(status="active")}
    check("前置：6 个成本公式均已发布(active)", set(exprs) <= active, str(sorted(active)))

    lead = svc.create_capability({
        "name": LEAD_CAP, "display_name": "备件更换提前期",
        "description": "库存判断＋采购提前期公式（M1-E / TCE2E-01）",
        "object_type": PART_OBJ, "expression": LEAD_EXPR,
        "output_attribute": "attr_lead_time_days",
    })
    v = svc.validate_capability(lead["id"], LEAD_EXPR)
    check("提前期公式定义期校验通过", v.get("valid") is True, str(v)[:250])
    svc.publish_capability(lead["id"])
    CTX.update({"svc": svc, "lead": lead, "exprs": exprs})

    # 最小本体 + 项目 MCP Server 发布 + token（供真实 SSE 客户端）
    client = TestClient(app)
    obj_payload = lambda oid, name, attr: {
        "id": oid, "name": name, "business_definition": f"{name}对象用于备件提前期全链验收",
        "attributes": [{"id": attr, "name": attr, "data_type": "string",
                        "required": True, "constraints": {}}],
        "relations": [],
    }
    r = client.post("/api/ontologies", params=P, json={
        "name": "M1E验收-备件域", "modeling_type": "manual",
        "objects": [obj_payload(PART_OBJ, "备件", "pn"),
                    obj_payload(BASE_OBJ, "提前期基线", "attr_baseline_code")],
    })
    check("最小本体创建（供 MCP 发布）", r.status_code == 201, r.text[:250])
    oid = r.json()["data"]["id"]
    rp = client.post("/api/mcp/server/publish", params={**P, "ontology_id": oid})
    check("V2-15 前置：项目 MCP Server 已发布",
          rp.status_code == 200 and rp.json()["data"].get("published") is True, rp.text[:250])
    CTX["token"] = client.post("/api/mcp/server/token", params=P).json()["data"]["token"]
    CTX["client"] = client


def text_of(res) -> str:
    return res.content[0].text if res.content else ""


def compose_answer(question: str, lookup: dict, calc: dict, inputs: dict) -> dict:
    """外部 AI 平台（Agent）侧：用平台工具返回结构组装带溯源回答与 citations。"""
    resolution = lookup.get("resolution") or {}
    merged = (lookup.get("merged") or [{}])[0]
    stock_cell = (merged.get("fields") or {}).get("stock_qty") or {}
    trace = calc.get("trace") or {}
    hits = trace.get("lookups") or []
    days = calc.get("value")
    citations = [
        {"claim": "术语解析路径", "requirement": "V2-01", "evidence": {
            "input_term": lookup.get("input_term"), "canonical": resolution.get("canonical"),
            "term_id": resolution.get("term_id"), "match": resolution.get("match"),
            "confidence": resolution.get("confidence")}},
        {"claim": "三系统合并与来源标记", "requirement": "V2-01", "evidence": {
            "present_in": merged.get("present_in"), "systems": lookup.get("systems"),
            "stock_sources": stock_cell.get("sources"), "uncovered": lookup.get("uncovered")}},
        {"claim": "提前期计算与能力版本", "requirement": "V2-04", "evidence": {
            "capability_version": calc.get("capability_version"),
            "instance_id": calc.get("instance_id"), "value": days,
            "input_overrides": calc.get("input_overrides"),
            "trace_inputs": trace.get("inputs"), "trace_lookups": hits}},
        {"claim": "基线记录原值", "requirement": "V2-04", "evidence": [
            {"matched_record_id": h.get("matched_record_id"),
             "matched_code": h.get("matched_code"), "value": h.get("value")}
            for h in hits]},
        {"claim": "经开放接口取得工具结果", "requirement": "V2-15",
         "evidence": {"transport": "MCP SSE", "tools": ["term_lookup", "capability_call"]}},
    ]
    gaps = calc.get("data_gaps") or []
    ps = lookup.get("per_system") or {}
    failed = [f"{sid}({v.get('status')})" for sid, v in ps.items() if v.get("status") != "ok"]
    if days is None or gaps or failed:
        text = (f"针对「{question}」：本次查询存在数据缺口，"
                f"缺失/异常系统：{', '.join(failed) or '未报告'}；"
                f"合并结果缺少关键输入：{', '.join(gaps) or '库存量'}。"
                "因此暂无法给出更换提前期，不猜测、不用其它数值替代；"
                f"已保留术语解析路径（{resolution.get('canonical')}）与其余系统结果供复核。")
    else:
        text = (f"针对「{question}」：字典把输入术语「{lookup.get('input_term')}」解析为规范术语"
                f"「{resolution.get('canonical')}」（{resolution.get('match')} 匹配，"
                f"置信度 {resolution.get('confidence')}），在 {'、'.join(merged.get('present_in') or [])} "
                f"三系统按各自字段名合并检索；备件 {inputs.get('pn')} 可用量 {inputs.get('stock_qty')} "
                f"（来源：{stock_cell.get('sources')}）。"
                f"经能力 {calc.get('capability_name')} v{calc.get('capability_version')} 计算，"
                f"更换提前期约 {days} 天（输入实例 {calc.get('instance_id')}）；"
                f"该数值取自基线记录 {hits[0].get('matched_code') if hits else '—'}"
                f"（{hits[0].get('matched_record_id') if hits else '—'}）的字段原值 "
                f"{hits[0].get('value') if hits else '—'}。")
    return {"question": question, "answer": text, "citations": citations,
            "value": days, "degraded": days is None or bool(gaps or failed)}


def run_agent_chain(base: str) -> dict:
    """真实 MCP SSE 客户端（外部 Agent）依次调用平台工具，含水库系统降级演练。"""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    pn, stock, token = CTX["pn"], CTX["stock"], CTX["token"]

    async def _call(s, tool: str, args: dict) -> dict:
        res = await s.call_tool(tool, args)
        raw = text_of(res)
        if res.is_error:
            return {"is_error": True, "raw": raw}
        return {"is_error": False, **json.loads(raw)}

    async def _run() -> dict:
        async with sse_client(f"{base}/mcp/{PID}",
                              headers={"Authorization": f"Bearer {token}"}) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                tools = sorted(t.name for t in (await s.list_tools()).tools)
                out: dict = {"tools": tools}
                out["listing"] = await _call(s, "capability_call", {"capability": ""})

                # ① 自然语言提问 → term_lookup 三系统合并
                q1 = f"给 客户A 更换一个 PN 码为 {pn} 的备件，需要多久？"
                out["lookup"] = await _call(s, "term_lookup", {"term": "PN码", "value": pn})
                out["question"] = q1
                fields = ((out["lookup"].get("merged") or [{}])[0].get("fields") or {})
                stock_value = (fields.get("stock_qty") or {}).get("value")
                level_value = (fields.get("stock_level") or {}).get("value")
                base_inputs = {"pn": pn, "stock_qty": stock_value,
                               "stock_level": level_value, "required_qty": 1,
                               "in_transit_qty": 0}

                # ② 有库存：提前期 = 现货出库天数
                out["calc_instock"] = await _call(s, "capability_call", {
                    "capability": LEAD_CAP, "instance_id": PART_ID,
                    "inputs": base_inputs, "dry_run": True})
                # ③ 缺货：提前期 = 采购 + 运输
                out["calc_outofstock"] = await _call(s, "capability_call", {
                    "capability": LEAD_CAP, "instance_id": PART_ID,
                    "inputs": {**base_inputs, "required_qty": float(stock) + 10.0},
                    "dry_run": True})

                # ④ EP-E2E-01：水库系统不可用（注入 503 立即失败）→ 数据缺口、不给错误提前期
                await asyncio.to_thread(admin_cfg, CTX["mocks"][2].port,
                                        fail_rate=1.0, error_status=503)
                out["degraded_lookup"] = await _call(
                    s, "term_lookup", {"term": "PN号", "value": pn})
                dfields = ((out["degraded_lookup"].get("merged") or [{}])[0].get("fields") or {})
                out["degraded_calc"] = await _call(s, "capability_call", {
                    "capability": LEAD_CAP, "instance_id": PART_ID,
                    "inputs": {"pn": pn, "stock_qty": (dfields.get("stock_qty") or {}).get("value"),
                               "stock_level": None, "required_qty": 1,
                               "in_transit_qty": 0},
                    "dry_run": True})
                await asyncio.to_thread(admin_cfg, CTX["mocks"][2].port,
                                        fail_rate=0.0, error_status=500)
        return out

    return asyncio.run(_run())


def verify_chain(out: dict) -> None:
    section("TCE2E-01 / TP-E2E-01 全链：自然语言 → 字典合并 → 公式提前期 → 带溯源回答")
    lookup = out["lookup"]
    check("① term_lookup 三系统合并成功且术语解析路径完整",
          lookup.get("status") == "ok" and lookup.get("merged_count", 0) >= 1
          and (lookup.get("resolution") or {}).get("canonical") == "PN码"
          and (lookup.get("resolution") or {}).get("match") in ("exact", "semantic")
          and len(lookup.get("systems") or {}) == 3
          and not lookup.get("uncovered"), str(lookup)[:350])
    merged = (lookup.get("merged") or [{}])[0]
    fields = merged.get("fields") or {}
    stock_cell = fields.get("stock_qty") or {}
    check("① 合并结果含来源标记（三系统覆盖 + 水库库存量 sources）",
          len(merged.get("present_in") or []) == 3
          and "水库系统" in (stock_cell.get("sources") or {})
          and stock_cell.get("value") is not None, str(merged)[:350])
    systems_view = lookup.get("systems") or {}
    check("① 三系统各自字段绑定可回溯（system_bindings）",
          all((systems_view.get(sid) or {}).get("covered")
              and (systems_view.get(sid) or {}).get("system_field") == fld
              for sid, fld in BINDINGS.items()), str(systems_view)[:250])

    calc = out["calc_instock"]
    hits = (calc.get("trace") or {}).get("lookups") or []
    check("② 公式求提前期返回四要素溯源（能力版本/输入实例/输入原值/基线命中）",
          calc.get("status") == "success" and calc.get("value") == 1
          and bool(calc.get("capability_version")) and calc.get("instance_id") == PART_ID
          and bool((calc.get("trace") or {}).get("inputs")) and bool(hits)
          and all(h.get("matched_record_id") and h.get("matched_code") and "value" in h
                  for h in hits), str(calc)[:350])
    check("② 联邦读结果作为临时输入：不落库、可回溯（input_overrides/data_gaps）",
          calc.get("inputs_persisted") is False and calc.get("input_source") == "federated_read_override"
          and "stock_qty" in (calc.get("input_overrides") or {})
          and (calc.get("data_gaps") or []) == [], str(calc.get("input_overrides"))[:250])
    check("② dry_run 不写回实例（联邦读内容不落地）",
          calc.get("dry_run") is True
          and "attr_lead_time_days" not in (CTX["svc"]._instances.get(PART_ID).get("instance") or {}))

    calc2 = out["calc_outofstock"]
    check("② 缺货分支按采购＋运输公式（前置库存判断生效）",
          calc2.get("status") == "success" and calc2.get("value") == 7,
          f"value={calc2.get('value')} trace={str((calc2.get('trace') or {}).get('lookups'))[:200]}")

    answer = compose_answer(out["question"], lookup, calc, calc.get("input_overrides") or {})
    reqs = {c["requirement"] for c in answer["citations"]}
    text = answer["answer"]
    check("③ 带溯源回答：覆盖 V2-01＋V2-04＋V2-15 且溯源链完整",
          reqs == {"V2-01", "V2-04", "V2-15"} and len(answer["citations"]) >= 5
          and all(k in text for k in ("PN码", "CRM系统", "水库系统", "1.0.0",
                                      str(calc.get("value")), "LEAD-", PART_ID)),
          text[:350])
    stock_text = str(stock_cell.get("value"))
    check("③ 回答中每个数字可回溯到平台返回结构",
          str(calc.get("value")) in text and stock_text in text
          and any(str(h.get("value")) in text and str(h.get("matched_code")) in text
                  for h in hits),
          f"text={text[:200]} hits={str(hits)[:200]}")
    check("③ 回答由外部 Agent 组装（平台仅返回结构化工具结果）",
          isinstance(text, str) and text.startswith("针对")
          and answer.get("degraded") is False, text[:150])
    CTX["answer"] = answer

    section("EP-E2E-01 / TCEE-01 水库系统不可用：降级回答不给错误提前期")
    dl, dc = out["degraded_lookup"], out["degraded_calc"]
    failed = [sid for sid, v in (dl.get("per_system") or {}).items() if v.get("status") != "ok"]
    dfields = ((dl.get("merged") or [{}])[0].get("fields") or {})
    check("水库系统失败显式标注，其余系统结果仍可查（不整体挂掉）",
          dl.get("status") == "ok" and "conn-rsr" in failed
          and (dl.get("per_system") or {}).get("conn-rsr", {}).get("status") in ("error", "timeout")
          and "stock_qty" not in dfields, str(dl)[:350])
    check("关键输入缺失 → 公式返回 null（不猜测、不给错误天数）",
          dc.get("value") is None and dc.get("status") == "success"
          and "stock_qty" in (dc.get("data_gaps") or []), str(dc)[:300])
    degraded_answer = compose_answer(out["question"], dl, dc, {"pn": CTX["pn"]})
    dtext = degraded_answer["answer"]
    check("降级回答显式说明缺失系统与数据缺口",
          degraded_answer["degraded"] is True
          and "数据缺口" in dtext and "conn-rsr" in dtext and "暂无法给出更换提前期" in dtext
          and "7 天" not in dtext and "1 天" not in dtext, dtext[:300])

    section("INV-3 / V2-15：平台只做工具提供方")
    check("MCP 工具清单无回答类工具（6 工具，平台不代 Agent 决策）",
          len(out["tools"]) == 6 and "capability_call" in out["tools"]
          and not any(t in out["tools"] for t in ("answer", "ask", "chat")), str(out["tools"]))
    check(" capability_call 空能力名返回已发布清单（先枚举再执行）",
          out["listing"].get("published") is True and out["listing"].get("count") >= 7,
          str(out["listing"])[:250])
    for path in ("/api/ask", "/api/qa", "/api/chat", "/api/answer"):
        r = CTX["client"].get(path, params=P)
        if r.status_code != 404:
            break
    check("平台不存在 /api/ask|qa|chat|answer 编排端点", r.status_code == 404, str(r.status_code))


def rest_consistency(base: str) -> None:
    section("V2-15 开放接口：REST 与 MCP 同口径 + 事后血缘/审计可核查")
    c, svc = CTX["client"], CTX["svc"]
    r = c.post("/api/dictionary/term-lookup", params=P,
               json={"term": "PN码", "value": CTX["pn"]})
    d = r.json().get("data") or {}
    mcp_lookup = CTX.get("chain", {}).get("lookup") or {}
    check("REST term-lookup 与 MCP 同口径（术语/系统/合并数）",
          r.status_code == 200 and d.get("status") == "ok"
          and (d.get("resolution") or {}).get("canonical") == (mcp_lookup.get("resolution") or {}).get("canonical")
          and d.get("merged_count") == mcp_lookup.get("merged_count")
          and set((d.get("systems") or {})) == set((mcp_lookup.get("systems") or {})), r.text[:250])

    cap_id = CTX["lead"]["id"]
    inputs = {"pn": CTX["pn"], "stock_qty": CTX["stock"], "stock_level": "normal",
              "required_qty": 1, "in_transit_qty": 0}
    rr = c.post(f"/api/capabilities/{cap_id}/execute", params=P, json={
        "instance_id": PART_ID, "inputs": inputs, "dry_run": True, "channel": "api"})
    rd = rr.json().get("data") or {}
    check("REST execute(inputs) 与 MCP capability_call 同口径",
          rr.status_code == 200 and rd.get("value") == 1
          and rd.get("inputs_persisted") is False and rd.get("dry_run") is True, rr.text[:300])

    audit = c.get("/api/dictionary/term-lookup-audit", params=P).json().get("data") or {}
    check("事后可核查：字典查询审计含本次输入/解析/系统状态",
          audit.get("total", 0) >= 1 and any(
              i.get("input_term") == "PN码" and i.get("status") == "ok"
              for i in audit.get("items", [])), str(audit)[:250])
    versions = c.get(f"/api/capabilities/{cap_id}/versions", params=P).json().get("data") or {}
    check("事后可核查：能力版本历史含 1.0.0（回答所引版本）",
          any(v.get("version") == "1.0.0" for v in versions.get("items", [])), str(versions)[:250])


def run_server() -> None:
    import uvicorn
    from main import app as fastapi_app

    port = socket.socket()
    port.bind(("127.0.0.1", 0))
    p = port.getsockname()[1]
    port.close()
    cfg = uvicorn.Config(fastapi_app, host="127.0.0.1", port=p,
                         log_level="warning", lifespan="off")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True, name="m1e-uvicorn")
    th.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        if srv.started:
            break
        if not th.is_alive():
            raise RuntimeError("uvicorn 线程意外退出")
        time.sleep(0.05)
    if not srv.started:
        check("uvicorn 就绪", False, "20s 未就绪")
        return
    base = f"http://127.0.0.1:{p}"
    try:
        out = run_agent_chain(base)
        CTX["chain"] = out
        verify_chain(out)
        rest_consistency(base)
    finally:
        srv.should_exit = True
        th.join(timeout=5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    print(f"DATA_DIR 隔离目录: {_TMP_DATA}")
    try:
        setup()
        run_server()
    finally:
        for m in CTX.get("mocks", ()):  # 关闭 3 个 mock
            try:
                m.stop()
            except Exception:  # noqa: BLE001
                pass
    print(f"\n===== 结果: PASS={PASS} FAIL={FAIL} SKIP={len(SKIPS)} =====")
    if FAILS:
        print("失败项:")
        for f in FAILS:
            print(" -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
