"""V2-05 登录鉴权 / RBAC 请求模型。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class LoginReq(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordReq(BaseModel):
    """用户修改本人密码（须验证当前密码；新密码至少 8 位且不得与原密码相同）。"""
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class GrantIn(BaseModel):
    scope: Literal["project", "ontology"]
    target: str = Field(min_length=1, max_length=128)
    level: Literal["read", "write", "review", "none"]


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    # 可选：不传则使用系统默认初始密码（用户首次登录强制改密）
    password: Optional[str] = Field(default=None, min_length=8, max_length=256)
    role: Literal["admin", "modeler", "reviewer", "viewer"] = "viewer"
    grants: list[GrantIn] = Field(default_factory=list)
    tenant_id: Optional[str] = None  # V2-06：租户绑定（缺省 default）
    department: Optional[str] = None  # 部门（独立于租户，仅展示/归属标签，缺省空串）


class UserPatch(BaseModel):
    """只传需要变更的字段；grants 传 [] 表示清空该用户的资源级授权。"""
    role: Optional[Literal["admin", "modeler", "reviewer", "viewer"]] = None
    grants: Optional[list[GrantIn]] = None
    disabled: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=256)
    tenant_id: Optional[str] = None  # V2-06：调整用户租户归属
    department: Optional[str] = None  # 部门（独立于租户）
