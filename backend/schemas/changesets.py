"""V2-07 变更审批流（M2-E）请求 schema。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from schemas.ontology import OntologyCreate


class ChangesetCreate(BaseModel):
    """提交变更提案：model 为提案后的完整模型（与 OntologyCreate 同构）。

    - 兼容变更：target_version 可缺省 → 自动升小版本并直接发布；
    - 不兼容变更：target_version 必填（主版本号须高于当前已发布版本）→ 进入审批；
    - supersedes：可选，被本提案取代的旧审批单 id（旧实例归档）。
    """

    model: OntologyCreate
    note: str = ""
    target_version: Optional[str] = None
    supersedes: Optional[str] = None


class AttributeDeprecate(BaseModel):
    """TC-07-05 先弃用后删除：标记属性 @deprecated（须给定宽限期终点）。"""

    object_id: str
    attribute_id: str
    until: str = Field(description="宽限期终点（ISO 日期/时间，须晚于当前）")
    note: str = ""


class RejectBody(BaseModel):
    reason: str = Field(min_length=1, description="驳回原因（必填，TC-07-02）")


class VersionQuery(BaseModel):
    pass
