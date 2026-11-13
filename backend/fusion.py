"""V2-02 跨本体对齐与融合 — 融合部分（M4-A③）：融合申请/会签/执行、虚拟融合查询、版本管理。

验收面（docs/V2-测试大纲.md TP-02）：
- TP-02-01/02/11 虚拟融合查询：等价声明+键对齐的快照 join、内容分渠道获取
  （源连接=联邦取、上传=本地快照，复用 content_access）、合并去重；
  同键一条合并记录（双来源标记）、异键多条；冲突双值并存/权威源取值，不静默覆盖。
- TP-02-03 负向：融合不改原本体快照（源本体模型只读）、源连接内容永远源端取、
  融合查询纯只读（不写实例/不写快照）。
- TP-02-05 融合本体版本管理：历史可查、语义 diff 对比、回滚到任一历史版本、
  消费方固定引用指定版本（复用 M2-E versions 机制；回滚/diff 端点在 router_changesets）。

设计决策：
- 融合申请复用对齐声明状态机（pending/approved/rejected/withdrawn、一票否决、
  owner 缺位时租户管理员可代行）。会签方 = 全部源本体 owner（含发起人本人，
  同 owner 即签即生效，与对齐声明同口径）。
- 融合本体是「派生本体」：会签通过后可执行（execute）生成新本体（带 fusion 标记），
  原本体不可变；重复执行=以源本体当前状态重建融合模型并产生新版本（无变化则不变）。
- 等价映射（object_groups）默认由已生效对齐声明自动推导，也可显式提供；
  映射调整（mappings）本身即版本演化（已物化则立即产生新版本）。
- 融合查询以（可固定版本的）融合本体 TBox 为 join 依据：对象带 fusion.sources 标记、
  属性带 fusion_sources 标记，查询据此跨源取数合并，全程只读。
"""
import copy
import hashlib
import json
from datetime import datetime

import auth
import content_access
import tenant
from core.instance_store import get_instance_store
from core.responses import ApiError
from core.storage import JsonStore
from modeling_translator import blocking_violations
from api.router_changesets import _apply_version, _published_base, resolve_versioned
from api.router_projects import get_project_store
import schemadiff as sd
import audit as audit_svc

DECL_STATUS_FLOW = {
    "pending": ["approved", "rejected", "withdrawn"],
    "approved": ["withdrawn"],
}


class FusionError(Exception):
    """融合服务层业务错误（code/message/http_status），REST 层转 ApiError。"""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _store(project_id: str) -> JsonStore:
    return JsonStore("fusion_requests", project_id)


def _decl_store(project_id: str) -> JsonStore:
    return JsonStore("alignment_declarations", project_id)


def _ont_store(project_id: str) -> JsonStore:
    return JsonStore("ontologies", project_id)


def _require_ontology(project_id: str, ontology_id: str) -> dict:
    rec = _ont_store(project_id).get(ontology_id)
    if not rec:
        raise FusionError("NOT_FOUND", f"本体 {ontology_id} 不存在", 404)
    return rec


def published_ontology_ids(project_id: str) -> list[str]:
    """MCP 通道访问边界＝已发布态（与 mcp_server 口径一致）。"""
    return [o["id"] for o in _ont_store(project_id).all() if o.get("status") == "active" and o.get("id")]


def _short(ontology_id: str) -> str:
    return (ontology_id or "")[:8]


# ---------------------------------------------------------------------------
# 等价映射（object_groups）：规范结构
# ---------------------------------------------------------------------------

def _find_object(ontology: dict, object_id: str) -> dict:
    for o in ontology.get("objects") or []:
        if o.get("id") == object_id:
            return o
    return {}


def _normalize_group(project_id: str, group_id: str, canonical_name: str,
                     objects: list[tuple[str, str]],
                     attr_pairs: list[list[tuple[str, str]]],
                     authoritative: dict[str, str] | None = None) -> dict:
    """把「对象引用列表＋属性对列表」规范化为 object_group 结构。

    objects[i] = (ontology_id, object_id)；objects[0] 为本组的「a 侧」（canonical 名/ID 基准）。
    attr_pairs[j] = [(ontology_id, attr_id), ...]（≥2 为跨源等价对；单元素=单源属性）。
    authoritative: {canonical_id: ontology_id} 权威源（来自已生效声明的冲突裁决）。
    """
    source_list = []
    for ontology_id, object_id in objects:
        ont = _require_ontology(project_id, ontology_id)
        obj = _find_object(ont, object_id)
        if not obj:
            raise FusionError("BAD_MAPPING", f"本体 {ontology_id} 下无对象 {object_id}", 422)
        source_list.append({
            "ontology_id": ontology_id,
            "object_id": object_id,
            "object_name": obj.get("name") or object_id,
            "stable_key": obj.get("stable_key"),
        })

    canon: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _push(canonical_id: str, canonical_name: str, refs: list[tuple[str, str]]) -> None:
        entries = []
        for ontology_id, attr_id in refs:
            obj = next((s for s in source_list if s["ontology_id"] == ontology_id), None)
            src_obj = _find_object(_require_ontology(project_id, ontology_id), obj["object_id"]) if obj else {}
            attr = next((a for a in src_obj.get("attributes") or [] if a.get("id") == attr_id), None)
            if not attr:
                raise FusionError("BAD_MAPPING",
                                  f"本体 {ontology_id} 对象 {obj['object_id'] if obj else '?'} 下无属性 {attr_id}", 422)
            entries.append({"ontology_id": ontology_id, "attr_id": attr_id,
                            "attr_name": attr.get("name") or attr_id})
            seen.add((ontology_id, attr_id))
        canon.append({"canonical_id": canonical_id, "canonical_name": canonical_name, "attrs": entries})

    for pair in attr_pairs:
        refs = list(pair)
        base = refs[0]
        if len(refs) >= 2:
            cid = "canon_" + base[1]
        else:
            cid = "src_%s_%s" % (_short(base[0]), base[1])
        name_obj = _find_object(_require_ontology(project_id, base[0]),
                                dict(objects)[base[0]])
        attr = next((a for a in name_obj.get("attributes") or [] if a.get("id") == base[1]), None)
        _push(cid, (attr.get("name") if attr else None) or base[1], refs)

    # 未参与任何映射对的源属性 → 各自单源 canonical 属性（不合并、不冲突）
    for ontology_id, object_id in objects:
        obj = _find_object(_require_ontology(project_id, ontology_id), object_id)
        for attr in obj.get("attributes") or []:
            if (ontology_id, attr.get("id")) in seen:
                continue
            cid = "src_%s_%s" % (_short(ontology_id), attr.get("id"))
            _push(cid, attr.get("name") or attr.get("id"), [(ontology_id, attr.get("id"))])

    return {
        "group_id": group_id,
        "canonical_name": canonical_name or source_list[0]["object_name"],
        "objects": source_list,
        "attribute_mappings": canon,
        "authoritative": {k: v for k, v in (authoritative or {}).items()},
    }


