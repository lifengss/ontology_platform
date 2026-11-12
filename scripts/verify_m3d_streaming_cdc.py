# -*- coding: utf-8 -*-
"""M3-D（V2-12 流式 CDC）验收脚本：TP-12-01~08 + EP-12-01~03。

链路：三 mock 源（cursor/mq/binlog 各一）→ 独立数据面 CDC 通道消费 →
增量归化（低风险自动/人工确认）→ 幂等去重 → 只更新关系快照（内容值负向断言）→
告警/熔断/积压限流 → 409 not_supported 降级（周期拉取+联邦读）→ 连接健康视图（REST/MCP 同口径）。

确定性设计：
  - Phase A（全部 TP）：ONTOLOGY_CDC_ENABLED=false 不启动后台循环，验收全走 REST 手动 poll；
  - Phase B（EP-12-01 自动恢复）：起真实 cdc._cdc_loop 后台线程（间隔 1s）验证自动续消费；
  - 幂等（lsn 单调）保证手动/后台双通道消费无重复副作用。

运行：backend 依赖就绪后 `python scripts/verify_m3d_streaming_cdc.py`（约 60s）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows 控制台为 GBK：统一 stdout 为 UTF-8（容忍无法编码字符），避免结尾 emoji 崩溃
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

BACKEND = Path(__file__).resolve().parents[1] / "backend"
TMP_DATA = BACKEND / "data" / "_m3d_verify"
shutil.rmtree(TMP_DATA, ignore_errors=True)

os.environ["DATA_DIR"] = str(TMP_DATA)
os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
os.environ["ONTOLOGY_CDC_ENABLED"] = "false"            # Phase A：手动 poll 驱动
os.environ["ONTOLOGY_CDC_POLL_INTERVAL_SECONDS"] = "1"  # Phase B 后台循环节拍
os.environ["ONTOLOGY_CDC_SUBSCRIBE_WAIT_MS"] = "200"
os.environ["ONTOLOGY_CDC_STALE_SECONDS"] = "5"
os.environ["ONTOLOGY_CDC_CIRCUIT_THRESHOLD"] = "3"
os.environ["ONTOLOGY_CDC_CIRCUIT_COOLDOWN_SECONDS"] = "1"
os.environ["ONTOLOGY_CDC_FALLBACK_PULL_SECONDS"] = "0"
os.environ["ONTOLOGY_CDC_BATCH_PAUSE_MS"] = "0"
os.environ["M3D_TOK"] = "tok-m3d"

sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

from config import get_settings  # noqa: E402

get_settings.cache_clear()
settings = get_settings()

from mocks.v2_external import ControllableMockSystem  # noqa: E402

# ---------------------------------------------------------------- 基础工具
PASS_COUNT = 0
FAILS: list[str] = []


def check(tag: str, desc: str, ok: bool, detail: str = "") -> None:
    global PASS_COUNT
    if ok:
        PASS_COUNT += 1
        print(f"  [PASS] {tag} {desc}")
    else:
        FAILS.append(f"{tag} {desc} | {detail}")
        print(f"  [FAIL] {tag} {desc} | {detail}")


def get(path: str):
    return TC.get(path)


def post(path: str, payload: dict | None = None):
    return TC.post(path, json=payload)


def put(path: str, payload: dict | None = None):
    return TC.put(path, json=payload)


ADMIN_KEY = "m3d-admin"
TOKS = {"tok-m3d": {"scope": "all"}}


def mock_admin(mock, path: str, body: dict) -> dict:
    """mock admin 端点挂在 /_admin/*（与数据面 /read /write /cdc/* 隔离）。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{mock.port}/_admin{path.removeprefix('/admin')}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"X-Mock-Admin": ADMIN_KEY, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def poll(cid: str) -> dict:
    r = post(f"/api/cdc/channels/{cid}/poll?project_id={PID}", {})
    assert r.status_code == 200, f"poll {cid} -> {r.status_code} {r.text[:200]}"
    return (r.json().get("data") or {})


def cdc_cfg(cid: str, channel: str | None = None, enabled: bool | None = None) -> dict:
    body = {}
    if channel is not None:
        body["channel"] = channel
    if enabled is not None:
        body["enabled"] = enabled
    r = post(f"/api/cdc/channels/{cid}/config?project_id={PID}", body)
    assert r.status_code == 200, f"config {cid} -> {r.text[:200]}"
    return (r.json().get("data") or {})


from core.storage import JsonStore  # noqa: E402
from core.instance_store import get_instance_store  # noqa: E402


def conn_rec(cid: str) -> dict:
    for rec in JsonStore("mcp_connections", PID).all():
        if rec.get("connection_id") == cid:
            return rec
    raise KeyError(cid)


def istore():
    return get_instance_store(PID)


def inst_keys(object_type: str) -> dict[str, dict]:
    """object_type -> {stable_key 值: instance dict}。"""
    out: dict[str, dict] = {}
    for rec in istore().find(object_type=object_type, limit=None):
        inst = rec.get("instance") or {}
        sk = object_type == "part" and "part_pn" or (
            object_type == "plm_item" and "plm_pn" or "rsr_pn")
        out[str(inst.get(sk) or "")] = inst
    return out


def alerts_of(cid: str, kind: str | None = None) -> list[dict]:
    r = get(f"/api/cdc/channels/{cid}/alerts?project_id={PID}&limit=100")
    assert r.status_code == 200, r.text[:200]
    items = (r.json().get("data") or {}).get("items") or []
    if kind:
        items = [a for a in items if a.get("type") == kind]
    return items


def open_drifts(cid: str) -> list[dict]:
    r = get(f"/api/mcp/connections/{cid}/drifts?project_id={PID}&status=open")
    assert r.status_code == 200, r.text[:200]
    return (r.json().get("data") or {}).get("items") or []


def drift_field_kinds(cid: str) -> list[str]:
    return [i.get("kind") for d in open_drifts(cid) for i in (d.get("items") or [])]


def channels_view() -> dict[str, dict]:
    r = get(f"/api/cdc/channels?project_id={PID}")
    assert r.status_code == 200, r.text[:200]
    items = (r.json().get("data") or {}).get("items") or []
    return {it.get("connection_id"): it for it in items}


# ---------------------------------------------------------------- mock 三系统
# ControllableMockSystem 的 fields 参数为 (name, type) 元组列表（按 n, t 解包）
CRM_FIELDS = [("PN", "string"), ("product_name", "string"), ("customer_name", "string")]
PLM_FIELDS = [("PN码", "string"), ("产品名称", "string"), ("product_model", "string")]
RSR_FIELDS = [("PN_Num", "string"), ("PN名称", "string"), ("capacity_mw", "float")]
CRM_SEED = [
    {"PN": "M3D-PN-001", "product_name": "内容值CRM_XYZ", "customer_name": "CUST-A"},
    {"PN": "M3D-PN-002", "product_name": "内容值CRM-002", "customer_name": "CUST-B"},
]
PLM_SEED = [
    {"PN码": "M3D-PN-001", "产品名称": "内容值PLM_XYZ", "product_model": "MOD-1"},
    {"PN码": "M3D-PN-002", "产品名称": "内容值PLM-002", "product_model": "MOD-2"},
]
RSR_SEED = [
    {"PN_Num": "M3D-PN-001", "PN名称": "内容值RSR_XYZ", "capacity_mw": 100},
    {"PN_Num": "M3D-PN-002", "PN名称": "内容值RSR-002", "capacity_mw": 200},
]

# 构造期不给种子：mock 构造不产 CDC 事件，种子随后经 /_admin/data upsert
# 以 create 事件注入（保证 create 语义与 lsn 起点 1,2 确定）
crm = ControllableMockSystem("crm", "PN", CRM_FIELDS, [],
                             tokens=TOKS, cdc_channel="cursor", admin_key=ADMIN_KEY)
plm = ControllableMockSystem("plm", "PN码", PLM_FIELDS, [],
                             tokens=TOKS, cdc_channel="mq", admin_key=ADMIN_KEY)
rsr = ControllableMockSystem("reservoir", "PN_Num", RSR_FIELDS, [],
                             tokens=TOKS, cdc_channel="binlog", admin_key=ADMIN_KEY)
crm.start(port=0)
plm.start(port=0)
rsr.start(port=0)

TC = TestClient(__import__("main").app)
TC.__enter__()

# ---------------------------------------------------------------- 项目/本体/连接
ONTO = {
    "name": "M3D 流式CDC 本体",
    "objects": [
        {"id": "part", "name": "部件", "stable_key": "part_pn",
         "attributes": [
             {"id": "part_pn", "name": "部件PN", "data_type": "string", "required": True},
             {"id": "part_name", "name": "部件名称", "data_type": "string"},
             {"id": "customer", "name": "客户代码", "data_type": "string"},
         ],
         "relations": [
             {"id": "customer", "name": "所属客户", "target_object": "customer",
              "cardinality": "many_to_one"},
         ]},
        {"id": "customer", "name": "客户", "stable_key": "customer_code",
         "attributes": [
             {"id": "customer_code", "name": "客户代码", "data_type": "string", "required": True},
             {"id": "customer_name", "name": "客户名称", "data_type": "string"},
         ]},
        {"id": "plm_item", "name": "PLM物料", "stable_key": "plm_pn",
         "attributes": [
             {"id": "plm_pn", "name": "物料PN", "data_type": "string", "required": True},
             {"id": "plm_name", "name": "物料名称", "data_type": "string"},
             {"id": "model", "name": "型号", "data_type": "string"},
         ]},
        {"id": "reservoir_unit", "name": "水库机组", "stable_key": "rsr_pn",
         "attributes": [
             {"id": "rsr_pn", "name": "机组PN", "data_type": "string", "required": True},
             {"id": "rsr_name", "name": "机组名称", "data_type": "string"},
             {"id": "capacity", "name": "容量MW", "data_type": "float"},
         ]},
    ],
}

r = post("/api/projects", {"name": "M3D 验证项目", "code": "m3d-verify"})
assert r.status_code in (200, 201), r.text[:300]
PID = (r.json().get("data") or {}).get("id") or (r.json().get("data") or {}).get("project_id")
assert PID, r.text[:300]

r = post(f"/api/ontologies?project_id={PID}", ONTO)
assert r.status_code in (200, 201), r.text[:300]
OID = (r.json().get("data") or {}).get("id")
assert OID, r.text[:300]

r = post(f"/api/mcp/server/publish?project_id={PID}&ontology_id={OID}", {})
check("S0", "MCP 发布成功", r.status_code in (200, 201), r.text[:200])

CID_CRM = CID_PLM = CID_RSR = None
for name, port, obj, fmap in (
        ("crm→part", crm.port, "part",
         {"PN": "part_pn", "product_name": "part_name", "customer_name": "customer"}),
        ("plm→plm_item", plm.port, "plm_item",
         {"PN码": "plm_pn", "产品名称": "plm_name", "product_model": "model"}),
        ("reservoir→reservoir_unit", rsr.port, "reservoir_unit",
         {"PN_Num": "rsr_pn", "PN名称": "rsr_name", "capacity_mw": "capacity"})):
    r = post(f"/api/mcp/connections?project_id={PID}", {
        "display_name": name, "transport": "streamable_http",
        "endpoint": f"http://127.0.0.1:{port}/read",
        "auth": {"type": "bearer_token", "bearer_token_ref": "${M3D_TOK}"},
        "maps_to_ontology": {"ontology_id": OID, "object_type": obj, "field_map": fmap}})
    assert r.status_code in (200, 201), f"连接创建 {name} -> {r.text[:300]}"
    cid = (r.json().get("data") or {}).get("connection_id")
    assert cid, r.text[:300]
    if obj == "part":
        CID_CRM = cid
    elif obj == "plm_item":
        CID_PLM = cid
    else:
        CID_RSR = cid

r = post(f"/api/dictionary/terms?project_id={PID}", {
    "canonical": "M3D PN码", "object_type": "part", "property_id": "part_pn",
    "synonyms": ["M3D PN"],
    "system_bindings": {CID_CRM: "PN", CID_PLM: "PN码", CID_RSR: "PN_Num"}})
check("S1", "词条创建（3 系统绑定）", r.status_code in (200, 201)
      and (r.json().get("data") or {}).get("canonical") == "M3D PN码", r.text[:200])

print("== setup：auto 通道自动探测（源端 409 提示优先 + 固定序兜底） ==")
for cid in (CID_CRM, CID_PLM, CID_RSR):
    cdc_cfg(cid, channel="auto", enabled=True)
for mock, seed in ((crm, CRM_SEED), (plm, PLM_SEED), (rsr, RSR_SEED)):
    mock_admin(mock, "/admin/data", {"action": "upsert", "records": seed})
r1 = poll(CID_CRM)
r2 = poll(CID_PLM)
r3 = poll(CID_RSR)
check("S2", "三连接 auto 探测命中各自通道（cursor/mq/binlog）并消费种子",
      r1.get("status") == "ok" and conn_rec(CID_CRM)["cdc"].get("supported_channel") == "cursor"
      and r2.get("status") == "ok" and conn_rec(CID_PLM)["cdc"].get("supported_channel") == "mq"
      and r3.get("status") == "ok" and conn_rec(CID_RSR)["cdc"].get("supported_channel") == "binlog"
      and r1.get("applied") == 2 and r2.get("applied") == 2 and r3.get("applied") == 2,
      json.dumps({"crm": r1.get("status"), "plm": r2.get("status"), "rsr": r3.get("status")},
                 ensure_ascii=False))
view = channels_view()
check("S3", "GET /api/cdc/channels 三连接均含 cdc 摘要（扁平字段）",
      len(view) == 3
      and all(v.get("enabled") for v in view.values())
      and all(v.get("supported_channel") for v in view.values()),
      json.dumps({k: (v.get("cdc_status"), v.get("supported_channel")) for k, v in view.items()},
                 ensure_ascii=False))

# ---------------------------------------------------------------- TP-12-01 crm(cursor) 增/改/删 + 关系
print("== TP-12-01 crm(cursor)：增/改/删 + 关系快照 ==")
r = poll(CID_CRM)
check("TP-12-01a", "初始 poll（S2 已消费）无新事件、无重复",
      r.get("status") == "ok" and r.get("events") == 0 and r.get("applied") == 0,
      json.dumps(r, ensure_ascii=False)[:200])
inst001 = inst_keys("part").get("M3D-PN-001") or {}
check("TP-12-01b", "create 事件落关系快照（键+关系属性+_source.kind=cdc）",
      inst001.get("part_pn") == "M3D-PN-001" and inst001.get("customer") == "CUST-A"
      and (inst001.get("_source") or {}).get("kind") == "cdc"
      and (inst001.get("_source") or {}).get("connection_id") == CID_CRM,
      json.dumps(inst001, ensure_ascii=False)[:200])

mock_admin(crm, "/admin/data", {"action": "upsert", "records": [
    {"PN": "M3D-PN-001", "customer_name": "CUST-C"}]})
r = poll(CID_CRM)
inst001b = inst_keys("part").get("M3D-PN-001") or {}
check("TP-12-01c", "update 事件 → 关系属性刷新（客户 CUST-A→CUST-C）",
      r.get("status") == "ok" and r.get("applied") == 1
      and inst001b.get("customer") == "CUST-C",
      json.dumps({"poll": r.get("status"), "inst": inst001b}, ensure_ascii=False)[:200])

mock_admin(crm, "/admin/data", {"action": "remove", "keys": ["M3D-PN-002"]})
r = poll(CID_CRM)
check("TP-12-01d", "remove 事件 → 实例删除",
      r.get("status") == "ok" and r.get("applied") == 1
      and "M3D-PN-002" not in inst_keys("part"),
      json.dumps(r, ensure_ascii=False)[:200])

r = get(f"/api/ingest/candidates?project_id={PID}&status=pending&pageSize=100")
pending = [c for c in ((r.json().get("data") or {}).get("items") or [])
           if c.get("source_type") == "cdc"]
check("TP-12-01e", "低风险增/改/删均自动入库、无人工确认候选",
      r.status_code == 200 and len(pending) == 0,
      json.dumps(pending, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- TP-12-03 内容值负向
print("== TP-12-03 只更新关系快照：内容值永不写实例 ==")
inst_files = sorted((TMP_DATA / "projects" / PID / "instances").glob("*.jsonl"))
blob = ""
for f in inst_files:
    blob += f.read_text(encoding="utf-8", errors="ignore")
check("TP-12-03a", "内容值 token 不出现在任何实例存储文件",
      len(inst_files) >= 3 and "内容值CRM_XYZ" not in blob
      and "内容值PLM_XYZ" not in blob and "内容值RSR_XYZ" not in blob,
      f"files={[f.name for f in inst_files]}")
leak = [k for k, v in {**inst_keys("part"), **inst_keys("plm_item"), **inst_keys("reservoir_unit")}.items()
        if any("内容值" in str(x) for x in (v.get("part_name"), v.get("plm_name"), v.get("rsr_name"),
                                            v.get("model"), v.get("capacity")))]
check("TP-12-03b", "实例字典无内容值属性（仅键+关系+_source）",
      not leak and "part_name" not in (inst001b or {}), json.dumps(leak, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- TP-12-02 plm(mq) 幂等
print("== TP-12-02 plm(mq)：重投递幂等（reset_offset） ==")
keys_before = set(inst_keys("plm_item").keys())
mock_admin(plm, "/admin/cdc", {"action": "reset_offset"})
r = poll(CID_PLM)
keys_after = set(inst_keys("plm_item").keys())
check("TP-12-02", "offset 重置重投递 → 全量跳过（dup/stale）、零重复入库",
      r.get("status") == "ok" and r.get("events") == 2 and r.get("applied") == 0
      and (r.get("skipped_dup") + r.get("skipped_stale")) == 2 and keys_before == keys_after,
      json.dumps({k: r.get(k) for k in ("events", "applied", "skipped_dup", "skipped_stale")},
                 ensure_ascii=False))

# ---------------------------------------------------------------- EP-12-01（手动）rsr 断流恢复
print("== EP-12-01 rsr(binlog)：断流恢复不丢事件（手动 poll 版） ==")
mock_admin(rsr, "/admin/config", {"outage": True})
time.sleep(6)  # > STALE_SECONDS(5) 触发 stalled
mock_admin(rsr, "/admin/data", {"action": "upsert", "records": [
    {"PN_Num": "M3D-PN-901", "PN名称": "断流期间新增", "capacity_mw": 99}]})
r = poll(CID_RSR)
check("EP-12-01a", "断流期间 poll 失败 + stalled 告警（持续无成功消费>5s）",
      r.get("status") == "error" and len(alerts_of(CID_RSR, "stalled")) == 1,
      json.dumps(r, ensure_ascii=False)[:200])
mock_admin(rsr, "/admin/config", {"outage": False})
r = poll(CID_RSR)
check("EP-12-01b", "恢复后自动续消费断流期间事件（不丢失）+ stalled 恢复",
      r.get("status") == "ok" and r.get("applied") == 1
      and "M3D-PN-901" in inst_keys("reservoir_unit")
      and len(alerts_of(CID_RSR, "stalled_recovered")) == 1,
      json.dumps(r, ensure_ascii=False)[:200])
r = poll(CID_RSR)
check("EP-12-01c", "续消费后无重复（游标已前进）",
      r.get("status") == "ok" and r.get("events") == 0 and r.get("applied") == 0,
      json.dumps(r, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- TP-12-05 rsr 409 降级 + 联邦读 + 自动重探测
print("== TP-12-05 rsr：409 CDC 未支持 → 降级周期拉取+联邦读 → 源端恢复自动重探测 ==")
read_calls_before = rsr.read_calls
mock_admin(rsr, "/admin/config", {"cdc_channel": "none"})
r = poll(CID_RSR)
read_calls_after = rsr.read_calls
check("TP-12-05a", "409 → not_supported，且 CDC 路径不经 MCP read（read_calls 不变）",
      r.get("status") == "not_supported"
      and "CDC_NOT_SUPPORTED" in json.dumps(r, ensure_ascii=False)
      and read_calls_after == read_calls_before
      and conn_rec(CID_RSR)["cdc"].get("supported") is False,
      json.dumps(r, ensure_ascii=False)[:200])
view = channels_view()
check("TP-12-05b", "健康视图标记 CDC 未支持（cdc_status=not_supported）",
      (view.get(CID_RSR) or {}).get("cdc_status") == "not_supported"
      and (view.get(CID_RSR) or {}).get("supported") is False,
      json.dumps({k: (view.get(CID_RSR) or {}).get(k)
                  for k in ("cdc_status", "supported", "supported_channel")},
                 ensure_ascii=False))

r = post(f"/api/cdc/channels/{CID_RSR}/fallback-pull?project_id={PID}", {})
pulled = (r.json().get("data") or {})
check("TP-12-05c", "降级 fallback-pull（MCP read 全量）：pulled=3 且 read_calls+1",
      r.status_code == 200 and pulled.get("status") == "ok" and pulled.get("pulled") == 3
      and rsr.read_calls == read_calls_after + 1
      and conn_rec(CID_RSR)["cdc"].get("last_fallback_pull_at"),
      json.dumps(pulled, ensure_ascii=False)[:200])

r = post(f"/api/dictionary/term-lookup?project_id={PID}",
         {"term": "M3D PN码", "value": "M3D-PN-001"})
td = r.json().get("data") or {}
if td.get("status") == "cost_confirmation_required":
    r = post(f"/api/dictionary/term-lookup?project_id={PID}",
             {"term": "M3D PN码", "value": "M3D-PN-001", "confirm_cost": True})
    td = r.json().get("data") or {}
sysview = td.get("systems") or {}
check("TP-12-05d", "联邦读覆盖三系统（降级期数据面仍可用）",
      td.get("status") == "ok"
      and all(sysview.get(k, {}).get("covered") for k in (CID_CRM, CID_PLM, CID_RSR)),
      json.dumps({k: v.get("covered") for k, v in sysview.items()}, ensure_ascii=False)[:200])

count_rsr_before = len(inst_keys("reservoir_unit"))
mock_admin(rsr, "/admin/config", {"cdc_channel": "cursor"})
cdc_cfg(CID_RSR, channel="auto")  # 复位 supported → 触发 auto 重探测
r = poll(CID_RSR)
check("TP-12-05e", "源端切 cursor 后 auto 自动重探测命中；切通道零副作用（since=last_lsn 不重复应用）",
      r.get("status") == "ok"
      and conn_rec(CID_RSR)["cdc"].get("supported_channel") == "cursor"
      and r.get("applied") == 0 and len(inst_keys("reservoir_unit")) == count_rsr_before,
      json.dumps({k: r.get(k) for k in ("status", "applied", "events")},
                 ensure_ascii=False))

# ---------------------------------------------------------------- TP-12-04 plm 告警/熔断
print("== TP-12-04 plm：lag 告警/恢复 + stalled + 熔断 closed→open→half_open→closed ==")
mock_admin(plm, "/admin/data", {"action": "upsert", "records": [
    {"PN码": "M3D-PN-101", "产品名称": "滞后台账", "product_model": "MOD-LAG"}]})
plm._events[-1]["ts"] = time.time() - 120  # 事件时间戳回拨 120s（> lag 阈值 60s）
r = poll(CID_PLM)
check("TP-12-04a", "滞后事件 → lag 告警（lag_seconds>60 且仅告警不阻断入库）",
      r.get("status") == "ok" and r.get("applied") == 1
      and (r.get("lag_seconds") or 0) > 60 and len(alerts_of(CID_PLM, "lag")) == 1
      and "M3D-PN-101" in inst_keys("plm_item"),
      json.dumps({k: r.get(k) for k in ("status", "applied", "lag_seconds")}, ensure_ascii=False))
mock_admin(plm, "/admin/data", {"action": "upsert", "records": [
    {"PN码": "M3D-PN-102", "产品名称": "正常时间戳", "product_model": "MOD-OK"}]})
r = poll(CID_PLM)
check("TP-12-04b", "实时事件 → lag_recovered 告警",
      r.get("status") == "ok" and r.get("applied") == 1
      and len(alerts_of(CID_PLM, "lag_recovered")) == 1,
      json.dumps(r, ensure_ascii=False)[:200])

mock_admin(plm, "/admin/config", {"outage": True})
time.sleep(6)  # 超 stalled 阈值
r1 = poll(CID_PLM)
check("TP-12-04c", "断流持续无成功消费 → stalled 告警（仅一次，不刷屏）",
      r1.get("status") == "error" and len(alerts_of(CID_PLM, "stalled")) == 1,
      json.dumps(r1, ensure_ascii=False)[:200])
r2 = poll(CID_PLM)
r3 = poll(CID_PLM)
check("TP-12-04d", "连续失败≥3 → 熔断 open；冷却期内 poll 直接 circuit_open 早退",
      (conn_rec(CID_PLM)["cdc"].get("circuit") or {}).get("state") == "open"
      and r3.get("status") == "circuit_open",
      json.dumps({"r2": r2.get("status"), "r3": r3.get("status"),
                  "circuit": conn_rec(CID_PLM)["cdc"].get("circuit")}, ensure_ascii=False)[:200])
time.sleep(1.2)  # 冷却到期
mock_admin(plm, "/admin/config", {"outage": False})
r4 = poll(CID_PLM)
check("TP-12-04e", "冷却到期 half_open 探测成功 → closed + stalled_recovered",
      r4.get("status") == "ok"
      and (conn_rec(CID_PLM)["cdc"].get("circuit") or {}).get("state") == "closed"
      and len(alerts_of(CID_PLM, "stalled_recovered")) == 1,
      json.dumps({"status": r4.get("status"),
                  "circuit": conn_rec(CID_PLM)["cdc"].get("circuit")}, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- TP-12-06 三通道验证
view = channels_view()
chan_map = {CID_CRM: "cursor", CID_PLM: "mq", CID_RSR: "cursor"}
ok6 = all((view.get(cid) or {}).get("supported_channel") == want
          and (view.get(cid) or {}).get("applied_total", 0) >= 1
          for cid, want in chan_map.items())
check("TP-12-06", "三通道（cursor/mq/binlog）均已实际消费过数据（applied_total≥1；binlog 见 S2）",
      ok6,
      json.dumps({k: v.get("supported_channel") for k, v in view.items()},
                 ensure_ascii=False))

# ---------------------------------------------------------------- TP-12-08 crm 键漂移全链路
print("== TP-12-08 crm：schema_change 人工确认 + 键漂移门禁 + 修复恢复 ==")
mock_admin(crm, "/admin/schema", {"action": "rename", "field": "PN", "new_field": "PN_New"})
r = poll(CID_CRM)
check("TP-12-08a", "键重命名 → schema_change 进人工确认（候选）+ 键漂移 opened + 快照停更",
      r.get("status") == "ok" and r.get("review") == 1
      and conn_rec(CID_CRM).get("snapshot_auto_update") == "stopped"
      and "key_missing" in drift_field_kinds(CID_CRM),
      json.dumps({"review": r.get("review"), "kinds": drift_field_kinds(CID_CRM)},
                 ensure_ascii=False)[:300])

r = get(f"/api/ingest/candidates?project_id={PID}&status=pending&pageSize=100")
cdc_cands = [c for c in ((r.json().get("data") or {}).get("items") or [])
             if c.get("source_type") == "cdc"]
check("TP-12-08b", "CDC 候选携带 cdc_reason（schema_change 语义，待人工确认）",
      len(cdc_cands) >= 1
      and str(cdc_cands[0].get("cdc_reason") or "").startswith("schema_change"),
      json.dumps({c.get("id"): c.get("cdc_reason") for c in cdc_cands}, ensure_ascii=False)[:200])

count_before = len(inst_keys("part"))
# mock 保真简化：键重命名后 key_field 仍是旧名，写入需同时携带旧键名（不影响事件 after，
# after 仅含当前字段表 PN_New/product_name/customer_name）
mock_admin(crm, "/admin/data", {"action": "upsert", "records": [
    {"PN": "M3D-PN-301", "PN_New": "M3D-PN-301", "product_name": "停更期间新增",
     "customer_name": "CUST-D"}]})
r = poll(CID_CRM)
check("TP-12-08c", "键漂移期间门禁拦截：poll 返回 stopped_drift、事件不消费",
      r.get("status") == "stopped_drift" and r.get("events") == 0
      and len(inst_keys("part")) == count_before,
      json.dumps(r, ensure_ascii=False)[:200])

# PUT 为全量更新（ConnectionConfig 要求 display_name 等必填）：取当前连接合并后提交
cur = get(f"/api/mcp/connections/{CID_CRM}?project_id={PID}")
curc = (cur.json().get("data") or {}) if cur.status_code == 200 else {}
mto = dict(curc.get("maps_to_ontology") or {})
mto["field_map"] = {"PN_New": "part_pn", "product_name": "part_name",
                    "customer_name": "customer"}
payload = {
    "display_name": curc.get("display_name") or "crm→part",
    "transport": curc.get("transport") or "http",
    "endpoint": curc.get("endpoint"),
    "auth": curc.get("auth"),
    "maps_to_ontology": mto,
}
if curc.get("tool_allowlist"):
    payload["tool_allowlist"] = curc["tool_allowlist"]
r = put(f"/api/mcp/connections/{CID_CRM}?project_id={PID}", payload)
check("TP-12-08d", "field_map 修复（PUT 连接）成功",
      r.status_code == 200
      and ((r.json().get("data") or {}).get("maps_to_ontology") or {}).get("field_map", {}).get("PN_New") == "part_pn",
      r.text[:200])
r = post(f"/api/mcp/connections/{CID_CRM}/schema-check?project_id={PID}", {})
check("TP-12-08e", "修复后手动 schema 检查：无漂移项（open 记录仅能人工 resolve）",
      r.status_code == 200
      and ((r.json().get("data") or {}).get("items") or []) == []
      and (r.json().get("data") or {}).get("severity") is None,
      r.text[:200])
drift_items = open_drifts(CID_CRM)
did = drift_items[0].get("id") if drift_items else None
r = post(f"/api/mcp/connections/{CID_CRM}/drifts/{did}/resolve?project_id={PID}", {})
check("TP-12-08f", "人工 resolve 键漂移 → 快照自动更新恢复（active）",
      r.status_code == 200
      and (r.json().get("data") or {}).get("resumed_snapshot_auto_update") is True
      and conn_rec(CID_CRM).get("snapshot_auto_update") == "active",
      r.text[:200])
r = poll(CID_CRM)
inst301 = inst_keys("part").get("M3D-PN-301") or {}
check("TP-12-08g", "恢复后续消费停更期间事件（新键 PN_New 归一化，create 低风险自动）",
      r.get("status") == "ok" and r.get("applied") == 1 and r.get("review") == 0
      and inst301.get("part_pn") == "M3D-PN-301" and inst301.get("customer") == "CUST-D",
      json.dumps({"r": {k: r.get(k) for k in ("status", "applied", "review")}, "inst": inst301},
                 ensure_ascii=False)[:300])

r = get(f"/api/ingest/candidates?project_id={PID}&status=pending&pageSize=100")
cdc_cands = [c for c in ((r.json().get("data") or {}).get("items") or [])
             if c.get("source_type") == "cdc"]
r = post(f"/api/ingest/candidates/{cdc_cands[0]['id']}/approve?project_id={PID}", {})
check("TP-12-08h", "人工确认（approve）CDC 候选走 V2 审批链落库",
      r.status_code == 200 and r.json().get("success") is True, r.text[:200])

# plm 非键漂移（新增未映射字段）：联动 drift 但不停更、低风险仍自动入库
mock_admin(plm, "/admin/schema", {"action": "add", "field": "part_ref", "type": "string"})
r = poll(CID_PLM)
check("TP-12-08i", "新增未映射字段 → drift(new_unmapped) 联动且不停更（snapshot 非 stopped）",
      r.get("status") == "ok" and r.get("review") == 1
      and conn_rec(CID_PLM).get("snapshot_auto_update") in (None, "active")
      and "new_unmapped" in drift_field_kinds(CID_PLM),
      json.dumps(drift_field_kinds(CID_PLM), ensure_ascii=False)[:200])
mock_admin(plm, "/admin/data", {"action": "upsert", "records": [
    {"PN码": "M3D-PN-302", "产品名称": "漂移未停更验证", "product_model": "MOD-N"}]})
r = poll(CID_PLM)
check("TP-12-08j", "非键漂移期间低风险事件仍自动入库（门禁放行）",
      r.get("status") == "ok" and r.get("applied") == 1 and "M3D-PN-302" in inst_keys("plm_item"),
      json.dumps(r, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- EP-12-03 洪峰
print("== EP-12-03 plm：350 条洪峰分批限流（max_batch=100 → 4 批）无丢失 ==")
count_before = len(inst_keys("plm_item"))
flood = [{"PN码": f"M3D-PN-F{i:03d}", "产品名称": f"洪峰{i}", "product_model": f"MOD-F{i}"}
         for i in range(350)]
mock_admin(plm, "/admin/data", {"action": "upsert", "records": flood})
r = poll(CID_PLM)
count_after = len(inst_keys("plm_item"))
check("EP-12-03", "350 事件 4 批消费、零丢失、零重复",
      r.get("status") == "ok" and r.get("events") == 350 and r.get("batches") == 4
      and r.get("applied") == 350 and count_after - count_before == 350,
      json.dumps({k: r.get(k) for k in ("events", "batches", "applied", "status")}, ensure_ascii=False))

# ---------------------------------------------------------------- TP-12-07 健康视图
print("== TP-12-07 连接健康视图：REST/MCP 同口径 + 三类统计 + 404 + disabled ==")
# rsr 此前无 schema 检查记录（never_checked）：先手动检查一次，保证检查年龄口径有值
r = post(f"/api/mcp/connections/{CID_RSR}/schema-check?project_id={PID}", {})
assert r.status_code == 200, f"schema-check rsr -> {r.status_code} {r.text[:200]}"
r = get(f"/api/mcp/connections/{CID_RSR}/health?project_id={PID}")
h = r.json().get("data") or {}
stats = h.get("stats") or {}
check("TP-12-07a", "REST 健康视图：cdc_status + cdc 块 + 三类调用统计（read/cdc/federation 均有记录）",
      r.status_code == 200 and h.get("cdc_status") == "running"
      and h.get("cdc", {}).get("supported_channel") == "cursor"
      and all(k in stats for k in ("read", "cdc", "federation"))
      and (stats.get("read") or {}).get("calls", 0) >= 1
      and (stats.get("cdc") or {}).get("calls", 0) >= 3
      and (stats.get("federation") or {}).get("calls", 0) >= 1,
      json.dumps({"cdc_status": h.get("cdc_status"), "stats": stats}, ensure_ascii=False)[:300])
check("TP-12-07b", "距上次 schema 检查人读时长（xx小时xx分xx秒）+ 检查元数据齐全",
      re.fullmatch(r"\d+小时\d+分\d+秒", h.get("schema_check_age_human") or "") is not None
      and h.get("last_schema_check_at") and h.get("last_schema_version") is not None,
      json.dumps({k: h.get(k) for k in ("schema_check_age_human", "last_schema_version")},
                 ensure_ascii=False))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp_server import _register_tools, get_mcp_server  # noqa: E402

tmp_srv = MCPServer(name="m3d-mcp")
_register_tools(get_mcp_server(), PID, tmp_srv)
ctool = tmp_srv._tool_manager._tools["connection_health"].fn
mcp_h = json.loads(ctool(connection_id=CID_RSR))
check("TP-12-07c", "MCP connection_health 与 REST 同口径（cdc_status/cdc/stats 一致）",
      mcp_h.get("cdc_status") == h.get("cdc_status")
      and mcp_h.get("cdc") == h.get("cdc")
      and mcp_h.get("stats") == h.get("stats"),
      json.dumps({"mcp_status": mcp_h.get("cdc_status")}, ensure_ascii=False))

# 契约：health/config/alerts 不存在 → 404 NOT_FOUND 信封；
# poll/fallback-pull 属轮询语义（模块"不抛错"设计）→ 200 + data.status="not_found"
bad = []
for path, method in (
        (f"/api/mcp/connections/nope/health?project_id={PID}", "get"),
        (f"/api/cdc/channels/nope/config?project_id={PID}", "post"),
        (f"/api/cdc/channels/nope/alerts?project_id={PID}", "get")):
    rr = get(path) if method == "get" else post(path, {})
    e = (rr.json() or {}).get("error") or {}
    if rr.status_code != 404 or e.get("code") != "NOT_FOUND":
        bad.append(f"{path} -> {rr.status_code}/{e.get('code')}")
for path in (f"/api/cdc/channels/nope/poll?project_id={PID}",
             f"/api/cdc/channels/nope/fallback-pull?project_id={PID}"):
    rr = post(path, {})
    st = (rr.json().get("data") or {}).get("status")
    if rr.status_code != 200 or st != "not_found":
        bad.append(f"{path} -> {rr.status_code}/{st}")
check("TP-12-07d", "不存在的连接：health/config/alerts→404 NOT_FOUND；poll/fallback-pull→200 status=not_found",
      not bad, "; ".join(bad))

cdc_cfg(CID_CRM, enabled=False)
r = poll(CID_CRM)
check("TP-12-07e", "enabled=false → poll 返回 disabled、健康视图 cdc_status=disabled",
      r.get("status") == "disabled"
      and (get(f"/api/mcp/connections/{CID_CRM}/health?project_id={PID}")
           .json().get("data") or {}).get("cdc_status") == "disabled",
      json.dumps(r, ensure_ascii=False)[:150])
cdc_cfg(CID_CRM, enabled=True)
r = poll(CID_CRM)
check("TP-12-07f", "重新启用 → running（恢复消费）",
      r.get("status") == "ok"
      and (get(f"/api/mcp/connections/{CID_CRM}/health?project_id={PID}")
           .json().get("data") or {}).get("cdc_status") == "running",
      json.dumps(r, ensure_ascii=False)[:150])

# ---------------------------------------------------------------- Phase B：EP-12-01 自动恢复
print("== Phase B：EP-12-01 自动恢复（真实后台 _cdc_loop 自动续消费，零手动 poll） ==")
import cdc as cdc_mod  # noqa: E402

settings.cdc_enabled = True
threading.Thread(target=cdc_mod._cdc_loop, daemon=True, name="m3d-verify-cdc-loop").start()
time.sleep(2)  # 等循环进入首轮

applied_before = (conn_rec(CID_CRM)["cdc"] or {}).get("applied_total", 0)
mock_admin(crm, "/admin/config", {"outage": True})
deadline = time.time() + 30
while time.time() < deadline:
    if (conn_rec(CID_CRM)["cdc"].get("circuit") or {}).get("state") == "open":
        break
    time.sleep(0.5)
check("EP-12-01auto-a", "后台循环连续失败≥3 → 自动熔断 open（全程零手动 poll）",
      (conn_rec(CID_CRM)["cdc"].get("circuit") or {}).get("state") == "open",
      json.dumps(conn_rec(CID_CRM)["cdc"].get("circuit"), ensure_ascii=False)[:200])

mock_admin(crm, "/admin/data", {"action": "upsert", "records": [
    {"PN": "M3D-PN-401", "PN_New": "M3D-PN-401", "product_name": "熔断期间事件",
     "customer_name": "CUST-E"}]})  # 同 08c：键重命名后 mock 按旧键名取 key
mock_admin(crm, "/admin/config", {"outage": False})

# 等 401 实例落库且熔断 closed 持久化（实例插入先于 circuit 状态落盘，需等两者齐备）
deadline = time.time() + 40
got = None
circuit = {}
while time.time() < deadline:
    got = inst_keys("part").get("M3D-PN-401")
    circuit = conn_rec(CID_CRM)["cdc"].get("circuit") or {}
    if got and circuit.get("state") == "closed":
        break
    time.sleep(0.5)
applied_after = (conn_rec(CID_CRM)["cdc"] or {}).get("applied_total", 0)
check("EP-12-01auto-b", "断流恢复：half_open→closed，熔断期间缓冲事件自动续消费（不丢失、仅 +1）",
      got is not None and got.get("part_pn") == "M3D-PN-401" and got.get("customer") == "CUST-E"
      and circuit.get("state") == "closed" and applied_after == applied_before + 1,
      json.dumps({"inst": got, "circuit": circuit,
                  "applied": f"{applied_before}->{applied_after}"}, ensure_ascii=False)[:300])
view = channels_view()
check("EP-12-01auto-c", "自动恢复后三连接健康视图均为 running（后台循环持续消费中）",
      all(view.get(c, {}).get("cdc_status") == "running" for c in (CID_CRM, CID_PLM, CID_RSR)),
      json.dumps({k: v.get("cdc_status") for k, v in view.items()}, ensure_ascii=False))

# ---------------------------------------------------------------- 汇总
print()
print("=" * 72)
print(f"M3-D 验收结果：PASS={PASS_COUNT}  FAIL={len(FAILS)}")
if FAILS:
    print("失败项：")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS ✅（TP-12-01~08 + EP-12-01~03 全绿）")
sys.exit(0)
