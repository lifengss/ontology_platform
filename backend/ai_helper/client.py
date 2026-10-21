"""统一 LLM 调用客户端（OpenAI 兼容 /chat/completions）。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ai_helper.config import get_llm_config
from core.responses import ApiError
from logging_setup import get_logger

logger = get_logger("ai_helper.client")


def call_llm(
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout: int | None = None,
    max_tokens: int | None = None,
    system_message: str | None = None,
) -> dict[str, Any]:
    """调用 LLM，返回完整的 OpenAI 兼容响应对象（含 choices/usage 等）。"""
    cfg = get_llm_config()
    url = (base_url or cfg.base_url()).rstrip("/") + "/chat/completions"
    selected_model = model or cfg.model
    messages: list[dict[str, str]] = []
    if system_message:
        messages.append({"role": "system", "content": system_message})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, Any] = {
        "model": selected_model,
        "messages": messages,
        "temperature": temperature if temperature is not None else cfg.temperature,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    elif cfg.max_tokens:
        body["max_tokens"] = cfg.max_tokens

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=cfg.headers(), method="POST")  # noqa: S310
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg.timeout) as resp:  # noqa: S310
            result = json.loads(resp.read().decode("utf-8"))
            logger.info(
                "LLM call ok model=%s url=%s latency_ms=%.0f",
                selected_model,
                url,
                (time.time() - started) * 1000,
            )
            return result
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        logger.warning("LLM HTTP error %s: %s", e.code, err_body)
        raise ApiError("LLM_ERROR", f"LLM HTTP {e.code}: {err_body}", http_status=502)  # noqa: TRY401
    except Exception as e:
        logger.warning("LLM call failed: %s", e)
        raise ApiError("LLM_ERROR", f"LLM 调用失败: {e}", http_status=502)  # noqa: TRY401


def test_connection(
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """发送一条轻量 probe，验证 LLM 连通性。不依赖全局配置，可用于前端测试。"""
    cfg = get_llm_config()
    url = (base_url or cfg.base_url()).rstrip("/") + "/chat/completions"
    selected_model = model or cfg.model
    messages = [{"role": "user", "content": "请回复一个 JSON：{\"ok\": true}"}]
    body = {
        "model": selected_model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 256,
    }
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif cfg.api_key():
        headers["Authorization"] = f"Bearer {cfg.api_key()}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")  # noqa: S310
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg.timeout) as resp:  # noqa: S310
            result = json.loads(resp.read().decode("utf-8"))
            latency = (time.time() - started) * 1000
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {
                "ok": True,
                "latency_ms": round(latency, 1),
                "model": selected_model,
                "url": url,
                "content_preview": content[:200],
            }
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {e.code}: {err_body}", "url": url, "model": selected_model}
    except Exception as e:
        return {"ok": False, "error": str(e), "url": url, "model": selected_model}