def _group_key(objects: list[tuple[str, str]]) -> frozenset:
    return frozenset(objects)


def derive_object_groups(project_id: str, source_ids: list[str]) -> list[dict]:
    """从已生效对齐声明自动推导等价映射（覆盖任意源本体对，忽略方向）。"""
    pairs_by: dict[frozenset, list[dict]] = {}
    for decl in _decl_store(project_id).all():
        if decl.get("status") != "approved":
            continue
        pair = frozenset({decl.get("ontology_a"), decl.get("ontology_b")})
        if len(pair) == 2:
            pairs_by.setdefault(pair, []).append(decl)

    groups: list[dict] = []
    used: set[frozenset] = set()
    for i, a in enumerate(source_ids):
        for b in source_ids[i + 1:]:
            for decl in pairs_by.get(frozenset({a, b}), []):
                for idx, op in enumerate(decl.get("object_pairs") or []):
                    obj_a = (decl.get("ontology_a"), op.get("object_a"))
                    obj_b = (decl.get("ontology_b"), op.get("object_b"))
                    key = _group_key([obj_a, obj_b])
                    if key in used:
                        continue
                    used.add(key)
                    # 归一到源列表顺序：a 侧＝source_ids 中靠前者（swap 时属性对同步换边，
                    # 冲突裁决按「组 a 侧」重映射，canonical ID 基准＝组 a 侧属性）
                    swap = (source_ids.index(decl.get("ontology_a"))
                            > source_ids.index(decl.get("ontology_b")))
                    if swap:
                        obj_a, obj_b = obj_b, obj_a
                    attr_pairs = []
                    for ap in op.get("attribute_pairs") or []:
                        g_attr_a = ap.get("attr_b") if swap else ap.get("attr_a")
                        g_attr_b = ap.get("attr_a") if swap else ap.get("attr_b")
                        ref_a = (obj_a[0], g_attr_a)
                        ref_b = (obj_b[0], g_attr_b)
                        if ref_a[1] and ref_b[1]:
                            attr_pairs.append([ref_a, ref_b])
                    # 冲突裁决 → 权威源（属性级；canonical ID 基准在组 a 侧属性）
                    authoritative: dict[str, str] = {}
                    for ap in op.get("attribute_pairs") or []:
                        g_attr_a = ap.get("attr_b") if swap else ap.get("attr_a")
                        g_attr_b = ap.get("attr_a") if swap else ap.get("attr_b")
                        for c in decl.get("conflicts") or []:
                            if c.get("resolution") not in ("a", "b"):
                                continue
                            if {c.get("attr_a"), c.get("attr_b")} != {g_attr_a, g_attr_b}:
                                continue
                            # resolution "a"/"b" 是声明坐标系；换算到组坐标系
                            decl_a_wins = (c.get("resolution") == "a")
                            group_a_wins = (decl_a_wins != swap)
                            authoritative["canon_" + g_attr_a] = (
                                obj_a[0] if group_a_wins else obj_b[0])
                    groups.append(_normalize_group(
                        project_id,
                        f"g_{decl.get('id', '')[:8]}_{idx}",
                        _find_object(_require_ontology(project_id, obj_a[0]), obj_a[1]).get("name") or "",
                        [obj_a, obj_b], attr_pairs, authoritative))
    return groups


def normalize_user_groups(project_id: str, source_ids: list[str],
                          raw_groups: list[dict]) -> list[dict]:
    """规范化用户显式提供的等价映射（objects/attribute_pairs 为 ontology_id+id 引用）。"""
    groups = []
    for idx, raw in enumerate(raw_groups or []):
        objects = [(o.get("ontology_id"), o.get("object_id")) for o in raw.get("objects") or []]
        if len(objects) < 2 or len(set(objects)) != len(objects):
            raise FusionError("BAD_MAPPING", "每个等价组至少包含 2 个不同源对象", 422)
        for ontology_id, _ in objects:
            if ontology_id not in source_ids:
                raise FusionError("BAD_MAPPING", f"对象来源本体 {ontology_id} 不在源本体列表内", 422)
        attr_pairs = []
        for pair in raw.get("attribute_pairs") or []:
            refs = [(r.get("ontology_id"), r.get("attr_id")) for r in (pair or [])]
            refs = [r for r in refs if r[0] and r[1]]
            if len(refs) >= 2:
                attr_pairs.append(refs)
        canonical = raw.get("canonical_name") or ""
        groups.append(_normalize_group(project_id, f"g_u{idx}", canonical,
                                       objects, attr_pairs, None))
    return groups


