"""GAP-23 前端/UI 持续可复跑冒烟：静态完整性断言 ＋ 核心用户旅程 API 契约冒烟。

静态断言（无需服务）：
  1. index.html 中 onclick 引用的全局函数在 app.js 均有定义（防"按钮点了没反应"回归）；
  2. app.js 引用的 DOM id 在 index.html 静态结构或 app.js 动态模板中存在；
  3. app.js 通过 node --check 语法校验；index.html 含 app.js 缓存参数（?v=）；
  4. 六大导航 tab（home/modeling/ingest/reasoning? 视实际）在 index.html 有视图容器。

旅程冒烟（进程内 TestClient，auth 关闭，隔离临时 DATA_DIR）：
  创建项目 → 建模（本体+约束）→ 发布 → CSV 上传 → 候选预览/审批 → 实例查询 →
  SPARQL 查询 → 知识图谱 → 按需邻居（GAP-12）→ 推理 → 能力批量（HF-11）→ MCP 就绪（GAP-18）。
  每步断言 success 信封；任一步失败即 FAIL 并输出步骤名。

用法：python scripts/verify_ui_smoke.py [--static-only]（退出码 0=PASS）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "frontend")
sys.path.insert(0, BACKEND_DIR)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def static_checks() -> None:
    html = open(os.path.join(FRONTEND_DIR, "index.html"), encoding="utf-8").read()
    js_path = os.path.join(FRONTEND_DIR, "app.js")
    js = open(js_path, encoding="utf-8").read()

    # 1. onclick 引用的函数必须已定义
    onclick_fns = set(re.findall(r'onclick="([A-Za-z_][A-Za-z0-9_]*)\s*\(', html))
    defined = set(re.findall(r'\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(', js))
    defined |= set(re.findall(r'\b(?:async\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(', js))
    missing = sorted(onclick_fns - defined)
    check("onclick 函数全部已定义", not missing, f"缺失: {missing[:8]}" if missing else f"{len(onclick_fns)} 个")

    # 2. $('id') 引用的 DOM id 存在（静态 HTML 或 app.js 动态模板）
    ref_ids = set(re.findall(r"\$\('([A-Za-z_][A-Za-z0-9_-]*)'\)", js))
    html_ids = set(re.findall(r'id="([A-Za-z_][A-Za-z0-9_-]*)"', html)) | set(re.findall(r"id='([A-Za-z_][A-Za-z0-9_-]*)'", html))
    js_ids = (set(re.findall(r'id="([A-Za-z_][A-Za-z0-9_-]*)"', js))
              | set(re.findall(r"id='([A-Za-z_][A-Za-z0-9_-]*)'", js))
              | set(re.findall(r"\binp\('([A-Za-z_][A-Za-z0-9_-]*)'", js))  # inp('id',...) 动态输入框
              | set(re.findall(r"\.id\s*=\s*['\"]([A-Za-z_][A-Za-z0-9_-]*)['\"]", js)))  # el.id='...' 赋值
    missing_ids = sorted(ref_ids - html_ids - js_ids)
    check("DOM id 引用完整", not missing_ids, f"缺失: {missing_ids[:8]}" if missing_ids else f"{len(ref_ids)} 个")

    # 3. app.js 语法校验（node --check）＋缓存参数
    r = subprocess.run(["node", "--check", js_path], capture_output=True, text=True)
    check("app.js 语法（node --check）", r.returncode == 0, (r.stderr or "")[:200])
    check("index.html 缓存参数", bool(re.search(r'app\.js\?v=', html)), "app.js?v=")

    # 4. 关键视图容器/导航存在（导航 tab ＋ 图谱容器 ＋ 文档上传）
    for key in ('data-tab="home"', 'data-tab="modeling"', 'data-tab="ingest"', 'data-tab="graph"',
                'data-tab="query"', 'data-tab="inference"', 'data-tab="action"',
                'id="graphContainer"', 'id="docFile"'):
        check(f"UI 结构 {key.split(chr(34))[1]}", key in html, "")

    # 5. 关键全局函数抽查（各里程碑代表性入口）
    for fn in ("loadOntologies", "loadGraph", "toggleGraphKind", "graphExpandNeighbors",
              "showOnboardingWizard", "createSeedProject", "seedProjectExample",
              "applyPermissionMatrix", "renderGraphFilterBar", "graphResetExpand"):
        check(f"入口函数 {fn}", fn in defined, "")


def journey_checks() -> None:
    data_dir = tempfile.mkdtemp(prefix="ont_smoke_")
    os.environ["DATA_DIR"] = data_dir
    os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
    os.environ["ONTOLOGY_RDF_CONSISTENCY_INTERVAL_SECONDS"] = "0"
    try:
        from fastapi.testclient import TestClient

        import main as m

        c = TestClient(m.app)
        step = "1 创建项目"
        r = c.post("/api/projects", json={"name": f"smoke-{uuid.uuid4().hex[:6]}", "description": ""})
        check(step, r.status_code == 201 and r.json()["success"], r.text[:120])
        pid = r.json()["data"]["id"]

        step = "2 建模（本体＋pattern 约束）"
        body = {"name": "smoke 本体", "modeling_type": "manual", "version": "1",
                "objects": [{"id": "obj_item", "name": "商品", "stable_key": "attr_code",
                             "attributes": [{"id": "attr_code", "name": "编码", "data_type": "string",
                                             "required": True, "constraints": {"pattern": "^S[0-9]{3}$"}},
                                            {"id": "attr_price", "name": "价格", "data_type": "float",
                                             "required": False, "constraints": {}}],
                             "relations": []}],
                "constraints": [], "functions": []}
        r = c.post("/api/ontologies", params={"project_id": pid}, json=body)
        check(step, r.status_code == 201 and r.json()["success"], r.text[:120])
        oid = r.json()["data"]["id"]

        step = "3 发布本体"
        r = c.post(f"/api/ontologies/{oid}/publish", params={"project_id": pid})
        check(step, r.status_code == 200 and r.json()["data"].get("status") == "active", r.text[:120])

        step = "4 CSV 上传（候选预览）"
        csv = io.BytesIO("编码,价格\nS001,10.5\nS002,20\n".encode("utf-8"))
        r = c.post("/api/ingest/upload", params={"project_id": pid, "ontology_id": oid, "object_type": "obj_item"},
                   files={"file": ("s.csv", csv, "text/csv")})
        ok = r.status_code == 201 and r.json()["data"]["candidates"]
        check(step, ok, r.text[:120])
        cid = r.json()["data"]["candidates"][0]["id"]

        step = "5 候选审批入库"
        r = c.post(f"/api/ingest/candidates/{cid}/approve", params={"project_id": pid})
        check(step, r.status_code == 200 and r.json()["data"]["imported"] == 2, r.text[:120])

        step = "6 实例查询"
        r = c.get("/api/ingest/instances", params={"project_id": pid, "object_type": "obj_item", "pageSize": 10})
        check(step, r.status_code == 200 and r.json()["data"]["total"] == 2, r.text[:120])

        step = "7 SPARQL 查询（本地快路径）"
        r = c.post("/api/query/sparql", params={"project_id": pid},
                   json={"ontology_id": oid, "query": "SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 50"})
        check(step, r.status_code == 200 and r.json()["success"], r.text[:120])

        step = "8 知识图谱（GAP-12 大图口径）"
        r = c.get(f"/api/graph/{oid}", params={"project_id": pid, "mode": "knowledge", "max_instances": 200})
        check(step, r.status_code == 200 and r.json()["data"]["nodes"], r.text[:120])

        step = "9 按需邻居（GAP-12）"
        r = c.get(f"/api/graph/{oid}/neighbors",
                  params={"project_id": pid, "node_id": "obj_item", "mode": "knowledge", "depth": 1, "limit": 50})
        check(step, r.status_code == 200 and r.json()["data"]["node"]["id"] == "obj_item", r.text[:120])

        step = "10 推理（本地引擎）"
        r = c.get(f"/api/ontologies/{oid}/inference", params={"project_id": pid, "engine": "local"})
        check(step, r.status_code == 200 and r.json()["success"], r.text[:120])

        step = "11 能力创建＋批量执行（HF-11 分块）"
        from core.instance_store import get_instance_store
        from core.storage import JsonStore
        cap = {"id": "smoke-cap", "name": "smoke_price", "object_type": "obj_item",
               "expression": "this.attr_price * 2", "output_attribute": "attr_price",
               "kind": "derive", "implementation": "expression", "version": "1",
               "status": "active", "channel": "api", "project_id": pid}
        JsonStore("capabilities", pid).insert(cap)
        eng = __import__("capabilities").CapabilityService(pid)
        res = eng.execute_batch("smoke-cap", channel="api", trigger_source="smoke")
        check(step, res.get("total") == 2 and res.get("success") == 2, json.dumps(res, ensure_ascii=False)[:120])

        step = "12 MCP 服务状态（GAP-18 就绪口径）"
        r = c.get("/mcp/status")
        check(step, r.status_code == 200, r.text[:120])
    finally:
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--static-only", action="store_true", help="只跑静态断言（不启服务）")
    args = ap.parse_args()

    static_checks()
    if not args.static_only:
        journey_checks()

    failed = [(n, d) for n, ok, d in results if not ok]
    for n, ok, d in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {n}" + (f" — {d}" if (d and not ok) else ""))
    print(f"\n=== UI 冒烟 {'PASS' if not failed else 'FAIL'}（{len(results)} 项，失败 {len(failed)}） ===")
    if failed:
        for n, d in failed:
            print(f"  失败: {n} {d}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
