# -*- coding: utf-8 -*-
"""E2E：MCP 接入层修复全量验收（docs/平台修改建议-MCP接入层修复.md 各验收项）。

运行：python _e2e_mcp.py
阶段1：无token/未发布 → token 生成 → 发布 → 双传输 → 工具验收（分页/投影/错误码/405）
阶段2：重启后端 → 发布态与 token 持久化 → 吊销后 401 → 重新生成
"""
from __future__ import annotations

import http.client as http_client
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
HOST, PORT = "127.0.0.1", 8000
BASE = f"http://{HOST}:{PORT}"
PID = "5cd5ccc06b604ed7829f99983f52e963"
OID = "1f2ea950c2914a69a994da880e676531"
FAKE_PID = "probe_fake_project_000"
FAKE_OID = "0" * 32
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "e2e", "version": "1"}}}

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def http(method: str, path: str, body=None, headers=None, timeout=30):
    """返回 (status, 小写headers, raw_bytes)。"""
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def jbody(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def parse_sse_events(raw) -> list:
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:].strip()))
            except Exception:
                pass
    return out


def rpc(path: str, token, session=None, body=None):
    """JSON-RPC over Streamable HTTP；返回 (status, headers, 解析对象, raw)。"""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
    status, hs, raw = http("POST", path, body=body, headers=headers)
    ct = hs.get("content-type") or ""
    if "event-stream" in ct:
        evs = parse_sse_events(raw)
        obj = evs[0] if evs else None
    else:
        obj = jbody(raw)
    return status, hs, obj, raw


