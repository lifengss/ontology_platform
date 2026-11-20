"""通用 FastAPI 依赖：项目隔离参数解析 + V2-06 租户边界强制（M2-C）。

project_id_param 是全站项目级资源的唯一入口依赖：在此统一执行租户隔离
（TC-06-01：跨租户访问项目级资源一律 404）。鉴权关闭（测试态豁免）时放行。
"""
from __future__ import annotations

from fastapi import Query, Request

from core.last_project import get_last_project, touch_last_project
from core.responses import ApiError
from tenant import bypass, current_user, ensure_project_access

DEFAULT_PROJECT_ID = "default"


def _fallback_project(request: Request) -> str:
    """GAP-07：project_id 省略时的回退——当前身份最近使用的项目 → default。

    最近项目须仍可读：通过 V2-06 项目访问判定（跨租户/不可读 → 跳过，不泄露存在性）；
    鉴权豁免（测试态）时访问判定直通，另校验项目真实存在（已删除的项目不得成为回退目标）。
    """
    user = current_user(request) or {}
    last = get_last_project(user.get("id"))
    if last and _project_readable(request, last):
        return last
    return DEFAULT_PROJECT_ID


def _project_readable(request: Request, project_id: str) -> bool:
    """最近项目是否仍可用（V2-06 访问判定通过且项目存在）。"""
    try:
        ensure_project_access(request, project_id)
    except ApiError:
        return False
    if bypass():
        try:
            from api.router_projects import get_project_store
            return get_project_store().get(project_id) is not None
        except Exception:  # noqa: BLE001
            return False
    return True


def project_id_param(
    request: Request,
    project_id: str | None = Query(
        default=None,
        description="业务项目 ID（多项目隔离）。GAP-07：可省略——省略时回退当前身份最近使用的项目（服务端默认项目），仍无则 default",
    ),
) -> str:
    """从 query 读取 project_id；省略时回退身份最近项目 → default（GAP-07）。

    V2-06：本体/实例为资源级共享判定（tenant.schema_visible），不在依赖层一刀切；
    连接/字典/候选等无共享语义的资源由各路由显式调用 tenant.ensure_project_access。
    显式传值时记录为该身份最近使用项目（仅值变化时写盘）。
    """
    if project_id:
        _touch_current(request, project_id)
        return project_id
    return _fallback_project(request)


def optional_project_id_param(
    request: Request,
    project_id: str | None = Query(default=None, description="业务项目 ID；省略时回退身份最近项目 → default（GAP-07）"),
) -> str:
    if project_id:
        _touch_current(request, project_id)
        return project_id
    return _fallback_project(request)


def _touch_current(request: Request, project_id: str) -> None:
    """GAP-07：显式指定项目时记录为该身份最近使用项目（失败不阻断主流程）。"""
    try:
        user = current_user(request) or {}
        touch_last_project(user.get("id"), project_id)
    except Exception:  # noqa: BLE001 记录属增强能力，异常不得影响业务请求
        pass


def namespace_param(namespace: str | None = Query(default=None)) -> str | None:
    """部门空间/命名空间；若未显式指定则继承当前 project_id。"""
    return namespace
