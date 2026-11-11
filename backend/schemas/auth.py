"""V2-05 登录鉴权 / RBAC 请求模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class LoginReq(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class GrantIn(BaseModel):
    scope: Literal["project", "ontology"]
    target: str = Field(min_length=1, max_length=128)
    level: Literal["read", "write", "review", "none"]


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=256)
    role: Literal["admin", "modeler", "reviewer", "viewer"] = "viewer"
    grants: list[GrantIn] = Field(default_factory=list)


class UserPatch(BaseModel):
    """只传需要变更的字段；grants 传 [] 表示清空该用户的资源级授权。"""
    role: Optional[Literal["admin", "modeler", "reviewer", "viewer"]] = None
    grants: Optional[list[GrantIn]] = None
    disabled: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=256)
