"""Postgres 实例存储实现（V2-20 / M4-H：多副本共享一致收敛）。

背景：
    JsonlInstanceStore 用进程内 threading.RLock + 本地文件，K8s 多副本下各自写本地盘
    会数据分叉，挂共享卷又无跨进程锁（update/update_many/delete 的「读全文件→整文件写回」
    会互相覆盖）。本实现把实例记录收敛进 Postgres：以数据库事务保证 read-modify-write
    原子性，多副本并发安全，满足 TC-20-03「已迁移共享持久存储/收敛 PG；无副本间文件分叉」。

设计：
    - 实现同样的 InstanceStore 抽象接口，接口语义与 JsonlInstanceStore 一致
      （insert 自动补 id/project_id/created_at；get/find/update/update_many/delete/count/all）。
    - 每行记录存 JSONB（rec_json），保持「整条 record 字典」的兼容语义，
      与 jsonl 逐行 JSON 完全一致；project_id 单独列便于多项目/多副本检索与聚合。
    - 事务：update/update_many/delete 用 BEGIN → SELECT ... FOR UPDATE → 改 → UPDATE → COMMIT，
      在 PG 侧加行锁，避免两个副本并发基于旧值互相覆盖。
    - 排序：jsonl 按 object_type 分文件且为插入追加序；PG 无自然插入序，
      用 (created_at, id) 近似稳定序，保证 find(limit=None)/all() 顺序可预期。
    - 连接：每操作短连接（psycopg2.connect），连接串复用 settings.postgres_dsn；
      表自动创建（幂等 CREATE TABLE IF NOT EXISTS）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg2
import psycopg2.extras
from psycopg2 import sql

from config import get_settings
from core.instance_store import InstanceStore
from logging_setup import get_logger

logger = get_logger("core.pg_instance_store")

_DDL = """
CREATE TABLE IF NOT EXISTS instance_records (
    project_id  TEXT        NOT NULL,
    object_type TEXT        NOT NULL,
    id          TEXT        NOT NULL,
    rec_json    JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, id)
);
CREATE INDEX IF NOT EXISTS idx_instance_records_project_obj
    ON instance_records (project_id, object_type);
CREATE INDEX IF NOT EXISTS idx_instance_records_project_id
    ON instance_records (project_id, id);
"""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class PgInstanceStore(InstanceStore):
    """Postgres 实现：整条记录存 JSONB，事务保证多副本原子读写。"""

    def __init__(self, project_id: str | None = None, dsn: str | None = None) -> None:
        settings = get_settings()
        self._project_id = project_id or "default"
        self._dsn = dsn or settings.postgres_dsn
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(_DDL)
                conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("PgInstanceStore 建表失败（连接不可用/权限）: %s", e)
            # 不阻断：后续操作重连时仍会尝试，若 PG 不可达则逐操作上抛，
            # 由调用方（health/caller）决定是否降级。

    def _connect(self):
        return psycopg2.connect(self._dsn)

    # ------------------------------------------------------------------ #
    # insert
    # ------------------------------------------------------------------ #
    def insert(self, record: dict[str, Any]) -> dict[str, Any]:
        record = dict(record)
        record.setdefault("id", uuid.uuid4().hex)
        record.setdefault("project_id", self._project_id)
        record.setdefault("created_at", _now())
        object_type = record.get("object_type")
        if not object_type:
            raise ValueError("实例记录必须包含 object_type")
        project_id = record.get("project_id") or self._project_id
        ts = _now()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO instance_records "
                        "(project_id, object_type, id, rec_json, created_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (project_id, id) DO NOTHING"
                    ),
                    (project_id, object_type, record["id"],
                     psycopg2.extras.Json(record), ts, ts),
                )
            conn.commit()
        return record

    # ------------------------------------------------------------------ #
    # get / find
    # ------------------------------------------------------------------ #
    def get(self, record_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rec_json FROM instance_records "
                    "WHERE project_id = %s AND id = %s",
                    (self._project_id, record_id),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return row[0]

    def find(
        self,
        object_type: str | None = None,
        filters: dict[str, Any] | None = None,
        limit: int | None = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters = filters or {}
        where = ["project_id = %s"]
        params: list[Any] = [self._project_id]
        if object_type:
            where.append("object_type = %s")
            params.append(object_type)
        # 记录级字段过滤（rec_json 内字段）：在 SQL 层用 JSONB 匹配
        for k, v in filters.items():
            where.append("rec_json->>%s = %s")
            params.extend([k, self._norm(v)])
        # 拼接参数（params 顺序必须与 SQL 占位符一致），分页用 psycopg2 参数化
        limit = int(limit) if limit is not None else None
        offset = int(offset or 0)
        sql_text = (
            "SELECT rec_json FROM instance_records WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at, id"
        )
        if limit is not None:
            sql_text += " LIMIT %s OFFSET %s"
            params.extend([limit, offset])
        else:
            sql_text += " OFFSET %s"
            params.append(offset)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL(sql_text), params)
                rows = cur.fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def _norm(v: Any) -> Any:
        # 统一为字符串比较，兼容 jsonl find 的 == 语义（布尔/数字转为字符串）
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return v

    # ------------------------------------------------------------------ #
    # update / update_many（事务 + 行锁）
    # ------------------------------------------------------------------ #
    def update(self, record_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT rec_json FROM instance_records "
                        "WHERE project_id = %s AND id = %s FOR UPDATE",
                        (self._project_id, record_id),
                    )
                    row = cur.fetchone()
                    if row is None:
                        conn.rollback()
                        return None
                    rec = dict(row[0])
                    rec.update({k: v for k, v in patch.items() if k != "id"})
                    rec["updated_at"] = _now()
                    cur.execute(
                        "UPDATE instance_records SET rec_json = %s, updated_at = %s "
                        "WHERE project_id = %s AND id = %s",
                        (psycopg2.extras.Json(rec), rec["updated_at"],
                         self._project_id, record_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return rec

    def update_many(self, object_type: str, patches: dict[str, dict[str, Any]]) -> int:
        if not patches:
            return 0
        object_type = str(object_type)
        updated = 0
        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, rec_json FROM instance_records "
                        "WHERE project_id = %s AND object_type = %s FOR UPDATE",
                        (self._project_id, object_type),
                    )
                    for rid, rec in cur.fetchall():
                        patch = patches.get(rid)
                        if patch is None:
                            continue
                        rec = dict(rec)
                        rec.update({k: v for k, v in patch.items() if k != "id"})
                        rec["updated_at"] = _now()
                        cur.execute(
                            "UPDATE instance_records SET rec_json = %s, updated_at = %s "
                            "WHERE project_id = %s AND id = %s",
                            (psycopg2.extras.Json(rec), rec["updated_at"],
                             self._project_id, rid),
                        )
                        updated += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return updated

    # ------------------------------------------------------------------ #
    # delete / count / all
    # ------------------------------------------------------------------ #
    def delete(self, record_id: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM instance_records WHERE project_id = %s AND id = %s",
                    (self._project_id, record_id),
                )
                deleted = cur.rowcount
            conn.commit()
        return deleted > 0

    def count(self, object_type: str | None = None) -> int:
        where = ["project_id = %s"]
        params: list[Any] = [self._project_id]
        if object_type:
            where.append("object_type = %s")
            params.append(object_type)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM instance_records WHERE " + " AND ".join(where),
                    params,
                )
                return int(cur.fetchone()[0])

    def all(self) -> list[dict[str, Any]]:
        return self.find(limit=None)
