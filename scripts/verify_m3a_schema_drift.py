#!/usr/bin/env python3
"""V2-11（M3-A）schema 漂移验收脚本：TP-11-01..11 + EP-11-01..03。

运行：cd backend && python ..\\scripts\\verify_m3a_schema_drift.py
     （或项目根：python scripts/verify_m3a_schema_drift.py，脚本自动补 sys.path）

设计：
- 独立临时 DATA_DIR（backend/data/_m3a_verify），运行前后清理；
- 关闭登录鉴权（ONTOLOGY_AUTH_ENABLED=false）；
- 关闭后台周期探测线程（ONTOLOGY_SCHEMA_DRIFT_PROBE_ENABLED=false），
  周期通道（source=periodic）以直接调用 check_connection 验证——与周期线程同一管道；
- stale 阈值乘数调极小（0.000001），使「超阈值未检查」可确定性断言；
- 进程内 ControllableMockSystem 充当外部源系统：GET /schema（探测）+
  POST /read（数据，需 Bearer），/_admin/schema 支持 schema 热变
  （rename/add/remove/change_type）；
- FastAPI TestClient（with 模式触发 startup）。
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

TMP_DATA = BACKEND / "data" / "_m3a_verify"
if TMP_DATA.exists():
    shutil.rmtree(TMP_DATA, ignore_errors=True)

os.environ["DATA_DIR"] = str(TMP_DATA)
os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
os.environ["ONTOLOGY_SCHEMA_DRIFT_PROBE_ENABLED"] = "false"
os.environ["ONTOLOGY_SCHEMA_DRIFT_PROBE_INTERVAL_SECONDS"] = "300"
os.environ["ONTOLOGY_SCHEMA_DRIFT_STALE_MULTIPLIER"] = "0.000001"
os.environ["M3A_CRM_TOKEN"] = "tok-full"

import config as _cfg  # noqa: E402

_cfg.get_settings.cache_clear()

import main as main_app  # noqa: E402
from core.project_store import ProjectStore  # noqa: E402
from mocks.v2_external import ControllableMockSystem  # noqa: E402
from schemas.project import ProjectCreate  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def admin_schema(port: int, action: str, **kw) -> None:
    import json
    body = json.dumps({"action": action, **kw}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/_admin/schema", data=body,
        headers={"Content-Type": "application/json", "X-Mock-Admin": "adm"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


def _dead_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    RECORDS = [
        {"customer_id": "C-001", "contact_id": "K-001", "name": "Alfa Co", "credit_limit": 1000, "region": "East"},
        {"customer_id": "C-002", "contact_id": "K-002", "name": "Beta Co", "credit_limit": 2000, "region": "West"},
        {"customer_id": "C-003", "contact_id": "K-003", "name": "Gamma Co", "credit_limit": 3000, "region": "North"},
        {"customer_id": "C-004", "contact_id": "K-004", "name": "Delta Co", "credit_limit": 4000, "region": "South"},
        {"customer_id": "C-005", "contact_id": "K-005", "name": "Eps Co", "credit_limit": 5000, "region": "East"},
    ]
    mock = ControllableMockSystem(
        name="crm", key_field="customer_id",
        fields=[("customer_id", "string"), ("contact_id", "string"), ("name", "string"),
                ("credit_limit", "number"), ("region", "string")],
        records=RECORDS, tokens={"tok-full": {"scope": "all", "keys": []}}, admin_key="adm")
    port = mock.start(port=0)
    endpoint = f"http://127.0.0.1:{port}/read"
    print(f"[setup] mock crm @ {endpoint}")

    try:
        from fastapi.testclient import TestClient
        with TestClient(main_app.app) as tc:
            # ---- 项目 / 本体 / 连接 / 词条 ----
            ps = ProjectStore()
            project = ps.create(ProjectCreate(name="M3A 验收", department="验证部"))
            pid = project["id"]
            print(f"[setup] project {pid[:8]}")

            ont = tc.post("/api/ontologies?project_id=" + pid, json={
                "name": "M3A 验收本体",
                "objects": [
                    {"id": "cust", "name": "客户", "stable_key": "cust_key",
                     "attributes": [
                         {"id": "cust_key", "name": "客户键", "data_type": "string", "required": True},
                         {"id": "cust_name", "name": "客户名", "data_type": "string"},
                         {"id": "credit_limit", "name": "信用额度", "data_type": "float"},
                         {"id": "region", "name": "区域", "data_type": "string"},
                         {"id": "contact_ref", "name": "联系键引用", "data_type": "string"}]},
                    {"id": "contact", "name": "联系人", "stable_key": "contact_key",
                     "attributes": [
                         {"id": "contact_key", "name": "联系键", "data_type": "string", "required": True},
                         {"id": "contact_name", "name": "联系人名", "data_type": "string"}]},
                ]}).json()
            oid = (ont.get("data") or {}).get("id")
            check("setup: 本体创建", bool(oid), str(ont)[:200])

            def conn(name: str, object_type: str, field_map: dict, ep: str) -> str:
                j = tc.post("/api/mcp/connections?project_id=" + pid, json={
                    "display_name": name, "transport": "streamable_http", "endpoint": ep,
                    "auth": {"type": "bearer_token", "bearer_token_ref": "${M3A_CRM_TOKEN}"},
                    "maps_to_ontology": {"ontology_id": oid, "object_type": object_type,
                                         "field_map": field_map}}).json()
                return (j.get("data") or {}).get("connection_id") or ""

            A = conn("crm-main", "cust",
                     {"customer_id": "cust_key", "contact_id": "contact_ref",
                      "name": "cust_name", "credit_limit": "credit_limit", "region": "region"},
                     endpoint)
            B = conn("crm-contact", "contact",
                     {"contact_id": "contact_key", "name": "contact_name"}, endpoint)
            C = conn("crm-down", "cust",
                     {"customer_id": "cust_key", "name": "cust_name",
                      "credit_limit": "credit_limit", "region": "region",
                      "contact_id": "contact_ref"},
                     f"http://127.0.0.1:{_dead_port()}/read")
            check("setup: 连接 A/B/C 创建", bool(A and B and C), f"A={A[:8]} B={B[:8]} C={C[:8]}")

            term = tc.post("/api/dictionary/terms?project_id=" + pid, json={
                "canonical": "customer", "object_type": "cust", "property_id": "cust_key",
                "system_bindings": {A: "customer_id"}}).json()
            check("setup: 字典词条创建", term.get("success"), str(term)[:200])

            def conn_rec(cid: str) -> dict:
                j = tc.get(f"/api/mcp/connections/{cid}?project_id={pid}").json()
                return (j.get("data") or {})

            def health(cid: str) -> dict:
                j = tc.get(f"/api/mcp/connections/{cid}/health?project_id={pid}").json()
                return (j.get("data") or {})

            def drifts(cid: str, status: str) -> dict:
                j = tc.get(f"/api/mcp/connections/{cid}/drifts?status={status}&project_id={pid}").json()
                return (j.get("data") or {})

            def scheck(cid: str) -> dict:
                j = tc.post(f"/api/mcp/connections/{cid}/schema-check?project_id={pid}", json={}).json()
                return (j.get("data") or {}), j.get("success"), (j.get("error") or {}).get("code", "")

            def resolve(cid: str, did: str):
                return tc.post(f"/api/mcp/connections/{cid}/drifts/{did}/resolve?project_id={pid}", json={})

            # ================= TP-11-01 手动通道 + EP-11-01 探测失败≠漂移 =================
            print("\n[TP-11-01 / EP-11-01] 手动检查 schema；探测失败不判定漂移")
            d, ok, code = scheck(C)
            check("TP-11-01: 不可达源 schema-check → probe_failed（端点不可达，非漂移）",
                  d.get("status") == "probe_failed" and "不可达" in (d.get("error") or ""),
                  f"d={d}")
            check("TP-11-01: 探测失败不产生漂移记录", drifts(C, "all").get("total") == 0,
                  str(drifts(C, "all"))[:200])
            check("TP-11-01: 探测失败不停快照更新", conn_rec(C).get("snapshot_auto_update") != "stopped")
            d, ok, code = scheck(A)
            check("EP-11-01: 正常源手动检查 → 无漂移（severity=None, items=[]）",
                  d.get("status") == "ok" and d.get("severity") is None and d.get("items") == [],
                  f"d={d}")
            check("EP-11-01: 检查后健康位更新（never_checked=false）",
                  health(A).get("never_checked") is False and health(A).get("last_schema_check_at"),
                  str(health(A)))

            # ================= TP-11-02 键属性漂移停更 + TP-11-04 不误报 =================
            print("\n[TP-11-02 / TP-11-04] 键字段重命名 → key 漂移停更；同源其他连接不误报")
            admin_schema(port, "rename", field="customer_id", new_field="cust_no")
            d, ok, code = scheck(A)
            kinds = {(i["kind"], i["field"]) for i in d.get("items", [])}
            check("TP-11-02: 键重命名 → key 漂移（key_missing customer_id + new_unmapped cust_no）",
                  d.get("severity") == "key" and ("key_missing", "customer_id") in kinds
                  and ("new_unmapped", "cust_no") in kinds, f"d={d}")
            check("TP-11-02: 快照自动更新已停止",
                  conn_rec(A).get("snapshot_auto_update") == "stopped")
            r = tc.post(f"/api/mcp/connections/{A}/read?project_id={pid}", json={})
            check("TP-11-02: 停更期间读取→候选被拒 409 SCHEMA_DRIFT_STOPPED",
                  r.status_code == 409 and r.json().get("error", {}).get("code") == "SCHEMA_DRIFT_STOPPED",
                  f"status={r.status_code} body={str(r.json())[:200]}")
            lst = (tc.get(f"/api/mcp/connections?project_id={pid}").json().get("data") or {}).get("items", [])
            ha = next((i.get("schema_health") or {} for i in lst if i.get("id") == A), {})
            hb = next((i.get("schema_health") or {} for i in lst if i.get("id") == B), {})
            check("TP-11-02: 列表内嵌 schema_health（stopped + open_drifts=1）",
                  ha.get("snapshot_auto_update") == "stopped" and ha.get("open_drifts") == 1, str(ha))
            b_open = drifts(B, "open").get("items") or []
            check("TP-11-04: 同源连接 B（不同对象/稳定键）无误报 key 漂移、不停更",
                  hb.get("snapshot_auto_update") == "active"
                  and all(it.get("severity") != "key" for r in b_open for it in r.get("items", [])),
                  f"hb={hb} b_open={b_open}")
            rb = tc.post(f"/api/mcp/connections/{B}/read?project_id={pid}", json={})
            check("TP-11-04: B 的读取→候选不受影响（200）", rb.status_code == 200, f"{rb.status_code}")

            # ================= TP-11-06 漂移明细 + 状态过滤 =================
            print("\n[TP-11-06] 漂移明细（kind/property/severity/来源）与状态过滤")
            open_recs = drifts(A, "open").get("items") or []
            rec = open_recs[0] if open_recs else {}
            check("TP-11-06: open 过滤返回 1 条（来源 manual）",
                  len(open_recs) == 1 and (rec.get("source") == "manual"
                                           and rec.get("last_detected_source") == "manual"),
                  str(open_recs)[:200])
            item0 = (rec.get("items") or [{}])[0]
            check("TP-11-06: 明细含 kind/property/severity/expected/actual 字段",
                  all(k in item0 for k in ("kind", "property", "severity", "expected", "actual")),
                  str(item0))

            # ================= TP-11-03 人工确认恢复 =================
            print("\n[TP-11-03] 人工确认恢复（唯一恢复路径）")
            rj = resolve(A, rec["id"]).json()
            check("TP-11-03: resolve → resolved 且恢复快照自动更新",
                  (rj.get("data") or {}).get("status") == "resolved"
                  and (rj.get("data") or {}).get("resumed_snapshot_auto_update") is True, str(rj)[:200])

            check("TP-11-03: 恢复后 snapshot_auto_update=active",
                  conn_rec(A).get("snapshot_auto_update") == "active")
            r = tc.post(f"/api/mcp/connections/{A}/read?project_id={pid}", json={})
            check("TP-11-03: 恢复后读取→候选放行（200）", r.status_code == 200, f"{r.status_code}")
            dup = resolve(A, rec["id"])
            check("TP-11-03: 重复 resolve → 409 ALREADY_RESOLVED",
                  dup.status_code == 409 and dup.json().get("error", {}).get("code") == "ALREADY_RESOLVED",
                  f"{dup.status_code}")
            aud = (tc.get("/api/audit?project_id=" + pid + "&action=schema_drift_resolved").json()
                   .get("data") or {})
            check("TP-11-03: 审计记录 schema_drift_resolved", (aud.get("items") or []) != [])

            # ================= EP-11-02 闭环再入 =================
            print("\n[EP-11-02] 源端未修复时再次检测 → 再次判定停更；修复后需再人工确认")
            d, ok, code = scheck(A)  # 源仍是 cust_no
            check("EP-11-02: 源端未修复，再次检查重新判定 key 漂移并停更",
                  d.get("severity") == "key" and conn_rec(A).get("snapshot_auto_update") == "stopped",
                  f"d={d}")
            all_recs = drifts(A, "all").get("items") or []
            check("EP-11-02: 历史已确认记录不复活（open=1 且 resolved 仍在）",
                  len([r for r in all_recs if r["status"] == "open"]) == 1
                  and len([r for r in all_recs if r["status"] == "resolved"]) == 1,
                  str([(r["status"]) for r in all_recs]))
            admin_schema(port, "rename", field="cust_no", new_field="customer_id")  # 源端修复
            d, ok, code = scheck(A)
            check("EP-11-02: 源端修复后不自动恢复（仍需人工确认）",
                  d.get("severity") is None and conn_rec(A).get("snapshot_auto_update") == "stopped",
                  f"d={d}")
            open2 = drifts(A, "open").get("items") or []
            rj = resolve(A, open2[0]["id"]).json()
            check("EP-11-02: 人工确认后恢复",
                  (rj.get("data") or {}).get("resumed_snapshot_auto_update") is True
                  and conn_rec(A).get("snapshot_auto_update") == "active", str(rj)[:200])

            # ================= TP-11-05 周期通道（source=periodic 同管道） =================
            print("\n[TP-11-05] 周期探测通道（直接调用同一管道，source=periodic）")
            admin_schema(port, "change_type", field="credit_limit", type="string")
            import schema_drift as sd
            res = sd.check_connection(pid, A, source="periodic", actor="scheduler")
            check("TP-11-05: periodic 通道检出属性类型变化（attribute，不停更）",
                  res.get("severity") == "attribute"
                  and any(i["kind"] == "attr_type_changed" and i["field"] == "credit_limit"
                          for i in res.get("items", [])), f"res={res}")
            check("TP-11-05: 记录来源标记 periodic",
                  (drifts(A, "open").get("items") or [{}])[0].get("last_detected_source") == "periodic")
            check("TP-11-05: 属性漂移不停快照更新", conn_rec(A).get("snapshot_auto_update") == "active")
            r = tc.post(f"/api/mcp/connections/{A}/read?project_id={pid}", json={})
            check("TP-11-05: 属性漂移下读取→候选仍可执行（200）", r.status_code == 200, f"{r.status_code}")

            # ================= TP-11-07 入库结构比对（ingest 反馈） =================
            print("\n[TP-11-07] 入库读取结构比对：缺失映射字段 → 属性漂移（不阻断）")
            admin_schema(port, "remove", field="region")
            d, ok, code = scheck(A)
            check("TP-11-07: 非键映射字段缺失 → attribute 漂移（attr_missing region）",
                  d.get("severity") == "attribute"
                  and any(i["kind"] == "attr_missing" and i["field"] == "region"
                          for i in d.get("items", [])), f"d={d}")
            r = tc.post(f"/api/mcp/connections/{A}/read?project_id={pid}", json={})
            check("TP-11-07: 入库读取不被阻断（200）", r.status_code == 200, f"{r.status_code}")

            # ================= TP-11-08 / EP-11-03 联邦读显式标记 =================
            print("\n[TP-11-08 / EP-11-03] 联邦查询：漂移字段显式标记（类型变化 + 源字段缺失）")
            lk = (tc.post("/api/dictionary/term-lookup?project_id=" + pid,
                          json={"term": "customer", "value": "C-001"}).json().get("data") or {})
            f1 = ((lk.get("merged") or [{}])[0].get("fields") or {})
            cl = f1.get("credit_limit") or {}
            check("TP-11-08: 联邦查询正常返回（不阻断），credit_limit 带漂移标记",
                  lk.get("status") == "ok" and (cl.get("drift_marks") or []) != []
                  and any(m.get("kind") == "attr_type_changed" for m in cl.get("drift_marks", [])),
                  f"credit_limit={cl}")
            check("EP-11-03: 漂移值仍显式返回但带标记（非静默旧值，value 有值 + drift_marks）",
                  cl.get("value") is not None and (cl.get("drift_marks") or []) != [], str(cl))
            region = f1.get("region") or {}
            check("EP-11-03: 源字段缺失 → 显式空值 + 缺失标记（不静默）",
                  region.get("value") is None and (region.get("drift_marks") or []) != []
                  and any("缺失" in (m.get("message") or "") for m in region.get("drift_marks", [])),
                  str(region))
            check("TP-11-08: 非漂移字段无标记（cust_name 干净）",
                  (f1.get("cust_name") or {}).get("drift_marks") in (None, []),
                  str(f1.get("cust_name")))

            # 源端修复并确认恢复，进入下一场景
            admin_schema(port, "add", field="region", type="string")
            admin_schema(port, "change_type", field="credit_limit", type="number")
            d, ok, code = scheck(A)
            check("场景清理: 源端修复后检查无新漂移", d.get("severity") is None, f"d={d}")
            open3 = drifts(A, "open").get("items") or []
            rj = resolve(A, open3[0]["id"]).json()
            check("场景清理: 人工确认后无待处理漂移",
                  (rj.get("data") or {}).get("status") == "resolved"
                  and drifts(A, "open").get("total") == 0, str(rj)[:200])

            # ================= TP-11-09 未映射新字段仅提示 =================
            print("\n[TP-11-09] 未映射新字段 → 仅提示，不阻断不停更")
            admin_schema(port, "add", field="tier", type="string")
            d, ok, code = scheck(A)
            check("TP-11-09: 新增源字段 → unmapped 提示（new_unmapped tier）",
                  d.get("severity") == "unmapped"
                  and any(i["kind"] == "new_unmapped" and i["field"] == "tier"
                          for i in d.get("items", [])), f"d={d}")
            check("TP-11-09: 不停更、读取放行",
                  conn_rec(A).get("snapshot_auto_update") == "active")
            r = tc.post(f"/api/mcp/connections/{A}/read?project_id={pid}", json={})
            check("TP-11-09: 读取→候选 200", r.status_code == 200, f"{r.status_code}")
            admin_schema(port, "remove", field="tier")
            d, ok, code = scheck(A)
            open4 = drifts(A, "open").get("items") or []
            rj = resolve(A, open4[0]["id"]).json()
            check("TP-11-09: 收尾确认（无待处理漂移）",
                  (rj.get("data") or {}).get("status") == "resolved"
                  and drifts(A, "open").get("total") == 0, str(rj)[:200])

            # ================= TP-11-10 / TP-11-11 健康端点 =================
            print("\n[TP-11-10 / TP-11-11] 健康端点：stale / never_checked / 全字段")
            time.sleep(1.1)  # 保证 age_seconds >= 1（stale 阈值已被乘数压到 0s）
            hC = health(C)
            check("TP-11-10: 已检查过且超阈值 → stale=true（never_checked=false）",
                  hC.get("never_checked") is False and hC.get("stale") is True, str(hC))
            D = conn("crm-new", "cust",
                     {"customer_id": "cust_key", "contact_id": "contact_ref",
                      "name": "cust_name", "credit_limit": "credit_limit",
                      "region": "region", "tier": "contact_ref"}, endpoint)
            hD = health(D)
            check("TP-11-10: 从未检查 → never_checked=true 且 stale=false",
                  hD.get("never_checked") is True and hD.get("stale") is False
                  and hD.get("last_schema_check_at") is None, str(hD))
            hA = health(A)
            need = ["last_schema_check_at", "age_seconds", "never_checked",
                    "probe_interval_seconds", "stale_threshold_seconds", "stale",
                    "snapshot_auto_update", "open_drifts", "cdc_status"]
            check("TP-11-11: health 返回全部约定字段", all(k in hA for k in need), str(hA))
            check("TP-11-11: cdc_status=planned_m3d（CDC 未上线前不显示假健康）",
                  hA.get("cdc_status") == "planned_m3d", str(hA.get("cdc_status")))
            check("TP-11-11: probe_interval_seconds 来自配置（300）",
                  hA.get("probe_interval_seconds") == 300, str(hA.get("probe_interval_seconds")))

            # 审计：检测/手动检查动作
            aud_d = (tc.get("/api/audit?project_id=" + pid + "&action=schema_drift_detected").json()
                     .get("data") or {})
            aud_m = (tc.get("/api/audit?project_id=" + pid + "&action=schema_check_manual").json()
                     .get("data") or {})
            check("审计: schema_drift_detected 与 schema_check_manual 均有记录",
                  (aud_d.get("items") or []) != [] and (aud_m.get("items") or []) != [])

    finally:
        mock.stop()
        shutil.rmtree(TMP_DATA, ignore_errors=True)

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 60}\nM3-A 验收结果: PASS={len(PASS)} FAIL={len(FAIL)} / {total}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
