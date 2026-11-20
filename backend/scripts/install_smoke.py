# -*- coding: utf-8 -*-
"""安装实例端到端冒烟（GAP-13/15/12/18 关键旅程，httpOnly cookie 会话）。
用法：python install_smoke.py <base_url> [admin_user] [admin_pass]
"""
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001"
USER = sys.argv[2] if len(sys.argv) > 2 else None
PASS = sys.argv[3] if len(sys.argv) > 3 else None

results = []


def check(name, ok, detail=""):
    results.append((name, "", ok, detail))


cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def req2(method, path, body=None, expect=200):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None)
    r.get_method = lambda: method
    r.add_header("Content-Type", "application/json")
    try:
        with opener.open(r, timeout=30) as resp:
            code, payload = resp.getcode(), json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = {}
    results.append((path, method, code == expect, code))
    return payload


try:
    with opener.open(BASE + "/health", timeout=15) as resp:
        check("/health 探活", resp.getcode() == 200)
except Exception as e:
    check("/health 探活", False, str(e)[:100])

req2("GET", "/mcp/status")

if USER and PASS:
    r = req2("POST", "/api/auth/login", {"username": USER, "password": PASS})
    check("登录（cookie 会话）", (r.get("data") or {}).get("authenticated") is True)

r = req2("POST", "/api/projects", {"name": "smoke-inst-%d" % int(time.time())}, expect=201)
pid = (r.get("data") or {}).get("id")
check("创建项目拿到 id", bool(pid), json.dumps(r, ensure_ascii=False)[:120])

if pid:
    r = req2("POST", "/api/projects/%s/seed-example" % pid, expect=201)
    d = r.get("data") or {}
    oid = (d.get("ontology") or {}).get("id") or d.get("ontology_id")
    check("GAP-13 一键示例返回本体 id", bool(oid), json.dumps(r, ensure_ascii=False)[:150])
    if oid:
        r = req2("GET", "/api/graph/%s/consistency?project_id=%s" % (oid, pid))
        check("GAP-15 一致性端点", "data" in r, json.dumps(r, ensure_ascii=False)[:120])
        req2("POST", "/api/ontologies/%s/publish?project_id=%s" % (oid, pid))
        # 焦点对象 id 从 schema 图动态取（不硬编码模板 id）
        g = req2("GET", "/api/graph/%s?mode=schema&project_id=%s" % (oid, pid))
        objs = [n["id"] for n in (g.get("data") or {}).get("nodes", []) if n.get("kind") == "object"]
        if objs:
            r = req2("GET", "/api/graph/%s/neighbors?project_id=%s&node_id=%s&mode=schema&depth=1&limit=10" % (oid, pid, objs[0]))
            check("GAP-12 邻居端点", bool((r.get("data") or {}).get("node")), json.dumps(r, ensure_ascii=False)[:150])
        else:
            check("GAP-12 邻居端点", False, "schema 图无对象节点")
        r = req2("POST", "/api/query/sparql?project_id=%s" % pid,
                 {"ontology_id": oid, "query": "SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 10"})
        check("SPARQL 查询", "data" in r, json.dumps(r, ensure_ascii=False)[:120])
    req2("DELETE", "/api/projects/%s" % pid)

failed = [x for x in results if not x[2]]
for name, method, ok, code in results:
    print("  [%s] %s %s %s" % ("PASS" if ok else "FAIL", method, name, "" if ok else code))
print("\n=== INSTALL SMOKE %s（%d 项，失败 %d） ===" % ("PASS" if not failed else "FAIL", len(results), len(failed)))
sys.exit(0 if not failed else 1)
