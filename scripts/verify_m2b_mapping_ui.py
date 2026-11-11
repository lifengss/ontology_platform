#!/usr/bin/env python3
"""verify_m2b_mapping_ui.py — V2-14i 凭据映射管理 UI 验收（M2-B 遗留收口）

TC-14-04 的 UI 视角＋RBAC 负向：Playwright（Chromium headless）驱动真实前端页面，
后端为独立 uvicorn 进程（临时 DATA_DIR＋鉴权启用态）。映射的 REST 契约已由
verify_m2b_identity_federation.py（19/19）验收；本脚本只验证 UI：
建/列表/状态/删除/明文不回显/admin 管理他人/viewer 只读。

流程：
  ① REST 前置（会话 Cookie）：alice(modeler) 建项目＋数据源连接
  ② alice UI：凭据映射 tab 可见 → 新建映射（连接/凭据类型/principal/secret 一次性）
     → 列表「已加密存储」→ 明文不出现在页面 DOM 与任何 /api/identity/ 响应
     → 删除 → 行消失 → 重建（供 ④ admin 跨用户验证）
  ③ carol(admin) UI：「管理用户」下拉可见 → 选 alice → 可见其映射并删除
  ④ bob(viewer) UI：tab 可见（账号级能力）但「新建映射」按钮禁用（write 动作）
     → REST 负向：POST /api/identity/mappings → 403（中间件 write 判定）

运行：PYTHONUTF8=1 python scripts/verify_m2b_mapping_ui.py
依赖：playwright（chromium）；端口 8000（API）/8123（前端静态）需空闲。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
DATA = BACKEND / "data" / "_m2bcred"
API_URL = "http://127.0.0.1:8000"
# 与 TCE2E 相同：页面源必须用 localhost（SameSite cookie 同站跨端口），API 直连 127.0.0.1
UI_URL = "http://localhost:8123/index.html"

os.environ.setdefault("PYTHONUTF8", "1")

SECRET_PLAIN = "s3cret-plain-XYZ-20261112"  # 一次性明文；断言其绝不回显
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


def _wait_http(url: str, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _start_static_server(port: int = 8123):
    """进程内多线程静态服务（dual-stack :: 绑定，localhost IPv4/IPv6 均可达）。"""
    import socket
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(FRONTEND), **kw)

        def log_message(self, fmt, *args):
            print(f"  [STATIC] {self.client_address[0]} {fmt % args}", flush=True)

        def log_error(self, fmt, *args):
            print(f"  [STATIC-ERR] {self.client_address[0]} {fmt % args}", flush=True)

    class _Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6
        daemon_threads = True
        allow_reuse_address = True

    class _ServerV4(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        httpd = _Server(("::", port), _Handler)
    except OSError:
        httpd = _ServerV4(("0.0.0.0", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _toasts(page) -> str:
    try:
        return page.eval_on_selector_all(
            ".toast", "els => els.map(e => e.innerText).join(' | ')")
    except Exception:
        return ""


def _wait_toast(page, needle: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in _toasts(page):
            return True
        time.sleep(0.4)
    return False


def _login(page, username: str, password: str) -> None:
    page.goto(UI_URL, wait_until="domcontentloaded")
    page.wait_for_selector("#loginOverlay", state="visible", timeout=15000)
    page.fill("#loginUsername", username)
    page.fill("#loginPassword", password)
    page.click("#loginSubmitBtn")
    page.wait_for_selector("#loginOverlay", state="hidden", timeout=15000)


class _Rest:
    """带会话 Cookie 的最小 REST 客户端（仅前置/负向断言用，主流程走 UI）。"""

    def __init__(self, username: str, password: str) -> None:
        import urllib.parse
        self.jar = CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        req = urllib.request.Request(
            f"{API_URL}/api/auth/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        self.op.open(req, timeout=15)

    def call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"{API_URL}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"}, method=method)
        try:
            with self.op.open(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {}


def _setup_prereqs() -> tuple[str, str]:
    """alice 建项目＋数据源连接（REST 前置），返回 (connection_id, project_id)。"""
    alice = _Rest("alice", "alice-pass-123")
    st, pj = alice.call("POST", "/api/projects",
                        {"name": PROJECT_NAME, "description": "TCE14-UI"})
    assert st in (200, 201), f"建项目失败 {st} {pj}"
    project_id = pj["data"]["id"]
    st, cj = alice.call("POST", f"/api/mcp/connections?project_id={project_id}", {
        "display_name": "CRM 外部源",
        "transport": "streamable_http",
        "endpoint": "http://ext-crm.local/mcp",
        "auth": {"type": "none"},
        "maps_to_ontology": {"object_type": "obj_part"},
        "read_schedule": "manual",
    })
    assert st in (200, 201), f"建连接失败 {st} {cj}"
    conn_id = cj["data"]["connection_id"]
    return conn_id, project_id


PROJECT_NAME = "凭据映射UI验收项目"


def _enter_project(page, name: str = PROJECT_NAME) -> None:
    """首页进入项目（前端 api() 注入 project_id 需与 REST 前置一致）。"""
    page.click('[data-tab="home"]')
    page.click(f'#projectTableBody tr:has-text("{name}") button:has-text("进入")')
    page.wait_for_timeout(800)


def _open_cred_tab(page) -> None:
    page.click('[data-tab="credentials"]')
    page.wait_for_selector("#credTable", state="attached", timeout=10000)
    page.wait_for_timeout(800)  # 列表/状态为异步渲染


def _create_mapping_ui(page, conn_id: str, principal: str, secret: str) -> None:
    page.click("#credNewBtn")
    page.wait_for_selector("#credEditor:not(.hidden)", timeout=5000)
    try:
        # 折叠 select 下的 option 在 Playwright 可见性语义里恒为 hidden，须用 attached 而非 visible
        page.wait_for_selector("#credConn option[value='" + conn_id + "']",
                               state="attached", timeout=10000)
    except Exception:
        print("  [DBG] connList:", page.evaluate("JSON.stringify(connList.map(c => c.connection_id))"))
        print("  [DBG] currentProject:",
              page.evaluate("state.currentProject ? (state.currentProject.id + '|' + state.currentProject.name) : null"))
        print("  [DBG] credConn html:",
              page.eval_on_selector_all("#credConn", "els => els.map(e => e.innerHTML).join('')")[:300])
        print("  [DBG] toasts:", _toasts(page))
        raise
    page.select_option("#credConn", conn_id)
    page.select_option("#credAuthType", "basic")
    page.fill("#credPrincipal", principal)
    page.fill("#credSecret", secret)
    page.click('#credEditor button:has-text("保存")')


def _accept_dialogs(page) -> None:
    page.on("dialog", lambda d: d.accept())


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    env = dict(os.environ,
               DATA_DIR=str(DATA),
               ONTOLOGY_AUTH_ENABLED="true",
               ONTOLOGY_PBKDF2_ITERATIONS="10000",
               ONTOLOGY_CRED_MASTER_KEY="credui-master-key-32bytes!!0123456",
               JENA_BASE_URL="http://127.0.0.1:3030",
               NEO4J_URI="bolt://127.0.0.1:7687",
               POSTGRES_DSN="postgresql://ont:ontsecret@127.0.0.1:5432/ont")

    # 进程内预建账号必须与 uvicorn 子进程同一数据目录：先同步环境变量再导入
    sys.path.insert(0, str(BACKEND))
    os.environ.update(env)
    from config import get_settings  # noqa: E402

    get_settings.cache_clear()
    import auth as auth_svc  # noqa: E402

    for u, p, r in (("alice", "alice-pass-123", "modeler"),
                    ("carol", "carol-pass-123", "admin"),
                    ("bob", "bob-pass-123", "viewer")):
        auth_svc.create_user(u, p, role=r, tenant_id="default")

    uv_err = BACKEND / "_uvicorn_credui.err"
    with open(uv_err, "wb") as ef:
        uv = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", "8000", "--log-level", "info"],
            cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=ef)
    httpd = _start_static_server(8123)
    try:
        ok_api = _wait_http(f"{API_URL}/health")
        ok_ui = _wait_http(UI_URL)
        check("真实后端(8000)就绪", ok_api, "")
        check("前端静态服务(8123)就绪", ok_ui, "")
        if not (ok_api and ok_ui):
            tail = uv_err.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
            print("\n服务未就绪，提前退出。uvicorn 日志末尾：")
            print("\n".join(tail))
            return 1

        conn_id, project_id = _setup_prereqs()
        check("REST 前置：项目＋数据源连接已建", bool(conn_id), f"project={project_id}")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                # ---------- ② alice(modeler)：建/列/状态/不回显/删 ----------
                banner("② alice(modeler) 凭据映射 UI 全流程")
                ctx_a = browser.new_context()
                pa = ctx_a.new_page()
                _login(pa, "alice", "alice-pass-123")
                pa.wait_for_selector("#appShell", state="visible", timeout=10000)
                chip = pa.text_content("#currentUserLabel") or ""
                check("alice 登录", "alice" in chip, chip)
                _enter_project(pa)
                tab_visible = pa.locator('[data-tab="credentials"]').is_visible()
                check("凭据映射 tab 对 modeler 可见", tab_visible, "")

                # 网络侧断言：任何 /api/identity/ 响应体不含明文
                leak = []

                def _watch(resp):
                    if "/api/" in resp.url:
                        print(f"  [API] {resp.status} {resp.request.method} {resp.url}", flush=True)
                    if "/api/identity/" in resp.url:
                        try:
                            if SECRET_PLAIN in resp.text():
                                leak.append(resp.url)
                        except Exception:
                            pass

                pa.on("response", _watch)

                _open_cred_tab(pa)
                check("映射列表容器渲染", pa.locator("#credTable").count() == 1, "")
                check("初始为空（暂无凭据映射）",
                      "暂无凭据映射" in (pa.text_content("#credTable") or ""),
                      (pa.text_content("#credTable") or "")[:80])

                _create_mapping_ui(pa, conn_id, "alice@crm", SECRET_PLAIN)
                pa.wait_for_selector('#credTable tr:has-text("alice@crm")', timeout=15000)
                row_txt = pa.text_content('#credTable tr:has-text("alice@crm")') or ""
                check("新建后列表含 principal", "alice@crm" in row_txt, row_txt[:100])
                check("凭据状态=已加密存储", "已加密存储" in row_txt, row_txt[:120])
                check("页面 DOM 不含明文", SECRET_PLAIN not in pa.content(), "")
                time.sleep(0.5)
                check("任何 /api/identity/ 响应不含明文", not leak, str(leak))
                check("modeler 有删除按钮",
                      pa.locator('#credTable tr:has-text("alice@crm") button:has-text("删除")').count() == 1, "")

                # 删除 → 行消失
                _accept_dialogs(pa)
                pa.click('#credTable tr:has-text("alice@crm") button:has-text("删除")')
                try:
                    pa.wait_for_selector('#credTable tr:has-text("alice@crm")',
                                         state="detached", timeout=15000)
                    check("删除后行消失", True, "")
                except Exception:
                    check("删除后行消失", False, pa.text_content("#credTable") or "")

                # 重建（供 admin 跨用户验证）
                _create_mapping_ui(pa, conn_id, "alice@crm", "other-secret-000")
                pa.wait_for_selector('#credTable tr:has-text("alice@crm")', timeout=15000)
                check("重建映射成功", True, "")
                ctx_a.close()

                # ---------- ③ carol(admin)：管理他人 ----------
                banner("③ carol(admin) 管理他人映射")
                ctx_b = browser.new_context()
                pb = ctx_b.new_page()
                _login(pb, "carol", "carol-pass-123")
                pb.wait_for_selector("#appShell", state="visible", timeout=10000)
                _enter_project(pb)
                _open_cred_tab(pb)
                check("admin 可见「管理用户」下拉",
                      pb.locator("#credUserBox").is_visible(), "")
                # 精确选中 alice（排除 carol 自己）
                val = pb.eval_on_selector_all(
                    "#credUser option",
                    "els => { for (const o of els) if (o.textContent.includes('alice')) return o.value; return null; }")
                check("管理用户下拉含 alice", bool(val), "")
                if val:
                    pb.select_option("#credUser", val)
                    pb.wait_for_timeout(1500)
                check("admin 切到 alice 后列表含其映射",
                      "alice@crm" in (pb.text_content("#credTable") or ""),
                      (pb.text_content("#credTable") or "")[:120])
                _accept_dialogs(pb)
                pb.click('#credTable tr:has-text("alice@crm") button:has-text("删除")')
                try:
                    pb.wait_for_selector('#credTable tr:has-text("alice@crm")',
                                         state="detached", timeout=15000)
                    check("admin 删除 alice 的映射后行消失", True, "")
                except Exception:
                    check("admin 删除 alice 的映射后行消失", False,
                          pb.text_content("#credTable") or "")
                ctx_b.close()

                # ---------- ④ bob(viewer)：tab 可见但只读 ----------
                banner("④ bob(viewer) RBAC 负向")
                ctx_c = browser.new_context()
                pc = ctx_c.new_page()
                _login(pc, "bob", "bob-pass-123")
                pc.wait_for_selector("#appShell", state="visible", timeout=10000)
                _enter_project(pc)
                check("viewer 凭据映射 tab 可见（账号级能力）",
                      pc.locator('[data-tab="credentials"]').is_visible(), "")
                _open_cred_tab(pc)
                btn_disabled = pc.locator("#credNewBtn").is_disabled()
                check("viewer 的「新建映射」按钮禁用（write 动作）", btn_disabled, "")
                check("viewer 管理用户下拉不可见",
                      not pc.locator("#credUserBox").is_visible(), "")
                ctx_c.close()

                # REST 负向：viewer POST 建映射 → 403（中间件 write 判定）
                bob = _Rest("bob", "bob-pass-123")
                st, body = bob.call("POST", f"/api/identity/mappings?project_id={project_id}", {
                    "user_id": "x", "connection_id": conn_id,
                    "auth_type": "basic", "secret": "should-fail"})
                check("REST 负向：viewer 建映射 403", st == 403, f"status={st} {str(body)[:120]}")
            finally:
                browser.close()
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        try:
            uv.terminate()
            uv.wait(timeout=10)
        except Exception:
            pass

    print(f"\nTOTAL PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
