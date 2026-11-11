#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""TCE2E-05 / TCE2E-06 端到端联验（真实浏览器 + 真实后端 + 真实 JWT Cookie）。

与既有 API 级验收（verify_m2a/m2c/m2d/m2e）不同，本脚本把 frontend/ 真实前端页面
用 Playwright（Chromium headless）驱动，后端为独立 uvicorn 进程（临时 DATA_DIR＋
鉴权启用态），全程走浏览器 UI（表单/按钮/上传），不使用接口捷径。

TCE2E-05 浏览器侧建模全链路（P0）：
  ① 登录 → ② 建项目 → ③ 定义本体（对象/属性/约束表单）→ ④ 配置数据字典
  → ⑤ 联邦查询预览（dry_run）→ ⑥ 审批发布（首发布 1.0 基线）
  → ⑦ 手工确认入库（SHACL 门禁：违规批次拦截＋合规批次入库）→ ⑧ 查询页验证
  （注：入库页下拉只列已发布本体，故发布必须先于入库）

TCE2E-06 跨角色浏览器协作流（P1）：
  三个独立浏览器上下文（alice/modeler、rita/reviewer、bob/viewer）：
  建模提交变更（表单内删属性→不兼容→大版本审批，已发布本体直编被 409 拦截）
  → 提交人不可自审负向断言 → 他人会签审批发布
  → 只读角色 RBAC 负向断言（未授权 tab 隐藏、写按钮禁用）→ 跨本体查询（图谱）可用。

运行：PYTHONUTF8=1 python scripts/verify_m2_endtoend.py
依赖：playwright（chromium）；端口 8000（API）/8123（前端静态）需空闲。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
DATA = BACKEND / "data" / "_m2e2e"
API_URL = "http://127.0.0.1:8000"
# 前端必须从 localhost:8123 访问（而非 127.0.0.1）：app.js 的 API_BASE 固定为
# http://localhost:8000，而 127.0.0.1 与 localhost 在 SameSite 判定中是不同站点，
# 页面源用 127.0.0.1 时，后端 Set-Cookie(SameSite=Lax) 会被浏览器视为跨站而拒存，
# 导致登录后 /api/auth/me 恒 401。localhost 同站跨端口则正常携带 cookie。
UI_URL = "http://localhost:8123/index.html"

os.environ.setdefault("PYTHONUTF8", "1")

PASS = 0
FAIL = 0
_LOGS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        _LOGS.append(f"[PASS] {name}")
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        _LOGS.append(f"[FAIL] {name} :: {detail}")
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
    """进程内多线程静态服务（daemon 线程＋逐请求日志）。

    原用 `python -m http.server` 子进程（stderr=DEVNULL）：进程一旦失联完全不可观测，
    曾出现第二次 goto 30s 超时却查无日志。改为进程内 ThreadingHTTPServer
    （每请求一线程、dual-stack :: 绑定使 localhost 的 IPv6/IPv4 均可达），逐请求打印日志。
    """
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
    """轮询 .toast 文本，命中 needle 即返回 True。

    前端 toast 默认 4s 后自动移除（duration=4000），固定 sleep 8s 再读会漏掉，
    故改为 0.4s 粒度轮询、尽早捕获。
    """
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


