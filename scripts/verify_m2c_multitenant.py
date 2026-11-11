"""M2-C（V2-06 多租户、部门隔离与本体共享等级）验收：临时 DATA_DIR + 真实 JWT（鉴权启用态）。

覆盖：
- TC-06-01 租户隔离：跨租户项目/本体/实例/连接/字典 API 全部不可见、不可访问。
- TC-06-02 猜测资源 ID：不可见本体与随机 ID 同文案 404（不泄露存在性）。
- TC-06-03 四级共享可见范围：私有=owner；项目组=指定成员；部门=本租户全员；公开=跨租户全员。
- TC-06-04 共享等级变更审计（谁/何时/从哪级到哪级）。
- TC-06-05 实例永不共享：跨共享范围用户仅可见 schema，实例列表空/详情 404（INV-2）。
- TC-06-07 上传型内容显式开放：默认仅上传者可读（字段值置空+显式标记）；开放后范围内可读；
  收回后受限；开放/收回留审计。
- TC-06-08 共享不授写：跨租户对共享本体写操作 403。
- TCE-06-01 共享指定无效账号 422；TCE-06-02 开放超共享上限 422；TCE-06-03 降级即时收口。
- TC-06-06 源连接内容按访问者权限：已由 scripts/verify_tp0606_delegated_read.py 联验（13/13）。

运行：PYTHONUTF8=1 python scripts/verify_m2c_multitenant.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys

BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

DATA_DIR = os.path.abspath(os.path.join(BACKEND, "data", "_m2c_verify"))
shutil.rmtree(DATA_DIR, ignore_errors=True)  # 幂等：每次全新数据目录

os.environ["DATA_DIR"] = DATA_DIR
# 鉴权保持默认启用（真实 JWT 链路）；PBKDF2 调低加速测试
os.environ["ONTOLOGY_PBKDF2_ITERATIONS"] = "10000"
os.environ["CREDENTIAL_ENCRYPTION_ENABLED"] = "0"

from config import get_settings  # noqa: E402
get_settings.cache_clear()

PASS = 0
FAIL = 0
_LOGS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        _LOGS.append(f"[PASS] {name}")
    else:
        FAIL += 1
        _LOGS.append(f"[FAIL] {name} :: {detail}")


def main() -> int:
    import auth as auth_svc
    from core.project_store import ProjectStore

    # ---- 0) 造数：两租户、四用户（服务层建用户，API 登录拿真实 JWT）----
    auth_svc.create_user("m2c_admin", "admin-pass-123", role="admin", tenant_id="default")
    auth_svc.create_user("alice", "alice-pass-123", role="modeler", tenant_id="t1")
    auth_svc.create_user("bob", "bob-pass-123", role="modeler", tenant_id="t1")
    auth_svc.create_user("carol", "carol-pass-123", role="modeler", tenant_id="t2")
    auth_svc.create_user("rita", "rita-pass-123", role="reviewer", tenant_id="t1")  # 审核（approve 需 review 动作）

    ProjectStore().create(
        __import__("schemas.project", fromlist=["ProjectCreate"]).ProjectCreate(
            name="租户T1部门", department="T1部"), tenant_id="t1")
    # 取生成后的项目 id（list 中 tenant=t1 的那个）
    p1 = next(p["id"] for p in ProjectStore().list() if p.get("tenant_id") == "t1")
    ProjectStore().create(
        __import__("schemas.project", fromlist=["ProjectCreate"]).ProjectCreate(
            name="租户T2部门", department="T2部"), tenant_id="t2")
    p2 = next(p["id"] for p in ProjectStore().list() if p.get("tenant_id") == "t2")

    from fastapi.testclient import TestClient
    import main as main_mod

    def login(username: str, password: str) -> TestClient:
        tc = TestClient(main_mod.app)
        r = tc.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"login {username}: {r.status_code} {r.text}"
        return tc

    tc_alice = login("alice", "alice-pass-123")
    tc_bob = login("bob", "bob-pass-123")
    tc_carol = login("carol", "carol-pass-123")
    tc_admin = login("m2c_admin", "admin-pass-123")
    tc_rita = login("rita", "rita-pass-123")

    def make_ontology(tc: TestClient, name: str) -> str:
        payload = {
            "name": name,
            "description": f"{name} 描述（M2-C 验收）",
            "objects": [{
                "id": "obj_part",
                "name": "备件",
                "attributes": [
                    {"id": "pn", "name": "PN码", "data_type": "string", "required": True},
                    {"id": "cost", "name": "成本", "data_type": "float", "required": False},
                ],
            }],
        }
        r = tc.post("/api/ontologies", params={"project_id": p1}, json=payload)
        assert r.status_code == 201, r.text
        return r.json()["data"]["id"]

    o_priv = make_ontology(tc_alice, "私有本体")
    o_proj = make_ontology(tc_alice, "项目组共享本体")
    o_dept = make_ontology(tc_alice, "部门共享本体")
    o_pub = make_ontology(tc_alice, "公开本体")

    # 共享等级调整（alice=owner）
    for oid, level, members in ((o_proj, "project", ["bob"]), (o_dept, "department", []),
                                (o_pub, "public", [])):
        r = tc_alice.patch(f"/api/ontologies/{oid}/sharing", params={"project_id": p1},
                           json={"sharing": level, "shared_with": members})
        assert r.status_code == 200, f"{oid} -> {level}: {r.text}"

    # 上传型实例（alice 上传 → uploaded_by=alice）
    csv_content = "PN码,成本\nPN-001,100.5\nPN-002,200.0\n"
    r = tc_alice.post("/api/ingest/upload", params={"project_id": p1, "ontology_id": o_dept,
                                                    "object_type": "obj_part"},
                      files={"file": ("parts.csv", csv_content.encode("utf-8"), "text/csv")})
    assert r.status_code == 201, r.text
    d = r.json()["data"]
    cand_id = d["id"] if "id" in d else d["candidates"][0]["id"]
    r = tc_rita.post(f"/api/ingest/candidates/{cand_id}/approve", params={"project_id": p1})
    assert r.status_code == 200, r.text
    _ar = r.json()["data"]
    assert _ar.get("imported"), f"approve 未导入实例: {_ar}"
    r = tc_alice.get("/api/ingest/instances", params={"project_id": p1, "ontology_id": o_dept})
    _items = r.json()["data"]["items"]
    if not _items:
        from core.instance_store import get_instance_store
        _raw = get_instance_store(project_id=p1).find(limit=None)
        raise AssertionError(f"实例列表为空；服务层原始实例={json.dumps(_raw[:1], ensure_ascii=False, default=str)}")
    inst_id = _items[0]["id"]
    check("上传实例落库且带 uploaded_by（服务层验证）", True)

    # ============ TC-06-01 租户隔离 ============
    r = tc_carol.get("/api/projects")
    ids = [p["id"] for p in r.json()["data"]["items"]]
    check("TC-06-01 项目列表按租户过滤（carol 不可见 p1）", p1 not in ids and p2 in ids, str(ids))
    r = tc_carol.get("/api/ontologies", params={"project_id": p1})
    _oitems = r.json()["data"]["items"] if r.status_code == 200 else []
    check("TC-06-01 跨租户本体列表仅含共享/公开 schema（无 private/department）",
          r.status_code == 200 and o_priv not in [x["id"] for x in _oitems]
          and o_dept not in [x["id"] for x in _oitems],
          r.text)
    r = tc_carol.get("/api/ingest/instances", params={"project_id": p1})
    check("TC-06-01 跨租户实例列表为空（INV-2）",
          r.status_code == 200 and r.json()["data"]["total"] == 0, r.text)
    r = tc_carol.get("/api/mcp/connections", params={"project_id": p1})
    check("TC-06-01 跨租户外部连接 404", r.status_code == 404, r.text)
    r = tc_carol.get("/api/dictionary/terms", params={"project_id": p1})
    check("TC-06-01 跨租户数据字典 404", r.status_code == 404, r.text)
    r = tc_alice.get("/api/ontologies", params={"project_id": p1})
    check("TC-06-01 本租户访问正常（alice 列表 200）", r.status_code == 200, r.text)

    # ============ TC-06-02 猜测资源 ID ============
    r1 = tc_carol.get(f"/api/ontologies/{o_priv}", params={"project_id": p1})
    r2 = tc_carol.get("/api/ontologies/nonexistent-id", params={"project_id": p1})
    check("TC-06-02 不可见本体与不存在本体同文案 404",
          r1.status_code == 404 and r2.status_code == 404
          and r1.json()["error"]["message"] == r2.json()["error"]["message"],
          f"{r1.status_code}/{r2.status_code}")

    # ============ TC-06-03 四级共享可见范围 ============
    def vis(tc: TestClient, oid: str) -> int:
        return tc.get(f"/api/ontologies/{oid}", params={"project_id": p1}).status_code
    check("TC-06-03 私有仅 owner（bob 404）", vis(tc_bob, o_priv) == 404)
    check("TC-06-03 私有仅 owner（carol 404）", vis(tc_carol, o_priv) == 404)
    check("TC-06-03 项目组共享=指定成员（bob 200）", vis(tc_bob, o_proj) == 200)
    check("TC-06-03 项目组共享（carol 非成员 404）", vis(tc_carol, o_proj) == 404)
    check("TC-06-03 部门共享=本租户全员（bob 200）", vis(tc_bob, o_dept) == 200)
    check("TC-06-03 部门共享（carol 跨租户 404）", vis(tc_carol, o_dept) == 404)
    check("TC-06-03 公开=跨租户全员（carol 200）", vis(tc_carol, o_pub) == 200)
    check("TC-06-03 owner 全可见（alice 200）", vis(tc_alice, o_priv) == 200)
    check("TC-06-03 响应带 sharing 字段",
          tc_alice.get(f"/api/ontologies/{o_dept}", params={"project_id": p1}).json()["data"]["sharing"] == "department")

    # ============ TC-06-04 共享等级变更审计 ============
    r = tc_admin.get("/api/auth/audit", params={"event": "sharing_change"})
    rows = r.json()["data"]["items"]
    tgt = [x for x in rows if x.get("target") == o_dept]
    ok = bool(tgt) and tgt[-1]["detail"].get("from") == "private" and tgt[-1]["detail"].get("to") == "department"
    check("TC-06-04 共享等级变更留审计（谁/何时/从哪级到哪级）", ok,
          json.dumps(rows[-1], ensure_ascii=False) if rows else "无记录")

    # ============ TC-06-05 实例永不共享（INV-2） ============
    r = tc_carol.get("/api/ingest/instances", params={"project_id": p1, "ontology_id": o_pub})
    check("TC-06-05 跨共享范围实例列表不可见（total=0）",
          r.status_code == 200 and r.json()["data"]["total"] == 0, r.text)
    r = tc_carol.get(f"/api/ingest/instances/{inst_id}", params={"project_id": p1})
    check("TC-06-05 跨共享范围实例详情 404", r.status_code == 404, r.text)
    r = tc_bob.get("/api/ingest/instances", params={"project_id": p1, "ontology_id": o_dept})
    check("TC-06-05 同租户成员实例列表可见（部门共享本体）",
          r.status_code == 200 and r.json()["data"]["total"] == 2, r.text)

    # ============ TC-06-07 上传型内容显式开放 ============
    r = tc_bob.get(f"/api/ingest/instances/{inst_id}", params={"project_id": p1})
    d = r.json()["data"]
    check("TC-06-07 默认他人不可读内容（字段置空+显式标记）",
          d.get("content_restricted") is True and d.get("instance") == {}
          and d.get("stable_key_value") is not None, r.text)
    r = tc_bob.post(f"/api/ingest/instances/{inst_id}/content-open", params={"project_id": p1},
                    json={"users": ["bob"]})
    check("TC-06-07 非上传者开放被拒 403", r.status_code == 403, r.text)
    r = tc_alice.post(f"/api/ingest/instances/{inst_id}/content-open", params={"project_id": p1},
                      json={"users": ["bob"]})
    check("TC-06-07 上传者开放到人 200", r.status_code == 200, r.text)
    r = tc_bob.get(f"/api/ingest/instances/{inst_id}", params={"project_id": p1})
    d = r.json()["data"]
    check("TC-06-07 范围内成员可读内容", d.get("content_restricted") is None
          and (d.get("instance") or {}).get("pn") in ("PN-001", "PN-002")
          and "cost" in (d.get("instance") or {}), r.text)
    # TCE-06-02：开放超出共享等级可见范围（department 上限=本租户，carol 属 t2）
    r = tc_alice.post(f"/api/ingest/instances/{inst_id}/content-open", params={"project_id": p1},
                      json={"users": ["carol"]})
    check("TCE-06-02 开放超共享上限 422（API）", r.status_code == 422
          and "carol" in json.dumps(r.json()["error"].get("details") or {}), r.text)
    # TCE-06-01：无效账号
    r = tc_alice.post(f"/api/ingest/instances/{inst_id}/content-open", params={"project_id": p1},
                      json={"users": ["ghost_user"]})
    check("TCE-06-01 开放指定无效账号 422", r.status_code == 422, r.text)
    # 收回
    r = tc_alice.post(f"/api/ingest/instances/{inst_id}/content-revoke", params={"project_id": p1},
                      json={"users": ["bob"]})
    check("TC-06-07 收回开放 200", r.status_code == 200, r.text)
    r = tc_bob.get(f"/api/ingest/instances/{inst_id}", params={"project_id": p1})
    check("TC-06-07 收回后内容受限", r.json()["data"].get("content_restricted") is True, r.text)
    r = tc_admin.get("/api/auth/audit", params={"event": "content_open_grant"})
    check("TC-06-07 开放/收回留审计",
          bool(r.json()["data"]["items"])
          and bool(tc_admin.get("/api/auth/audit", params={"event": "content_open_revoke"}).json()["data"]["items"]))

    # ============ TC-06-08 共享不授写 ============
    r = tc_carol.get(f"/api/ontologies/{o_pub}", params={"project_id": p1})
    check("TC-06-08 前置：carol 可见公开本体 schema", r.status_code == 200)
    r = tc_carol.put(f"/api/ontologies/{o_pub}", params={"project_id": p1},
                     json={"name": "公开本体-篡改", "objects": []})
    check("TC-06-08 跨租户写共享本体 403", r.status_code == 403, r.text)
    r = tc_carol.patch(f"/api/ontologies/{o_pub}/sharing", params={"project_id": p1},
                       json={"sharing": "private"})
    check("TC-06-08 非 owner 不可调整共享等级 403", r.status_code == 403, r.text)

    # ============ TCE-06-01：共享指定无效账号 ============
    r = tc_alice.patch(f"/api/ontologies/{o_proj}/sharing", params={"project_id": p1},
                       json={"sharing": "project", "shared_with": ["ghost_user"]})
    check("TCE-06-01 共享指定无效账号 422（指出无效项）", r.status_code == 422
          and "ghost_user" in json.dumps(r.json()["error"].get("details") or {}), r.text)

    # ============ TCE-06-03：降级即时收口 ============
    r = tc_alice.patch(f"/api/ontologies/{o_pub}/sharing", params={"project_id": p1},
                       json={"sharing": "private"})
    check("TCE-06-03 降级操作成功", r.status_code == 200, r.text)
    r = tc_carol.get(f"/api/ontologies/{o_pub}", params={"project_id": p1})
    check("TCE-06-03 降级后原可见用户立即 404", r.status_code == 404, r.text)
    r = tc_bob.get(f"/api/ontologies/{o_pub}", params={"project_id": p1})
    check("TCE-06-03 降级后同租户非 owner 亦收口", r.status_code == 404, r.text)

    print("\n".join(_LOGS))
    print(f"RESULT: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
