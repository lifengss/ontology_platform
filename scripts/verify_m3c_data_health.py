# -*- coding: utf-8 -*-
"""M3-C 验收脚本：V2-21 数据健康度（核心四维：完整性/唯一性/合规性/新鲜度）。

覆盖 V2-测试大纲.md §14.1/§14.2：
  TP-21-01 脏数据批次健康卡（缺键/重复键/枚举出域 + 分布 + 必填填充率 + 新鲜度）
  TP-21-02 建模后基线（is_baseline）+ 趋势序列 + 可归因（口径 hash + audit quality_evaluated）
  TP-21-03 口径变化（新增 sh:in 约束）→ 违规率升且趋势标注 caliber_changed（不误导）
  TP-21-04 规模变化（新增实例）→ 趋势标注 scale_changed，指标恒伴随规模
  TP-21-05 MCP quality_health（对象级 + 属性级钻取数据 + 趋势）与 REST 同口径
  TP-21-06 入库阻断职责不变（脏批次 SHACL 门禁仍拦截，健康卡仅观测不阻断）
  TP-21-07 执行通道：local_snapshot（键/关系/新鲜度）/ local_full（上传）/
           governance_sampled（源连接治理身份采样读源端最新值）/ source_required（采样关闭显式标记）
  EP-21-01 健康卡计算失败 → 显式 error，不 500、不阻断确认主流程
  EP-21-02 无快照本体 → 即时计算 is_snapshot=false；趋势空 + baseline_hint
  EP-21-05 本体不存在 → 404 NOT_FOUND
（TP-21-08 为 P2，依赖 V2-01/02，不在 M3-C 范围）

运行：Set-Location backend; python ..\\scripts\\verify_m3c_data_health.py
"""
import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "backend"))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
TMP_DATA = os.path.join(BACKEND, "data", "_m3c_verify")
shutil.rmtree(TMP_DATA, ignore_errors=True)
os.makedirs(TMP_DATA, exist_ok=True)
os.environ["DATA_DIR"] = TMP_DATA
os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
os.environ["M3C_PART_TOKEN"] = "tok-part"
os.environ.setdefault("ONTOLOGY_MOCK_MODE", "true")
from config import get_settings  # noqa: E402

get_settings.cache_clear()
RESULTS = []


def check(cid, desc, cond, detail=""):
    RESULTS.append((cid, desc, bool(cond), str(detail)[:260]))
    print(f"[{'PASS' if cond else 'FAIL'}] {cid} {desc}" + (f" | {detail}" if not cond else ""))


# ================= 外部模拟系统（可受控：read/write 计数） =================
from mocks.v2_external import ControllableMockSystem  # noqa: E402

MOCK = ControllableMockSystem(
    name="partdb", key_field="part_code",
    fields=[("part_code", "string"), ("part_name", "string"),
            ("price", "number"), ("status", "string")],
    records=[
        {"part_code": "P-101", "part_name": "MCP Bolt", "price": 99.0, "status": "active"},
        {"part_code": "P-102", "part_name": "MCP Nut", "price": 5.5, "status": "active"},
    ],
    tokens={"tok-part": {"scope": "all", "keys": []}},
)
MOCK_PORT = MOCK.start(port=0)
MOCK_EP = f"http://127.0.0.1:{MOCK_PORT}/read"
print(f"[setup] mock partdb @ {MOCK_EP}")


def _mock_write(**kv):
    body = json.dumps({"tool": "write", "params": kv}).encode("utf-8")
    req = urllib.request.Request(MOCK_EP.replace("/read", "/write"), data=body,
                                 headers={"Authorization": "Bearer tok-part",
                                          "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


# ================= 应用启动（with 模式：稳定事件循环） =================
from fastapi.testclient import TestClient  # noqa: E402
import main as app_main  # noqa: E402

TC = TestClient(app_main.app)
TC.__enter__()

J = []  # (name, payload) 收集关键响应


def post(path, body=None):
    r = TC.post(path, json=body or {})
    J.append((path, r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]))
    return r


def get(path):
    r = TC.get(path)
    J.append((path, r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]))
    return r


