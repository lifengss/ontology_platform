# -*- coding: utf-8 -*-
"""M1-A（V2-01 数据字典与联邦查询）验收脚本。

用法：
    python scripts/verify_m1a_dictionary.py           # 全量（服务层 + REST + MCP 工具）
    python scripts/verify_m1a_dictionary.py --no-mcp  # 跳过 MCP SSE 段（不需要 uvicorn）

覆盖 docs/V2-测试用例.md TC-01-01~09（TP-01-01~09）与 TCE-01-01~06（EP-01-01~06）：
  TP-01-01 三系统合并带来源标记
  TP-01-02 未覆盖系统显式列出（不隐藏）
  TP-01-03 field_map 收敛（explicit/dictionary 同一数据源，入库/查询一致）
  TP-01-04 同义词解析（"PN码"/"PN"/"PN号" 同一标准词，结果一致）
  TP-01-05 歧义词 → 候选+置信度交人确认（不自动选）；晋升同义词后走精确匹配
  TP-01-06 成本护栏（dry_run 预览 / limit 下推 / 全量超限声明上限 / 确认-取消）
  TP-01-07 冲突 key（三系统值不一致 → 标记+并存；权威来源取权威值，其他值仍可见）
  TP-01-08 单系统故障 → 显式标 error，整体不挂
  TP-01-09 审计（输入/解析/各系统状态/行数/耗时/结果可查）
  EP-01-01 单系统 MCP 超时 → error 显式标注
  EP-01-02 全部系统不可用 → 显式报错
  EP-01-03 源头脏数据 → 拒收并定位（系统/字段），不进合并结果
  EP-01-04 全量拉取超限 → 预览提示成本，可确认/取消，不静默截断
  EP-01-05 词条未命中 → 显式未匹配，不猜测
  EP-01-06 字典冲突配置 → 保存时报错指出冲突项
"""
from __future__ import annotations

import argparse
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

# ---- 关键：导入 backend 前固定环境（隔离数据目录、零重试、短超时、mock token）----
_TMP_DATA = tempfile.mkdtemp(prefix="m1a_data_")
os.environ["DATA_DIR"] = _TMP_DATA
os.environ["MCP_MAX_RETRIES"] = "0"       # 故障用例零重试（快速失败）
os.environ["MCP_TIMEOUT_SECONDS"] = "1"   # EP-01-01 超时演练：mock 注入 2s 延迟 > 1s 超时
os.environ["M1A_MOCK_TOKEN"] = "tok-full"
sys.path.insert(0, os.path.join(REPO, "backend"))

import requests  # noqa: E402

from mocks import build_three_system_seed, make_all  # noqa: E402
from core.responses import ApiError  # noqa: E402
from core.storage import JsonStore  # noqa: E402
import dictionary  # noqa: E402

PID = "m1a-verify"
ADMIN = {"X-Mock-Admin": "mock-admin-2026"}
DEAD = "http://127.0.0.1:9/read"  # 未监听端口 → 连接拒绝

PASS = FAIL = 0
FAILS: list[str] = []


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
        "auth": {"type": "bearer_token", "bearer_token_ref": "${M1A_MOCK_TOKEN}"},
        "read_tool": "read",
        "fetch_mode": "paged", "fetch_limit": 0, "field_map_source": "explicit",
        "maps_to_ontology": {"ontology_id": "ont-pn", "object_type": "Product",
                             "field_map": dict(fmap)},
        "created_at": ts, "updated_at": ts,
    }


# 三系统连接：显式 field_map 把各异字段名映射到统一 property_id
CRM_FM = {"PN": "pn", "product_name": "name", "customer_name": "customer",
          "region": "region", "contract_amount": "amount", "contract_status": "status"}
PLM_FM = {"PN码": "pn", "产品名称": "name", "产品型号": "model",
          "版本": "version", "生命周期": "lifecycle"}
RSR_FM = {"PN_Num": "pn", "PN名称": "name", "装机容量MW": "capacity",
          "库水位": "level", "设计年份": "design_year"}

FULL_BINDINGS = {"conn-crm": "PN", "conn-plm": "PN码", "conn-rsr": "PN_Num"}


