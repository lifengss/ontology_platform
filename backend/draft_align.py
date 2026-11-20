"""多文档增量建模：确定性对齐层（extract-then-merge 的 align 环节）。

背景：同一本体建模常由多份文档先后解析而来。每份文档独立抽取（LLM 或确定性
兜底）产出的对象/属性 ID 彼此独立——第二份文档的「订单」与第一份的「订单」
ID 不同，直接 diff 会产生「新增对象+删除对象」的假变更。本模块在 diff 之前
做确定性 ID 对齐：

    通道 1  名称归一化精确相等（NFKC/去空白/casefold）
    通道 2  数据字典同义词（词条 object_type 绑定目标对象，输入名在 synonyms 中）——
            可选通道：字典未维护时为空，链路自动退化为 1+3，不依赖字典先行
    通道 3  确定性相似度（复用 dictionary._similarity：相等/包含/SequenceMatcher）

歧义（top1-top2 差距 < AMBIGUITY_GAP）或无候选 → **不对齐**（保持新增语义，
交差异审核由人工裁决，不猜测）。

对齐产物：
- aligned_source_model：源模型深拷贝，已对齐的对象/属性/关系 ID 重映射到目标侧，
  使 schemadiff.diff_ontology 按 ID 匹配时得到真实的成员级变更（新增属性/类型变化…）
- alignment_info：对齐明细（供 UI 展示与采纳后沉淀字典同义词）
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from dictionary import _similarity, list_terms, normalize_text

# 对象/属性级相似度阈值：对象误对齐代价高（属性会挂错对象），取比字典术语
# 解析（0.6）更严的口径；歧义判定与字典一致（top1-top2 < 0.1 交人工）。
SIM_THRESHOLD = 0.85
AMBIGUITY_GAP = 0.1


def _norm(s: Any) -> str:
    return normalize_text(s)


def _match_score(name: str, candidates: list[dict]) -> Optional[dict]:
    """在候选中为 name 找唯一胜出者。candidates: [{key, name, confidence, method}]。

    返回胜出 {key, name, method, confidence} 或 None（无候选/歧义）。
    精确与同义词通道 confidence=1.0；相似度通道取分值。
    """
    hits = [c for c in candidates if c["confidence"] >= SIM_THRESHOLD]
    if not hits:
        return None
    hits.sort(key=lambda x: (-x["confidence"], x["key"]))
    if len(hits) == 1:
        return hits[0]
    if hits[0]["confidence"] - hits[1]["confidence"] >= AMBIGUITY_GAP:
        return hits[0]
    return None  # 歧义：不对齐，不猜测


def _object_candidates(name: str, targets: list[dict], terms: list[dict]) -> list[dict]:
    """为一个源对象名生成候选目标对象（三通道合并，同 key 取最优通道）。"""
    n = _norm(name)
    if not n:
        return []
    cands: dict[str, dict] = {}
    for t in targets:
        key = t["id"]
        tname_n = _norm(t.get("name"))
        conf, method = 0.0, ""
        if n and n == tname_n:
            conf, method = 1.0, "exact"
        else:
            # 通道 2：字典同义词（词条绑定该目标对象）
            for term in terms:
                if _norm(term.get("object_type")) != _norm(key):
                    continue
                names = [term.get("canonical") or ""] + list(term.get("synonyms") or [])
                if any(n == _norm(x) for x in names if x):
                    conf, method = 1.0, "synonym"
                    break
            if conf < SIM_THRESHOLD:
                s = _similarity(n, tname_n) if tname_n else 0.0
                if s >= SIM_THRESHOLD:
                    conf, method = s, "semantic"
        if conf >= SIM_THRESHOLD:
            cands[key] = {"key": key, "name": t.get("name") or key, "confidence": conf, "method": method}
    return list(cands.values())


def align_model(target_model: dict[str, Any], source_model: dict[str, Any],
                project_id: Optional[str] = None) -> tuple[dict[str, Any], list[dict]]:
    """把 source 模型的对象/属性/关系 ID 对齐到 target 模型。

    返回 (aligned_source_model, alignment_info)。
    - ID 已相同的对象：不做对象级对齐，但成员（属性）仍按名称对齐（同 ID 对象内
      属性 ID 漂移同样会产生假变更）。
    - 对齐只改写 ID，不改名称/类型/约束等语义字段。
    """
    src = copy.deepcopy(source_model or {})
    tgt_objs = [o for o in (target_model or {}).get("objects") or [] if o.get("id")]
    src_objs = [o for o in src.get("objects") or [] if o.get("id")]
    if not tgt_objs:
        return src, []

    terms: list[dict] = []
    if project_id:
        try:
            terms = list_terms(project_id)
        except Exception:  # noqa: BLE001  字典不可用不影响对齐主链路
            terms = []
    targets = [{"id": o.get("id"), "name": o.get("name") or ""} for o in tgt_objs]

    obj_map: dict[str, dict] = {}   # src_obj_id -> 对齐明细
    id_map: dict[str, str] = {}     # src_obj_id -> tgt_obj_id
    for o in src_objs:
        sid = o.get("id")
        if any(t.get("id") == sid for t in tgt_objs):
            continue  # ID 相同：无需对象级对齐
        hit = _match_score(o.get("name") or sid, _object_candidates(o.get("name") or sid, targets, terms))
        if hit:
            id_map[sid] = hit["key"]
            obj_map[sid] = {
                "source_object_id": sid,
                "source_name": o.get("name") or "",
                "target_object_id": hit["key"],
                "target_name": hit["name"],
                "method": hit["method"],
                "confidence": round(hit["confidence"], 3),
                "attributes": [],
            }

    # 属性/关系对齐：每个源对象找到对应目标对象（ID 相同或已对齐）
    tgt_by_id = {o.get("id"): o for o in tgt_objs}
    for o in src.get("objects") or []:
        sid = o.get("id")
        tgt_obj = tgt_by_id.get(id_map.get(sid, sid))
        if tgt_obj is None:
            continue
        tgt_attr_ids = {a.get("id") for a in tgt_obj.get("attributes") or [] if a.get("id")}
        tgt_attrs = [{"id": a.get("id"), "name": a.get("name") or ""}
                     for a in tgt_obj.get("attributes") or [] if a.get("id")]
        attr_map_entry = obj_map.get(sid)
        attr_id_map: dict[str, str] = {}
        for a in o.get("attributes") or []:
            aid = a.get("id")
            if not aid or aid in tgt_attr_ids:
                continue
            an = a.get("name") or aid
            cands = [{"key": t["id"], "name": t["name"],
                      "confidence": 1.0 if _norm(an) and _norm(an) == _norm(t["name"]) else
                      (_similarity(_norm(an), _norm(t["name"])) if t["name"] else 0.0),
                      "method": "exact" if _norm(an) and _norm(an) == _norm(t["name"]) else "semantic"}
                     for t in tgt_attrs]
            hit = _match_score(an, cands)
            if hit:
                a["id"] = hit["key"]
                attr_id_map[aid] = hit["key"]
                if attr_map_entry is not None:
                    attr_map_entry["attributes"].append({
                        "source_attribute_id": aid,
                        "source_name": an,
                        "target_attribute_id": hit["key"],
                        "target_name": hit["name"],
                        "method": hit["method"],
                    })
        # 对象 stable_key 指向被重映射的属性时同步改写，避免 diff 出现假 stable_key_changed
        if o.get("stable_key") in attr_id_map:
            o["stable_key"] = attr_id_map[o["stable_key"]]
        # 关系：对象端点重映射 + （名称,目标对象）完全一致的关系 ID 对齐
        for r in o.get("relations") or []:
            if r.get("target_object") in id_map:
                r["target_object"] = id_map[r["target_object"]]
            rid = r.get("id")
            if not rid:
                continue
            rn = _norm(r.get("name"))
            rt = _norm(r.get("target_object"))
            if rn or rt:
                for tr in tgt_obj.get("relations") or []:
                    if (rn and rt and tr.get("id") != rid
                            and _norm(tr.get("name")) == rn and _norm(tr.get("target_object")) == rt):
                        r["id"] = tr.get("id")
                        break

    for o in src.get("objects") or []:
        if o.get("id") in id_map:
            o["id"] = id_map[o["id"]]

    info = list(obj_map.values())
    return src, info
