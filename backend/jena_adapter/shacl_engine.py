"""V2-17（M4-D）SHACL 执行路径引擎配置。

优先级：运行时配置 data/shacl_engine.json（PUT /api/shacl/engine 写入）
> 环境变量 ONTOLOGY_SHACL_ENGINE（部署期默认）> 缺省 "local"。

引擎：
- local：本地 pyshacl（既有默认路径）；
- jena：Jena SHACL 引擎（org.apache.jena.shacl，随 Fuseki fat jar 内置，与推理服务
  同一 Jena/RDF 栈），经 Java 子进程执行（jena_adapter.shacl_jena）。

诚实口径（2026-11-16 依赖事实，详见 API 文档 §36 与 fuseki-config.ttl 头部注记）：
Fuseki standalone fat jar 无 SHACL HTTP 服务端点（SHACL_Validation servlet 未注册进
任何默认服务组，配置格式未文档化），故 Jena 侧为「Java 子进程执行 Jena SHACL 引擎」，
每次校验含 JVM 启动＋文件序列化开销——时延基线如实记录（TC-17-05），不断言 Jena 快于本地。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from config import get_settings

VALID_ENGINES = ("local", "jena")
_DEFAULT = "local"
ENV_VAR = "ONTOLOGY_SHACL_ENGINE"


def _config_path() -> str:
    return os.path.join(get_settings().data_dir, "shacl_engine.json")


def get_configured_engine() -> tuple[str, str]:
    """解析生效 SHACL 引擎。返回 (engine, source)：source ∈ runtime | env | default。"""
    p = _config_path()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                v = (json.load(f) or {}).get("engine")
            if v in VALID_ENGINES:
                return v, "runtime"
        except (OSError, ValueError):
            pass
    env = os.environ.get(ENV_VAR, "").strip().lower()
    if env in VALID_ENGINES:
        return env, "env"
    return _DEFAULT, "default"


def set_engine(engine: str) -> dict[str, Any]:
    """运行时切换 SHACL 执行路径（原子写 data/shacl_engine.json）。"""
    if engine not in VALID_ENGINES:
        raise ValueError(f"engine 必须是 {' 或 '.join(VALID_ENGINES)}")
    p = _config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"engine": engine, "updated_at": datetime.now().isoformat(timespec="seconds")},
                  f, ensure_ascii=False)
    os.replace(tmp, p)
    return get_configured_engine()
