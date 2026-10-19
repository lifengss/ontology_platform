"""credential_store：外部凭据本地管理（不入库、不打印；chmod 600；.gitignore）。

VS9。凭据以 ${ENV_NAME} 引用形式出现在连接配置中，明文只存在于 env 或本地配置文件。
生产建议 cryptography.Fernet 加密落盘（CREDENTIAL_ENCRYPTION_ENABLED）。
"""
from __future__ import annotations

import os
import re

from config import get_settings
from logging_setup import get_logger

logger = get_logger("credential_store")

_REF_PATTERN = re.compile(r"\$\{([^}]+)\}")


def list_configured() -> dict:
    """仅返回已配置/缺失的引用名，绝不返回明文值。"""
    settings = get_settings()
    refs = set()
    # 扫描 secrets 目录下文件名（形如 <ENV_NAME>.json）
    secrets_dir = settings.secrets_dir
    for fn in os.listdir(secrets_dir) if os.path.isdir(secrets_dir) else []:
        if fn.endswith(".json"):
            refs.add(fn[: -len(".json")].upper())
    for key in os.environ:
        if key.isupper():
            refs.add(key)
    configured = sorted(r for r in refs if os.getenv(r) is not None or r in refs)
    missing = [r for r in configured if os.getenv(r) is None]
    return {"configured": [r for r in configured if os.getenv(r) is not None], "missing": missing}


def resolve_ref(ref: str) -> str | None:
    """解析 ${ENV_NAME} 为实际值（仅在确需调用外部时使用，严禁记录日志打印）。"""
    m = _REF_PATTERN.fullmatch(ref) if ref.startswith("${") else None
    if m:
        return os.getenv(m.group(1))
    return os.getenv(ref)


def redact(value: str) -> str:
    """脱敏：日志中打印凭据占位。"""
    return "***redacted***"
