"""MCP 接入路由（UC3/UC4 / VS6,VS7,VS9）：连接配置 CRUD + 连通测试 + 读取 + Server 发布。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query

from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from ingestion import apply_field_map
from logging_setup import get_logger
from mcp_client import McpClient
from mcp_server import McpServer
from schemas.mcp import ConnectionConfig, ConnectionCreate

router = APIRouter(prefix="/api/mcp", tags=["mcp"])
logger = get_logger("api.mcp")
_store = JsonStore("mcp_connections")
_ontologies = JsonStore("ontologies")
_candidates = JsonStore("ingest_candidates")
_client = McpClient()
_server = McpServer()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_candidate_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/connections", status_code=201)
def create_connection(payload: ConnectionCreate):
    rec = payload.model_dump()
    rec["connection_id"] = uuid.uuid4().hex
    rec["id"] = rec["connection_id"]  # JsonStore 按 id 索引
    rec["created_at"] = _now()
    rec["updated_at"] = rec["created_at"]
    _store.insert(rec)
    logger.info("create_connection id=%s transport=%s", rec["connection_id"], rec["transport"])
    return success(rec, http_status=201)


@router.get("/connections")
def list_connections(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    page, page_size = parse_pagination(page, page_size)
    items = _store.all()
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start: start + page_size], total, page, page_size))


@router.get("/connections/{connection_id}")
def get_connection(connection_id: str):
    rec = _store.get(connection_id)
    if not rec:
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    return success(rec)  # 不含凭据明文（auth 中仅存引用）


@router.put("/connections/{connection_id}")
def update_connection(connection_id: str, payload: ConnectionConfig):
    if not _store.get(connection_id):
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    patch.pop("connection_id", None)
    patch["updated_at"] = _now()
    updated = _store.update(connection_id, patch)
    return success(updated)


@router.delete("/connections/{connection_id}")
def delete_connection(connection_id: str):
    if not _store.delete(connection_id):
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    return success({"deleted": True})


@router.post("/connections/{connection_id}/test")
def test_connection(connection_id: str):
    rec = _store.get(connection_id)
    if not rec:
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    result = _client.test_connection(rec)
    return success(result)


@router.post("/connections/{connection_id}/read")
def read_connection(connection_id: str, tool: str | None = None, params: dict | None = None):
    rec = _store.get(connection_id)
    if not rec:
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)

    # 工具白名单校验
    allowlist = rec.get("tool_allowlist") or []
    tool = tool or rec.get("maps_to_ontology", {}).get("read_tool")
    if allowlist and tool not in allowlist:
        raise ApiError("FORBIDDEN_WRITE", f"工具 {tool} 不在白名单 {allowlist} 中", http_status=403)

    maps = rec.get("maps_to_ontology") or {}
    ontology_id = maps.get("ontology_id")
    if not ontology_id:
        raise ApiError("VALIDATION_ERROR", "连接缺少 maps_to_ontology.ontology_id", http_status=422)
    ontology = _ontologies.get(ontology_id)
    if not ontology:
        raise ApiError("NOT_FOUND", "关联本体不存在", http_status=404)

    object_type = maps.get("object_type")
    field_map: dict[str, str] = maps.get("field_map") or {}
    if not object_type or not field_map:
        raise ApiError("VALIDATION_ERROR", "连接缺少 maps_to_ontology.object_type 或 field_map", http_status=422)

    # 调用 MCP 客户端读取
    raw = _client.read(rec, tool or "read", params or {})
    records = raw.get("records", [])

    # 映射为候选实例
    obj = next((o for o in ontology.get("objects", []) if o["id"] == object_type), None)
    valid_attrs = {a["id"] for a in obj.get("attributes", [])} if obj else set()
    instances = apply_field_map(records, field_map, object_type, connection_id, valid_attrs)

    candidate = {
        "id": _make_candidate_id(),
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
    _candidates.insert(candidate)
    logger.info("read connection_id=%s tool=%s records=%d candidate=%s", connection_id, tool, len(instances), candidate["id"])
    return success({
        "candidate_id": candidate["id"],
        "record_count": len(instances),
        "object_type": object_type,
    })


@router.post("/server/publish")
def publish_server(ontology_id: str):
    # VS6：由本体自动生成 MCP 工具并挂载 /mcp
    result = _server.publish(ontology_id)
    return success(result)


@router.get("/server/status")
def server_status():
    return success(_server.status())