# ---------------------------------------------------------------------------
# 融合申请（会签状态机复用对齐声明：pending→approved/rejected，一票否决）
# ---------------------------------------------------------------------------

def _require_fusion(project_id: str, fusion_id: str) -> dict:
    rec = _store(project_id).get(fusion_id)
    if not rec:
        raise FusionError("NOT_FOUND", f"融合申请 {fusion_id} 不存在", 404)
    return rec


def _sources_visible(project_id: str, user: dict, source_ids: list[str]) -> list[dict]:
    """REST 通道：全部源本体须对当前用户可见；返回本体记录。"""
    project = get_project_store().get(project_id)
    recs = []
    for ontology_id in source_ids:
        rec = _require_ontology(project_id, ontology_id)
        if not tenant.schema_visible(user, project, rec):
            raise FusionError("NOT_FOUND", f"源本体 {ontology_id} 不可见", 404)
        recs.append(rec)
    return recs


def create_fusion_request(request, project_id: str, user: dict, name: str | None,
                          source_ids: list[str],
                          raw_groups: list[dict] | None = None) -> dict:
    tenant.ensure_project_access(request, project_id)
    source_ids = list(dict.fromkeys(source_ids or []))
    if len(source_ids) < 2:
        raise ApiError("BAD_REQUEST", "至少选择 2 个源本体进行融合", 422)
    recs = _sources_visible(project_id, user, source_ids)
    for rec in recs:
        if rec.get("status") != "active":
            raise ApiError("SOURCE_NOT_PUBLISHED", f"源本体《{rec.get('name')}》未发布（active），暂不可作为融合源", 422)

    store = _store(project_id)
    existing = store.all()
    for r in existing:
        if sorted(r.get("source_ontologies") or []) == sorted(source_ids) and r.get("status") in ("pending", "approved"):
            raise ApiError("FUSION_IN_PROGRESS", f"已存在同源的融合申请（{r.get('status')}），请勿重复发起", 409)

    if raw_groups:
        groups = normalize_user_groups(project_id, source_ids, raw_groups)
    else:
        groups = derive_object_groups(project_id, source_ids)
    if not groups:
        raise ApiError("NO_MAPPING", "未找到可用的等价映射：请先完成源本体间的对齐声明并会签生效，或显式提供 object_groups", 422)

    # 会签方：全部源本体 owner（含发起人本人——同 owner 即签即生效，与对齐声明同口径；
    # owner 缺位可由租户管理员代行）
    usernames = {u.get("username") for u in auth.list_users() if u.get("username")}
    seen_signers: set[tuple[str, str]] = set()
    signers = []
    for ontology_id in source_ids:
        rec = _require_ontology(project_id, ontology_id)
        owner = rec.get("owner") or ""
        if not owner or (owner, ontology_id) in seen_signers:
            continue
        seen_signers.add((owner, ontology_id))
        signers.append({
            "owner": owner,
            "ontology_id": ontology_id,
            "objects": [s["object_id"] for g in groups for s in g["objects"] if s["ontology_id"] == ontology_id],
            "present": bool(owner) and owner in usernames,
        })
    rec = {
        "project_id": project_id,
        "name": (name or "").strip() or "融合本体（" + "、".join(r.get("name") or s for r, s in zip(recs, source_ids)) + "）",
        "source_ontologies": source_ids,
        "object_groups": groups,
        "status": "pending",
        "signers_required": signers,
        "signatures": [],
        "target_ontology_id": None,
        "reject_reason": None,
        "executed_at": None,
        "executed_by": None,
        "submitted_by": user.get("username") or "",
        "submitted_at": now_iso(),
    }
    created = store.insert(rec)
    audit_svc.record_from_request(request, "fusion_request_submit",
                                  project_id=project_id,
                                  target_type="fusion_request", target_id=created["id"],
                                  target_name=created["name"],
                                  detail={"source_ontologies": source_ids, "groups": len(groups)})
    return created


def list_fusion_requests(project_id: str, user: dict,
                         status: str | None = None) -> list[dict]:
    items = []
    for r in _store(project_id).all():
        if status and r.get("status") != status:
            continue
        try:
            _sources_visible(project_id, user, r.get("source_ontologies") or [])
        except FusionError:
            continue
        items.append(r)
    items.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    return items


def get_fusion_request(project_id: str, user: dict, fusion_id: str) -> dict:
    rec = _require_fusion(project_id, fusion_id)
    _sources_visible(project_id, user, rec.get("source_ontologies") or [])
    return rec


