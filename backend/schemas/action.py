"""Action 定义 schema（与 API-INTERFACE-DOC §6.1 / §29 对齐）。

V2-13（M3-E）：新增 risk_level 回写风险分级（low/medium/high，默认 high 最保守）。
- low：dry-run/映射校验门禁通过即自动回写（无人工介入）；
- medium：AI 三条件评估（无预期外外部影响/原子性/完整性）通过才自动执行，
  评估失败/超时/无法判定 → 升级人工确认（失败≠通过）；
- high：必须人工确认（签核标识=身份+时间戳）。
风险等级调整仅管理员可操作（PUT /api/actions/{id}/risk-level），普通 update 拦截。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

RISK_LEVELS = ("low", "medium", "high")


class ActionInvoke(BaseModel):
    kind: str  # mcp_tool | webhook | rest
    tool: Optional[str] = None
    url: Optional[str] = None
    method: Optional[str] = None


class ActionDefinition(BaseModel):
    action_id: Optional[str] = None
    name: Optional[str] = None  # 业务可读名称
    description: Optional[str] = None  # 一句话说明
    category: Optional[str] = None  # 业务分类，如 数据同步/审批触发/通知推送
    target_connection: str
    invoke: ActionInvoke
    input_map: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str
    confirmation: str = "human_required"
    risk_level: str = "high"  # V2-13：low/medium/high（默认 high=人工确认，最保守）
    dry_run_supported: bool = True
    side_effect: bool = True  # Action 恒为 True，与 OntologyFunction 的 False 区分
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @field_validator("risk_level")
    @classmethod
    def _check_risk_level(cls, v: str) -> str:
        if v not in RISK_LEVELS:
            raise ValueError(f"risk_level 必须是 {RISK_LEVELS} 之一")
        return v


class ActionCreate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    target_connection: str
    invoke: ActionInvoke
    input_map: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str
    confirmation: str = "human_required"
    risk_level: str = "high"  # V2-13：创建时可指定（后续调整仅管理员）
    dry_run_supported: bool = True
    side_effect: bool = True

    @field_validator("risk_level")
    @classmethod
    def _check_risk_level(cls, v: str) -> str:
        if v not in RISK_LEVELS:
            raise ValueError(f"risk_level 必须是 {RISK_LEVELS} 之一")
        return v


class ActionExecuteRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False


class ActionExecuteResult(BaseModel):
    confirmation_token: Optional[str] = None
    dry_run: bool = False
    planned_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = None


class ActionConfirmRequest(BaseModel):
    confirmation_token: str


class ActionRejectRequest(BaseModel):
    """V2-13：拒绝待确认回写（EP-13-03：取消原因与操作者留痕）。"""
    confirmation_token: str
    reason: str = ""


class ActionRiskLevelUpdate(BaseModel):
    """V2-13：风险等级调整（仅平台管理员；防降级绕过，TP-13-04）。"""
    risk_level: str
    reason: str = ""


class ActionConfirmResult(BaseModel):
    executed: bool
    external_response: dict[str, Any] = Field(default_factory=dict)
    log_id: Optional[str] = None
