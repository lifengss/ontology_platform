"""性能基线与告警（GAP-03）。

冻结本地快路径性能基线（data/perf_baseline.json，由 scripts/bench_v2_16_reasoning.py /
bench_v2_17_shacl.py 的 --freeze 写入），运行期对关键操作（SPARQL 查询 / 推理 / SHACL
校验）记录实测并与基线/目标对比：
- 实测 > 绝对目标（查询 <1s、推理 <3s，见 DEFAULT_TARGETS）→ PERF_TARGET_EXCEEDED 告警；
- 实测 > 冻结基线 × 回归系数（缺省 2.0，ONTOLOGY_PERF_REGRESSION_FACTOR）→ PERF_REGRESSION 告警。

基线文件结构：{scope: {"baseline_ms": float, "target_ms": float|null, "frozen_at": str}}。
record() 内部任何异常都被吞掉（性能观测不得影响主流程）。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from config import get_settings
from logging_setup import get_logger

logger = get_logger("perf")

# 绝对目标（秒）：本地快路径查询 <1s、推理 <3s（GAP-03 清单口径）
DEFAULT_TARGETS: dict[str, float] = {
    "sparql_local": 1.0,
    "sparql_remote": 30.0,
    "reasoning_local": 3.0,
    "reasoning_remote": 60.0,
    "shacl_local": 3.0,
    "shacl_jena": 10.0,
}
REGRESSION_FACTOR = float(os.environ.get("ONTOLOGY_PERF_REGRESSION_FACTOR", "2.0") or 2.0)

_lock = threading.Lock()
_baseline_cache: dict | None = None


def _baseline_path() -> str:
    return os.path.join(get_settings().data_dir, "perf_baseline.json")


def load_baseline() -> dict:
    """读取（并缓存）基线文件；文件不存在返回空。"""
    global _baseline_cache
    if _baseline_cache is not None:
        return _baseline_cache
    try:
        with open(_baseline_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        _baseline_cache = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _baseline_cache = {}
    return _baseline_cache


def freeze(scope: str, elapsed_ms: float) -> dict:
    """把一次实测冻结为该 scope 的基线（bench 脚本 --freeze 调用）。"""
    global _baseline_cache
    entry = {
        "baseline_ms": round(float(elapsed_ms), 3),
        "target_ms": round(DEFAULT_TARGETS[scope] * 1000, 3) if scope in DEFAULT_TARGETS else None,
        "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with _lock:
        data = dict(load_baseline())
        data[scope] = entry
        path = _baseline_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _baseline_cache = data
    logger.info("性能基线已冻结 scope=%s baseline_ms=%.3f target_ms=%s",
                scope, entry["baseline_ms"], entry["target_ms"])
    return entry


def record(scope: str, elapsed: float) -> None:
    """记录一次操作实测（秒）；超目标/超基线回归系数时告警。永不抛异常。"""
    try:
        elapsed_ms = elapsed * 1000
        base = load_baseline().get(scope) or {}
        target_ms = base.get("target_ms")
        if not target_ms and scope in DEFAULT_TARGETS:
            target_ms = DEFAULT_TARGETS[scope] * 1000
        if target_ms and elapsed_ms > float(target_ms):
            logger.warning("PERF_TARGET_EXCEEDED scope=%s elapsed_ms=%.1f target_ms=%.1f",
                           scope, elapsed_ms, float(target_ms))
        baseline_ms = base.get("baseline_ms")
        if baseline_ms and elapsed_ms > float(baseline_ms) * REGRESSION_FACTOR:
            logger.warning("PERF_REGRESSION scope=%s elapsed_ms=%.1f baseline_ms=%.1f factor=%.1f",
                           scope, elapsed_ms, float(baseline_ms), REGRESSION_FACTOR)
    except Exception as e:  # noqa: BLE001 - 观测失败不影响主流程
        logger.debug("perf record 失败 scope=%s: %s", scope, e)
