"""TP-06-06 联合验收（源连接内容按访问者权限，V2-06 × M2-B 身份联邦）：临时 DATA_DIR + 本地 mock 源系统。

用例：访问者 A/B 源系统权限范围不同；以 A/B 身份分别读同一源连接内容；
预期：以访问者身份代查源系统，返回由业务端按该访问者权限决定（同一本体 A/B 范围不同）。

覆盖：
1. 同一连接、两名访问者各持独立凭据映射（bearer，权限范围不同）。
2. federated_read(alice) / federated_read(bob)：委托身份真实传递到源系统（mock 记录每次请求的 Authorization）。
3. A/B 返回范围不同（TP-06-06 核心断言：同连接同工具，宽/窄范围）。
4. 响应带来源标记（source_connection / resolved_as）。
5. 无映射用户 → IdentityMappingMissing 403，且源系统未收到任何匿名/兜底调用。
6. 审计 identity_delegated_read 落盘且无凭据明文；DATA_DIR + secrets 目录 grep 无明文。

边界（如实标注）：范围"由业务端按访问者权限决定"中的平台侧租户/schema 过滤随 M2-C（V2-06）与
M4（V2-02）落地；本验收证明的是平台以访问者身份代查、源端按身份返回不同范围的委托链路真实生效。

运行：PYTHONUTF8=1 python scripts/verify_tp0606_delegated_read.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

DATA_DIR = os.path.abspath(os.path.join(BACKEND, "data", "_tp0606_verify"))
shutil.rmtree(DATA_DIR, ignore_errors=True)  # 幂等：每次全新数据目录

os.environ["DATA_DIR"] = DATA_DIR
os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
os.environ["CREDENTIAL_ENCRYPTION_ENABLED"] = "1"
os.environ["CREDENTIAL_MASTER_KEY"] = "tp0606-test-master-key-0123456789"

from config import get_settings  # noqa: E402
get_settings.cache_clear()
SECRETS_DIR = get_settings().secrets_dir

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


# ---- mock 源系统：按 Bearer token 决定返回范围（alice=宽 8 行，bob=窄 3 行，其余 401）----
TOK = {"tok_alice_full": "alice", "tok_bob_scoped": "bob"}
SEEN: list[dict] = []  # 每次请求的 Authorization（线程安全 append）
_LOCK = threading.Lock()

_FULL_ROWS = [
    {"pn": f"PN-{i:03d}", "plant": "P1" if i % 2 else "P2", "cost": 100.0 + i, "secret_note": f"internal-{i}"}
    for i in range(8)
]
_SCOPED_ROWS = [
    {"pn": f"PN-{i:03d}", "plant": "P1" if i % 2 else "P2", "cost": 100.0 + i, "secret_note": "[masked]"}
    for i in range(3)
]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        _body = self.rfile.read(length) if length else b""
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else None
        with _LOCK:
            SEEN.append({"auth": auth, "path": self.path})
        if token in TOK:
            rows = _FULL_ROWS if token == "tok_alice_full" else _SCOPED_ROWS
            payload = json.dumps({"records": rows}).encode("utf-8")  # mcp_client 兼容 {"records":[...]} 信封
            self.send_response(200)
        else:
            payload = json.dumps({"error": "unauthorized"}).encode("utf-8")
            self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静默
        return


def _start_mock() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    from identity_federation import (
        IdentityMappingMissing,
        create_mapping,
        delete_mapping,
        federated_read,
        list_mappings,
    )

    PID = "tp0606"
    CONN_ID = "src_crm"

    srv = _start_mock()
    port = srv.server_address[1]
    connection = {
        "id": CONN_ID,
        "transport": "http",
        "endpoint": f"http://127.0.0.1:{port}",
        "auth": {"type": "bearer_token", "bearer_token_ref": "SHOULD_BE_OVERRIDDEN"},  # 连接自带 auth 必须被委托身份覆盖
    }

    # 1) 两名访问者对同一连接各建映射（服务层）
    rec_a = create_mapping(PID, user_id="alice", connection_id=CONN_ID, auth_type="bearer_token",
                           principal="alice@corp", secret="tok_alice_full", actor="admin")
    rec_b = create_mapping(PID, user_id="bob", connection_id=CONN_ID, auth_type="bearer_token",
                           principal="bob@corp", secret="tok_bob_scoped", actor="admin")
    check("A/B 映射创建", bool(rec_a.get("id")) and bool(rec_b.get("id")))
    check("映射不落凭据明文", "tok_alice_full" not in json.dumps(rec_a) and "tok_bob_scoped" not in json.dumps(rec_b))

    # 2) 以 A 身份联邦读（宽范围）
    res_a = federated_read(PID, user_id="alice", connection=connection, tool="query", params={"q": "all"})
    rows_a = res_a["result"]
    check("A 身份联邦读成功（8 行宽范围）", len(rows_a) == 8 and rows_a[0]["secret_note"].startswith("internal"), f"rows={len(rows_a)}")

    # 3) 以 B 身份联邦读（窄范围）
    res_b = federated_read(PID, user_id="bob", connection=connection, tool="query", params={"q": "all"})
    rows_b = res_b["result"]
    check("B 身份联邦读成功（3 行窄范围）", len(rows_b) == 3 and rows_b[0]["secret_note"] == "[masked]", f"rows={len(rows_b)}")

    # 4) TP-06-06 核心：同一连接同一工具，A/B 返回范围不同（宽范围含 internal 明细、窄范围已掩码）
    check("同一本体/连接 A/B 返回范围不同（TP-06-06）",
          len(rows_a) != len(rows_b)
          and rows_a[0]["secret_note"].startswith("internal")
          and rows_b[0]["secret_note"] == "[masked]")

    # 5) 来源标记
    check("响应带来源标记（source_connection/resolved_as）",
          res_a["source_connection"] == CONN_ID
          and res_a["resolved_as"] == {"user_id": "alice", "auth_type": "bearer_token"}
          and res_b["resolved_as"]["user_id"] == "bob")

    # 6) 委托身份真实传递：alice 的调用收到 tok_alice，bob 收到 tok_bob；连接自带 auth 未被使用
    a_calls = [s for s in SEEN if s["auth"] == "Bearer tok_alice_full"]
    b_calls = [s for s in SEEN if s["auth"] == "Bearer tok_bob_scoped"]
    bad_calls = [s for s in SEEN if s["auth"] == "Bearer SHOULD_BE_OVERRIDDEN"]
    check("委托身份真实传递（alice→tok_alice，bob→tok_bob）", len(a_calls) == 1 and len(b_calls) == 1,
          f"seen={SEEN}")
    check("连接自带 auth 被委托身份覆盖", not bad_calls)

    # 7) 缺失映射 → 显式拒绝，源系统无任何额外匿名调用
    calls_before = len(SEEN)
    try:
        federated_read(PID, user_id="carol", connection=connection, tool="query")
        check("缺失映射被拒（不回落匿名）", False, "应当抛 IdentityMappingMissing")
    except IdentityMappingMissing as e:
        check("缺失映射被拒（不回落匿名）", e.http_status == 403 and "carol" in str(e), str(e))
    check("拒绝时不产生源系统调用", len(SEEN) == calls_before)

    # 8) 审计 identity_delegated_read 落盘且无凭据明文（auth 审计走 JsonStore("auth_audit")）
    from auth import list_audit as _list_audit
    audit_rows = _list_audit(event="identity_delegated_read")
    check("审计含 identity_delegated_read（A/B 各一条）",
          len(audit_rows) == 2 and {r["actor"] for r in audit_rows} == {"alice", "bob"},
          f"rows={len(audit_rows)}")
    audit_txt = json.dumps(audit_rows, ensure_ascii=False)
    check("审计无凭据明文", "tok_alice_full" not in audit_txt and "tok_bob_scoped" not in audit_txt)

    # 9) 全目录 grep 无明文（DATA_DIR + secrets）
    hits: list[str] = []
    for scan_dir in (DATA_DIR, SECRETS_DIR):
        if not os.path.isdir(scan_dir):
            continue
        for root, _dirs, files in os.walk(scan_dir):
            for fn in files:
                if fn.endswith((".json", ".jsonl")):
                    p = os.path.join(root, fn)
                    txt = open(p, encoding="utf-8", errors="replace").read()
                    if "tok_alice_full" in txt or "tok_bob_scoped" in txt:
                        hits.append(p)
    check("存储无凭据明文（grep）", not hits, f"明文出现在: {hits}")

    # 清理
    for m in list_mappings(PID):
        delete_mapping(PID, m["id"])
    srv.shutdown()

    print("\n".join(_LOGS))
    print(f"RESULT: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
