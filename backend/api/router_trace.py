"""V2-22 过程链 trace 查看路由（M4-J）：过程审计证据的读入口。

- ``GET /api/trace``：按 trace_id / run_id / log_id 检索过程链，仅平台 admin
  （与 GET /api/audit 同源：复用 audit 权限分级，非管理员 403）。
- ``GET /api/trace/export``：JSONL 导出（与检索同条件，V2-22 访问控制与体积）。
- 任何 PUT/PATCH/DELETE 写尝试 → 405 TRACE_IMMUTABLE（append-only，不改不删）。

设计口径（docs/V2-功能需求定义.md V2-22）：
- trace 查看/导出走审计权限分级（仅管理员，与 GET /api/audit 同源）；
- step details 含实例属性值/动作入参（可能敏感）：集合类字段限量（前 N 条 + total）、大值截断；
- 证据链关联：trace_id ↔ run_id ↔ action log_id ↔ 审计事件。
"""
from __future__ import annotations

import csv
import io
import json
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

import auth as auth_svc
import audit as audit_svc
from core.responses import ApiError, fail, success
from core.trace import _trace_store

router = APIRouter(prefix="/api/trace", tags=["trace"])


def _require_admin(request: Request) -> None:
    """trace 查看走审计权限分级（仅管理员）。复用 audit 的 _require_admin 判定口径。"""
    if not auth_svc.auth_enabled():
        return  # 开发态未启用鉴权：放行（与平台其他端点一致）
    user = getattr(request.state, "auth_user", None) or {}
    if user.get("role") != "admin":
        audit_svc.record(
            "audit_access_denied",
            actor=user.get("username", "-"), actor_role=user.get("role", ""),
            project_id=request.query_params.get("project_id", "") or "",
            target_type="trace", target_id=request.url.path,
            detail={"method": request.method, "role": user.get("role")},
        )
        raise ApiError("FORBIDDEN", "仅平台管理员可访问过程链 trace", http_status=403)


def _filter(project_id: Optional[str], trace_id: Optional[str],
            run_id: Optional[str], log_id: Optional[str]) -> list[dict]:
    rows = _trace_store(project_id).all()
    if trace_id:
        rows = [r for r in rows if r.get("trace_id") == trace_id]
    if run_id:
        rows = [r for r in rows if r.get("run_id") == run_id]
    if log_id:
        rows = [r for r in rows if r.get("log_id") == log_id]
    return rows


@router.get("")
def list_trace(request: Request,
               project_id: Optional[str] = Query(default=None),
               trace_id: Optional[str] = Query(default=None),
               run_id: Optional[str] = Query(default=None),
               log_id: Optional[str] = Query(default=None),
               page: int = Query(default=1, ge=1),
               page_size: int = Query(default=50, ge=1, le=200)):
    """检索过程链 trace（仅管理员；可按 trace_id / run_id / log_id 关联证据链）。"""
    _require_admin(request)
    rows = _filter(project_id, trace_id, run_id, log_id)
    rows.sort(key=lambda r: r.get("recorded_at") or "", reverse=True)
    total = len(rows)
    start = (page - 1) * page_size
    paged = [{"trace_id": r.get("trace_id"),
              "steps": r.get("steps") or [],
              "run_id": r.get("run_id"),
              "log_id": r.get("log_id"),
              "audit_event_id": r.get("audit_event_id"),
              "source": r.get("source"),
              "recorded_at": r.get("recorded_at")} for r in rows[start:start + page_size]]
    return success({"items": paged, "total": total, "page": page, "page_size": page_size})


@router.get("/export")
def export_trace(request: Request,
                 project_id: Optional[str] = Query(default=None),
                 trace_id: Optional[str] = Query(default=None),
                 run_id: Optional[str] = Query(default=None),
                 log_id: Optional[str] = Query(default=None)):
    """导出 JSONL（与检索同条件，仅管理员）。"""
    _require_admin(request)
    rows = _filter(project_id, trace_id, run_id, log_id)

    def gen():
        for r in rows:
            yield json.dumps(r, ensure_ascii=False) + "\n"

    return StreamingResponse(
        gen(), media_type="application/jsonl; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=trace-export.jsonl"})


def _tamper(request: Request, method: str):
    actor, role = audit_svc.actor_from_request(request)
    audit_svc.record("audit_tamper_attempt", actor=actor, actor_role=role,
                     project_id=request.query_params.get("project_id", "") or "",
                     target_type="trace", target_id=request.url.path,
                     detail={"method": method})
    return fail(ApiError("TRACE_IMMUTABLE", "过程链 trace 为 append-only，无修改/删除接口（V2-22）",
                         http_status=405))


@router.put("/{path:path}")
def _no_put(request: Request, path: str):
    return _tamper(request, "PUT")


@router.patch("/{path:path}")
def _no_patch(request: Request, path: str):
    return _tamper(request, "PATCH")


@router.delete("/{path:path}")
def _no_delete(request: Request, path: str):
    return _tamper(request, "DELETE")