def tce2e_05(browser) -> None:
    """TCE2E-05 浏览器侧建模全链路。

    ①–⑥＋⑦上传：alice/modeler；⑦入库确认（approve/reject 属 review 动作）：rita/reviewer；
    ⑧查询页验证：alice（上传者，实例内容默认仅上传者可读）。
    """
    banner("TCE2E-05 浏览器侧建模全链路")
    ctx = browser.new_context()
    page = ctx.new_page()

    # ---------- ① 浏览器登录 ----------
    _login(page, "alice", "alice-pass-123")
    page.wait_for_selector("#appShell", state="visible", timeout=10000)
    chip = page.text_content("#currentUserLabel") or ""
    check("① 浏览器登录（alice/modeler）进入主界面",
          "alice" in chip and "建模人" in chip, chip)

    # ---------- ② 建项目 ----------
    page.click('[data-tab="home"]')
    page.click('button:has-text("＋ 新建项目")')
    page.wait_for_selector("#projectForm", state="visible", timeout=5000)
    page.fill("#newProjectName", "E2E 端到端项目")
    page.fill("#newProjectDesc", "TCE2E-05 全链路验证")
    page.click('#projectForm button:has-text("创建")')
    page.wait_for_selector("#projectForm", state="hidden", timeout=5000)
    page.wait_for_selector('#projectTableBody tr:has-text("E2E 端到端项目")', timeout=20000)
    check("② 浏览器表单建项目成功", True, "")
    page.click('#projectTableBody tr:has-text("E2E 端到端项目") button:has-text("进入")')
    page.wait_for_timeout(600)
    # enterProject = setProject + switchTab('modeling')：进入后应已在建模页
    ol = page.text_content("#ontologyList") or ""
    check("进入项目（已切到建模页、项目上下文切换成功）",
          page.is_visible("#ontologyList"), ol[:120])

    # ---------- ③ 定义本体（对象/属性/约束表单） ----------
    page.click('[data-tab="modeling"]')
    page.click('button:has-text("+ 新建")')
    page.wait_for_selector("#formName", state="visible", timeout=5000)
    page.fill("#formName", "E2E 零件本体")
    page.fill("#formDesc", "TCE2E-05 表单建模")
    page.fill("#formVersion", "1")
    # 对象 1：obj_part / 零件（覆盖默认对象 obj_customer）
    page.fill(".obj-id[data-idx='0']", "obj_part")
    page.fill(".obj-name[data-idx='0']", "零件")
    # 属性 1：pn（string 必填，长度 4~64 → SHACL 门禁钩子）
    page.fill(".attr-id[data-aidx='0']", "pn")
    page.fill(".attr-name[data-aidx='0']", "零件号")
    page.select_option(".attr-type[data-aidx='0']", "string")
    page.check(".attr-required[data-aidx='0']")
    # .attr-row 行 div 本身无 data-aidx（挂在行内 input 上），须用 :has() 定位行
    page.click(".attr-row:has([data-aidx='0']) span:has-text('约束')")
    page.fill(".c-minlen[data-aidx='0']", "4")
    page.fill(".c-maxlen[data-aidx='0']", "64")
    # 属性 2：cost（float，0~1000000）
    page.click('button:has-text("+ 添加属性")')
    page.fill(".attr-id[data-aidx='1']", "cost")
    page.fill(".attr-name[data-aidx='1']", "成本")
    page.select_option(".attr-type[data-aidx='1']", "float")
    page.click(".attr-row:has([data-aidx='1']) span:has-text('约束')")
    page.fill(".c-min[data-aidx='1']", "0")
    page.fill(".c-max[data-aidx='1']", "1000000")
    # 属性 3：status（string，枚举 A/B）
    page.click('button:has-text("+ 添加属性")')
    page.fill(".attr-id[data-aidx='2']", "status")
    page.fill(".attr-name[data-aidx='2']", "状态")
    page.select_option(".attr-type[data-aidx='2']", "string")
    page.click(".attr-row:has([data-aidx='2']) span:has-text('约束')")
    page.fill(".c-enum[data-aidx='2']", "A, B")
    page.click('#editorCard button:has-text("保存本体")')
    page.wait_for_selector('#ontologyList .group:has-text("E2E 零件本体")', timeout=20000)
    check("③ 表单建模保存（obj_part＋pn/cost/status＋约束），本体出现在列表", True, "")

    # ---------- ④ 配置数据字典 ----------
    page.click('[data-tab="dictionary"]')
    page.wait_for_selector("#dictTermsBody", timeout=10000)
    page.click('button:has-text("＋ 新建词条")')
    page.wait_for_selector("#dictTermEditor", state="visible", timeout=5000)
    page.fill("#dictTCan", "零件号")
    page.fill("#dictTObj", "obj_part")
    page.fill("#dictTProp", "pn")
    page.fill("#dictTSyn", "PN, 编号")
    page.click('#dictTermEditor button:has-text("保存")')
    page.wait_for_selector('#dictTermsBody >> text=零件号', timeout=20000)
    terms = page.text_content("#dictTermsBody") or ""
    check("④ 数据字典词条配置成功", "零件号" in terms and "obj_part" in terms, terms[:200])

    # ---------- ⑤ 联邦查询预览（dry_run） ----------
    page.fill("#dictLookupTerm", "零件号")
    page.fill("#dictLookupValue", "PN-100001")
    page.click('button:has-text("成本预览 (dry_run)")')
    res = ""
    for _ in range(24):
        time.sleep(0.5)
        res = (page.text_content("#dictResult") or "").strip()
        if ("（preview）" in res or "（ok）" in res
                or "（cost_confirmation_required）" in res
                or "查询失败" in res or "（error）" in res):
            break
    check("⑤ 联邦查询预览（dry_run）有结果且未失败",
          bool(res) and ("（preview）" in res or "（ok）" in res)
          and "查询失败" not in res, res[:200])

    # ---------- ⑥ 审批发布（首发布 1.0 基线） ----------
    # 注意：入库页下拉（#ingestOntology）只列 active 本体，故发布必须先于入库
    page.click('[data-tab="modeling"]')
    page.wait_for_selector('#ontologyList .group:has-text("E2E 零件本体")', timeout=10000)
    page.click('#ontologyList .group:has-text("E2E 零件本体")')
    page.wait_for_selector("#btnPublish", state="visible", timeout=10000)
    page.click("#btnPublish")
    published = _wait_toast(page, "本体已发布", 15)
    page.wait_for_function(
        "document.getElementById('detailBadge').textContent === '已发布'",
        timeout=15000)
    check("⑥ 浏览器发布（active＋1.0 基线写入，详情页徽章=已发布）", published, "")

    # ⑥b 共享等级 private→department（V2-06 新建本体默认私有仅本人可见；
    #     调整为部门级后同租户的 reviewer/viewer 才能看到本体，是 ⑦ 与 06 的协作前置）
    page.click('#detailSharing button:has-text("调整")')
    page.wait_for_selector("#sharingEditor", state="visible", timeout=5000)
    page.select_option("#sharingLevel", "department")
    page.click('#sharingEditor button:has-text("保存")')
    shared = _wait_toast(page, "共享等级已更新", 10)
    check("⑥b 共享等级调整为部门（同租户可见）", shared, "")

    # ---------- ⑦ 手工确认入库（SHACL 门禁；RBAC：approve/reject 属 review 动作） ----------
    # API-INTERFACE-DOC §21.3：/approve|reject|revert|batch-* → review 动作。
    # modeler（alice）只能上传生成候选，无审批入口（TCE2E-06 负向断言）；
    # 入库确认由 reviewer（rita）独立上下文执行。
    page.click('[data-tab="ingest"]')
    page.wait_for_selector("#ingestOntology", timeout=10000)
    page.select_option("#ingestOntology", label="E2E 零件本体")
    page.wait_for_selector("#ingestTarget >> text=零件", timeout=20000)
    page.click("#ingestTarget >> text=零件")
    page.wait_for_selector("#ingestFile", state="visible", timeout=10000)
    check("⑦a 入库页选中对象（实体树 UI 点击）", True, "")

    def _upload_csv(name: str, text: str) -> None:
        p = DATA / name
        p.write_text(text, encoding="utf-8")
        page.click("#subtabObject")
        page.set_input_files("#ingestFile", str(p))
        page.click('button:has-text("上传生成候选")')
        page.click("#subtabReview")

    # 两组候选：违规批（2 行：X1 的 pn 长度 2<4、status=Z 不在枚举 A/B）＋合规批（1 行）
    _upload_csv("e2e_bad.csv", "pn,cost,status\nPN-100001,100.5,A\nX1,50,Z\n")
    page.wait_for_selector(
        '#candidateList tbody tr:has(td:nth-child(4):text-is("2"))', timeout=20000)
    _upload_csv("e2e_ok.csv", "pn,cost,status\nPN-100001,100.5,A\n")
    page.wait_for_selector(
        '#candidateList tbody tr:has(td:nth-child(4):text-is("1"))', timeout=20000)
    check("⑦b CSV 上传生成 2 组待确认候选（违规批 2 行＋合规批 1 行）", True, "")

    # 负向断言（TCE2E-06 RBAC）：modeler 无 review 动作 → 行内无审批入口
    n_approve = page.locator('#candidateList button:has-text("批准入库")').count()
    n_reject = page.locator('#candidateList button:has-text("驳回")').count()
    check("⑦c 负向：modeler 无入库确认/驳回入口（RBAC 矩阵）",
          n_approve == 0 and n_reject == 0, f"approve={n_approve} reject={n_reject}")

    # rita/reviewer：独立上下文执行入库确认
    ctx_r = browser.new_context()
    pr = ctx_r.new_page()
    _login(pr, "rita", "rita-pass-123")
    pr.wait_for_selector("#appShell", state="visible", timeout=10000)
    pr.click('[data-tab="home"]')
    pr.click('#projectTableBody tr:has-text("E2E 端到端项目") button:has-text("进入")')
    pr.wait_for_timeout(600)
    pr.click('[data-tab="ingest"]')
    pr.wait_for_selector("#ingestOntology", timeout=10000)
    pr.select_option("#ingestOntology", label="E2E 零件本体")
    pr.wait_for_selector("#subtabReview", timeout=10000)
    pr.click("#subtabReview")
    pr.wait_for_selector('#candidateList tbody tr:has(td:nth-child(4):text-is("2"))', timeout=20000)
    n_approve_r = pr.locator('#candidateList button:has-text("批准入库")').count()
    check("⑦d reviewer 可见入库确认入口（2 行待审核）", n_approve_r == 2, f"count={n_approve_r}")

    # SHACL 门禁：批准违规批 → 整批拦截、候选仍待审核、零入库
    pr.locator('#candidateList tbody tr:has(td:nth-child(4):text-is("2"))') \
      .locator('button:has-text("批准入库")').click()
    blocked = _wait_toast(pr, "SHACL", 15)
    pr.wait_for_selector(
        '#candidateList tbody tr:has(td:nth-child(4):text-is("2")):has-text("待审核")',
        timeout=10000)
    pr.click("#subtabObject")
    inst = pr.text_content("#instanceList") or ""
    check("⑦e SHACL 门禁拦截违规批次（整批拒绝，候选仍待审核）", blocked, inst[:200])
    check("违规批次零入库（PN-100001 与 X1 均未入）",
          "PN-100001" not in inst and "X1" not in inst, inst[:200])

    # 驳回违规批 → 已驳回
    pr.click("#subtabReview")
    pr.locator('#candidateList tbody tr:has(td:nth-child(4):text-is("2"))') \
      .locator('button:has-text("驳回")').click()
    pr.wait_for_selector(
        '#candidateList tbody tr:has(td:nth-child(4):text-is("2")):has-text("已驳回")',
        timeout=20000)
    check("⑦f 违规批次驳回 → 已驳回", True, "")

    # 批准合规批 → 入库（Neo4j 降级 JSONL 回退，约 4s）
    pr.locator('#candidateList tbody tr:has(td:nth-child(4):text-is("1"))') \
      .locator('button:has-text("批准入库")').click()
    imported = _wait_toast(pr, "候选已入库", 30)
    pr.wait_for_selector(
        '#candidateList tbody tr:has(td:nth-child(4):text-is("1")):has-text("已入库")',
        timeout=20000)
    check("⑦g 合规批次确认入库（toast 成功＋候选→已入库）", imported, "")
    ctx_r.close()

    # ---------- ⑧ 查询页验证（alice=上传者；实例内容默认仅上传者可读） ----------
    # 子 tab 切换只重渲染全局缓存、不重新拉取 → reload 获取最新状态；
    # bootApp 不加载本体列表（loadOntologies 仅在 enterProject 触发）→ reload 后须重新「进入」项目
    page.reload()
    page.wait_for_selector("#appShell", state="visible", timeout=15000)
    page.click('[data-tab="home"]')
    page.click('#projectTableBody tr:has-text("E2E 端到端项目") button:has-text("进入")')
    page.wait_for_timeout(600)
    page.click('[data-tab="ingest"]')
    page.wait_for_selector("#ingestOntology", timeout=10000)
    page.select_option("#ingestOntology", label="E2E 零件本体")
    page.wait_for_selector("#ingestTarget >> text=零件", timeout=20000)
    page.click("#ingestTarget >> text=零件")
    page.click("#subtabObject")
    page.wait_for_selector("#instanceList >> text=PN-100001", timeout=30000)
    oc = (page.text_content("#instanceCount") or "").strip()
    page.click("#subtabAll")
    page.wait_for_selector("#allInstanceList >> text=PN-100001", timeout=20000)
    alll = page.text_content("#allInstanceList") or ""
    allc = (page.text_content("#allInstanceCount") or "").strip()
    check("⑧ 查询页验证入库结果（对象视图＋全部实例）",
          allc.startswith("共 1") and "共 1 条" in oc, f"count={allc} list={alll[:150]}")
    check("⑧ 违规行 X1 未入库", "X1" not in alll, "")

    ctx.close()


