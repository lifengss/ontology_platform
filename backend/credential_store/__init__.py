"""credential_store：外部凭据本地管理（不入库、不打印；chmod 600；.gitignore）。

VS9。凭据以 ${ENV_NAME} 引用形式出现在连接配置中，明文只存在于 env 或本地配置文件。
生产建议 cryptography.Fernet 加密落盘（CREDENTIAL_ENCRYPTION_ENABLED）。
"""
from __future__ import annotations

import json
import os
import re
import stat

from config import get_settings
from logging_setup import get_logger

logger = get_logger("credential_store")

_REF_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _secrets_dir() -> str:
    return get_settings().secrets_dir


def _encryption_key() -> bytes | None:
    """返回稳定的 Fernet 密钥（优先环境变量，次之用项目固定密钥文件）。"""
    key_env = os.getenv("CREDENTIAL_MASTER_KEY")
    if key_env:
        return key_env.encode()
    key_file = os.path.join(_secrets_dir(), ".master.key")
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            return f.read()
    return None


def _get_fernet():
    from cryptography.fernet import Fernet
    key = _encryption_key()
    if not key:
        return None
    # Fernet 需要 32 字节 base64 编码密钥；如果用户提供原始字符串，先做派生
    try:
        return Fernet(key)
    except Exception:  # noqa: BLE001
        import base64
        import hashlib
        derived = base64.urlsafe_b64encode(hashlib.sha256(key).digest())
        return Fernet(derived)


def _secret_path(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name.upper())
    return os.path.join(_secrets_dir(), f"{safe}.json")


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
        return load_secret(m.group(1)) or os.getenv(m.group(1))
    return load_secret(ref) or os.getenv(ref)


def redact(value: str) -> str:
    """脱敏：日志中打印凭据占位。"""
    return "***redacted***"


def load_secret(name: str) -> str | None:
    """从本地 secrets 文件读取凭据；若启用加密则自动解密。环境变量优先。"""
    env_val = os.getenv(name)
    if env_val:
        return env_val
    path = _secret_path(name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        value = payload.get("value")
        if not value:
            return None
        if payload.get("encrypted") and get_settings().credential_encryption_enabled:
            fernet = _get_fernet()
            if fernet:
                value = fernet.decrypt(value.encode()).decode("utf-8")
        return value
    except Exception as e:  # noqa: BLE001
        logger.warning(f"load_secret failed for {name}: {e}")
        return None


def save_secret(name: str, value: str) -> None:
    """保存凭据到本地 secrets 文件；启用加密时自动加密。设置 chmod 600。"""
    os.makedirs(_secrets_dir(), exist_ok=True)
    path = _secret_path(name)
    encrypted = False
    stored_value = value
    if get_settings().credential_encryption_enabled:
        fernet = _get_fernet()
        if fernet:
            stored_value = fernet.encrypt(value.encode("utf-8")).decode("utf-8")
            encrypted = True
        else:
            logger.warning("credential_encryption_enabled but no master key; storing plaintext")
    payload = {"name": name.upper(), "value": stored_value, "encrypted": encrypted}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    logger.info(f"saved secret {name}")


def delete_secret(name: str) -> None:
    path = _secret_path(name)
    if os.path.exists(path):
        os.remove(path)
