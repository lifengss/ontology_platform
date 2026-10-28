"""项目元数据存储。

项目隔离数据目录结构：
    data_dir/projects/<project_id>/ontologies.json
    data_dir/projects/<project_id>/ingest_candidates.json
    data_dir/projects/<project_id>/actions.json
    data_dir/projects/<project_id>/mcp_connections.json
    data_dir/projects/<project_id>/instances/<object_id>.jsonl
共享库（MVP 仅提示，只读）：
    data_dir/shared/ontologies.json
归档（archive）：
    data_dir/archives/<project_id>.zip —— 项目重数据整目录压缩；
    项目 meta 保留 status=archived + archive_info（本体索引/计数/大小），即「只保留索引」。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import zipfile
from datetime import datetime, timezone
from typing import Any

from core.storage import JsonStore
from logging_setup import get_logger
from schemas.project import Project, ProjectCreate

logger = get_logger("core.project_store")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectStore:
    """管理项目元数据，并提供按项目的数据集合访问。"""

    def __init__(self) -> None:
        self._meta = JsonStore("projects")  # 项目列表存在 data_dir/projects.json

    def create(self, payload: ProjectCreate) -> dict[str, Any]:
        rec = payload.model_dump()
        rec["id"] = Project.generate_id()
        rec["department"] = (rec.get("department") or "默认部门").strip() or "默认部门"
        rec["created_at"] = _now()
        rec["updated_at"] = rec["created_at"]
        self._meta.insert(rec)
        # 初始化项目数据目录（空集合）
        JsonStore("ontologies", project_id=rec["id"])
        JsonStore("ingest_candidates", project_id=rec["id"])
        JsonStore("actions", project_id=rec["id"])
        JsonStore("mcp_connections", project_id=rec["id"])
        JsonStore("namespaces", project_id=rec["id"])
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
        # 级联删除项目数据目录（含归档包，若已归档）
        import config
        base = config.get_settings().data_dir
        path = JsonStore._project_dir(base, project_id)
        if path.exists():
            shutil.rmtree(path)
        archive = self.archive_path(project_id)
        if archive.exists():
            archive.unlink()
        return self._meta.delete(project_id)

    # ------------------------------------------------------------------
    # 归档 / 恢复 / 统计
    # ------------------------------------------------------------------

    @staticmethod
    def archive_path(project_id: str) -> "pathlib.Path":
        import pathlib
        import config
        return pathlib.Path(config.get_settings().data_dir) / "archives" / f"{project_id}.zip"

    @staticmethod
    def _ontology_index(project_id: str) -> list[dict[str, Any]]:
        """归档时保留的轻量本体索引（不含定义体），供归档状态下浏览。"""
        index = []
        for rec in JsonStore("ontologies", project_id=project_id).all():
            index.append({
                "id": rec.get("id"),
                "name": rec.get("name"),
                "status": rec.get("status"),
                "version": rec.get("version"),
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
            })
        return index

    @staticmethod
    def _dir_size(path: "pathlib.Path") -> int:
        total = 0
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
        return total

    def archive(self, project_id: str) -> dict[str, Any]:
        """归档项目：重数据整目录压缩到 archives/<id>.zip，meta 仅保留索引。

        顺序保证安全：先写归档包并校验可读，再删原始目录，最后更新 meta。
        """
        import config
        import pathlib
        base = config.get_settings().data_dir
        path = JsonStore._project_dir(base, project_id)
        if not path.exists():
            raise FileNotFoundError("项目数据目录不存在")

        ontology_index = self._ontology_index(project_id)
        original_size = self._dir_size(path)
        try:
            from core.instance_store import get_instance_store
            instance_count = get_instance_store(project_id).count()
        except Exception:  # noqa: BLE001 —— 统计失败不阻塞归档
            instance_count = 0

        archive = self.archive_path(project_id)
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            archive.unlink()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(path.rglob("*")):
                if p.is_file():
                    zf.write(p, arcname=str(p.relative_to(path)))
        # 校验归档包完整性，通过后才允许删除原始数据
        with zipfile.ZipFile(archive, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f"归档包校验失败: {bad}")

        shutil.rmtree(path)

        info = {
            "path": str(archive),
            "archived_at": _now(),
            "original_size": original_size,
            "archive_size": archive.stat().st_size,
            "ontology_count": len(ontology_index),
            "instance_count": instance_count,
            "ontology_index": ontology_index,
        }
        updated = self.update(project_id, {
            "status": "archived",
            "archive_info": info,
        })
        logger.info("archive_project id=%s ontologies=%d instances=%d %d -> %d bytes",
                    project_id, len(ontology_index), instance_count, original_size, info["archive_size"])
        return {"project": updated, "archive_info": info}

    def restore(self, project_id: str) -> dict[str, Any]:
        """恢复归档项目：解压归档包回原目录，status 置回 active。

        解压目标目录若仍存在（理论上归档后不应存在）先清理，避免新旧混杂。
        """
        import config
        base = config.get_settings().data_dir
        archive = self.archive_path(project_id)
        if not archive.exists():
            raise FileNotFoundError("归档包不存在，无法恢复")
        target = JsonStore._project_dir(base, project_id)
        with zipfile.ZipFile(archive, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f"归档包损坏: {bad}")
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
            for member in zf.namelist():
                # 防 zip-slip：成员路径必须落在目标目录内
                dest = (target / member).resolve()
                if not str(dest).startswith(str(target.resolve()) + os.sep) and dest != target.resolve():
                    raise RuntimeError(f"归档包含非法路径: {member}")
                zf.extract(member, target)
        # 恢复成功：删除归档包（避免双份占用），meta 回到 active
        archive.unlink()
        updated = self.update(project_id, {"status": "active", "archive_info": None})
        logger.info("restore_project id=%s from %s", project_id, archive)
        return {"project": updated}

    def stats(self, project_id: str) -> dict[str, Any]:
        """项目级统计（首页总览用）。归档项目直接读 meta 索引，不解压。"""
        import config
        import pathlib
        base = config.get_settings().data_dir
        path = JsonStore._project_dir(base, project_id)
        rec = self.get(project_id) or {}
        if rec.get("status") == "archived":
            info = rec.get("archive_info") or {}
            return {
                "ontology_count": info.get("ontology_count", 0),
                "instance_count": info.get("instance_count", 0),
                "data_size": info.get("archive_size", 0),
                "size_kind": "archive",
            }
        if not path.exists():
            return {"ontology_count": 0, "instance_count": 0, "data_size": 0, "size_kind": "dir"}
        try:
            ontologies = JsonStore("ontologies", project_id=project_id).all()
        except Exception:  # noqa: BLE001
            ontologies = []
        try:
            from core.instance_store import get_instance_store
            instance_count = get_instance_store(project_id).count()
        except Exception:  # noqa: BLE001
            instance_count = 0
        return {
            "ontology_count": len(ontologies),
            "instance_count": instance_count,
            "data_size": self._dir_size(path),
            "size_kind": "dir",
        }

    def collection(self, name: str, project_id: str | None = None) -> JsonStore:
        return JsonStore(name, project_id=project_id or "default")

    def shared_collection(self, name: str) -> JsonStore:
        return JsonStore(name, project_id="_shared")