def setup() -> dict:
    """启动 3 mock + 写 3 条连接 + 建主词条。返回上下文。"""
    seed = build_three_system_seed(42)
    mocks = make_all(seed=42)
    crm, plm, rsr = mocks["crm"], mocks["plm"], mocks["reservoir"]
    for m in (crm, plm, rsr):
        m.start()  # port=0 → 自动分配空闲端口
    time.sleep(0.3)
    conn_store = JsonStore("mcp_connections", PID)
    conn_store.insert(conn_rec("conn-crm", "CRM系统", crm.port, CRM_FM))
    conn_store.insert(conn_rec("conn-plm", "PLM系统", plm.port, PLM_FM))
    conn_store.insert(conn_rec("conn-rsr", "水库系统", rsr.port, RSR_FM))
    term = dictionary.create_term(PID, {
        "canonical": "PN码", "object_type": "Product", "property_id": "pn",
        "synonyms": ["PN号"], "system_bindings": dict(FULL_BINDINGS),
    })
    ck = set(seed.conflict_keys)
    normal_key = next(r["PN"] for r in seed.crm_records if r["PN"] not in ck)
    return {"crm": crm, "plm": plm, "rsr": rsr, "seed": seed,
            "conn_store": conn_store, "term_id": term["id"],
            "normal_key": normal_key, "conflict_key": seed.conflict_keys[0]}


# --------------------------------------------------------------------------- #
# TP-01-04 同义词解析（TC-01-04）
# --------------------------------------------------------------------------- #
def tp01_04(ctx: dict) -> None:
    section("TP-01-04 同义词解析：PN码/PN/PN号 → 同一标准词")
    r1 = dictionary.resolve_term(PID, "PN码")
    r2 = dictionary.resolve_term(PID, "PN号")
    r3 = dictionary.resolve_term(PID, "PN")
    check("精确命中（canonical）", r1["match"] == "exact" and r1["confidence"] == 1.0,
          str(r1))
    check("同义词精确命中", r2["match"] == "exact" and r2["term"]["id"] == ctx["term_id"],
          str(r2))
    check("近似词确定性语义命中", r3["match"] == "semantic" and r3["term"]["id"] == ctx["term_id"]
          and r3["confidence"] > 0.6, str(r3))
    check("三种输入解析到同一词条", r1["term"]["id"] == r2["term"]["id"] == r3["term"]["id"])
    check("歧义标记未误触发", not r3["needs_disambiguation"], str(r3))


# --------------------------------------------------------------------------- #
# TP-01-01 三系统合并（TC-01-01）
# --------------------------------------------------------------------------- #
def tp01_01(ctx: dict) -> None:
    section("TP-01-01 三系统合并：同 key 一致，带来源标记")
    res = dictionary.federated_lookup(PID, "PN码", value=ctx["normal_key"], actor="tp01-01")
    check("status=ok", res["status"] == "ok", str(res.get("status")))
    check("合并为 1 条", res.get("merged_count") == 1, str(res.get("merged_count")))
    m0 = res["merged"][0]
    check("present_in 含 3 系统",
          set(m0["present_in"]) == {"CRM系统", "PLM系统", "水库系统"}, str(m0["present_in"]))
    f = m0["fields"]
    check("键字段一致", f["pn"]["value"] == ctx["normal_key"] and not f["pn"]["conflict"],
          str(f["pn"]))
    check("name 字段三系统一致且带来源", not f["name"]["conflict"]
          and len(f["name"]["sources"]) == 3, str(f["name"]))
    ps = res["per_system"]
    check("三系统均 ok 且各 1 行",
          all(v["status"] == "ok" and v["row_count"] == 1 for v in ps.values()), str(ps))


