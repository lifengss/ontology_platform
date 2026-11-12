"""V2-21（M3-C）数据健康度评估——核心四维（零依赖，不依赖 M3-B 血缘/M4-A 字典治理）。

四维：
  1. 完整性  —— 键属性填充率 / 必填属性填充率（按属性细分）/ 关系指向解析率
  2. 唯一性  —— stable_key 重复率 / 缺键率
  3. 合规性  —— SHACL 违规率（按属性细分）/ 枚举值域命中率
  4. 新鲜度  —— 按来源连接的数据新鲜度（fetched_at 距今天数）/ 陈旧数据占比（阈值可配）

两个评估时点：
  - 入库时：批次健康卡 compute_batch_health（preview 落候选记录、人工确认卡展示）—— TP-21-06
  - 建模后：质量趋势线 evaluate_ontology（快照 + 口径/规模标注，基线=首次全量入库）—— TP-21-02

护栏原则：
  - 指标观测/告警为主，不阻断（不影响入库/查询）
  - 快照记录指标口径版本（constraint_hash/dictionary_hash/module_version），
    口径变化时趋势线标注 caliber_changed（EP-21-02，不把口径变化误报为质量下降）
  - 指标与规模（instance_count/批大小）同屏展示（scale 恒伴随）
  - 与公式引擎职责严格分界：不计算公式结果的业务正确性

执行通道（TP-21-07，结果按通道展示）：
  - local_snapshot    键/关系/新鲜度指标直接在本地关系快照执行（stable_key、关系值、
                      _source.fetched_at 均本地）
  - local_full        上传型实例值本地存，值指标（填充/SHACL/枚举）按本地值全量执行
  - governance_sampled 源连接型（external_mcp）实例的非键值指标，平台治理身份
                      （连接自带凭据，mcp_client）采样读源端最新值后合并计算
  - source_required   源连接型值指标不可读（连接缺失/读取失败/采样关闭）时显式标记，
                      不静默跳过、不臆测（coverage < 1 明示）
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from config import get_settings
from core.storage import JsonStore
from logging_setup import get_logger

logger = get_logger("data_quality")

MODULE_VERSION = "m3c-v1"
SNAPSHOTS = "quality_snapshots"


class QualityNotFound(Exception):
    """本体不存在（router 映射 404 NOT_FOUND）。"""


# ---------- 存储 ---------- #

def _snap_store(project_id: str) -> JsonStore:
    return JsonStore(SNAPSHOTS, project_id)


# ---------- 口径版本 ---------- #

def _ontology_hash(ontology: dict[str, Any]) -> str:
    """约束口径 hash：对象属性定义（含 enum 约束）+ 关系 + 业务约束（复合唯一）。"""
    core = {
        "objects": [
            {
                "id": o.get("id"),
                "stable_key": o.get("stable_key"),
                "attributes": [
                    {k: a.get(k) for k in ("id", "data_type", "required", "constraints")}
                    for a in o.get("attributes", [])
                ],
                "relations": [
                    {k: r.get(k) for k in ("id", "target_object")}
                    for r in o.get("relations", [])
                ],
            }
            for o in ontology.get("objects", [])
        ],
        "constraints": ontology.get("constraints", []),
    }
    raw = json.dumps(core, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _dictionary_hash(project_id: str) -> str:
    try:
        from dictionary import _terms
        terms = _terms(project_id).all()
    except Exception:  # 字典模块异常不阻断健康度
        return "unavailable"
    if not terms:
        return "none"
    raw = json.dumps(terms, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _caliber(project_id: str, ontology: dict[str, Any]) -> dict[str, str]:
    return {
        "constraint_hash": _ontology_hash(ontology),
        "dictionary_hash": _dictionary_hash(project_id),
        "module_version": MODULE_VERSION,
    }


# ---------- 数据加载 ---------- #

def get_ontology(project_id: str, ontology_id: str) -> Optional[dict[str, Any]]:
    return JsonStore("ontologies", project_id).get(ontology_id)


def _load_records(project_id: str, ontology_id: str) -> dict[str, list[dict[str, Any]]]:
    """本体内全部实例记录，按 object_type 分桶（只读本体归属，不截断）。"""
    from core.instance_store import get_instance_store
    istore = get_instance_store(project_id)
    out: dict[str, list[dict[str, Any]]] = {}
    for rec in istore.all():
        if rec.get("ontology_id") != ontology_id:
            continue
        otype = rec.get("object_type") or (rec.get("instance") or {}).get("object_type") or "?"
        out.setdefault(otype, []).append(rec)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_filled(v: Any) -> bool:
    """非空判定：None/""/[] 为空；0/False 视为已填。"""
    if v is None or v == "" or v == []:
        return False
    return True


def _valid_connection_ids(project_id: str) -> set[str]:
    """项目内真实 MCP 连接 id 集合。

    判定依据必须是 connection_id 而非 _source.kind：ingestion.apply_field_map
    对上传/MCP 两条通道恒写 kind="field_map"（M3-B 已确认），上传实例的
    connection_id=文件名（非连接 id），只有落在连接集合内的才是源连接型。
    """
    return {c.get("id") for c in JsonStore("mcp_connections", project_id).all() if c.get("id")}


def _is_source_conn(inst: dict[str, Any], valid_cids: set[str]) -> bool:
    """源连接型实例：_source.connection_id 是真实 MCP 连接（当前值须读源）。"""
    cid = (inst.get("_source") or {}).get("connection_id")
    return bool(cid) and cid in valid_cids


def _parse_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


# ---------- 治理身份采样（源连接型非键值指标） ---------- #

def _governance_sample(
    project_id: str,
    obj: dict[str, Any],
    src_recs: list[dict[str, Any]],
    sample_size: int,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """对源连接型实例按稳定键回读源端最新值（平台治理身份=连接自带凭据）。

    返回 {实例记录id: {属性id: 源端值}} 与错误明细。确定性采样：按存储顺序取前 sample_size 条。
    """
    from mcp_client import McpClient
    conn_store = JsonStore("mcp_connections", project_id)
    client = McpClient()
    sk = obj.get("stable_key")
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for rec in src_recs[:sample_size]:
        inst = rec.get("instance") or {}
        cid = (inst.get("_source") or {}).get("connection_id")
        conn = conn_store.get(cid) if cid else None
        if not conn:
            errors.append(f"连接 {cid or '?'} 不存在，无法读源")
            continue
        fmap = (conn.get("maps_to_ontology") or {}).get("field_map") or {}
        rev = {v: k for k, v in fmap.items()}  # 属性id → 源字段
        if not sk or sk not in rev:
            errors.append(f"连接 {cid} field_map 未映射主键 {sk}，无法按键回读")
            continue
        params = {rev[sk]: rec.get("stable_key_value")}
        try:
            rows = client.invoke_tool(conn, tool=conn.get("read_tool") or "read", params=params)
        except Exception as e:  # 鉴权/网络/超时
            errors.append(f"读源 {cid} 失败: {e}")
            continue
        if not rows:
            errors.append(f"源端无行 {sk}={rec.get('stable_key_value')}")
            continue
        row = rows[0] or {}
        vals = {aid: row[sf] for aid, sf in rev.items() if sf in row and _is_filled(row.get(sf))}
        merged[rec["id"]] = vals
    return merged, errors


# ---------- 关系指向解析（local_snapshot 通道） ---------- #

def _target_key_set(project_id: str, ontology: dict[str, Any], ttype: str) -> set[str]:
    """目标对象全部实例的稳定键值集合（关系值为字面量主键值，按此解析）。"""
    if not ttype:
        return set()
    from core.instance_store import get_instance_store
    tobj = next((o for o in ontology.get("objects", []) if o["id"] == ttype), None)
    tsk = (tobj or {}).get("stable_key")
    recs = get_instance_store(project_id).find(ttype, limit=None)
    out: set[str] = set()
    for r in recs:
        v = r.get("stable_key_value")
        if v in (None, "") and tsk:
            v = (r.get("instance") or {}).get(tsk)
        if v not in (None, ""):
            out.add(str(v))
    return out


# ---------- 对象级指标 ---------- #

def _obj_metrics(
    project_id: str,
    ontology: dict[str, Any],
    obj: dict[str, Any],
    records: list[dict[str, Any]],
    settings,
    valid_cids: set[str],
) -> dict[str, Any]:
    otype = obj["id"]
    sk = obj.get("stable_key")
    attrs = obj.get("attributes", [])
    rels = obj.get("relations", [])
    n = len(records)

    # 本地平坦实例（键/关系/新鲜度通道 = local_snapshot，直接用本地记录）
    local_flat = [(r.get("instance") or {}) for r in records]

    # ---- 唯一性（local_snapshot）---- #
    kv = [r.get("stable_key_value") for r in records]
    has_kv = [k for k in kv if k not in (None, "")]
    c = Counter(str(k) for k in has_kv)
    dup_excess = sum(cnt - 1 for cnt in c.values() if cnt > 1)
    uniqueness = {
        "channel": "local_snapshot",
        "stable_key": sk,
        "total": n,
        "missing_key_count": n - len(has_kv),
        "missing_key_rate": round((n - len(has_kv)) / n, 4) if n else 0.0,
        "dup_key_groups": sum(1 for cnt in c.values() if cnt > 1),
        "dup_key_count": dup_excess,
        "dup_rate": round(dup_excess / n, 4) if n else 0.0,
    }

    # ---- 值通道判定（源连接型实例走治理身份采样）---- #
    src_recs = [r for r in records if _is_source_conn(r.get("instance") or {}, valid_cids)]
    value_channel = "local_full"
    sample_meta: Optional[dict[str, Any]] = None
    eval_flat = list(local_flat)  # 值指标（填充/枚举/SHACL 由调用方统一跑）用的评估值
    if src_recs:
        if settings.data_quality_source_enabled:
            merged, errors = _governance_sample(project_id, obj, src_recs, settings.data_quality_sample_size)
            src_ids = {r["id"]: i for i, r in enumerate(records) if _is_source_conn(r.get("instance") or {}, valid_cids)}
            for rec_id, vals in merged.items():
                i = src_ids.get(rec_id)
                if i is not None:
                    inst = dict(local_flat[i])
                    inst.update(vals)  # 源端最新值覆盖本地值
                    eval_flat[i] = inst
            sample_meta = {
                "source_count": len(src_recs),
                "sample_count": len(merged),
                "errors": errors[:10],
            }
            value_channel = "governance_sampled" if merged else "source_required"
        else:
            value_channel = "source_required"
            sample_meta = {
                "source_count": len(src_recs),
                "sample_count": 0,
                "errors": ["源端采样已关闭（ONTOLOGY_DATA_QUALITY_SOURCE_ENABLED=false），值指标未读源"],
            }
    uncovered = len(src_recs) - (sample_meta or {}).get("sample_count", 0) if src_recs else 0
    coverage = 1.0 if n == 0 else round((n - uncovered) / n, 4)

    # ---- 完整性：键填充率 / 必填填充率 / 关系指向解析率 ---- #
    req_attrs = [a for a in attrs if a.get("required")]
    req_detail: dict[str, Any] = {}
    req_occ = 0
    req_filled = 0
    for a in req_attrs:
        aid = a["id"]
        filled = sum(1 for inst in eval_flat if _is_filled(inst.get(aid)))
        req_occ += n
        req_filled += filled
        req_detail[aid] = {
            "name": a.get("name"),
            "filled": filled,
            "total": n,
            "fill_rate": round(filled / n, 4) if n else 0.0,
        }
    rel_detail: dict[str, Any] = {}
    rel_vals_total = 0
    rel_resolved = 0
    for r in rels:
        rid = r["id"]
        ttype = r.get("target_object")
        vals: list[Any] = []
        for inst in local_flat:  # 关系值本地存（关系快照通道）
            v = inst.get(rid)
            if v in (None, ""):
                continue
            vals.extend(v if isinstance(v, list) else [v])
        if not vals:
            rel_detail[rid] = {"name": r.get("name"), "target_object": ttype, "total": 0,
                               "resolved": 0, "resolve_rate": None}
            continue
        tset = _target_key_set(project_id, ontology, ttype)
        res = sum(1 for v in vals if str(v) in tset)
        rel_vals_total += len(vals)
        rel_resolved += res
        rel_detail[rid] = {
            "name": r.get("name"), "target_object": ttype,
            "total": len(vals), "resolved": res,
            "resolve_rate": round(res / len(vals), 4),
        }
    completeness = {
        "channel": "local_snapshot",  # 键/关系本地；必填填充率随值通道
        "key_fill_rate": round(len(has_kv) / n, 4) if n and sk else None,
        "required_fill_rate": round(req_filled / req_occ, 4) if req_occ else None,
        "required_detail": req_detail,
        "relation_resolve_rate": round(rel_resolved / rel_vals_total, 4) if rel_vals_total else None,
        "relation_detail": rel_detail,
        "value_channel": value_channel,
        "coverage": coverage,
    }

    # ---- 合规性：枚举值域命中率（SHACL 由 evaluate_ontology 统一跑，结果回填）---- #
    enum_detail: dict[str, Any] = {}
    enum_total = 0
    enum_hit = 0
    for a in attrs:
        enum = (a.get("constraints") or {}).get("enum")
        if not enum:
            continue
        aid = a["id"]
        vals = [inst.get(aid) for inst in eval_flat if _is_filled(inst.get(aid))]
        if not vals:
            continue
        allowed = {str(x) for x in enum}
        h = sum(1 for v in vals if str(v) in allowed)
        enum_total += len(vals)
        enum_hit += h
        enum_detail[aid] = {
            "name": a.get("name"),
            "total": len(vals),
            "hit": h,
            "hit_rate": round(h / len(vals), 4),
            "out_of_enum": [str(v) for v in vals if str(v) not in allowed][:10],
        }
    conformity = {
        "channel": value_channel,
        "value_channel": value_channel,
        "coverage": coverage,
        "shacl_violation_count": None,
        "shacl_violation_rate": None,
        "shacl_by_attribute": None,
        "shacl_available": None,  # evaluate_ontology 回填
        "enum_hit_rate": round(enum_hit / enum_total, 4) if enum_total else None,
        "enum_detail": enum_detail,
    }

    return {
        "object_type": otype,
        "total": n,
        "uniqueness": uniqueness,
        "completeness": completeness,
        "conformity": conformity,
        "value_channel": value_channel,
        "sample": sample_meta,
        "coverage": coverage,
        "_eval_flat": eval_flat,  # 内部：供统一 SHACL 评估，输出前剔除
    }


# ---------- 新鲜度（local_snapshot 通道） ---------- #

def _timeliness(records_all: list[dict[str, Any]], settings, valid_cids: set[str]) -> dict[str, Any]:
    """新鲜度只统计源连接型实例（上传实例"取数即入库"，新鲜度=刚上传，不入陈旧口径）。"""
    by_conn: dict[str, dict[str, Any]] = {}
    for rec in records_all:
        src = (rec.get("instance") or {}).get("_source") or {}
        kind = src.get("kind")
        cid = src.get("connection_id")
        fa = src.get("fetched_at")
        if not cid or not fa or cid not in valid_cids:
            continue
        key = f"{kind}:{cid}"
        e = by_conn.setdefault(key, {"kind": kind, "connection_id": cid,
                                     "instance_count": 0, "last_fetched_at": None})
        e["instance_count"] += 1
        if fa > (e["last_fetched_at"] or ""):
            e["last_fetched_at"] = fa
    now = datetime.now(timezone.utc)
    connections: list[dict[str, Any]] = []
    stale_total = 0
    for key in sorted(by_conn):
        e = by_conn[key]
        fetched = _parse_iso(e["last_fetched_at"])
        age_days = round((now - fetched).total_seconds() / 86400, 2) if fetched else None
        stale = age_days is not None and age_days > settings.data_quality_stale_days
        if stale:
            stale_total += e["instance_count"]
        connections.append({
            **e,
            "age_days": age_days,
            "stale": stale,
            "stale_threshold_days": settings.data_quality_stale_days,
        })
    total = sum(e["instance_count"] for e in connections)
    return {
        "channel": "local_snapshot",
        "connections": connections,
        "stale_instance_count": stale_total,
        "stale_ratio": round(stale_total / total, 4) if total else None,
    }


# ---------- 统一 SHACL 评估（全本体一次） ---------- #

def _run_shacl(ontology: dict[str, Any], objs: dict[str, dict[str, Any]]) -> None:
    """对每个对象的评估值列表跑 SHACL，把违规率/按属性分布回填 conformity。

    逐对象跑（形状图为全本体、数据图为单对象实例）——focus_node 归属无歧义，
    与门禁（ingestion.approve 全批次跑）同口径同引擎（jena_adapter.client.shacl_validate）。
    """
    from modeling_translator import build_instance_graph, translate_to_shacl
    from jena_adapter.client import shacl_validate

    shapes = translate_to_shacl(ontology)
    for otype, m in objs.items():
        insts = m.pop("_eval_flat", [])
        res = shacl_validate(build_instance_graph(ontology, insts), shapes) if insts else None
        conf = m["conformity"]
        n = m["total"]
        if res is None:
            conf["shacl_available"] = False if insts else True  # 无实例不算不可用
            continue
        conf["shacl_available"] = True
        by_attr: dict[str, dict[str, Any]] = {}
        total_v = 0
        for v in res.get("violations", []):
            total_v += 1
            path = v.get("path") or ""
            attr = path.rsplit("#", 1)[-1] or "unknown"
            d = by_attr.setdefault(attr, {"count": 0, "messages": []})
            d["count"] += 1
            if len(d["messages"]) < 5:
                d["messages"].append(v.get("message") or "")
        conf["shacl_violation_count"] = total_v
        conf["shacl_violation_rate"] = round(total_v / n, 4) if n else 0.0
        conf["shacl_by_attribute"] = by_attr


# ---------- 本体级评估（建模后：质量趋势线的数据源） ---------- #

def evaluate_ontology(project_id: str, ontology_id: str, actor: str = "", save: bool = True) -> dict[str, Any]:
    """评估本体四维健康度并（save=True）落快照。指标恒伴随规模（scale）展示。"""
    onto = get_ontology(project_id, ontology_id)
    if not onto:
        raise QualityNotFound(ontology_id)
    settings = get_settings()
    by_obj = _load_records(project_id, ontology_id)
    all_recs = [r for v in by_obj.values() for r in v]
    valid_cids = _valid_connection_ids(project_id)

    objs: dict[str, dict[str, Any]] = {}
    for obj in onto.get("objects", []):
        objs[obj["id"]] = _obj_metrics(project_id, onto, obj, by_obj.get(obj["id"], []), settings, valid_cids)
    _run_shacl(onto, objs)

    n_total = len(all_recs)
    # ---- 汇总（按实例数加权） ---- #
    dup_total = sum(m["uniqueness"]["dup_key_count"] for m in objs.values())
    miss_total = sum(m["uniqueness"]["missing_key_count"] for m in objs.values()
                     if m["uniqueness"]["stable_key"])
    sk_objs_n = sum(m["total"] for m in objs.values() if m["uniqueness"]["stable_key"])
    req_occ = sum(m["completeness"]["required_detail"][a]["total"]
                  for m in objs.values() for a in m["completeness"]["required_detail"])
    req_filled = sum(m["completeness"]["required_detail"][a]["filled"]
                     for m in objs.values() for a in m["completeness"]["required_detail"])
    rel_tot = sum(m["completeness"]["relation_detail"][r]["total"]
                  for m in objs.values() for r in m["completeness"]["relation_detail"])
    rel_res = sum(m["completeness"]["relation_detail"][r]["resolved"]
                  for m in objs.values() for r in m["completeness"]["relation_detail"])
    shacl_tot = sum((m["conformity"]["shacl_violation_count"] or 0) for m in objs.values())
    enum_tot = sum(m["conformity"]["enum_detail"][a]["total"]
                   for m in objs.values() for a in m["conformity"]["enum_detail"])
    enum_hit = sum(m["conformity"]["enum_detail"][a]["hit"]
                   for m in objs.values() for a in m["conformity"]["enum_detail"])
    timeliness = _timeliness(all_recs, settings, valid_cids)

    summary = {
        "completeness": {
            "key_fill_rate": round((sk_objs_n - miss_total) / sk_objs_n, 4) if sk_objs_n else None,
            "required_fill_rate": round(req_filled / req_occ, 4) if req_occ else None,
            "relation_resolve_rate": round(rel_res / rel_tot, 4) if rel_tot else None,
        },
        "uniqueness": {
            "dup_rate": round(dup_total / n_total, 4) if n_total else 0.0,
            "missing_key_rate": round(miss_total / sk_objs_n, 4) if sk_objs_n else 0.0,
        },
        "conformity": {
            "shacl_violation_rate": round(shacl_tot / n_total, 4) if n_total else (
                0.0 if any(m["conformity"]["shacl_available"] for m in objs.values()) else None),
            "shacl_violation_count": shacl_tot,
            "enum_hit_rate": round(enum_hit / enum_tot, 4) if enum_tot else None,
        },
        "timeliness": timeliness,
    }
    # ---- 通道汇总（TP-21-07：结果按通道展示） ---- #
    channels = {
        "uniqueness": "local_snapshot",
        "relation_resolution": "local_snapshot",
        "timeliness": "local_snapshot",
        "value_metrics": {
            "local_full": sum(m["total"] for m in objs.values() if m["value_channel"] == "local_full"),
            "governance_sampled": sum((m["sample"] or {}).get("sample_count", 0)
                                      for m in objs.values() if m["value_channel"] == "governance_sampled"),
            "source_required": sum(m["total"] for m in objs.values()
                                   if m["value_channel"] == "source_required"),
        },
    }
    snap = {
        "id": uuid.uuid4().hex,
        "ontology_id": ontology_id,
        "computed_at": _now(),
        "computed_by": actor,
        "caliber": _caliber(project_id, onto),
        "scale": {
            "instance_count": n_total,
            "by_object": {k: len(v) for k, v in sorted(by_obj.items())},
        },
        "summary": summary,
        "objects": objs,
        "channels": channels,
    }
    if save:
        _snap_store(project_id).insert(snap)
        logger.info("M3-C 健康度快照 project=%s ontology=%s scale=%s actor=%s",
                    project_id, ontology_id, n_total, actor)
    return snap


# ---------- 快照 / 趋势（基线 + 口径/规模标注） ---------- #

def latest_snapshot(project_id: str, ontology_id: str) -> Optional[dict[str, Any]]:
    items = _snap_store(project_id).all()
    mine = [s for s in items if s.get("ontology_id") == ontology_id]
    if not mine:
        return None
    return max(mine, key=lambda s: s.get("computed_at") or "")


def list_snapshots(project_id: str, ontology_id: str, limit: int = 50) -> dict[str, Any]:
    """趋势序列（旧→新，附 caliber_changed/scale_changed/is_baseline 标注）。"""
    items = _snap_store(project_id).all()
    mine = sorted(
        (s for s in items if s.get("ontology_id") == ontology_id),
        key=lambda s: s.get("computed_at") or "",
    )
    for i, s in enumerate(mine):
        s["is_baseline"] = (i == 0)
        prev = mine[i - 1] if i else None
        s["caliber_changed"] = bool(prev and prev.get("caliber") != s.get("caliber"))
        s["scale_changed"] = bool(prev and (prev.get("scale") or {}).get("instance_count")
                                  != (s.get("scale") or {}).get("instance_count"))
    return {"items": mine[-limit:], "total": len(mine)}


# ---------- 批次健康卡（入库时：preview 落候选、确认卡展示） ---------- #

def compute_batch_health(project_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
    """对候选批次算健康卡：键重复（批内+对存量）/缺键/枚举违规分布/必填填充率/新鲜度。

    观测性指标，不阻断审批（审批仍走 SHACL 门禁）。"""
    insts = candidate.get("instances") or []
    otype = candidate.get("object_type") or ""
    n = len(insts)
    onto = get_ontology(project_id, candidate.get("ontology_id") or "")
    obj = next((o for o in (onto or {}).get("objects", []) if o["id"] == otype), None)
    sk = (obj or {}).get("stable_key")
    attrs = (obj or {}).get("attributes", [])

    # 键：批内重复 + 对存量重复
    kv = [str(i.get(sk)) for i in insts if sk and i.get(sk) not in (None, "")]
    c = Counter(kv)
    dup_within = sum(cnt - 1 for cnt in c.values() if cnt > 1)
    dup_groups = {k: cnt for k, cnt in c.items() if cnt > 1}
    from core.instance_store import get_instance_store
    existing: set[str] = set()
    if otype:
        for r in get_instance_store(project_id).find(otype, limit=None):
            v = r.get("stable_key_value")
            if v not in (None, ""):
                existing.add(str(v))
    dup_existing = sorted({k for k in set(kv) if k in existing})
    missing_key_count = (n - len(kv)) if sk else 0

    # 枚举违规分布（按属性）
    enum_violations: dict[str, Any] = {}
    enum_viol_total = 0
    for a in attrs:
        enum = (a.get("constraints") or {}).get("enum")
        if not enum:
            continue
        aid = a["id"]
        allowed = {str(x) for x in enum}
        bad = [str(v) for v in (i.get(aid) for i in insts)
               if _is_filled(v) and str(v) not in allowed]
        if bad:
            enum_violations[aid] = {"name": a.get("name"), "count": len(bad), "values": bad[:10]}
            enum_viol_total += len(bad)

    # 必填填充率
    fill_detail: dict[str, Any] = {}
    fill_occ = 0
    fill_ok = 0
    for a in attrs:
        if not a.get("required"):
            continue
        aid = a["id"]
        f = sum(1 for i in insts if _is_filled(i.get(aid)))
        fill_occ += n
        fill_ok += f
        fill_detail[aid] = {"name": a.get("name"), "filled": f, "total": n,
                            "fill_rate": round(f / n, 4) if n else 0.0}

    # 新鲜度：MCP 连接=源取数时间（fetched_at）；上传=现在
    now = datetime.now(timezone.utc)
    fetched_at = max((str(((i.get("_source") or {}).get("fetched_at")) or "") for i in insts), default="")
    if candidate.get("source_type") == "external_mcp":
        ft = _parse_iso(fetched_at)
        age_days = round((now - ft).total_seconds() / 86400, 2) if ft else None
        freshness = {"basis": "fetched_at", "fetched_at": fetched_at or None, "age_days": age_days}
    else:
        freshness = {"basis": "now", "fetched_at": None, "age_days": 0.0}

    return {
        "object_type": otype,
        "batch_size": n,
        "stable_key": sk,
        "key_missing_count": missing_key_count,
        "key_dup_within_batch": dup_within,
        "key_dup_groups": dup_groups,
        "key_dup_with_existing": dup_existing,
        "enum_violation_total": enum_viol_total,
        "enum_violations": enum_violations,
        "required_fill_rate": round(fill_ok / fill_occ, 4) if fill_occ else None,
        "required_detail": fill_detail,
        "freshness": freshness,
        "computed_at": _now(),
    }
