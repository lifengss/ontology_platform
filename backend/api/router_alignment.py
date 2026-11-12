"""V2-02 跨本体对齐（M4-A②）API：候选检测／等价声明／会签／冲突决策。

端点（EP-02-01/02/04/06）：
- POST /api/alignment/candidates          候选检测（信号+证据+置信度；仅双方可见对；被拒对不重提）
- POST /api/alignment/declarations        手动等价声明（候选可预填；进入会签流程）
- GET  /api/alignment/declarations        声明列表（任一涉及本体可见即可见）
- GET  /api/alignment/declarations/{id}   声明详情（含冲突与会签进度）
- POST /api/alignment/declarations/{id}/sign           会签（approve/reject；一票否决；admin 代行缺位 owner）
- POST /api/alignment/declarations/{id}/withdraw       撤回（提交人，pending）
- POST /api/alignment/declarations/{id}/conflicts/{i}/resolve   冲突决策（权威源 a/b；批准前置）
"""
from __future__ import annotations

import alignment
from fastapi import APIRouter, Depends, Request
from fastapi import status as http_status
from pydantic import BaseModel

import audit
from api.dependencies import project_id_param
from api.router_projects import get_project_store
from core.responses import ApiError, success
from core.storage import JsonStore
from tenant import current_user, ensure_project_access

router = APIRouter(prefix="/api/alignment", tags=["alignment"])


class _ObjectPairIn(BaseModel):
    object_a: str
    object_b: str
    attribute_pairs: list[dict] = []


class DeclareIn(BaseModel):
    ontology_a: str
    ontology_b: str
    object_pairs: list[_ObjectPairIn]
    candidate: dict | None = None  # 候选预填（signals/confidence）
    repropose: bool = False


class SignIn(BaseModel):
    decision: str  # approve | reject
    comment: str = ""
    proxy_reason: str = ""


class ConflictResolveIn(BaseModel):
    resolution: str  # a | b


def _ont_store(project_id: str) -> JsonStore:
    return JsonStore("ontologies", project_id)


def _get_ont(project_id: str, oid: str) -> dict:
    ont = _ont_store(project_id).get(oid)
    if not ont:
        raise ApiError("NOT_FOUND", "本体不存在", http_status.HTTP_404_NOT_FOUND)
    return ont


def _decl_store(project_id: str):
    return alignment._store(project_id)


def _project(project_id: str) -> dict:
    return get_project_store().get(project_id) or {"id": project_id}


@router.post("/candidates")
def api_detect_candidates(request: Request, project_id: str = Depends(project_id_param)):
    """EP-02-01：候选对齐检测（key overlap / common origin / semantic / structural＋置信度＋证据）。
    仅生成查看者双方可见的本体对（TP-02-07）；被拒对证据显著变化才重提（TP-02-06）。"""
    ensure_project_access(request, project_id)
    user = current_user(request)
    ontologies = [o for o in _ont_store(project_id).all() if o.get("id")]
    out = alignment.detect_candidates(user, _project(project_id), ontologies)
    return success(out)


@router.post("/declarations", status_code=http_status.HTTP_201_CREATED)
def api_create_declaration(request: Request, body: DeclareIn,
                           project_id: str = Depends(project_id_param)):
    """EP-02-02 / TP-02-13：手动等价声明（候选预填可选）→ 进入多 owner 会签。
    可见性/会签口径同候选检测；public 本体不能由非 owner 会签生效。"""
    ensure_project_access(request, project_id)
    user = current_user(request)
    username = user.get("username") or ""
    a = _get_ont(project_id, body.ontology_a)
    b = _get_ont(project_id, body.ontology_b)
    if a["id"] == b["id"]:
        raise ApiError("BAD_PAIR", "不能声明本体与自身等价", http_status.HTTP_422_UNPROCESSABLE_ENTITY)
    from tenant import schema_visible
    for ont in (a, b):
        if not schema_visible(user, _project(project_id), ont):
            raise ApiError("NOT_FOUND", "本体不存在", http_status.HTTP_404_NOT_FOUND)
    if not body.object_pairs:
        raise ApiError("BAD_BODY", "object_pairs 不能为空", http_status.HTTP_422_UNPROCESSABLE_ENTITY)
    # 对象/属性存在性
    objs_a = {o.get("id") for o in (a.get("objects") or [])}
    objs_b = {o.get("id") for o in (b.get("objects") or [])}
    for p in body.object_pairs:
        if p.object_a not in objs_a or p.object_b not in objs_b:
            raise ApiError("NOT_FOUND", "对象不存在", http_status.HTTP_404_NOT_FOUND)
    # 重复的 pending 声明：同对进行中则 409（避免并行会签）
    for d in _decl_store(project_id).all():
        if (tuple(sorted((d.get("ontology_a") or "", d.get("ontology_b") or "")))
                == tuple(sorted((a["id"], b["id"])))
                and d.get("status") == "pending"):
            raise ApiError("DECLARATION_IN_PROGRESS",
                           f"该本体对已有进行中的声明 {d['id']}，请先完成或撤回",
                           http_status.HTTP_409_CONFLICT)
    decl = alignment.make_declaration(
        project_id, a, b, [p.model_dump() for p in body.object_pairs],
        submitted_by=username, candidate=body.candidate, repropose=body.repropose)
    _decl_store(project_id).insert(decl)
    audit.record_from_request(request, "alignment_declare",
                              project_id=project_id, target_type="alignment_declaration",
                              target_id=decl["id"],
                              before=None,
                              after={"ontology_a": a["id"], "ontology_b": b["id"],
                                     "status": "pending",
                                     "conflicts": len(decl["conflicts"]),
                                     "signers_required": decl["signers_required"]})
    return success(decl, http_status=http_status.HTTP_201_CREATED)


