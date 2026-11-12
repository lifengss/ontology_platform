"""V2-14 Action 编排（M3-F）——编排工作流 / 运行 / 触发 / 生成相关 Pydantic 模型。

no-code 红线（INV-3）：
- 节点仅允许控制类型 + Action 引用；任何 code/script/表达式字段一律拒绝。
- 参数值仅三种来源：literal（字面量）/ field_ref（上游输出字段引用）/ trigger_ctx（触发上下文）。
- 分支条件仅受限算子（eq/ne/gt/ge/lt/le/contains/in_set/is_empty/is_not_empty）+ AND/OR。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

NodeKind = Literal["start", "end", "action", "branch", "confirm", "capability_gap"]
ParamValueType = Literal["literal", "field_ref", "trigger_ctx"]
FailurePolicy = Literal["retry", "skip", "abort"]
TriggerType = Literal["manual", "event", "cron"]
WorkflowStatus = Literal["draft", "published", "archived"]

#: 分支条件受限算子白名单（INV-3）
ALLOWED_OPS = {"eq", "ne", "gt", "ge", "lt", "le", "contains", "in_set", "is_empty", "is_not_empty"}
#: 参数取值来源白名单（INV-3）
ALLOWED_VALUE_TYPES = {"literal", "field_ref", "trigger_ctx"}
#: 节点 kind 白名单（INV-3）
ALLOWED_NODE_KINDS = {"start", "end", "action", "branch", "confirm", "capability_gap"}
#: 疑似代码/脚本内容特征（出现即拒绝，INV-3）
CODE_PATTERN_TOKENS = (
    "def ", "import ", "eval(", "exec(", "<script", "os.system", "subprocess",
    "function (", "=> ", "; rm ", "powershell", "$(", "python -c",
)


class WorkflowTrigger(BaseModel):
    """触发器：manual（手动）/ event（事件 topic）/ cron（5 段 cron 表达式，可视化配置）。"""

    type: TriggerType = "manual"
    event_topic: Optional[str] = None
    cron_expr: Optional[str] = None
    #: event 触发要求 payload 必须携带的上下文字段（点分路径）；缺失 → 拒绝启动（不半启动）
    required_ctx: list[str] = Field(default_factory=list)
    #: cron 触发的静态 payload
    static_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_topic")
    @classmethod
    def _event_topic(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("event_topic 不能为空")
        return v


class WorkflowNode(BaseModel):
    """编排节点。action 节点引用已发布 Action（风险分级继承自 Action 层 V2-13）。"""

    id: str
    kind: NodeKind
    label: str = ""
    # --- action 节点 ---
    action_id: Optional[str] = None
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    failure_policy: FailurePolicy = "retry"
    retry_max: int = 2
    # --- branch 节点 ---
    # port_id -> {"condition": {"logic": "AND"|"OR", "rules": [{"field","op","value"}]} | None,
    #            "is_default": bool, "label": str}
    branches: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # --- confirm 节点 ---
    confirm_title: str = ""
    # --- capability_gap 节点（SOP/AI 生成未 grounded 步骤；存在即阻断发布）---
    gap_reason: str = ""
    # --- 生成 grounding 记录（逐节点：依据 + 置信度）---
    grounding: Optional[dict[str, Any]] = None
    origin: str = "manual"  # manual | sop | skill | ai_fragment

    @field_validator("kind")
    @classmethod
    def _kind_allowed(cls, v: str) -> str:
        if v not in ALLOWED_NODE_KINDS:
            raise ValueError(f"不支持的节点类型「{v}」：仅允许 {sorted(ALLOWED_NODE_KINDS)}（no-code 红线，代码/脚本节点被拒绝）")
        return v

    @field_validator("failure_policy")
    @classmethod
    def _policy_allowed(cls, v: str) -> str:
        if v not in {"retry", "skip", "abort"}:
            raise ValueError(f"失败策略「{v}」不合法：仅允许 retry/skip/abort")
        return v

    @field_validator("retry_max")
    @classmethod
    def _retry_max(cls, v: int) -> int:
        if v < 1 or v > 10:
            raise ValueError("retry_max 必须在 1~10 之间")
        return v


class WorkflowEdge(BaseModel):
    id: str
    from_: str = Field(alias="from")
    to: str
    from_port: Optional[str] = None  # branch 节点输出端口

    model_config = {"populate_by_name": True}


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    trigger: WorkflowTrigger = Field(default_factory=WorkflowTrigger)


class WorkflowUpdate(WorkflowCreate):
    pass


class DryRunRequest(BaseModel):
    """试运行：不产生外部调用，逐节点输入/输出预览；风险节点展示待确认状态。"""

    payload: dict[str, Any] = Field(default_factory=dict)
    #: 供分支条件/下游引用预览的节点输出样例（node_id -> output dict）
    sample_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)


class RunRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    trigger_source: str = "manual"  # manual | event | cron


class EventDispatchRequest(BaseModel):
    topic: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "event"


class ConfirmRunNodeRequest(BaseModel):
    node_id: str
    reason: str = ""


class GenerateDraftRequest(BaseModel):
    """SOP/skill 文本 → 编排草稿（不落地发布；未 grounded 步骤标 capability_gap）。"""

    text: str = Field(min_length=1)
    source_type: Literal["sop", "skill"] = "sop"
    name: str = ""


class GenerateFragmentRequest(BaseModel):
    """AI 局部补全：选定节点/分支 + 自然语言描述 → 结构化流程片段（草稿通道）。"""

    intent: str = Field(min_length=1)
    #: 锚点：after_node（在该节点之后插入）或 {branch_node, port}
    after_node: Optional[str] = None
    branch_node: Optional[str] = None
    port: Optional[str] = None


__all__ = [
    "ALLOWED_NODE_KINDS", "ALLOWED_OPS", "ALLOWED_VALUE_TYPES", "CODE_PATTERN_TOKENS",
    "ConfirmRunNodeRequest", "DryRunRequest", "EventDispatchRequest",
    "GenerateDraftRequest", "GenerateFragmentRequest", "RunRequest",
    "WorkflowCreate", "WorkflowEdge", "WorkflowNode", "WorkflowTrigger", "WorkflowUpdate",
]