# --------------------------------------------------------------------------- #
# TP-01-07 冲突 key（TC-01-07）
# --------------------------------------------------------------------------- #
def tp01_07(ctx: dict) -> None:
    section("TP-01-07 冲突 key：值不一致标记并存；权威来源取权威值")
    res = dictionary.federated_lookup(PID, "PN码", value=ctx["conflict_key"])
    name = res["merged"][0]["fields"]["name"]
    check("冲突显式标记 conflict=true", name.get("conflict") is True, str(name))
    check("无权威来源时 value=None", name.get("value") is None, str(name.get("value")))
    vals = list(name.get("sources", {}).values())
    check("三方值并存且互不相同", len(vals) == 3 and len(set(vals)) == 3, str(name.get("sources")))

    dictionary.update_term(PID, ctx["term_id"], {"authoritative_source": "conn-crm"})
    res2 = dictionary.federated_lookup(PID, "PN码", value=ctx["conflict_key"])
    name2 = res2["merged"][0]["fields"]["name"]
    check("权威来源取权威值（crm 无后缀值）",
          name2.get("value") == name2.get("sources", {}).get("CRM系统")
          and not str(name2["value"]).endswith(("(甲)", "(乙)")), str(name2.get("value")))
    check("authoritative 标记为权威显示名", name2.get("authoritative") == "CRM系统",
          str(name2.get("authoritative")))
    check("非权威值仍可查（sources 保留）",
          len(name2.get("sources", {})) == 3, str(name2.get("sources", {}).keys()))
    dictionary.update_term(PID, ctx["term_id"], {"authoritative_source": None})


# --------------------------------------------------------------------------- #
# TP-01-02 未覆盖系统（TC-01-02）
# --------------------------------------------------------------------------- #
def tp01_02(ctx: dict) -> None:
    section("TP-01-02 未覆盖系统显式列出（不隐藏）")
    dictionary.update_term(PID, ctx["term_id"],
                           {"system_bindings": {"conn-crm": "PN", "conn-plm": "PN码"}})
    res = dictionary.federated_lookup(PID, "PN码", value=ctx["normal_key"])
    check("其余系统查询正常 ok", res["status"] == "ok", str(res["status"]))
    check("conn-rsr 在 uncovered 显式列出", "conn-rsr" in res.get("uncovered", []),
          str(res.get("uncovered")))
    check("systems 视图 covered=false",
          res["systems"]["conn-rsr"]["covered"] is False, str(res["systems"]["conn-rsr"]))
    check("present_in 只剩 2 系统",
          set(res["merged"][0]["present_in"]) == {"CRM系统", "PLM系统"},
          str(res["merged"][0]["present_in"]))
    dictionary.update_term(PID, ctx["term_id"], {"system_bindings": dict(FULL_BINDINGS)})

# --------------------------------------------------------------------------- #
# TP-01-05 歧义词（TC-01-05）
# --------------------------------------------------------------------------- #
def tp01_05(ctx: dict) -> None:
    section("TP-01-05 歧义：候选+置信度交人确认（不自动选）；晋升后走精确匹配")
    t_old = dictionary.create_term(PID, {
        "canonical": "PN码(旧)", "object_type": "Product", "property_id": "pn_old"})
    try:
        res = dictionary.federated_lookup(PID, "PN", value=ctx["normal_key"])
        check("status=needs_disambiguation（不自动选）",
              res["status"] == "needs_disambiguation", str(res.get("status")))
        cands = res.get("candidates") or []
        check("候选含两个近似词条", len(cands) == 2
              and {c["canonical"] for c in cands} == {"PN码", "PN码(旧)"}, str(cands))
        check("候选带置信度", all(isinstance(c.get("confidence"), float) for c in cands), str(cands))
        check("未产生合并结果", not res.get("merged"))
        # 人工确认：把 "PN" 晋升为 "PN码" 的同义词
        dictionary.promote_synonym(PID, ctx["term_id"], "PN")
        r = dictionary.resolve_term(PID, "PN")
        check("晋升后走精确匹配", r["match"] == "exact" and r["term"]["id"] == ctx["term_id"],
              str(r))
        res2 = dictionary.federated_lookup(PID, "PN", value=ctx["normal_key"])
        check("精确命中后查询正常", res2["status"] == "ok" and res2["merged_count"] == 1,
              str(res2.get("status")))
    finally:
        dictionary.delete_term(PID, t_old["id"])
        dictionary.update_term(PID, ctx["term_id"],
                               {"synonyms": [s for s in
                                              (dictionary.get_term(PID, ctx["term_id"]) or {})
                                              .get("synonyms", []) if s != "PN"]})


