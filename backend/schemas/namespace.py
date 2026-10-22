"""部门空间（namespace）schemas。

空间是项目内的逻辑分组：本体按 namespace 字段归属空间，
默认空间 id 恒等于 project_id。空间 ID 创建后不可变（仅可改显示名）。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class NamespaceCreate(BaseModel):
    id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="可选自定义空间 ID（^[A-Za-z][A-Za-z0-9_-]{0,63}$）；缺省自动生成 ns_xxxxxxxx，创建后不可变",
    )
    name: str = Field(..., min_length=1, max_length=64, description="空间显示名称")
    description: Optional[str] = Field(default=None, max_length=255, description="空间说明")


class NamespaceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=255)
