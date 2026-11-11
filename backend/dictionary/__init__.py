"""V2-01 数据字典与联邦查询服务（M1-A 切片）。

职责（docs/V2-功能需求描述.md V2-01）：
1. 术语库：canonical 词条 + 同义词集（输入侧）+ 各系统字段绑定（输出侧）+ 权威来源系统；
2. 术语解析：精确匹配（canonical/同义词）→ 确定性语义近似（子串/相似度），
   置信度不足或多候选接近 → 返回候选+置信度交人确认，不猜测；人工晋升为同义词后走精确匹配；
3. 联邦查询：解析词条 → 各系统以其字段名并行查询（复用 mcp_client 鉴权/超时/重试/熔断）→
   按 canonical key 归并去重；同 key 值冲突默认并存+来源标记，配了权威来源则取权威值（其他值仍可查）；
   未配置系统显式"未覆盖"；全失败显式报错；
4. 成本护栏：limit/分页参数下推外部工具；外部工具仅支持全量拉取时须声明 fetch_limit，
   预览给出成本提示（预计返回量/时延），确认前可取消；
5. field_map 收敛：连接 field_map 可声明来源为字典（field_map_source=dictionary），
   入库方向按字典解析，与查询方向同一数据源；
6. 全过程审计：输入词/解析路径/命中系统/返回规模/成本 落 term_lookup_audit 集合（可查询）。

存储：JsonStore 每项目两集合——dictionary_terms / term_lookup_audit（与平台其余资源一致）。
"""
from __future__ import annotations

import difflib
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from core.responses import ApiError
from core.storage import JsonStore
from logging_setup import get_logger
from mcp_client import McpClient

logger = get_logger("dictionary")
_client: McpClient | None = None


def _mcp() -> McpClient:
    global _client
    if _client is None:
        _client = McpClient()
    return _client

# ---- 阈值（MVP 确定性口径，V2-06 授权域接入后不变）----
SEMANTIC_THRESHOLD = 0.6      # 语义近似最低置信度
AMBIGUITY_GAP = 0.1           # top1-top2 差距小于该值 → 视为歧义，交人确认
COST_CONFIRM_THRESHOLD = 100  # 全量拉取预计返回量超过该值 → 需用户确认成本
DEFAULT_LATENCY_EST_MS = 2000  # 无实测时延时的成本估算时延

TERMS = "dictionary_terms"
AUDIT = "term_lookup_audit"
CONNS = "mcp_connections"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terms(project_id: str) -> JsonStore:
    return JsonStore(TERMS, project_id)


def _audit(project_id: str) -> JsonStore:
    return JsonStore(AUDIT, project_id)


def _conns(project_id: str) -> JsonStore:
    return JsonStore(CONNS, project_id)


def normalize_text(s: Any, rules: Optional[dict] = None) -> str:
    """值归一化：全角→半角（NFKC）+ 去首尾空白 + 内部空白折叠；rules 可扩展 casefold/正则。"""
    val = "" if s is None else str(s)
    val = unicodedata.normalize("NFKC", val)
    val = re.sub(r"\s+", " ", val.strip())
    rules = rules or {}
    if rules.get("casefold", True):
        val = val.casefold()
    if rules.get("pattern"):
        try:
            val = re.sub(rules["pattern"], "", val)
        except re.error:
            pass
    return val


def _similarity(inp: str, target: str) -> float:
    """确定性近似度：相等=1.0；一方包含另一方（常见缩写：PN vs PN Code）→ 0.75+0.25*长度比；
    否则 SequenceMatcher ratio。"""
    if not inp or not target:
        return 0.0
    if inp == target:
        return 1.0
    ratio = difflib.SequenceMatcher(None, inp, target).ratio()
    if inp in target or target in inp:
        shorter = min(len(inp), len(target))
        longer = max(len(inp), len(target))
        return max(ratio, 0.75 + 0.25 * (shorter / longer))
    return ratio


# ---------------------------------------------------------------------------
# 术语 CRUD
# ---------------------------------------------------------------------------

def list_terms(project_id: str) -> list[dict[str, Any]]:
    return sorted(_terms(project_id).all(), key=lambda t: (t.get("canonical") or ""))


