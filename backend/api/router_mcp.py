"""MCP 接入路由（UC3/UC4 / VS6,VS7,VS9）：连接配置 CRUD + 连通测试 + 读取 + Server 发布。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api.dependencies import project_id_param
from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from ingestion import apply_field_map
from logging_setup import get_logger
from mcp_client import McpClient
from mcp_server import get_mcp_server
from schemas.mcp import ConnectionConfig, ConnectionCreate

router = APIRouter(prefix="/api/mcp", tags=["mcp"])
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    return success(list_payload(items[start: start + page_size], total, page, page_size))


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
    connection_id: str,
    project_id: str = Depends(project_id_param),
    tool: str | None = None,
    params: dict | None = None,
):
    conn_store = _connections_for(project_id)
    rec = conn_store.get(connection_id)
    if not rec:
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)

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
    field_map: dict[str, str] = maps.get("field_map") or {}
    if not object_type or not field_map:
        raise ApiError("VALIDATION_ERROR", "连接缺少 maps_to_ontology.object_type 或 field_map", http_status=422)

    raw = _client.read(rec, tool or "read", params or {})
    records = raw.get("records", [])

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
        "mapping": {"field_map": field_map},
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
