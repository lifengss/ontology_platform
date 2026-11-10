# -*- coding: utf-8 -*-
"""M1-B（V2-04 计算能力与公式引擎首片）验收脚本。

用法：
    python scripts/verify_m1b_capabilities.py            # 全量（服务层 + REST + 真实 MCP SSE）
    python scripts/verify_m1b_capabilities.py --no-mcp  # 跳过 uvicorn / MCP 段

覆盖 docs/V2-测试用例.md：
  TC-04-01 六成本公式对 33 行 golden（L1 87 项硬基准 / L2 8 行 MOCK 软基准）
  TC-04-02 四要素溯源；TC-04-03 删除被依赖 422 + draft 经 MCP 403
  TC-04-04 版本锁不跟随；TC-04-05 上传型写回本地
  TC-04-07 MCP 与 UI 同口径；TC-04-08 与 V2-21 分工边界；TC-04-09 性能基线计时
  TCE-04-02（EP-04-02）非法公式 / 运行期错误不中断批次
  TCE-04-04（EP-04-04）引用失效版本拒绝
  TCE-04-01（EP-04-01）null 显式计数
  EP-04-03 / TC-04-06 源连接型渠道按偏差决策 D2 延至 M4-A（显式 SKIP）

数据：data/datasets/v2/maintenance_cost_2297.xlsx（17 对象 2297 实例，上传型）
      + 仓库根 成本清单_维保服务产品_复算版.xlsx（33 行 golden，L1/L2 分级）
口径冻结见 data/datasets/v2/README.md §1；L1 容差 1e-9，L2 不参与 L1 判定。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import statistics
import sys
import tempfile
import threading
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TMP_DATA = tempfile.mkdtemp(prefix="m1b_data_")
os.environ["DATA_DIR"] = _TMP_DATA
sys.path.insert(0, os.path.join(REPO, "backend"))

import openpyxl  # noqa: E402
import requests  # noqa: E402

import capabilities  # noqa: E402
from capabilities import CapabilityApiError, CapabilityService  # noqa: E402
from core.instance_store import get_instance_store  # noqa: E402

PID = "m1b-verify"
ONT = "ont-m1b-maintenance-cost"
DATA_XLSX = os.path.join(REPO, "data", "datasets", "v2", "maintenance_cost_2297.xlsx")
GOLDEN_XLSX = os.path.join(REPO, "成本清单_维保服务产品_复算版.xlsx")
GOLDEN_SHEET = "成本清单33行"
PRODUCT = "obj_maintenance_product"
TOL = 1e-9

PASS = FAIL = 0
FAILS: list[str] = []
SKIPS: list[str] = []

# golden 列：源表 / 复算 / 校验 → 引擎能力
COLS = {
    "E": (8, 9, 10, "cap_labor_cost_e"),
    "K": (14, 15, 16, "cap_spare_aux_cost_k"),
    "N": (19, 20, 21, "cap_mgmt_cost_n"),
    "O": (22, 23, 24, "cap_total_cost_o"),
    "S": (26, 27, 28, "cap_cost_ratio_s"),
}

EXPRS = {
    "cap_onsite_rate_d": """d3 = lookup('obj_onsite_rate_baseline', {
  'attr_product_line': this.attr_product_line,
  'attr_customer_segment': this.attr_customer_segment,
  'attr_domestic': this.attr_domestic
}, 'attr_onsite_rate')
d3 * this.attr_term_factor""",
    "cap_labor_cost_e": """if(this.attr_has_labor_baseline,
  if(this.attr_domestic == '国内',
    lookup('obj_aftersales_labor_baseline', {
      'attr_domestic': this.attr_domestic,
      'attr_u_band': this.attr_u_band,
      'attr_product_category': this.attr_product_category,
      'attr_response_level': this.attr_response_level,
      'attr_customer': this.attr_customer
    }, 'attr_baseline_value'),
    lookup('obj_aftersales_labor_baseline', {
      'attr_domestic': this.attr_domestic,
      'attr_u_band': this.attr_u_band,
      'attr_product_category': this.attr_product_category,
      'attr_response_level': this.attr_response_level,
      'attr_region': this.attr_c_region
    }, 'attr_baseline_value')) * ref('cap_onsite_rate_d'),
  null)""",
    "cap_spare_aux_cost_k": """if(this.attr_has_aux_baseline,
  lookup('obj_spare_aux_baseline', {'attr_country': this.attr_country}, this.attr_clause_attr)
    * ref('cap_onsite_rate_d'),
  null)""",
    "cap_mgmt_cost_n": """if(this.attr_has_mgmt_baseline,
  lookup('obj_mgmt_cost_baseline', {
    'attr_customer': this.attr_customer,
    'attr_business_stage': this.attr_business_stage,
    'attr_domestic': this.attr_domestic
  }, 'attr_baseline_value') * ref('cap_onsite_rate_d'),
  null)""",
    "cap_total_cost_o": """if(this.attr_has_aux_baseline,
  if_null(ref('cap_labor_cost_e'), 0) + this.attr_spare_direct_cost
    + if_null(ref('cap_spare_aux_cost_k'), 0) + if_null(ref('cap_mgmt_cost_n'), 0),
  this.attr_spare_direct_cost)""",
    "cap_cost_ratio_s": "ref('cap_total_cost_o') / this.attr_machine_cost",
}

OUTPUT_ATTRS = {
    "cap_onsite_rate_d": "attr_calc_d",
    "cap_labor_cost_e": "attr_calc_e",
    "cap_spare_aux_cost_k": "attr_calc_k",
    "cap_mgmt_cost_n": "attr_calc_n",
    "cap_total_cost_o": "attr_calc_o",
    "cap_cost_ratio_s": "attr_calc_s",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}  {detail}")


def skip(name: str, why: str) -> None:
    SKIPS.append(name)
    print(f"  [SKIP] {name}  {why}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def dims_for(region: str, term: str, ptype: str) -> dict:
    """仅从成本清单自身列（区域/服务期限/类型）推导 lookup 维度，不窥答案列。"""
    domestic = "国内" if region == "国内" else "海外"
    years = re.findall(r"([二三四五])年", term or "")
    # ARMA+RMA 组合行以 RMA 年数计年限（二年→1.0 / 三年→1.4 / 四年→1.88），
    # 标准与 ARMA 单列以服务期限年数计（三年→1.0 / 四年→1.4 / 五年→1.88）。口径见 P9 §2.1。
    factors = ({"二": 1.0, "三": 1.4, "四": 1.88} if "ARMA+" in (term or "")
               else {"三": 1.0, "四": 1.4, "五": 1.88})
    factor = factors.get(years[-1] if years else "三", 1.0)
    if "NBD" in term:
        resp = "NBD"
    elif "4H" in term:
        resp = "4H"
    elif "2H" in term:
        resp = "2H"
    else:
        resp = None
    if ptype == "介质保留":
        clause = None
    elif ptype == "RMA":
        clause = "attr_machine_rma"
    elif ptype == "ARMA":
        clause = "attr_machine_arma"
    elif resp == "NBD":
        clause = "attr_machine_nd"
    else:
        clause = "attr_machine_4h"
    return {
        "attr_region_label": region,
        "attr_service_term_label": term,
        "attr_product_type": ptype,
        "attr_domestic": domestic,
        "attr_country": "国内" if domestic == "国内" else "德国",
        "attr_c_region": "欧洲",
        "attr_product_line": "SV",
        "attr_customer_segment": "非BATZ",
        "attr_term_factor": factor,
        "attr_response_level": resp,
        "attr_clause_attr": clause,
        "attr_u_band": "1U/2U",
        "attr_product_category": "通用服务器",
        "attr_customer": "其他",
        "attr_business_stage": "售后",
        "attr_has_labor_baseline": resp is not None,
        "attr_has_aux_baseline": clause is not None,
        "attr_has_mgmt_baseline": ptype != "介质保留",
    }


def _rows(wb, sheet: str, header_row: int = 1):
    ws = wb[sheet]
    rows = [r for r in ws.iter_rows(values_only=True)]
    hdr = [str(c) if c is not None else f"col_{i}" for i, c in enumerate(rows[header_row - 1])]
    return hdr, [r for r in rows[header_row:] if r and r[0] is not None]


def setup() -> dict:
    section("准备：2297 实例（上传型）+ 33 行 golden + 6 个成本公式")
    store = get_instance_store(PID)
    dwb = openpyxl.load_workbook(DATA_XLSX, read_only=True, data_only=True)
    gwb = openpyxl.load_workbook(GOLDEN_XLSX, read_only=True, data_only=True)
    _, golden = _rows(gwb, GOLDEN_SHEET)
    phdr, products = _rows(dwb, PRODUCT)
    check("golden 33 行 / 产品 33 行", len(golden) == 33 and len(products) == 33,
          f"golden={len(golden)} products={len(products)}")
    aligned = all(p[1] == g[2] for p, g in zip(products, golden))
    check("产品行与 golden 服务期限逐行对齐", aligned,
          str([(p[1], g[2]) for p, g in zip(products, golden) if p[1] != g[2]][:3]))

    total = 0
    product_ids: list[str] = []
    for sheet in dwb.sheetnames:
        hdr, rows = _rows(dwb, sheet)
        for i, row in enumerate(rows, start=2):
            inst = {hdr[j]: row[j] for j in range(min(len(hdr), len(row)))}
            rid = f"{sheet}-{i}"
            if sheet == PRODUCT:
                g = golden[i - 2]
                inst.update(dims_for(g[1], g[2], g[3]))
                product_ids.append(rid)
            store.insert({
                "id": rid, "project_id": PID, "ontology_id": ONT,
                "object_type": sheet, "instance": inst,
                "source": {"kind": "upload", "raw_ref": f"{os.path.basename(DATA_XLSX)}:{sheet}!{i}"},
            })
            total += 1
    check("17 对象 / 2297 实例落本地 JSONL", total == 2297 and store.count() == 2297,
          f"total={total} count={store.count()}")

    svc = CapabilityService(PID)
    caps: dict[str, dict] = {}
    for name, expr in EXPRS.items():
        rec = svc.create_capability({
            "name": name, "display_name": name, "description": "V2-04 成本公式（M1-B 验收）",
            "kind": "derive", "implementation": "expression", "ontology_id": ONT,
            "object_type": PRODUCT, "expression": expr,
            "output_attribute": OUTPUT_ATTRS[name],
        })
        caps[name] = rec
    check("6 个成本公式创建为 draft 1.0.0", len(caps) == 6
          and all(r["status"] == "draft" and r["version"] == "1.0.0" for r in caps.values()))
    published = {}
    for name in EXPRS:  # 依赖顺序：D → E/K/N → O → S
        published[name] = svc.publish_capability(caps[name]["id"])
    check("6 个公式发布 active 并锁定引用版本", all(r["status"] == "active" for r in published.values())
          and all(published[n]["ref_locks"] for n in ("cap_labor_cost_e", "cap_total_cost_o")))

    draft = svc.create_capability({
        "name": "cap_draft_forbidden", "object_type": PRODUCT,
        "expression": "1 + 1", "output_attribute": "attr_calc_draft",
    })
    return {"store": store, "svc": svc, "caps": caps, "golden": golden,
            "products": products, "product_ids": product_ids, "draft": draft,
            "perf_ms": None}


# --------------------------------------------------------------------------- #
def tp04_01(ctx: dict) -> None:
    section("TC-04-01 六成本公式 × 33 行 golden（L1 87 项硬基准）")
    svc, ids, golden = ctx["svc"], ctx["product_ids"], ctx["golden"]
    values: dict[str, dict[str, dict]] = {}
    for col, (_, _, _, cap) in COLS.items():
        res = svc.execute_batch(ctx["caps"][cap]["id"], instance_ids=ids,
                                dry_run=True, channel="ui")
        values[col] = {it["instance_id"]: it for it in res["items"]}
        check(f"批量 {col} 返回 33 项且无框架异常", res["total"] == 33 and len(values[col]) == 33,
              f"total={res['total']} success={res['success']} error={res['error']}")

    bad: list[str] = []
    l1_count = l2_count = l1_diff = null_count = 0
    for ri, g in enumerate(golden):
        pid = ids[ri]
        tier = str(g[29] or "").strip()
        for col, (src_i, calc_i, flag_i, _) in COLS.items():
            item = values[col].get(pid, {})
            flag = str(g[flag_i] or "").strip()
            val, src, calc = item.get("value"), num(g[src_i]), num(g[calc_i])
            if flag == "n/a":
                null_count += 1
                if not (item.get("status") == "success" and val is None):
                    bad.append(f"r{ri + 3}/{col} 期望 null，实际 {item.get('status')} {val}")
                continue
            if not (item.get("status") == "success" and val is not None
                    and calc is not None and abs(float(val) - calc) <= TOL):
                bad.append(f"r{ri + 3}/{col} 引擎={val} 复算={calc} status={item.get('status')}")
                continue
            if tier == "VERIFIED":
                l1_count += 1
                if src is None or abs(src - calc) > TOL:
                    l1_diff += 1
            elif tier == "MOCK":
                l2_count += 1
    check("L1 硬基准 87 项且 golden 自身源表=复算（容差 1e-9）",
          l1_count == 87 and l1_diff == 0, f"l1={l1_count} diff={l1_diff}")
    check("E/K/N/O/S 逐行复算全部一致（含 L2 MOCK 软基准）", not bad,
          f"bad={len(bad)} 例: {bad[:6]}")
    check("TC-04-02 golden '—' 项引擎返回 null（不当 0）", null_count > 0 and not any(
        "期望 null" in b for b in bad), f"null_items={null_count}")
    check("L2 MOCK 行按区域口径出数（不静默跟随源表）", l2_count > 0,
          f"l2_items={l2_count}")
    ctx["col_values"] = values


def tp04_02_05(ctx: dict) -> None:
    section("TC-04-02 四要素溯源 / TC-04-05 上传型写回本地")
    svc, store = ctx["svc"], ctx["store"]
    pid = ctx["product_ids"][3]  # 国内四年标准行
    cap = ctx["caps"]["cap_total_cost_o"]
    res = svc.execute(cap["id"], pid, channel="ui", trigger_source="manual")
    tr = res.get("trace") or {}
    check("溯源：能力版本 + 输入实例", bool(res.get("capability_version"))
          and res.get("instance_id") == pid, str(res)[:200])
    check("溯源：输入属性 + 输入原值", bool(tr.get("inputs"))
          and all("attr" in x and "value" in x for x in tr["inputs"]), str(tr.get("inputs"))[:200])
    check("溯源：lookup 命中 record_id/编码/原始值", bool(tr.get("lookups"))
          and all(x.get("matched_record_id") and x.get("matched_code")
                  and "value" in x for x in tr["lookups"]), str(tr.get("lookups"))[:300])
    rec = store.get(pid)
    inst = rec.get("instance", {})
    meta = inst.get("__capability__") or {}
    check("结果写回本地 output_attribute", inst.get("attr_calc_o") == res.get("value"),
          f"written={inst.get('attr_calc_o')} value={res.get('value')}")
    check("源字段不覆盖", inst.get("attr_total_cost") == ctx["products"][3][6],
          f"attr_total_cost={inst.get('attr_total_cost')}")
    check("写回 __capability__ 元数据含版本/渠道/触发源/溯源",
          meta.get("capability_version") and meta.get("execution_channel") == "ui"
          and meta.get("trigger_source") and meta.get("trace"), str(meta)[:200])
    ctx["written_pid"] = pid


def ep04_01_02_04(ctx: dict) -> None:
    section("EP-04-01 null 显式计数 / EP-04-02 非法公式 / EP-04-04 失效版本")
    svc, store = ctx["svc"], ctx["store"]
    base = dict(store.get(ctx["product_ids"][0])["instance"])
    null_inst = dict(base)
    null_inst["attr_term_factor"] = None
    miss_inst = dict(base)
    miss_inst.pop("attr_term_factor", None)
    for rid, inst in (("prod-null", null_inst), ("prod-miss", miss_inst)):
        store.insert({"id": rid, "project_id": PID, "ontology_id": ONT,
                      "object_type": PRODUCT, "instance": inst, "source": {"kind": "upload"}})
    batch = svc.execute_batch(ctx["caps"]["cap_onsite_rate_d"]["id"],
                              instance_ids=[ctx["product_ids"][0], "prod-null", "prod-miss"],
                              dry_run=True)
    by_id = {x["instance_id"]: x for x in batch["items"]}
    check("EP-04-01 null 结果显式计数（batch.null）",
          batch["null"] == 1 and by_id["prod-null"]["value"] is None
          and by_id["prod-null"]["status"] == "success", str(batch)[:300])
    check("EP-04-01 缺失属性单实例错误、不中断批次",
          batch["error"] == 1 and batch["success"] == 2
          and by_id["prod-miss"]["error_code"] == "MISSING_INPUT_ATTR", str(batch)[:300])

    v = svc.validate_capability(ctx["draft"]["id"], "1/0")
    check("EP-04-02 定义期常量除零被拒（422 语义）",
          v["valid"] is False and v["error_code"] == "VALIDATION_ERROR"
          and any(i["code"] == "CONSTANT_DIV_ZERO" for i in v["issues"]), str(v)[:250])
    try:
        svc.create_capability({"name": "cap_self_cycle", "object_type": PRODUCT,
                               "expression": "ref('cap_self_cycle')"})
        check("EP-04-02 自引用（循环）创建期拒绝", False, "未抛错")
    except CapabilityApiError as e:
        check("EP-04-02 自引用（循环）创建期拒绝", e.code == "CYCLE" and e.http_status == 422,
              f"{e.code} {e.message}")

    bad_ver = svc.create_capability({
        "name": "cap_bad_version", "object_type": PRODUCT,
        "expression": "ref('cap_onsite_rate_d', '9.9.9')",
    })
    try:
        svc.publish_capability(bad_ver["id"])
        check("EP-04-04 引用不存在版本发布期拒绝", False, "未抛错")
    except CapabilityApiError as e:
        check("EP-04-04 引用不存在版本发布期拒绝（不静默指向最新版）",
              e.code == "REF_VERSION_NOT_FOUND" and e.http_status == 422, f"{e.code} {e.message}")

    divcap = svc.create_capability({
        "name": "cap_runtime_divzero", "object_type": PRODUCT,
        "expression": "1 / this.attr_zero", "output_attribute": "attr_calc_div",
    })
    svc.publish_capability(divcap["id"])
    for rid, z in (("prod-zero", 0), ("prod-two", 2)):
        inst = dict(base)
        inst["attr_zero"] = z
        store.insert({"id": rid, "project_id": PID, "ontology_id": ONT,
                      "object_type": PRODUCT, "instance": inst, "source": {"kind": "upload"}})
    div = svc.execute_batch(divcap["id"], instance_ids=["prod-zero", "prod-two"], dry_run=True)
    dz = {x["instance_id"]: x for x in div["items"]}
    check("EP-04-02 运行期除零单实例标记、批次不中断",
          div["total"] == 2 and div["success"] == 1 and div["error"] == 1
          and dz["prod-zero"]["error_code"] == "DIV_ZERO"
          and abs(dz["prod-two"]["value"] - 0.5) < TOL, str(div)[:300])


def tp04_03_04(ctx: dict) -> None:
    section("TC-04-03 删除被依赖 422 + draft 调用 403 / TC-04-04 版本锁")
    svc = ctx["svc"]
    try:
        svc.delete_capability(ctx["caps"]["cap_onsite_rate_d"]["id"])
        check("TC-04-03 删除被依赖公式 422 且列出依赖方", False, "未抛错")
    except CapabilityApiError as e:
        deps = (e.details or {}).get("dependents") or []
        check("TC-04-03 删除被依赖公式 422 且列出依赖方",
              e.code == "HAS_DEPENDENTS" and e.http_status == 422 and len(deps) >= 3,
              f"{e.code} deps={deps}")

    try:
        svc.execute(ctx["draft"]["id"], ctx["product_ids"][0], mcp_only=True, dry_run=True)
        check("TC-04-03 draft 能力经 MCP 通道拒绝（403）", False, "未抛错")
    except CapabilityApiError as e:
        check("TC-04-03 draft 能力经 MCP 通道拒绝（403）",
              e.code == "FORBIDDEN_DRAFT" and e.http_status == 403, f"{e.code} {e.message}")

    pid = ctx["product_ids"][3]
    d_id = ctx["caps"]["cap_onsite_rate_d"]["id"]
    d_before = svc.execute(d_id, pid, dry_run=True)["value"]
    before = svc.execute(ctx["caps"]["cap_labor_cost_e"]["id"], pid, dry_run=True)["value"]
    svc.update_capability(d_id, {"expression": EXPRS["cap_onsite_rate_d"] + " * 2.0"})
    d_after = svc.execute(d_id, pid, dry_run=True)["value"]
    after = svc.execute(ctx["caps"]["cap_labor_cost_e"]["id"], pid, dry_run=True)["value"]
    e_view = svc.get_capability(ctx["caps"]["cap_labor_cost_e"]["id"])
    versions = svc.list_versions(d_id)
    check("TC-04-04 被引用方升版（1.0.0 → 1.1.0）且旧版保留",
          svc.get_capability(d_id)["version"] == "1.1.0"
          and {v["version"] for v in versions} >= {"1.0.0", "1.1.0"},
          str([v["version"] for v in versions]))
    check("TC-04-04 引用方锁 v1.0.0，求值不自动跟随",
          before is not None and after is not None and abs(before - after) <= TOL
          and e_view["ref_locks"]["cap_onsite_rate_d"]["version"] == "1.0.0",
          f"before={before} after={after} lock={e_view['ref_locks']['cap_onsite_rate_d']['version']}")
    check("TC-04-04 新版本生效于直接执行（证明升版确有内容变化）",
          d_before is not None and d_after is not None
          and abs(d_after - 2 * d_before) <= TOL, f"d_before={d_before} d_after={d_after}")


def tp04_08_09(ctx: dict) -> None:
    section("TC-04-08 与 V2-21 分工边界 / TC-04-09 2297 实例规模性能基线")
    public = [n for n in dir(capabilities) if not n.startswith("_")]
    check("TC-04-08 公式引擎不含质量指标实现（分工边界）",
          not any("quality" in n.lower() or "metric" in n.lower() for n in public), str(public))
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        svc_res = ctx["svc"].execute_batch(ctx["caps"]["cap_total_cost_o"]["id"],
                                           instance_ids=ctx["product_ids"], dry_run=True)
        times.append((time.perf_counter() - t0) * 1000)
        if svc_res["total"] != 33:
            break
    med = statistics.median(times)
    ctx["perf_ms"] = round(med, 2)
    check("TC-04-09 3 次计时完成并记录基线（目标值未冻结，不作达标判定）",
          len(times) == 3 and med > 0, f"samples_ms={[round(t, 2) for t in times]}")
    print(f"  [BASELINE] 2297 实例库 / 33 产品行 × 6 公式（O 能力含 3 级 ref）"
          f" 3 次中位数 = {med:.2f} ms  samples={[round(t, 2) for t in times]}")


# --------------------------------------------------------------------------- #
def _pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def rest_smoke(ctx: dict, base: str) -> None:
    section("REST：能力 CRUD / 校验 / 版本 / 依赖 / 执行 信封与状态")
    P = {"project_id": PID}
    r = requests.get(f"{base}/api/capabilities", params=P, timeout=10)
    data = r.json().get("data") or {}
    check("GET /api/capabilities 统一信封（items/total/page/page_size）",
          r.status_code == 200 and r.json().get("success") is True
          and isinstance(data.get("items"), list) and len(data["items"]) >= 6
          and {"total", "page", "page_size"} <= set(data), r.text[:250])
    cap = ctx["caps"]["cap_onsite_rate_d"]
    rv = requests.post(f"{base}/api/capabilities/{cap['id']}/validate", params=P, timeout=10)
    vd = rv.json().get("data") or {}
    check("POST /validate 返回 refs/attr_refs",
          rv.status_code == 200 and vd.get("valid") is True
          and isinstance(vd.get("attr_refs"), list) and len(vd["attr_refs"]) >= 4, rv.text[:250])
    rvers = requests.get(f"{base}/api/capabilities/{cap['id']}/versions", params=P, timeout=10)
    check("GET /versions 保留旧版", rvers.status_code == 200
          and len((rvers.json().get("data") or {}).get("items", [])) >= 2, rvers.text[:200])
    rdeps = requests.get(f"{base}/api/capabilities/{cap['id']}/dependents", params=P, timeout=10)
    check("GET /dependents 列出引用方", rdeps.status_code == 200
          and len((rdeps.json().get("data") or {}).get("items", [])) >= 3, rdeps.text[:200])
    rd = requests.delete(f"{base}/api/capabilities/{cap['id']}", params=P, timeout=10)
    check("DELETE 被依赖能力 → 422 HAS_DEPENDENTS",
          rd.status_code == 422 and (rd.json().get("error") or {}).get("code") == "HAS_DEPENDENTS",
          rd.text[:250])
    re_ = requests.post(f"{base}/api/capabilities/{ctx['caps']['cap_cost_ratio_s']['id']}/execute",
                        params=P, json={"instance_id": ctx["product_ids"][3],
                                        "channel": "ui", "dry_run": True}, timeout=20)
    check("POST /execute dry_run 不写回且返回信封",
          re_.status_code == 200 and re_.json().get("success") is True
          and re_.json()["data"].get("dry_run") is True, re_.text[:250])
    rb = requests.post(f"{base}/api/capabilities/{ctx['caps']['cap_onsite_rate_d']['id']}/execute-batch",
                       params=P, json={"instance_ids": ctx["product_ids"],
                                       "dry_run": True, "channel": "api"}, timeout=30)
    bd = rb.json().get("data") or {}
    check("POST /execute-batch 返回 total/success/error/null 汇总",
          rb.status_code == 200 and bd.get("total") == 33
          and {"success", "error", "null"} <= set(bd), rb.text[:250])
    rc = requests.post(f"{base}/api/capabilities", params=P, json={
        "name": "cap_rest_tmp", "object_type": PRODUCT, "expression": "1 + 1",
    }, timeout=10)
    check("POST /api/capabilities 创建 201 信封",
          rc.status_code == 201 and rc.json().get("success") is True, rc.text[:250])
    if rc.status_code == 201:
        rid = rc.json()["data"]["id"]
        rdel = requests.delete(f"{base}/api/capabilities/{rid}", params=P, timeout=10)
        check("DELETE 无依赖能力 200 统一信封",
              rdel.status_code == 200 and rdel.json().get("success") is True
              and rdel.json()["data"].get("deleted") == rid, rdel.text[:250])


def mcp_calls(ctx: dict, base: str) -> None:
    section("TC-04-07 MCP capability_call 与 UI 同口径（真实 SSE SDK 客户端） / TC-04-03 draft 403")
    P = {"project_id": PID}
    r = requests.post(f"{base}/api/ontologies", params=P, json={
        "name": "M1B验收-维保成本域", "description": "M1-B 验收用最小本体",
        "modeling_type": "manual",
        "objects": [{"id": PRODUCT, "name": "维保服务产品", "business_definition": "维保服务产品",
                     "stable_key": "attr_product_code",
                     "attributes": [{"id": "attr_product_code", "name": "产品编码",
                                     "data_type": "string", "required": True, "constraints": {}}],
                     "relations": []}], "constraints": []}, timeout=15)
    check("最小本体创建", r.status_code == 201, r.text[:250])
    if r.status_code != 201:
        return
    oid = r.json()["data"]["id"]
    rp = requests.post(f"{base}/api/mcp/server/publish", params={**P, "ontology_id": oid}, timeout=15)
    check("发布项目 MCP Server", rp.status_code == 200
          and rp.json()["data"]["published"] is True, rp.text[:250])
    token = requests.post(f"{base}/api/mcp/server/token", params=P, timeout=15).json()["data"]["token"]

    from mcp import ClientSession
    from mcp.client.sse import sse_client

    pid = ctx["product_ids"][5]
    ui_value = ctx["svc"].execute(ctx["caps"]["cap_onsite_rate_d"]["id"], pid,
                                  channel="ui", dry_run=True)["value"]

    async def _run() -> dict:
        out: dict = {}
        async with sse_client(f"{base}/mcp/{PID}",
                              headers={"Authorization": f"Bearer {token}"}) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                tools = await s.list_tools()
                out["tools"] = sorted(t.name for t in tools.tools)
                out["listing"] = await s.call_tool("capability_call", {"capability": ""})
                out["ok"] = await s.call_tool("capability_call", {
                    "capability": "cap_onsite_rate_d", "instance_id": pid, "dry_run": True})
                out["total"] = await s.call_tool("capability_call", {
                    "capability": "cap_total_cost_o", "instance_id": pid, "dry_run": True})
                out["draft"] = await s.call_tool("capability_call", {
                    "capability": "cap_draft_forbidden", "instance_id": pid, "dry_run": True})
        return out

    out = asyncio.run(_run())
    check("TOOLS_CONTRACT 6 工具含 capability_call",
          "capability_call" in out["tools"] and len(out["tools"]) == 6, str(out["tools"]))

    def text(res) -> str:
        return res.content[0].text if res.content else ""

    check("MCP 空能力名返回已发布清单",
          not out["listing"].is_error and json.loads(text(out["listing"])).get("published") is True,
          text(out["listing"])[:250])
    ok_res = out["ok"]
    mcp_val = json.loads(text(ok_res)).get("value") if not ok_res.is_error else None
    check("TC-04-07 MCP 与 UI 求值同口径",
          not ok_res.is_error and mcp_val is not None and abs(float(mcp_val) - float(ui_value)) <= TOL,
          f"mcp={mcp_val} ui={ui_value} {text(ok_res)[:150]}")
    total_res = out["total"]
    total_json = json.loads(text(total_res)) if not total_res.is_error else {}
    check("MCP 结果含四要素溯源（版本/实例/lookup 命中原值）",
          bool(total_json.get("capability_version")) and total_json.get("instance_id") == pid
          and bool((total_json.get("trace") or {}).get("lookups")), text(total_res)[:250])
    draft_res = out["draft"]
    check("TC-04-03 draft 能力 MCP 调用 isError + FORBIDDEN_DRAFT + 403",
          draft_res.is_error and "FORBIDDEN_DRAFT" in text(draft_res)
          and "403" in text(draft_res), text(draft_res)[:250])
    inst = ctx["store"].get(pid).get("instance", {})
    check("MCP dry_run 不写回实例", "attr_calc_d" not in inst and "__capability__" not in inst,
          str(list(inst)[-8:]))


def run_server(ctx: dict, no_mcp: bool) -> None:
    import uvicorn
    from main import app

    port = _pick_free_port()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True, name="m1b-uvicorn")
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
        rest_smoke(ctx, base)
        if no_mcp:
            skip("TC-04-07 / TC-04-03 MCP 段", "--no-mcp 指定跳过")
        else:
            mcp_calls(ctx, base)
    finally:
        srv.should_exit = True
        th.join(timeout=5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mcp", action="store_true", help="跳过 MCP SSE 段")
    args = ap.parse_args()
    print(f"DATA_DIR 隔离目录: {_TMP_DATA}")
    ctx = setup()
    tp04_01(ctx)
    tp04_02_05(ctx)
    ep04_01_02_04(ctx)
    tp04_03_04(ctx)
    tp04_08_09(ctx)
    run_server(ctx, args.no_mcp)
    skip("TC-04-06 / EP-04-03 源连接型渠道（批量联邦读 / 派生图 / 成本提示）",
         "偏差决策 D2：依赖 V2-02 实例数据模型，随 M4-A 交付")
    print(f"\n===== 结果: PASS={PASS} FAIL={FAIL} SKIP={len(SKIPS)} =====")
    if ctx["perf_ms"] is not None:
        print(f"性能基线（实测，未冻结目标值）: {ctx['perf_ms']} ms")
    if FAILS:
        print("失败项:")
        for f in FAILS:
            print(" -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