def _check_signer(rec: dict, user: dict, proxy_reason: str | None) -> tuple[dict, bool]:
    username = user.get("username") or ""
    proxy = False
    for s in rec.get("signers_required") or []:
        if s.get("owner") == username:
            return s, False
    # owner 缺位：租户管理员代行（对齐声明同款规则）
    if not proxy_reason and not tenant.is_admin(user):
        raise FusionError("NOT_A_SIGNER", "您不在会签方中（如需代行须租户管理员并提供代行理由）", 403)
    absent = [s for s in rec.get("signers_required") or [] if not s.get("present")]
    if not absent:
        raise FusionError("NOT_A_SIGNER", "您不是本申请的任何源本体 owner", 403)
    return absent[0], True


def sign_fusion_request(request, project_id: str, user: dict, fusion_id: str,
                        decision: str, comment: str | None = None,
                        proxy_reason: str | None = None) -> dict:
    tenant.ensure_project_access(request, project_id)
    if decision not in ("approve", "reject"):
        raise ApiError("BAD_DECISION", "decision 只能是 approve 或 reject", 422)
    rec = _require_fusion(project_id, fusion_id)
    if rec.get("status") != "pending":
        raise ApiError("FUSION_NOT_PENDING", f"当前状态 {rec.get('status')} 不可会签", 409)
    signer, proxy = _check_signer(rec, user, proxy_reason)
    signatures = list(rec.get("signatures") or [])
    signatures = [s for s in signatures if not (s.get("owner") == signer["owner"] and not s.get("proxy_reason"))]
    sig = {
        "owner": signer["owner"],
        "ontology_id": signer["ontology_id"],
        "decision": decision,
        "comment": (comment or "").strip() or None,
        "signed_at": now_iso(),
        "role": user.get("role") or "member",
        "proxy_reason": (proxy_reason or "").strip() or None,
    }
    signatures.append(sig)
    patch = {"signatures": signatures, "updated_at": now_iso()}
    audit_svc.record_from_request(request, "fusion_request_sign",
                                  project_id=project_id,
                                  target_type="fusion_request", target_id=fusion_id,
                                  target_name=rec.get("name"),
                                  detail={"decision": decision, "owner": signer["owner"],
                                          "proxy": proxy, "proxy_reason": proxy_reason})
    if decision == "reject":
        patch["status"] = "rejected"
        patch["reject_reason"] = comment or signer["owner"] + " 否决"
        updated = _store(project_id).update(fusion_id, patch)
        audit_svc.record_from_request(request, "fusion_request_reject",
                                      project_id=project_id,
                                      target_type="fusion_request", target_id=fusion_id,
                                      target_name=rec.get("name"),
                                      detail={"by": signer["owner"], "reason": patch["reject_reason"]})
        return updated

    done = {s.get("owner") or s.get("signed_for") for s in signatures if s.get("decision") == "approve"}
    if all((s.get("owner") or "") in done for s in rec.get("signers_required") or []):
        patch["status"] = "approved"
        audit_svc.record_from_request(request, "fusion_request_approve",
                                      project_id=project_id,
                                      target_type="fusion_request", target_id=fusion_id,
                                      target_name=rec.get("name"),
                                      detail={"signatures": len(signatures)})
    return _store(project_id).update(fusion_id, patch)


def withdraw_fusion_request(request, project_id: str, user: dict, fusion_id: str) -> dict:
    tenant.ensure_project_access(request, project_id)
    rec = _require_fusion(project_id, fusion_id)
    username = user.get("username") or ""
    if username != rec.get("submitted_by") and not tenant.is_admin(user):
        raise FusionError("FORBIDDEN", "仅发起人或租户管理员可撤回", 403)
    if rec.get("status") not in ("pending", "approved"):
        raise ApiError("FUSION_NOT_WITHDRAWABLE", f"当前状态 {rec.get('status')} 不可撤回", 409)
    updated = _store(project_id).update(fusion_id, {
        "status": "withdrawn", "updated_at": now_iso(),
    })
    audit_svc.record_from_request(request, "fusion_request_withdraw",
                                  project_id=project_id,
                                  target_type="fusion_request", target_id=fusion_id,
                                  target_name=rec.get("name"))
    return updated


# ---------------------------------------------------------------------------
# 融合本体（派生本体）：构建 / 物化 / 重执行 / 映射调整
# ---------------------------------------------------------------------------

_VALID_DTYPES = ("boolean", "date", "datetime", "float", "integer", "string")