def _check_binding_conflict(project_id: str, bindings: dict[str, str],
                            exclude_term_id: str | None = None) -> None:
    """TCE-01-06 字典冲突配置：两个规范术语绑定同一系统字段 → 保存时报错指出冲突项，不静默接受。"""
    bindings = {k: v for k, v in (bindings or {}).items() if v}
    if not bindings:
        return
    for t in _terms(project_id).all():
        if t.get("id") == exclude_term_id:
            continue
        other = t.get("system_bindings") or {}
        for conn, fld in bindings.items():
            if other.get(conn) == fld:
                raise ApiError("DICT_FIELD_CONFLICT",
                               f"系统字段冲突：系统 {conn} 的字段 '{fld}' 已被词条 "
                               f"'{t.get('canonical')}' 绑定，一个系统字段只能对应一个规范术语",
                               409)


def get_term(project_id: str, term_id: str) -> Optional[dict[str, Any]]:
    return _terms(project_id).get(term_id)


def create_term(project_id: str, data: dict[str, Any]) -> dict[str, Any]:
    canonical = (data.get("canonical") or "").strip()
    if not canonical:
        raise ApiError("INVALID_TERM", "canonical 不能为空", 422)
    _check_binding_conflict(project_id, data.get("system_bindings") or {})
    rec = {
        "canonical": canonical,
        "object_type": (data.get("object_type") or "").strip(),
        "property_id": (data.get("property_id") or "").strip(),
        "synonyms": [s.strip() for s in (data.get("synonyms") or []) if str(s).strip()],
        "system_bindings": {k: v for k, v in (data.get("system_bindings") or {}).items() if v},
        "authoritative_source": data.get("authoritative_source") or None,
        "normalization": data.get("normalization") or {},
        "created_at": _now(),
        "updated_at": _now(),
    }
    rec = _terms(project_id).insert(dict(rec))
    logger.info("dictionary.term_create project=%s term='%s'", project_id, canonical)
    return rec


def update_term(project_id: str, term_id: str, data: dict[str, Any]) -> dict[str, Any]:
    store = _terms(project_id)
    if store.get(term_id) is None:
        raise ApiError("NOT_FOUND", f"词条 {term_id} 不存在", 404)
    patch = {k: data[k] for k in
             ("canonical", "object_type", "property_id", "synonyms",
              "system_bindings", "authoritative_source", "normalization")
             if k in data}
    if "system_bindings" in patch:
        _check_binding_conflict(project_id, patch.get("system_bindings") or {},
                                exclude_term_id=term_id)
    patch["updated_at"] = _now()
    rec = store.update(term_id, patch)
    logger.info("dictionary.term_update project=%s term=%s", project_id, term_id)
    return rec


def delete_term(project_id: str, term_id: str) -> None:
    if not _terms(project_id).delete(term_id):
        raise ApiError("NOT_FOUND", f"词条 {term_id} 不存在", 404)
    logger.info("dictionary.term_delete project=%s term=%s", project_id, term_id)


def promote_synonym(project_id: str, term_id: str, synonym: str) -> dict[str, Any]:
    """人工晋升：把输入词加入词条同义词集（之后走精确匹配）。"""
    syn = (synonym or "").strip()
    if not syn:
        raise ApiError("INVALID_TERM", "synonym 不能为空", 422)
    store = _terms(project_id)
    rec = store.get(term_id)
    if not rec:
        raise ApiError("NOT_FOUND", f"词条 {term_id} 不存在", 404)
    if syn not in rec.get("synonyms", []):
        rec = store.update(term_id, {"synonyms": rec.get("synonyms", []) + [syn],
                                      "updated_at": _now()})
    logger.info("dictionary.synonym_promote project=%s term='%s' synonym='%s'",
                project_id, rec["canonical"], syn)
    return rec


# ---------------------------------------------------------------------------
# 术语解析（精确 → 语义近似；歧义/不足 → 候选交人确认，不猜测）
# ---------------------------------------------------------------------------