# --------------------------------------------------------------------------- #
# TP-01-06 + EP-01-04 成本护栏（TC-01-06 / TCE-01-04）
# --------------------------------------------------------------------------- #
class _CaptureClient:
    """桩 client：记录 invoke_tool 参数，验证 limit 下推行为。"""

    def __init__(self):
        self.calls = []

    def invoke_tool(self, config, tool, params):
        self.calls.append((config.get("connection_id"), dict(params or {})))
        return []


def tp01_06(ctx: dict) -> None:
    section("TP-01-06/EP-01-04 成本护栏：dry_run 预览 / limit 下推 / 全量声明上限 / 确认-取消")
    cs = ctx["conn_store"]
    crm_p, plm_p = ctx["crm"].port, ctx["plm"].port
    cs.update("conn-crm", {"fetch_mode": "full", "fetch_limit": 200})
    admin_cfg(crm_p, full_fetch_only=True)  # mock 不再支持 limit 下推
    try:
        res_dry = dictionary.federated_lookup(PID, "PN码", value=ctx["normal_key"],
                                              dry_run=True)
        ce = res_dry.get("cost_estimate") or {}
        check("dry_run 返回 preview（不发起外呼）", res_dry["status"] == "preview",
              str(res_dry.get("status")))
        check("全量系统预估=声明的 fetch_limit(200) 且 needs_confirm",
              ce["systems"]["conn-crm"]["est_rows"] == 200 and ce["needs_confirm"] is True,
              str(ce))
        check("分页系统 limit 下推进预估（est=50）",
              ce["systems"]["conn-plm"]["est_rows"] == 50, str(ce["systems"]["conn-plm"]))

        reads_before = sum(m.read_calls for m in (ctx["crm"], ctx["plm"], ctx["rsr"]))
        res_c = dictionary.federated_lookup(PID, "PN码", value=ctx["normal_key"])
        check("未确认 → cost_confirmation_required（可取消）",
              res_c["status"] == "cost_confirmation_required", str(res_c.get("status")))
        reads_after = sum(m.read_calls for m in (ctx["crm"], ctx["plm"], ctx["rsr"]))
        check("确认前未发起外呼（mock read_calls 不变）", reads_before == reads_after,
              f"{reads_before} -> {reads_after}")

        res_e = dictionary.federated_lookup(PID, "PN码", value=ctx["normal_key"],
                                            confirm_cost=True)
        check("确认后执行 ok", res_e["status"] == "ok", str(res_e.get("status")))
        check("crm 以 full 模式取数", res_e["per_system"]["conn-crm"]["fetch_mode"] == "full",
              str(res_e["per_system"]["conn-crm"]))

        # limit 下推：分页系统 params 带 limit；全量系统不带（平台本地兜底）
        term = dictionary.get_term(PID, ctx["term_id"])
        cap_p, cap_f = _CaptureClient(), _CaptureClient()
        dictionary._fetch_one(cap_p, cs.get("conn-plm"), term, "pn-x", 50)
        dictionary._fetch_one(cap_f, cs.get("conn-crm"), term, "pn-x", 50)
        check("分页系统 limit 下推（params.limit=50）",
              cap_p.calls and cap_p.calls[0][1].get("limit") == 50, str(cap_p.calls))
        check("全量系统不下推 limit（本地兜底）",
              cap_f.calls and "limit" not in cap_f.calls[0][1], str(cap_f.calls))

        # 全量返回超声明上限 → 本地截断并标记（不静默）
        # 一对多结果：250 行均属键 PN-100000（本地键过滤全保留）→ 超 fetch_limit=200 截断
        class _BigClient(_CaptureClient):
            def invoke_tool(self, config, tool, params):
                return [{"PN": "PN-100000", "seq": i} for i in range(250)]
        r_big = dictionary._fetch_one(_BigClient(), cs.get("conn-crm"), term,
                                      "PN-100000", 50)
        check("超 fetch_limit 本地截断并标 truncated",
              r_big["row_count"] == 200 and r_big["truncated"] is True,
              f"row_count={r_big['row_count']} truncated={r_big.get('truncated')}")
    finally:
        admin_cfg(crm_p, full_fetch_only=False)
        cs.update("conn-crm", {"fetch_mode": "paged", "fetch_limit": 0})


