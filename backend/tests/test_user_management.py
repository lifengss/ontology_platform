"""用户管理能力回归（部门字段 / 批量导入 / 禁用与删除 / 角色权限）。

覆盖方案 TC-U1~U9：
- TC-U1 创建用户带 department/tenant_id → GET /users 返回对应字段
- TC-U2 PUT /users/{id} 改 department → 立即生效
- TC-U3 DELETE /users/{id} → 物理删除
- TC-U4 DELETE 不存在用户 → 404
- TC-U5 非 admin 调 DELETE / POST import → 403
- TC-U6 POST import 合法 xlsx → 创建成功
- TC-U7 POST import 含失败行（重名/小密码）→ failed 明细，成功行仍创建
- TC-U8 PUT /users/{id} disabled=true 再登录被拒；false 后可登录
- TC-U9 部门与租户独立：改 department 不影响 tenant_id，反之亦然
"""
import io
import os
import uuid

from fastapi.testclient import TestClient
from openpyxl import Workbook

from main import app
from core.storage import JsonStore

c = TestClient(app)


def _b(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _ok(resp) -> dict:
    assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("success") is True, body
    return body["data"]


def _err(resp, want_code: str, want_status: int = 422) -> dict:
    assert resp.status_code == want_status, f"{resp.status_code} != {want_status}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("success") is False, body
    assert body["error"]["code"] == want_code, body["error"]
    return body["error"]


def _login(username: str, password: str) -> str:
    r = c.post("/api/auth/login", params={"mode": "bearer"},
               json={"username": username, "password": password})
    return _ok(r)["token"]


def _setup_auth(admin_pw: str) -> str:
    """清空用户存储 → 本测试独占 bootstrap（密码可控）；返回 admin token。"""
    import auth as auth_svc
    ustore = JsonStore("auth_users", project_id="_shared")
    for u in ustore.all():
        ustore.delete(u["id"])
    os.environ["ONTOLOGY_AUTH_ENABLED"] = "true"
    os.environ["ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD"] = admin_pw
    os.environ["ONTOLOGY_PBKDF2_ITERATIONS"] = "10000"
    auth_svc.ensure_bootstrap()
    return _login("admin", admin_pw)


def _mk_users_table(rows: list[list]) -> bytes:
    """生成 xlsx 字节流，首行为表头。"""
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_user_crud_and_department():
    """TC-U1/U2/U3/U4/U9：部门字段、独立租户、删除。"""
    admin = _setup_auth("um-admin-pw")
    hdr = _b(admin)
    try:
        # TC-U1：创建用户带 department + tenant_id
        rec = _ok(c.post("/api/auth/users", headers=hdr, json={
            "username": "um-dept1", "password": "um-pw-123456",
            "role": "modeler", "department": "研发部", "tenant_id": "t-alfa",
        }))
        assert rec["department"] == "研发部"
        assert rec["tenant_id"] == "t-alfa"

        # GET /users 返回对应字段
        lst = _ok(c.get("/api/auth/users", headers=hdr))["items"]
        u = next(x for x in lst if x["username"] == "um-dept1")
        assert u["department"] == "研发部"
        assert u["tenant_id"] == "t-alfa"

        # TC-U9：部门与租户独立——只改 department 不影响 tenant_id
        _ok(c.put(f"/api/auth/users/{rec['id']}", headers=hdr,
                  json={"department": "市场部"}))
        u2 = _ok(c.get("/api/auth/users", headers=hdr))["items"]
        u2 = next(x for x in u2 if x["username"] == "um-dept1")
        assert u2["department"] == "市场部"
        assert u2["tenant_id"] == "t-alfa"

        # 只改 tenant_id 不影响 department
        _ok(c.put(f"/api/auth/users/{rec['id']}", headers=hdr,
                  json={"tenant_id": "t-beta"}))
        u3 = _ok(c.get("/api/auth/users", headers=hdr))["items"]
        u3 = next(x for x in u3 if x["username"] == "um-dept1")
        assert u3["department"] == "市场部"
        assert u3["tenant_id"] == "t-beta"

        # TC-U3：删除
        _ok(c.delete(f"/api/auth/users/{rec['id']}", headers=hdr))
        lst2 = _ok(c.get("/api/auth/users", headers=hdr))["items"]
        assert all(x["username"] != "um-dept1" for x in lst2)

        # TC-U4：删除不存在用户 → 404
        _err(c.delete(f"/api/auth/users/{uuid.uuid4().hex}", headers=hdr),
             "NOT_FOUND", 404)
    finally:
        os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"


def test_toggle_disabled_login():
    """TC-U8：禁用后不可登录，恢复后可登录。"""
    admin = _setup_auth("um-admin-pw")
    hdr = _b(admin)
    try:
        rec = _ok(c.post("/api/auth/users", headers=hdr, json={
            "username": "um-toggle", "password": "um-pw-123456", "role": "viewer",
        }))
        uid = rec["id"]

        # 初始可登录
        _login("um-toggle", "um-pw-123456")

        # 禁用
        _ok(c.put(f"/api/auth/users/{uid}", headers=hdr, json={"disabled": True}))
        r = c.post("/api/auth/login", params={"mode": "bearer"},
                   json={"username": "um-toggle", "password": "um-pw-123456"})
        assert r.status_code == 401, r.text

        # 恢复
        _ok(c.put(f"/api/auth/users/{uid}", headers=hdr, json={"disabled": False}))
        _login("um-toggle", "um-pw-123456")
    finally:
        os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"


def test_import_users():
    """TC-U6 / TC-U7：Excel 批量导入，失败行返回明细。"""
    admin = _setup_auth("um-admin-pw")
    hdr = _b(admin)
    try:
        # 先建一个用户用于制造重名失败
        _ok(c.post("/api/auth/users", headers=hdr, json={
            "username": "um-exists", "password": "um-pw-123456", "role": "viewer",
        }))

        data = _mk_users_table([
            ["账号", "密码", "角色", "部门", "租户"],
            ["um-imp1", "um-pw-123456", "viewer", "研发部", "t-alfa"],
            ["um-imp2", "um-pw-123456", "modeler", "市场部", "t-beta"],
            ["um-exists", "um-pw-123456", "viewer", "", ""],        # 重名 → 失败
            ["um-imp3", "short", "viewer", "", ""],                  # 密码过短 → 失败
            ["", "", "", "", ""],                                    # 空行 → 跳过
        ])
        res = _ok(c.post("/api/auth/users/import", headers=hdr,
                         files={"file": ("users.xlsx", data,
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}))
        assert res["created"] == 2, res
        assert res["total"] == 4, res
        fail_rows = {f["row"] for f in res["failed"]}
        assert 4 in fail_rows and 5 in fail_rows, res["failed"]

        # 成功行确实创建
        lst = _ok(c.get("/api/auth/users", headers=hdr))["items"]
        names = {x["username"] for x in lst}
        assert "um-imp1" in names and "um-imp2" in names
        assert "um-imp3" not in names

        # 非法扩展名 → 400
        _err(c.post("/api/auth/users/import", headers=hdr,
                    files={"file": ("users.txt", b"abc", "text/plain")}),
             "BAD_REQUEST", 400)
    finally:
        os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"


def test_non_admin_forbidden():
    """TC-U5：非 admin 调 DELETE / POST import → 403。"""
    admin = _setup_auth("um-admin-pw")
    hdr = _b(admin)
    try:
        # 建一个普通用户
        rec = _ok(c.post("/api/auth/users", headers=hdr, json={
            "username": "um-viewer1", "password": "um-pw-123456", "role": "viewer",
        }))
        tok = _login("um-viewer1", "um-pw-123456")
        uh = _b(tok)

        r = c.delete(f"/api/auth/users/{rec['id']}", headers=uh)
        assert r.status_code == 403, r.text

        r = c.post("/api/auth/users/import", headers=uh,
                   files={"file": ("users.xlsx", b"", "application/octet-stream")})
        assert r.status_code == 403, r.text
    finally:
        os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
