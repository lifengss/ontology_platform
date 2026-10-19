"""模板 schema（与 API-INTERFACE-DOC §11 对齐）。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from schemas.ontology import OntologyCreate


class TemplateCreate(BaseModel):
    name: str
    description: Optional[str] = None
    industry: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    model: OntologyCreate


class Template(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    industry: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    model: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