# --------------------------------------------------------------------------- #
# TP-01-03 field_map 收敛（TC-01-03）
# --------------------------------------------------------------------------- #
def tp01_03(ctx: dict) -> None:
    section("TP-01-03 field_map 收敛：explicit/dictionary 同一数据源，入库/查询一致")
    cs = ctx["conn_store"]
    srcs0 = dictionary.field_map_sources(PID)
    check("初始 3 连接均为 explicit 且非空",
          len(srcs0) == 3 and all(s["field_map_source"] == "explicit"
                                  and s["field_map_size"] > 0 for s in srcs0), str(srcs0))

    # 切 PLM 为 dictionary 源：field_map 由数据字典（词条 system_bindings）生成
    cs.update("conn-plm", {"field_map_source": "dictionary"})
    fm, fsrc = dictionary.resolve_field_map(PID, cs.get("conn-plm"))
    check("dictionary 源映射由词条绑定生成", fsrc == "dictionary" and fm == {"PN码": "pn"},
          f"{fsrc}: {fm}")
    srcs1 = dictionary.field_map_sources(PID)
    plm1 = next(s for s in srcs1 if s["connection_id"] == "conn-plm")
    check("盘点视图显示 dictionary/size=1",
          plm1["field_map_source"] == "dictionary" and plm1["field_map_size"] == 1, str(plm1))

    # 字典单点修改 → 入库与查询两个方向同时生效（同一解析函数 resolve_field_map）
    dictionary.update_term(PID, ctx["term_id"],
                           {"system_bindings": {**FULL_BINDINGS, "conn-plm": "PN码v2"}})
    fm2, _ = dictionary.resolve_field_map(PID, cs.get("conn-plm"))
    check("字典改绑定 → field_map 同步变更", fm2 == {"PN码v2": "pn"}, str(fm2))
    dictionary.update_term(PID, ctx["term_id"], {"system_bindings": dict(FULL_BINDINGS)})
    cs.update("conn-plm", {"field_map_source": "explicit"})

# --------------------------------------------------------------------------- #
# TP-01-08 + EP-01-01 单系统故障 / 超时（TC-01-08 / TCE-01-01）
# --------------------------------------------------------------------------- #
def tp01_08(ctx: dict) -> None:
    section("TP-01-08/EP-01-01 单系统故障或超时：显式标 error，整体不挂")
    cs = ctx["conn_store"]
    rsr_url = cs.get("conn-rsr")["endpoint"]
    cs.update("conn-rsr", {"endpoint": DEAD})
    try:
        res = dictionary.federated_lookup(PID, "PN码", value=ctx["normal_key"])
        check("整体仍 ok（其余系统正常）", res["status"] == "ok", str(res["status"]))
        ps = res["per_system"]["conn-rsr"]
        check("故障系统显式 status=error + code", ps["status"] == "error"
              and ps.get("code") == "MCP_UNREACHABLE", str(ps))
        check("present_in 只剩 2 系统",
              set(res["merged"][0]["present_in"]) == {"CRM系统", "PLM系统"},
              str(res["merged"][0]["present_in"]))
    finally:
        cs.update("conn-rsr", {"endpoint": rsr_url})

    # 超时演练（EP-01-01）：plm 注入 2s 延迟 > MCP_TIMEOUT_SECONDS=1
    admin_cfg(ctx["plm"].port, delay_ms=2000)
    try:
        res = dictionary.federated_lookup(PID, "PN码", value=ctx["normal_key"])
        ps = res["per_system"]["conn-plm"]
        check("超时系统显式 error，其余正常", ps["status"] == "error"
              and res["status"] == "ok", f"overall={res['status']} plm={ps}")
    finally:
        admin_cfg(ctx["plm"].port, delay_ms=0)