r = post("/api/projects?project_id=", {"name": "M3C 验证项目"})
check("setup-1", "建项目", r.status_code in (200, 201) and (r.json().get("data") or {}).get("id"), r.text[:200])
PID = r.json()["data"]["id"]
Q = f"?project_id={PID}"

ONTO = {
    "name": "M3C 零件本体",
    "objects": [
        {
            "id": "part", "name": "零件", "stable_key": "part_code",
            "attributes": [
                {"id": "part_code", "name": "料号", "data_type": "string", "required": True},
                {"id": "part_name", "name": "名称", "data_type": "string", "required": False},
                {"id": "unit_price", "name": "单价", "data_type": "float", "required": True},
                {"id": "status", "name": "状态", "data_type": "string", "required": False,
                 "constraints": {"enum": ["active", "discontinued"]}},
            ],
            "relations": [],
        },
        {
            "id": "order", "name": "订单", "stable_key": "order_code",
            "attributes": [
                {"id": "order_code", "name": "订单号", "data_type": "string", "required": True},
                {"id": "qty", "name": "数量", "data_type": "float", "required": False},
            ],
            "relations": [
                {"id": "part", "name": "物料", "target_object": "part", "cardinality": "many_to_one"},
            ],
        },
    ],
}
r = post("/api/ontologies" + Q, ONTO)
check("setup-2", "建本体(part+order)", r.status_code in (200, 201) and (r.json().get("data") or {}).get("id"), r.text[:300])
OID = r.json()["data"]["id"]
r = post(f"/api/mcp/server/publish?project_id={PID}&ontology_id={OID}")
check("setup-3", "发布本体（MCP P0-3）", (r.json().get("data") or {}).get("published") is True, r.text[:200])


def upload_and_approve(name, csv_text, field_map, object_type, ontology_id=None):
    """CSV 上传 → 候选（含批次健康卡）→ 审批，返回 (candidate, approve_resp)。"""
    r = TC.post(f"/api/ingest/upload?project_id={PID}&ontology_id={ontology_id or OID}&object_type={object_type}",
                data={"field_map": json.dumps(field_map)},
                files={"file": (name, io.BytesIO(csv_text.encode("utf-8")), "text/csv")})
    cands = (r.json().get("data") or {}).get("candidates") or []
    cand = cands[0] if cands else {}
    if not cand.get("id"):
        return None, r
    r2 = TC.post(f"/api/ingest/candidates/{cand['id']}/approve?project_id={PID}", json={})
    return cand, r2


# ---- 基线批次 A：3 零件（干净） ----
cand_a, r = upload_and_approve("baseline_parts.csv",
                               "part_code,part_name,unit_price,status\n"
                               "P-001,Bolt,10.0,active\nP-002,Nut,20.0,active\nP-003,Washer,1.5,discontinued\n",
                               {"part_code": "part_code", "part_name": "part_name",
                                "unit_price": "unit_price", "status": "status"}, "part")
check("setup-4", "基线零件批次入库(3)", r.status_code == 200 and (r.json().get("data") or {}).get("imported") == 3, r.text[:300])
check("setup-4b", "上传候选自带批次健康卡", (cand_a or {}).get("health", {}).get("batch_size") == 3,
      json.dumps((cand_a or {}).get("health"), ensure_ascii=False)[:200])

# ---- 基线批次 B：2 订单（1 关系可解析 P-001，1 指向不存在的 P-999） ----
cand_b, r = upload_and_approve("baseline_orders.csv",
                               "order_code,qty,part\nO-001,2,P-001\nO-002,5,P-999\n",
                               {"order_code": "order_code", "qty": "qty", "part": "part"}, "order")
check("setup-5", "基线订单批次入库(2)", r.status_code == 200 and (r.json().get("data") or {}).get("imported") == 2, r.text[:300])

