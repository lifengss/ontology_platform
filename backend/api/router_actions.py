"""Action 受控回写路由（UC5 / VS8）：定义 + 触发(风险分级) + 确认/拒绝(真正回写) + 重放。

V2-13（M3-E）回写风险分级：
- 触发按 risk_level 分流：low 门禁通过自动回写 / medium AI 三条件评估 / high 人工确认；
- 风险等级调整仅平台管理员（PUT /{id}/risk-level，防降级绕过，尝试留痕）；
- 确认超时/拒绝 → 取消并留痕（EP-13-03）；失败场景可重放（POST /{id}/logs/{log_id}/replay）；
- 三类回写统一审计（action_writeback，detail 含 risk_level/trigger_source/signer）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request

from action_engine import ActionEngine, list_logs, _risk_downgraded, _risk_level
from api.dependencies import project_id_param
import audit as audit_svc
from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from logging_setup import get_logger
import tenant as tenant_svc
from schemas.action import (
    ActionConfirmRequest,
    ActionCreate,
    ActionDefinition,
    ActionExecuteRequest,
    ActionRejectRequest,
    ActionRiskLevelUpdate,
)

router = APIRouter(prefix="/api/actions", tags=["actions"])
logger = get_logger("api.actions")
_engine = ActionEngine()


def _store_for(project_id: str) -> JsonStore:
    return JsonStore("actions", project_id=project_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("", status_code=201)
def create_action(payload: ActionCreate, project_id: str = Depends(project_id_param)):
    rec = payload.model_dump()
    rec["action_id"] = uuid.uuid4().hex
    rec["id"] = rec["action_id"]
    rec["project_id"] = project_id
    rec["created_at"] = _now()
    rec["updated_at"] = rec["created_at"]
    if not rec.get("name"):
        rec["name"] = rec["action_id"]
    _store_for(project_id).insert(rec)
    logger.info("create_action id=%s project_id=%s target=%s", rec["action_id"], project_id, rec["target_connection"])
    return success(rec, http_status=201)


@router.get("")
def list_actions(
    project_id: str = Depends(project_id_param),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
):
    page, page_size = parse_pagination(page, page_size)
    items = _store_for(project_id).all()
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/{action_id}")
def get_action(action_id: str, project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    return success(rec)


@router.put("/{action_id}")
def update_action(action_id: str, payload: ActionDefinition, request: Request,
                  project_id: str = Depends(project_id_param)):
    store = _store_for(project_id)
    rec = store.get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    patch.pop("action_id", None)
    # V2-13（TP-13-04）：风险等级调整仅管理员经 risk-level 专用端点，普通 update 拦截（防降级绕过）
    if "risk_level" in patch and patch["risk_level"] != _risk_level(rec):
        audit_svc.record_from_request(request, "action_risk_level_denied", project_id=project_id,
                                      target_type="action", target_id=action_id,
                                      target_name=rec.get("name", ""),
                                      before={"risk_level": _risk_level(rec)},
                                      after={"attempted": patch["risk_level"]},
                                      detail={"reason": "普通 update 端点不允许变更风险等级（需管理员经 risk-level 专用端点）"})
        raise ApiError("RISK_LEVEL_LOCKED",
                       "风险等级只能由平台管理员经 PUT /api/actions/{id}/risk-level 调整",
                       http_status=403)
    patch.pop("risk_level", None)
    patch["updated_at"] = _now()
    return success(store.update(action_id, patch))


@router.put("/{action_id}/risk-level")
def update_action_risk_level(action_id: str, payload: ActionRiskLevelUpdate, request: Request,
                             project_id: str = Depends(project_id_param)):
    """V2-13（TP-13-04）：风险等级调整——仅平台管理员；降级/升级均审计留痕（防降级绕过护栏）。"""
    user = tenant_svc.current_user(request)
    store = _store_for(project_id)
    rec = store.get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    if not tenant_svc.bypass() and not tenant_svc.is_admin(user):
        audit_svc.record_from_request(request, "action_risk_level_denied", project_id=project_id,
                                      target_type="action", target_id=action_id,
                                      target_name=rec.get("name", ""),
                                      before={"risk_level": _risk_level(rec)},
                                      after={"attempted": payload.risk_level},
                                      detail={"reason": "非平台管理员尝试调整风险等级"})
        raise ApiError("FORBIDDEN", "调整回写风险等级需要平台管理员权限", http_status=403)
    if payload.risk_level not in ("low", "medium", "high"):
        raise ApiError("VALIDATION_ERROR", "risk_level 必须是 low/medium/high", http_status=422)
    current = _risk_level(rec)
    if payload.risk_level == current:
        raise ApiError("NO_CHANGE", "风险等级未变化，无需调整", http_status=400)
    history = list(rec.get("risk_level_history") or [])
    history.append({"from": current, "to": payload.risk_level,
                    "by": (user or {}).get("username"), "at": _now(),
                    "reason": payload.reason, "downgrade": _risk_downgraded(current, payload.risk_level)})
    updated = store.update(action_id, {"risk_level": payload.risk_level,
                                       "risk_level_history": history, "updated_at": _now()})
    audit_svc.record_from_request(request, "action_risk_level_change", project_id=project_id,
                                  target_type="action", target_id=action_id,
                                  target_name=rec.get("name", ""),
                                  before={"risk_level": current},
                                  after={"risk_level": payload.risk_level},
                                  detail={"downgrade": _risk_downgraded(current, payload.risk_level),
                                          "reason": payload.reason})
    logger.info("risk-level action_id=%s %s→%s by=%s", action_id, current, payload.risk_level,
                (user or {}).get("username"))
    return success(updated)


@router.delete("/{action_id}")
def delete_action(action_id: str, project_id: str = Depends(project_id_param)):
    if not _store_for(project_id).delete(action_id):
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    return success({"deleted": True})


@router.post("/{action_id}/execute")
def execute_action(action_id: str, req: ActionExecuteRequest, request: Request,
                   project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    user = tenant_svc.current_user(request)
    result = _engine.execute(rec, req.model_dump(), project_id=project_id,
                             actor=user, trigger_source="api")
    audit_svc.record_from_request(request, "action_execute", project_id=project_id,
                                  target_type="action", target_id=action_id,
                                  target_name=rec.get("name", ""),
                                  after={"dry_run": result.get("dry_run"),
                                         "target": rec.get("target_connection"),
                                         "risk_level": _risk_level(rec),
                                         "outcome": result.get("outcome"),
                                         "auto_executed": result.get("auto_executed"),
                                         "escalated": result.get("escalated")},
                                  detail={"risk_level": _risk_level(rec), "trigger_source": "api"})
    logger.info("execute action_id=%s project_id=%s dry_run=%s risk=%s outcome=%s",
                action_id, project_id, result.get("dry_run"), _risk_level(rec), result.get("outcome"))
    return success(result)


@router.post("/{action_id}/confirm")
def confirm_action(action_id: str, req: ActionConfirmRequest, request: Request,
                   project_id: str = Depends(project_id_param)):
    rec = _store_for(project_id).get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    user = tenant_svc.current_user(request)
    # V2-13（TC-13-06）：action_writeback 统一审计由引擎 _do_writeback/confirm 记录
    # （API/MCP 入口一致，detail 含 risk_level/trigger_source/signer），路由不重复记录
    result = _engine.confirm(req.confirmation_token, {"action": rec, "project_id": project_id},
                             actor=user, trigger_source="api")
    logger.info("confirm action_id=%s project_id=%s token=%s outcome=%s",
                action_id, project_id, req.confirmation_token, (result or {}).get("outcome"))
    return success(result)


@router.post("/{action_id}/reject")
def reject_action(action_id: str, req: ActionRejectRequest, request: Request,
                  project_id: str = Depends(project_id_param)):
    """V2-13（EP-13-03）：拒绝待确认回写——取消原因与操作者（签核标识）留痕，不产生外部调用。"""
    rec = _store_for(project_id).get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    user = tenant_svc.current_user(request)
    # V2-13：拒绝审计（action_writeback，trigger_source=reject）由引擎 reject() 记录
    result = _engine.reject(req.confirmation_token, req.reason, actor=user,
                            project_id=project_id, expected_action_id=action_id)
    logger.info("reject action_id=%s project_id=%s reason=%s", action_id, project_id, req.reason)
    return success(result)


@router.get("/{action_id}/logs")
def action_logs(
    action_id: str,
    project_id: str = Depends(project_id_param),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    rec = _store_for(project_id).get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    page, page_size = parse_pagination(page, page_size)
    items = list_logs(action_id, project_id=project_id)
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.post("/{action_id}/logs/{log_id}/replay")
def replay_action_log(action_id: str, log_id: str, request: Request,
                      project_id: str = Depends(project_id_param)):
    """V2-13（EP-13-04）：失败场景重放——以原 planned_payload/幂等键重新执行；
    已生效的回写不可重放（幂等键防重）。"""
    rec = _store_for(project_id).get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    user = tenant_svc.current_user(request)
    # V2-13：重放审计（action_writeback，trigger_source=replay，detail.replay_of）由引擎记录
    result = _engine.replay(rec, log_id, project_id, actor=user, trigger_source="replay")
    logger.info("replay action_id=%s log_id=%s outcome=%s", action_id, log_id, (result or {}).get("outcome"))
    return success(result)
