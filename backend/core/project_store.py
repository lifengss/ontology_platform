"""项目元数据存储。

项目隔离数据目录结构：
    data_dir/projects/<project_id>/ontologies.json
    data_dir/projects/<project_id>/ingest_candidates.json
    data_dir/projects/<project_id>/actions.json
    data_dir/projects/<project_id>/mcp_connections.json
    data_dir/projects/<project_id>/instances.jsonl
共享库（MVP 仅提示，只读）：
    data_dir/shared/ontologies.json
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from typing import Any

from core.storage import JsonStore
from schemas.project import Project, ProjectCreate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectStore:
    """管理项目元数据，并提供按项目的数据集合访问。"""

    def __init__(self) -> None:
        self._meta = JsonStore("projects")  # 项目列表存在 data_dir/projects.json

    def create(self, payload: ProjectCreate) -> dict[str, Any]:
        rec = payload.model_dump()
        rec["id"] = Project.generate_id()
        rec["created_at"] = _now()
        rec["updated_at"] = rec["created_at"]
        self._meta.insert(rec)
        # 初始化项目数据目录（空集合）
        JsonStore("ontologies", project_id=rec["id"])
        JsonStore("ingest_candidates", project_id=rec["id"])
        JsonStore("actions", project_id=rec["id"])
        JsonStore("mcp_connections", project_id=rec["id"])
        return rec

    def list(self) -> list[dict[str, Any]]:
        return self._meta.all()

    def get(self, project_id: str) -> dict[str, Any] | None:
        return self._meta.get(project_id)

    def update(self, project_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        patch.pop("id", None)
        patch["updated_at"] = _now()
        return self._meta.update(project_id, patch)

    def delete(self, project_id: str) -> bool:
        if project_id in ("default", "_shared"):
            return False
        # 级联删除项目数据目录
        import config
        base = config.get_settings().data_dir
        path = JsonStore._project_dir(base, project_id)
        if path.exists():
            shutil.rmtree(path)
        return self._meta.delete(project_id)

    def collection(self, name: str, project_id: str | None = None) -> JsonStore:
        return JsonStore(name, project_id=project_id or "default")

    def shared_collection(self, name: str) -> JsonStore:
        return JsonStore(name, project_id="_shared")
