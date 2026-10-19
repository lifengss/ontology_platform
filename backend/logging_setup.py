"""结构化日志（JSON 行），便于 grep/jq 诊断。

按天轮转，应用日志保留 7 天。大模型请求/响应日志（含 prompt/response）为后续扩展点，
当前统一写入 app.log。遵循 test-knowledge-system 已验证的日志规范：extra 字段避开 LogRecord 内建名。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone

from config import get_settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # 避开 module/msg/levelname 等内建名，统一用 src
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "src": record.name,
            "msg": record.getMessage(),
        }
        if getattr(record, "extra_fields", None):
            payload.update(record.extra_fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str, extra: dict | None = None) -> logging.Logger:
    settings = get_settings()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(settings.log_level)

    os.makedirs(settings.log_dir, exist_ok=True)
    app_log = os.path.join(settings.log_dir, "app.log")

    # 按天轮转，保留 7 天
    file_handler = logging.handlers.TimedRotatingFileHandler(
        app_log, when="midnight", backupCount=7, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(JsonFormatter())

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **fields) -> None:
    """便捷方法：带结构化字段记录（字段避开内建名）。"""
    logger.log(level, message, extra={"extra_fields": fields})