# --------------------------------------------------------------------------- #
# EP-01-02 全部系统不可用（TCE-01-02）
# --------------------------------------------------------------------------- #
def ep01_02(ctx: dict) -> None:
    section("EP-01-02 全部系统不可用 → 显式报错")
    cs = ctx["conn_store"]
    orig = {cid: cs.get(cid)["endpoint"] for cid in ("conn-crm", "conn-plm", "conn-rsr")}
    for cid in orig:
        cs.update(cid, {"endpoint": DEAD})
    try:
        res = dictionary.federated_lookup(PID, "PN码", value=ctx["normal_key"])
        check("status=error（非静默空结果）", res["status"] == "error", str(res.get("status")))
        check("错误信息含各系统失败详情", "conn-crm" in (res.get("error") or "")
              and "conn-plm" in (res.get("error") or ""), str(res.get("error")))
        check("无合并结果", not res.get("merged"))
    finally:
        for cid, url in orig.items():
            cs.update(cid, {"endpoint": url})


# --------------------------------------------------------------------------- #
# EP-01-03 源头脏数据（TCE-01-03）
# --------------------------------------------------------------------------- #
def ep01_03(ctx: dict) -> None:
    section("EP-01-03 源头脏数据 → 拒收并定位（系统/字段），不进合并结果")
    cs = ctx["conn_store"]
    term = dictionary.get_term(PID, ctx["term_id"])
    crm = cs.get("conn-crm")

    class _DirtyClient:
        """部分脏：1 行缺键、1 行非对象、1 行正常。"""
        def invoke_tool(self, config, tool, params):
            return [{"product_name": "无键行"}, "garbage", {"PN": "PN-100000"}]

    r = dictionary._fetch_one(_DirtyClient(), crm, term, "pn-100000", 50)
    check("部分脏：仅干净行进结果", r["status"] == "ok" and r["row_count"] == 1
          and r["rejected_rows"] == 2, str(r))

    class _AllDirtyClient:
        def invoke_tool(self, config, tool, params):
            return [{"别的字段": 1}, {"PN": ""}]

    r2 = dictionary._fetch_one(_AllDirtyClient(), crm, term, "pn-100000", 50)
    check("全脏：系统级 error + code=MCP_DIRTY_DATA",
          r2["status"] == "error" and r2.get("code") == "MCP_DIRTY_DATA", str(r2))
    check("错误定位到系统与字段", "conn-crm" in (r2.get("error") or "")
          and "PN" in (r2.get("error") or ""), str(r2.get("error")))


# --------------------------------------------------------------------------- #
# EP-01-05 词条未命中（TCE-01-05）
# --------------------------------------------------------------------------- #
def ep01_05(ctx: dict) -> None:
    section("EP-01-05 词条未命中 → 显式未匹配，不猜测")
    res = dictionary.federated_lookup(PID, "zzz不存在的词", value="PN-100000")
    check("status=term_not_found", res["status"] == "term_not_found", str(res.get("status")))
    check("resolution 显式未匹配（不猜近似）",
          (res.get("resolution") or {}).get("term") is None, str(res.get("resolution")))
    check("无合并结果", not res.get("merged"))


# --------------------------------------------------------------------------- #
# EP-01-06 字典冲突配置（TCE-01-06）
# --------------------------------------------------------------------------- #
def ep01_06(ctx: dict) -> None:
    section("EP-01-06 字典冲突配置：同一系统字段绑定两词条 → 保存报错")
    try:
        dictionary.create_term(PID, {
            "canonical": "零件编号", "object_type": "Product",
            "system_bindings": {"conn-crm": "PN"}})  # PN 已被 "PN码" 绑定
        check("冲突绑定被拒绝", False, "未抛错")
    except ApiError as e:
        check("冲突绑定被拒绝（DICT_FIELD_CONFLICT 409）",
              e.code == "DICT_FIELD_CONFLICT" and e.http_status == 409, str(e))
        check("报错指出冲突项（系统/字段/已有词条）",
              "conn-crm" in e.message and "PN" in e.message and "PN码" in e.message, e.message)

