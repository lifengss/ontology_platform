"""默认初始密码与首次登录强制改密回归。

覆盖方案 TC-PP-01~08：
- TC-PP-01 建用户不传密码 → 默认初始密码 123456a? 且 must_change_password=true
- TC-PP-02 建用户传密码 → must_change_password=false
- TC-PP-03 change-password 当前密码错误 → 400
- TC-PP-04 change-password 成功 → 清 flag + 旧 token 立即失效 + 新密码可登录
- TC-PP-05 change-password 新密码同当前/过短 → 拒绝
- TC-PP-06 管理员重置密码（PUT）→ must_change_password=true + 目标用户旧会话失效
- TC-PP-07 Excel 导入密码列留空 → 默认密码 + 强制改密
- TC-PP-08 bootstrap 无环境变量 → 默认初始密码、不落盘任何明文密码文件

数据约定与 test_user_management.py 一致：独占真实 auth_users store（先清空）；
运行前后由外部脚本备份/恢复 data/shared/auth_*.json。
"""
import io
import os
import pathlib
import tempfile

from fastapi.testclient import TestClient
from openpyxl import Workbook

import auth as auth_svc
from core.storage import JsonStore
from main import app

c = TestClient(app)

DEFAULT_PW = auth_svc.DEFAULT_INITIAL_PASSWORD  # "123456a?"


