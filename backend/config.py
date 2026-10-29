"""平台配置加载。

遵循 PRD §12 已确认决策：平台自身不实现鉴权层；外部凭据通过 env / 本地配置文件管理。
所有配置项支持从环境变量覆盖，便于 Docker Compose 注入。
"""
from __future__ import annotations

import os
from functools import lru_cache

# 优先从项目 .env 加载环境变量；启动时执行一次，允许覆盖既有环境变量
from dotenv import load_dotenv

load_dotenv(override=True)


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


class Settings:
    """运行时配置（单例）。"""

    def __init__(self) -> None:
        self.app_name: str = os.getenv("APP_NAME", "EnterpriseOntologyPlatform")
        self.version: str = os.getenv("APP_VERSION", "1.0.1")

        # 依赖服务（VS0 健康检查会探测可达性）
        # MVP 默认从本机启动，不依赖 Docker；Docker Compose 通过环境变量覆盖即可
        self.jena_base_url: str = os.getenv("JENA_BASE_URL", "http://localhost:3030")
        self.jena_dataset: str = os.getenv("JENA_DATASET", "ontology")
        self.neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password: str = os.getenv("NEO4J_PASSWORD", "neo4jsecret")
        self.postgres_dsn: str = os.getenv(
            "POSTGRES_DSN", "postgresql://ont:ontsecret@localhost:5432/ont"
        )

        # 本地持久化（无需 Postgres 也能跑通 VS0/VS1 的演示路径）
        self.data_dir: str = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
        self.secrets_dir: str = os.getenv("SECRETS_DIR", os.path.join(os.path.dirname(__file__), "secrets"))

        # 外部 MCP 调用健壮性（PRD §6.5）
        self.mcp_timeout_seconds: int = _env_int("MCP_TIMEOUT_SECONDS", 10)
        self.mcp_max_retries: int = _env_int("MCP_MAX_RETRIES", 3)

        # Action 受控回写健壮性（PRD §6.6 护栏 4：失败指数退避 + 熔断）
        self.action_max_retries: int = _env_int("ACTION_MAX_RETRIES", 2)
        self.action_breaker_failure_threshold: int = _env_int("ACTION_BREAKER_FAILURE_THRESHOLD", 3)
        self.action_breaker_cooldown_seconds: float = _env_float("ACTION_BREAKER_COOLDOWN_SECONDS", 30.0)

        # 凭据加密（生产建议；Demo 默认关闭）
        self.credential_encryption_enabled: bool = _env_bool("CREDENTIAL_ENCRYPTION_ENABLED", False)

        # 默认 LLM 端点（建模/逆向用）
        self.llm_endpoint: str = os.getenv("LLM_ENDPOINT", "http://localhost:11434/v1")
        self.llm_model: str = os.getenv("LLM_MODEL", "default")

        # 日志
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO")
        self.log_dir: str = os.getenv("LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))

    def ensure_dirs(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        os.makedirs(self.secrets_dir, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
