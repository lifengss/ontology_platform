"""通用 FastAPI 依赖：项目隔离参数解析 + V2-06 租户边界强制（M2-C）。

project_id_param 是全站项目级资源的唯一入口依赖：在此统一执行租户隔离
（TC-06-01：跨租户访问项目级资源一律 404）。鉴权关闭（测试态豁免）时放行。
"""
from __future__ import annotations

from fastapi import Query, Request

from tenant import ensure_project_access


def project_id_param(request: Request, project_id: str | None = Query(default="default")) -> str:
    """从 query 读取 project_id，默认 default 项目。

    V2-06：本体/实例为资源级共享判定（tenant.schema_visible），不在依赖层一刀切；
    连接/字典/候选等无共享语义的资源由各路由显式调用 tenant.ensure_project_access。
    """
    return project_id or "default"


def optional_project_id_param(request: Request, project_id: str | None = Query(default=None)) -> str:
    return project_id or "default"


def namespace_param(namespace: str | None = Query(default=None)) -> str | None:
    """部门空间/命名空间；若未显式指定则继承当前 project_id。"""
    return namespace
