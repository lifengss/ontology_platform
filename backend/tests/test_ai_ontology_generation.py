"""AI 生成本体（from-text/from-doc）链路回归测试。

背景（2026-10-29 用户诊断）：
1. max_tokens 曾被硬编码 8192，思考型模型 reasoning 吃光 token → content 为空 → 静默走兜底；
2. LLM 失败与兜底结果对 UI 不可区分；
3. 默认 prompt 模板双花括号被原样送给模型。

本测试用本地 fake LLM 服务器（无网络依赖）覆盖：
- LLM 正常返回契约 JSON → generation_mode="llm"，对象为模型产出
- LLM 返回垃圾文本 → generation_mode="fallback"，确定性关键词抽取
- LLM 返回空 content（reasoning 耗尽）→ generation_mode="fallback"
- from-text 的 max_tokens 参数透传到请求体；缺省取 LLM 配置值
- generate_ontology_from_text 返回 (本体, 来源) 元组；fallback=False 时异常上抛
- 默认模板渲染为单花括号（非 {{ }}），且含 {text} 注入
"""
from __future__ import annotations

import http.server
import json
import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient

from ai_helper import generate_ontology_from_text
from ai_helper.prompts import DEFAULT_ONTOLOGY_GENERATION_PROMPT, render_ontology_generation
from ai_helper.config import get_llm_config
from main import app

VALID_ONTOLOGY = {
    "name": "AI 测试本体",
    "description": "测试用本体，覆盖两个对象及其关系。",
    "objects": [
        {
            "id": "obj_alpha",
            "name": "阿尔法对象",
            "business_definition": "测试中的第一个业务对象，用于验证生成链路。",
            "stable_key": "attr_alpha_code",
            "parent": None,
            "attributes": [
                {"id": "attr_alpha_code", "name": "编码", "data_type": "string", "required": True, "constraints": {}},
                {"id": "attr_alpha_name", "name": "名称", "data_type": "string", "required": True, "constraints": {}},
                {"id": "attr_alpha_status", "name": "状态", "data_type": "string", "required": False, "constraints": {}},
            ],
            "relations": [
                {"id": "rel_alpha_beta", "name": "关联贝塔", "target_object": "obj_beta", "cardinality": "one_to_many", "inverse_name": "被阿尔法关联"},
            ],
        },
        {
            "id": "obj_beta",
            "name": "贝塔对象",
            "business_definition": "测试中的第二个业务对象，被第一个对象引用。",
            "stable_key": "attr_beta_code",
            "parent": None,
            "attributes": [
                {"id": "attr_beta_code", "name": "编码", "data_type": "string", "required": True, "constraints": {}},
                {"id": "attr_beta_title", "name": "标题", "data_type": "string", "required": True, "constraints": {}},
                {"id": "attr_beta_amount", "name": "金额", "data_type": "float", "required": False, "constraints": {}},
            ],
            "relations": [
                {"id": "rel_beta_alpha", "name": "属于阿尔法", "target_object": "obj_alpha", "cardinality": "many_to_one", "inverse_name": "关联贝塔"},
            ],
        },
    ],
    "constraints": [],
}

# 兜底用例文本：命中 FALLBACK_TEMPLATES 关键词（客户/订单/商品）
FALLBACK_TEXT = "客户可以下多个订单，每个订单包含若干商品，商品有SKU和库存。"


