"""干净重启后端 FastAPI（按 start-all.ps1 同款方式：python backend/main.py）。

用法：python scripts/restart_backend.py [port]
步骤：kill 现有 main.py/uvicorn 进程 → 等端口释放 → 以 start-all.ps1 相同环境变量 detached 启动 → 健康检查 → 探测新端点。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import psutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
PORT = sys.argv[1] if len(sys.argv) > 1 else "8000"
LOG = os.path.join(BACKEND, "logs", "backend-manual.log")


def is_backend_proc(p: psutil.Process) -> bool:
    try:
        cmd = " ".join(p.cmdline() or [])
    except Exception:
        return False
    return ("main.py" in cmd or "uvicorn" in cmd) and "restart_backend" not in cmd


def main() -> int:
    me = psutil.Process()
    killed = []
    parent_pid = me.parent().pid if me.parent() else None
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if p.pid in (me.pid, parent_pid):
                continue
            if is_backend_proc(p):
                p.kill()
                killed.append(p.pid)
        except psutil.NoSuchProcess:
            pass
    print(f"killed: {killed}")

    deadline = time.time() + 15
    while time.time() < deadline:
        alive = [p.pid for p in psutil.process_iter(["pid", "cmdline"]) if p.pid not in (me.pid,) and is_backend_proc(p)]
        if not alive:
            break
        time.sleep(0.5)
    else:
        print("WARN: 仍有后端进程存活，继续尝试启动")

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    env = os.environ.copy()
    env.update({
        "JENA_BASE_URL": "http://localhost:3030",
        "JENA_DATASET": "ontology",
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "neo4jsecret",
        "POSTGRES_DSN": "postgresql://ont:ontsecret@localhost:5432/ont",
        "PYTHONUTF8": "1",
    })
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with open(LOG, "ab") as lf:
        subprocess.Popen([sys.executable, "main.py"], cwd=BACKEND, env=env,
                         stdout=lf, stderr=lf, creationflags=flags, close_fds=True)
    print(f"started backend, log={LOG}")

    # 注意：/health 每次都会同步探测 Neo4j/PG（约 8-9s），探测预算须留足；
    # 先探 /docs（无 DB 依赖，秒回）确认进程在监听，再等一次 /health 确认依赖状态。
    docs_url = f"http://127.0.0.1:{PORT}/docs"
    deadline = time.time() + 180
    attempts = 0
    listening = False
    while time.time() < deadline:
        attempts += 1
        try:
            with urllib.request.urlopen(docs_url, timeout=5) as r:
                if r.status < 500:
                    listening = True
                    print(f"server listening after {attempts} attempts")
                    break
        except Exception as e:
            print(f"attempt {attempts}: not listening yet ({type(e).__name__}: {e})")
            time.sleep(3)
    if not listening:
        print("FAIL: backend not listening in 180s")
        return 1

    url = f"http://127.0.0.1:{PORT}/health"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                body = r.read(300).decode("utf-8", "replace")
                print("health:", r.status, body[:120])
                break
        except Exception as e:
            print(f"health probe failed: {type(e).__name__}: {e}")
            time.sleep(3)
    else:
        print("FAIL: /health not ok in 60s（服务已在监听，可继续；检查依赖探测耗时）")
        return 1

    # 探测本次新增端点（路由存在性）
    probes = [
        ("POST", f"/api/ingest/candidates/probe_nonexist/revert?project_id=probe", b"{}"),
        ("POST", f"/api/ingest/candidates/batch-reject?project_id=probe", b"{}"),
        ("DELETE", f"/api/ingest/candidates/probe_nonexist?project_id=probe", None),
        ("POST", f"/api/inference/impact?project_id=probe", b'{"ontology_id":"x"}'),
        ("POST", f"/api/mcp/server/unpublish?project_id=probe", b"{}"),
    ]
    for method, path, body in probes:
        req = urllib.request.Request(f"http://localhost:{PORT}{path}", data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            code = f"ERR {e}"
        print(f"probe {method} {path.split('?')[0]} -> {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
