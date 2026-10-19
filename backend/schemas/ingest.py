"""归化候选 schema（与 API-INTERFACE-DOC §3.1 对齐）。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class IngestSource(BaseModel):
    kind: str  # llm | field_map | external_mcp
    text: Optional[str] = None
    llm_endpoint: Optional[str] = None
    model: Optional[str] = None
    records: Optional[list[dict[str, Any]]] = None
    connection_id: Optional[str] = None
    tool: Optional[str] = None


class IngestPreviewRequest(BaseModel):
    ontology_id: str
    object_type: Optional[str] = None  # 目标对象类型（field_map 必填；llm 可选）
    source: IngestSource
    mapping: Optional[dict[str, Any]] = None


class Candidate(BaseModel):
    id: Optional[str] = None
    ontology_id: str
    source_type: str  # llm | field_map | external_mcp
    source_ref: Optional[str] = None
    object_type: str
    instances: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "pending"  # pending | approved | rejected | imported
    created_at: Optional[str] = None


class ApproveResult(BaseModel):
    imported: int
    rejected: int
    violations: list[dict[str, Any]] = Field(default_factory=list)
    neo_tx_id: Optional[str] = None