def build_fusion_model(project_id: str, rec: dict) -> dict:
    """按等价映射把源本体 TBox 合并为融合本体模型（纯计算，不写库）。

    - 等价组 → 融合对象（canonical 属性并集，属性带 fusion_sources 标记）；
    - 未映射对象 → 透传对象（对象/属性 ID 加前缀避免冲突，带 pass_through 标记）；
    - relations：仅保留两端都能解析到融合本体内对象的（端点重映射）。
    """
    sources = rec.get("source_ontologies") or []
    ont_recs = {oid: _require_ontology(project_id, oid) for oid in sources}
    groups = rec.get("object_groups") or []

    # 源 (ontology_id, object_id) → 融合对象 id
    source_to_fus: dict[tuple[str, str], str] = {}
    objects_out: list[dict] = []

    for g in groups:
        srcs = g.get("objects") or []
        if len(srcs) < 2:
            continue
        # 对象 ID 由源对象集合确定性生成（与顺序、group_id 表示无关）：跨重建稳定，
        # 版本 diff 才能按同对象配对，避免 ID 漂移造成假「对象增/删」
        sig = "+".join(f"{s.get('ontology_id')}:{s.get('object_id')}"
                       for s in sorted(srcs, key=lambda o: (o.get("ontology_id") or "",
                                                           o.get("object_id") or "")))
        fus_id = "fus_" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:10]
        attrs_out: list[dict] = []
        key_candidates: list[str] = []
        for am in g.get("attribute_mappings") or []:
            entries = am.get("attrs") or []
            src_objs = [next((s for s in srcs if s["ontology_id"] == e["ontology_id"]), None) for e in entries]
            attr_defs = []
            dtypes: list[str] = []
            enums: list = []
            required = False
            for e, s in zip(entries, src_objs):
                obj = _find_object(ont_recs[e["ontology_id"]], s["object_id"]) if s else {}
                attr = next((a for a in obj.get("attributes") or [] if a.get("id") == e["attr_id"]), {})
                attr_defs.append(attr)
                dt = attr.get("data_type") or "string"
                dtypes.append(dt if dt in _VALID_DTYPES else "string")
                required = required or bool(attr.get("required"))
                if attr.get("enum") is not None:
                    enums.append(list(attr.get("enum")))
                if obj.get("stable_key") == e["attr_id"]:
                    key_candidates.append(am["canonical_id"])
            data_type = dtypes[0] if dtypes and all(d == dtypes[0] for d in dtypes) else "string"
            a: dict = {
                "id": am["canonical_id"],
                "name": am.get("canonical_name") or am["canonical_id"],
                "data_type": data_type,
                "required": required,
                "fusion_sources": [{"ontology_id": e["ontology_id"], "attr_id": e["attr_id"]} for e in entries],
            }
            if enums and all(en == enums[0] for en in enums):
                a["enum"] = enums[0]
            attrs_out.append(a)
        # stable_key：优先取「源侧即各源对象 stable_key」的 canonical 属性
        stable_key = None
        if key_candidates:
            stable_key = key_candidates[0]
        else:
            for a in attrs_out:
                if a["required"] and a["data_type"] == "string":
                    stable_key = a["id"]
                    break
            if not stable_key:
                for a in attrs_out:
                    if a["data_type"] == "string":
                        stable_key = a["id"]
                        break
            if not stable_key and attrs_out:
                stable_key = attrs_out[0]["id"]
        objects_out.append({
            "id": fus_id,
            "name": g.get("canonical_name") or srcs[0]["object_name"],
            "namespace": "",
            "business_definition": "跨本体融合对象：" + (g.get("canonical_name") or srcs[0]["object_name"])
                + "（来源：" + "、".join(f"{ont_recs[s['ontology_id']].get('name', s['ontology_id'])}.{s['object_name']}"
                                         for s in srcs) + "）",
            "stable_key": stable_key,
            "attributes": attrs_out,
            "relations": [],
            "fusion": {"group_id": g.get("group_id"), "sources": [
                {"ontology_id": s["ontology_id"], "object_id": s["object_id"], "object_name": s["object_name"]}
                for s in srcs]},
        })
        for s in srcs:
            source_to_fus[(s["ontology_id"], s["object_id"])] = fus_id

    # 未映射对象 → 透传
    for oid in sources:
        for obj in ont_recs[oid].get("objects") or []:
            if (oid, obj.get("id")) in source_to_fus:
                continue
            short = _short(oid)
            pt_id = f"pt_{short}_{obj.get('id')}"
            attrs = []
            for a in obj.get("attributes") or []:
                aa = copy.deepcopy(a)
                aa["id"] = f"pt_{short}_{a.get('id')}"
                attrs.append(aa)
            objects_out.append({
                "id": pt_id,
                "name": obj.get("name"),
                "namespace": obj.get("namespace") or "",
                "business_definition": (obj.get("business_definition") or "").strip()
                    or f"透传自本体《{ont_recs[oid].get('name')}》的对象 {obj.get('name')}",
                "stable_key": obj.get("stable_key") and f"pt_{short}_{obj.get('stable_key')}",
                "attributes": attrs,
                "relations": [],
                "fusion": {"pass_through": {"ontology_id": oid, "object_id": obj.get("id")}},
            })
            source_to_fus[(oid, obj.get("id"))] = pt_id

    # relations：两端均可解析则保留并重映射
    for oid in sources:
        for obj in ont_recs[oid].get("objects") or []:
            fus = source_to_fus.get((oid, obj.get("id")))
            if not fus:
                continue
            target = next((o for o in objects_out if o["id"] == fus), None)
            if not target:
                continue
            for rel in obj.get("relations") or []:
                other = source_to_fus.get((oid, rel.get("object")))
                if not other or other == fus:
                    continue
                target["relations"].append({
                    **rel,
                    "object": other,
                    "label": rel.get("label") or f"{ont_recs[oid].get('name', oid)}:{rel.get('label')}",
                })

    constraints: list[dict] = []
    functions: list[dict] = []
    for oid in sources:
        for c in ont_recs[oid].get("constraints") or []:
            cc = copy.deepcopy(c)
            cc["id"] = f"fus_c_{_short(oid)}_{c.get('id')}"
            constraints.append(cc)
        for fn in ont_recs[oid].get("functions") or []:
            ff = copy.deepcopy(fn)
            ff["id"] = f"fus_f_{_short(oid)}_{fn.get('id')}"
            functions.append(ff)

    return {
        "name": rec.get("name") or "融合本体",
        "description": "跨本体融合（派生本体）：源本体不可变，融合视图与版本独立管理。源："
            + "、".join(ont_recs[o].get("name") or o for o in sources),
        "objects": objects_out,
        "constraints": constraints,
        "functions": functions,
    }