# --------------------------------------------------------------------------- #
# TP-01-09 审计（TC-01-09）
# --------------------------------------------------------------------------- #
def tp01_09(ctx: dict) -> None:
    section("TP-01-09 审计：输入/解析/各系统状态/行数/耗时可查")
    a = dictionary.list_audit(PID)
    check("审计 total>0", a["total"] > 0, str(a["total"]))
    item = next((i for i in a["items"]
                 if i.get("actor") == "tp01-01" and i.get("status") == "ok"
                 and i.get("value") == ctx["normal_key"]), None)
    check("能按输入/状态查到 tp01-01 记录", item is not None,
          f"items={len(a['items'])}")
    if item:
        rinfo = item.get("resolution") or {}
        ps = item.get("per_system") or {}
        check("记录含解析(词/方式/置信度)+各系统状态/行数+耗时",
              rinfo.get("canonical") == "PN码" and rinfo.get("match") in ("exact", "semantic")
              and rinfo.get("confidence") == 1.0
              and set(ps) == {"conn-crm", "conn-plm", "conn-rsr"}
              and all(v.get("status") == "ok" and v.get("row_count") == 1 for v in ps.values())
              and isinstance(item.get("cost_ms"), int),
              json.dumps(item, ensure_ascii=False)[:400])


# --------------------------------------------------------------------------- #
# REST 冒烟（信封/错误码/分页结构）
# --------------------------------------------------------------------------- #
def rest_smoke(ctx: dict) -> None:
    section("REST 冒烟：/api/dictionary 信封与错误码")
    from fastapi.testclient import TestClient
    from main import app

    c = TestClient(app)
    P = {"project_id": PID}

    r = c.post("/api/dictionary/terms", params=P, json={
        "canonical": "客户编码", "object_type": "Customer", "property_id": "code"})
    ok = r.status_code == 201 and r.json()["success"] is True
    check("建词条 201 信封", ok, r.text)
    tid = r.json()["data"]["id"]

    r = c.get("/api/dictionary/terms", params=P)
    check("词条列表 {items,total}", r.json()["success"]
          and isinstance(r.json()["data"].get("items"), list)
          and r.json()["data"].get("total", 0) >= 2, r.text)

    r = c.post("/api/dictionary/terms", params=P, json={
        "canonical": "零件编号", "system_bindings": {"conn-crm": "PN"}})
    check("冲突绑定 409 + error.code", r.status_code == 409
          and r.json()["error"]["code"] == "DICT_FIELD_CONFLICT", r.text)

    r = c.post("/api/dictionary/term-lookup", params=P, json={
        "term": "PN码", "value": ctx["normal_key"], "dry_run": True})
    check("term-lookup dry_run → data.status=preview",
          r.status_code == 200 and r.json()["data"]["status"] == "preview", r.text)

    r = c.post("/api/dictionary/import-from-connections", params=P,
               json={"connection_ids": ["conn-plm"]})
    d = r.json().get("data") or {}
    # PLM 5 字段：pn 已有词条且绑定已存在（不计 merged），其余 4 个属性新建词条
    check("import-from-connections 建词条（created=4，存量绑定补全为 0）",
          r.status_code == 200 and len(d.get("created", [])) == 4
          and d.get("connections_scanned") == 1, r.text)

    r = c.get("/api/dictionary/field-map-sources", params=P)
    d = r.json()
    check("field-map-sources {items,total}", d["success"]
          and isinstance(d["data"].get("items"), list)
          and d["data"].get("total") == 3, r.text)

    r = c.get("/api/dictionary/term-lookup-audit", params=P)
    d = r.json()
    check("审计列表 {items,total} 且 total>0", d["success"]
          and isinstance(d["data"].get("items"), list)
          and d["data"].get("total", 0) > 0, r.text)

    r = c.delete(f"/api/dictionary/terms/{tid}", params=P)
    check("删词条 200 信封（项目约定）", r.status_code == 200
          and r.json()["success"] and r.json()["data"].get("deleted"), r.text)


# --------------------------------------------------------------------------- #
# MCP 工具段：真实 SDK 客户端经 SSE 调 term_lookup
# --------------------------------------------------------------------------- #
def _pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_mcp(ctx: dict) -> None:
    section("MCP 工具：term_lookup 注册并可经 SSE 调用（真实 SDK 客户端）")
    import uvicorn
    from main import app

    port = _pick_free_port()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port,
                         log_level="warning", lifespan="off")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True, name="m1a-uvicorn")
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
    base = f"http://127.0.0.1:{port}"
    try:
        _mcp_calls(ctx, base)
    finally:
        srv.should_exit = True
        th.join(timeout=5)