# ---- 基线快照（TP-21-02） ----
r = post(f"/api/quality/ontologies/{OID}/evaluate" + Q)
check("TP-21-02a", "POST evaluate 落基线快照", r.status_code == 200 and (r.json().get("data") or {}).get("id"), r.text[:300])
snap1 = r.json()["data"]
check("TP-21-02b", "基线规模=5（3零件+2订单）且指标伴随规模",
      snap1.get("scale", {}).get("instance_count") == 5 and snap1.get("summary") and snap1.get("caliber"),
      json.dumps(snap1.get("scale"), ensure_ascii=False))
r = get(f"/api/quality/ontologies/{OID}/snapshots" + Q + "&limit=50")
items = (r.json().get("data") or {}).get("items") or []
check("TP-21-02c", "趋势首条 is_baseline=true", r.status_code == 200 and items and items[0].get("is_baseline") is True,
      json.dumps(items[:1], ensure_ascii=False)[:200])

# ================= TP-21-01 脏批次健康卡（入库时点） =================
# 4 行：P-003 两次（批内重复 + 与存量重复）、无料号（缺键）、P-004 status=retired（枚举出域）
r = TC.post(f"/api/ingest/upload?project_id={PID}&ontology_id={OID}&object_type=part",
            data={"field_map": json.dumps({"part_code": "part_code", "part_name": "part_name",
                                           "unit_price": "unit_price", "status": "status"})},
            files={"file": ("dirty.csv", io.BytesIO(
                b"part_code,part_name,unit_price,status\n"
                b"P-003,Washer2,2.0,active\nP-003,Washer3,2.5,active\n"
                b",NoKey,3.0,active\nP-004,Gear,4.0,retired\n"), "text/csv")})
cands = (r.json().get("data") or {}).get("candidates") or []
cand_d = cands[0] if cands else {}
h = (cand_d or {}).get("health") or {}
check("TP-21-01a", "脏批次候选带 health 健康卡", bool(h and not h.get("error")), json.dumps(h, ensure_ascii=False)[:300])
check("TP-21-01b", "缺键计数=1", h.get("key_missing_count") == 1, str(h.get("key_missing_count")))
check("TP-21-01c", "批内重复键=1 且重复组 P-003",
      h.get("key_dup_within_batch") == 1 and (h.get("key_dup_groups") or {}).get("P-003") == 2,
      json.dumps(h.get("key_dup_groups"), ensure_ascii=False))
check("TP-21-01d", "与存量重复键=[P-003]", h.get("key_dup_with_existing") == ["P-003"],
      json.dumps(h.get("key_dup_with_existing"), ensure_ascii=False))
check("TP-21-01e", "枚举出域=1（status: retired 分布）",
      h.get("enum_violation_total") == 1 and (h.get("enum_violations") or {}).get("status", {}).get("count") == 1
      and "retired" in (h.get("enum_violations") or {}).get("status", {}).get("values", []),
      json.dumps(h.get("enum_violations"), ensure_ascii=False))
check("TP-21-01f", "必填填充率=0.875（part_code 3/4 + unit_price 4/4）", h.get("required_fill_rate") == 0.875,
      json.dumps(h.get("required_detail"), ensure_ascii=False)[:200])
check("TP-21-01g", "新鲜度=上传即新（basis=now, age 0）",
      (h.get("freshness") or {}).get("basis") == "now" and (h.get("freshness") or {}).get("age_days") == 0.0,
      json.dumps(h.get("freshness"), ensure_ascii=False))
r = get(f"/api/ingest/candidates/{cand_d['id']}/health" + Q)
h2 = r.json().get("data") or {}
check("TP-21-01h", "GET 批次健康卡即时重算一致", r.status_code == 200 and h2.get("key_missing_count") == 1
      and h2.get("key_dup_within_batch") == 1 and h2.get("enum_violation_total") == 1, r.text[:200])
