# -*- coding: utf-8 -*-
"""真实 LLM 端到端验证：from-text 走真实网关，断言 generation_mode/对象质量/实际发出的 max_tokens。
用独立 project_id，跑完删除草稿与项目目录。"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from fastapi.testclient import TestClient

from config import get_settings
from main import app

PID = f"test_ai_live_{int(time.time() * 1000)}"
TEXT = ("电商平台的客户可以下多个订单，每个订单包含若干商品，商品有 SKU 编码、名称和单价；"
        "订单有订单编号、下单时间、状态（待付款/已付款/已发货/已完成）和收货地址。"
        "客户有客户编号、姓名和注册时间。")

c = TestClient(app)
t0 = time.time()
r = c.post(f"/api/ontologies/from-text?project_id={PID}", json={"text": TEXT})
dt = time.time() - t0
print("status:", r.status_code, "| elapsed: %.1fs" % dt)
assert r.status_code == 201, r.text
draft = r.json()["data"]
mode = draft.get("generation_mode")
print("generation_mode:", mode)
print("name:", draft.get("name"))
for o in draft.get("objects", []):
    print("  obj:", o["id"], o["name"], "| stable_key:", o.get("stable_key"),
          "| attrs:", len(o.get("attributes", [])), "| rels:", len(o.get("relations", [])))

assert mode == "llm", "期望 LLM 真实产出，实际为 %s（检查 LLM 设置/网关）" % mode
assert draft["name"], "name 缺失"
objs = draft["objects"]
assert len(objs) >= 2
for o in objs:
    assert o.get("business_definition"), "缺 business_definition: " + o["id"]
    sk = o.get("stable_key")
    attr_ids = {a["id"] for a in o.get("attributes", [])}
    assert sk in attr_ids, "stable_key 悬空: %s -> %s" % (o["id"], sk)

# 核对 LLM 日志：实际发出的 max_tokens 应为 LLM 设置值（65535），而不是 8192
from ai_helper.config import get_llm_config
want = get_llm_config().max_tokens
log_file = os.path.join(get_settings().log_dir, "llm", "llm-%s.log" % time.strftime("%Y-%m-%d", time.gmtime()))
sent = None
if os.path.exists(log_file):
    with open(log_file, encoding="utf-8", errors="replace") as f:
        for line in f.readlines()[-20:]:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("msg") == "llm call ok":
                body = rec.get("request") or {}
                if isinstance(body, dict) and body.get("model"):
                    sent = body.get("max_tokens")
print("LLM log sent max_tokens:", sent, "| config max_tokens:", want)
assert sent == want, "实际发出的 max_tokens(%s) != 配置值(%s)——硬编码未去掉？" % (sent, want)

# 清理：删除草稿 + 项目目录
d = c.get(f"/api/ontologies?project_id={PID}")
ids = [o["id"] for o in d.json()["data"]["items"]]
for i in ids:
    rd = c.delete(f"/api/ontologies/{i}", params={"project_id": PID})
    print("delete draft:", i, rd.status_code)
shutil.rmtree(os.path.join(get_settings().data_dir, "projects", PID), ignore_errors=True)
print("CLEANUP DONE | elapsed %.1fs" % dt)