def tce2e_06(browser) -> None:
    """TCE2E-06 跨角色协作流（三独立浏览器上下文）。"""
    banner("TCE2E-06 跨角色浏览器协作流（RBAC 矩阵＋负向断言）")

    # ---------- ① alice(modeler)：建模→提交变更（不兼容→大版本审批） ----------
    ctx_a = browser.new_context()
    pa = ctx_a.new_page()
    _login(pa, "alice", "alice-pass-123")
    pa.wait_for_selector("#appShell", state="visible", timeout=10000)
    check("① alice(modeler) 登录", True, "")

    pa.click('[data-tab="home"]')
    pa.click('#projectTableBody tr:has-text("E2E 端到端项目") button:has-text("进入")')
    pa.wait_for_timeout(600)
    pa.click('[data-tab="modeling"]')
    pa.wait_for_selector('#ontologyList .group:has-text("E2E 零件本体")', timeout=20000)
    # 负向断言：modeler 有写权限 → 写按钮可用
    nw = pa.locator('button:has-text("+ 新建")')
    wbtn_ok = nw.count() > 0 and nw.first.evaluate(
        "el => !el.disabled && !el.classList.contains('op-hidden')")
    check("alice 写按钮可用（＋新建未禁用）", wbtn_ok, "")
    # 详情面板 →「提交变更（V2-07）」→ 表单编辑态（已发布本体直编被后端 409 拦截）
    pa.click('#ontologyList .group:has-text("E2E 零件本体")')
    pa.wait_for_selector('#detailSharing button:has-text("提交变更（V2-07）")',
                         state="visible", timeout=10000)
    cs_btn = pa.locator('#detailSharing button:has-text("提交变更（V2-07）")')
    check("已发布本体显示「提交变更」入口（详情面板）", cs_btn.count() > 0, "")
    pa.click('#detailSharing button:has-text("提交变更（V2-07）")')
    pa.wait_for_selector("#btnFormChangeset", state="visible", timeout=10000)
    # 删 status 属性（不兼容变更：attribute_removed）
    pa.click(".attr-row:has([data-aidx='2']) span.remove-btn")
    pa.wait_for_timeout(400)
    pa.click("#btnFormChangeset")
    pa.wait_for_selector("#changesetEditor", state="visible", timeout=5000)
    pa.fill("#csTargetVersion", "2.0")
    pa.fill("#csNote", "删除 status 属性（E2E 会签流）")
    pa.click('#changesetEditor button:has-text("提交")')
    submitted = _wait_toast(pa, "大版本审批", 15)
    check("② 不兼容变更提交进入大版本审批（pending）", submitted, _toasts(pa)[:250])
    # 负向断言：提交人不可自审（变更单详情无「通过」按钮）
    pa.click('[data-tab="approvals"]')
    pa.wait_for_selector("#csList >> text=待审批", timeout=20000)
    pa.click('#csList button:has-text("详情")')
    pa.wait_for_selector("#csDetail >> text=目标版本", timeout=10000)
    self_btn = pa.locator('#csDetail button:has-text("通过")').count()
    check("负向：提交人不可自审（详情无「通过」按钮）", self_btn == 0, "")
    ctx_a.close()

    # ---------- ② rita(reviewer)：会签审批发布 ----------
    ctx_r = browser.new_context()
    pr = ctx_r.new_page()
    _login(pr, "rita", "rita-pass-123")
    pr.wait_for_selector("#appShell", state="visible", timeout=10000)
    check("② rita(reviewer) 登录", True, "")
    # 负向断言：reviewer 无写 → 建模/字典/用户 tab 隐藏
    hidden_modeling = pr.locator('[data-tab="modeling"]').evaluate(
        "el => el.classList.contains('op-hidden')")
    check("rita 无建模 tab（负向断言）", hidden_modeling, "")
    # 切项目
    pr.click('[data-tab="home"]')
    pr.click('#projectTableBody tr:has-text("E2E 端到端项目") button:has-text("进入")')
    pr.wait_for_timeout(600)
    pr.click('[data-tab="approvals"]')
    pr.wait_for_selector("#csList", timeout=10000)
    pr.wait_for_selector('#csList >> text=待审批', timeout=30000)
    check("审批页可见 pending（待审批）变更单", True, "")
    pr.click('#csList button:has-text("详情")')
    pr.wait_for_selector("#csDetail", state="visible", timeout=5000)
    detail = pr.text_content("#csDetail") or ""
    check("变更单详情（基线 1.0→目标 2.0、提交人 alice、含 diff）",
          "1.0" in detail and "2.0" in detail and "alice" in detail
          and "删除属性" in detail, detail[:250])
    pr.click('#csDetail button:has-text("通过")')
    approved = _wait_toast(pr, "审批通过", 25)
    pr.wait_for_selector('#csList >> text=已通过', timeout=15000)
    check("③ 会签审批通过→发布大版本 2.0", approved, _toasts(pr)[:250])

    # ---------- ④ bob(viewer)：只读＋负向断言＋跨本体查询 ----------
    ctx_b = browser.new_context()
    pb = ctx_b.new_page()
    _login(pb, "bob", "bob-pass-123")
    pb.wait_for_selector("#appShell", state="visible", timeout=10000)
    check("④ bob(viewer) 登录", True, "")
    for tab in ("modeling", "ingest", "dictionary", "approvals", "audit", "users", "mcp"):
        hidden = pb.locator(f'[data-tab="{tab}"]').evaluate(
            "el => el.classList.contains('op-hidden')")
        check(f"bob 无 {tab} tab（RBAC 负向）", hidden, "")
    nw = pb.locator('button:has-text("+ 新建")')
    btn_new_hidden = False
    if nw.count() > 0:
        btn_new_hidden = nw.first.evaluate(
            "el => el.hidden || el.classList.contains('op-hidden') || el.disabled")
    check("bob 写按钮隐藏/禁用（RBAC 负向）", btn_new_hidden, "")
    # 进入项目（viewer 同部门可见），保证图谱选择器有项目上下文
    pb.click('[data-tab="home"]')
    pb.click('#projectTableBody tr:has-text("E2E 端到端项目") button:has-text("进入")')
    pb.wait_for_timeout(600)
    # 跨本体查询：图谱（viewer 只读 tab）
    pb.click('[data-tab="graph"]')
    pb.wait_for_selector("#graphOntology", timeout=10000)
    pb.select_option("#graphOntology", label="E2E 零件本体", timeout=30000)
    pb.wait_for_selector("#graphContainer svg text", timeout=30000)
    pb.wait_for_timeout(4000)  # 等 d3 布局稳定
    gbox = pb.text_content("#graphContainer") or ""
    check("⑤ bob 跨本体查询（图谱）可用且无权限错误",
          ("403" not in gbox) and ("无权" not in gbox) and ("forbidden" not in gbox.lower()),
          gbox[:200])
    # 图谱中应含发布后的本体对象（obj_part 节点）
    check("图谱含 obj_part（跨本体查询数据在）",
          "obj_part" in gbox or "零件" in gbox, gbox[:200])

    ctx_b.close()


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    env = dict(os.environ,
               DATA_DIR=str(DATA),
               ONTOLOGY_AUTH_ENABLED="true",
               ONTOLOGY_PBKDF2_ITERATIONS="10000",
               ONTOLOGY_CRED_MASTER_KEY="e2e-master-key-32bytes!!0123456789",
               # 依赖服务本机未运行（文件降级态）。用 127.0.0.1 替代 localhost：
               # 本机上 localhost 解析出 ::1+127.0.0.1 双地址，每个探测 2×2s，
               # 127.0.0.1 直连单地址 ~2s，显著缩短 /health 与热路径降级探测耗时。
               JENA_BASE_URL="http://127.0.0.1:3030",
               NEO4J_URI="bolt://127.0.0.1:7687",
               POSTGRES_DSN="postgresql://ont:ontsecret@127.0.0.1:5432/ont")

    # 进程内预建三个角色账号（默认租户；前端登录不带 tenant）
    sys.path.insert(0, str(BACKEND))
    os.environ.update(env)
    from config import get_settings  # noqa: E402

    get_settings.cache_clear()
    import auth as auth_svc  # noqa: E402

    for u, p, r in (("alice", "alice-pass-123", "modeler"),
                    ("rita", "rita-pass-123", "reviewer"),
                    ("bob", "bob-pass-123", "viewer")):
        auth_svc.create_user(u, p, role=r, tenant_id="default")

    # 独立 uvicorn 进程（真实 HTTP + 真实 Cookie 会话）
    uv_err = BACKEND / "_uvicorn_e2e.err"
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

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                tce2e_05(browser)
                tce2e_06(browser)
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
            try:
                uv.kill()
            except Exception:
                pass
        shutil.rmtree(DATA, ignore_errors=True)
        uv_err.unlink(missing_ok=True)

    banner("汇总")
    for line in _LOGS:
        print(line)
    print(f"\nTOTAL PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
