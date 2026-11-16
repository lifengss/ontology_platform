"""V2-10 JSONL 旁路操作日志路由（M4-G）：检索 / 统计 / 按天回放 / 哈希链校验。

- ``GET /api/sidecar``：组合检索（类型/操作者/项目/对象/按天），追加序（便于回放）。
- ``GET /api/sidecar/stats``：统计（按类型/操作者计数）。
- ``GET /api/sidecar/replay``：按天回放重建当日 Action 回写序列（TC-10-02）。
- ``GET /api/sidecar/verify``：哈希链完整性校验（TC-10-03）。
- 任何 PUT/PATCH/DELETE 写尝试 → 405（append-only，无修改/删除接口）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request

import sidecar_log as sidecar_svc
from core.responses import ApiError, fail, success

router = APIRouter(prefix="/api/sidecar", tags=["sidecar"])


@router.get("")
def list_sidecar(request: Request,
                 type: Optional[str] = Query(default=None, alias="type"),
                 actor: Optional[str] = Query(default=None),
                 project_id: Optional[str] = Query(default=None),
                 target_type: Optional[str] = Query(default=None),
                 target_id: Optional[str] = Query(default=None),
                 target: Optional[str] = Query(default=None),
                 day: Optional[str] = Query(default=None, description="按天过滤 YYYY-MM-DD"),
                 page: int = Query(default=1, ge=1),
                 page_size: int = Query(default=50, ge=1, le=500)):
    """旁路日志检索页（追加序，便于按序回放）。"""
    res = sidecar_svc.query(type_=type, actor=actor, project_id=project_id,
                            target_type=target_type, target_id=target_id,
                            target=target, day=day, page=page, page_size=page_size)
    return success(res)


@router.get("/stats")
def stats_sidecar(request: Request,
                  type: Optional[str] = Query(default=None, alias="type"),
                  actor: Optional[str] = Query(default=None),
                  project_id: Optional[str] = Query(default=None),
                  day: Optional[str] = Query(default=None, description="按天统计 YYYY-MM-DD")):
    """旁路日志统计（按类型/操作者计数）。"""
    res = sidecar_svc.stats(type_=type, actor=actor, project_id=project_id, day=day)
    return success(res)


@router.get("/replay")
def replay_sidecar(request: Request,
                   day: str = Query(..., description="回放日 YYYY-MM-DD"),
                   type: str = Query(default=sidecar_svc.TYPE_ACTION_EXECUTION,
                                     alias="type", description="回放事件类型，默认 Action 执行")):
    """按天回放：重建当日指定事件序列（追加序，供与外部系统实际状态对账）。"""
    res = sidecar_svc.replay_day(day, type_=type)
    return success({"day": day, "type": type, "total": len(res), "items": res})


@router.get("/verify")
def verify_sidecar(request: Request):
    """哈希链完整性校验（篡改可检测）。"""
    res = sidecar_svc.verify_chain()
    return success(res)


def _tamper(request: Request, method: str):
    return fail(ApiError("SIDECAR_IMMUTABLE",
                         "旁路日志为 append-only，无修改/删除接口（TC-10-03）",
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
