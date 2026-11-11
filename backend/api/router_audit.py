"""V2-09 完整审计路由（M2-D）：统一检索页 + 导出（INV-4：无修改/删除 API）。

- ``GET /api/audit``：组合检索（操作者/动作/对象/项目/时间范围），仅平台 admin
  （auth 过滤器按 AUTH_ADMIN_PREFIXES 拦截非 admin，permission_denied 本身留痕）。
- ``GET /api/audit/export``：CSV / JSONL 导出（与检索同条件，内容一致 TC-09-03）。
- 任何 PUT/PATCH/DELETE 写尝试 → 405 AUDIT_IMMUTABLE，并记录 audit_tamper_attempt（INV-4）。
"""
from __future__ import annotations

import csv
import io
import json
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

import audit as audit_svc
import auth as auth_svc
from core.responses import ApiError, fail, success

router = APIRouter(prefix="/api/audit", tags=["audit"])

_CSV_FIELDS = [
    "id", "ts", "actor", "actor_role", "action", "project_id", "tenant_id",
    "target_type", "target_id", "target_name", "before", "after", "detail",
]


def _require_admin(request: Request) -> None:
    if not auth_svc.auth_enabled():
        return  # 开发态未启用鉴权：放行（与平台其他端点一致）
    user = getattr(request.state, "auth_user", None) or {}
    if user.get("role") != "admin":
        # 正常路径由 auth 过滤器（admin 前缀）先行拦截；此处兜底并保证"尝试本身留痕"
        audit_svc.record(
            "audit_access_denied",
            actor=user.get("username", "-"), actor_role=user.get("role", ""),
            project_id=request.query_params.get("project_id", "") or "",
            target_type="audit", target_id=request.url.path,
            detail={"method": request.method, "role": user.get("role")},
        )
        raise ApiError("FORBIDDEN", "仅平台管理员可访问审计日志", http_status=403)


def _filters(actor: Optional[str], action: Optional[str], project_id: Optional[str],
             target: Optional[str], target_id: Optional[str], start: Optional[str],
             end: Optional[str]) -> dict:
    return dict(actor=actor, action=action, project_id=project_id, target=target,
                target_id=target_id, start=start, end=end)


@router.get("")
def list_audit(request: Request,
               actor: Optional[str] = Query(default=None),
               action: Optional[str] = Query(default=None),
               project_id: Optional[str] = Query(default=None),
               target: Optional[str] = Query(default=None),
               target_id: Optional[str] = Query(default=None),
               start: Optional[str] = Query(default=None, description="起始时间 ISO"),
               end: Optional[str] = Query(default=None, description="结束时间 ISO"),
               page: int = Query(default=1, ge=1),
               page_size: int = Query(default=50, ge=1, le=500)):
    """审计检索页（操作者＋时间＋对象组合；倒序；前值/后值随记录返回）。"""
    _require_admin(request)
    res = audit_svc.query(page=page, page_size=page_size, **_filters(
        actor, action, project_id, target, target_id, start, end))
    return success(res)


@router.get("/export")
def export_audit(request: Request,
                 format: str = Query(default="csv", pattern="^(csv|jsonl)$"),
                 actor: Optional[str] = Query(default=None),
                 action: Optional[str] = Query(default=None),
                 project_id: Optional[str] = Query(default=None),
                 target: Optional[str] = Query(default=None),
                 target_id: Optional[str] = Query(default=None),
                 start: Optional[str] = Query(default=None),
                 end: Optional[str] = Query(default=None)):
    """导出 CSV / JSONL（与检索页同条件、同内容，TC-09-03）。"""
    _require_admin(request)
    rows = audit_svc.export_rows(**_filters(actor, action, project_id, target, target_id, start, end))

    if format == "jsonl":
        def gen_jsonl():
            for r in rows:
                yield json.dumps(r, ensure_ascii=False) + "\n"
        return StreamingResponse(
            gen_jsonl(), media_type="application/jsonl; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=audit-export.jsonl"})

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        row = dict(r)
        for k in ("before", "after", "detail"):
            if isinstance(row.get(k), (dict, list)):
                row[k] = json.dumps(row[k], ensure_ascii=False)
        writer.writerow({k: row.get(k, "") for k in _CSV_FIELDS})
    data = buf.getvalue().encode("utf-8-sig")  # BOM 便于 Excel 打开中文
    return StreamingResponse(
        io.BytesIO(data), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit-export.csv"})


def _tamper(request: Request, method: str):
    actor, role = audit_svc.actor_from_request(request)
    # 携带请求携带的 project_id（如有），便于按项目检索篡改尝试
    project_id = request.query_params.get("project_id", "") or ""
    audit_svc.record("audit_tamper_attempt", actor=actor, actor_role=role,
                     project_id=project_id,
                     target_type="audit", target_id=request.url.path,
                     detail={"method": method})
    return fail(ApiError("AUDIT_IMMUTABLE", "审计日志为 append-only，无修改/删除接口（INV-4）",
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
