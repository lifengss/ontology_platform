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
    """按集合名存一组记录（list[dict]）到 data_dir/projects/<project_id>/<collection>.json。"""

    def __init__(self, collection: str, project_id: str | None = None) -> None:
        self._settings = get_settings()
        self._project_id = project_id or "default"
        self._dir = self._project_dir(self._settings.data_dir, self._project_id)
        self._path = self._dir / f"{collection}.json"
        self._lock = threading.RLock()
        self._dir.mkdir(parents=True, exist_ok=True)
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
            tmp = str(self._path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)

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

    def delete(self, record_id: str) -> bool:
        records = self._read()
        new_records = [r for r in records if r.get("id") != record_id]
        if len(new_records) == len(records):
            return False
        self._write(new_records)
        return True

    def find(self, predicate) -> list[dict[str, Any]]:
        return [r for r in self._read() if predicate(r)]
