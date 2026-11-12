"""V2-08 细粒度血缘路由（M3-B）：实例级 / 字段级血缘查询（只读）。

与 MCP 工具 lineage_get、前端实例详情面板共用 lineage.build_instance_lineage()，
保证 API / MCP / UI 三通道返回同一血缘结构。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi import status as http_status

from api.dependencies import project_id_param
from core.responses import ApiError, success
from logging_setup import get_logger
from lineage import build_instance_lineage
from tenant import project_tenant_guard  # V2-06 租户隔离

router = APIRouter(prefix="/api/lineage", tags=["lineage"], dependencies=[Depends(project_tenant_guard)])
logger = get_logger("api.lineage")


@router.get("/instances/{instance_id}")
def api_get_instance_lineage(
    instance_id: str,
    project_id: str = Depends(project_id_param),
):
    """实例血缘：来源系统/源字段（经字典术语）/导入批次/fetched_at/转换规则/上游实例。"""
    data = build_instance_lineage(project_id, instance_id)
    if data is None:
        raise ApiError("NOT_FOUND", "实例不存在", http_status.HTTP_404_NOT_FOUND)
    return success(data)
