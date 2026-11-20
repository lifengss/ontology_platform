"""真实 Postgres 可用性探测（GAP-21）。

PG 依赖用例（test_v2_20_k8s 的 PgInstanceStore 系列）统一用 `pg_skipif()`：
5432 不可达时全量运行自动 skip（pytest -rs 可见原因），不再抛连接错误。

探测目标：优先解析 settings.postgres_dsn；PGHOST/PGPORT 环境变量可覆盖；
缺省 127.0.0.1:5432。TCP 探测超时 0.6s（收集期执行一次，开销可忽略）。
"""
from __future__ import annotations

import os
import socket


def _pg_host_port() -> tuple[str, int]:
    try:
        from config import get_settings
        dsn = (get_settings().postgres_dsn or "")
        if dsn and "://" in dsn:
            netloc = dsn.split("://", 1)[1].split("/", 1)[0]
            hp = netloc.rsplit("@", 1)[-1]
            if ":" in hp:
                host, _, port = hp.rpartition(":")
                try:
                    return host, int(port)
                except ValueError:
                    pass
    except Exception:  # noqa: BLE001 - 配置缺失时回退缺省
        pass
    return os.environ.get("PGHOST", "127.0.0.1"), int(os.environ.get("PGPORT", "5432"))


def pg_available(timeout: float = 0.6) -> bool:
    host, port = _pg_host_port()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pg_skipif():
    import pytest

    host, port = _pg_host_port()
    return pytest.mark.skipif(
        not pg_available(),
        reason=f"PG 不可用（{host}:{port} 不可达），跳过 PG/K8s 收敛用例（GAP-21 自动跳过）")