def _next_free_version(rec: dict, base_version: str, breaking: bool) -> str:
    v = sd.bump_major(base_version) if breaking else sd.bump_minor(base_version)
    existing = {ver.get("version") for ver in rec.get("versions") or []}
    while v in existing:
        v = sd.bump_minor(v)
    return v


def execute_fusion(request, project_id: str, user: dict, fusion_id: str,
                   note: str | None = None, materialize: bool = False) -> dict:
    """执行/重执行融合。

    TP-02-12：物化快照**默认关闭**（materialize=False 仅虚拟执行——校验模型并报告
    would_create/would_change，不产生任何物理本体）；显式 materialize=True 才物化，
    物化出的派生本体带显式"派生数据，非权威"标记（derived/authoritative/derived_note）。
    源本体变更后以 materialize=True 重执行 → 按 diff 产生新版本（append-only 重建）。
    """
    tenant.ensure_project_access(request, project_id)
    rec = _require_fusion(project_id, fusion_id)
    if rec.get("status") != "approved":
        raise ApiError("FUSION_NOT_APPROVED", f"当前状态 {rec.get('status')} 不可执行融合（须会签通过）", 409)
    username = user.get("username") or ""
    if username != rec.get("submitted_by") and not tenant.is_admin(user):
        raise FusionError("FORBIDDEN", "仅发起人或租户管理员可执行融合", 403)

    model = build_fusion_model(project_id, rec)
    if not model["objects"]:
        raise ApiError("EMPTY_MODEL", "融合模型为空：等价映射未覆盖任何对象", 422)
    violations = blocking_violations(model)
    if violations:
        raise ApiError("MODEL_INVALID", "融合模型未通过校验："
                       + "；".join(v.get("message", str(v)) for v in violations[:5]), 422)

    ont_store = _ont_store(project_id)
    store = _store(project_id)
    actor = username
    ont_id = rec.get("target_ontology_id")
    ont_rec = ont_store.get(ont_id) if ont_id else None

    if not materialize:
        # 虚拟执行：只读校验＋变更预演（不落盘、不产版本）
        if ont_rec is None:
            would_create, would_change = True, True
            base_version = None
        else:
            base_model, base_version = _published_base(ont_rec)
            would_create, would_change = False, bool(sd.diff_ontology(base_model, model)["changes"])
        audit_svc.record_from_request(request, "fusion_execute_virtual",
                                      project_id=project_id,
                                      target_type="fusion_request", target_id=fusion_id,
                                      target_name=rec.get("name"),
                                      detail={"materialize": False, "would_create": would_create,
                                              "would_change": would_change,
                                              "base_version": base_version})
        return {"materialized": False, "virtual": True,
                "would_create": would_create, "would_change": would_change,
                "base_version": base_version,
                "model": model,
                "note": "虚拟执行：未物化（TP-02-12 物化默认关闭）；"
                        "显式 materialize=true 才产生派生本体（派生数据，非权威）"}

    if not ont_rec:
        ont_rec = ont_store.insert({
            "project_id": project_id,
            "name": model["name"],
            "description": model["description"],
            "namespace": project_id,
            "objects": copy.deepcopy(model["objects"]),
            "constraints": copy.deepcopy(model["constraints"]),
            "functions": copy.deepcopy(model["functions"]),
            "status": "active",
            "owner": username,
            "sharing": "project",
            "shared_with": [],
            "version": None,
            "versions": [],
            "fusion": {
                "derived": True,
                "authoritative": False,
                "derived_note": "派生数据，非权威：物化融合快照，唯一权威来源为源本体；仅作只读查询，不作为权威数据回写",
                "materialized": True,
                "fusion_id": fusion_id,
                "source_ontologies": list(rec.get("source_ontologies") or []),
            },
            "created_at": now_iso(),
            "updated_at": now_iso(),
        })
        diff = sd.diff_ontology({}, model)
        updated = _apply_version(ont_rec, project_id, model, "1.0",
                                 f"初始融合（源本体：{'、'.join(rec.get('source_ontologies') or [])}）",
                                 diff["changes"], actor)
        ont_store.update(ont_rec["id"], updated)
        store.update(fusion_id, {"target_ontology_id": ont_rec["id"],
                                 "executed_at": now_iso(), "executed_by": actor})
        audit_svc.record_from_request(request, "fusion_execute",
                                      project_id=project_id,
                                      target_type="ontology", target_id=ont_rec["id"],
                                      target_name=ont_rec["name"],
                                      detail={"fusion_id": fusion_id, "version": "1.0", "initial": True})
        return {"ontology_id": ont_rec["id"], "version": "1.0", "initial": True, "changed": True,
                "materialized": True}

    base_model, base_version = _published_base(ont_rec)
    diff = sd.diff_ontology(base_model, model)
    if not diff["changes"]:
        return {"ontology_id": ont_rec["id"], "version": base_version, "changed": False,
                "initial": False, "materialized": True,
                "diff": {"compatible": True, "changes": []}}
    breaking = any(not c.get("compatible", True) for c in diff["changes"])
    version = _next_free_version(ont_rec, base_version, breaking)
    updated = _apply_version(ont_rec, project_id, model, version,
                             note or "重执行融合（按源本体当前状态重建）", diff["changes"], actor)
    ont_store.update(ont_rec["id"], updated)
    store.update(fusion_id, {"executed_at": now_iso(), "executed_by": actor})
    audit_svc.record_from_request(request, "fusion_execute",
                                  project_id=project_id,
                                  target_type="ontology", target_id=ont_rec["id"],
                                  target_name=ont_rec["name"],
                                  detail={"fusion_id": fusion_id, "version": version,
                                          "previous": base_version, "changes": len(diff["changes"])})
    return {"ontology_id": ont_rec["id"], "version": version, "changed": True, "initial": False,
            "materialized": True,
            "diff": {"compatible": diff["compatible"], "version_bump": diff.get("version_bump"),
                     "changes": diff["changes"]}}