def tool_call(token, session, name, args, rpc_id):
    return rpc(f"/mcp/{PID}", token, session=session,
               body={"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                     "params": {"name": name, "arguments": args}})


def tool_payload(obj):
    """返回 (解析后的json或None, 文本)。isError 时 json 为 None、文本为错误内容。"""
    res = (obj or {}).get("result") or {}
    text = "".join(c.get("text", "") for c in res.get("content") or [])
    if res.get("isError"):
        return None, text
    try:
        return json.loads(text), text
    except Exception:
        return None, text[:200]


class SSEClient:
    """SSE 兼容路径：GET 长连接 + 增量读取。"""

    def __init__(self, path: str, token: str | None = None):
        self.conn = http_client.HTTPConnection(HOST, PORT, timeout=20)
        headers = {"Accept": "text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.conn.request("GET", path, headers=headers)
        self.resp = self.conn.getresponse()
        self._buf = ""

    @property
    def status(self):
        return self.resp.status

    @property
    def content_type(self):
        return self.resp.getheader("Content-Type") or ""

    def read_until(self, needle: str, timeout: float = 20.0) -> str:
        deadline = time.time() + timeout
        while needle not in self._buf:
            if time.time() > deadline:
                break
            try:
                chunk = self.resp.read(1024)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            self._buf += chunk.decode("utf-8", "replace")
        return self._buf

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def phase1() -> str:
    print("== 阶段1：前置状态 / token / 发布 / 双传输 / 工具验收 ==")
    # --- 重置到修复前状态（幂等重跑）：吊销 token + 整体下线 ---
    http("POST", f"/api/mcp/server/token/revoke?project_id={PID}")
    http("POST", f"/api/mcp/server/unpublish?project_id={PID}")
    # --- P0-1：status 声明双传输与协议版本 ---
    st, _, raw = http("GET", "/mcp/status")
    data = (jbody(raw) or {}).get("data") or {}
    check("status 声明双传输", set(data.get("transports") or []) == {"streamable-http", "sse"},
          str(data.get("transports")))
    check("status 声明 3 个协议版本",
          set(data.get("protocol_versions") or []) == {"2025-06-18", "2025-03-26", "2024-11-05"},
          str(data.get("protocol_versions")))

    # --- P0-2：无 token / 错 token → 401 ---
    st, _, raw = http("GET", f"/mcp/{PID}", headers={"Accept": "text/event-stream"})
    check("SSE 无 token 401", st == 401 and (jbody(raw) or {}).get("error") == "UNAUTHORIZED",
          f"status={st} body={jbody(raw)}")
    st, _, raw = http("POST", f"/mcp/{PID}", body=INIT,
                      headers={"Content-Type": "application/json",
                               "Accept": "application/json, text/event-stream"})
    check("Streamable 无 token 401", st == 401, f"status={st}")
    st, _, raw = http("GET", f"/mcp/{PID}?token=wrong-token", headers={"Accept": "text/event-stream"})
    check("SSE 错误 token 401", st == 401, f"status={st}")
    st, _, raw = http("GET", f"/mcp/{FAKE_PID}", headers={"Accept": "text/event-stream"})
    check("不存在项目 401（鉴权先于发布）", st == 401, f"status={st}")

    # --- P0-2：status 不含未发布/不存在项目 ---
    st, _, raw = http("GET", "/mcp/status")
    pids = [p.get("project_id") for p in ((jbody(raw) or {}).get("data") or {}).get("published_projects") or []]
    check("status 不含未发布/不存在项目", PID not in pids and FAKE_PID not in pids, str(pids))

    # --- token 生成（201）与查询 ---
    st, _, raw = http("POST", f"/api/mcp/server/token?project_id={PID}")
    token = ((jbody(raw) or {}).get("data") or {}).get("token") or ""
    check("POST /api/mcp/server/token 201+token", st == 201 and len(token) >= 32,
          f"status={st} len={len(token)}")
    st, _, raw = http("GET", f"/api/mcp/server/token?project_id={PID}")
    ts = (jbody(raw) or {}).get("data") or {}
    check("GET /api/mcp/server_token 一致", ts.get("configured") is True and ts.get("token") == token)

    # --- P0-2：已带 token 但未发布 → 404 NOT_PUBLISHED ---
    st, _, raw = http("GET", f"/mcp/{PID}",
                      headers={"Accept": "text/event-stream", "Authorization": f"Bearer {token}"})
    check("SSE 未发布 404 NOT_PUBLISHED", st == 404 and (jbody(raw) or {}).get("error") == "NOT_PUBLISHED",
          f"status={st} body={jbody(raw)}")
    st, _, raw = http("POST", f"/mcp/{PID}", body=INIT,
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                               "Accept": "application/json, text/event-stream"})
    check("Streamable 未发布 404 NOT_PUBLISHED", st == 404 and (jbody(raw) or {}).get("error") == "NOT_PUBLISHED")

    # --- P2-7：无 session 头 GET（非 SSE）→ 405 ---
    st, _, raw = http("GET", f"/mcp/{PID}", headers={"Accept": "application/json",
                                                     "Authorization": f"Bearer {token}"})
    check("GET 无 session 头非 SSE 405", st == 405, f"status={st} body={jbody(raw)}")

    # --- 发布 ---
    st, _, raw = http("POST", f"/api/mcp/server/publish?project_id={PID}&ontology_id={OID}")
    check("publish 成功", st == 200 and (jbody(raw) or {}).get("data", {}).get("published") is True,
          f"status={st} body={jbody(raw)}")
    st, _, raw = http("GET", f"/mcp/status?project_id={PID}")
    data = (jbody(raw) or {}).get("data") or {}
    check("单项目 status 发布清单正确", data.get("published") is True
          and any(o.get("ontology_id") == OID for o in data.get("published_ontologies") or []),
          json.dumps(data.get("published_ontologies"), ensure_ascii=False))

    # --- P0-1：Streamable initialize（2025-06-18） ---
    st, hs, obj, _ = rpc(f"/mcp/{PID}", token, body=INIT)
    tries = 0
    while st == 503 and tries < 10:
        time.sleep(1)
        st, hs, obj, _ = rpc(f"/mcp/{PID}", token, body=INIT)
        tries += 1
    session = hs.get("mcp-session-id") or ""
    result = (obj or {}).get("result") or {}
    check("Streamable initialize 200+Mcp-Session-Id", st == 200 and bool(session),
          f"status={st} session={session[:12]}")
    check("协议版本协商 2025-06-18", result.get("protocolVersion") == "2025-06-18",
          str(result.get("protocolVersion")))
    check("serverInfo.name 正确", (result.get("serverInfo") or {}).get("name") == f"EnterpriseOntologyMCP_{PID}")

    # --- tools/list ---
    st, _, obj, _ = rpc(f"/mcp/{PID}", token, session=session,
                        body={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = ((obj or {}).get("result") or {}).get("tools") or []
    names = {t.get("name") for t in tools}
    check("tools/list 4 标准工具", st == 200 and names == {"ontology_query", "inference_trace",
                                                           "instance_get", "action_invoke"},
          str(sorted(names)))
    ig = next((t for t in tools if t.get("name") == "instance_get"), {})
    props = (ig.get("inputSchema") or {}).get("properties") or {}
    check("instance_get schema 含 limit/offset/fields", {"limit", "offset", "fields"} <= set(props),
          str(sorted(props)))

    # --- P0-3/P1-5：ontology_query 正常 + 错误语义 ---
    st, _, obj, _ = tool_call(token, session, "ontology_query", {"ontology_id": OID}, 3)
    payload, text = tool_payload(obj)
    check("ontology_query 已发布本体可读", st == 200 and payload is not None
          and isinstance(payload.get("objects"), list) and len(payload["objects"]) >= 1,
          f"objects={len((payload or {}).get('objects') or [])}")
    st, _, obj, _ = tool_call(token, session, "ontology_query", {"ontology_id": FAKE_OID}, 4)
    payload, text = tool_payload(obj)
    check("ontology_query 未发布 isError NOT_PUBLISHED", payload is None and "NOT_PUBLISHED:" in text,
          text[:80])
    st, _, obj, _ = tool_call(token, session, "action_invoke", {"action_id": "probe-nonexistent"}, 5)
    payload, text = tool_payload(obj)
    check("action_invoke 不存在 isError NOT_FOUND", payload is None and "NOT_FOUND:" in text,
          text[:80])

    # --- P1-4：instance_get 分页 / 投影 ---
    st, _, obj, _ = tool_call(token, session, "instance_get", {"ontology_id": OID}, 6)
    p, text = tool_payload(obj)
    check("instance_get 默认 limit=50 且 total/has_more 正确",
          st == 200 and p is not None and p.get("limit") == 50 and p.get("offset") == 0
          and p.get("count") == min(50, p.get("total", -1))
          and p.get("has_more") == (p.get("total", 0) > 50) and "hint" in p,
          f"total={p and p.get('total')} count={p and p.get('count')} has_more={p and p.get('has_more')}")
    total = (p or {}).get("total", 0)
    st, _, obj, _ = tool_call(token, session, "instance_get", {"ontology_id": OID, "limit": 500}, 7)
    p2, _ = tool_payload(obj)
    check("instance_get limit=500 截断到 200", p2 is not None and p2.get("limit") == 200
          and p2.get("count") == min(200, total) and p2.get("has_more") == (total > 200),
          f"limit={p2 and p2.get('limit')} count={p2 and p2.get('count')}")
    st, _, obj, _ = tool_call(token, session, "instance_get", {"ontology_id": OID, "limit": 10, "offset": 5}, 8)
    p3, _ = tool_payload(obj)
    check("instance_get offset 翻页", p3 is not None and p3.get("offset") == 5 and p3.get("count") == 10,
          f"offset={p3 and p3.get('offset')} count={p3 and p3.get('count')}")
    sample = ((p or {}).get("instances") or [{}])[0].get("instance") or {}
    fkeys = [k for k in sample.keys() if k != "id"][:2]
    st, _, obj, _ = tool_call(token, session, "instance_get",
                              {"ontology_id": OID, "limit": 3, "fields": fkeys}, 9)
    p4, _ = tool_payload(obj)
    ok_proj = p4 is not None and all(set((r.get("instance") or {}).keys()) <= set(fkeys)
                                     for r in p4.get("instances") or [])
    check("instance_get fields 投影", ok_proj, f"fields={fkeys}")

    # --- P0-1：SSE 兼容路径（旧客户端，?token=） ---
    sse = SSEClient(f"/mcp/{PID}?token={token}")
    check("SSE 兼容 GET 200 event-stream", sse.status == 200 and "text/event-stream" in sse.content_type,
          f"status={sse.status} ct={sse.content_type}")
    buf = sse.read_until("event: endpoint")
    m = re.search(r"data: (\S+)", buf)
    msg_path = m.group(1) if m else ""
    check("SSE 返回 endpoint", bool(msg_path), msg_path)
    if msg_path:
        sep = "&" if "?" in msg_path else "?"
        init_sse = dict(INIT)
        init_sse["params"] = {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "e2e-sse", "version": "1"}}
        st, _, raw = http("POST", f"{msg_path}{sep}token={token}", body=init_sse)
        check("SSE initialize 202", st == 202, f"status={st} body={raw[:80]!r}")
        buf2 = sse.read_until("protocolVersion")
        evs = parse_sse_events(buf2)
        pv = next(((e.get("result") or {}).get("protocolVersion") for e in evs
                   if (e.get("result") or {}).get("protocolVersion")), None)
        check("SSE 协议版本 2024-11-05", pv == "2024-11-05", str(pv))
    sse.close()

    # --- P0-1：GET 带 session 头 → 会话流；DELETE 终止 ---
    conn = http_client.HTTPConnection(HOST, PORT, timeout=8)
    conn.request("GET", f"/mcp/{PID}", headers={"Accept": "text/event-stream",
                                                "Mcp-Session-Id": session,
                                                "Authorization": f"Bearer {token}"})
    resp = conn.getresponse()
    check("GET 带 session 头 200 会话流", resp.status == 200
          and "text/event-stream" in (resp.getheader("Content-Type") or ""),
          f"status={resp.status} ct={resp.getheader('Content-Type')}")
    conn.close()
    st, _, raw = http("DELETE", f"/mcp/{PID}",
                      headers={"Authorization": f"Bearer {token}", "Mcp-Session-Id": session,
                               "Accept": "application/json"})
    check("DELETE 终止会话", st in (200, 204), f"status={st} body={raw[:120]!r}")
    st, _, obj, _ = rpc(f"/mcp/{PID}", token, session=session,
                        body={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}})
    check("终止后旧 session 404", st == 404, f"status={st}")
    return token


def restart_backend() -> bool:
    print("== 重启后端（验证发布态持久化 P0-3） ==")
    try:
        p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "restart_backend.py"), "8000"],
                           cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=400)
        tail = (p.stdout or "")[-500:]
        print(tail)
        if p.returncode != 0:
            print((p.stderr or "")[-500:])
        return p.returncode == 0
    except Exception as e:  # noqa: BLE001
        print(f"restart failed: {e}")
        return False


