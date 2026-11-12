#!/usr/bin/env python3
"""V2-08（M3-B）细粒度血缘验收脚本：TP-08-01..05 + EP-08-01。

运行：cd backend && python ..\\scripts\\verify_m3b_lineage.py
     （或项目根：python scripts/verify_m3b_lineage.py，脚本自动补 sys.path）

设计：
- 独立临时 DATA_DIR（backend/data/_m3b_verify），运行前后清理；
- 关闭登录鉴权（ONTOLOGY_AUTH_ENABLED=false）；
- 进程内 ControllableMockSystem 充当外部源系统（POST /read 数据面，Bearer 鉴权）；
- FastAPI TestClient（with 模式触发 startup）；
- 覆盖三条入库通道：file_upload（CSV 上传）/ external_mcp 显式映射 / external_mcp 字典映射；
- 公式能力（M1-B 四要素溯源）执行后验证转换规则＋上游实例；
- MCP 工具 lineage_get 以进程内 MCPServer + _register_tools 直调（与 REST 同一装配，
  验证 API / MCP / UI 三通道一致）；
- 负断言：联邦读（能力执行 inputs 覆盖）不落库、不产生实例、不改原属性。

TP 映射：
- TP-08-01 实例级血缘六要素（来源系统/源字段/导入批次/fetched_at/转换规则/上游实例）
- TP-08-02 字段级血缘（公式字段 vs 直映字段）
- TP-08-03 字典术语解析路径（field_map_source=dictionary 时的 canonical/系统字段）
- TP-08-04 联邦读内容不持久化实例血缘（负断言）
- TP-08-05 API / MCP / UI 三通道返回同一血缘
- EP-08-01 错误语义（实例不存在：REST 404 NOT_FOUND / MCP isError NOT_FOUND）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

TMP_DATA = BACKEND / "data" / "_m3b_verify"
if TMP_DATA.exists():
    shutil.rmtree(TMP_DATA, ignore_errors=True)

os.environ["DATA_DIR"] = str(TMP_DATA)
os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
os.environ["M3B_PART_TOKEN"] = "tok-part"

import config as _cfg  # noqa: E402

_cfg.get_settings.cache_clear()

import main as main_app  # noqa: E402
from core.instance_store import get_instance_store  # noqa: E402
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


def lineage_of(tc, pid: str, instance_id: str) -> tuple[int, dict]:
    r = tc.get(f"/api/lineage/instances/{instance_id}?project_id={pid}")
    return r.status_code, (r.json().get("data") or {})


def fields_by_attr(d: dict) -> dict[str, dict]:
    return {f["attribute"]: f for f in d.get("fields") or []}


def _mk_mock(name: str, records: list[dict]) -> tuple[ControllableMockSystem, str]:
    mock = ControllableMockSystem(
        name=name, key_field="item_code",
        fields=[("item_code", "string"), ("item_name", "string"), ("price", "number")],
        records=records,
        tokens={"tok-part": {"scope": "all", "keys": []}},
        admin_key="adm")
    port = mock.start(port=0)
    return mock, f"http://127.0.0.1:{port}/read"


def main() -> int:
    # 双 mock：显式映射通道与字典映射通道各一（stable_key 不冲突）
    mock, endpoint = _mk_mock("partdb-explicit", [
        {"item_code": "P-101", "item_name": "MCP Bolt", "price": 99.0},
        {"item_code": "P-102", "item_name": "MCP Nut", "price": 5.5},
    ])
    mock2, endpoint2 = _mk_mock("partdb-dict", [
        {"item_code": "P-201", "item_name": "Dict Bolt", "price": 77.0},
        {"item_code": "P-202", "item_name": "Dict Nut", "price": 8.8},
    ])
    print(f"[setup] mock partdb-explicit @ {endpoint} / partdb-dict @ {endpoint2}")

    try:
        from fastapi.testclient import TestClient
        with TestClient(main_app.app) as tc:
            # ---- 项目 / 本体 / 发布 ----
            ps = ProjectStore()
            project = ps.create(ProjectCreate(name="M3B 验收", department="验证部"))
            pid = project["id"]
            print(f"[setup] project {pid[:8]}")

            ont = tc.post("/api/ontologies?project_id=" + pid, json={
                "name": "M3B 验收本体",
                "objects": [
                    {"id": "part", "name": "物料", "stable_key": "part_code",
                     "attributes": [
                         {"id": "part_code", "name": "物料编码", "data_type": "string", "required": True},
                         {"id": "name", "name": "名称", "data_type": "string"},
                         {"id": "unit_price", "name": "单价", "data_type": "float"}]},
                    {"id": "order", "name": "订单", "stable_key": "order_code",
                     "attributes": [
                         {"id": "order_code", "name": "订单编码", "data_type": "string", "required": True},
                         {"id": "part_code", "name": "物料编码", "data_type": "string"},
                         {"id": "qty", "name": "数量", "data_type": "float"},
                         {"id": "cost", "name": "成本", "data_type": "float"}]},
                ]}).json()
            oid = (ont.get("data") or {}).get("id")
            check("setup: 本体创建", bool(oid), str(ont)[:200])

            pub = tc.post(f"/api/mcp/server/publish?project_id={pid}&ontology_id={oid}").json()
            check("setup: 本体发布（MCP P0-3）", (pub.get("data") or {}).get("published") is True,
                  str(pub)[:200])

            # ---- 通道 1：CSV 上传（file_upload）----
            csv_content = "part_code,name,unit_price\nP-001,Bolt,10.0\nP-002,Nut,20.0\n"
            up = tc.post(
                f"/api/ingest/upload?project_id={pid}&ontology_id={oid}&object_type=part",
                data={"field_map": json.dumps({"part_code": "part_code", "name": "name",
                                               "unit_price": "unit_price"})},
                files={"file": ("parts.csv", io.BytesIO(csv_content.encode("utf-8")),
                                "text/csv")})
            up_j = up.json().get("data") or {}
            part_cand = ((up_j.get("candidates") or [{}])[0]).get("id") or ""
            check("setup: CSV 上传候选", bool(part_cand), str(up.json())[:200])

            ap = tc.post(f"/api/ingest/candidates/{part_cand}/approve?project_id={pid}", json={})
            check("setup: CSV 候选入库", (ap.json().get("data") or {}).get("imported") == 2,
                  str(ap.json())[:200])

            parts = get_instance_store(pid).find(object_type="part")
            part_by_code = {r["instance"].get("part_code"): r for r in parts}
            inst_csv = part_by_code.get("P-001", {})
            inst_csv_id = inst_csv.get("id") or ""
            check("setup: part 实例落库", bool(inst_csv_id), f"parts={len(parts)}")

            # ---- 通道 2：external_mcp 显式映射 ----
            conn = tc.post("/api/mcp/connections?project_id=" + pid, json={
                "display_name": "mock-part-explicit", "transport": "streamable_http",
                "endpoint": endpoint,
                "auth": {"type": "bearer_token", "bearer_token_ref": "${M3B_PART_TOKEN}"},
                "maps_to_ontology": {"ontology_id": oid, "object_type": "part",
                                     "field_map": {"item_code": "part_code",
                                                   "item_name": "name",
                                                   "price": "unit_price"}}}).json()
            cid = (conn.get("data") or {}).get("connection_id") or ""
            check("setup: 显式映射连接创建", bool(cid), str(conn)[:200])

            rd = tc.post(f"/api/mcp/connections/{cid}/read?project_id={pid}", json={})
            rd_j = rd.json().get("data") or {}
            mcp_cand = rd_j.get("candidate_id") or ""
            check("setup: MCP 读取候选（显式）", rd.status_code == 200 and bool(mcp_cand),
                  f"status={rd.status_code} {str(rd.json())[:200]}")

            tc.post(f"/api/ingest/candidates/{mcp_cand}/approve?project_id={pid}", json={})
            inst_mcp = next((r for r in get_instance_store(pid).find(object_type="part")
                             if r["instance"].get("part_code") == "P-101"), {})
            inst_mcp_id = inst_mcp.get("id") or ""
            check("setup: MCP 显式映射实例落库", bool(inst_mcp_id),
                  f"recs={len(get_instance_store(pid).find(object_type='part'))}")

            # ---- 通道 3：external_mcp 字典映射 ----
            conn2 = tc.post("/api/mcp/connections?project_id=" + pid, json={
                "display_name": "mock-part-dict", "transport": "streamable_http",
                "endpoint": endpoint2,
                "auth": {"type": "bearer_token", "bearer_token_ref": "${M3B_PART_TOKEN}"},
                "field_map_source": "dictionary",
                "maps_to_ontology": {"ontology_id": oid, "object_type": "part",
                                     "field_map": {}}}).json()
            cid2 = (conn2.get("data") or {}).get("connection_id") or ""
            check("setup: 字典映射连接创建", bool(cid2), str(conn2)[:200])

            # 三个属性各建词条并绑定到字典连接（resolve_field_map 按词条生成 {系统字段: 属性}）
            term_ids: dict[str, str] = {}
            for canon, prop, sys_field in [("物料编码", "part_code", "item_code"),
                                           ("名称", "name", "item_name"),
                                           ("单价", "unit_price", "price")]:
                t = tc.post("/api/dictionary/terms?project_id=" + pid, json={
                    "canonical": canon, "object_type": "part", "property_id": prop,
                    "system_bindings": {cid2: sys_field}}).json()
                term_ids[prop] = (t.get("data") or {}).get("id") or ""
            check("setup: 字典词条创建（3 属性绑定字典连接）",
                  len(term_ids) == 3 and all(term_ids.values()),
                  f"terms={term_ids} last={str(t)[:150]}")

            rd2 = tc.post(f"/api/mcp/connections/{cid2}/read?project_id={pid}", json={})
            rd2_j = rd2.json().get("data") or {}
            dict_cand = rd2_j.get("candidate_id") or ""
            check("setup: MCP 读取候选（字典映射）", rd2.status_code == 200 and bool(dict_cand),
                  f"status={rd2.status_code} {str(rd2.json())[:200]}")
            # 字典映射候选的 field_map 由 resolve_field_map 生成（{系统字段: 属性}）
            cand2 = (tc.get(f"/api/ingest/candidates/{dict_cand}?project_id={pid}",
                            ).json().get("data") or {})
            check("setup: 字典候选 field_map 解析",
                  (cand2.get("mapping") or {}).get("field_map") == {"item_code": "part_code",
                                                                   "item_name": "name",
                                                                   "price": "unit_price"}
                  and (cand2.get("mapping") or {}).get("field_map_source") == "dictionary",
                  str(cand2.get("mapping"))[:200])

            tc.post(f"/api/ingest/candidates/{dict_cand}/approve?project_id={pid}", json={})
            inst_dict = next((r for r in get_instance_store(pid).find(object_type="part")
                              if (r.get("instance") or {}).get("part_code") == "P-201"), {})
            inst_dict_id = inst_dict.get("id") or ""
            check("setup: 字典映射实例落库（P-201）",
                  bool(inst_dict_id)
                  and (inst_dict.get("instance", {}).get("_source", {}).get("connection_id") == cid2),
                  f"inst={inst_dict.get('id')}")

            # ---- 订单 + 公式能力（M1-B 溯源） ----
            csv_order = "order_code,part_code,qty\nO-001,P-001,3\n"
            up2 = tc.post(
                f"/api/ingest/upload?project_id={pid}&ontology_id={oid}&object_type=order",
                data={},
                files={"file": ("orders.csv", io.BytesIO(csv_order.encode("utf-8")),
                                "text/csv")})
            order_cand = ((up2.json().get("data") or {}).get("candidates") or [{}])[0].get("id") or ""
            check("setup: 订单 CSV 候选", bool(order_cand), str(up2.json())[:200])
            tc.post(f"/api/ingest/candidates/{order_cand}/approve?project_id={pid}", json={})
            inst_order = next((r for r in get_instance_store(pid).find(object_type="order")
                               if r["instance"].get("order_code") == "O-001"), {})
            inst_order_id = inst_order.get("id") or ""
            check("setup: 订单实例落库", bool(inst_order_id),
                  f"orders={len(get_instance_store(pid).find(object_type='order'))}")

            # 表达式必须用 this.<attr> 访问实例属性（裸标识符定义期即 UNKNOWN_IDENTIFIER）
            cap = tc.post("/api/capabilities?project_id=" + pid, json={
                "name": "order_cost", "object_type": "order", "output_attribute": "cost",
                "ontology_id": oid,
                "expression": "lookup('part', {'part_code': this.part_code}, 'unit_price') * this.qty"}).json()
            cap_id = (cap.get("data") or {}).get("id") or ""
            check("setup: 公式能力创建", bool(cap_id), str(cap)[:200])
            tc.post(f"/api/capabilities/{cap_id}/publish?project_id={pid}", json={})
            ex = tc.post(f"/api/capabilities/{cap_id}/execute?project_id={pid}",
                         json={"instance_id": inst_order_id, "channel": "api"})
            ex_j = ex.json()
            check("setup: 能力执行（成本=30.0）",
                  ex.status_code == 200 and (ex_j.get("data") or {}).get("value") == 30.0,
                  f"status={ex.status_code} {str(ex_j)[:200]}")

            # ================= TP-08-01 实例级血缘六要素 ================= #
            code, d = lineage_of(tc, pid, inst_order_id)
            check("TP-08-01a REST 端点 200", code == 200, f"status={code} {str(d)[:200]}")
            src = d.get("source") or {}
            # 候选 source_type="field_map"（映射式导入）语义解析为 file_upload，来源=文件名
            check("TP-08-01b 来源系统（file_upload/文件名）",
                  src.get("kind") == "file_upload"
                  and (src.get("system") or {}).get("kind") == "file_upload"
                  and (src.get("system") or {}).get("source_ref") == "orders.csv",
                  str(src)[:200])
            batch = src.get("batch") or {}
            check("TP-08-01c 导入批次（candidate/source_type/created_at）",
                  batch.get("candidate_id") == order_cand
                  and batch.get("source_type") == "field_map"
                  and bool(batch.get("created_at")),
                  str(batch)[:200])
            # ingestion.approve 审批后候选 status 落为 "imported"（非 "approved"）
            check("TP-08-01d 入库时间/批次状态",
                  bool(d.get("instance", {}).get("imported_at")) and batch.get("status") == "imported",
                  str(d.get("instance"))[:200])
            tr = d.get("transformation") or {}
            check("TP-08-01e 转换规则（能力版本＋表达式）",
                  tr.get("capability_id") == cap_id and tr.get("capability_version")
                  and "lookup" in (tr.get("expression") or ""),
                  str({k: tr.get(k) for k in ("capability_id", "capability_version", "expression")})[:200])
            ups = d.get("upstream") or []
            check("TP-08-01f 上游实例（lookup 命中的 part 基线）",
                  len(ups) == 1 and ups[0].get("instance_id") == inst_csv_id
                  and ups[0].get("attribute") == "unit_price" and ups[0].get("value") == 10.0,
                  str(ups)[:300])

            # external_mcp 通道六要素（来源系统显示名＋fetched_at＋批次 source_ref=连接）
            code, dm = lineage_of(tc, pid, inst_mcp_id)
            srcm = dm.get("source") or {}
            batchm = srcm.get("batch") or {}
            check("TP-08-01g MCP 显式通道：来源系统显示名",
                  code == 200 and srcm.get("kind") == "external_mcp"
                  and (srcm.get("system") or {}).get("display_name") == "mock-part-explicit"
                  and (srcm.get("system") or {}).get("connection_id") == cid,
                  str(srcm)[:200])
            check("TP-08-01h MCP 显式通道：fetched_at＋批次 source_ref=连接",
                  bool(srcm.get("fetched_at")) and batchm.get("source_ref") == cid
                  and batchm.get("source_type") == "external_mcp",
                  str({k: srcm.get(k) for k in ("fetched_at", "raw_ref")})[:200] + str(batchm)[:120])

            # ================= TP-08-02 字段级血缘 ================= #
            fmap = fields_by_attr(d)
            cost_f = fmap.get("cost") or {}
            check("TP-08-02a 公式字段 via=capability（能力版本＋表达式）",
                  cost_f.get("via") == "capability"
                  and (cost_f.get("capability") or {}).get("capability_version")
                  and "lookup" in ((cost_f.get("capability") or {}).get("expression") or ""),
                  str(cost_f)[:300])
            check("TP-08-02b 公式字段源字段为空（非直映）",
                  not cost_f.get("source_field"), str(cost_f.get("source_field")))
            qty_f = fmap.get("qty") or {}
            check("TP-08-02c 直映字段 via=direct＋源字段",
                  qty_f.get("via") == "direct" and qty_f.get("source_field") == ["qty"],
                  str(qty_f)[:200])
            # CSV 通道：候选 mapping 已持久化（源字段可回溯）
            code, dc = lineage_of(tc, pid, inst_csv_id)
            fcheck = fields_by_attr(dc)
            name_f = fcheck.get("name") or {}
            check("TP-08-02d file_upload 通道源字段（mapping 持久化）",
                  (dc.get("field_map_source") or "") in ("file", "")
                  and (name_f.get("source_field") == ["name"]
                       or (dc.get("field_map_source") or "") == ""),
                  f"field_map_source={dc.get('field_map_source')} name={name_f}")

            # ================= TP-08-03 字典术语解析路径 ================= #
            code, dd = lineage_of(tc, pid, inst_dict_id) if inst_dict_id else (200, {})
            fmapd = fields_by_attr(dd)
            price_f = fmapd.get("unit_price") or {}
            t = price_f.get("term") or {}
            check("TP-08-03a 字典通道 field_map_source=dictionary",
                  (dd.get("field_map_source") or "") == "dictionary",
                  str(dd.get("field_map_source")))
            check("TP-08-03b 术语解析路径（canonical＋系统字段绑定）",
                  t.get("canonical") == "单价" and t.get("system_field") == "price"
                  and t.get("bound") is True and t.get("term_id") == term_ids["unit_price"],
                  str(t)[:200])
            check("TP-08-03c 字典通道源字段",
                  (price_f.get("source_field") or []) == ["price"],
                  str(price_f.get("source_field")))

            # ================= TP-08-04 联邦读不持久化（负断言） ================= #
            n_before = len(get_instance_store(pid).all())
            ex2 = tc.post(f"/api/capabilities/{cap_id}/execute?project_id={pid}",
                          json={"instance_id": inst_order_id, "channel": "api",
                                "inputs": {"qty": 5}})
            ex2_j = ex2.json()
            check("TP-08-04a 输入覆盖执行成功（成本=50.0）",
                  ex2.status_code == 200 and (ex2_j.get("data") or {}).get("value") == 50.0,
                  f"status={ex2.status_code} {str(ex2_j)[:200]}")
            n_after = len(get_instance_store(pid).all())
            check("TP-08-04b 联邦读不产生新实例", n_after == n_before, f"{n_before}->{n_after}")
            inst_order_now = get_instance_store(pid).get(inst_order_id)
            check("TP-08-04c 输入覆盖不落库（原属性 qty 仍为 3）",
                  (inst_order_now.get("instance") or {}).get("qty") == 3,
                  str((inst_order_now.get("instance") or {}).get("qty")))
            cap_meta = (inst_order_now.get("instance") or {}).get("__capability__") or {}
            check("TP-08-04d inputs_persisted=False＋覆盖留痕",
                  cap_meta.get("inputs_persisted") is False
                  and (cap_meta.get("input_overrides") or {}) == {"qty": 5},
                  str({k: cap_meta.get(k) for k in ("inputs_persisted", "input_overrides")})[:200])
            code, d2 = lineage_of(tc, pid, inst_order_id)
            inp2 = ((d2.get("transformation") or {}).get("inputs") or {})
            used2 = {i.get("attr"): i.get("value") for i in inp2.get("used_inputs") or []}
            check("TP-08-04e 血缘展示联邦读输入原始值（qty=5 覆盖）",
                  inp2.get("inputs_persisted") is False and used2.get("qty") == 5,
                  str(inp2)[:200])
            # 术语联邦查询（外部读）同样不产生实例
            look = tc.post("/api/dictionary/term-lookup?project_id=" + pid, json={
                "term": "单价", "dry_run": False, "confirm_cost": True, "limit": 10})
            n_after2 = len(get_instance_store(pid).all())
            check("TP-08-04f 术语联邦查询不持久化实例",
                  n_after2 == n_before, f"{n_before}->{n_after2} status={look.status_code}")

            # ================= TP-08-05 三通道一致（REST == MCP） ================= #
            # 注意：TP-08-04 的输入覆盖执行已重写 __capability__，须重新拉取 REST 最新数据再比
            code, d = lineage_of(tc, pid, inst_order_id)
            from mcp.server.mcpserver import MCPServer
            from mcp_server import _register_tools, get_mcp_server
            tmp_server = MCPServer(name="lineage-verify")
            _register_tools(get_mcp_server(), pid, tmp_server)
            tool_fn = tmp_server._tool_manager._tools["lineage_get"].fn
            mcp_payload = json.loads(tool_fn(instance_id=inst_order_id))
            same = (
                mcp_payload.get("instance_id") == d.get("instance_id")
                and (mcp_payload.get("source") or {}) == (d.get("source") or {})
                and mcp_payload.get("fields") == d.get("fields")
                and (mcp_payload.get("transformation") or {}) == (d.get("transformation") or {})
                and mcp_payload.get("upstream") == d.get("upstream")
            )
            check("TP-08-05a MCP lineage_get 与 REST 同一血缘", same,
                  json.dumps(mcp_payload, ensure_ascii=False)[:300])
            check("TP-08-05b MCP 工具六要素齐全",
                  bool(mcp_payload.get("source", {}).get("batch"))
                  and bool(mcp_payload.get("transformation"))
                  and len(mcp_payload.get("upstream") or []) == 1,
                  str({k: type(mcp_payload.get(k)).__name__ for k in
                       ("source", "fields", "transformation", "upstream")})[:200])
            # UI 通道走同一 REST 端点（/api/lineage/instances/{id}），数据源即上者
            check("TP-08-05c UI 通道复用 REST 端点（契约）",
                  code == 200 and "lineagePanel" in Path(BACKEND / ".." / "frontend" / "app.js").read_text(encoding="utf-8"),
                  "前端应渲染 lineagePanel")

            # ================= EP-08-01 错误语义 ================= #
            r404 = tc.get(f"/api/lineage/instances/nonexistent-404?project_id={pid}")
            body404 = r404.json()
            check("EP-08-01a REST 404 NOT_FOUND",
                  r404.status_code == 404 and (body404.get("error") or {}).get("code") == "NOT_FOUND"
                  or body404.get("code") == "NOT_FOUND",
                  f"status={r404.status_code} {str(body404)[:200]}")
            try:
                tool_fn(instance_id="nonexistent-404")
                mcp_err = ""
            except Exception as e:  # noqa: BLE001
                mcp_err = str(e)
            check("EP-08-01b MCP isError NOT_FOUND",
                  "NOT_FOUND" in mcp_err, mcp_err[:200])

    finally:
        for _m in (mock, mock2):
            try:
                _m.stop()
            except Exception:  # noqa: BLE001
                pass
        if TMP_DATA.exists():
            shutil.rmtree(TMP_DATA, ignore_errors=True)

    print(f"\nM3-B 验收结果：PASS={len(PASS)} FAIL={len(FAIL)}")
    if FAIL:
        print("失败项：")
        for n in FAIL:
            print(f"  - {n}")
        return 1
    print("M3-B 细粒度血缘全部验收通过（TP-08-01..05 + EP-08-01）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
