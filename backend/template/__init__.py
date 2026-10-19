"""template：预置行业模板（LinkML）+ 网络加载预览外部模板（VS5，P1）。"""
from __future__ import annotations

from logging_setup import get_logger

logger = get_logger("template")


class TemplateManager:
    def list_templates(self) -> list[dict]:
        logger.info("list_templates (VS5 补全 LinkML 预置模板)")
        return []

    def load_external(self, url: str) -> dict:
        logger.info("load_external url=%s (VS5 补全网络预览)", url)
        raise NotImplementedError("外部模板预览在 VS5 切片实现")