class _FakeLLMHandler(http.server.BaseHTTPRequestHandler):
    """返回预设 content 的 /v1/chat/completions，并记录最后一个请求体。"""

    content: str = ""
    status: int = 200
    captured: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).captured.append(body)
        if type(self).status == 200:
            payload = {
                "id": "chatcmpl-fake",
                "model": body.get("model"),
                "choices": [{"message": {"role": "assistant", "content": type(self).content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            }
        else:
            payload = {"error": {"message": "fake upstream error", "code": type(self).status}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture()
def fake_llm():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLMHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeLLMHandler.content = ""
    _FakeLLMHandler.status = 200
    _FakeLLMHandler.captured = []
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture(scope="module")
def c():
    client = TestClient(app)
    project_id = f"test_ai_gen_{int(time.time() * 1000)}"
    client._test_project_id = project_id
    data_dir = None
    from config import get_settings
    data_dir = get_settings().data_dir
    yield client
    shutil.rmtree(
        f"{data_dir}/projects/{project_id}", ignore_errors=True
    )


def test_from_text_llm_success(c, fake_llm):
    _FakeLLMHandler.content = json.dumps(VALID_ONTOLOGY, ensure_ascii=False)
    _FakeLLMHandler.captured = []
    r = c.post(
        f"/api/ontologies/from-text?project_id={c._test_project_id}",
        json={"text": FALLBACK_TEXT, "llm_endpoint": fake_llm},
    )
    assert r.status_code == 201, r.text
    draft = r.json()["data"]
    assert draft["generation_mode"] == "llm"
    assert draft["draft_source"] == "text"
    names = {o["name"] for o in draft["objects"]}
    # 对象必须来自 LLM 产出（阿尔法/贝塔），而不是确定性兜底（客户/订单）
    assert "阿尔法对象" in names and "贝塔对象" in names
    assert "客户" not in names


def test_from_text_llm_garbage_falls_back(c, fake_llm):
    _FakeLLMHandler.content = "抱歉，我无法回答这个问题。这是一段不是 JSON 的回复。"
    r = c.post(
        f"/api/ontologies/from-text?project_id={c._test_project_id}",
        json={"text": FALLBACK_TEXT, "llm_endpoint": fake_llm},
    )
    assert r.status_code == 201, r.text
    draft = r.json()["data"]
    assert draft["generation_mode"] == "fallback"
    names = {o["name"] for o in draft["objects"]}
    assert "客户" in names and "订单" in names


def test_from_text_empty_content_falls_back(c, fake_llm):
    # 致命 bug 回归：思考型模型 reasoning 吃光 max_tokens → content 为空，必须走兜底而非崩溃/误判
    _FakeLLMHandler.content = ""
    r = c.post(
        f"/api/ontologies/from-text?project_id={c._test_project_id}",
        json={"text": FALLBACK_TEXT, "llm_endpoint": fake_llm},
    )
    assert r.status_code == 201, r.text
    draft = r.json()["data"]
    assert draft["generation_mode"] == "fallback"


def test_from_text_max_tokens_passthrough(c, fake_llm):
    _FakeLLMHandler.content = json.dumps(VALID_ONTOLOGY, ensure_ascii=False)
    _FakeLLMHandler.captured = []
    r = c.post(
        f"/api/ontologies/from-text?project_id={c._test_project_id}",
        json={"text": FALLBACK_TEXT, "llm_endpoint": fake_llm, "max_tokens": 4096},
    )
    assert r.status_code == 201, r.text
    assert _FakeLLMHandler.captured[0]["max_tokens"] == 4096

    # 缺省时取 LLM 设置中的 max_tokens（不得是小硬编码值）
    _FakeLLMHandler.captured = []
    r2 = c.post(
        f"/api/ontologies/from-text?project_id={c._test_project_id}",
        json={"text": FALLBACK_TEXT, "llm_endpoint": fake_llm},
    )
    assert r2.status_code == 201, r2.text
    assert _FakeLLMHandler.captured[0]["max_tokens"] == get_llm_config().max_tokens


def test_generate_ontology_from_text_returns_source_tuple(fake_llm):
    _FakeLLMHandler.content = json.dumps(VALID_ONTOLOGY, ensure_ascii=False)
    ontology, source = generate_ontology_from_text("任意业务描述", endpoint=fake_llm)
    assert source == "llm"
    assert ontology["name"] == "AI 测试本体"

    _FakeLLMHandler.content = "不是 JSON 的垃圾输出"
    ontology, source = generate_ontology_from_text(FALLBACK_TEXT, endpoint=fake_llm)
    assert source == "fallback"
    assert any(o["name"] == "客户" for o in ontology["objects"])


def test_generate_ontology_from_text_no_fallback_raises(fake_llm):
    _FakeLLMHandler.status = 500
    with pytest.raises(Exception):
        generate_ontology_from_text("任意业务描述", endpoint=fake_llm, fallback=False)
    _FakeLLMHandler.status = 200


def test_default_prompt_template_single_braces():
    # 渲染用 .replace("{text}") 而非 .format()：模板里必须是单花括号，{{ }} 会原样送给模型
    assert "{{" not in DEFAULT_ONTOLOGY_GENERATION_PROMPT
    assert "{text}" in DEFAULT_ONTOLOGY_GENERATION_PROMPT
    assert '"stable_key"' in DEFAULT_ONTOLOGY_GENERATION_PROMPT
    rendered = render_ontology_generation("客户可以下多个订单")
    assert "客户可以下多个订单" in rendered
    assert "{{" not in rendered
