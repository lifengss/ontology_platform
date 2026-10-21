"""项目模型 schema。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Project(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
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
