"""V2-07 变更审批流（M2-E）：兼容性判定 / 大版本审批 / 状态机 / 版本固定引用。

状态机（TC-07-02，无逐条审批，审批粒度=变更集）：
    pending → approved | rejected | withdrawn | abandoned
    rejected/withdrawn/abandoned → archived（被新提案取代）
- 兼容变更（新增可选属性/范围放宽/新增对象等）→ 自动升小版本直接发布（TC-07-04）；
- 不兼容变更（删属性/类型变/主键变/范围收窄/删对象等）→ 必须升大版本＋审批，
  通过前对外仍是旧版本（TC-07-01）；API 直发被拦截（TCE-07-02）。
- 先弃用后删除（TC-07-05）：@deprecated 宽限期内可读（带提示），期满前真删被拒。
- 旧版本持续可用（TC-07-06）：?version= 固定引用（REST/MCP），缺省最新稳定版。
- 并发审批幂等（TCE-07-03）：锁内复检状态，重复请求返回既有结果不产生双实例。
"""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request

import audit as audit_svc
import schemadiff as sd
from api.dependencies import project_id_param
from core.responses import ApiError, success
from logging_setup import get_logger
from modeling_translator import validate_business_layer
from schemas.changesets import AttributeDeprecate, ChangesetCreate, RejectBody
from tenant import (
    current_user,
    ensure_project_access,
    ensure_schema_visible,
    ensure_schema_write,
)

router = APIRouter(tags=["changesets"])
logger = get_logger("api.changesets")

_CS_LOCK = threading.Lock()  # TCE-07-03：同一审批单状态迁移串行化

_TERMINAL = {"approved", "rejected", "withdrawn", "abandoned", "archived"}


def _ont_store(project_id: str):
    from core.storage import JsonStore
    return JsonStore("ontologies", project_id=project_id)


