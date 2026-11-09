# -*- coding: utf-8 -*-
"""VS2-00 测试基建验收（mock 五能力 + 数据集完整性）。

用法：
    python scripts/verify_vs2_00_mock.py          # mock 五能力 + 数据集
    python scripts/verify_vs2_00_mock.py --mocks  # 仅 mock
    python scripts/verify_vs2_00_mock.py --data   # 仅数据集（需先跑生成器）

数据集不存在时先执行：python scripts/generate_v2_datasets.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from mocks import make_all  # noqa: E402

DATA_DIR = os.path.join(REPO, "data", "datasets", "v2")
RUNTIME_PROJECT = "5cd5ccc06b604ed7829f99983f52e963"
RUNTIME_ROOT = os.path.join(REPO, "backend", "data", "projects")
ADMIN = {"X-Mock-Admin": "mock-admin-2026"}
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# A. mock 五能力
# ---------------------------------------------------------------------------

def base(sysm, path: str) -> str:
    return f"http://127.0.0.1:{sysm.port}{path}"


def auth(tok: str = "tok-full") -> dict:
    return {"Authorization": f"Bearer {tok}"}


def run_mock_tests() -> None:
    print("== A. mock 五能力 ==")
    systems = make_all()
    crm, plm, rsr = systems["crm"], systems["plm"], systems["reservoir"]
    for s in systems.values():
        s.start()
    try:
        # ---- A0 基线连通与调用形态 ----
        h = requests.get(base(crm, "/health"), timeout=3).json()
        check("A0 health", h.get("status") == "ok" and h.get("system") == "crm")
        sch = requests.get(base(crm, "/schema"), timeout=3).json()
        check("A0 schema 无值", "PN" in [f["name"] for f in sch["fields"]]
              and all("records" not in json.dumps(f) for f in sch["fields"])
              and sch["key_field"] == "PN")
        r = requests.post(base(crm, "/read"), json={"tool": "read", "params": {}},
                          headers=auth(), timeout=5).json()
        check("A0 read 全量", r["count"] == 484, f"count={r['count']}")
        r2 = requests.post(base(crm, "/read"), json={"tool": "read", "params": {"limit": 5}},
                           headers=auth(), timeout=5).json()
        check("A0 read limit", r2["count"] == 5)
        r3 = requests.post(base(crm, "/read"), json={"tool": "read", "params": {"PN": "PN-100000"}},
                           headers=auth(), timeout=5).json()
        check("A0 read 单键", r3["count"] <= 1)
        r4 = requests.post(base(crm, "/read"), json={"tool": "read", "params": {}},
                           headers=auth("tok-unknown"), timeout=5)
        check("A0 未知 token 401", r4.status_code == 401)
        # 三系统字段名各异
        names = {
            "crm": requests.get(base(crm, "/schema"), timeout=3).json()["key_field"],
            "plm": requests.get(base(plm, "/schema"), timeout=3).json()["key_field"],
            "reservoir": requests.get(base(rsr, "/schema"), timeout=3).json()["key_field"],
        }
        check("A0 字段名各异", names == {"crm": "PN", "plm": "PN码",
                                         "reservoir": "PN_Num"}, str(names))

        # ---- A1 时延注入 ----
        requests.post(base(crm, "/_admin/config"), json={"delay_ms": 500},
                      headers=ADMIN, timeout=3)
        t0 = time.time()
        resp = requests.post(base(crm, "/read"), json={"tool": "read", "params": {"limit": 1}},
                             headers=auth(), timeout=5)
        elapsed = time.time() - t0
        check("A1 时延注入 >=500ms", resp.status_code == 200 and elapsed >= 0.45,
              f"elapsed={elapsed:.2f}s")
        requests.post(base(crm, "/_admin/config"), json={"delay_ms": 0},
                      headers=ADMIN, timeout=3)

        # ---- A2 schema 热变 ----
        st0 = requests.get(base(crm, "/_admin/state"), headers=ADMIN, timeout=3).json()
        resp = requests.post(base(crm, "/_admin/schema"),
                             json={"action": "rename", "field": "product_name",
                                   "new_field": "product_name_new"},
                             headers=ADMIN, timeout=3)
        check("A2 rename", resp.status_code == 200)
        sch = requests.get(base(crm, "/schema"), timeout=3).json()
        field_names = [f["name"] for f in sch["fields"]]
        check("A2 rename 生效", "product_name_new" in field_names
              and "product_name" not in field_names)
        r = requests.post(base(crm, "/read"), json={"tool": "read", "params": {"limit": 1}},
                          headers=auth(), timeout=5).json()
        check("A2 rename 数据跟随", "product_name_new" in r["records"][0])
        requests.post(base(crm, "/_admin/schema"),
                      json={"action": "add", "field": "new_field", "type": "string"},
                      headers=ADMIN, timeout=3)
        requests.post(base(crm, "/_admin/schema"),
                      json={"action": "change_type", "field": "contract_amount",
                            "type": "integer"},
                      headers=ADMIN, timeout=3)
        requests.post(base(crm, "/_admin/schema"),
                      json={"action": "remove", "field": "new_field"},
                      headers=ADMIN, timeout=3)
        st1 = requests.get(base(crm, "/_admin/state"), headers=ADMIN, timeout=3).json()
        check("A2 schema_version 递增",
              st1["schema_version"] == st0["schema_version"] + 4,
              f"{st0['schema_version']} -> {st1['schema_version']}")
        evs = st1["events_tail"]
        check("A2 schema_change 入 CDC 日志",
              any(e["op"] == "schema_change" for e in evs))
        # 恢复
        requests.post(base(crm, "/_admin/schema"),
                      json={"action": "rename", "field": "product_name_new",
                            "new_field": "product_name"},
                      headers=ADMIN, timeout=3)
        requests.post(base(crm, "/_admin/schema"),
                      json={"action": "change_type", "field": "contract_amount",
                            "type": "decimal"},
                      headers=ADMIN, timeout=3)

        # ---- A3 权限切换 ----
        total = requests.post(base(crm, "/read"), json={"tool": "read", "params": {}},
                              headers=auth("tok-full"), timeout=5).json()["count"]
        even = requests.post(base(crm, "/read"), json={"tool": "read", "params": {}},
                             headers=auth("tok-even"), timeout=5).json()
        odd = requests.post(base(crm, "/read"), json={"tool": "read", "params": {}},
                            headers=auth("tok-odd"), timeout=5).json()
        empty = requests.post(base(crm, "/read"), json={"tool": "read", "params": {}},
                              headers=auth("tok-empty"), timeout=5).json()
        ev_keys = {r["PN"] for r in even["records"]}
        od_keys = {r["PN"] for r in odd["records"]}
        check("A3 子集划分无漏无重",
              even["count"] + odd["count"] == total and not (ev_keys & od_keys),
              f"total={total} even={even['count']} odd={odd['count']}")
        check("A3 空权限 0 行", empty["count"] == 0)
        sample = sorted(ev_keys)[:3]
        requests.post(base(crm, "/_admin/identity"),
                      json={"action": "set", "token": "tok-x", "scope": "subset",
                            "keys": sample},
                      headers=ADMIN, timeout=3)
        rx = requests.post(base(crm, "/read"), json={"tool": "read", "params": {}},
                           headers=auth("tok-x"), timeout=5).json()
        check("A3 热切换即时生效", rx["count"] == 3
              and {r["PN"] for r in rx["records"]} == set(sample))
        requests.post(base(crm, "/_admin/identity"),
                      json={"action": "remove", "token": "tok-x"},
                      headers=ADMIN, timeout=3)
        rx2 = requests.post(base(crm, "/read"), json={"tool": "read", "params": {}},
                            headers=auth("tok-x"), timeout=5)
        check("A3 移除 token 后 401", rx2.status_code == 401)

        # ---- A4 断流与故障注入 ----
        requests.post(base(crm, "/_admin/config"), json={"outage": True},
                      headers=ADMIN, timeout=3)
        try:
            requests.post(base(crm, "/read"), json={"tool": "read", "params": {}},
                          headers=auth(), timeout=3)
            check("A4 断流无响应", False, "未抛异常")
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout):
            check("A4 断流无响应", True)
        requests.post(base(crm, "/_admin/config"), json={"outage": False},
                      headers=ADMIN, timeout=3)
        rec = requests.post(base(crm, "/read"), json={"tool": "read", "params": {"limit": 1}},
                            headers=auth(), timeout=5)
        check("A4 断流恢复", rec.status_code == 200)
        requests.post(base(crm, "/_admin/config"),
                      json={"fail_rate": 1.0, "error_status": 503},
                      headers=ADMIN, timeout=3)
        fr = requests.post(base(crm, "/read"), json={"tool": "read", "params": {}},
                           headers=auth(), timeout=5)
        check("A4 故障注入 503", fr.status_code == 503
              and fr.json().get("error") == "INJECTED_FAILURE")
        requests.post(base(crm, "/_admin/config"),
                      json={"fail_rate": 0.0, "error_status": 500},
                      headers=ADMIN, timeout=3)

        # ---- A5 CDC 通道切换与序列一致性 ----
        # 固定变更序列：2 create + 1 update + 1 remove + 1 create
        requests.post(base(crm, "/_admin/cdc"), json={"action": "reset"},
                      headers=ADMIN, timeout=3)
        seq_payload = [
            ("upsert", {"records": [{"PN": "PX-0001", "product_name": "a"},
                                    {"PN": "PX-0002", "product_name": "b"}]}),
            ("upsert", {"records": [{"PN": "PX-0001", "product_name": "a2"}]}),
            ("remove", {"keys": ["PX-0002"]}),
            ("upsert", {"records": [{"PN": "PX-0003", "product_name": "c"}]}),
        ]
        for action, body in seq_payload:
            requests.post(base(crm, "/_admin/data"),
                          json={"action": action, **body}, headers=ADMIN, timeout=3)

        # 通道 none -> 409
        requests.post(base(crm, "/_admin/config"), json={"cdc_channel": "none"},
                      headers=ADMIN, timeout=3)
        for ep in ("/cdc/cursor", "/cdc/mq", "/cdc/subscribe"):
            r9 = requests.post(base(crm, ep), json={}, headers=auth(), timeout=3)
            check(f"A5 channel=none {ep} 409",
                  r9.status_code == 409 and r9.json().get("error") == "CDC_NOT_SUPPORTED")

        # cursor 通道：完整重放 + 幂等分段
        requests.post(base(crm, "/_admin/config"), json={"cdc_channel": "cursor"},
                      headers=ADMIN, timeout=3)
        c_all = requests.post(base(crm, "/cdc/cursor"),
                              json={"since": 0, "limit": 100},
                              headers=auth(), timeout=3).json()
        seq = [(e["op"], e["key"]) for e in c_all["events"]]
        expected = [("create", "PX-0001"), ("create", "PX-0002"),
                    ("update", "PX-0001"), ("remove", "PX-0002"),
                    ("create", "PX-0003")]
        check("A5 cursor 完整序列 5 事件", seq == expected, str(seq))
        nxt = c_all["next_cursor"]
        c_more = requests.post(base(crm, "/cdc/cursor"),
                               json={"since": nxt, "limit": 100},
                               headers=auth(), timeout=3).json()
        check("A5 cursor 无重放", c_more["events"] == [])
        p1 = requests.post(base(crm, "/cdc/cursor"), json={"since": 0, "limit": 2},
                           headers=auth(), timeout=3).json()
        p2 = requests.post(base(crm, "/cdc/cursor"),
                           json={"since": p1["next_cursor"], "limit": 100},
                           headers=auth(), timeout=3).json()
        check("A5 cursor 分段不重不漏",
              [e["lsn"] for e in p1["events"]] + [e["lsn"] for e in p2["events"]]
              == [e["lsn"] for e in c_all["events"]])

        # mq 通道：同一序列
        requests.post(base(crm, "/_admin/config"), json={"cdc_channel": "mq"},
                      headers=ADMIN, timeout=3)
        requests.post(base(crm, "/_admin/cdc"), json={"action": "reset_offset"},
                      headers=ADMIN, timeout=3)
        m1 = requests.post(base(crm, "/cdc/mq"), json={"max": 100},
                           headers=auth(), timeout=3).json()
        m2 = requests.post(base(crm, "/cdc/mq"), json={"max": 100},
                           headers=auth(), timeout=3).json()
        check("A5 mq 同序列", [(e["op"], e["key"]) for e in m1["events"]] == seq
              and m2["events"] == [])

        # binlog 通道（长轮询）：空等待 + 有事件
        requests.post(base(crm, "/_admin/config"), json={"cdc_channel": "binlog"},
                      headers=ADMIN, timeout=3)
        lsn_now = requests.get(base(crm, "/_admin/state"), headers=ADMIN,
                               timeout=3).json()["lsn"]
        t0 = time.time()
        s0 = requests.post(base(crm, "/cdc/subscribe"),
                           json={"since": lsn_now, "wait_ms": 500},
                           headers=auth(), timeout=5).json()
        waited = time.time() - t0
        check("A5 binlog 空等待", s0["events"] == [] and waited >= 0.4,
              f"waited={waited:.2f}s")
        requests.post(base(crm, "/_admin/data"),
                      json={"action": "upsert",
                            "records": [{"PN": "PX-0009", "product_name": "z"}]},
                      headers=ADMIN, timeout=3)
        s1 = requests.post(base(crm, "/cdc/subscribe"),
                           json={"since": lsn_now, "wait_ms": 2000},
                           headers=auth(), timeout=5).json()
        check("A5 binlog 收到新事件",
              len(s1["events"]) == 1 and s1["events"][0]["key"] == "PX-0009")

        # 清理回写产生的记录，恢复基线
        requests.post(base(crm, "/_admin/data"), json={"action": "reset"},
                      headers=ADMIN, timeout=3)

        # ---- A6 回写流水（审计对账基础）----
        requests.post(base(crm, "/write"),
                      json={"tool": "write",
                            "params": {"PN": "PW-0001", "product_name": "w",
                                       "region": "华东"}},
                      headers=auth("tok-full"), timeout=3)
        requests.post(base(crm, "/write"),
                      json={"tool": "write",
                            "params": {"PN": "PW-0001", "product_name": "w2"}},
                      headers=auth("tok-full"), timeout=3)
        st = requests.get(base(crm, "/_admin/state"), headers=ADMIN, timeout=3).json()
        wc = st["write_calls"]
        check("A6 回写流水记录", len(wc) == 2 and wc[0]["token"] == "tok-full"
              and wc[0]["payload"]["PN"] == "PW-0001")
        requests.post(base(crm, "/_admin/data"), json={"action": "reset"},
                      headers=ADMIN, timeout=3)
    finally:
        for s in systems.values():
            s.stop()


# ---------------------------------------------------------------------------
# B. 数据集完整性
# ---------------------------------------------------------------------------

def run_data_tests() -> None:
    print("== B. 数据集完整性 ==")
    import openpyxl
    import rdflib

    def load_xlsx(rel: str):
        return openpyxl.load_workbook(os.path.join(DATA_DIR, rel), read_only=True)

    if not os.path.isdir(DATA_DIR):
        check("B0 数据集目录", False, "先运行 scripts/generate_v2_datasets.py")
        return

    # ---- B1 维保 2297 ----
    wb = load_xlsx("maintenance_cost_2297.xlsx")
    counts = {}
    for name in wb.sheetnames:
        rows = wb[name].iter_rows(values_only=True)
        next(rows)  # 表头
        counts[name] = sum(1 for _ in rows)
    total = sum(counts.values())
    check("B1 2297 条", total == 2297, f"total={total} sheets={len(counts)}")
    # 与运行期 JSONL 逐对象一致
    inst_dir = os.path.join(RUNTIME_ROOT, RUNTIME_PROJECT, "instances")
    ok_all = len(counts) == 17
    for obj, n in counts.items():
        f = os.path.join(inst_dir, obj + ".jsonl")
        if not os.path.exists(f):
            ok_all = False
            continue
        n_jsonl = sum(1 for _ in open(f, encoding="utf-8"))
        if n_jsonl != n:
            ok_all = False
            print(f"    不一致: {obj} xlsx={n} jsonl={n_jsonl}")
    check("B1 与运行期实例逐对象一致", ok_all)
    g = rdflib.Graph()
    g.parse(os.path.join(DATA_DIR, "maintenance_cost_ontology.ttl"),
            format="turtle")
    n_cls = len([s for s in g.subjects(rdflib.RDF.type, rdflib.OWL.Class)])
    check("B1 本体 TTL 可解析(17 类)", n_cls == 17, f"classes={n_cls}")
    ref = os.path.join(REPO, "成本清单_维保服务产品_复算版.xlsx")
    check("B1 Excel 参照在位", os.path.exists(ref))
    if os.path.exists(ref):
        wbr = openpyxl.load_workbook(ref, read_only=True)
        ws = wbr["成本清单33行"]
        n_rows = sum(1 for _ in ws.iter_rows(values_only=True)) - 1
        check("B1 参照 33 行", n_rows == 33, f"rows={n_rows}")

    # ---- B2 脏数据批 ----
    man = json.load(open(os.path.join(DATA_DIR, "dirty_manifest.json"),
                         encoding="utf-8"))
    wb2 = load_xlsx("dirty_batch.xlsx")
    ws2 = wb2.active
    rows = list(ws2.iter_rows(values_only=True))
    header, data = rows[0], rows[1:]
    check("B2 200 行", len(data) == 200, f"rows={len(data)}")
    idx = {c: i for i, c in enumerate(header)}
    d = man["dirt_row_numbers"]
    missing_ok = all(data[rn - 1][idx["item_code"]] is None
                     for rn in d["missing_key"])
    dup_codes = {}
    for row in data:
        c = row[idx["item_code"]]
        if c is not None:
            dup_codes[c] = dup_codes.get(c, 0) + 1
    dup_ok = all(dup_codes[data[rn - 1][idx["item_code"]]] >= 2
                 for rn in d["duplicate_key"])
    enum_ok = all(data[rn - 1][idx["category"]] not in ("A", "B", "C")
                  for rn in d["enum_violation"])
    range_ok = all(not (0 <= data[rn - 1][idx["quantity"]] <= 1000000)
                   for rn in d["out_of_range"])
    codes = {row[idx["item_code"]] for row in data if row[idx["item_code"]]}
    dang_ok = all(data[rn - 1][idx["parent_code"]] is not None
                  and data[rn - 1][idx["parent_code"]] not in codes
                  for rn in d["dangling_relation"])
    check("B2 缺键 10", missing_ok and len(d["missing_key"]) == 10)
    check("B2 重复键 8", dup_ok and len(d["duplicate_key"]) == 8)
    check("B2 越枚举 10", enum_ok and len(d["enum_violation"]) == 10)
    check("B2 越界 10", range_ok and len(d["out_of_range"]) == 10)
    check("B2 悬空关系 5", dang_ok and len(d["dangling_relation"]) == 5)
    g2 = rdflib.Graph()
    g2.parse(os.path.join(DATA_DIR, "dirty_test_ontology.ttl"), format="turtle")
    check("B2 脏测本体可解析", len(g2) > 0)

    # ---- B3 供应商对齐集 ----
    man3 = json.load(open(os.path.join(DATA_DIR,
                                       "supplier_alignment_manifest.json"),
                          encoding="utf-8"))
    wba = load_xlsx("supplier_alignment_a_instances.xlsx")
    wbb = load_xlsx("supplier_alignment_b_instances.xlsx")
    ra = list(wba.active.iter_rows(values_only=True))
    rb = list(wbb.active.iter_rows(values_only=True))
    check("B3 A=1200 B=1380", len(ra) - 1 == 1200 and len(rb) - 1 == 1380,
          f"A={len(ra) - 1} B={len(rb) - 1}")
    ha = {c: i for i, c in enumerate(ra[0])}
    hb = {c: i for i, c in enumerate(rb[0])}
    a = {r[ha["sup_code"]]: r for r in ra[1:]}
    b = {r[hb["sup_code"]]: r for r in rb[1:]}
    overlap = set(a) & set(b)
    check("B3 键重叠 1000", len(overlap) == 1000, f"overlap={len(overlap)}")
    n_conf = n_sem = n_cs = 0
    for k in overlap:
        ra_, rb_ = a[k], b[k]
        if ra_[ha["reg_capital"]] != rb_[hb["reg_capital"]] or \
           ra_[ha["city"]] != rb_[hb["city"]]:
            n_conf += 1
        if ra_[ha["sup_name"]] != rb_[hb["sup_name"]]:
            n_sem += 1
        if ra_[ha["source_ref"]] == rb_[hb["source_ref"]] == "shared-erp":
            n_cs += 1
    check("B3 值冲突 120", n_conf == 120, f"conflicts={n_conf}")
    check("B3 语义相似 40", n_sem == 40, f"semantic={n_sem}")
    check("B3 共同来源 150", n_cs == 150, f"common_src={n_cs}")

    # ---- B4 基准规模图 ----
    man4 = json.load(open(os.path.join(DATA_DIR, "benchmark_manifest.json"),
                          encoding="utf-8"))
    g4 = rdflib.Graph()
    t0 = time.time()
    g4.parse(os.path.join(DATA_DIR, "benchmark_graph.ttl"), format="turtle")
    n_cls4 = len([s for s in g4.subjects(rdflib.RDF.type, rdflib.OWL.Class)])
    n_prop4 = len([s for s in g4.subjects(rdflib.RDF.type,
                                          rdflib.OWL.DatatypeProperty)])
    n_inst4 = len([s for s, _, o in g4.triples((None, rdflib.RDF.type, None))
                   if str(o).startswith("urn") or "bcls" in str(o)])
    check("B4 类 2000 / 属性 6000", n_cls4 == 2000 and n_prop4 == 6000,
          f"classes={n_cls4} props={n_prop4}")
    check("B4 实例 8000", n_inst4 == 8000, f"instances={n_inst4}")
    check("B4 三元组数万", len(g4) >= 30000, f"triples={len(g4)} "
          f"parse={time.time() - t0:.1f}s")


# ---------------------------------------------------------------------------

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--mocks", action="store_true")
    ap.add_argument("--data", action="store_true")
    args = ap.parse_args()
    do_mocks = args.mocks or not args.data
    do_data = args.data or not args.mocks

    if do_mocks:
        run_mock_tests()
    if do_data:
        run_data_tests()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"\n==== VS2-00 验收: PASS={passed} FAIL={len(failed)} / {len(RESULTS)} ====")
    for n, d in failed:
        print(f"  FAIL: {n} | {d}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
