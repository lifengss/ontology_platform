"""Action 受控回写路由（UC5 / VS8）：定义 + 触发(确认令牌) + 确认(真正回写)。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query

from action_engine import ActionEngine
from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from logging_setup import get_logger
from schemas.action import (
    ActionConfirmRequest,
    ActionCreate,
    ActionDefinition,
    ActionExecuteRequest,
)

router = APIRouter(prefix="/api/actions", tags=["actions"])
logger = get_logger("api.actions")
_store = JsonStore("actions")
_engine = ActionEngine()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("", status_code=201)
def create_action(payload: ActionCreate):
    rec = payload.model_dump()
    rec["action_id"] = uuid.uuid4().hex
    rec["id"] = rec["action_id"]  # JsonStore 按 id 索引
    rec["created_at"] = _now()
    rec["updated_at"] = rec["created_at"]
    _store.insert(rec)
    logger.info("create_action id=%s target=%s", rec["action_id"], rec["target_connection"])
    return success(rec, http_status=201)


@router.get("")
def list_actions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    page, page_size = parse_pagination(page, page_size)
    items = _store.all()
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/{action_id}")
def get_action(action_id: str):
    rec = _store.get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    return success(rec)


@router.put("/{action_id}")
def update_action(action_id: str, payload: ActionDefinition):
    if not _store.get(action_id):
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    patch.pop("action_id", None)
    patch["updated_at"] = _now()
    return success(_store.update(action_id, patch))


@router.delete("/{action_id}")
def delete_action(action_id: str):
    if not _store.delete(action_id):
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    return success({"deleted": True})


@router.post("/{action_id}/execute")
def execute_action(action_id: str, req: ActionExecuteRequest):
    rec = _store.get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    result = _engine.execute(rec, req.model_dump())
    logger.info("execute action_id=%s dry_run=%s", action_id, result.get("dry_run"))
    return success(result)


@router.post("/{action_id}/confirm")
def confirm_action(action_id: str, req: ActionConfirmRequest):
    rec = _store.get(action_id)
    if not rec:
        raise ApiError("NOT_FOUND", "Action 不存在", http_status=404)
    result = _engine.confirm(req.confirmation_token, {"action": rec})
    logger.info("confirm action_id=%s token=%s", action_id, req.confirmation_token)
    return success(result)