def resolve_term(project_id: str, input_term: str) -> dict[str, Any]:
    """返回 {term, match, confidence, candidates, needs_disambiguation}。

    match: "exact"（canonical/同义词归一化后相等）| "semantic"（近似度达标且显著领先）| None。
    needs_disambiguation=True：多候选置信度接近，调用方须人工选择（可晋升同义词）。
    """
    norm_in = normalize_text(input_term)
    terms = list_terms(project_id)
    # 1) 精确匹配
    for t in terms:
        names = [t.get("canonical") or ""] + list(t.get("synonyms") or [])
        if norm_in in {normalize_text(n) for n in names if n}:
            return {"term": t, "match": "exact", "confidence": 1.0,
                    "candidates": [], "needs_disambiguation": False}
    # 2) 语义近似（确定性；embedding/LLM 为后续增强点，接口不变）
    scored = []
    for t in terms:
        names = [t.get("canonical") or ""] + list(t.get("synonyms") or [])
        best = max((_similarity(norm_in, normalize_text(n)) for n in names if n), default=0.0)
        if best >= SEMANTIC_THRESHOLD:
            scored.append({"term_id": t["id"], "canonical": t.get("canonical"),
                           "match_method": "semantic", "confidence": round(best, 3)})
    scored.sort(key=lambda c: -c["confidence"])
    top = scored[:5]
    if scored and (len(scored) == 1 or scored[0]["confidence"] - scored[1]["confidence"] >= AMBIGUITY_GAP):
        winner = next(t for t in terms if t["id"] == scored[0]["term_id"])
        return {"term": winner, "match": "semantic",
                "confidence": scored[0]["confidence"],
                "candidates": top, "needs_disambiguation": False}
    if scored:
        return {"term": None, "match": None, "confidence": None,
                "candidates": top, "needs_disambiguation": True}
    return {"term": None, "match": None, "confidence": None,
            "candidates": [], "needs_disambiguation": False}


# ---------------------------------------------------------------------------
# field_map 收敛（入库/查询同一字典数据源）
# ---------------------------------------------------------------------------

def resolve_field_map(project_id: str, connection: dict[str, Any]) -> tuple[dict[str, str], str]:
    """连接 field_map 的单一解析点。

    field_map_source="dictionary"：按字典生成——该连接绑定对象下所有词条的
    {系统字段: 本体属性id}；"explicit"：用连接自带 field_map（向后兼容）。
    返回 (field_map, source)。
    """
    source = connection.get("field_map_source") or "explicit"
    if source == "dictionary":
        maps: dict[str, str] = {}
        obj = (connection.get("maps_to_ontology") or {}).get("object_type")
        for t in list_terms(project_id):
            if obj and t.get("object_type") and t.get("object_type") != obj:
                continue
            sys_field = (t.get("system_bindings") or {}).get(connection.get("connection_id") or "")
            prop = t.get("property_id")
            if sys_field and prop:
                maps[sys_field] = prop
        return maps, "dictionary"
    return dict((connection.get("maps_to_ontology") or {}).get("field_map") or {}), "explicit"


def field_map_sources(project_id: str) -> list[dict[str, Any]]:
    """field_map 配置来源盘点（TC-01-03 步骤①）。"""
    out = []
    for c in _conns(project_id).all():
        fmap, source = resolve_field_map(project_id, c)
        out.append({
            "connection_id": c.get("connection_id"),
            "display_name": c.get("display_name"),
            "field_map_source": source,
            "field_map_size": len(fmap),
        })
    return out


