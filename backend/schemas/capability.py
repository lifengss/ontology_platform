"""能力层（V2-04 首片）API 请求模型（snake_case，对齐 schema 命名规范）。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class CapabilityCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    kind: Optional[str] = Field(default="derive", pattern="^(derive|assert|act)$")
    implementation: Optional[str] = Field(default="expression", pattern="^expression$")
    ontology_id: Optional[str] = Field(default=None, max_length=64)
    object_type: str = Field(..., min_length=1, max_length=64)
    expression: str = Field(..., min_length=1)
    output_attribute: Optional[str] = Field(default=None, max_length=64)


class CapabilityUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    ontology_id: Optional[str] = Field(default=None, max_length=64)
    object_type: Optional[str] = Field(default=None, min_length=1, max_length=64)
    expression: Optional[str] = Field(default=None, min_length=1)
    output_attribute: Optional[str] = Field(default=None, max_length=64)


class CapabilityExecute(BaseModel):
    instance_id: str = Field(..., min_length=1)
    channel: str = Field(default="api", pattern="^(ui|api|mcp)$")
    trigger_source: str = Field(default="manual", max_length=32)
    dry_run: bool = Field(default=False)
    # M1-E / TCE2E-01：联邦读结果（term_lookup 合并值）作为本次临时输入覆盖，不落库
    inputs: Optional[dict[str, Any]] = Field(default=None)


class CapabilityExecuteBatch(BaseModel):
    limit: int = Field(default=1000, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)
    instance_ids: Optional[list[str]] = Field(default=None, max_length=5000)
    channel: str = Field(default="api", pattern="^(ui|api|mcp)$")
    trigger_source: str = Field(default="batch", max_length=32)
    dry_run: bool = Field(default=False)