def phase2(token: str) -> None:
    print("== 阶段2：重启后持久化 / 吊销 / 重新生成 ==")
    check("后端重启成功", restart_backend())
    st, _, raw = http("GET", f"/mcp/status?project_id={PID}")
    data = (jbody(raw) or {}).get("data") or {}
    check("发布态重启不丢（磁盘恢复）", data.get("published") is True
          and any(o.get("ontology_id") == OID for o in data.get("published_ontologies") or []),
          json.dumps(data.get("published_ontologies"), ensure_ascii=False))
    check("token 重启不丢", (data.get("token") or {}).get("configured") is True, str(data.get("token")))

    st, hs, obj, _ = rpc(f"/mcp/{PID}", token, body=INIT)
    tries = 0
    while st == 503 and tries < 10:
        time.sleep(1)
        st, hs, obj, _ = rpc(f"/mcp/{PID}", token, body=INIT)
        tries += 1
    check("旧 token 重启后可重连 200", st == 200 and bool(hs.get("mcp-session-id")), f"status={st}")

    st, _, raw = http("POST", f"/api/mcp/server/token/revoke?project_id={PID}")
    check("token 吊销", st == 200 and (jbody(raw) or {}).get("data", {}).get("revoked") is True,
          f"status={st} body={jbody(raw)}")
    st, _, obj, raw = rpc(f"/mcp/{PID}", token, body=INIT)
    check("吊销后旧 token 401", st == 401, f"status={st} body={jbody(raw)}")
    st, _, raw = http("GET", f"/api/mcp/server/token?project_id={PID}")
    check("吊销后 configured=false", (jbody(raw) or {}).get("data", {}).get("configured") is False)

    st, _, raw = http("POST", f"/api/mcp/server/token?project_id={PID}")
    token2 = ((jbody(raw) or {}).get("data") or {}).get("token") or ""
    st2, hs2, _, _ = rpc(f"/mcp/{PID}", token2, body=INIT)
    tries = 0
    while st2 == 503 and tries < 10:
        time.sleep(1)
        st2, hs2, _, _ = rpc(f"/mcp/{PID}", token2, body=INIT)
        tries += 1
    check("重新生成 token 可连 200", st == 201 and st2 == 200 and bool(hs2.get("mcp-session-id")),
          f"gen={st} connect={st2}")
    print(f"\n当前有效 token（已存 mcp_tokens.json，MCP 页可查）：{token2[:12]}…（共 {len(token2)} 字符）")


def main() -> int:
    t0 = time.time()
    token = phase1()
    phase2(token)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n==== E2E 结果：{passed}/{len(RESULTS)} PASS"
          + (f"，耗时 {int(time.time() - t0)}s" if not failed else "") + " ====")
    if failed:
        print("FAIL 项：")
        for n in failed:
            detail = next(d for name, _, d in RESULTS if name == n)
            print(f"  - {n} | {detail}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
