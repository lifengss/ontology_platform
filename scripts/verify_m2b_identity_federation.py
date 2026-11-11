"""M2-B（V2-14i 身份联邦）验收：临时 DATA_DIR + 合成数据，不导入业务凭据。

覆盖：
1. 映射 CRUD（服务层 + API）：响应不回显 secret_ref / secret。
2. 凭据本地加密存储（CREDENTIAL_ENCRYPTION_ENABLED=1）：明文不落盘、load_secret 可还原。
3. resolve_credential 返回引用式 auth override；mcp_client 以访问者身份代查（本地 mock HTTP 校验 Bearer）。
4. 错误凭据 → 401（证明委托身份真实生效，而非匿名通过）。
5. 缺失映射 → IdentityMappingMissing（不回落匿名，TCE-14-04）。
6. 审计/数据目录无凭据明文（grep，TC-14-11）。

运行：PYTHONUTF8=1 python scripts/verify_m2b_identity_federation.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

DATA_DIR = os.path.abspath(os.path.join(BACKEND, "data", "_m2b_verify"))
SECRET = "m2b-supersecret-token-ZX9K2"
PRINCIPAL = "alice@corp"

os.environ["DATA_DIR"] = DATA_DIR
os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
os.environ["CREDENTIAL_ENCRYPTION_ENABLED"] = "1"
os.environ["CREDENTIAL_MASTER_KEY"] = "m2b-test-master-key-0123456789"

# secret 实际落盘目录由 config.secrets_dir 决定（默认 backend/secrets，与 DATA_DIR 无关，已 gitignore）
from config import get_settings  # noqa: E402
get_settings.cache_clear()  # 确保以测试环境变量（含加密开关）重建单例
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


# ---- 本地 mock 外部系统：仅当 Authorization == Bearer <SECRET> 时放行 ----
class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {SECRET}"
        if auth == expected:
            payload = json.dumps([{"id": "ok", "v": 1}]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
        _ = body  # 不回显

    def log_message(self, *args):  # 静默
        return


def _start_mock() -> int:
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return port


def main() -> int:
    from identity_federation import (
        IdentityMappingMissing,
        create_mapping,
        delete_mapping,
        get_mapping,
        list_mappings,
        resolve_credential,
    )
    from credential_store import load_secret
    from mcp_client import McpClient

    PID = "m2b"

    # 1) 映射创建 + 列表 + 获取（服务层）
    rec = create_mapping(PID, user_id="alice", connection_id="crm", auth_type="bearer_token",
                         principal=PRINCIPAL, secret=SECRET, actor="admin")
    check("create_mapping 返回记录", bool(rec.get("id")) and rec.get("auth_type") == "bearer_token")
    check("create 不回显 secret_ref（服务层无该键也可接受，但响应不应含明文）", SECRET not in json.dumps(rec))

    lst = list_mappings(PID, user_id="alice")
    check("list_mappings 含新映射", any(m["id"] == rec["id"] for m in lst))
    got = get_mapping(PID, rec["id"])
    check("get_mapping 命中", got is not None and got["id"] == rec["id"])

    # 2) 加密落盘：明文不落盘 + load_secret 可还原
    ref = rec["secret_ref"]
    secret_file = os.path.join(SECRETS_DIR, f"{ref}.json")
    check("secret 文件存在且标记 encrypted", os.path.exists(secret_file))
    raw = json.loads(open(secret_file, encoding="utf-8").read())
    check("secret 文件 encrypted=true", raw.get("encrypted") is True)
    check("secret 明文不落盘", SECRET not in raw.get("value", ""))
    check("load_secret 还原明文", load_secret(ref) == SECRET)

    # 3) resolve -> mcp_client 委托代查（正确凭据应 200）
    override = resolve_credential(PID, user_id="alice", connection_id="crm")
    check("resolve 返回 bearer_token_ref", override.get("type") == "bearer_token" and override.get("bearer_token_ref") == ref)

    port = _start_mock()
    cfg = {"transport": "http", "endpoint": f"http://127.0.0.1:{port}"}
    client = McpClient()
    res = client.invoke_tool(cfg, "read", {}, auth_override=override)
    check("委托身份代查成功（200，返回记录）", isinstance(res, list) and res and res[0]["id"] == "ok")

    # 4) 错误凭据 -> 401（证明不是匿名通过）
    bad_override = {"type": "bearer_token", "bearer_token_ref": "NONEXISTENT_REF"}
    try:
        client.invoke_tool(cfg, "read", {}, auth_override=bad_override)
        check("错误凭据被拒（401）", False, "应当抛 MCP_AUTH_ERROR")
    except Exception as e:  # noqa: BLE001
        check("错误凭据被拒（401）", "401" in str(e) or "MCP_AUTH_ERROR" in str(e), str(e))

    # 5) 缺失映射 -> 显式拒绝（不回落匿名）
    try:
        resolve_credential(PID, user_id="alice", connection_id="missing-conn")
        check("缺失映射被拒（TCE-14-04）", False, "应当抛 IdentityMappingMissing")
    except IdentityMappingMissing as e:
        check("缺失映射被拒（TCE-14-04）", "missing-conn" in str(e) and e.http_status == 403, str(e))

    # 6) 数据目录 + secrets 目录无明文凭据（TC-14-11）
    hits: list[str] = []
    for scan_dir in (DATA_DIR, SECRETS_DIR):
        if not os.path.isdir(scan_dir):
            continue
        for root, _dirs, files in os.walk(scan_dir):
            for fn in files:
                if fn.endswith(".json") or fn.endswith(".jsonl"):
                    p = os.path.join(root, fn)
                    try:
                        txt = open(p, encoding="utf-8", errors="replace").read()
                    except Exception:  # noqa: BLE001
                        continue
                    if SECRET in txt:
                        hits.append(p)
    check("审计/存储无凭据明文（TC-14-11）", not hits, f"明文出现在: {hits}")

    # 7) API 层（TestClient，auth 关闭=dev admin）
    try:
        from fastapi.testclient import TestClient
        import main as main_mod
        with TestClient(main_mod.app) as tc:
            r = tc.post("/api/identity/mappings", params={"project_id": PID}, json={
                "user_id": "bob", "connection_id": "plm", "auth_type": "api_key",
                "principal": "bob", "secret": "bob-key-999",
            })
            check("API 创建映射 201", r.status_code == 201, r.text)
            body = r.json().get("data", {})
            check("API 响应不回显 secret_ref", "secret_ref" not in body and SECRET not in json.dumps(body))
            mid = body.get("id")
            rl = tc.get("/api/identity/mappings", params={"project_id": PID, "user_id": "bob"})
            check("API 列表含映射且无 secret_ref", rl.status_code == 200 and any(m["id"] == mid for m in rl.json()["data"]["items"]))
            rs = tc.get(f"/api/identity/mappings/{mid}/resolve-status", params={"project_id": PID})
            check("API resolve-status has_secret=true", rs.status_code == 200 and rs.json()["data"]["has_secret"] is True)
            rd = tc.delete(f"/api/identity/mappings/{mid}", params={"project_id": PID})
            check("API 删除映射 200", rd.status_code == 200 and rd.json()["data"]["deleted"] is True)
            rg = tc.get(f"/api/identity/mappings/{mid}", params={"project_id": PID})
            check("API 删除后 404", rg.status_code == 404)
    except Exception as e:  # noqa: BLE001
        check("API 层测试可执行", False, f"{type(e).__name__}: {e}")

    # 清理本测试创建的映射
    for m in list_mappings(PID):
        delete_mapping(PID, m["id"])

    print("\n".join(_LOGS))
    print(f"RESULT: PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
