"""V2-02 跨本体对齐（M4-A②）：候选检测（四类信号＋置信度）／等价声明／
多 owner 会签（一票否决、owner 缺位租户管理员代行）／冲突检测
（属性级值域/必填/主键——显式权威源决策，不自动合并）。

INV（对应测试大纲 TP-02-04/06/07/08/13、EP-02-01/02/04/06）：
- 候选对仅取**双方对查看者同时可见**的本体（V2-06 `tenant.schema_visible`）；
  被拒绝的对不再重复提议，除非证据显著变化（confidence 提升 ≥ REPROPOSE_DELTA）。
- 会签：required signers＝各对象对两侧本体的 owner（按 (owner, ontology_id) 去重）；
  全 approve → approved；任一 reject → rejected（一票否决）；
  owner 缺位（账号不存在）时租户管理员（role=admin）可代行，必须给 proxy_reason 并留审计；
  共享等级（shared_with）不携带审批权；public 本体不能由非 owner 会签生效。
- 冲突：属性对的值域（data_type/enum/min/max）、必填、主键（stable_key）冲突
  必须显式权威源决策（resolution ∈ {a,b}）后才可批准——不自动合并。
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Optional

from core.storage import JsonStore

# 信号权重（TP-02-01：key overlap / common origin / semantic / structural）
SIGNAL_WEIGHTS = {"key_overlap": 0.40, "common_origin": 0.25,
                  "semantic": 0.20, "structural": 0.15}
# 被拒对「证据显著变化」阈值：confidence 提升 ≥ 此值才重新提议
REPROPOSE_DELTA = 0.20
# 实例信号采样上限（防大实例集拖垮检测）
_KEY_SAMPLE_LIMIT = 500
_ORIGIN_SAMPLE_LIMIT = 2000
# 候选返回上限
CANDIDATE_LIMIT = 20


def _store(project_id: str) -> JsonStore:
    return JsonStore("alignment_declarations", project_id)


def _now() -> str:
    from ingestion import _now as _n
    return _n()


def _tokens(s: Any) -> set[str]:
    s = str(s or "")
    return set(re.findall(r"[A-Za-z0-9_]+", s.lower())) | set(
        re.findall(r"[\u4e00-\u9fff]{2,}", s))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


# ---------------------------------------------------------------------------
# 实例信号数据（key overlap / common origin）
# ---------------------------------------------------------------------------

def _instance_signal_data(project_id: str, ontology_id: str):
    """object_type → (set(stable_key_value), set((kind, connection_id)))（采样截断）"""
    from core.instance_store import JsonlInstanceStore
    keys: dict[str, set[str]] = {}
    origins: dict[str, set[tuple[str, str]]] = {}
    try:
        for rec in JsonlInstanceStore(project_id=project_id).all():
            if rec.get("ontology_id") != ontology_id:
                continue
            ot = rec.get("object_type") or ""
            sk = rec.get("stable_key_value")
            if sk is not None:
                keys.setdefault(ot, set())
                if len(keys[ot]) < _KEY_SAMPLE_LIMIT:
                    keys[ot].add(str(sk))
            src = (rec.get("instance") or {}).get("_source") or {}
            cid = src.get("connection_id")
            if cid:
                origins.setdefault(ot, set())
                if len(origins[ot]) < _ORIGIN_SAMPLE_LIMIT:
                    origins[ot].add((src.get("kind") or "", str(cid)))
    except Exception:  # noqa: BLE001 实例存储缺失不阻塞检测
        pass
    return keys, origins


def object_pair_signals(ont_a: dict, obj_a: dict, ont_b: dict, obj_b: dict,
                        sig_a, sig_b) -> dict[str, Any]:
    """单对象对四类信号＋证据＋置信度。sig_* = (keys, origins) 实例信号数据。"""
    keys_a, origins_a = sig_a
    keys_b, origins_b = sig_b
    ot_a, ot_b = obj_a.get("id") or "", obj_b.get("id") or ""

    # 1) key overlap：实例 stable_key_value 交集率
    ka = keys_a.get(ot_a, set())
    kb = keys_b.get(ot_b, set())
    key_overlap = _jaccard(ka, kb)
    key_ev = f"键值重叠 {len(ka & kb)}（A {len(ka)} / B {len(kb)}）" if (ka or kb) else "两侧均无实例键值"

    # 2) common origin：同源（相同 kind+connection_id）
    oa = origins_a.get(ot_a, set())
    ob = origins_b.get(ot_b, set())
    common = oa & ob
    common_origin = _jaccard(oa, ob)
    common_ev = (["来源:" + "/".join(c) for c in sorted(common)[:3]] if common
                 else "无共同来源")

    # 3) semantic：对象名/描述＋属性名 token 相似度
    name_sim = _jaccard(_tokens(f"{obj_a.get('name','')} {obj_a.get('business_definition','') or obj_a.get('description','')}"),
                        _tokens(f"{obj_b.get('name','')} {obj_b.get('business_definition','') or obj_b.get('description','')}"))
    attr_names_a = {a.get("name") or a.get("id") for a in (obj_a.get("attributes") or [])}
    attr_names_b = {a.get("name") or a.get("id") for a in (obj_b.get("attributes") or [])}
    attr_sim = _jaccard(_tokens(" ".join(map(str, attr_names_a))),
                        _tokens(" ".join(map(str, attr_names_b))))
    semantic = round(0.5 * name_sim + 0.5 * attr_sim, 4)
    sem_ev = f"对象名相似度 {name_sim:.2f}（{obj_a.get('name')} / {obj_b.get('name')}）、属性名相似度 {attr_sim:.2f}"

    # 4) structural：属性名集合＋关系目标匹配
    struct = round(0.7 * _jaccard(attr_names_a, attr_names_b)
                   + 0.3 * _relation_match(obj_a, obj_b), 4)
    struct_ev = f"属性名匹配 {len(attr_names_a & attr_names_b)}/{max(len(attr_names_a), len(attr_names_b)) or 0}"

    signals = {"key_overlap": round(key_overlap, 4), "common_origin": round(common_origin, 4),
               "semantic": semantic, "structural": struct}
    confidence = round(sum(SIGNAL_WEIGHTS[k] * v for k, v in signals.items()), 4)
    return {"signals": signals, "confidence": confidence,
            "evidence": {"key_overlap": key_ev, "common_origin": common_ev,
                         "semantic": sem_ev, "structural": struct_ev}}


def _relation_match(obj_a: dict, obj_b: dict) -> float:
    ra = obj_a.get("relations") or []
    rb = obj_b.get("relations") or []
    if not ra or not rb:
        return 0.0
    names_a = {r.get("name") or r.get("id") for r in ra}
    names_b = {r.get("name") or r.get("id") for r in rb}
    return _jaccard(names_a, names_b)


def detect_candidates(user: dict | None, project: dict, ontologies: list[dict],
                      store: JsonStore | None = None) -> dict:
    """TP-02-06/07：仅对查看者双方可见的本体对生成候选；被拒对证据显著变化才重提。
    user=None 时跳过用户可见性过滤（MCP 通道：访问边界=发布态，调用方须预筛已发布本体）。"""
    store = store or _store(project.get("id") or "")
    pid = project.get("id") or ""
    if user is None:
        visible = [o for o in ontologies if o.get("id")]
    else:
        import tenant
        visible = [o for o in ontologies
                   if o.get("id") and tenant.schema_visible(user, project, o)]
    visible.sort(key=lambda o: o["id"])

    # 已有声明：(a,b) → latest（approved 优先；否则最近一条）
    declared: dict[tuple[str, str], dict] = {}
    for d in store.all():
        pair = tuple(sorted((d.get("ontology_a") or "", d.get("ontology_b") or "")))
        prev = declared.get(pair)
        if prev is None or (d.get("status") == "approved") or \
           (d.get("submitted_at") or "") > (prev.get("submitted_at") or ""):
            declared[pair] = d

    sig_cache: dict[str, tuple] = {}

    def sig_of(oid: str):
        if oid not in sig_cache:
            sig_cache[oid] = _instance_signal_data(pid, oid)
        return sig_cache[oid]

    cands: list[dict] = []
    scanned = 0
    for i, a in enumerate(visible):
        for b in visible[i + 1:]:
            scanned += 1
            pair = tuple(sorted((a["id"], b["id"])))
            prev = declared.get(pair)
            if prev and prev.get("status") in ("approved", "pending"):
                continue  # 已生效/进行中不再重复提议
            sig_a, sig_b = sig_of(a["id"]), sig_of(b["id"])
            objs_a = [o for o in (a.get("objects") or []) if o.get("id")]
            objs_b = [o for o in (b.get("objects") or []) if o.get("id")]
            if not objs_a or not objs_b:
                continue
            best = []
            for oa in objs_a:
                for ob in objs_b:
                    s = object_pair_signals(a, oa, b, ob, sig_a, sig_b)
                    best.append({"object_a": oa["id"], "object_b": ob["id"],
                                 "object_a_name": oa.get("name"), "object_b_name": ob.get("name"),
                                 **s})
            best.sort(key=lambda x: x["confidence"], reverse=True)
            if not best or best[0]["confidence"] <= 0:
                continue
            confidence = best[0]["confidence"]
            repropose = False
            if prev and prev.get("status") == "rejected":
                # 证据显著变化才重提（TP-02-06）
                prev_conf = prev.get("last_confidence") or 0
                if confidence - prev_conf < REPROPOSE_DELTA:
                    continue
                repropose = True
            cands.append({
                "ontology_a": a["id"], "ontology_b": b["id"],
                "ontology_a_name": a.get("name"), "ontology_b_name": b.get("name"),
                "owner_a": a.get("owner"), "owner_b": b.get("sharing_owner", a.get("owner")),
                "confidence": confidence, "repropose": repropose,
                "object_pairs": best[:3],
            })
    cands.sort(key=lambda c: c["confidence"], reverse=True)
    return {"candidates": cands[:CANDIDATE_LIMIT], "scanned_pairs": scanned,
            "visible_ontologies": [o["id"] for o in visible]}


# ---------------------------------------------------------------------------
# 冲突检测（TP-02-04/EP-02-06：值域/必填/主键；显式决策，不自动合并）
# ---------------------------------------------------------------------------

def detect_attribute_conflicts(obj_a: dict, attr_a: dict,
                               obj_b: dict, attr_b: dict) -> list[dict]:
    out: list[dict] = []
    base = {"attr_a": attr_a.get("id"), "attr_b": attr_b.get("id"),
            "object_a": obj_a.get("id"), "object_b": obj_b.get("id")}
    if (attr_a.get("data_type") or "string") != (attr_b.get("data_type") or "string"):
        out.append({**base, "type": "value_domain",
                    "detail": f"data_type 不一致：{attr_a.get('data_type') or 'string'} vs {attr_b.get('data_type') or 'string'}"})
    ca = attr_a.get("constraints") or {}
    cb = attr_b.get("constraints") or {}
    ea, eb = ca.get("enum"), cb.get("enum")
    if ea is not None and eb is not None and sorted(map(str, ea)) != sorted(map(str, eb)):
        out.append({**base, "type": "value_domain",
                    "detail": f"枚举白名单不一致：{ea} vs {eb}"})
    for bound in ("minimum", "maximum"):
        va, vb = ca.get(bound), cb.get(bound)
        if va is not None and vb is not None and va != vb:
            out.append({**base, "type": "value_domain",
                        "detail": f"{bound} 不一致：{va} vs {vb}"})
    if bool(attr_a.get("required")) != bool(attr_b.get("required")):
        out.append({**base, "type": "required",
                    "detail": f"必填不一致：A required={bool(attr_a.get('required'))} / B required={bool(attr_b.get('required'))}"})
    if ((obj_a.get("stable_key") or "") == (attr_a.get("id") or "")
        and (obj_b.get("stable_key") or "") != (attr_b.get("id") or "")) or \
       ((obj_b.get("stable_key") or "") == (attr_b.get("id") or "")
        and (obj_a.get("stable_key") or "") != (attr_a.get("id") or "")):
        out.append({**base, "type": "primary_key",
                    "detail": f"主键不一致：A stable_key={'是' if obj_a.get('stable_key') == attr_a.get('id') else '否'} / B stable_key={'是' if obj_b.get('stable_key') == attr_b.get('id') else '否'}"})
    return out


# ---------------------------------------------------------------------------
# 声明与会签状态机（TP-02-08/13、EP-02-02/04）
# ---------------------------------------------------------------------------

def _users_by_username() -> dict[str, dict]:
    from auth import list_users
    return {u.get("username"): u for u in list_users() if u.get("username")}


def build_signers_required(ontology_a: dict, ontology_b: dict,
                           object_pairs: list[dict]) -> list[dict]:
    """required signers：各本体 owner（按 (owner, ontology_id) 去重）；缺位判定＝账号不存在。"""
    users = _users_by_username()
    req: dict[tuple[str, str], dict] = {}
    for oid, ont in ((ontology_a.get("id"), ontology_a), (ontology_b.get("id"), ontology_b)):
        owner = ont.get("owner") or ""
        if not owner:
            continue
        objs = []
        if oid == (ontology_a.get("id") or ""):
            objs = [p.get("object_a") for p in object_pairs if p.get("object_a")]
        else:
            objs = [p.get("object_b") for p in object_pairs if p.get("object_b")]
        req[(owner, oid)] = {"owner": owner, "ontology_id": oid,
                             "objects": sorted({o for o in objs if o}),
                             "present": owner in users,
                             "admin": bool(users.get(owner, {}).get("role") == "admin")}
    return list(req.values())


def signers_pending(decl: dict) -> list[dict]:
    """尚未批准（approve）的 required signers。

    代行签名 signed_for=缺位 owner（owner 字段是代签人 admin），故按 signed_for 归位，
    否则 owner 缺位的声明经 admin 代行后仍永远 pending。
    """
    done = {(s.get("signed_for") or s.get("owner"), s.get("ontology_id"))
            for s in (decl.get("signatures") or []) if s.get("decision") == "approve"}
    return [r for r in (decl.get("signers_required") or [])
            if (r.get("owner"), r.get("ontology_id")) not in done]


def check_all_conflicts_resolved(decl: dict) -> list[dict]:
    return [c for c in (decl.get("conflicts") or []) if not c.get("resolution")]


def make_declaration(project_id: str, ontology_a: dict, ontology_b: dict,
                     object_pairs: list[dict], submitted_by: str,
                     candidate: dict | None = None, repropose: bool = False) -> dict:
    """创建 pending 声明（含冲突检测＋会签要求清单）。"""
    a, b = ontology_a, ontology_b
    pairs = []
    conflicts = []
    for p in object_pairs:
        pairs.append({"object_a": p.get("object_a"), "object_b": p.get("object_b"),
                      "attribute_pairs": [
                          {"attr_a": ap.get("attr_a"), "attr_b": ap.get("attr_b")}
                          for ap in (p.get("attribute_pairs") or []) if ap.get("attr_a") and ap.get("attr_b")],
                      "signals": p.get("signals"), "confidence": p.get("confidence")})
        oa = next((o for o in (a.get("objects") or []) if o.get("id") == p.get("object_a")), None)
        ob = next((o for o in (b.get("objects") or []) if o.get("id") == p.get("object_b")), None)
        if not oa or not ob:
            continue
        attrs_a = {x.get("id"): x for x in (oa.get("attributes") or [])}
        attrs_b = {x.get("id"): x for x in (ob.get("attributes") or [])}
        for ap in p.get("attribute_pairs") or []:
            xa, xb = attrs_a.get(ap.get("attr_a")), attrs_b.get(ap.get("attr_b"))
            if xa and xb:
                for c in detect_attribute_conflicts(oa, xa, ob, xb):
                    conflicts.append({**c, "resolution": None,
                                      "resolved_by": None, "resolved_at": None})
    conf = (candidate or {}).get("confidence") or 0
    return {
        "id": uuid.uuid4().hex, "project_id": project_id,
        "ontology_a": a.get("id"), "ontology_b": b.get("id"),
        "ontology_a_name": a.get("name"), "ontology_b_name": b.get("name"),
        "object_pairs": pairs, "conflicts": conflicts,
        "status": "pending",
        "signers_required": build_signers_required(a, b, pairs),
        "signatures": [],
        "last_confidence": conf,
        "repropose": repropose,
        "submitted_by": submitted_by, "submitted_at": _now(),
        "decided_at": None, "decided_by": None, "reject_reason": "",
    }


def sign_decl(project_id: str, decl: dict, user: dict, decision: str,
              comment: str = "", proxy_reason: str = "") -> dict:
    """会签：一票否决；owner 缺位时 admin 代行（proxy_reason 必填）。
    批准前置：所有冲突已显式决策（权威源）。返回 (decl, audit_events)。
    调用方负责 store 落盘。
    """
    import tenant
    username = user.get("username") or ""
    is_admin = user.get("role") == "admin" or tenant.is_admin(user)
    if decision not in ("approve", "reject"):
        raise ValueError("decision 须为 approve/reject")
    if decl.get("status") != "pending":
        raise _DeclError("DECLARATION_NOT_PENDING", f"声明已处于终态 {decl.get('status')}")

    # 定位该用户可签的 required signer
    reqs = [r for r in decl.get("signers_required") or []
            if r.get("owner") == username]
    proxying = None  # (owner, ontology_id)
    if not reqs:
        # 代行：owner 缺位且 admin
        reqs = [r for r in decl.get("signers_required") or []
                if not r.get("present") and r.get("owner") != username]
        if not (is_admin and reqs):
            raise _DeclError("SIGNER_NOT_ALLOWED",
                             "仅对象所属本体的 owner（或 owner 缺位时的租户管理员代行）可会签；共享等级不携带审批权")
        if not proxy_reason:
            raise _DeclError("PROXY_REASON_REQUIRED", "owner 缺位代行必须填写 proxy_reason")
        proxying = (reqs[0].get("owner"), reqs[0].get("ontology_id"))
    else:
        if not reqs[0].get("present"):
            raise _DeclError("OWNER_ABSENT", "owner 账号缺位，需租户管理员代行")

    for r in reqs:
        if not r.get("present") and not (is_admin and proxy_reason):
            raise _DeclError("OWNER_ABSENT", "owner 账号缺位，需租户管理员代行")

    # 批准前置：冲突显式决策（不自动合并）
    if decision == "approve":
        unresolved = check_all_conflicts_resolved(decl)
        if unresolved:
            raise _DeclError("CONFLICT_RESOLUTION_REQUIRED",
                             f"存在 {len(unresolved)} 个未决策的属性冲突，须先指定权威源（a/b）")

    role = "tenant_admin_proxy" if proxying else "owner"
    # owner 路径对其名下每个 (owner, ontology_id) req 分别落签（同一 owner 持两侧本体时
    # 一次会签同时满足两条，避免 signers_pending 按 (owner, ontology_id) 匹配导致的死锁）；
    # 代行路径仅签 reqs[0]（一次代行对应一个缺位 owner）。
    for r in ([reqs[0]] if proxying else reqs):
        decl.setdefault("signatures", []).append({
            "owner": username, "ontology_id": r.get("ontology_id"),
            "signed_for": proxying[0] if proxying else username,
            "role": role, "decision": decision, "comment": comment or "",
            "proxy_reason": proxy_reason or "", "decided_at": _now()})

    events = []
    if proxying:
        events.append("alignment_proxy_sign")
    if decision == "reject":
        decl["status"] = "rejected"
        decl["reject_reason"] = comment or ""
        decl["decided_at"] = _now()
        decl["decided_by"] = username
        events.append("alignment_sign_reject")
    else:
        pending = signers_pending(decl)
        if not pending:
            decl["status"] = "approved"
            decl["decided_at"] = _now()
            decl["decided_by"] = username
            events.append("alignment_sign_approve")
            events.append("alignment_approved")
        else:
            events.append("alignment_sign_approve")
    return decl, events


def withdraw_decl(project_id: str, decl: dict, user: dict) -> dict:
    if decl.get("status") != "pending":
        raise _DeclError("DECLARATION_NOT_PENDING", f"声明已处于终态 {decl.get('status')}")
    if user.get("username") != decl.get("submitted_by"):
        raise _DeclError("WITHDRAW_NOT_ALLOWED", "仅提交人可撤回")
    decl["status"] = "withdrawn"
    decl["decided_at"] = _now()
    decl["decided_by"] = user.get("username")
    return decl


def resolve_conflict(project_id: str, decl: dict, user: dict, idx: int,
                     resolution: str) -> dict:
    """EP-02-06：冲突显式决策（权威源 a/b）。提交人/会签人可决策。"""
    if decl.get("status") != "pending":
        raise _DeclError("DECLARATION_NOT_PENDING", f"声明已处于终态 {decl.get('status')}")
    if resolution not in ("a", "b"):
        raise _DeclError("BAD_RESOLUTION", "resolution 须为 'a'（以 A 为准）或 'b'（以 B 为准）")
    conflicts = decl.get("conflicts") or []
    if not (0 <= idx < len(conflicts)):
        raise _DeclError("CONFLICT_NOT_FOUND", f"冲突索引 {idx} 越界")
    username = user.get("username") or ""
    allowed = {decl.get("submitted_by")}
    allowed |= {r.get("owner") for r in (decl.get("signers_required") or [])}
    if user.get("role") != "admin" and username not in allowed:
        raise _DeclError("RESOLVE_NOT_ALLOWED", "仅提交人或会签 owner/管理员可决策冲突")
    conflicts[idx]["resolution"] = resolution
    conflicts[idx]["resolved_by"] = username
    conflicts[idx]["resolved_at"] = _now()
    return decl


class _DeclError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_decl_visible(user: dict, project: dict, decl: dict,
                    ontologies: list[dict]) -> bool:
    """声明可见性＝任一涉及本体对查看者可见（V2-06）。"""
    import tenant
    by_id = {o.get("id"): o for o in ontologies}
    return any(tenant.schema_visible(user, project, by_id[decl.get(k)]) is True
               for k in ("ontology_a", "ontology_b") if by_id.get(decl.get(k)))
