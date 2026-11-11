"""M2-D（V2-09 完整审计）验收：TC-09-01~04 + TCE-09-01。

临时 DATA_DIR + 真实 JWT（鉴权启用态）。
覆盖操作留痕：ontology_create/update/object_update/delete/publish、
ingest_upload/approve、instance_delete、action_execute/writeback、
changeset_submit/reject；before/after 可见；导出与检索一致；
非 admin 403 留痕；篡改 PUT/PATCH/DELETE → 405 AUDIT_IMMUTABLE 留痕（INV-4）。

运行：PYTHONUTF8=1 python scripts/verify_m2d_audit.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

DATA_DIR = os.path.abspath(os.path.join(BACKEND, "data", "_m2d_verify"))
shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["DATA_DIR"] = DATA_DIR
os.environ["ONTOLOGY_PBKDF2_ITERATIONS"] = "10000"
os.environ["CREDENTIAL_ENCRYPTION_ENABLED"] = "0"

from config import get_settings  # noqa: E402

get_settings.cache_clear()

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} :: {detail}")


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def base_model() -> dict:
    return {
        "name": "M2D 基线本体",
        "description": "V2-09 审计验收",
        "objects": [
            {
                "id": "obj_part", "name": "部件", "business_definition": "生产部件定义",
                "attributes": [
                    {"id": "pn", "name": "部件号", "data_type": "string", "required": True},
                    {"id": "cost", "name": "成本", "data_type": "float",
                     "constraints": {"maximum": 1000}},
                    {"id": "status", "name": "状态", "data_type": "string",
                     "constraints": {"enum": ["A", "B"]}},
                ],
                "relations": [],
            },
        ],
        "constraints": [],
    }


def main() -> int:
    import auth as auth_svc
    from core.project_store import ProjectStore
    from schemas.project import ProjectCreate

    auth_svc.create_user("m2d_admin", "admin-pass-123", role="admin", tenant_id="t1")
    auth_svc.create_user("carol", "carol-pass-123", role="modeler", tenant_id="t1")
    auth_svc.create_user("dan", "dan-pass-123", role="viewer", tenant_id="t1")

    ProjectStore().create(ProjectCreate(name="M2D Verify", description="V2-09 验收"), tenant_id="t1")
    p1 = next(p["id"] for p in ProjectStore().list() if p.get("tenant_id") == "t1")
    P = {"project_id": p1}

    from fastapi.testclient import TestClient
    import main as main_mod

    def login(username: str, password: str) -> TestClient:
        tc = TestClient(main_mod.app)
        r = tc.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"login {username}: {r.status_code} {r.text[:200]}"
        return tc

    tc_admin = login("m2d_admin", "admin-pass-123")
    tc_carol = login("carol", "carol-pass-123")
    tc_dan = login("dan", "dan-pass-123")

    def audit_items(**kw) -> list[dict]:
        r = tc_admin.get("/api/audit", params={"project_id": p1, "page_size": 500, **kw})
        assert r.status_code == 200, r.text[:200]
        return r.json()["data"]["items"]

    def find_item(action: str, target_id: str | None = None) -> dict | None:
        its = audit_items(action=action)
        if target_id:
            its = [i for i in its if i.get("target_id") == target_id]
        return its[0] if its else None

    # ---------- 操作链：建模 ----------
    banner("操作链：建模/更新/对象更新/发布")
    r = tc_carol.post("/api/ontologies", params=P, json=base_model())
    oid = r.json().get("data", {}).get("id")
    check("创建本体（carol/modeler）", r.status_code == 201 and bool(oid), r.text[:200])
    ci = find_item("ontology_create", oid)
    check("ontology_create 留痕（actor=carol）", ci is not None and ci.get("actor") == "carol", str(ci)[:200])

    m = base_model()
    m["name"] = "M2D 基线本体 v2"
    m["description"] = "改名审计"
    r = tc_carol.put(f"/api/ontologies/{oid}", params=P, json=m)
    check("PUT 改名（draft 可编辑）", r.status_code == 200, r.text[:200])
    ui = find_item("ontology_update", oid)
    check("ontology_update 留痕", ui is not None, str(ui)[:200])
    check("before/after 名字可见且不同",
          (ui or {}).get("before", {}).get("name") == "M2D 基线本体"
          and (ui or {}).get("after", {}).get("name") == "M2D 基线本体 v2",
          f"before={str((ui or {}).get('before'))[:100]} after={str((ui or {}).get('after'))[:100]}")

    attrs = m["objects"][0]["attributes"] + [{"id": "note", "name": "备注", "data_type": "string"}]
    r = tc_carol.patch(f"/api/ontologies/{oid}/objects/obj_part", params=P,
                       json={"attributes": attrs})
    check("PATCH 加 note 字段", r.status_code == 200, r.text[:200])
    # 埋点 target_id=对象 id（obj_part），detail 带 ontology_id 回溯
    oi = find_item("ontology_object_update", "obj_part")
    check("ontology_object_update 留痕（target=obj_part, detail.ontology_id 回溯）",
          oi is not None and (oi or {}).get("detail", {}).get("ontology_id") == oid, str(oi)[:200])

    r = tc_carol.post(f"/api/ontologies/{oid}/publish", params=P)
    check("发布本体", r.status_code == 200, r.text[:200])
    pi = find_item("ontology_publish", oid)
    check("ontology_publish 留痕 before.draft→after.active",
          (pi or {}).get("before", {}).get("status") == "draft"
          and (pi or {}).get("after", {}).get("status") == "active",
          str(pi)[:200])

    # ---------- 操作链：入库（上传→审批→实例删除） ----------
    banner("操作链：CSV 上传/审批/实例删除")
    csv_bytes = "pn,cost\nP001,100\n".encode("utf-8")
    r = tc_carol.post(
        "/api/ingest/upload",
        params={**P, "ontology_id": oid, "object_type": "obj_part"},
        files={"file": ("parts.csv", csv_bytes, "text/csv")},
    )
    cands = (r.json().get("data") or {}).get("candidates") or []
    cid = cands[0]["id"] if cands else None
    check("CSV 上传生成候选", r.status_code in (200, 201) and bool(cid), r.text[:200])
    upi = find_item("ingest_upload")
    check("ingest_upload 留痕（actor=carol）", upi is not None and upi.get("actor") == "carol",
          str(upi)[:200])

    r = tc_admin.post(f"/api/ingest/candidates/{cid}/approve", params=P)
    d = r.json().get("data") or {}
    check("admin 审批候选→imported", r.status_code == 200 and d.get("imported") == 1, r.text[:200])
    ai = find_item("ingest_approve", cid)
    check("ingest_approve 留痕 before/after 状态",
          ai is not None
          and (ai.get("before") or {}).get("status") in (None, "pending")
          and (ai.get("after") or {}).get("status") == "imported",
          json.dumps(ai, ensure_ascii=False)[:300] if ai else "not found")

    r = tc_carol.get("/api/ingest/instances", params={**P, "ontology_id": oid,
                                                      "object_type": "obj_part", "pageSize": 10})
    items = r.json().get("data", {}).get("items") or []
    iid = items[0]["id"] if items else None
    check("导入产生实例", r.status_code == 200 and bool(iid), r.text[:200])

    r = tc_carol.delete(f"/api/ingest/instances/{iid}", params=P)
    check("删除实例（上传者本人）", r.status_code == 200, r.text[:200])
    di = find_item("instance_delete", iid)
    check("instance_delete 留痕（actor=carol）", di is not None and di.get("actor") == "carol",
          str(di)[:200])

    # ---------- 操作链：受控回写（execute/confirm） ----------
    banner("操作链：Action 触发/确认回写")
    r = tc_admin.post("/api/actions", params=P, json={
        "name": "审计验收动作",
        "target_connection": "conn_m2d",
        "invoke": {"kind": "webhook", "url": "http://127.0.0.1:1/none"},
        "input_map": {"k": "{k}"},
        "idempotency_key": "idem-m2d-1",
    })
    aid = r.json().get("data", {}).get("id")
    check("创建 Action（DISPLAY_ONLY 模式）", r.status_code == 201 and bool(aid), r.text[:200])

    r = tc_admin.post(f"/api/actions/{aid}/execute", params=P,
                      json={"input": {"k": "v"}, "dry_run": False})
    token = (r.json().get("data") or {}).get("confirmation_token")
    check("execute 返回确认令牌", r.status_code == 200 and bool(token), r.text[:200])
    ei = find_item("action_execute", aid)
    check("action_execute 留痕", ei is not None and (ei or {}).get("after", {}).get("dry_run") is False,
          str(ei)[:200])

    r = tc_admin.post(f"/api/actions/{aid}/confirm", params=P,
                      json={"confirmation_token": token})
    check("confirm 回写（display_only）", r.status_code == 200, r.text[:200])
    wi = find_item("action_writeback", aid)
    check("action_writeback 留痕（before 含 target）",
          wi is not None and (wi or {}).get("before", {}).get("target") == "conn_m2d", str(wi)[:200])

    # ---------- 操作链：变更审批（submit/reject） ----------
    banner("操作链：变更单提交/驳回留痕")

    def get_rec() -> dict:
        r = tc_carol.get(f"/api/ontologies/{oid}", params=P)
        assert r.status_code == 200, r.text[:200]
        return r.json()["data"]

    m = {k: get_rec()[k] for k in ("name", "description", "objects", "constraints") if k in get_rec()}
    m["objects"][0]["attributes"] = [a for a in m["objects"][0]["attributes"] if a["id"] != "cost"]
    r = tc_carol.post(f"/api/ontologies/{oid}/changesets", params=P,
                      json={"model": m, "note": "删 cost（2.0）", "target_version": "2.0"})
    csid = r.json().get("data", {}).get("changeset_id")
    check("不兼容变更单创建（pending）", r.status_code == 201 and bool(csid), r.text[:200])
    ssi = find_item("changeset_submit", csid)
    check("changeset_submit 留痕（actor=carol）", ssi is not None and ssi.get("actor") == "carol",
          str(ssi)[:200])

    r = tc_admin.post(f"/api/changesets/{csid}/reject", params=P,
                      json={"reason": "审计验收驳回"})
    check("admin 驳回变更单", r.status_code == 200, r.text[:200])
    rji = find_item("changeset_reject", csid)
    check("changeset_reject 留痕（actor=admin）", rji is not None and rji.get("actor") == "m2d_admin",
          str(rji)[:200])

    # ---------- 操作链：本体删除 ----------
    banner("操作链：本体删除留痕（含 before 快照）")
    m2 = base_model()
    m2["name"] = "M2D 删除用本体"
    r = tc_carol.post("/api/ontologies", params=P, json=m2)
    o2 = r.json().get("data", {}).get("id")
    check("创建删除用本体", r.status_code == 201 and bool(o2), r.text[:200])

    r = tc_carol.delete(f"/api/ontologies/{o2}", params=P)
    check("删除本体（draft 直删）", r.status_code == 200, r.text[:200])
    dli = find_item("ontology_delete", o2)
    check("ontology_delete 留痕（before.name 可读）",
          (dli or {}).get("before", {}).get("name") == "M2D 删除用本体", str(dli)[:200])

    # ---------- TC-09-02 before/after 值可见 ----------
    banner("TC-09-02 前值/后值随记录返回")
    check("update 记录含 before/after 且键齐全",
          all(k in (ui or {}) for k in ("before", "after", "actor", "ts", "action")), "")
    check("publish 记录 after.version=1.0", (pi or {}).get("after", {}).get("version") == "1.0",
          str(pi)[:200])
    check("delete 记录 detail 含删除信息", isinstance((dli or {}).get("detail"), dict),
          str(dli)[:200])

    # ---------- TC-09-01 组合检索（操作者/动作/对象/时间） ----------
    banner("TC-09-01 组合检索")
    its = audit_items(action="ontology_publish", actor="carol")
    check("按动作+操作者检索命中发布", any(i.get("target_id") == oid for i in its),
          str([i.get("action") for i in its])[:200])
    its = audit_items(target_id=oid)
    acts = {i.get("action") for i in its}
    check("按对象（target_id）检索命中该本体 create/update/publish",
          len(its) >= 3 and {"ontology_create", "ontology_update", "ontology_publish"} <= acts,
          str(acts)[:200])
    far = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = tc_admin.get("/api/audit", params={"project_id": p1, "start": far, "page_size": 10})
    check("时间范围过滤（未来 start→0 条）",
          r.json()["data"]["total"] == 0, r.text[:200])
    its = audit_items(actor="m2d_admin")
    check("按操作者检索（admin 动作可见）", len(its) >= 4,
          str([i.get("action") for i in its])[:200])

    # ---------- TC-09-03 导出与检索一致 ----------
    banner("TC-09-03 导出（CSV/JSONL）与检索同条件一致")
    r = tc_admin.get("/api/audit", params={"project_id": p1, "page_size": 500})
    listed = r.json()["data"]["items"]
    check("检索返回全部记录（<500）", r.json()["data"]["total"] == len(listed) and len(listed) > 0,
          f"total={r.json()['data']['total']} len={len(listed)}")

    r = tc_admin.get("/api/audit/export", params={"project_id": p1, "format": "jsonl"})
    exp_ids = {json.loads(line)["id"] for line in r.text.splitlines() if line.strip()}
    check("JSONL 导出与检索 id 集合一致", exp_ids == {i["id"] for i in listed},
          f"export={len(exp_ids)} listed={len(listed)}")

    r = tc_admin.get("/api/audit/export", params={"project_id": p1, "format": "csv"})
    text = r.text.lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(text)))
    check("CSV 表头字段完整", rows and rows[0] == [
        "id", "ts", "actor", "actor_role", "action", "project_id", "tenant_id",
        "target_type", "target_id", "target_name", "before", "after", "detail"],
          str(rows[0] if rows else "")[:200])
    check("CSV 行数=检索条数+1", len(rows) == len(listed) + 1,
          f"rows={len(rows)} listed={len(listed)}")

    # 同条件导出一致（action 过滤）
    r = tc_admin.get("/api/audit/export", params={"project_id": p1, "format": "jsonl",
                                                  "action": "ontology_publish"})
    exp2 = {json.loads(line)["id"] for line in r.text.splitlines() if line.strip()}
    r = tc_admin.get("/api/audit", params={"project_id": p1, "action": "ontology_publish",
                                           "page_size": 500})
    check("同条件（action）导出与检索一致",
          exp2 == {i["id"] for i in r.json()["data"]["items"]}, f"export={len(exp2)}")

    # ---------- TC-09-04 非 admin 拒绝 + 留痕 ----------
    banner("TC-09-04 非 admin 访问 403 且留痕")
    r = tc_dan.get("/api/audit", params=P)
    check("viewer 访问审计页→403", r.status_code == 403, r.text[:200])
    r = tc_dan.get("/api/audit/export", params={**P, "format": "csv"})
    check("viewer 导出→403", r.status_code == 403, r.text[:200])
    denied = [i for i in audit_items()
              if i.get("actor") == "dan"
              and i.get("action") in ("permission_denied", "audit_access_denied")]
    check("拒绝留痕（actor=dan）", len(denied) >= 2, str(denied)[:300])

    # ---------- TCE-09-01 篡改尝试 → 405 + 留痕 ----------
    banner("TCE-09-01 审计日志不可改/删（INV-4）")
    before_tamper = len([i for i in audit_items() if i.get("action") == "audit_tamper_attempt"])
    r = tc_admin.put(f"/api/audit/any/{'x' * 12}", params=P, json={"action": "hacked"})
    check("PUT→405 AUDIT_IMMUTABLE", r.status_code == 405
          and r.json().get("error", {}).get("code") == "AUDIT_IMMUTABLE", r.text[:200])
    r = tc_admin.patch(f"/api/audit/any/{'x' * 12}", params=P, json={"action": "hacked"})
    check("PATCH→405 AUDIT_IMMUTABLE", r.status_code == 405
          and r.json().get("error", {}).get("code") == "AUDIT_IMMUTABLE", r.text[:200])
    r = tc_admin.delete(f"/api/audit/any/{'x' * 12}", params=P)
    check("DELETE→405 AUDIT_IMMUTABLE", r.status_code == 405
          and r.json().get("error", {}).get("code") == "AUDIT_IMMUTABLE", r.text[:200])
    r = tc_admin.get("/api/audit", params={"action": "audit_tamper_attempt", "page_size": 100})
    tampers = r.json()["data"]["items"]
    check("篡改留痕 3 条（method 各异，含 project_id）",
          len(tampers) == before_tamper + 3
          and {t.get("detail", {}).get("method") for t in tampers[-3:]} == {"PUT", "PATCH", "DELETE"}
          and all(t.get("project_id") == p1 for t in tampers[-3:]),
          json.dumps(tampers, ensure_ascii=False)[:300])

    banner("汇总")
    print(f"\nTOTAL PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
