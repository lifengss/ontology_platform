"""项目模型 schema。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Project(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    status: str = "active"  # active | archived（归档：仅保留索引，重数据压缩存放）
    archive_info: Optional[dict] = None  # 归档元信息（路径/大小/本体索引/计数）
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @staticmethod
    def generate_id() -> str:
        import uuid
        return uuid.uuid4().hex


class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
