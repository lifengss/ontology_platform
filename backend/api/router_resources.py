"""资源下载中心：把可分享的资源（如用户侧 Skill 包）对外提供下载，供下载后导入外部 AI 平台。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.responses import JSONResponse

from core.responses import success

router = APIRouter(tags=["resources"])

_RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"


def _manifest() -> dict:
    path = _RESOURCES_DIR / "manifest.json"
    if not path.exists():
        return {"version": "1.0.0", "resources": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": "1.0.0", "resources": []}


def _build_item(entry: dict) -> dict:
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "type": entry.get("type", "resource"),
        "size": entry.get("size", 0),
        "updated": entry.get("updated", ""),
        "description": entry.get("description", ""),
        "usage": entry.get("usage", ""),
        "download_url": f"/api/resources/{entry.get('id')}/download",
    }


@router.get("/api/resources")
def list_resources():
    """返回可下载的资源清单。资源为公开分享物（不含平台敏感数据），无需登录。"""
    manifest = _manifest()
    items = [_build_item(e) for e in manifest.get("resources", []) if e.get("id")]
    return success({"items": items, "version": manifest.get("version", "1.0.0")})


@router.get("/api/resources/{resource_id}/download")
def download_resource(resource_id: str):
    """下载指定资源 zip 包（Content-Disposition: attachment）。"""
    manifest = _manifest()
    entry = next((e for e in manifest.get("resources", []) if e.get("id") == resource_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="资源不存在")
    file_path = (_RESOURCES_DIR / entry["file"]).resolve()
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="资源文件缺失")
    return FileResponse(
        file_path,
        media_type="application/zip",
        filename=entry["file"],
    )
