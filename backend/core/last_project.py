"""GAP-07：全局「当前项目」上下文 —— 每个身份（登录用户）最近使用的项目（服务端默认项目）。

存储：JsonStore 全局集合 `last_project`（独立命名空间 "global"，不挂任何业务项目），
记录形如 {id, user_id, project_id, ts}。仅当项目切换（值变化）时写盘，每请求零额外 IO。
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.storage import JsonStore

_COLLECTION = "last_project"
_NAMESPACE = "global"


def _store() -> JsonStore:
    return JsonStore(_COLLECTION, project_id=_NAMESPACE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_last_project(user_id: str | None) -> str | None:
    """某身份最近使用的项目 id（无记录返回 None）。

    并发竞态下极端情况可能存在同 user_id 重复记录；取列表中最后一次匹配
    （后写入者位于其后，视为最新）。
    """
    if not user_id:
        return None
    best: dict | None = None
    for rec in _store().all():
        if rec.get("user_id") == user_id:
            best = rec
    return best.get("project_id") if best else None


def touch_last_project(user_id: str | None, project_id: str | None) -> None:
    """记录某身份最近使用的项目；值未变化时不写盘（避免每请求文件 IO）。"""
    if not user_id or not project_id:
        return
    for rec in _store().all():
        if rec.get("user_id") == user_id:
            if rec.get("project_id") == project_id:
                return
            _store().update(rec.get("id") or "", {"project_id": project_id, "ts": _now()})
            return
    _store().insert({"user_id": user_id, "project_id": project_id, "ts": _now()})