r = TC.post(f"/api/ingest/candidates/{cand_d['id']}/approve" + Q, json={})
data_d = r.json().get("data") or {}
r_st = get(f"/api/ingest/candidates/{cand_d['id']}" + Q)
r_st = r_st.json().get("data") or {}
check("TP-21-06a", "脏批次审批仍被 SHACL 门禁拦截（imported=0 且 violations>0）",
      data_d.get("imported") == 0 and len(data_d.get("violations") or []) > 0, json.dumps(data_d)[:300])
check("TP-21-06b", "拦截后候选仍可取且健康卡仍在（观测不阻断、主流程正常）",
      r_st.get("status") == "pending" and (r_st.get("health") or {}).get("key_missing_count") == 1,
      str(r_st.get("status")))

# ================= TP-21-07 执行通道（MCP 连接型 + 治理身份采样） =================
r = post("/api/mcp/connections" + Q, {
    "display_name": "partdb-零件连接", "transport": "streamable_http", "endpoint": MOCK_EP,
    "auth": {"type": "bearer_token", "bearer_token_ref": "${M3C_PART_TOKEN}"},
    "maps_to_ontology": {"ontology_id": OID, "object_type": "part",
                         "field_map": {"part_code": "part_code", "part_name": "part_name",
                                       "price": "unit_price", "status": "status"}},
})
check("TP-21-07a", "建 MCP 连接", r.status_code in (200, 201) and (r.json().get("data") or {}).get("connection_id"), r.text[:300])
CID = (r.json().get("data") or {}).get("connection_id")
r = post(f"/api/mcp/connections/{CID}/read" + Q, {})
cand_m_id = (r.json().get("data") or {}).get("candidate_id")
check("TP-21-07b", "MCP 读源产生候选(2)", bool(cand_m_id) and (r.json().get("data") or {}).get("record_count") == 2, r.text[:200])
r = TC.post(f"/api/ingest/candidates/{cand_m_id}/approve" + Q, json={})
check("TP-21-07c", "MCP 批次入库(2)", r.status_code == 200 and (r.json().get("data") or {}).get("imported") == 2, r.text[:300])

reads_before = MOCK.read_calls
r = post(f"/api/quality/ontologies/{OID}/evaluate" + Q)
snap2 = r.json().get("data") or {}
check("TP-21-07d", "评估落快照(规模7)", snap2.get("scale", {}).get("instance_count") == 7,
      json.dumps(snap2.get("scale"), ensure_ascii=False))
objs2 = snap2.get("objects") or {}
part2, order2 = objs2.get("part") or {}, objs2.get("order") or {}
check("TP-21-07e", "part 值通道=governance_sampled（采样2/0错）",
      part2.get("value_channel") == "governance_sampled"
      and (part2.get("sample") or {}).get("sample_count") == 2
      and not (part2.get("sample") or {}).get("errors"),
      json.dumps(part2.get("sample"), ensure_ascii=False))
check("TP-21-07f", "order 值通道=local_full（上传值全量）", order2.get("value_channel") == "local_full",
      str(order2.get("value_channel")))
ch = snap2.get("channels") or {}
check("TP-21-07g", "键/关系/新鲜度通道=local_snapshot",
      ch.get("uniqueness") == "local_snapshot" and ch.get("relation_resolution") == "local_snapshot"
      and ch.get("timeliness") == "local_snapshot", json.dumps(ch, ensure_ascii=False))
check("TP-21-07h", "评估期间真实读源（read_calls 增加）", MOCK.read_calls > reads_before,
      f"before={reads_before} after={MOCK.read_calls}")
tim = (snap2.get("summary") or {}).get("timeliness") or {}
conns = tim.get("connections") or []
check("TP-21-07i", "新鲜度按来源连接：1连接/2实例/新鲜(阈值30天)",
      len(conns) == 1 and conns[0].get("instance_count") == 2 and conns[0].get("stale") is False
      and conns[0].get("age_days") is not None and tim.get("stale_ratio") == 0.0,
      json.dumps(tim, ensure_ascii=False)[:300])
