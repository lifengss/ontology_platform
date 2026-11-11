"""MCP 接入路由（UC3/UC4 / VS6,VS7,VS9）：连接配置 CRUD + 连通测试 + 读取 + Server 发布 + V2-11 schema 漂移。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from api.dependencies import project_id_param
from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from dictionary import resolve_field_map
from ingestion import apply_field_map
from logging_setup import get_logger
from mcp_client import McpClient
from mcp_server import get_mcp_server
from schemas.mcp import ConnectionConfig, ConnectionCreate
from schema_drift import assert_connection_allowed, check_connection, field_feedback, health_info, resolve_drift

from tenant import current_user, project_tenant_guard  # V2-06 租户隔离（TC-06-01）/ V2-11 审计 actor

router = APIRouter(prefix="/api/mcp", tags=["mcp"], dependencies=[Depends(project_tenant_guard)])
logger = get_logger("api.mcp")
_client = McpClient()
_server = get_mcp_server()


def _connections_for(project_id: str) -> JsonStore:
    return JsonStore("mcp_connections", project_id=project_id)


def _ontologies_for(project_id: str) -> JsonStore:
    return JsonStore("ontologies", project_id=project_id)


def _candidates_for(project_id: str) -> JsonStore:
    return JsonStore("ingest_candidates", project_id=project_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_candidate_id() -> str:
    return uuid.uuid4().hex


def _instance_counts(project_id: str) -> dict[str, int]:
    """统计每个本体的实例数（数据源与审批入库一致：core.instance_store，按对象分文件 JSONL）。"""
    from core.instance_store import get_instance_store
    counts: dict[str, int] = {}
    for rec in get_instance_store(project_id).all():
        oid = rec.get("ontology_id")
        if oid:
            counts[oid] = counts.get(oid, 0) + 1
    return counts


@router.post("/connections", status_code=201)
def create_connection(payload: ConnectionCreate, project_id: str = Depends(project_id_param)):
    rec = payload.model_dump()
    rec["connection_id"] = uuid.uuid4().hex
    rec["id"] = rec["connection_id"]
    rec["project_id"] = project_id
    rec["created_at"] = _now()
    rec["updated_at"] = rec["created_at"]
    _connections_for(project_id).insert(rec)
    logger.info("create_connection id=%s project_id=%s transport=%s", rec["connection_id"], project_id, rec["transport"])
    return success(rec, http_status=201)


@router.get("/connections")
def list_connections(
    project_id: str = Depends(project_id_param),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    page, page_size = parse_pagination(page, page_size)
    items = _connections_for(project_id).all()
    total = len(items)
    start = (page - 1) * page_size
    page_items = []
    for it in items[start: start + page_size]:
        item = dict(it)
        # V2-11：列表内嵌 schema 检查健康摘要（上次检查/距上次秒/超阈值/停更/open 漂移数）
        try:
            item["schema_health"] = health_info(project_id, it["id"])
        except ApiError:
            pass
        page_items.append(item)
    return success(list_payload(page_items, total, page, page_size))


@router.get("/connections/{connection_id}")
def get_connection(connection_id: str, project_id: str = Depends(project_id_param)):
    rec = _connections_for(project_id).get(connection_id)
    if not rec:
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    return success(rec)


@router.put("/connections/{connection_id}")
def update_connection(connection_id: str, payload: ConnectionConfig, project_id: str = Depends(project_id_param)):
    store = _connections_for(project_id)
    if not store.get(connection_id):
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    patch.pop("connection_id", None)
    patch["updated_at"] = _now()
    updated = store.update(connection_id, patch)
    return success(updated)


@router.delete("/connections/{connection_id}")
def delete_connection(connection_id: str, project_id: str = Depends(project_id_param)):
    if not _connections_for(project_id).delete(connection_id):
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    return success({"deleted": True})


@router.post("/connections/{connection_id}/test")
def test_connection(connection_id: str, project_id: str = Depends(project_id_param)):
    rec = _connections_for(project_id).get(connection_id)
    if not rec:
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    result = _client.test_connection(rec)
    return success(result)


@router.post("/connections/{connection_id}/read")
def read_connection(
    request: Request,
    connection_id: str,
    project_id: str = Depends(project_id_param),
    tool: str | None = None,
    params: dict | None = None,
):
    conn_store = _connections_for(project_id)
    rec = conn_store.get(connection_id)
    if not rec:
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)

    # V2-11 门禁：键属性漂移停止期间，拒绝该连接的快照更新（读取→候选）
    assert_connection_allowed(project_id, connection_id)

    allowlist = rec.get("tool_allowlist") or []
    tool = tool or rec.get("maps_to_ontology", {}).get("read_tool")
    if allowlist and tool not in allowlist:
        raise ApiError("FORBIDDEN_WRITE", f"工具 {tool} 不在白名单 {allowlist} 中", http_status=403)

    maps = rec.get("maps_to_ontology") or {}
    ontology_id = maps.get("ontology_id")
    if not ontology_id:
        raise ApiError("VALIDATION_ERROR", "连接缺少 maps_to_ontology.ontology_id", http_status=422)
    ontology = _ontologies_for(project_id).get(ontology_id)
    if not ontology:
        raise ApiError("NOT_FOUND", "关联本体不存在", http_status=404)

    object_type = maps.get("object_type")
    # V2-01：field_map 单一解析点（explicit=连接自带；dictionary=按数据字典生成，与联邦查询同一数据源）
    field_map, field_map_source = resolve_field_map(project_id, rec)
    if not object_type or not field_map:
        hint = "（field_map_source=dictionary：请先创建绑定该连接与本体属性的数据字典词条）" \
            if field_map_source == "dictionary" else ""
        raise ApiError("VALIDATION_ERROR", f"连接缺少 maps_to_ontology.object_type 或 field_map{hint}", http_status=422)
    eff_rec = {**rec, "maps_to_ontology": {**maps, "field_map": field_map}}

    raw = _client.read(eff_rec, tool or "read", params or {})
    records = raw.get("records", [])

    # V2-11 TP-11-07：快照更新（入库读取）时结构比对——源字段缺失作为漂移信号（不阻断本次读取）
    _actor = (current_user(request) or {}).get("username", "-")
    field_feedback(project_id, eff_rec, records, actor=_actor, source="ingest")

    obj = next((o for o in ontology.get("objects", []) if o["id"] == object_type), None)
    valid_attrs = {a["id"] for a in obj.get("attributes", [])} if obj else set()
    instances = apply_field_map(records, field_map, object_type, connection_id, valid_attrs)

    candidate = {
        "id": _make_candidate_id(),
        "project_id": project_id,
        "ontology_id": ontology_id,
        "object_type": object_type,
        "source_type": "external_mcp",
        "source_ref": connection_id,
        "instances": instances,
        "status": "pending",
        "mapping": {"field_map": field_map, "field_map_source": field_map_source},
        "created_at": _now(),
        "updated_at": _now(),
    }
    _candidates_for(project_id).insert(candidate)
    logger.info("read connection_id=%s project_id=%s tool=%s records=%d candidate=%s", connection_id, project_id, tool, len(instances), candidate["id"])
    return success({
        "candidate_id": candidate["id"],
        "record_count": len(instances),
        "object_type": object_type,
    })


# --------------------------------------------------------------------------- #
# V2-11（M3-A）：schema 漂移检测 + 连接健康视图
# --------------------------------------------------------------------------- #
@router.get("/connections/{connection_id}/drifts")
def list_drifts(
    connection_id: str,
    status: str = Query(default="all", pattern="^(all|open|resolved)$"),
    project_id: str = Depends(project_id_param),
):
    """V2-11：该连接的 schema 漂移记录（含明细项列表；status: all|open|resolved）。"""
    from schema_drift import _drift_store

    if not _connections_for(project_id).get(connection_id):
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    recs = [d for d in _drift_store(project_id).all() if d.get("connection_id") == connection_id]
    if status != "all":
        recs = [d for d in recs if d.get("status") == status]
    recs.sort(key=lambda d: d.get("detected_at") or "", reverse=True)
    return success({"items": recs, "total": len(recs), "page": 1, "page_size": len(recs)})


@router.post("/connections/{connection_id}/schema-check")
def schema_check(request: Request, connection_id: str, project_id: str = Depends(project_id_param)):
    """V2-11：手动 schema 检查（复用周期探测同一管道；检查后距上次时间清零）。

    write 权限：检查可能使连接进入「快照自动更新停止」状态。
    """
    actor = (current_user(request) or {}).get("username", "-")
    return success(check_connection(project_id, connection_id, source="manual", actor=actor))


@router.post("/connections/{connection_id}/drifts/{drift_id}/resolve")
def drift_resolve(request: Request, connection_id: str, drift_id: str, project_id: str = Depends(project_id_param)):
    """V2-11：人工确认漂移恢复（唯一恢复路径，不允许自动恢复）。"""
    actor = (current_user(request) or {}).get("username", "-")
    return success(resolve_drift(project_id, connection_id, drift_id, actor=actor))


@router.get("/connections/{connection_id}/health")
def connection_health(connection_id: str, project_id: str = Depends(project_id_param)):
    """V2-11：连接健康视图——上次 schema 检查时间/距上次秒/探测周期/超阈值「长期未检查」标记。

    成功率/熔断/CDC 状态随 M3-D（V2-12）补齐（cdc_status 先返回 planned_m3d）。
    """
    return success(health_info(project_id, connection_id))


class _McpServerOpBody(BaseModel):
    """方案 C 兼容：/api/mcp/server/* 的 project_id/ontology_id 支持 query 参数或 JSON body 双形态传参。"""
    project_id: str | None = None
    ontology_id: str | None = None


def _merge_op_params(
    project_id: str | None, ontology_id: str | None, body: _McpServerOpBody | None
) -> tuple[str, str | None]:
    """query 优先，缺省回退 JSON body；project_id 缺省保持旧行为（default 项目）。"""
    body = body or _McpServerOpBody()
    return (project_id or body.project_id or "default", ontology_id or body.ontology_id)


@router.post("/server/publish")
def publish_server(
    ontology_id: str | None = None,
    project_id: str | None = None,
    body: _McpServerOpBody | None = None,
):
    """由本体一键发布 MCP Server（project_id/ontology_id 可走 query 或 JSON body）。"""
    pid, ontology_id = _merge_op_params(project_id, ontology_id, body)
    if not ontology_id:
        raise ApiError("VALIDATION_ERROR", "缺少 ontology_id（query 参数或 JSON body）", http_status=422)
    result = _server.publish(project_id=pid, ontology_id=ontology_id)
    if not result.get("published"):
        raise ApiError("BAD_REQUEST", result.get("error", "发布失败"), http_status=400)
    return success(result)


@router.post("/server/unpublish")
def unpublish_server(
    ontology_id: str | None = None,
    project_id: str | None = None,
    body: _McpServerOpBody | None = None,
):
    """取消发布：不带 ontology_id=整个项目下线；带 ontology_id=仅下线该本体（清单空则项目下线）。"""
    pid, ontology_id = _merge_op_params(project_id, ontology_id, body)
    result = _server.unpublish(project_id=pid, ontology_id=ontology_id)
    return success(result)


@router.get("/server/status")
def server_status(project_id: str | None = Query(default=None)):
    return success(_server.status(project_id=project_id))


@router.get("/server/tools")
def server_tools():
    return success(_server.tools())


@router.post("/server/token", status_code=201)
def generate_server_token(
    project_id: str | None = None,
    body: _McpServerOpBody | None = None,
):
    """生成/轮换项目 MCP 接入 token（P0-2）。旧 token 立即失效，需重新分发。project_id 可走 query 或 JSON body。"""
    pid, _ = _merge_op_params(project_id, None, body)
    rec = _server.generate_token(pid)
    return success({
        "token": rec["token"],
        "created_at": rec["created_at"],
        "usage": "Authorization: Bearer <token>（推荐）或 URL ?token=<token>",
    }, http_status=201)


@router.post("/server/token/revoke")
def revoke_server_token(
    project_id: str | None = None,
    body: _McpServerOpBody | None = None,
):
    """吊销项目 MCP 接入 token（吊销后该项目 /mcp/{pid} 所有连接 401）。project_id 可走 query 或 JSON body。"""
    pid, _ = _merge_op_params(project_id, None, body)
    return success(_server.revoke_token(pid))


@router.get("/server/token")
def get_server_token(project_id: str = Depends(project_id_param)):
    """查询项目 MCP 接入 token（含当前值，供前端展示/复制）。"""
    return success(_server.token_status(project_id))


@router.get("/ontologies")
def list_mcp_ontologies(
    project_id: str = Depends(project_id_param),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
):
    """MCP 发布清单：当前项目全部本体 + 实体数/实例数/发布状态（分页，前端每页 10 个）。"""
    page, page_size = parse_pagination(page, page_size)
    store = _ontologies_for(project_id)
    items = store.all()
    counts = _instance_counts(project_id)
    published = set(_server.published_ids(project_id))
    enriched = []
    for o in items:
        oid = o.get("id")
        enriched.append({
            "ontology_id": oid,
            "name": o.get("name") or oid,
            "description": o.get("description") or "",
            "entity_count": len(o.get("objects") or []),
            "instance_count": counts.get(oid, 0),
            "published": oid in published,
        })
    total = len(enriched)
    start = (page - 1) * page_size
    data = list_payload(enriched[start: start + page_size], total, page, page_size)
    data.update({
        "server_published": _server.server_active(project_id),
        "published_count": sum(1 for e in enriched if e["published"]),
        "endpoint": f"/mcp/{project_id}",
        "messages_endpoint": f"/mcp/{project_id}/messages/",
    })
    return success(data)