def update_fusion_mappings(request, project_id: str, user: dict, fusion_id: str,
                           raw_groups: list[dict]) -> dict:
    """调整等价映射（pending/approved 均可；已物化则立即产生融合本体新版本）。"""
    tenant.ensure_project_access(request, project_id)
    rec = _require_fusion(project_id, fusion_id)
    if rec.get("status") not in ("pending", "approved"):
        raise ApiError("FUSION_MAPPING_LOCKED", f"当前状态 {rec.get('status')} 不可调整映射", 409)
    username = user.get("username") or ""
    if username != rec.get("submitted_by") and not tenant.is_admin(user):
        raise FusionError("FORBIDDEN", "仅发起人或租户管理员可调整映射", 403)
    groups = normalize_user_groups(project_id, rec.get("source_ontologies") or [], raw_groups or [])
    if not groups:
        raise ApiError("EMPTY_MAPPING", "等价映射不能为空", 422)
    store = _store(project_id)
    store.update(fusion_id, {"object_groups": groups, "updated_at": now_iso()})
    audit_svc.record_from_request(request, "fusion_mappings_update",
                                  project_id=project_id,
                                  target_type="fusion_request", target_id=fusion_id,
                                  target_name=rec.get("name"),
                                  detail={"groups": len(groups)})
    out = {"fusion_id": fusion_id, "groups": groups, "version_result": None}
    if rec.get("status") == "approved" and rec.get("target_ontology_id"):
        # 已物化场景：映射变更须落新版本（materialize 默认关闭仅影响未物化首执行）
        out["version_result"] = execute_fusion(request, project_id, user, fusion_id,
                                               note="等价映射调整（自动版本）", materialize=True)
    return out


# ---------------------------------------------------------------------------
# 虚拟融合查询（TP-02-01/02/11）：只读，不写实例、不写快照
# ---------------------------------------------------------------------------

def _fetch_source_values(project_id: str, rec: dict, user_id: str | None,
                         attr_ids: list[str]) -> tuple[dict, dict, str | None]:
    """单条源记录按渠道取数。返回 (values, meta, error)；取数失败不抛错，显式降级留痕。"""
    attr_ids = [a for a in dict.fromkeys(attr_ids) if a]
    if not attr_ids:
        return {}, {}, None
    if not content_access.is_federated(project_id, rec):
        # 上传/本地型：内容值落本地即当前值（不回源、非 stale）；stable_key 等标识值
        # 对上传型同样可本地展示（剥离语义仅对源连接型生效），只排除下划线元数据键
        inst = rec.get("instance") or {}
        local = {k: v for k, v in inst.items() if not str(k).startswith("_")}
        return {a: local.get(a) for a in attr_ids}, {"mode": "local"}, None
    try:
        values, meta = content_access.federate_values(project_id, rec, user_id=user_id, attr_ids=attr_ids)
        # meta.error 为联邦侧降级留痕（如 MCP 端点不可达），必须透传给上层
        return values, meta, meta.get("error")
    except ApiError as e:
        # 源端取数失败（如访客无凭据映射）：显式降级，绝不静默
        fb = rec.get("fallback_content") or {}
        values = {a: fb.get(a) for a in attr_ids if a in fb}
        return values, {"mode": "fallback", "fallback": "fallback_content"}, str(e)


def _distinct_count(values_map: dict) -> tuple[int, list]:
    seen: list = []
    for v in values_map.values():
        key = json_dumps_key(v)
        if key not in {json_dumps_key(s) for s in seen}:
            seen.append(v)
    return len(seen), seen


def json_dumps_key(v) -> str:
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(v)