comp2, uniq2, conf2 = part2.get("completeness") or {}, part2.get("uniqueness") or {}, part2.get("conformity") or {}
check("TP-21-07j", "part 指标：键填充1.0/重复0/枚举命中1.0/SHACL违规0且可用",
      comp2.get("key_fill_rate") == 1.0 and uniq2.get("dup_rate") == 0.0
      and conf2.get("enum_hit_rate") == 1.0 and conf2.get("shacl_violation_rate") == 0.0
      and conf2.get("shacl_available") is True,
      json.dumps({k: comp2.get(k) for k in ("key_fill_rate",)}, ensure_ascii=False) + str(conf2.get("shacl_violation_rate")))
rel2 = (order2.get("completeness") or {}).get("relation_detail") or {}
check("TP-21-07k", "关系指向解析率=0.5（O-002→P-999 不可解析）",
      (order2.get("completeness") or {}).get("relation_resolve_rate") == 0.5
      and (rel2.get("part") or {}).get("resolved") == 1, json.dumps(rel2, ensure_ascii=False)[:200])

# ---- 源端值漂移：P-101 status→archived（本地副本仍 active）→ 采样后指标须反映源端最新 ----
wr = _mock_write(part_code="P-101", status="archived", price=111.0)
check("TP-21-07l", "源端漂移写入成功", wr.get("ok") is True,
      json.dumps(wr, ensure_ascii=False)[:200])
r = post(f"/api/quality/ontologies/{OID}/evaluate" + Q)
snap3 = r.json().get("data") or {}
part3 = (snap3.get("objects") or {}).get("part") or {}
conf3 = part3.get("conformity") or {}
check("TP-21-07m", "采样读源端最新值：枚举命中<1 且 status 出域含 archived",
      conf3.get("enum_hit_rate") is not None and conf3.get("enum_hit_rate") < 1.0
      and "archived" in (conf3.get("enum_detail") or {}).get("status", {}).get("out_of_enum", []),
      json.dumps(conf3.get("enum_detail"), ensure_ascii=False)[:300])
check("TP-21-07n", "SHACL 违规随源端值上升（sh:in status）且按属性细分",
      (conf3.get("shacl_violation_count") or 0) >= 1
      and (conf3.get("shacl_by_attribute") or {}).get("status", {}).get("count", 0) >= 1,
      json.dumps(conf3.get("shacl_by_attribute"), ensure_ascii=False)[:300])

# ---- 采样关闭 → source_required 显式标记（不静默、不臆测） ----
os.environ["ONTOLOGY_DATA_QUALITY_SOURCE_ENABLED"] = "false"
get_settings.cache_clear()
r = post(f"/api/quality/ontologies/{OID}/evaluate" + Q)
snap4 = r.json().get("data") or {}
part4 = (snap4.get("objects") or {}).get("part") or {}
ch4 = snap4.get("channels") or {}
check("TP-21-07o", "采样关闭：part 值通道=source_required 且通道汇总明示",
      part4.get("value_channel") == "source_required"
      and (ch4.get("value_metrics") or {}).get("source_required", 0) >= 2
      and (part4.get("sample") or {}).get("sample_count") == 0,
      json.dumps(part4.get("sample"), ensure_ascii=False)[:200])
os.environ["ONTOLOGY_DATA_QUALITY_SOURCE_ENABLED"] = "true"
get_settings.cache_clear()

# ================= TP-21-04 规模变化标注 =================
cand_c, r = upload_and_approve("grow_parts.csv",
                               "part_code,part_name,unit_price,status\nP-005,Spring,8.0,active\n",
                               {"part_code": "part_code", "part_name": "part_name",
                                "unit_price": "unit_price", "status": "status"}, "part")
