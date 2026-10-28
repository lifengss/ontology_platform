"""统一 LLM 调用客户端（OpenAI 兼容 /chat/completions）。"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from ai_helper.config import get_llm_config
from core.responses import ApiError
from logging_setup import get_llm_logger, get_logger, log_event

logger = get_logger("ai_helper.client")


def _llm_logger() -> logging.Logger:
    """LLM 请求/响应内容专用 logger（logs/llm/ 独立 JSON 行文件）。"""
    return get_llm_logger()


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
            latency_ms = round((time.time() - started) * 1000, 1)
            # LLM 内容日志：完整请求体（messages）+ 完整响应对象（不含请求头/API Key）
            log_event(
                _llm_logger(),
                logging.INFO,
                "llm call ok",
                ok=True,
                model=selected_model,
                url=url,
                latency_ms=latency_ms,
                request=body,
                response=result,
            )
            logger.info(
                "LLM call ok model=%s url=%s latency_ms=%.0f",
                selected_model,
                url,
                latency_ms,
            )
            return result
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        log_event(
            _llm_logger(),
            logging.WARNING,
            f"llm http error {e.code}",
            ok=False,
            model=selected_model,
            url=url,
            status=e.code,
            latency_ms=round((time.time() - started) * 1000, 1),
            request=body,
            response=err_body,
        )
        logger.warning("LLM HTTP error %s: %s", e.code, err_body)
        raise ApiError("LLM_ERROR", f"LLM HTTP {e.code}: {err_body}", http_status=502)  # noqa: TRY401
    except Exception as e:
        log_event(
            _llm_logger(),
            logging.WARNING,
            "llm call failed",
            ok=False,
            model=selected_model,
            url=url,
            latency_ms=round((time.time() - started) * 1000, 1),
            request=body,
            error=str(e),
        )
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
            log_event(
                _llm_logger(),
                logging.INFO,
                "llm probe ok",
                ok=True,
                model=selected_model,
                url=url,
                latency_ms=round(latency, 1),
                request=body,
                response=result,
            )
            return {
                "ok": True,
                "latency_ms": round(latency, 1),
                "model": selected_model,
                "url": url,
                "content_preview": content[:200],
            }
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        log_event(
            _llm_logger(),
            logging.WARNING,
            f"llm probe http error {e.code}",
            ok=False,
            model=selected_model,
            url=url,
            status=e.code,
            request=body,
            response=err_body,
        )
        return {"ok": False, "error": f"HTTP {e.code}: {err_body}", "url": url, "model": selected_model}
    except Exception as e:
        log_event(
            _llm_logger(),
            logging.WARNING,
            "llm probe failed",
            ok=False,
            model=selected_model,
            url=url,
            request=body,
            error=str(e),
        )
        return {"ok": False, "error": str(e), "url": url, "model": selected_model}