def _b(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _ok(resp) -> dict:
    assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("success") is True, body
    return body["data"]


def _login(username: str, password: str) -> str:
    return _login_data(username, password)["token"]


def _login_data(username: str, password: str) -> dict:
    r = c.post("/api/auth/login", params={"mode": "bearer"},
               json={"username": username, "password": password})
    return _ok(r)


def _setup_auth(admin_pw: str) -> str:
    """清空用户存储 → 本测试独占 bootstrap（密码可控）；返回 admin token。"""
    ustore = JsonStore("auth_users", project_id="_shared")
    for u in ustore.all():
        ustore.delete(u["id"])
    os.environ["ONTOLOGY_AUTH_ENABLED"] = "true"
    os.environ["ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD"] = admin_pw
    os.environ["ONTOLOGY_PBKDF2_ITERATIONS"] = "10000"
    auth_svc.ensure_bootstrap()
    return _login("admin", admin_pw)


def _mk_users_table(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_create_user_default_password():
    """TC-PP-01/02：不传密码 → 默认初始密码+强制改密；传密码 → 不强制。"""
    admin = _setup_auth("um-admin-pw")
    hdr = _b(admin)
    r = c.post("/api/auth/users", headers=hdr, json={"username": "dflt", "role": "viewer"})
    d = _ok(r)
    assert d["must_change_password"] is True
    # 默认初始密码可登录，且登录响应带强制改密标记
    data = _login_data("dflt", DEFAULT_PW)
    assert data["user"]["must_change_password"] is True

    r = c.post("/api/auth/users", headers=hdr,
               json={"username": "withpw", "password": "withpw-withpw", "role": "viewer"})
    assert _ok(r)["must_change_password"] is False
    assert _login_data("withpw", "withpw-withpw")["user"]["must_change_password"] is False


def test_change_password_flow():
    """TC-PP-03/04/05：改密校验、成功清 flag、旧会话失效、新密码可登录。"""
    admin = _setup_auth("um-admin-pw")
    hdr = _b(admin)
    c.post("/api/auth/users", headers=hdr, json={"username": "chpw", "role": "viewer"})
    tok = _login("chpw", DEFAULT_PW)

    # 当前密码错误 → 400
    r = c.post("/api/auth/change-password", headers=_b(tok),
               json={"old_password": "wrong-pw-1", "new_password": "newpw-newpw"})
    assert r.status_code == 400, r.text[:300]
    # 新密码与当前相同 → 400
    r = c.post("/api/auth/change-password", headers=_b(tok),
               json={"old_password": DEFAULT_PW, "new_password": DEFAULT_PW})
    assert r.status_code == 400, r.text[:300]
    # 新密码过短 → 422（schema 校验）
    r = c.post("/api/auth/change-password", headers=_b(tok),
               json={"old_password": DEFAULT_PW, "new_password": "short"})
    assert r.status_code == 422, r.text[:300]

    # 成功改密：清 flag，响应直接签发新会话
    r = c.post("/api/auth/change-password", params={"mode": "bearer"}, headers=_b(tok),
               json={"old_password": DEFAULT_PW, "new_password": "newpw-newpw"})
    d = _ok(r)
    assert d["changed"] is True
    assert d["user"]["must_change_password"] is False
    new_tok = d["token"]
    # 改密前签发的旧 token 立即失效
    assert c.get("/api/auth/me", headers=_b(tok)).status_code == 401
    # 新 token 有效
    assert c.get("/api/auth/me", headers=_b(new_tok)).status_code == 200
    # 新密码可登录，且不再强制改密
    assert _login_data("chpw", "newpw-newpw")["user"]["must_change_password"] is False


def test_admin_reset_forces_change_and_invalidates_session():
    """TC-PP-06：管理员重置他人密码 → 强制改密 + 目标用户旧会话立即失效。"""
    admin = _setup_auth("um-admin-pw")
    hdr = _b(admin)
    r = c.post("/api/auth/users", headers=hdr,
               json={"username": "resetme", "password": "oldpw-oldpw", "role": "viewer"})
    uid = _ok(r)["id"]
    # 重置前：旧密码可登录且会话有效
    stale_tok = _login("resetme", "oldpw-oldpw")
    assert c.get("/api/auth/me", headers=_b(stale_tok)).status_code == 200

    # 管理员重置密码
    d = _ok(c.put(f"/api/auth/users/{uid}", headers=hdr, json={"password": "reset-pw-123"}))
    assert d["must_change_password"] is True
    # 重置前签发的旧会话立即失效，旧密码不可再登录
    assert c.get("/api/auth/me", headers=_b(stale_tok)).status_code == 401
    assert c.post("/api/auth/login", params={"mode": "bearer"},
                  json={"username": "resetme", "password": "oldpw-oldpw"}).status_code == 401
    # 新密码可登录，且被强制改密
    data = _login_data("resetme", "reset-pw-123")
    assert data["user"]["must_change_password"] is True


def test_import_empty_password_uses_default():
    """TC-PP-07：导入密码列留空 → 默认初始密码 + 强制改密。"""
    admin = _setup_auth("um-admin-pw")
    hdr = _b(admin)
    buf = _mk_users_table([["账号", "密码", "角色"], ["importee", "", "viewer"]])
    r = c.post("/api/auth/users/import", headers=hdr,
               files={"file": ("users.xlsx", buf,
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    d = _ok(r)
    assert d["created"] == 1 and d["failed"] == []
    data = _login_data("importee", DEFAULT_PW)
    assert data["user"]["must_change_password"] is True


def test_bootstrap_default_password_no_plaintext_file():
    """TC-PP-08：无环境变量时 bootstrap 用默认初始密码，且不落盘明文密码文件。"""
    ustore = JsonStore("auth_users", project_id="_shared")
    for u in ustore.all():
        ustore.delete(u["id"])
    os.environ["ONTOLOGY_AUTH_ENABLED"] = "true"
    os.environ["ONTOLOGY_PBKDF2_ITERATIONS"] = "10000"
    was_set = os.environ.pop("ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD", None)
    tmp = tempfile.mkdtemp(prefix="onto_secrets_")
    old_secrets = os.environ.get("SECRETS_DIR")
    os.environ["SECRETS_DIR"] = tmp
    try:
        auth_svc.ensure_bootstrap()
        admin = auth_svc.find_user("admin")
        assert admin is not None
        assert admin.get("must_change_password") is True
        # secrets 目录（含子目录）不得出现任何明文密码文件
        assert not list(pathlib.Path(tmp).rglob("*.txt")), "发现明文密码文件"
        # 默认初始密码可登录且带强制改密标记
        assert _login_data("admin", DEFAULT_PW)["user"]["must_change_password"] is True
    finally:
        if was_set is not None:
            os.environ["ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD"] = was_set
        if old_secrets is not None:
            os.environ["SECRETS_DIR"] = old_secrets
        else:
            os.environ.pop("SECRETS_DIR", None)