check("TP-21-04a", "新增实例入库", r.status_code == 200 and (r.json().get("data") or {}).get("imported") == 1, r.text[:200])
r = post(f"/api/quality/ontologies/{OID}/evaluate" + Q)
snap5 = r.json().get("data") or {}
r = get(f"/api/quality/ontologies/{OID}/snapshots" + Q + "&limit=50")
items5 = (r.json().get("data") or {}).get("items") or []
last5 = items5[-1] if items5 else {}
check("TP-21-04b", "规模 7→8 且趋势标注 scale_changed（指标伴随规模）",
      snap5.get("scale", {}).get("instance_count") == 8 and last5.get("scale_changed") is True,
      json.dumps({k: last5.get(k) for k in ("scale_changed", "caliber_changed")}, ensure_ascii=False))
check("TP-21-04c", "未改口径的快照 caliber_changed=false", last5.get("caliber_changed") is False,
      json.dumps(last5.get("caliber"), ensure_ascii=False)[:200])

# ================= TP-21-03 口径变化（草稿本体 B：新增 sh:in 约束） =================
ONTO_B = {
    "name": "M3C 口径本体",
    "objects": [
        {
            "id": "item", "name": "条目", "stable_key": "code",
            "attributes": [
                {"id": "code", "name": "编码", "data_type": "string", "required": True},
                {"id": "name", "name": "名称", "data_type": "string", "required": False},
            ],
            "relations": [],
        },
    ],
}
r = post("/api/ontologies" + Q, ONTO_B)
OIDB = (r.json().get("data") or {}).get("id")
check("TP-21-03a", "建草稿本体B", bool(OIDB), r.text[:200])
cand_b2, r = upload_and_approve("items.csv", "code,name\nI-001,Alpha\nI-002,Beta\n",
                                {"code": "code", "name": "name"}, "item", ontology_id=OIDB)
check("TP-21-03b", "本体B入库2条", r.status_code == 200 and (r.json().get("data") or {}).get("imported") == 2, r.text[:200])
r = post(f"/api/quality/ontologies/{OIDB}/evaluate" + Q)
sb1 = r.json().get("data") or {}
check("TP-21-03c", "B 基线：无约束 → SHACL 违规率 0",
      ((sb1.get("summary") or {}).get("conformity") or {}).get("shacl_violation_rate") == 0.0,
      json.dumps((sb1.get("summary") or {}).get("conformity"), ensure_ascii=False)[:200])
r = TC.patch(f"/api/ontologies/{OIDB}/objects/item" + Q, json={
    "attributes": [
        {"id": "code", "name": "编码", "data_type": "string", "required": True},
        {"id": "name", "name": "名称", "data_type": "string", "required": False,
         "constraints": {"enum": ["Alpha"]}},
    ],
})
check("TP-21-03d", "PATCH 对象新增 sh:in 约束（200）", r.status_code == 200, r.text[:300])
r = post(f"/api/quality/ontologies/{OIDB}/evaluate" + Q)
sb2 = r.json().get("data") or {}
r = get(f"/api/quality/ontologies/{OIDB}/snapshots" + Q + "&limit=50")
items_b = (r.json().get("data") or {}).get("items") or []
last_b = items_b[-1] if items_b else {}
b_rate = ((sb2.get("summary") or {}).get("conformity") or {}).get("shacl_violation_rate")
check("TP-21-03e", "口径变化：constraint_hash 改变且趋势标注 caliber_changed",
      (sb2.get("caliber") or {}).get("constraint_hash") != (sb1.get("caliber") or {}).get("constraint_hash")
      and last_b.get("caliber_changed") is True,
      json.dumps({"h1": (sb1.get("caliber") or {}).get("constraint_hash"),
                  "h2": (sb2.get("caliber") or {}).get("constraint_hash"),
                  "flag": last_b.get("caliber_changed")}, ensure_ascii=False))
check("TP-21-03f", "违规率升（0→>0，Beta 出域）且可归因口径变化（非数据质量误报）",
      b_rate is not None and b_rate > 0 and last_b.get("caliber_changed") is True,
      f"rate={b_rate} flag={last_b.get('caliber_changed')}")

