# -*- coding: utf-8 -*-
"""验证 LLM API key 保存不丢失修复（文件存储场景 = 远程机/新装机场景）：
临时移开 .env（env 优先级会掩盖文件 key，模拟无 env 的环境）+ 备份 CFG/SECRET，
跑完完整还原。断言：
1) 保存 key 后 reveal 返回明文
2) 只改非 key 字段（api_key 缺省/null）→ key 保留
3) api_key="" 空串（旧前端行为）→ key 保留（回归点）
4) 轮换新 key → 生效
5) GET /settings 不泄露 key 明文
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

BASE = r"d:\self_coding\ontology_platform\backend"
API = "http://127.0.0.1:8000"
CFG = os.path.join(BASE, "data", "llm_config.json")
SECRET = os.path.join(BASE, "secrets", "LLM_API_KEY.json")
ENV = os.path.join(BASE, ".env")
BAK = {CFG: CFG + ".bak_llmtest", SECRET: SECRET + ".bak_llmtest", ENV: ENV + ".bak_llmtest"}
SRVLOG = r"d:\self_coding\ontology_platform\logs\_llmtest_server.log"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + str(detail)))


def req(method, path, body=None, timeout=15):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_data(path):
    j = req("GET", path)
    return j.get("data", j)


def put(body):
    return req("PUT", "/api/llm/settings", body)


def reveal():
    return get_data("/api/llm/settings/reveal").get("api_key", "")


# ---- 备份 ----
for src, bak in BAK.items():
    if os.path.exists(src):
        shutil.copy2(src, bak)
    elif os.path.exists(bak):
        os.remove(bak)
# 移开 .env（模拟无 env key 的环境，如远程机/新装机）
if os.path.exists(ENV) and os.path.exists(BAK[ENV]):
    os.remove(ENV)

proc = None
logf = None
try:
    logf = open(SRVLOG, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app",
                             "--host", "127.0.0.1", "--port", "8000"],
                            cwd=BASE, stdout=logf, stderr=subprocess.STDOUT)
    up = False
    for _ in range(60):
        time.sleep(0.5)
        if proc.poll() is not None:
            break
        try:
            get_data("/api/llm/settings")
            up = True
            break
        except Exception:
            continue
    check("服务启动（无 env key）", up)
    if up:
        print("baseline reveal: " + (reveal()[:6] + "..." if reveal() else "(none)"))
        assert not reveal(), "测试前提不成立：仍有 env key"

        put({"api_key": "sk-test-123", "model": "test-model"})
        check("保存 key 后 reveal=明文", reveal() == "sk-test-123", reveal())

        put({"base_url": "http://127.0.0.1:9/v1"})  # 新前端：未改 key → null（字段缺省）
        check("只改 base_url（api_key 缺省）key 保留", reveal() == "sk-test-123", reveal())

        put({"base_url": "http://127.0.0.1:9/v1", "api_key": ""})  # 旧前端空串行为
        check("api_key 空串 key 保留（回归点）", reveal() == "sk-test-123", reveal())

        put({"api_key": "sk-new-456"})
        check("轮换新 key 生效", reveal() == "sk-new-456", reveal())

        s = get_data("/api/llm/settings")
        leaked = "sk-new-456" in json.dumps(s)
        check("GET /settings 不泄露明文且 source=file",
              not leaked and s.get("api_key_configured") is True and s.get("api_key_source") == "file",
              s.get("api_key_source"))
finally:
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    if logf:
        logf.close()
    # ---- 还原 ----
    for src, bak in BAK.items():
        if os.path.exists(bak):
            shutil.move(bak, src)
        elif os.path.exists(src) and src != CFG:
            os.remove(src)  # 测试产物（原本不存在的 SECRET）
    if os.path.exists(SRVLOG):
        os.remove(SRVLOG)

fails = [n for n, ok in results if not ok]
print("=" * 40)
print("TOTAL %d, PASS %d, FAIL %d" % (len(results), len(results) - len(fails), len(fails)))
if fails and os.path.exists(SRVLOG):
    with open(SRVLOG, encoding="utf-8", errors="replace") as f:
        print(f.read()[-3000:])
sys.exit(1 if fails else 0)
