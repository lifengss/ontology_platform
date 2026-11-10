"""极简 JSON 文件存储抽象（支持项目隔离）。

MVP 演示路径：无需 Postgres 即可跑通 VS0/VS1（建模闭环）。生产可替换为 Postgres/pgvector，
但对外返回结构不变（始终走 core.responses 信封）。

项目隔离目录：
    data_dir/projects/<project_id>/<collection>.json
共享库（MVP 只读/提示）：
    data_dir/shared/<collection>.json
未指定 project_id 时退回到默认项目 default。
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import uuid
from typing import Any

from config import get_settings


class JsonStore:
    """按集合名存一组记录（list[dict]）到 data_dir/projects/<project_id>/<collection>.json。

    锁按文件路径共享（而非每实例一把）：同一集合在并发请求中会创建多个 JsonStore
    实例，实例级 RLock 无法保护跨实例的 read-modify-write（V2-03 并发主键变更场景）。
    """

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    @classmethod
    def _path_lock(cls, path: pathlib.Path) -> threading.RLock:
        key = str(path)
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.RLock())

    def __init__(self, collection: str, project_id: str | None = None) -> None:
        self._settings = get_settings()
        self._project_id = project_id or "default"
        self._dir = self._project_dir(self._settings.data_dir, self._project_id)
        self._path = self._dir / f"{collection}.json"
        self._lock = self._path_lock(self._path)
        self._dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if not self._path.exists():
                self._write([])

    @staticmethod
    def _project_dir(base: str, project_id: str) -> pathlib.Path:
        if project_id == "_shared":
            return pathlib.Path(base) / "shared"
        return pathlib.Path(base) / "projects" / project_id

    def _read(self) -> list[dict[str, Any]]:
        with self._lock:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)

    def _write(self, records: list[dict[str, Any]]) -> None:
        with self._lock:
            # 每次写独立临时文件：并发/异常残留时不会互相踩同一 .tmp（Windows WinError 32）
            tmp = f"{self._path}.{uuid.uuid4().hex}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(records, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._path)
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass

    def all(self) -> list[dict[str, Any]]:
        return self._read()

    def get(self, record_id: str) -> dict[str, Any] | None:
        for r in self._read():
            if r.get("id") == record_id:
                return r
        return None

    def insert(self, record: dict[str, Any]) -> dict[str, Any]:
        records = self._read()
        record.setdefault("id", uuid.uuid4().hex)
        records.append(record)
        self._write(records)
        return record

    def update(self, record_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        records = self._read()
        for i, r in enumerate(records):
            if r.get("id") == record_id:
                r.update({k: v for k, v in patch.items() if k != "id"})
                records[i] = r
                self._write(records)
                return r
        return None

    def update_if_unchanged(self, record_id: str, expected_updated_at: Any,
                            patch: dict[str, Any]) -> dict[str, Any] | None:
        """乐观锁更新（V2-03 EP-03-02）：仅当记录 updated_at 与请求开始时一致才写入。

        read+compare+write 在同一把锁内完成，避免两个并发 PATCH 各自基于旧状态相互覆盖；
        返回 None 表示记录不存在或已被其它请求更新（由调用方转 409/404）。
        """
        with self._lock:
            records = self._read()
            for i, r in enumerate(records):
                if r.get("id") == record_id:
                    if r.get("updated_at") != expected_updated_at:
                        return None
                    r.update({k: v for k, v in patch.items() if k != "id"})
                    records[i] = r
                    self._write(records)
                    return r
        return None

    def delete(self, record_id: str) -> bool:
        records = self._read()
        new_records = [r for r in records if r.get("id") != record_id]
        if len(new_records) == len(records):
            return False
        self._write(new_records)
        return True

    def find(self, predicate) -> list[dict[str, Any]]:
        return [r for r in self._read() if predicate(r)]