# ================= 新鲜度：陈旧判定（fetched_at 40 天前 > 阈值30） =================
from core.instance_store import get_instance_store  # noqa: E402
istore = get_instance_store(PID)
old_iso = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
n_old = 0
for rec in istore.all():
    src = (rec.get("instance") or {}).get("_source") or {}
    if src.get("connection_id") == CID:  # 源连接型按连接 id 判定（_source.kind 两通道恒 field_map）
        inst = rec["instance"]
        inst["_source"]["fetched_at"] = old_iso
        istore.update(rec["id"], {"instance": inst})
        n_old += 1
check("STALE-a", "回拨 2 条 MCP 实例 fetched_at 至 40 天前", n_old == 2, f"n={n_old}")
r = post(f"/api/quality/ontologies/{OID}/evaluate" + Q)
tim6 = ((r.json().get("data") or {}).get("summary") or {}).get("timeliness") or {}
conns6 = tim6.get("connections") or []
check("STALE-b", "陈旧判定：stale=true、age≈40天、陈旧占比=1.0",
      conns6 and conns6[0].get("stale") is True and conns6[0].get("age_days", 0) >= 39
      and tim6.get("stale_ratio") == 1.0 and tim6.get("stale_instance_count") == 2,
      json.dumps(tim6, ensure_ascii=False)[:300])

# ================= TP-21-05 MCP 工具（对象级 + 属性级钻取数据）与 REST 同口径 =================
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp_server import _register_tools, get_mcp_server  # noqa: E402

tmp_srv = MCPServer(name="m3c-mcp")
_register_tools(get_mcp_server(), PID, tmp_srv)
qtool = tmp_srv._tool_manager._tools["quality_health"].fn
mcp_payload = json.loads(qtool(ontology_id=OID))
mcp_health = mcp_payload.get("health") or {}
mcp_objs = mcp_health.get("objects") or {}
mcp_part = mcp_objs.get("part") or {}
check("TP-21-05a", "MCP quality_health 返回健康（对象级）+ 规模 + 趋势",
      mcp_health.get("scale", {}).get("instance_count") == 8 and "part" in mcp_objs
      and (mcp_payload.get("trend") or {}).get("items"),
      json.dumps({k: mcp_health.get(k) for k in ("is_snapshot",)}, ensure_ascii=False))
check("TP-21-05b", "MCP 返回属性级钻取数据（必填/关系/SHACL/枚举 按属性）",
      (mcp_part.get("completeness") or {}).get("required_detail")
      and (mcp_part.get("conformity") or {}).get("enum_detail")
      and (mcp_part.get("conformity") or {}).get("shacl_by_attribute") is not None,
      json.dumps({"required_detail_keys": list((mcp_part.get("completeness") or {}).get("required_detail", {}).keys())},
                 ensure_ascii=False))
r = get(f"/api/quality/ontologies/{OID}/health" + Q)
rest_health = r.json().get("data") or {}
check("TP-21-05c", "REST /health 与 MCP 同口径（summary 一致）",
      r.status_code == 200 and (rest_health.get("summary") or {}).get("completeness")
      == (mcp_health.get("summary") or {}).get("completeness"),
      json.dumps(rest_health.get("summary", {}).get("completeness"), ensure_ascii=False))

# ================= EP-21-05 本体不存在 → 404 =================
r = get(f"/api/quality/ontologies/no-such-ontology/health" + Q)
check("EP-21-05a", "REST health 404 NOT_FOUND", r.status_code == 404
      and (r.json().get("error") or {}).get("code") == "NOT_FOUND", r.text[:200])
try:
    _err = json.loads(qtool(ontology_id="no-such-ontology"))
    _err_msg = json.dumps(_err, ensure_ascii=False)
    check("EP-21-05b", "MCP 404 返回错误结构（NOT_FOUND/NOT_PUBLISHED 均视为未发布/不存在）",
          _err.get("isError") is True or ("NOT_FOUND" in _err_msg) or ("NOT_PUBLISHED" in _err_msg),
          _err_msg[:200])
