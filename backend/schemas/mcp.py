"""MCP 连接配置 schema（与 API-INTERFACE-DOC §5.1 对齐；凭据仅存引用）。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ConnectionAuth(BaseModel):
    type: str  # bearer_token | api_key | oauth2_client_credentials
    token_url: Optional[str] = None
    client_id_ref: Optional[str] = None   # ${ENV_NAME}
    client_secret_ref: Optional[str] = None
    api_key_ref: Optional[str] = None
    bearer_token_ref: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)


class MapsToOntology(BaseModel):
    ontology_id: Optional[str] = None   # 目标本体 ID（read 端点必填）
    read_tool: Optional[str] = None     # 缺省读取工具名（read 端点未显式传 tool 时使用）
    object_type: str
    field_map: dict[str, str] = Field(default_factory=dict)


class ConnectionConfig(BaseModel):
    connection_id: Optional[str] = None
    display_name: str
    transport: str  # stdio | streamable_http
    endpoint: Optional[str] = None
    auth: ConnectionAuth
    tool_allowlist: list[str] = Field(default_factory=list)
    maps_to_ontology: MapsToOntology
    read_schedule: str = "manual"
    # V2-01：field_map 来源收敛（explicit=连接自带；dictionary=按数据字典生成，入库/查询同一数据源）
    field_map_source: Literal["explicit", "dictionary"] = "explicit"
    # V2-01：成本护栏——外部工具取数模式（paged=支持 limit 下推；full=仅全量拉取，须声明 fetch_limit）
    fetch_mode: Literal["paged", "full"] = "paged"
    fetch_limit: int = Field(0, ge=0, le=1_000_000)  # full 模式声明上限（0=未声明）
    last_latency_ms: int = Field(0, ge=0)  # 实测时延（联邦查询回写，供成本估算）
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ConnectionCreate(BaseModel):
    connection_id: Optional[str] = None  # 可选自定义 ID；缺省自动生成
    display_name: str
    transport: str
    endpoint: Optional[str] = None
    auth: ConnectionAuth
    tool_allowlist: list[str] = Field(default_factory=list)
    maps_to_ontology: MapsToOntology
    read_schedule: str = "manual"
    field_map_source: Literal["explicit", "dictionary"] = "explicit"
    fetch_mode: Literal["paged", "full"] = "paged"
    fetch_limit: int = Field(0, ge=0, le=1_000_000)


class ConnectionTestResult(BaseModel):
    reachable: bool
    auth_ok: bool
    latency_ms: Optional[int] = None
    error: Optional[str] = None


class ConnectionReadResult(BaseModel):
    candidate_id: Optional[str] = None
    record_count: int = 0
