"""本体业务层模型 schema（请求/响应共用，与 API-INTERFACE-DOC §2.1 对齐）。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from schemas.common import Cardinality, DataType, ModelingType


class AttributeConstraint(BaseModel):
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    enum: Optional[list[str]] = None  # 枚举白名单（值约束），生成 sh:in


class ObjectAttribute(BaseModel):
    id: str
    name: str
    data_type: str = DataType.STRING
    required: bool = False
    constraints: AttributeConstraint = Field(default_factory=AttributeConstraint)


class ObjectRelation(BaseModel):
    id: str
    name: str
    target_object: str
    cardinality: str = Cardinality.MANY_TO_ONE
    inverse_name: Optional[str] = None


class OntologyObject(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    parent: Optional[str] = None
    attributes: list[ObjectAttribute] = Field(default_factory=list)
    relations: list[ObjectRelation] = Field(default_factory=list)


class OntologyConstraint(BaseModel):
    id: str
    name: str
    scope: Optional[str] = None
    rule: str
    params: dict[str, Any] = Field(default_factory=dict)


class Ontology(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    modeling_type: str = ModelingType.MANUAL
    version: str = "1"
    objects: list[OntologyObject] = Field(default_factory=list)
    constraints: list[OntologyConstraint] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OntologyCreate(BaseModel):
    """创建请求体（不含 id/时间戳）。"""

    name: str
    description: Optional[str] = None
    modeling_type: str = ModelingType.MANUAL
    version: str = "1"
    objects: list[OntologyObject] = Field(default_factory=list)
    constraints: list[OntologyConstraint] = Field(default_factory=list)


class OntologyFromTextRequest(BaseModel):
    text: str
    llm_endpoint: Optional[str] = None
    model: Optional[str] = None


class OntologyFromDocResponse(BaseModel):
    draft_id: str
    status: str  # pending | done | error
    error: Optional[str] = None


class OntologyPublishRequest(BaseModel):
    pass


class ValidateRequest(BaseModel):
    level: str = "full"
    sample_instances: list[dict[str, Any]] | None = None  # 可选：样本实例数据，用于真实 SHACL 校验


class ValidateResult(BaseModel):
    valid: bool
    violations: list[dict[str, Any]] = Field(default_factory=list)


class ExportRequest(BaseModel):
    format: str = "turtle"


class ExportResult(BaseModel):
    format: str
    content: str
    filename: str


class SnapshotMeta(BaseModel):
    snapshot_id: str
    ontology_id: str
    note: Optional[str] = None
    created_at: str


class SnapshotRequest(BaseModel):
    note: Optional[str] = None