except Exception as e:  # noqa: BLE001
    check("EP-21-05b", "MCP 404 返回错误结构（NOT_FOUND/NOT_PUBLISHED 均视为未发布/不存在）",
          ("NOT_FOUND" in str(e)) or ("NOT_PUBLISHED" in str(e)), str(e)[:200])

# ================= EP-21-02 无快照本体：即时计算 + 趋势空 + baseline_hint =================
r = post("/api/ontologies" + Q, {
    "name": "M3C 空本体",
    "objects": [
        {"id": "ghost", "name": "幽灵对象", "stable_key": "g_code",
         "attributes": [{"id": "g_code", "name": "编码", "data_type": "string", "required": True}],
         "relations": []},
    ],
})
OIDC = (r.json().get("data") or {}).get("id")
r = get(f"/api/quality/ontologies/{OIDC}/health" + Q)
live = r.json().get("data") or {}
check("EP-21-02a", "无快照：即时计算 is_snapshot=false 且空规模不崩",
      r.status_code == 200 and live.get("is_snapshot") is False
      and live.get("scale", {}).get("instance_count") == 0 and "ghost" in (live.get("objects") or {}),
      r.text[:300])
r = get(f"/api/quality/ontologies/{OIDC}/snapshots" + Q)
d_empty = r.json().get("data") or {}
check("EP-21-02b", "无快照：趋势空 + baseline_hint 引导建基线",
      d_empty.get("items") == [] and d_empty.get("total") == 0 and bool(d_empty.get("baseline_hint")),
      json.dumps(d_empty, ensure_ascii=False)[:200])
r = get(f"/api/quality/ontologies/{OIDC}/snapshots" + Q)
check("EP-21-02c", "即时计算不落快照（仍无快照）", ((r.json().get("data") or {}).get("total") or 0) == 0, "")

# ================= EP-21-01 健康卡计算失败：显式 error，不阻断主流程 =================
from core.storage import JsonStore  # noqa: E402
cstore = JsonStore("ingest_candidates", PID)
drec = cstore.get(cand_d["id"])
cstore.update(cand_d["id"], {"instances": {"corrupted": True}})
r = get(f"/api/ingest/candidates/{cand_d['id']}" + Q)
check("EP-21-01a", "候选损坏后主流程（取候选）仍 200", r.status_code == 200 and r.json().get("success"), r.text[:200])
r = get(f"/api/ingest/candidates/{cand_d['id']}/health" + Q)
d_err = r.json().get("data") or {}
check("EP-21-01b", "健康卡计算失败：200 + 显式 error（非 500）",
      r.status_code == 200 and bool(d_err.get("error")), r.text[:200])
cstore.update(cand_d["id"], {"instances": drec.get("instances")})  # 复原

# ================= TP-21-02 可归因：audit quality_evaluated =================
import audit  # noqa: E402
aq = audit.query(action="quality_evaluated", project_id=PID)
a_items = (aq or {}).get("items") or []
check("TP-21-02d", "审计留痕 quality_evaluated（可归因）", len(a_items) >= 5, f"n={len(a_items)}")

# ================= 汇总 =================
passed = sum(1 for _, _, ok, _ in RESULTS if ok)
failed = [x for x in RESULTS if not x[2]]
print(f"\n===== M3-C V2-21 验收结果: {passed}/{len(RESULTS)} PASS =====")
for cid, desc, ok, detail in failed:
    print(f"  FAIL {cid} {desc} | {detail}")
try:
    with open(os.path.join(SCRIPT_DIR, "_m3c_payloads.json"), "w", encoding="utf-8") as f:
        json.dump([{"call": n, "resp": p} for n, p in J], f, ensure_ascii=False, indent=1, default=str)
except Exception:  # noqa: BLE001
    pass
time.sleep(0.1)
sys.exit(1 if failed else 0)