def _visible_decl(request: Request, project_id: str, decl_id: str) -> dict:
    user = current_user(request)
    d = _decl_store(project_id).get(decl_id)
    if not d:
        raise ApiError("NOT_FOUND", "声明不存在", http_status.HTTP_404_NOT_FOUND)
    ontologies = [o for o in _ont_store(project_id).all() if o.get("id")]
    if not alignment.is_decl_visible(user, _project(project_id), d, ontologies):
        raise ApiError("NOT_FOUND", "声明不存在", http_status.HTTP_404_NOT_FOUND)
    return d


@router.get("/declarations")
def api_list_declarations(request: Request, status: str = "",
                          project_id: str = Depends(project_id_param)):
    """声明列表（任一涉及本体对查看者可见即可见）。"""
    ensure_project_access(request, project_id)
    user = current_user(request)
    ontologies = [o for o in _ont_store(project_id).all() if o.get("id")]
    items = [d for d in _decl_store(project_id).all()
             if (not status or d.get("status") == status)
             and alignment.is_decl_visible(user, _project(project_id), d, ontologies)]
    items.sort(key=lambda d: d.get("submitted_at") or "", reverse=True)
    return success({"items": items, "total": len(items)})


@router.get("/declarations/{decl_id}")
def api_get_declaration(request: Request, decl_id: str,
                        project_id: str = Depends(project_id_param)):
    return success(_visible_decl(request, project_id, decl_id))


@router.post("/declarations/{decl_id}/sign")
def api_sign(request: Request, decl_id: str, body: SignIn,
             project_id: str = Depends(project_id_param)):
    """EP-02-04：会签（一票否决；owner 缺位租户管理员代行＋审计；批准前置＝冲突全决策）。"""
    ensure_project_access(request, project_id)
    user = current_user(request)
    store = _decl_store(project_id)
    d = _visible_decl(request, project_id, decl_id)
    try:
        decl, events = alignment.sign_decl(project_id, dict(d), user, body.decision,
                                           body.comment, body.proxy_reason)
    except alignment._DeclError as e:
        http = http_status.HTTP_422_UNPROCESSABLE_ENTITY if e.code in (
            "CONFLICT_RESOLUTION_REQUIRED", "PROXY_REASON_REQUIRED", "OWNER_ABSENT",
            "BAD_RESOLUTION", "CONFLICT_NOT_FOUND") else http_status.HTTP_403_FORBIDDEN
        audit.record_from_request(request, "alignment_sign_denied",
                                  project_id=project_id, target_type="alignment_declaration",
                                  target_id=decl_id, detail={"code": e.code, "message": e.message})
        raise ApiError(e.code, e.message, http)
    store.update(decl["id"], decl)
    for ev in events:
        audit.record_from_request(request, ev, project_id=project_id,
                                  target_type="alignment_declaration", target_id=decl_id,
                                  before={"status": d.get("status")},
                                  after={"status": decl.get("status")})
    return success(decl)


@router.post("/declarations/{decl_id}/withdraw")
def api_withdraw(request: Request, decl_id: str,
                 project_id: str = Depends(project_id_param)):
    ensure_project_access(request, project_id)
    user = current_user(request)
    store = _decl_store(project_id)
    d = _visible_decl(request, project_id, decl_id)
    try:
        decl = alignment.withdraw_decl(project_id, dict(d), user)
    except alignment._DeclError as e:
        raise ApiError(e.code, e.message, http_status.HTTP_403_FORBIDDEN)
    store.update(decl["id"], decl)
    audit.record_from_request(request, "alignment_withdraw", project_id=project_id,
                              target_type="alignment_declaration", target_id=decl_id,
                              before={"status": d.get("status")},
                              after={"status": decl.get("status")})
    return success(decl)


@router.post("/declarations/{decl_id}/conflicts/{idx}/resolve")
def api_resolve_conflict(request: Request, decl_id: str, idx: int, body: ConflictResolveIn,
                         project_id: str = Depends(project_id_param)):
    """EP-02-06：属性冲突显式决策（权威源 a/b）；批准前置，不自动合并。"""
    ensure_project_access(request, project_id)
    user = current_user(request)
    store = _decl_store(project_id)
    d = _visible_decl(request, project_id, decl_id)
    try:
        decl = alignment.resolve_conflict(project_id, dict(d), user, idx, body.resolution)
    except alignment._DeclError as e:
        http = http_status.HTTP_422_UNPROCESSABLE_ENTITY if e.code in (
            "BAD_RESOLUTION", "CONFLICT_NOT_FOUND") else http_status.HTTP_403_FORBIDDEN
        raise ApiError(e.code, e.message, http)
    store.update(decl["id"], decl)
    audit.record_from_request(request, "alignment_conflict_resolve", project_id=project_id,
                              target_type="alignment_declaration", target_id=decl_id,
                              detail={"conflict_index": idx, "resolution": body.resolution,
                                      "conflict": decl["conflicts"][idx]})
    return success(decl)
