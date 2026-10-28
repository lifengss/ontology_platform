"""结构化日志（JSON 行），便于 grep/jq 诊断。

- 日志按天落到 logs/app-YYYY-MM-DD.log
- 仅保留最近 2 天日志，启动时自动清理过期文件
- 提供 read_recent_logs(days=2, limit=2000)：按日期从新到旧拼接，返回最近 N 条
- 遵循 test-knowledge-system 已验证的日志规范：extra 字段避开 LogRecord 内建名
"""
from __future__ import annotations

import glob
import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta, timezone

from config import get_settings

DEFAULT_RETENTION_DAYS = 2


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


def _log_path(log_dir: str, day: datetime) -> str:
    return os.path.join(log_dir, f"app-{day.strftime('%Y-%m-%d')}.log")


def _sweep_old_logs(
    log_dir: str,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    pattern: str = "app-*.log",
) -> None:
    """删除 mtime 超过 retention_days 天且匹配 pattern 的日志文件。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for path in glob.glob(os.path.join(log_dir, pattern)):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
            if mtime < cutoff:
                os.remove(path)
        except OSError:
            pass


def read_recent_logs(
    *,
    days: int = DEFAULT_RETENTION_DAYS,
    limit: int = 2000,
    log_dir: str | None = None,
) -> list[dict]:
    """读取最近 days 天的日志，按时间从新到旧返回最多 limit 条 JSON 对象。"""
    if log_dir is None:
        log_dir = get_settings().log_dir
    if not os.path.isdir(log_dir):
        return []

    since = datetime.now(timezone.utc) - timedelta(days=days)
    files: list[tuple[str, datetime]] = []
    for path in glob.glob(os.path.join(log_dir, "app-*.log")):
        basename = os.path.basename(path)
        try:
            day = datetime.strptime(basename, "app-%Y-%m-%d.log").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if day >= since:
            files.append((path, day))

    # 日期从新到旧
    files.sort(key=lambda x: x[1], reverse=True)

    out: list[dict] = []
    for path, _ in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        # 单个文件按行号倒序（最新记录在最后），保持整体从新到旧
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                return out
    return out


def get_logger(name: str, extra: dict | None = None) -> logging.Logger:
    settings = get_settings()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(settings.log_level)

    os.makedirs(settings.log_dir, exist_ok=True)
    _sweep_old_logs(settings.log_dir)

    app_log = _log_path(settings.log_dir, datetime.now(timezone.utc))

    # 按天文件；Windows 多进程同时写同一文件时 rename 会冲突，故 Windows 退化为不轮转 FileHandler
    if sys.platform == "win32":
        file_handler = logging.FileHandler(app_log, encoding="utf-8")
    else:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            app_log, when="midnight", backupCount=DEFAULT_RETENTION_DAYS, encoding="utf-8"
        )
    file_handler.setFormatter(JsonFormatter())

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(JsonFormatter())

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


def get_llm_logger(name: str = "ai_helper.llm") -> logging.Logger:
    """LLM 请求/响应内容专用日志：``logs/llm/llm-YYYY-MM-DD.log``（JSON 行，按天）。

    - 记录完整请求体（messages/prompt）与完整响应体，便于诊断 prompt 质量与模型输出
    - 不记录请求头（避免泄露 API Key）；仅落文件，不刷 stdout（prompt 可能很大）
    - 保留策略复用应用日志（DEFAULT_RETENTION_DAYS 天，按 mtime 清扫）
    """
    settings = get_settings()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(settings.log_level)

    llm_dir = os.path.join(settings.log_dir, "llm")
    os.makedirs(llm_dir, exist_ok=True)
    _sweep_old_logs(llm_dir, DEFAULT_RETENTION_DAYS, pattern="llm-*.log")

    file_handler = logging.FileHandler(
        os.path.join(llm_dir, f"llm-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"),
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **fields) -> None:
    """便捷方法：带结构化字段记录（字段避开内建名）。"""
    logger.log(level, message, extra={"extra_fields": fields})
