"""Action 定义 schema（与 API-INTERFACE-DOC §6.1 对齐）。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


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
    dry_run_supported: bool = True
    side_effect: bool = True  # Action 恒为 True，与 OntologyFunction 的 False 区分
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ActionCreate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    target_connection: str
    invoke: ActionInvoke
    input_map: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str
    confirmation: str = "human_required"
    dry_run_supported: bool = True
    side_effect: bool = True


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


class ActionConfirmResult(BaseModel):
    executed: bool
    external_response: dict[str, Any] = Field(default_factory=dict)
    log_id: Optional[str] = None