def fusion_query(project_id: str, user: dict | None, *,
                 fusion_id: str | None = None,
                 fusion_ontology_id: str | None = None,
                 object_type: str | None = None,
                 stable_key_value: str | None = None,
                 version: str | None = None,
                 limit: int = 100) -> dict:
    """跨本体虚拟融合查询。

    user=None 表示 MCP/治理通道（访问边界=已发布态，治理身份取数）；
    user 非空为 REST 通道（全部源本体须对当前用户可见，访客身份取数）。
    全程只读：不写实例、不写快照、不改源本体（TP-02-03 负向面由测试断言）。
    """
    if not fusion_id and not fusion_ontology_id:
        raise FusionError("BAD_REQUEST", "须提供 fusion_id 或 fusion_ontology_id", 422)
    store = _store(project_id)
    if fusion_id:
        req = _require_fusion(project_id, fusion_id)
    else:
        req = next((r for r in store.all() if r.get("target_ontology_id") == fusion_ontology_id), None)
        if not req:
            raise FusionError("NOT_FOUND", f"融合本体 {fusion_ontology_id} 未找到对应融合申请", 404)
    if req.get("status") != "approved":
        raise FusionError("FUSION_NOT_APPROVED", f"融合申请状态 {req.get('status')} 不可查询", 409)
    sources = list(req.get("source_ontologies") or [])
    user_id: str | None = None
    if user is not None:
        _sources_visible(project_id, user, sources)
        user_id = user.get("id")
    else:
        published = set(published_ontology_ids(project_id))
        if not set(sources) <= published:
            raise FusionError("NOT_FOUND", "存在未发布的源本体（MCP 通道访问边界=已发布态）", 404)

    ont_id = req.get("target_ontology_id")
    if not ont_id:
        raise FusionError("FUSION_NOT_EXECUTED", "融合尚未执行（无融合本体），请先执行融合", 409)
    ont_rec = _ont_store(project_id).get(ont_id)
    if not ont_rec:
        raise FusionError("NOT_FOUND", "融合本体记录不存在", 404)
    _, model_override = resolve_versioned(ont_rec, version)
    fus_objects = model_override.get("objects", ont_rec.get("objects") or [])
    effective_version = version or (ont_rec.get("version") or "")
    fus_names = {o.get("name"): o.get("id") for o in fus_objects}
    if object_type:
        object_type = fus_names.get(object_type, object_type)

    istore = get_instance_store(project_id)
    all_recs = istore.all()
    records_by_source: dict[str, list[dict]] = {oid: [] for oid in sources}
    for r in all_recs:
        oid = r.get("ontology_id")
        if oid in records_by_source:
            records_by_source[oid].append(r)
    ont_recs = {oid: _require_ontology(project_id, oid) for oid in sources}

    items: list[dict] = []
    for fobj in fus_objects:
        fus = fobj.get("fusion") or {}
        src_list = fus.get("sources")
        if not src_list and fus.get("pass_through"):
            pt = fus["pass_through"]
            src_list = [{"ontology_id": pt["ontology_id"], "object_id": pt["object_id"],
                         "object_name": ""}]
        if not src_list:
            continue
        if object_type and fobj.get("id") != object_type:
            continue

        # 每个源：候选记录 + 分渠道取数
        entries_by_key: dict[str, list[dict]] = {}
        for s in src_list:
            oid, s_obj_id = s["ontology_id"], s["object_id"]
            # canonical 属性中该源承担的 attr_id
            cid_to_attr: dict[str, str] = {}
            for am in fobj.get("attributes") or []:
                for fs in am.get("fusion_sources") or []:
                    if fs.get("ontology_id") == oid:
                        cid_to_attr[am["id"]] = fs.get("attr_id")
            for rec in records_by_source.get(oid, []):
                if rec.get("object_type") != s_obj_id:
                    continue
                kv_raw = rec.get("stable_key_value")
                if stable_key_value is not None and str(kv_raw) != str(stable_key_value):
                    continue
                attr_ids = list(dict.fromkeys(cid_to_attr.values()))
                values, meta, err = _fetch_source_values(project_id, rec, user_id, attr_ids)
                entry = {
                    "ontology_id": oid,
                    "ontology_name": ont_recs[oid].get("name"),
                    "object_id": s_obj_id,
                    "instance_id": rec.get("id"),
                    "channel": rec.get("channel") or "upload",
                    "content_mode": meta.get("mode") or "local",
                    "content_resolution": meta.get("fallback"),
                    "error": err,
                    "values": {cid: values.get(a) for cid, a in cid_to_attr.items()},
                }
                kv = str(kv_raw) if kv_raw is not None else f"__nokey__:{rec.get('id')}"
                entries_by_key.setdefault(kv, []).append(entry)

        authoritative = {}
        for g in req.get("object_groups") or []:
            if fus.get("group_id") and g.get("group_id") == fus.get("group_id"):
                authoritative = g.get("authoritative") or {}
                break

        for kv, entries in entries_by_key.items():
            merged: dict = {}
            conflicts: list[str] = []
            for am in fobj.get("attributes") or []:
                cid = am["id"]
                vals: dict = {}
                for e in entries:
                    v = e["values"].get(cid)
                    if v is not None:
                        vals[e["ontology_id"]] = v
                if not vals:
                    continue
                _, distinct = _distinct_count(vals)
                if len(distinct) <= 1:
                    v = next(iter(vals.values()))
                    if len(vals) == 1:
                        merged[cid] = {"value": v, "source": next(iter(vals.keys()))}
                    else:
                        merged[cid] = {"value": v, "sources": sorted(vals.keys())}
                else:
                    auth_ont = authoritative.get(cid)
                    if auth_ont and auth_ont in vals:
                        merged[cid] = {"value": vals[auth_ont], "source": auth_ont,
                                       "conflict": True, "values": vals}
                    else:
                        # 走到这里必然取不到权威源值：无权威决策，或权威源无值 → 双值并存
                        merged[cid] = {"conflict": True, "values": vals,
                                       "authoritative_missing": True}
                    # 只要存在冲突值即登记（无论是否已权威裁决），裁决细节在 merged[cid] 内
                    conflicts.append(cid)
            items.append({
                "object": fobj.get("id"),
                "object_name": fobj.get("name"),
                "stable_key_value": None if kv.startswith("__nokey__:") else kv,
                # TP-02-14：跨源未匹配键保持独立记录（来源本体标记在 sources 各 entry），不强行合并
                "independent": len(entries) == 1,
                "merged": merged,
                "conflicts": conflicts,
                "sources": entries,
            })

    total = len(items)
    lim = max(1, min(int(limit or 100), 200))
    return {
        "fusion_id": req.get("id"),
        "fusion_ontology_id": ont_id,
        "version": effective_version,
        "source_ontologies": sources,
        "total": total,
        "items": items[:lim],
    }