def _cs_store(project_id: str):
    from core.storage import JsonStore
    return JsonStore("changesets", project_id=project_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor(user) -> str:
    return (user or {}).get("username", "-")


def _tenant_id(user) -> str:
    return (user or {}).get("tenant_id", "") or ""


def _model_of(rec: dict[str, Any]) -> dict[str, Any]:
    """本体记录 → 模型视图（版本化快照用）。"""
    return {k: copy.deepcopy(rec.get(k)) for k in ("name", "description", "objects",
                                                   "constraints", "functions") if k in rec}


def _published_base(rec: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """当前已发布基线：(model, version)。存量本体无 versions 时以当前模型为基线。"""
    versions = rec.get("versions") or []
    if versions:
        last = versions[-1]
        return last["model"], last["version"]
    return _model_of(rec), sd.norm_version(rec.get("version"))


def _require_ontology(project_id: str, ontology_id: str) -> dict[str, Any]:
    rec = _ont_store(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    return rec


def _backfill_proposal_model(model: dict[str, Any], base_model: dict[str, Any],
                             fallback_namespace: str = "") -> None:
    """提案模型自动补齐（对齐创建路径 _backfill_object_fields 的同一规则）。

    前端表单不采集 business_definition/stable_key/namespace，UI 提交的变更提案缺失
    这些字段时会被 validate_business_layer 直接 422 拦截（TCE2E-06 根因）。补齐优先级：
    - business_definition：提案值 → 基线同 id 对象继承 → description → name 派生（<5 字兜底）；
    - stable_key：提案值 → 基线继承（且主键属性仍存在于提案中，防止删属性导致主键漂移、
      在语义 diff 中引入无关变化）→ 首个 required string 属性 → 首个 string → 任意属性；
    - namespace：提案值 → 基线继承 → 本体 namespace。
    """
    base_objs = {o.get("id"): o for o in (base_model.get("objects") or [])}
    for obj in model.get("objects") or []:
        base_obj = base_objs.get(obj.get("id")) or {}
        if not obj.get("namespace"):
            obj["namespace"] = base_obj.get("namespace") or fallback_namespace
        biz_def = (obj.get("business_definition")
                   or base_obj.get("business_definition")
                   or obj.get("description")
                   or f"{obj.get('name', '对象')}的业务语义").strip()
        if len(biz_def) < 5:
            biz_def = f"{obj.get('name', '对象')}的业务语义定义"
        obj["business_definition"] = biz_def
        attrs = obj.get("attributes") or []
        attr_ids = {a.get("id") for a in attrs}
        if not obj.get("stable_key"):
            inherited = base_obj.get("stable_key")
            if inherited and inherited in attr_ids:
                obj["stable_key"] = inherited
                obj["stable_key_explicit"] = bool(base_obj.get("stable_key_explicit"))
            elif attrs:
                candidates = [a for a in attrs if a.get("required") and a.get("data_type", "string") == "string"]
                if not candidates:
                    candidates = [a for a in attrs if a.get("data_type", "string") == "string"]
                if not candidates:
                    candidates = attrs
                if candidates:
                    obj["stable_key"] = candidates[0]["id"]
                    obj["stable_key_explicit"] = False  # 自动推断；用户显式指定时由请求体置 True


def _validate_model(model: dict[str, Any]) -> None:
    violations = validate_business_layer(model)
    if violations:
        raise ApiError("VALIDATION_ERROR", "提案模型未通过业务层校验",
                       http_status=422, details={"violations": violations})


def _check_deprecation_grace(base_model: dict[str, Any], diff: dict[str, Any]) -> None:
    """TC-07-05：宽限期未满的属性不得真删。"""
    base_attrs: dict[str, dict[str, Any]] = {}
    for o in base_model.get("objects") or []:
        for a in o.get("attributes") or []:
            if a.get("id"):
                base_attrs[a["id"]] = a
    now = _now()
    for c in diff.get("changes", []):
        if c.get("kind") != "attribute_removed":
            continue
        attr = base_attrs.get(c.get("attribute_id")) or {}
        dep = attr.get("deprecated") or {}
        if dep.get("until") and str(dep["until"]) > now:
            raise ApiError(
                "DEPRECATION_GRACE_NOT_EXPIRED",
                f"属性 {c.get('attribute_id')} 处于弃用宽限期（至 {dep['until']}），期满后方可删除（TC-07-05）",
                http_status=422,
                details={"attribute_id": c.get("attribute_id"), "until": dep.get("until")},
            )


def _apply_version(rec: dict[str, Any], project_id: str, model: dict[str, Any],
                   version: str, note: str, changes: list[dict[str, Any]],
                   actor: str) -> dict[str, Any]:
    """发布新版本：更新模型字段＋版本指针＋versions 历史（深拷贝快照）。"""
    store = _ont_store(project_id)
    versions = list(rec.get("versions") or [])
    versions.append({
        "version": version,
        "published_at": _now(),
        "published_by": actor,
        "note": note,
        "changes": changes,
        "model": copy.deepcopy(model),
    })
    return store.update(rec["id"], {
        **{k: copy.deepcopy(model[k]) for k in ("name", "description", "objects", "constraints", "functions") if k in model},
        "version": version,
        "published_version": version,
        "versions": versions,
        "updated_at": _now(),
    })


# --------------------------------------------------------------------------- #
# 提交变更提案
# --------------------------------------------------------------------------- #
@router.post("/api/ontologies/{ontology_id}/changesets")
def submit_changeset(ontology_id: str, req: ChangesetCreate, request: Request,
                     project_id: str = Depends(project_id_param)):
    rec = _require_ontology(project_id, ontology_id)
    user = ensure_schema_write(request, project_id, rec)
    if rec.get("status") != "active":
        raise ApiError("VALIDATION_ERROR", "仅已发布（active）本体可提交变更", http_status=422)

    base_model, base_version = _published_base(rec)
    model = req.model.model_dump()
    _backfill_proposal_model(model, base_model, fallback_namespace=rec.get("namespace") or "")
    _validate_model(model)
    diff = sd.diff_ontology(base_model, model)
    _check_deprecation_grace(base_model, diff)

    actor = _actor(user)
    if diff["compatible"]:
        # 兼容变更：自动升小版本直接发布（TC-07-04 ①）
        new_version = sd.bump_minor(base_version)
        updated = _apply_version(rec, project_id, model, new_version,
                                 req.note, diff["changes"], actor)
        audit_svc.record_from_request(request, "ontology_version_publish", project_id=project_id,
                                      tenant_id=_tenant_id(user),
                                      target_type="ontology", target_id=ontology_id,
                                      target_name=rec.get("name", ""),
                                      before={"version": base_version},
                                      after={"version": new_version, "changes": diff["changes"]})
        return success({"status": "published", "version": new_version,
                        "base_version": base_version, "diff": diff,
                        "ontology": updated}, http_status=201)

    # 不兼容变更：必须升大版本（TCE-07-02 拦截点）
    target = (req.target_version or "").strip()
    breaking = [c for c in diff["changes"] if not c["compatible"]]
    try:
        t_major, _t_minor = sd.parse_version(target) if target else (0, 0)
    except sd.VersionError:
        t_major = 0
    b_major, _b_minor = sd.parse_version(base_version)
    if not target or t_major <= b_major:
        raise ApiError(
            "INCOMPATIBLE_REQUIRES_MAJOR",
            f"检测到 {len(breaking)} 项不兼容变更，必须升大版本（当前 {base_version}，须 ≥ {sd.bump_major(base_version)}）并走审批",
            http_status=422,
            details={"breaking_changes": breaking, "base_version": base_version,
                     "suggested_target_version": sd.bump_major(base_version)},
        )
    if any(v["version"] == sd.norm_version(target) for v in rec.get("versions") or []):
        raise ApiError("VALIDATION_ERROR", f"版本 {sd.norm_version(target)} 已存在", http_status=422)

    cs = _cs_store(project_id).insert({
        "id": uuid.uuid4().hex,
        "ontology_id": ontology_id,
        "ontology_name": rec.get("name", ""),
        "base_version": base_version,
        "target_version": sd.norm_version(target),
        "model": copy.deepcopy(model),
        "note": req.note,
        "diff": diff,
        "status": "pending",
        "submitted_by": actor,
        "submitted_at": _now(),
    })
    # supersedes：旧实例归档（TC-07-02 ④ 重新提交；在新单落地后才归档，避免失败残留）
    if req.supersedes:
        cs_store = _cs_store(project_id)
        old = cs_store.get(req.supersedes)
        if not old or old.get("ontology_id") != ontology_id:
            raise ApiError("NOT_FOUND", "supersedes 指定的审批单不存在", http_status=404)
        if old.get("status") == "pending":
            raise ApiError("VALIDATION_ERROR", "被取代的审批单仍待审批，请先撤回", http_status=409)
        if old.get("status") not in ("rejected", "withdrawn", "abandoned"):
            raise ApiError("VALIDATION_ERROR", "仅 rejected/withdrawn/abandoned 的审批单可被取代归档",
                           http_status=422)
        cs_store.update(old["id"], {"status": "archived", "archived_at": _now(),
                                    "archived_reason": f"superseded by {cs['id']}"})
        audit_svc.record_from_request(request, "changeset_archived", project_id=project_id,
                                      tenant_id=_tenant_id(user),
                                      target_type="changeset", target_id=old["id"],
                                      target_name=rec.get("name", ""),
                                      before={"status": old.get("status")}, after={"status": "archived"},
                                      detail={"ontology_id": ontology_id, "superseded_by": cs["id"]})
    audit_svc.record_from_request(request, "changeset_submit", project_id=project_id,
                                  tenant_id=_tenant_id(user),
                                  target_type="changeset", target_id=cs["id"],
                                  target_name=rec.get("name", ""),
                                  before={"version": base_version},
                                  after={"status": "pending", "target_version": cs["target_version"],
                                         "changes": diff["changes"]},
                                  detail={"ontology_id": ontology_id, "breaking": len(breaking)})
    logger.info("changeset_submit id=%s ontology=%s target=%s breaking=%d",
                cs["id"], ontology_id, cs["target_version"], len(breaking))
    return success({"status": "pending", "changeset_id": cs["id"], "diff": diff,
                    "base_version": base_version, "target_version": cs["target_version"]},
                   http_status=201)


# --------------------------------------------------------------------------- #
# 变更单查询 / 状态迁移
# --------------------------------------------------------------------------- #
@router.get("/api/ontologies/{ontology_id}/changesets")
def list_ontology_changesets(ontology_id: str, request: Request,
                             status: Optional[str] = Query(default=None),
                             project_id: str = Depends(project_id_param)):
    rec = _require_ontology(project_id, ontology_id)
    ensure_schema_visible(request, project_id, rec)
    items = [c for c in _cs_store(project_id).all() if c.get("ontology_id") == ontology_id]
    if status:
        items = [c for c in items if c.get("status") == status]
    items.sort(key=lambda c: c.get("submitted_at", ""), reverse=True)
    return success({"items": items, "total": len(items)})


@router.get("/api/changesets")
def list_changesets(request: Request,
                    status: Optional[str] = Query(default=None),
                    project_id: str = Depends(project_id_param)):
    ensure_project_access(request, project_id)
    items = _cs_store(project_id).all()
    if status:
        items = [c for c in items if c.get("status") == status]
    items.sort(key=lambda c: c.get("submitted_at", ""), reverse=True)
    return success({"items": items, "total": len(items)})


@router.get("/api/changesets/{changeset_id}")
def get_changeset(changeset_id: str, request: Request,
                  project_id: str = Depends(project_id_param)):
    ensure_project_access(request, project_id)
    cs = _cs_store(project_id).get(changeset_id)
    if not cs:
        raise ApiError("NOT_FOUND", "审批单不存在", http_status=404)
    return success(cs)


def _review_allowed(request: Request, project_id: str, cs: dict[str, Any]) -> str:
    """审批人判定：review 动作（admin/reviewer 或 grant）＋非提交人本人＋schema 可见。"""
    import auth as auth_svc
    if not auth_svc.auth_enabled():
        return "-"
    user = current_user(request)
    if user is None:
        raise ApiError("UNAUTHORIZED", "未登录", http_status=401)
    rec = _require_ontology(project_id, cs["ontology_id"])
    ensure_schema_visible(request, project_id, rec)
    actions = auth_svc.effective_actions(user, project_id, cs["ontology_id"])
    if "review" not in actions:
        audit_svc.record_from_request(request, "changeset_review_denied", project_id=project_id,
                                      tenant_id=_tenant_id(user),
                                      target_type="changeset", target_id=cs["id"],
                                      detail={"reason": "no_review_action", "role": user.get("role")})
        raise ApiError("FORBIDDEN", "无审批权限（需 review 角色/授权）", http_status=403)
    if user.get("username") == cs.get("submitted_by"):
        audit_svc.record_from_request(request, "changeset_review_denied", project_id=project_id,
                                      tenant_id=_tenant_id(user),
                                      target_type="changeset", target_id=cs["id"],
                                      detail={"reason": "self_approval"})
        raise ApiError("FORBIDDEN", "提交人不能审批自己的变更单（TC-07-08 不可代签）", http_status=403)
    return user.get("username", "-")


def _do_transition(request: Request, cs_id: str, project_id: str, act: str,
                   reason: str = "") -> dict[str, Any]:
    """锁内状态迁移（TCE-07-03 幂等：已终态直接返回当前记录）。"""
    with _CS_LOCK:
        store = _cs_store(project_id)
        cs = store.get(cs_id)
        if not cs:
            raise ApiError("NOT_FOUND", "审批单不存在", http_status=404)
        if cs.get("status") != "pending":
            audit_svc.record_from_request(request, "changeset_transition_ignored", project_id=project_id,
                                          target_type="changeset", target_id=cs_id,
                                          detail={"act": act, "current_status": cs.get("status")})
            return {**cs, "_idempotent": True}
        rec = _require_ontology(project_id, cs["ontology_id"])
        if act in ("approve", "reject"):
            approver = _review_allowed(request, project_id, cs)
            if act == "approve":
                # 基线漂移：提交后基线已变 → 须基于最新基线重提
                _base_model, base_version = _published_base(rec)
                if base_version != cs.get("base_version"):
                    raise ApiError("BASE_DRIFT",
                                   f"基线版本已变更（{cs.get('base_version')} → {base_version}），请基于最新基线重新提交",
                                   http_status=409)
                updated = _apply_version(rec, project_id, cs["model"], cs["target_version"],
                                         cs.get("note", ""), cs.get("diff", {}).get("changes", []),
                                         approver)
                patch = {"status": "approved", "approved_by": approver, "approved_at": _now(),
                         "published_version": cs["target_version"]}
                audit_svc.record_from_request(request, "changeset_approve", project_id=project_id,
                                              target_type="changeset", target_id=cs_id,
                                              target_name=rec.get("name", ""),
                                              before={"status": "pending", "version": base_version},
                                              after={"status": "approved", "version": cs["target_version"]},
                                              detail={"ontology_id": cs["ontology_id"], "approver": approver})
                logger.info("changeset_approve id=%s ontology=%s version=%s by=%s",
                            cs_id, cs["ontology_id"], cs["target_version"], approver)
            else:
                if not reason:
                    raise ApiError("VALIDATION_ERROR", "驳回必须填写理由（TC-07-02）", http_status=422)
                patch = {"status": "rejected", "rejected_by": approver,
                         "rejected_at": _now(), "reject_reason": reason}
                audit_svc.record_from_request(request, "changeset_reject", project_id=project_id,
                                              target_type="changeset", target_id=cs_id,
                                              target_name=rec.get("name", ""),
                                              before={"status": "pending"}, after={"status": "rejected"},
                                              detail={"ontology_id": cs["ontology_id"],
                                                      "approver": approver, "reason": reason})
        else:  # withdraw / abandon：仅提交人
            user = current_user(request)
            actor = _actor(user)
            if auth_enabled() and user and user.get("username") != cs.get("submitted_by"):
                raise ApiError("FORBIDDEN", "仅提交人可撤回/放弃", http_status=403)
            patch = {
                "withdraw": {"status": "withdrawn", "withdrawn_by": actor, "withdrawn_at": _now()},
                "abandon": {"status": "abandoned", "abandoned_by": actor, "abandoned_at": _now()},
            }[act]
            audit_svc.record_from_request(request, f"changeset_{act}", project_id=project_id,
                                          target_type="changeset", target_id=cs_id,
                                          target_name=rec.get("name", ""),
                                          before={"status": "pending"}, after={"status": patch["status"]},
                                          detail={"ontology_id": cs["ontology_id"], "actor": actor})
        return store.update(cs_id, patch)


def auth_enabled() -> bool:
    import auth as auth_svc
    return auth_svc.auth_enabled()


@router.post("/api/changesets/{changeset_id}/approve")
def approve_changeset(changeset_id: str, request: Request,
                      project_id: str = Depends(project_id_param)):
    ensure_project_access(request, project_id)
    return success(_do_transition(request, changeset_id, project_id, "approve"))


@router.post("/api/changesets/{changeset_id}/reject")
def reject_changeset(changeset_id: str, req: RejectBody, request: Request,
                     project_id: str = Depends(project_id_param)):
    ensure_project_access(request, project_id)
    return success(_do_transition(request, changeset_id, project_id, "reject", req.reason))


@router.post("/api/changesets/{changeset_id}/withdraw")
def withdraw_changeset(changeset_id: str, request: Request,
                       project_id: str = Depends(project_id_param)):
    ensure_project_access(request, project_id)
    return success(_do_transition(request, changeset_id, project_id, "withdraw"))


@router.post("/api/changesets/{changeset_id}/abandon")
def abandon_changeset(changeset_id: str, request: Request,
                      project_id: str = Depends(project_id_param)):
    ensure_project_access(request, project_id)
    return success(_do_transition(request, changeset_id, project_id, "abandon"))


# --------------------------------------------------------------------------- #
# 版本固定引用（TC-07-06）＋ 属性弃用（TC-07-05）
# --------------------------------------------------------------------------- #
@router.get("/api/ontologies/{ontology_id}/versions")
def list_versions(ontology_id: str, request: Request,
                  project_id: str = Depends(project_id_param)):
    rec = _require_ontology(project_id, ontology_id)
    ensure_schema_visible(request, project_id, rec)
    _base_model, base_version = _published_base(rec)
    items = [{k: v for k, v in ver.items() if k != "model"} for ver in (rec.get("versions") or [])]
    if not items:
        items = [{"version": base_version, "published_at": rec.get("created_at"),
                  "published_by": rec.get("owner", "-"), "note": "（存量基线）", "changes": []}]
    return success({"current": base_version, "items": list(reversed(items))})


@router.get("/api/ontologies/{ontology_id}/versions/{version}")
def get_version(ontology_id: str, version: str, request: Request,
                project_id: str = Depends(project_id_param)):
    rec = _require_ontology(project_id, ontology_id)
    ensure_schema_visible(request, project_id, rec)
    for ver in rec.get("versions") or []:
        if ver.get("version") == version:
            return success({"version": version, "published_at": ver.get("published_at"),
                            "published_by": ver.get("published_by"),
                            "model": ver.get("model")})
    _base_model, base_version = _published_base(rec)
    if version == base_version and not (rec.get("versions") or []):
        return success({"version": version, "published_at": rec.get("created_at"),
                        "published_by": rec.get("owner", "-"), "model": _model_of(rec)})
    raise ApiError("NOT_FOUND", f"版本 {version} 不存在", http_status=404)


@router.post("/api/ontologies/{ontology_id}/deprecate-attribute")
def deprecate_attribute(ontology_id: str, req: AttributeDeprecate, request: Request,
                        project_id: str = Depends(project_id_param)):
    """标记属性 @deprecated（兼容变更：升小版本直接发布）。宽限期内可读，期满前禁止真删。"""
    rec = _require_ontology(project_id, ontology_id)
    user = ensure_schema_write(request, project_id, rec)
    if rec.get("status") != "active":
        raise ApiError("VALIDATION_ERROR", "仅已发布（active）本体可标记弃用", http_status=422)
    try:
        until_dt = datetime.fromisoformat(req.until)
    except ValueError:
        raise ApiError("VALIDATION_ERROR", "until 须为 ISO 日期/时间", http_status=422)
    now_dt = datetime.now(timezone.utc)
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=timezone.utc)
    if until_dt <= now_dt:
        raise ApiError("VALIDATION_ERROR", "宽限期终点须晚于当前时间", http_status=422)

    model = _model_of(rec)
    obj = next((o for o in model.get("objects") or [] if o.get("id") == req.object_id), None)
    if not obj:
        raise ApiError("NOT_FOUND", "对象不存在", http_status=404)
    attr = next((a for a in obj.get("attributes") or [] if a.get("id") == req.attribute_id), None)
    if not attr:
        raise ApiError("NOT_FOUND", "属性不存在", http_status=404)
    _base_model, base_version = _published_base(rec)
    attr["deprecated"] = {"until": until_dt.isoformat(), "note": req.note,
                          "since_version": base_version, "deprecated_at": _now()}
    updated = _apply_version(rec, project_id, model, sd.bump_minor(base_version),
                             f"弃用属性 {req.attribute_id}",
                             [{"kind": "attribute_deprecated", "compatible": True,
                               "detail": f"@deprecated 至 {until_dt.isoformat()}",
                               "object_id": req.object_id, "attribute_id": req.attribute_id}],
                             _actor(user))
    audit_svc.record_from_request(request, "ontology_attribute_deprecate", project_id=project_id,
                                  tenant_id=_tenant_id(user),
                                  target_type="attribute", target_id=req.attribute_id,
                                  target_name=attr.get("name", ""),
                                  before={"deprecated": attr.get("deprecated")},
                                  after={"deprecated": attr["deprecated"]},
                                  detail={"ontology_id": ontology_id, "object_id": req.object_id})
    return success({"deprecated": attr["deprecated"], "version": updated.get("version")},
                   http_status=200)


# 供 router_ontologies / mcp_server 复用的版本解析（TC-07-06 固定引用）
def resolve_versioned(rec: dict[str, Any], version: Optional[str]) -> dict[str, Any]:
    """按 version 参数返回（记录, 模型覆盖 dict）。缺省=当前；指定→快照。"""
    if not version:
        return rec, {}
    for ver in rec.get("versions") or []:
        if ver.get("version") == version:
            return rec, {k: v for k, v in ver.get("model", {}).items()}
    _base_model, base_version = _published_base(rec)
    if version == base_version:
        return rec, _model_of(rec)
    raise ApiError("NOT_FOUND", f"版本 {version} 不存在", http_status=404)