def import_from_connections(project_id: str,
                            connection_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """从既有连接 field_map 收敛生成词条草稿（需求 2：存量 field_map 收敛到字典）。

    按 (object_type, property_id) 归组：canonical 取属性名（无则 property_id），
    system_bindings 收集各连接的系统字段；已存在同 (object_type, property_id) 词条则仅补绑定。
    """
    conns = _conns(project_id).all()
    if connection_ids:
        conns = [c for c in conns if c.get("connection_id") in set(connection_ids)]
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for c in conns:
        obj = (c.get("maps_to_ontology") or {}).get("object_type") or ""
        fmap = (c.get("maps_to_ontology") or {}).get("field_map") or {}
        for sys_field, prop in fmap.items():
            g = groups.setdefault((obj, prop), {
                "canonical": prop, "object_type": obj, "property_id": prop,
                "synonyms": [], "system_bindings": {},
            })
            if sys_field:
                g["system_bindings"][c.get("connection_id") or ""] = sys_field
    created, merged = [], []
    for (obj, prop), g in sorted(groups.items()):
        existing = [t for t in list_terms(project_id)
                    if (t.get("property_id") == prop and t.get("object_type") == obj)]
        if existing:
            t = existing[0]
            missing = {k: v for k, v in g["system_bindings"].items()
                       if k not in (t.get("system_bindings") or {})}
            if missing:
                update_term(project_id, t["id"],
                            {"system_bindings": {** (t.get("system_bindings") or {}), **missing}})
                merged.append(t["id"])
        else:
            t = create_term(project_id, g)
            created.append(t["id"])
    logger.info("dictionary.import_from_connections project=%s conns=%d created=%d merged=%d",
                project_id, len(conns), len(created), len(merged))
    return {"created": created, "merged": merged, "connections_scanned": len(conns)}


# ---------------------------------------------------------------------------
# 联邦查询（并行 + 归并 + 成本护栏 + 审计）
# ---------------------------------------------------------------------------

def _cost_estimate(covered: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """成本预估：每系统按取数模式给预计返回量/时延；全量拉取且超阈值 → needs_confirm。"""
    per: dict[str, Any] = {}
    total = 0
    needs_confirm = False
    for c in covered:
        mode = c.get("fetch_mode") or "paged"
        fl = int(c.get("fetch_limit") or 0)
        if mode == "full":
            est = fl if fl > 0 else 1000  # 未声明上限按 1000 保守估
        else:
            est = min(limit, fl) if fl > 0 else limit
        lat = int(c.get("last_latency_ms") or 0) or DEFAULT_LATENCY_EST_MS
        per[c.get("connection_id") or ""] = {
            "display_name": c.get("display_name"), "fetch_mode": mode,
            "est_rows": est, "est_latency_ms": lat,
        }
        total += est
        if mode == "full" and est > COST_CONFIRM_THRESHOLD:
            needs_confirm = True
    return {"needs_confirm": needs_confirm, "total_est_rows": total, "systems": per}


def _fetch_one(client, c: dict[str, Any], term: dict[str, Any],
               norm_value: str, limit: int) -> dict[str, Any]:
    """单系统取数（调用方在独立线程执行）：失败显式返回 error，不吞。"""
    sid = c.get("connection_id") or ""  # system_bindings 以 connection_id 为键（与 federated_lookup/_merge 一致）
    sys_field = (term.get("system_bindings") or {}).get(sid) or ""
    params: dict[str, Any] = {sys_field: norm_value}
    mode = c.get("fetch_mode") or "paged"
    if mode != "full":
        params["limit"] = limit  # 分页/limit 下推外部工具
    started = time.time()
    try:
        rows = client.invoke_tool(c, tool=c.get("read_tool") or "read", params=params)
    except ApiError as e:
        return {"status": "error", "error": str(e), "code": e.code,
                "latency_ms": int((time.time() - started) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"调用异常: {e}", "code": "MCP_READ_ERROR",
                "latency_ms": int((time.time() - started) * 1000)}
    latency_ms = int((time.time() - started) * 1000)
    rows = list(rows or [])
    # TCE-01-03 脏数据拒收：缺键字段/非对象行不进合并结果，显式计数
    clean: list[dict[str, Any]] = []
    rejected = 0
    for row in rows:
        if not isinstance(row, dict) or row.get(sys_field) in (None, ""):
            rejected += 1
            continue
        clean.append(row)
    # 全量拉取模式：外部系统可能不支持过滤下推，平台本地按键过滤（与下推值同口径：不 casefold）
    if mode == "full" and norm_value and sys_field:
        clean = [r for r in clean
                 if normalize_text(r.get(sys_field), {"casefold": False}) == norm_value]
    fl = int(c.get("fetch_limit") or 0)
    truncated = False
    if mode == "full" and fl > 0 and len(clean) > fl:
        clean = clean[:fl]  # 声明上限本地截断（外部不支持 limit，平台兜底）
        truncated = True
    if not clean and rejected:
        return {"status": "error", "code": "MCP_DIRTY_DATA",
                "error": f"系统 {sid} 返回 {rejected} 行脏数据（缺键字段 '{sys_field}' 或非对象行），全部拒收",
                "rejected_rows": rejected, "latency_ms": latency_ms}
    return {"status": "ok", "row_count": len(clean), "truncated": truncated,
            "fetch_mode": mode, "rejected_rows": rejected,
            "latency_ms": latency_ms, "records": clean}


def _merge(covered: list[dict[str, Any]], results: dict[str, dict[str, Any]],
           term: dict[str, Any], project_id: str, norm_value: str) -> list[dict[str, Any]]:
    """按 canonical key 归并：字段映射（term.property_id + 连接 field_map）→
    同属性跨系统值一致则合一，不一致则 conflict=true 并存；权威来源存在则取其值（其他值仍可查）。"""
    auth = term.get("authoritative_source")
    auth_aliases: set[str] = set()
    if auth:
        auth_aliases.add(auth)
        for c in covered:
            if c.get("connection_id") == auth:
                auth_aliases.add(c.get("display_name") or auth)
    fields_acc: dict[str, dict[str, Any]] = {}
    present_in: list[str] = []
    for c in covered:
        sid = c.get("connection_id") or ""
        r = results.get(sid) or {}
        if r.get("status") != "ok" or not r.get("records"):
            continue
        present_in.append((c.get("display_name") or sid))
        fmap, _src = resolve_field_map(project_id, c)
        sys_key_field = (term.get("system_bindings") or {}).get(sid) or ""
        prop_of = dict(fmap)
        if sys_key_field:
            prop_of[sys_key_field] = term.get("property_id") or sys_key_field
        for row in r["records"]:
            for k, v in (row or {}).items():
                prop = prop_of.get(k, k)  # 未映射字段保留原字段名
                cell = fields_acc.setdefault(prop, {"value": None, "conflict": False,
                                                     "authoritative": None, "sources": {}})
                cell["sources"][(c.get("display_name") or sid)] = v
    for prop, cell in fields_acc.items():
        vals = list(cell["sources"].values())
        if len({str(v) for v in vals}) <= 1:
            cell["value"] = vals[0] if vals else None
            cell["conflict"] = False
        else:
            cell["conflict"] = True
            hit = next((a for a in auth_aliases if a in cell["sources"]), None)
            if hit:
                cell["value"] = cell["sources"][hit]
                cell["authoritative"] = hit
            else:
                cell["value"] = None  # 冲突且无权威来源：两值并存，由调用方/用户裁决
    if not fields_acc:
        return []
    return [{
        "key": norm_value,
        "present_in": present_in,
        "fields": fields_acc,
    }]


def _record_audit(project_id: str, payload: dict[str, Any]) -> None:
    try:
        _audit(project_id).insert({"created_at": _now(), **payload})
    except Exception as e:  # noqa: BLE001 审计失败不阻断查询
        logger.warning("dictionary.audit_error project=%s err=%s", project_id, e)
    # M2-D / V2-09：字典域事件镜像到统一 append-only 审计日志
    try:
        import audit as audit_svc
        audit_svc.record(
            action=str(payload.get("event") or payload.get("action") or "dictionary_event"),
            actor=str(payload.get("actor") or "-"),
            project_id=project_id,
            target_type="dictionary",
            target_id=str(payload.get("term") or payload.get("target") or "")[:120],
            detail=payload,
        )
    except Exception:  # noqa: BLE001
        pass


def list_audit(project_id: str, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    items = _audit(project_id).all()
    items = sorted(items, key=lambda a: a.get("created_at") or "", reverse=True)
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "total": len(items)}


def federated_lookup(project_id: str, input_term: str, value: str = "",
                     systems: str = "", limit: int = 50, dry_run: bool = False,
                     confirm_cost: bool = False, actor: str = "mcp") -> dict[str, Any]:
    """联邦查询主入口（MCP term_lookup 与 REST /api/dictionary/term-lookup 共用）。

    status:
      ok                         查询成功（merged 可能为空数组——key 不存在，属合法结果）
      needs_disambiguation       多候选歧义，交人确认（附 candidates+置信度）
      term_not_found             无匹配词条（显式未覆盖，不猜测）
      preview                    dry_run 成本预览（未发起外呼）
      cost_confirmation_required 成本超阈值未确认（附 cost_estimate；可确认或取消）
      error                      全部覆盖系统失败（显式报错，不以空结果冒充成功）
    """
    t0 = time.time()
    limit = max(1, min(int(limit or 50), 500))
    res = resolve_term(project_id, input_term)
    term = res.get("term")
    filter_ids = {s.strip() for s in (systems or "").split(",") if s.strip()}

    conns = [c for c in _conns(project_id).all()
             if not filter_ids or (c.get("connection_id") in filter_ids
                                   or c.get("display_name") in filter_ids)]
    systems_view: dict[str, Any] = {}
    covered: list[dict[str, Any]] = []
    for c in conns:
        sid = c.get("connection_id") or ""
        sys_field = (term.get("system_bindings") or {}).get(sid) if term else None
        covered_flag = bool(sys_field)
        if covered_flag:
            covered.append(c)
        systems_view[sid] = {
            "display_name": c.get("display_name"),
            "covered": covered_flag,
            "system_field": sys_field,
        }
    base = {
        "input_term": input_term,
        "value": value,
        "resolution": {
            "canonical": term.get("canonical") if term else None,
            "term_id": term.get("id") if term else None,
            "match": res.get("match"),
            "confidence": res.get("confidence"),
            "candidates": res.get("candidates") or [],
        },
        "systems": systems_view,
        "uncovered": [sid for sid, v in systems_view.items() if not v["covered"]],
        "actor": actor,
    }

    def _finish(status: str, **extra) -> dict[str, Any]:
        out = {**base, "status": status, **extra}
        _record_audit(project_id, {
            "input_term": input_term, "value": value, "actor": actor,
            "resolution": base["resolution"], "status": status,
            "systems": systems_view, "uncovered": base["uncovered"],
            "merged_count": extra.get("merged_count"),
            "per_system": {sid: {"status": (r or {}).get("status"),
                                  "row_count": (r or {}).get("row_count"),
                                  "latency_ms": (r or {}).get("latency_ms")}
                           for sid, r in (extra.get("per_system") or {}).items()},
            "cost_ms": extra.get("cost_ms"),
            "dry_run": dry_run,
        })
        return out

    if term is None:
        if res.get("needs_disambiguation"):
            return _finish("needs_disambiguation",
                           candidates=res.get("candidates") or [],
                           hint="多个词条置信度接近，请从 candidates 人工选择（可将其晋升为同义词后重试）")
        return _finish("term_not_found",
                       hint="未找到匹配的词条（显式未覆盖，不猜测）。请补充数据字典后重试")

    # 键值归一化：NFKC+空白折叠，但【不做 casefold】——标识符大小写敏感（PN-100536 ≠ pn-100536），
    # casefold 只用于词条/字段名匹配（resolve_term），不用于键值
    norm_value = normalize_text(value, {**(term.get("normalization") or {}),
                                        "casefold": False})
    cost = _cost_estimate(covered, limit)
    if dry_run:
        return _finish("preview", cost_estimate=cost,
                       hint="预览仅估算成本，未发起外呼；确认无误后以 dry_run=false 执行")
    if not covered:
        return _finish("error", error="没有已覆盖系统（词条未绑定任何当前连接）",
                       per_system={}, cost_estimate=cost)
    if cost["needs_confirm"] and not confirm_cost:
        return _finish("cost_confirmation_required", cost_estimate=cost,
                       hint="存在仅支持全量拉取的系统且预计返回量超过阈值：请确认后以 confirm_cost=true 重新调用，"
                            "或放弃本次查询（取消即不调用；可先 dry_run=true 查看成本）")

    client = _mcp()
    results: dict[str, dict[str, Any]] = {}
    if covered:
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(covered)))) as ex:
            futs = {ex.submit(_fetch_one, client, c, term, norm_value, limit): c
                    for c in covered}
            for fut, c in futs.items():
                results[c.get("connection_id") or ""] = fut.result()
    # 回写实测时延（供后续成本估算）
    conn_store = _conns(project_id)
    for sid, r in results.items():
        if r.get("status") == "ok" and r.get("latency_ms"):
            conn_store.update(sid, {"last_latency_ms": int(r["latency_ms"])})

    ok = [sid for sid, r in results.items() if r.get("status") == "ok"]
    per_system = {sid: {k: r.get(k) for k in
                        ("status", "row_count", "truncated", "fetch_mode",
                         "rejected_rows", "latency_ms", "error", "code")}
                  for sid, r in results.items()}
    cost_ms = int((time.time() - t0) * 1000)
    if not ok:
        detail = "；".join(f"{sid}: {r.get('error') or r.get('code') or r.get('status')}"
                           for sid, r in results.items())
        return _finish("error",
                       error=f"全部覆盖系统查询失败（显式报错，不以空结果冒充成功）：{detail}",
                       per_system=per_system, cost_estimate=cost, cost_ms=cost_ms)
    merged = _merge(covered, results, term, project_id, norm_value)
    return _finish("ok", key_field=term.get("property_id"), value=norm_value,
                   merged=merged, merged_count=len(merged),
                   per_system=per_system, cost_estimate=cost, cost_ms=cost_ms)