def _mcp_calls(ctx: dict, base: str) -> None:
    P = {"project_id": PID}
    r = requests.post(f"{base}/api/ontologies", params=P, json={
        "name": "M1A验收-PN域", "description": "M1-A 验收用最小本体",
        "modeling_type": "manual",
        "objects": [{
            "id": "Product", "name": "产品", "business_definition": "产品",
            "stable_key": "pn",
            "attributes": [{"id": "pn", "name": "PN码", "data_type": "string",
                            "required": True, "constraints": {}}],
            "relations": [],
        }], "constraints": []})
    ok = r.status_code == 201
    check("最小本体创建", ok, r.text)
    if not ok:
        return
    oid = r.json()["data"]["id"]

    rv = requests.post(f"{base}/api/ontologies/{oid}/validate", params=P, json={
        "sample_instances": [
            {"object_type": "Product", "pn": ctx["normal_key"]}]})
    check("SHACL 校验通过", rv.status_code == 200
          and rv.json()["data"]["valid"] is True, rv.text)

    rp = requests.post(f"{base}/api/mcp/server/publish",
                       params={**P, "ontology_id": oid})
    check("发布项目 MCP Server", rp.status_code == 200
          and rp.json()["data"]["published"] is True, rp.text)
    rt = requests.post(f"{base}/api/mcp/server/token", params=P)
    token = rt.json()["data"]["token"]
    check("生成 MCP token", rt.status_code == 201 and bool(token), rt.text)

    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async def _run() -> dict:
        out: dict = {}
        async with sse_client(f"{base}/mcp/{PID}",
                              headers={"Authorization": f"Bearer {token}"}) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                tools = await s.list_tools()
                out["tools"] = sorted(t.name for t in tools.tools)
                out["ok"] = await s.call_tool(
                    "term_lookup", {"term": "PN码", "value": ctx["normal_key"]})
                out["preview"] = await s.call_tool(
                    "term_lookup", {"term": "PN码", "value": ctx["normal_key"],
                                    "dry_run": True})
                out["miss"] = await s.call_tool("term_lookup", {"term": "zzz不存在的词"})
        return out

    out = asyncio.run(_run())
    check("term_lookup 在工具列表（共 5 工具）", "term_lookup" in out["tools"]
          and len(out["tools"]) == 5, str(out["tools"]))

    def _text(res) -> str:
        return res.content[0].text if res.content else ""

    ok_res = out["ok"]
    check("MCP ok 调用：合并结果带来源",
          not ok_res.is_error
          and json.loads(_text(ok_res)).get("status") == "ok"
          and json.loads(_text(ok_res))["merged_count"] == 1, _text(ok_res)[:200])
    pv = out["preview"]
    check("MCP dry_run：preview 成本预览",
          not pv.is_error and json.loads(_text(pv)).get("status") == "preview",
          _text(pv)[:200])
    miss = out["miss"]
    check("MCP 词条未命中：isError + TERM_NOT_FOUND 错误码",
          miss.is_error and "TERM_NOT_FOUND" in _text(miss), _text(miss)[:200])


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mcp", action="store_true", help="跳过 MCP SSE 段")
    args = ap.parse_args()

    print(f"DATA_DIR 隔离目录: {_TMP_DATA}")
    ctx = setup()
    print(f"mock: crm={ctx['crm'].port} plm={ctx['plm'].port} rsr={ctx['rsr'].port}")
    print(f"normal_key={ctx['normal_key']} conflict_key={ctx['conflict_key']}")

    tp01_04(ctx)
    tp01_01(ctx)
    tp01_07(ctx)
    tp01_02(ctx)
    tp01_05(ctx)
    tp01_06(ctx)
    tp01_03(ctx)
    tp01_08(ctx)
    ep01_02(ctx)
    ep01_03(ctx)
    ep01_05(ctx)
    ep01_06(ctx)
    tp01_09(ctx)
    rest_smoke(ctx)
    if not args.no_mcp:
        run_mcp(ctx)

    for m in (ctx["crm"], ctx["plm"], ctx["rsr"]):
        m.stop()

    print(f"\n===== 结果: PASS={PASS} FAIL={FAIL} =====")
    if FAILS:
        print("失败项:")
        for f in FAILS:
            print(" -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
