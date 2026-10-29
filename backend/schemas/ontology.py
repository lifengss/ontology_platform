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
    enum: Optional[list[str | int | float]] = None  # 枚举白名单（值约束），生成 sh:in；项应与 data_type 匹配（integer/float 允许数值）


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
    business_definition: Optional[str] = None  # 业务语义定义（一句话说明对象代表什么、边界在哪）
    stable_key: Optional[str] = None            # 业务主键字段 id（禁止用内部自增 id）
    namespace: Optional[str] = None            # 部门/域命名空间；MVP 默认继承 project
    parent: Optional[str] = None
    attributes: list[ObjectAttribute] = Field(default_factory=list)
    relations: list[ObjectRelation] = Field(default_factory=list)


class OntologyObjectPatch(BaseModel):
    """对象就地更新请求体：id 由路径参数决定，字段可选。

    parent 语义（partial update）：
    - 不传：保持旧父级（由端点回填原值）
    - 显式传 null：清除父级（变回根对象）
    - 传对象 id：改挂新父级；端点随后整体跑业务层校验（父存在/继承环），失败 422
    """
    name: Optional[str] = None
    description: Optional[str] = None
    parent: Optional[str] = None
    attributes: Optional[list[ObjectAttribute]] = None
    relations: Optional[list[ObjectRelation]] = None


class OntologyConstraint(BaseModel):
    id: str
    name: str
    scope: Optional[str] = None
    rule: str
    params: dict[str, Any] = Field(default_factory=dict)


class OntologyFunction(BaseModel):
    """无副作用计算逻辑（Function）元模型预留。

    与 Action 的区别：function 只产生派生值，不触发外部写；side_effect 恒为 False。
    MVP 仅建模存储，V2 接入 function_engine 执行。
    """

    function_id: Optional[str] = None
    name: str
    expression: str
    inputs: list[str] = Field(default_factory=list)
    output_type: str = "string"
    side_effect: bool = False


class Ontology(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    modeling_type: str = ModelingType.MANUAL
    version: str = "1"
    objects: list[OntologyObject] = Field(default_factory=list)
    constraints: list[OntologyConstraint] = Field(default_factory=list)
    functions: list[OntologyFunction] = Field(default_factory=list)  # 元模型预留（O-02）
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
    functions: list[OntologyFunction] = Field(default_factory=list)


class OntologyFromTextRequest(BaseModel):
    text: str
    llm_endpoint: Optional[str] = None
    model: Optional[str] = None
    max_tokens: Optional[int] = Field(
        default=None, ge=1,
        description="LLM 生成最大 token 数；缺省用 LLM 设置中的 max_tokens（思考型模型勿设小值，reasoning 计入 completion_tokens）",
    )


class OntologyEnrichRequest(BaseModel):
    """本体增强（增量抽取）参数。"""

    instructions: Optional[str] = None
    llm_endpoint: Optional[str] = None
    model: Optional[str] = None


class OntologyEnrichSummary(BaseModel):
    added_objects: list[str] = Field(default_factory=list)
    added_attributes: list[str] = Field(default_factory=list)
    added_relations: list[str] = Field(default_factory=list)
    updated_objects: list[str] = Field(default_factory=list)


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
