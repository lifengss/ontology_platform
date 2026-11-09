"""V2-01 数据字典 schema（M1-A）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TermCreate(BaseModel):
    canonical: str = Field(min_length=1, max_length=128)
    object_type: str = ""
    property_id: str = ""
    synonyms: list[str] = Field(default_factory=list)
    system_bindings: dict[str, str] = Field(default_factory=dict)  # {connection_id: 系统字段名}
    authoritative_source: str | None = None  # connection_id
    normalization: dict = Field(default_factory=dict)


class TermUpdate(BaseModel):
    canonical: str | None = Field(default=None, min_length=1, max_length=128)
    object_type: str | None = None
    property_id: str | None = None
    synonyms: list[str] | None = None
    system_bindings: dict[str, str] | None = None
    authoritative_source: str | None = None
    normalization: dict | None = None


class SynonymAdd(BaseModel):
    synonym: str = Field(min_length=1, max_length=128)


class TermLookupReq(BaseModel):
    term: str = Field(min_length=1, max_length=256)
    value: str = ""
    systems: str = ""  # connection_id/display_name 逗号分隔，空=全部
    limit: int = Field(50, ge=1, le=500)
    dry_run: bool = False
    confirm_cost: bool = False


class ImportFromConnectionsReq(BaseModel):
    connection_ids: list[str] | None = None
    object_type: str | None = None
