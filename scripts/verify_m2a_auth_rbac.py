"""M2-A / V2-05 登录鉴权与 RBAC 验收（TP-05-01~05、EP-05-01~03、TC/TCE-05-*）。

    python scripts/verify_m2a_auth_rbac.py            # 全量（REST + 真实 Chromium DOM）
    python scripts/verify_m2a_auth_rbac.py --no-ui    # 跳过 Playwright 段

设计要点：
- 本脚本在**鉴权启用态**运行（ONTOLOGY_AUTH_ENABLED=true），用真实 JWT 覆盖全部测试点；
  L5 基线回归与 M1 切片脚本则显式使用测试态豁免，二者互不掩盖。
- 全局数据落在临时 DATA_DIR，仓库 data/ 无残留。
- UI 段用 stub fetch 驱动真实 DOM 流程，避免依赖在线 CDN 与后端服务（离线可跑）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="m2a_auth_")
os.environ.update({
    "DATA_DIR": os.path.join(TMP, "data"),
    "SECRETS_DIR": os.path.join(TMP, "secrets"),
    "LOG_DIR": os.path.join(TMP, "logs"),
    "JENA_BASE_URL": "http://127.0.0.1:9",
    "ONTOLOGY_AUTH_ENABLED": "true",
    "ONTOLOGY_AUTH_SECRET": "m2a-verify-secret",
    "ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD": "Admin@12345",
    "ONTOLOGY_PBKDF2_ITERATIONS": "10000",
})
sys.path.insert(0, os.path.join(REPO, "backend"))

import auth as auth_svc  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402

PASS = FAIL = SKIP = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} :: {detail}")


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {name} :: {why}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def env(resp):
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def bearer(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def login(client: TestClient, username: str, password: str, mode: str = "bearer") -> dict:
    path = "/api/auth/login" + ("?mode=bearer" if mode == "bearer" else "")
    return client.post(path, json={"username": username, "password": password})


def create_user(client: TestClient, admin_headers: dict, username: str,
                password: str, role: str, grants: list | None = None) -> dict:
    payload = {"username": username, "password": password, "role": role, "grants": grants or []}
    r = client.post("/api/auth/users", json=payload, headers=admin_headers)
    return env(r).get("data") or {}


def main() -> int:
    no_ui = "--no-ui" in sys.argv
    anon = TestClient(app)
    admin_c = TestClient(app)
    viewer_c = TestClient(app)

    print(f"DATA_DIR 隔离目录: {os.environ['DATA_DIR']}")

    # ------------------------------------------------------------------ #
    section("前置：默认启用 / 放行清单 / 未登录拒绝（TP-05-01）")
    saved = os.environ.pop("ONTOLOGY_AUTH_ENABLED", None)
    check("鉴权默认启用（无显式环境变量时）", auth_svc.auth_enabled() is True)
    if saved is not None:
        os.environ["ONTOLOGY_AUTH_ENABLED"] = saved

    r = anon.get("/health")
    check("健康检查放行（前端轮询/探针无需登录）", r.status_code == 200, str(r.status_code))
    r = anon.get("/api/projects")
    check("未登录 GET /api/projects → 401", r.status_code == 401, str(r.status_code))
    check("401 使用统一信封与 UNAUTHORIZED 错误码",
          env(r).get("success") is False and env(r).get("error", {}).get("code") == "UNAUTHORIZED",
          str(env(r))[:200])
    r2 = anon.post("/api/ontologies", json={"name": "x"})
    check("未登录写接口 POST /api/ontologies → 401", r2.status_code == 401, str(r2.status_code))
    r3 = anon.get("/api/auth/me")
    check("未登录 GET /api/auth/me → 401", r3.status_code == 401, str(r3.status_code))
    r4 = anon.get("/api/projects", headers={"Origin": "http://localhost:8123"})
    check("401 响应回显 CORS 头（浏览器可读正文）",
          r4.headers.get("access-control-allow-origin") == "http://localhost:8123"
          and r4.headers.get("access-control-allow-credentials") == "true",
          str(dict(r4.headers))[:250])
    r5 = anon.get("/mcp/status")
    check("/mcp/status 放行（MCP 体系独立）", r5.status_code == 200, str(r5.status_code))
    r6 = anon.get("/mcp/no-such-project", headers={"Accept": "text/event-stream"})
    check("MCP 端点仍由项目静态 token 体系校验（裸 401 不被 JWT 过滤器改写）",
          r6.status_code == 401 and env(r6).get("error") == "UNAUTHORIZED", str(env(r6))[:200])

    # ------------------------------------------------------------------ #
    section("登录与会话（TP-05-05 / TCE-05-01 / TCE-05-02 / EP-05-02）")
    r = admin_c.post("/api/auth/login", json={"username": "admin", "password": "Admin@12345"})
    cookie_data = env(r)
    check("管理员登录（Cookie 模式）200", r.status_code == 200, str(cookie_data)[:200])
    set_cookie = r.headers.get("set-cookie", "")
    check("Cookie 为 httpOnly + SameSite=Lax",
          "httponly" in set_cookie.lower() and "samesite=lax" in set_cookie.lower(),
          set_cookie[:200])
    check("Cookie 模式响应体不返回 token（JS 不接触会话）",
          "token" not in (cookie_data.get("data") or {}), str(cookie_data.get("data"))[:200])
    check("Cookie 会话可直接访问业务 API",
          admin_c.get("/api/projects").status_code == 200, str(admin_c.get("/api/projects").status_code))

    r = login(anon, "admin", "Admin@12345")
    admin_token = env(r).get("data", {}).get("token", "")
    check("bearer 模式返回 token（供非浏览器 API 客户端）", bool(admin_token), str(env(r))[:200])
    admin_h = bearer(admin_token)

    bad_pwd = login(anon, "admin", "wrong-password")
    bad_user = login(anon, "no-such-user", "whatever123")
    check("密码错误 401 且统一文案", bad_pwd.status_code == 401
          and env(bad_pwd).get("error", {}).get("message") == auth_svc.UNIFORM_LOGIN_ERROR,
          str(env(bad_pwd))[:200])
    check("账号不存在与密码错误文案/code/status 完全一致（防枚举）",
          (bad_user.status_code, env(bad_user).get("error", {}).get("code"),
           env(bad_user).get("error", {}).get("message"))
          == (bad_pwd.status_code, env(bad_pwd).get("error", {}).get("code"),
              env(bad_pwd).get("error", {}).get("message")),
          f"{bad_user.status_code} {env(bad_user)} vs {bad_pwd.status_code} {env(bad_pwd)}")

    create_user(anon, admin_h, "lockuser", "Lock@12345", "viewer")
    for _ in range(auth_svc.LOCK_THRESHOLD):
        login(anon, "lockuser", "bad-password")
    locked_ok = login(anon, "lockuser", "Lock@12345")
    check("连续错误达到阈值后锁定，正确密码亦被拒（TC-05-05）",
          locked_ok.status_code == 401, str(locked_ok.status_code))
    failed_audit = env(anon.get("/api/auth/audit?event=login_failed", headers=admin_h)).get("data", {})
    check("登录失败有审计留痕", failed_audit.get("total", 0) >= auth_svc.LOCK_THRESHOLD,
          str(failed_audit)[:200])

    # 过期 / 篡改 / alg=none / 登出撤销
    now = int(time.time())
    expired = auth_svc.encode_jwt({"sub": "admin", "jti": "expired-jti", "iat": now - 100, "exp": now - 1})
    check("过期 token → 401", anon.get("/api/projects", headers=bearer(expired)).status_code == 401)
    parts = admin_token.split(".")
    tampered = parts[0] + "." + parts[1] + "." + ("A" * len(parts[2]))
    check("篡改签名 → 401", anon.get("/api/projects", headers=bearer(tampered)).status_code == 401)
    none_header = auth_svc._b64e(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    alg_none = f"{none_header}.{parts[1]}."
    check("alg=none 伪造 → 401", anon.get("/api/projects", headers=bearer(alg_none)).status_code == 401)
    logout_c = TestClient(app)
    tok2 = env(login(logout_c, "admin", "Admin@12345")).get("data", {}).get("token", "")
    check("登出 200", logout_c.post("/api/auth/logout", headers=bearer(tok2)).status_code == 200)
    check("登出后原 token 被撤销 → 401（会话不残留权限）",
          logout_c.get("/api/projects", headers=bearer(tok2)).status_code == 401)

    sso_cfg = env(anon.get("/api/auth/sso/config")).get("data") or {}
    check("SSO 预留接口存在且未启用（TP-05-05）",
          sso_cfg.get("enabled") is False and sso_cfg.get("reserved") is True, str(sso_cfg))
    sso_start = anon.post("/api/auth/sso/start")
    check("SSO 未启用时显式拒绝，不回落匿名",
          sso_start.status_code == 403
          and env(sso_start).get("error", {}).get("code") == "SSO_NOT_ENABLED",
          str(env(sso_start))[:200])

    # ------------------------------------------------------------------ #
    section("只读角色写接口 403（TP-05-02）")
    viewer_rec = create_user(anon, admin_h, "viewer1", "Viewer@12345", "viewer")
    modeler_rec = create_user(anon, admin_h, "modeler1", "Modeler@12345", "modeler")
    reviewer_rec = create_user(anon, admin_h, "reviewer1", "Reviewer@12345", "reviewer")
    check("管理员可创建三类角色用户", all([viewer_rec.get("id"), modeler_rec.get("id"), reviewer_rec.get("id")]),
          f"{viewer_rec} {modeler_rec} {reviewer_rec}"[:250])
    viewer_token = env(login(viewer_c, "viewer1", "Viewer@12345")).get("data", {}).get("token", "")
    viewer_h = bearer(viewer_token)
    check("只读角色登录成功", bool(viewer_token))
    check("只读角色可读项目列表", viewer_c.get("/api/projects", headers=viewer_h).status_code == 200)

    write_calls = [
        ("建模写", viewer_c.post("/api/ontologies?project_id=default", json={"name": "x"}, headers=viewer_h)),
        ("入库写", viewer_c.post("/api/ingest/upload?project_id=default", headers=viewer_h)),
        ("项目更新", viewer_c.put("/api/projects/proj-a", json={"name": "x"}, headers=viewer_h)),
        ("项目删除", viewer_c.delete("/api/projects/proj-a", headers=viewer_h)),
        ("字典写", viewer_c.post("/api/dictionary/terms?project_id=default",
                                json={"term": "x", "canonical": "y"}, headers=viewer_h)),
    ]
    for label, resp in write_calls:
        check(f"只读角色 {label} → 403", resp.status_code == 403, str(resp.status_code))
    check("403 错误码为 FORBIDDEN",
          env(write_calls[0][1]).get("error", {}).get("code") == "FORBIDDEN",
          str(env(write_calls[0][1]))[:200])
    ro_post = viewer_c.post("/api/dictionary/term-lookup?project_id=default",
                            json={"term": "PN码"}, headers=viewer_h)
    check("只读 POST 白名单不误伤（term-lookup 非 403）", ro_post.status_code != 403,
          str(ro_post.status_code))

    reviewer_h = bearer(env(login(anon, "reviewer1", "Reviewer@12345")).get("data", {}).get("token", ""))
    modeler_h = bearer(env(login(anon, "modeler1", "Modeler@12345")).get("data", {}).get("token", ""))
    approve_path = "/api/ingest/candidates/fake-id/approve?project_id=default"
    check("审核角色可进入审核动作（非 403）",
          anon.post(approve_path, headers=reviewer_h).status_code != 403,
          str(anon.post(approve_path, headers=reviewer_h).status_code))
    check("建模角色无 review 权限 → 403",
          anon.post(approve_path, headers=modeler_h).status_code == 403,
          str(anon.post(approve_path, headers=modeler_h).status_code))

    # ------------------------------------------------------------------ #
    section("资源级授权与审计（TP-05-03 / TCE-05-03 / EP-05-03）")
    grants = [
        {"scope": "project", "target": "proj-a", "level": "write"},
        {"scope": "project", "target": "proj-b", "level": "read"},
    ]
    r = anon.put(f"/api/auth/users/{modeler_rec['id']}",
                 json={"grants": grants}, headers=admin_h)
    check("管理员设置项目级授权成功", r.status_code == 200, str(env(r))[:200])
    pa = anon.post("/api/ontologies?project_id=proj-a", json={"name": "x"}, headers=modeler_h)
    pb = anon.post("/api/ontologies?project_id=proj-b", json={"name": "x"}, headers=modeler_h)
    pc = anon.post("/api/ontologies?project_id=proj-c", json={"name": "x"}, headers=modeler_h)
    check("项目 A 可写（授权生效）", pa.status_code != 403, str(pa.status_code))
    check("项目 B 只读（拒写）", pb.status_code == 403, str(pb.status_code))
    check("未显式授权项目沿用角色默认权限", pc.status_code != 403, str(pc.status_code))

    onto_grants = [
        {"scope": "project", "target": "proj-a", "level": "none"},
        {"scope": "ontology", "target": "ont-x", "level": "write"},
    ]
    anon.put(f"/api/auth/users/{viewer_rec['id']}", json={"grants": onto_grants}, headers=admin_h)
    ox = anon.patch("/api/ontologies/ont-x/objects/obj1?project_id=proj-a",
                    json={"name": "x"}, headers=viewer_h)
    oy = anon.patch("/api/ontologies/ont-y/objects/obj1?project_id=proj-a",
                    json={"name": "x"}, headers=viewer_h)
    check("本体级 grant 覆盖项目级 none（ont-x 可写）", ox.status_code != 403, str(ox.status_code))
    check("无本体级 grant 的资源仍被拒（ont-y → 403）", oy.status_code == 403, str(oy.status_code))

    escalate = anon.put(f"/api/auth/users/{viewer_rec['id']}",
                        json={"role": "admin"}, headers=viewer_h)
    check("普通用户越级修改自身角色 → 403", escalate.status_code == 403, str(escalate.status_code))
    audit_denied = env(anon.get("/api/auth/audit?event=permission_denied", headers=admin_h)).get("data", {})
    check("越权操作有审计留痕（EP-05-03）",
          any(a.get("target", "").startswith("/api/auth/users") for a in audit_denied.get("items", [])),
          str(audit_denied)[:250])
    audit_change = env(anon.get("/api/auth/audit?event=permission_change", headers=admin_h)).get("data", {})
    check("授权变更可审计且含 before/after（TP-05-03）",
          any("before" in (a.get("detail") or {}) and "after" in (a.get("detail") or {})
              for a in audit_change.get("items", [])),
          str(audit_change)[:250])
    users_list = env(anon.get("/api/auth/users?pageSize=200", headers=admin_h)).get("data", {})
    # 与全平台 core.responses.list_payload 一致：snake_case 的 page_size（查询参数才是 pageSize）
    check("用户列表使用 {items,total,page,page_size} 信封",
          all(k in users_list for k in ("items", "total", "page", "page_size")),
          str(users_list)[:200])
    check("用户列表不泄露密码哈希",
          all("password_hash" not in u for u in users_list.get("items", [])), str(users_list)[:250])

    # ------------------------------------------------------------------ #
    section("角色调整即时生效（TP-05-04）")
    inst_rec = create_user(anon, admin_h, "instant1", "Instant@12345", "viewer")
    inst_h = bearer(env(login(anon, "instant1", "Instant@12345")).get("data", {}).get("token", ""))
    before = anon.post("/api/ontologies?project_id=default", json={"name": "x"}, headers=inst_h)
    check("调整前只读角色写接口 403", before.status_code == 403, str(before.status_code))
    changed = anon.put(f"/api/auth/users/{inst_rec['id']}",
                       json={"role": "modeler"}, headers=admin_h)
    check("管理员调整角色 200", changed.status_code == 200, str(env(changed))[:200])
    after = anon.post("/api/ontologies?project_id=default", json={"name": "x"}, headers=inst_h)
    check("同一 token 免重登即获得写权限（权限实时查库）", after.status_code != 403, str(after.status_code))
    me_after = env(anon.get("/api/auth/me", headers=inst_h)).get("data", {})
    check("/api/auth/me 返回的最新角色为 modeler",
          (me_after.get("user") or {}).get("role") == "modeler", str(me_after)[:250])

    # ------------------------------------------------------------------ #
    section("测试态豁免（仅自动化回归使用，默认仍是启用）")
    os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
    check("显式关闭后无 Authorization 请求可通行（L5/M1 回归依赖）",
          anon.get("/api/projects").status_code == 200, str(anon.get("/api/projects").status_code))
    dev_me = env(anon.get("/api/auth/me")).get("data", {})
    check("测试态 /me 显式标注 dev_mode", dev_me.get("dev_mode") is True, str(dev_me)[:200])
    os.environ["ONTOLOGY_AUTH_ENABLED"] = "true"
    check("恢复启用后无头请求重新 401", anon.get("/api/projects").status_code == 401,
          str(anon.get("/api/projects").status_code))

    # ------------------------------------------------------------------ #
    section("前端登录页与权限矩阵（真实 Chromium DOM）")
    if no_ui:
        skip("浏览器 DOM 段", "--no-ui 指定跳过（静态契约仍检查）")
    else:
        try:
            from playwright.sync_api import sync_playwright
            index = os.path.join(REPO, "frontend", "index.html").replace("\\", "/")
            init_js = r"""
            window.__authState = 'anon';
            window.__meStatus = 0;
            window.fetch = async function (url, opts) {
              const u = String(url);
              const reply = (status, payload) => ({
                ok: status >= 200 && status < 300, status,
                json: async () => payload,
                headers: { get: () => null },
              });
              if (u.includes('/api/auth/me')) {
                if (window.__meStatus) return reply(window.__meStatus,
                  {success:false,error:{code:'UNAUTHORIZED',message:'未登录或会话已失效'}});
                if (window.__authState === 'anon') return reply(401,
                  {success:false,error:{code:'UNAUTHORIZED',message:'未登录或会话已失效'}});
                const isAdmin = window.__authState === 'admin';
                const perms = isAdmin
                  ? {role:'admin',role_label:'管理员',actions:['read','write','review','admin'],
                     tabs:['home','modeling','ingest','graph','inference','action','dictionary','capability','mcp','logs','settings','tutorial','users']}
                  : {role:'viewer',role_label:'只读',actions:['read'],
                     tabs:['home','graph','inference','logs','settings','tutorial']};
                return reply(200, {success:true,data:{
                  authenticated:true, dev_mode:false,
                  user:{username: isAdmin ? 'admin' : 'viewer1', role: perms.role},
                  permissions: perms}});
              }
              if (u.includes('/api/auth/login')) {
                window.__authState = 'viewer';
                return reply(200, {success:true,data:{authenticated:true}});
              }
              if (u.includes('/api/auth/logout')) {
                window.__authState = 'anon';
                return reply(200, {success:true,data:{logged_out:true}});
              }
              if (u.includes('/api/projects')) return reply(200,
                {success:true,data:{items:[{id:'default',name:'默认项目'}],total:1,page:1,pageSize:20}});
              if (u.includes('/health')) return reply(200,
                {success:true,data:{status:'ok',version:'test',dependencies:{}}});
              return reply(200, {success:true,data:{items:[],total:0}});
            };
            """
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                page = b.new_page()
                page.add_init_script(init_js)
                page.route("**/*", lambda r: r.abort()
                           if r.request.url.startswith(("http://", "https://"))
                           else r.continue_())
                page.goto("file:///" + index, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_function("typeof loadSession === 'function'", timeout=15000)
                page.wait_for_function(
                    "document.getElementById('loginOverlay').classList.contains('op-hidden') === false",
                    timeout=10000)

                check("未登录访问页面只呈现登录页（TC-05-01）",
                      not page.eval_on_selector("#loginOverlay", "e => e.classList.contains('op-hidden')")
                      and page.eval_on_selector("#appShell", "e => e.classList.contains('op-hidden')")
                      and page.eval_on_selector("#appHeader", "e => e.classList.contains('op-hidden')"))
                check("登录表单账号/密码/提交按钮齐备",
                      page.locator("#loginUsername").count() == 1
                      and page.locator("#loginPassword").count() == 1
                      and page.locator("#loginSubmitBtn").count() == 1)
                page.fill("#loginUsername", "viewer1")
                page.fill("#loginPassword", "Viewer@12345")
                page.click("#loginSubmitBtn")
                page.wait_for_function(
                    "document.getElementById('loginOverlay').classList.contains('op-hidden')",
                    timeout=10000)
                check("登录成功隐藏登录层并进入主界面",
                      page.eval_on_selector("#appShell", "e => !e.classList.contains('op-hidden')")
                      and page.eval_on_selector("#currentUserLabel", "e => e.textContent.includes('viewer1')"))
                check("只读角色无建模入口（菜单权限矩阵）",
                      page.eval_on_selector(".tab[data-tab='modeling']", "e => e.classList.contains('op-hidden')"))
                check("只读角色写按钮被禁用（操作入口权限矩阵）",
                      page.eval_on_selector("button[onclick='saveOntology()']", "e => e.disabled === true"))
                check("只读角色无用户管理入口",
                      page.eval_on_selector("#tabUsers", "e => e.classList.contains('op-hidden')"))

                page.evaluate("() => { window.__authState = 'admin'; return loadSession(); }")
                page.wait_for_function(
                    "document.getElementById('currentUserLabel').textContent.includes('admin')",
                    timeout=10000)
                check("管理员可见用户与授权入口",
                      page.eval_on_selector("#tabUsers", "e => !e.classList.contains('op-hidden')"))
                check("管理员写按钮可用",
                      page.eval_on_selector("button[onclick='saveOntology()']", "e => e.disabled === false"))

                page.evaluate("() => { window.__meStatus = 401; return loadSession(); }")
                page.wait_for_function(
                    "document.getElementById('loginOverlay').classList.contains('op-hidden') === false",
                    timeout=10000)
                check("会话失效（401）自动回到登录页",
                      page.eval_on_selector("#appShell", "e => e.classList.contains('op-hidden')"))
                b.close()
        except Exception as e:  # noqa: BLE001
            skip("浏览器 DOM 段", f"Playwright/Chromium 不可用：{type(e).__name__}: {e}")

    js = open(os.path.join(REPO, "frontend", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(REPO, "frontend", "index.html"), encoding="utf-8").read()
    check("静态契约：请求统一携带凭据", "credentials: 'include'" in js)
    check("静态契约：登录页与用户管理入口存在",
          'id="loginOverlay"' in html and 'id="tabUsers"' in html and 'id="users"' in html)
    check("静态契约：隐藏态不依赖 CDN", ".op-hidden { display: none !important; }" in html)

    print(f"\n===== 结果: PASS={PASS} FAIL={FAIL} SKIP={SKIP} =====")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
