"""LLM 配置管理：.env 存 secrets，llm_config.json 存非敏感运行时配置。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from config import get_settings


class LLMConfig:
    """LLM 运行时配置（非敏感部分）与 secrets 桥接。"""

    def __init__(self, config_path: str | None = None) -> None:
        self._config_path = config_path or os.path.join(get_settings().data_dir, "llm_config.json")
        self._defaults: dict[str, Any] = {
            "provider": "openai_compatible",
            "model": "default",
            "temperature": 0.2,
            "timeout": 120,
            "max_tokens": 4096,
            "prompts": {
                "ontology_generation": (
                    "你是企业业务本体建模专家。请根据以下业务描述，生成一个结构化的本体定义。\n\n"
                    "业务描述：\n{text}\n\n"
                    "请只返回如下 JSON 格式，不要返回任何解释或 markdown 代码块外的内容：\n"
                    "{...}"
                ),
                "instance_extraction": (
                    "你是知识抽取专家。请根据以下本体定义和文本，抽取实例。"
                ),
                "reverse_engineering": (
                    "你是数据建模专家。请根据以下表结构/DDL/JSON 样本，反向工程生成本体。"
                ),
                "kg_construction": (
                    "你是知识图谱构建专家。请根据以下文档提取实体与关系。"
                ),
            },
        }
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        path = Path(self._config_path)
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # 合并缺省值，避免旧文件缺少新字段
                return {**self._defaults, **loaded}
            except Exception:  # noqa: BLE001
                return dict(self._defaults)
        return dict(self._defaults)

    def save(self, data: dict[str, Any] | None = None) -> None:
        if data is not None:
            self._data = {**self._defaults, **data}
        path = Path(self._config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    @property
    def provider(self) -> str:
        return str(self._data.get("provider", self._defaults["provider"]))

    @property
    def model(self) -> str:
        return str(self._data.get("model", os.getenv("LLM_MODEL", self._defaults["model"])))

    @property
    def temperature(self) -> float:
        return float(self._data.get("temperature", self._defaults["temperature"]))

    @property
    def timeout(self) -> int:
        return int(self._data.get("timeout", self._defaults["timeout"]))

    @property
    def max_tokens(self) -> int:
        return int(self._data.get("max_tokens", self._defaults["max_tokens"]))

    @property
    def prompts(self) -> dict[str, str]:
        return self._data.get("prompts", self._defaults["prompts"])

    def get_prompt(self, name: str) -> str:
        return self.prompts.get(name, self._defaults["prompts"].get(name, ""))

    def base_url(self, override: str | None = None) -> str:
        return (
            override
            or os.getenv("LLM_BASE_URL")
            or os.getenv("LLM_ENDPOINT")
            or str(self._data.get("base_url") or "")
            or "http://localhost:11434/v1"
        ).rstrip("/")

    def api_key(self) -> str | None:
        from credential_store import load_secret
        key = os.getenv("LLM_API_KEY")
        if not key:
            key = load_secret("LLM_API_KEY")
        if not key:
            key = os.getenv("OPENAI_API_KEY")
        if not key:
            key = load_secret("OPENAI_API_KEY")
        return key or None

    def headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        key = self.api_key()
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h


_llm_config: LLMConfig | None = None


def get_llm_config() -> LLMConfig:
    global _llm_config
    if _llm_config is None:
        _llm_config = LLMConfig()
    return _llm_config


def reload_llm_config() -> LLMConfig:
    global _llm_config
    _llm_config = LLMConfig()
    return _llm_config
