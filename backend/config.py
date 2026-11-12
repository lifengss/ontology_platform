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
        self.version: str = os.getenv("APP_VERSION", "1.0.2")

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

        # V2-11（M3-A）：schema 漂移检测
        # 周期探测开关（只拉字段结构、不拉数据值）；周期秒数；
        # 连接健康视图「长期未检查」阈值 = stale_multiplier × 探测周期
        self.schema_drift_probe_enabled: bool = _env_bool("ONTOLOGY_SCHEMA_DRIFT_PROBE_ENABLED", True)
        self.schema_drift_probe_interval_seconds: int = _env_int("ONTOLOGY_SCHEMA_DRIFT_PROBE_INTERVAL_SECONDS", 300)
        self.schema_drift_stale_multiplier: float = _env_float("ONTOLOGY_SCHEMA_DRIFT_STALE_MULTIPLIER", 3.0)

        # V2-21（M3-C）：数据健康度评估
        # 陈旧阈值（天）：来源连接 last_fetched 距今超过该值记为 stale；
        # 治理身份采样大小：源连接型实例的非键值指标（填充/SHACL）采样读取源端最新值；
        # 源端采样开关：关闭时源连接型值指标显式标记 source_required（不静默、不臆测）
        self.data_quality_stale_days: int = _env_int("ONTOLOGY_DATA_QUALITY_STALE_DAYS", 30)
        self.data_quality_sample_size: int = _env_int("ONTOLOGY_DATA_QUALITY_SAMPLE_SIZE", 50)
        self.data_quality_source_enabled: bool = _env_bool("ONTOLOGY_DATA_QUALITY_SOURCE_ENABLED", True)

        # M3-D（V2-12）流式 CDC + 连接健康视图：
        # 全局开关 / 消费轮询周期 / binlog 长轮询等待（mock subscribe wait_ms 上限 15s）/
        # 单批事件上限（积压限流）/ 单轮最多连续批数 / 批间停顿（限流）/
        # 事件滞后告警阈值（秒）/ 断流（停滞）告警阈值（秒）/
        # 熔断连续失败阈值 / 熔断冷却基础秒（指数退避翻倍）/
        # CDC 未支持连接的降级周期拉取间隔（秒）
        self.cdc_enabled: bool = _env_bool("ONTOLOGY_CDC_ENABLED", True)
        self.cdc_poll_interval_seconds: int = _env_int("ONTOLOGY_CDC_POLL_INTERVAL_SECONDS", 5)
        self.cdc_subscribe_wait_ms: int = min(15000, _env_int("ONTOLOGY_CDC_SUBSCRIBE_WAIT_MS", 2000))
        self.cdc_max_batch: int = _env_int("ONTOLOGY_CDC_MAX_BATCH", 100)
        self.cdc_max_batches_per_poll: int = _env_int("ONTOLOGY_CDC_MAX_BATCHES_PER_POLL", 10)
        self.cdc_batch_pause_ms: int = _env_int("ONTOLOGY_CDC_BATCH_PAUSE_MS", 100)
        self.cdc_lag_alert_seconds: int = _env_int("ONTOLOGY_CDC_LAG_ALERT_SECONDS", 60)
        self.cdc_stale_seconds: int = _env_int("ONTOLOGY_CDC_STALE_SECONDS", 120)
        self.cdc_circuit_threshold: int = _env_int("ONTOLOGY_CDC_CIRCUIT_THRESHOLD", 5)
        self.cdc_circuit_cooldown_seconds: int = _env_int("ONTOLOGY_CDC_CIRCUIT_COOLDOWN_SECONDS", 30)
        self.cdc_fallback_pull_seconds: int = _env_int("ONTOLOGY_CDC_FALLBACK_PULL_SECONDS", 300)

        # M3-E（V2-13）回写风险分级：
        # - writeback_enabled：真实回写开关（false=展示模式，confirm 后不真实外部调用，
        #   保留 MVP 默认行为；true 时按风险分级真实执行低/中/高危回写）
        # - writeback_confirm_ttl_seconds：确认 token 有效期（秒），超时未确认 → 回写取消
        #   并记录取消原因与操作者（EP-13-03；<=0 表示不过期）
        # - writeback_ai_timeout_seconds：中危 AI 三条件评估超时（秒），
        #   超时/评估失败/无法判定 → 升级人工确认（失败≠通过，EP-13-02）
        self.writeback_enabled: bool = _env_bool("ONTOLOGY_WRITEBACK_ENABLED", False)
        self.writeback_confirm_ttl_seconds: int = _env_int("ONTOLOGY_WRITEBACK_CONFIRM_TTL_SECONDS", 600)
        self.writeback_ai_timeout_seconds: int = _env_int("ONTOLOGY_WRITEBACK_AI_TIMEOUT_SECONDS", 30)

    def ensure_dirs(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        os.makedirs(self.secrets_dir, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
