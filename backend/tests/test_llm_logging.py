"""LLM 请求/响应内容日志测试。

覆盖 ai_helper.client 的两类 LLM 调用（call_llm / test_connection）：
完整请求体（messages）与完整响应体须落到 logs/llm/llm-YYYY-MM-DD.log 的 JSON 行，
HTTP 错误路径同样须记录请求与错误响应。
"""
from __future__ import annotations

import glob
import http.server
import json
import logging
import os
import threading

import pytest

from ai_helper import client as llm_client
from config import get_settings
from core.responses import ApiError
from logging_setup import get_llm_logger


def _make_handler(status: int, content: str):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            req_body = json.loads(self.rfile.read(length) or b"{}")
            if status == 200:
                payload = {
                    "id": "chatcmpl-fake",
                    "model": req_body.get("model"),
                    "choices": [{"message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                }
            else:
                payload = {"error": {"message": "fake upstream error", "code": status}}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return _Handler


@pytest.fixture()
def llm_env(monkeypatch, tmp_path):
    """LLM 日志落到本测试的 tmp 目录，且用独立 logger 名隔离。"""
    log_dir = str(tmp_path / "logs")
    monkeypatch.setattr(get_settings(), "log_dir", log_dir)
    name = "test.llm_logging"
    logging.getLogger(name).handlers.clear()
    test_logger = get_llm_logger(name)
    monkeypatch.setattr(llm_client, "_llm_logger", lambda: test_logger)
    return log_dir


@pytest.fixture()
def fake_server():
    started: list[http.server.ThreadingHTTPServer] = []

    def _start(status: int = 200, content: str = '{"echo": 1}') -> str:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(status, content))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    yield _start
    for s in started:
        s.shutdown()


def _read_llm_records(log_dir: str) -> list[dict]:
    out: list[dict] = []
    for path in glob.glob(os.path.join(log_dir, "llm", "llm-*.log")):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def test_call_llm_logs_full_request_and_response(llm_env, fake_server):
    base = fake_server(content='{"ok": 42}')
    result = llm_client.call_llm("你好，测试请求A", base_url=base, model="fake-model")
    assert result["choices"][0]["message"]["content"] == '{"ok": 42}'

    recs = [r for r in _read_llm_records(llm_env) if r.get("msg") == "llm call ok"]
    assert len(recs) == 1
    r = recs[0]
    assert r["ok"] is True
    assert r["model"] == "fake-model"
    assert r["request"]["model"] == "fake-model"
    assert r["request"]["messages"][-1] == {"role": "user", "content": "你好，测试请求A"}
    assert r["response"]["choices"][0]["message"]["content"] == '{"ok": 42}'
    assert r["response"]["usage"]["total_tokens"] == 8
    assert r["latency_ms"] >= 0


def test_call_llm_logs_http_error_with_request(llm_env, fake_server):
    base = fake_server(status=500)
    with pytest.raises(ApiError):
        llm_client.call_llm("触发 500", base_url=base, model="fake-model")

    recs = [r for r in _read_llm_records(llm_env) if r.get("msg") == "llm http error 500"]
    assert len(recs) == 1
    r = recs[0]
    assert r["ok"] is False
    assert r["status"] == 500
    assert r["request"]["messages"][-1]["content"] == "触发 500"
    assert "fake upstream error" in r["response"]


def test_test_connection_logs_probe(llm_env, fake_server):
    base = fake_server(content='{"ok": true}')
    result = llm_client.test_connection(base_url=base, model="fake-model")
    assert result["ok"] is True

    recs = [r for r in _read_llm_records(llm_env) if r.get("msg") == "llm probe ok"]
    assert len(recs) == 1
    r = recs[0]
    assert r["request"]["messages"][0]["content"].startswith("请回复一个 JSON")
    assert r["response"]["choices"][0]["message"]["content"] == '{"ok": true}'
