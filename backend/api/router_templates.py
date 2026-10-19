"""模板路由（VS5）：模板库 CRUD + 从模板实例化本体。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query

from api.router_ontologies import create_ontology
from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from logging_setup import get_logger
from schemas.ontology import OntologyCreate
from schemas.template import Template, TemplateCreate
from templates import BUILTIN_TEMPLATES

router = APIRouter(prefix="/api/templates", tags=["templates"])
logger = get_logger("api.templates")
_store = JsonStore("templates")


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
def instantiate_template(template_id: str):
    rec = _store.get(template_id)
    if not rec:
        raise ApiError("NOT_FOUND", "模板不存在", http_status=404)
    model = rec.get("model", {})
    # 使用 OntologyCreate 校验并复用已有创建逻辑
    create_payload = OntologyCreate(**model)
    response = create_ontology(create_payload)
    # create_ontology 返回 JSONResponse，直接透传
    body = json.loads(response.body.decode("utf-8"))
    logger.info("instantiate_template template_id=%s -> ontology_id=%s", template_id, body.get("data", {}).get("id"))
    return success(body.get("data"), http_status=201)
