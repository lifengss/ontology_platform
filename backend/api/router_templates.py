"""模板路由（VS5）：模板库 CRUD + 从模板实例化本体。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request

from pydantic import BaseModel

from api.dependencies import project_id_param
from api.router_ontologies import create_ontology
from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from logging_setup import get_logger
from schemas.ontology import OntologyCreate
from schemas.template import Template, TemplateCreate
from template import TemplateError, manager as template_manager
from templates import BUILTIN_TEMPLATES

router = APIRouter(prefix="/api/templates", tags=["templates"])
logger = get_logger("api.templates")
_store = JsonStore("templates")


class ExternalTemplateRequest(BaseModel):
    """V2-19：外部 LinkML 模板加载请求。"""

    url: str  # 网络模板地址（http/https）
    # registry_key 预留：registry 挂载点解析由后续迭代接入，当前仅支持 URL


class ExternalInstantiateRequest(ExternalTemplateRequest):
    """V2-19：外部模板实例化请求。"""

    name: str | None = None  # 允许覆盖实例化后的本体名


def _handle_template_error(e: TemplateError):
    """把 TemplateError 翻译为 HTTP 错误；网络/超时/超大/非法均映射为客户端错误。"""
    status = {
        "UNSAFE_SCHEME": 400,
        "TOO_LARGE": 413,
        "TIMEOUT": 408,
        "NETWORK": 502,
        "MALFORMED": 422,
    }.get(e.code, 422)
    # 网络不可达/超时在离线降级语义下也按 502 返回（前端提示「网络不可达」+ 本地模板库可用）
    return ApiError(e.code, e.message, http_status=status)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_builtin() -> None:
    records = _store.all()
    existing = {r.get("id"): r for r in records}
    changed = False
    for tpl in BUILTIN_TEMPLATES:
        if tpl["id"] not in existing:
            records.append({**tpl, "created_at": _now(), "updated_at": _now()})
            changed = True
        else:
            # 覆盖内置模板的 model，确保代码升级后内置模板保持最新
            rec = existing[tpl["id"]]
            rec.update({**tpl, "updated_at": _now()})
            changed = True
    if changed:
        # 直接写回（JsonStore 没有公开 replace，这里通过 update 写入）
        import json
        path = _store._path
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)


_seed_builtin()


@router.post("", status_code=201)
def create_template(payload: TemplateCreate):
    rec = payload.model_dump()
    rec["id"] = uuid.uuid4().hex
    rec["created_at"] = _now()
    rec["updated_at"] = rec["created_at"]
    _store.insert(rec)
    logger.info("create_template id=%s name=%s", rec["id"], rec["name"])
    return success(rec, http_status=201)


@router.get("")
def list_templates(
    industry: str | None = None,
    tag: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    page, page_size = parse_pagination(page, page_size)
    items = _store.all()
    if industry:
        items = [t for t in items if (t.get("industry") or "").lower() == industry.lower()]
    if tag:
        items = [t for t in items if tag.lower() in [x.lower() for x in (t.get("tags") or [])]]
    total = len(items)
    start = (page - 1) * page_size
    return success(list_payload(items[start:start + page_size], total, page, page_size))


@router.get("/{template_id}")
def get_template(template_id: str):
    rec = _store.get(template_id)
    if not rec:
        raise ApiError("NOT_FOUND", "模板不存在", http_status=404)
    return success(rec)


@router.put("/{template_id}")
def update_template(template_id: str, payload: TemplateCreate):
    if not _store.get(template_id):
        raise ApiError("NOT_FOUND", "模板不存在", http_status=404)
    patch = payload.model_dump(exclude_unset=True)
    patch.pop("id", None)
    patch["updated_at"] = _now()
    updated = _store.update(template_id, patch)
    return success(updated)


@router.delete("/{template_id}")
def delete_template(template_id: str):
    if not _store.delete(template_id):
        raise ApiError("NOT_FOUND", "模板不存在", http_status=404)
    return success({"deleted": True})


@router.post("/{template_id}/instantiate", status_code=201)
def instantiate_template(template_id: str, request: Request, project_id: str = Depends(project_id_param)):
    rec = _store.get(template_id)
    if not rec:
        raise ApiError("NOT_FOUND", "模板不存在", http_status=404)
    model = rec.get("model", {})
    create_payload = OntologyCreate(**model)
    response = create_ontology(create_payload, request=request, project_id=project_id, namespace=None)
    body = json.loads(response.body.decode("utf-8"))
    logger.info("instantiate_template template_id=%s project_id=%s -> ontology_id=%s", template_id, project_id, body.get("data", {}).get("id"))
    return success(body.get("data"), http_status=201)


# ---- V2-19（M4-F）外部 LinkML 模板网络加载 ----
# 固定前缀路由放在 /{template_id} 之前，避免被动态路径吞掉


@router.post("/preview-external")
def preview_external_template(payload: ExternalTemplateRequest):
    """加载外部 LinkML 模板并返回结构预览（对象/属性/约束/许可声明）。

    离线不可达返回 502（前端提示「网络不可达」+ 本地模板库可用）；
    超大/格式非法/恶意协议均拒绝（413/422/400）。
    """
    if not payload.url.strip():
        raise ApiError("VALIDATION_ERROR", "请提供模板 URL", http_status=422)
    try:
        preview = template_manager.preview_external(payload.url.strip())
    except TemplateError as e:
        raise _handle_template_error(e)
    logger.info("preview_external url=%s -> %d objects", payload.url, preview["object_count"])
    return success(preview)


@router.post("/instantiate-external", status_code=201)
def instantiate_external_template(payload: ExternalInstantiateRequest,
                                  request: Request,
                                  project_id: str = Depends(project_id_param)):
    """加载外部 LinkML 模板，翻译为本体模型并创建本地可编辑本体（draft）。

    复用 create_ontology（业务层校验/RDF 同步/审计/权限）。
    """
    if not payload.url.strip():
        raise ApiError("VALIDATION_ERROR", "请提供模板 URL", http_status=422)
    try:
        model = template_manager.load_external(payload.url.strip())
    except TemplateError as e:
        raise _handle_template_error(e)
    if payload.name:
        model["name"] = payload.name
    try:
        create_payload = OntologyCreate(**model)
    except Exception as e:  # noqa: BLE001
        raise ApiError("MALFORMED", f"模板翻译结果不符合本体结构: {e}", http_status=422)
    response = create_ontology(create_payload, request=request, project_id=project_id, namespace=None)
    body = json.loads(response.body.decode("utf-8"))
    logger.info("instantiate_external url=%s project_id=%s -> ontology_id=%s", payload.url, project_id, body.get("data", {}).get("id"))
    return success(body.get("data"), http_status=201)
