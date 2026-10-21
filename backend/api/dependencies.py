"""通用 FastAPI 依赖：项目隔离参数解析。"""
from __future__ import annotations

from fastapi import Query


def project_id_param(project_id: str | None = Query(default="default")) -> str:
    """从 query 读取 project_id，默认 default 项目。"""
    return project_id or "default"


def optional_project_id_param(project_id: str | None = Query(default=None)) -> str:
    return project_id or "default"


def namespace_param(namespace: str | None = Query(default=None)) -> str | None:
    """部门空间/命名空间；若未显式指定则继承当前 project_id。"""
    return namespace
