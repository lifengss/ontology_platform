"""GAP-02 验收：错误统一封装——通俗解释（message_zh）＋可操作修复建议（suggestion）。

覆盖（docs/本体平台功能增补清单.md GAP-02）：
- fail() 按 FRIENDLY_ERRORS 注册表自动补全 message_zh/suggestion；
- 显式 message_zh/suggestion 优先于注册表；
- 未知错误码走消息模式兜底（如 pydantic "not in list"）；
- FastAPI/pydantic 校验错误（原裸 422 detail）统一转信封并友好化
  （"Value Literal('') not in list" → 通俗说明＋允许值清单）；
- 成功信封保持向后兼容（错误字段为 null）。
"""
from __future__ import annotations

from typing import Literal, Optional

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from _authenv import auth_module_guard
from main import app
from core.responses import ApiError, friendly_error_text, fail

pytestmark = pytest.mark.auth
_guard = auth_module_guard()  # GAP-20：模块边界隔离鉴权环境

c = TestClient(app)


# ------------------------------------------------------------ 1. 注册表补全

def test_01_known_code_enriched():
    r = fail(ApiError("INCOMPATIBLE_REQUIRES_MAJOR",
                      "incompatible change detected: attr 'x' required->optional removed"))
    assert r.status_code == 409 or r.status_code == 400
    err = r.body if isinstance(r.body, str) else r.body.decode()
    import json
    body = json.loads(err)
    assert body["success"] is False
    e = body["error"]
    assert e["code"] == "INCOMPATIBLE_REQUIRES_MAJOR"
    assert e["message_zh"], "已知错误码应自动带通俗解释"
    assert e["suggestion"], "已知错误码应自动带修复建议"
    # message 保留原始技术信息（审计/排障依赖）
    assert "incompatible change" in e["message"]


def test_02_message_pattern_fallback():
    # 未知错误码＋pydantic v1 风格消息 → 消息模式兜底
    zh, sug = friendly_error_text("SOME_NEW_CODE", "Value Literal('') not in list")
    assert zh and sug, "未知错误码应走消息模式兜底"
    assert "枚举" in zh or "列表" in zh
    # 完全无匹配 → 双双 None（不硬凑话术）
    assert friendly_error_text("SOME_NEW_CODE", "xyz") == (None, None)


def test_03_explicit_friendly_wins():
    r = fail(ApiError("NOT_FOUND", "resource missing",
                      message_zh="自定义通俗话术", suggestion="自定义建议"))
    import json
    body = json.loads(r.body.decode() if isinstance(r.body, bytes) else r.body)
    e = body["error"]
    assert e["message_zh"] == "自定义通俗话术"
    assert e["suggestion"] == "自定义建议"


# ------------------------------------------------------------ 2. pydantic 校验错误友好化

class _Gap02In(BaseModel):
    engine: Literal["local", "fuseki"]
    limit: Optional[int] = None


@app.post("/api/_gap02_test_validation")
def _gap02_probe(payload: _Gap02In):
    from core.responses import success
    return success({"payload": payload.model_dump()})


def test_04_validation_error_envelope_friendly():
    # 非法枚举（对应清单示例 "Value Literal('') not in list" 的 v2 形态）
    r = c.post("/api/_gap02_test_validation", json={"engine": "bogus"})
    assert r.status_code == 422
    body = r.json()
    assert body["success"] is False, "校验错误应转统一信封"
    e = body["error"]
    assert e["code"] == "VALIDATION_ERROR"
    assert e["message_zh"] and "枚举" in e["message_zh"], e
    assert e["suggestion"] and "local" in e["suggestion"], "建议应列出允许值"
    assert e["details"]["count"] >= 1


def test_05_validation_missing_and_type():
    r = c.post("/api/_gap02_test_validation", json={"limit": 5})  # 缺 engine
    assert r.status_code == 422
    e = r.json()["error"]
    assert e["message_zh"] and "engine" in e["message_zh"], e
    assert e["suggestion"]

    r = c.post("/api/_gap02_test_validation", json={"engine": "local", "limit": "abc"})
    assert r.status_code == 422
    e = r.json()["error"]
    assert e["message_zh"] and e["suggestion"], e


# ------------------------------------------------------------ 3. 既有 API 受益（回归向）

def test_06_existing_api_error_has_friendly_fields():
    # 与 GAP-03 相同的引擎切换接口：非法值 422，且带通俗解释＋建议
    r = c.put("/api/query/engine", json={"engine": "bogus"})
    assert r.status_code == 422
    e = r.json()["error"]
    assert e["message_zh"] and e["suggestion"]
    assert "local" in e["suggestion"] and "fuseki" in e["suggestion"]


# ------------------------------------------------------------ 4. 向后兼容

def test_07_success_envelope_backward_compat():
    from core.responses import Envelope, ErrorDetail
    e = ErrorDetail(code="X", message="m")
    assert e.message_zh is None and e.suggestion is None
    env = Envelope(success=True, data={"a": 1}, error=None)
    d = env.model_dump()
    assert d["success"] is True and d["data"] == {"a": 1}
