"""V2-14 编排生成（M3-F）：SOP/skill 文本 → 编排草稿；AI 局部补全（片段）。

生成原则（对齐 V2-14 / TC-14）：
- AI 优先（ai_helper 统一通道，与 action_engine 三条件评估同模式），AI 不可用/输出非法 →
  确定性解析回退（关键词匹配已知 Action），并在草稿上记录 _gen_note（不静默）；
- 逐节点 grounding：{"basis": 原文/Action 定义依据, "confidence": 0.0~1.0, "origin": sop|skill|ai|ai_fragment}；
  未 grounded 的步骤 → capability_gap 节点（发布阻断 EP-14-05），不静默丢弃；
- 高危自动确认：risk_level=high 的 Action 节点前自动插入 confirm 控制节点；
- no-code 红线（INV-3）：源文本含代码特征 → 整段拒绝；AI 输出再过节点/参数/条件白名单，
  非法不合并、回退确定性；
- 执行路径不受影响（TP-14-10）：生成为编辑期能力，运行引擎自身不调 LLM。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from core.responses import ApiError
from logging_setup import get_logger
from orchestration import (
    ALLOWED_OPS,
    ALLOWED_VALUE_TYPES,
    check_no_code_redline,
    orchestrator,
)

logger = get_logger("orchestration.generation")

_MAX_STEPS = 30
_STEP_HEAD_RE = re.compile(
    r"^\s*(?:\d+\s*[\.、\)）]|[-•*]|步骤\s*[一二三四五六七八九十0-9]+|第\s*[一二三四五六七八九十0-9]+\s*[步条])\s*[::]?\s*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")


def _action_keywords(action: dict) -> set[str]:
    """Action 关键词集：名称/描述/输入字段的中英 token（中文名按连续中文切词近似）。"""
    text = " ".join(str(action.get(k) or "") for k in ("name", "description"))
    for field in (action.get("input_fields") or []):
        text += " " + (field.get("name") or "") + " " + (field.get("label") or "")
    kws = set()
    for m in _CJK_RE.finditer(text):
        chunk = m.group()
        kws.add(chunk)
        if len(chunk) > 2:  # 连续中文再切 2-gram，提高匹配粒度
            kws.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    for m in _ASCII_WORD_RE.finditer(text):
        kws.add(m.group().lower())
    return {k for k in kws if k}


def _step_tokens(step: str) -> set[str]:
    kws: set[str] = set()
    for m in _CJK_RE.finditer(step):
        chunk = m.group()
        kws.add(chunk)
        if len(chunk) > 2:
            kws.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    for m in _ASCII_WORD_RE.finditer(step):
        kws.add(m.group().lower())
    return {k for k in kws if k}


# --------------------------------------------------------------------------- #
# AI 片段生成
# --------------------------------------------------------------------------- #
def _ai_fragment(text: str, source_type: str, actions: dict[str, dict],
                 project_id: str, intent_mode: bool = False) -> dict | None:
    """AI 通道：自然语言 → 结构化片段 {nodes, edges}；不可用/非法 → None（调用方回退）。"""
    from ai_helper.client import call_llm
    from ai_helper.config import get_llm_config
    from config import get_settings
    cfg = get_llm_config()
    if not (cfg.base_url() and cfg.api_key()):
        logger.info("orch generation: LLM not configured, fallback deterministic (project=%s)", project_id)
        return None
    action_list = [
        {"action_id": a["action_id"], "name": a.get("name"), "description": a.get("description"),
         "input_fields": [{"name": f.get("name"), "label": f.get("label"),
                           "required": f.get("required")} for f in (a.get("input_fields") or [])][:10],
         "risk_level": a.get("risk_level", "high")}
        for a in actions.values()
    ]
    prompt = (
        "你是 no-code 流程编排设计器。把输入文本编排成工作流片段，严格遵守约束：\n"
        "1) 节点 kind 仅允许 start/end/action/branch/confirm/capability_gap；禁止代码、脚本、表达式节点；\n"
        "2) action 节点只能引用给定 action_id（不得虚构），参数 value_type 仅 literal/field_ref/trigger_ctx；\n"
        "3) branch 节点条件算子仅限 " + ",".join(sorted(ALLOWED_OPS)) + "，可用 AND/OR 组合，"
        "字段路径形如 node:<节点id>.<字段> 或 trigger.<字段>；\n"
        "4) 文本中无法对应任何 action 的步骤，输出 capability_gap 节点（gap_reason 说明原因），不得省略；\n"
        "5) 每个节点给 grounding：{\"basis\": \"来源原文或Action定义的简短引用\", \"confidence\": 0.0~1.0}；\n"
        "6) 恰好一个 start 节点；片段末端必须是 end 节点（或 capability_gap 直连 end）。\n\n"
        f"可用 Action 集合（仅可引用其中 id）：\n{json.dumps(action_list, ensure_ascii=False)[:8000]}\n\n"
        + (f"补全意图（限定插入到现有编排的锚点）：{text[:3000]}" if intent_mode
           else f"{'SOP' if source_type == 'sop' else 'skill'} 文本：\n{text[:6000]}")
        + "\n\n只返回一个 JSON（不要其它文字）："
          "{\"nodes\": [{\"id\",\"kind\",\"label\",\"action_id\",\"params\","
          "\"failure_policy\",\"branches\",\"confirm_title\",\"gap_reason\",\"grounding\"}], "
          "\"edges\": [{\"id\",\"from\",\"to\",\"from_port\"}]}"
    )
    try:
        result = call_llm(prompt, timeout=get_settings().writeback_ai_timeout_seconds)
    except Exception as e:  # noqa: BLE001 — AI 通道不可用（HTTP 错误/超时/网络）→ 回退确定性骨架
        logger.warning("orch generation: AI 通道不可用（%s），回退确定性生成", e)
        return None
    content = ((result.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    from action_engine import _extract_json
    obj = _extract_json(content)
    if not obj or not isinstance(obj.get("nodes"), list) or not isinstance(obj.get("edges"), list):
        logger.warning("orch generation: AI returned invalid JSON, fallback deterministic")
        return None
    return {"nodes": obj["nodes"], "edges": obj["edges"]}


def _fragment_valid(frag: dict, actions: dict[str, dict]) -> bool:
    """AI/确定性片段结构校验：节点白名单、action 引用、参数来源、条件算子、边闭合。"""
    frag = frag or {}
    nodes = frag.get("nodes") or []
    edges = frag.get("edges") or []
    if not nodes or not isinstance(nodes, list) or not isinstance(edges, list):
        return False
    ids = set()
    starts = 0
    for n in nodes:
        if not isinstance(n, dict) or not n.get("id"):
            return False
        kind = n.get("kind")
        if kind not in ("start", "end", "action", "branch", "confirm", "capability_gap"):
            return False
        if kind == "start":
            starts += 1
        if kind == "action":
            if n.get("action_id") not in actions:
                return False  # 虚构 action 引用 → 整段回退
            for pspec in (n.get("params") or {}).values():
                spec = pspec if isinstance(pspec, dict) else {"value_type": "literal"}
                if spec.get("value_type", "literal") not in ALLOWED_VALUE_TYPES:
                    return False
        if kind == "branch":
            for spec in (n.get("branches") or {}).values():
                cond = spec.get("condition") or {}
                if not _ops_allowed(cond):
                    return False
        ids.add(n["id"])
    if starts != 1:
        return False
    for e in edges:
        if not isinstance(e, dict) or e.get("from") not in ids or e.get("to") not in ids:
            return False
    # no-code 红线复查（含 AI 输出）
    return not check_no_code_redline(frag)


def _ops_allowed(cond: Any) -> bool:
    if not isinstance(cond, dict):
        return False
    for key, sub in cond.items():
        if key == "AND":
            if not isinstance(sub, list) or not all(_ops_allowed(c) for c in sub):
                return False
        elif key == "OR":
            if not isinstance(sub, list) or not all(_ops_allowed(c) for c in sub):
                return False
        else:
            if key not in ALLOWED_OPS or not isinstance(sub, list) or len(sub) != 2:
                return False
    return True


# --------------------------------------------------------------------------- #
# 确定性回退：SOP 步骤切分 + Action 关键词匹配（线性 DAG）
# --------------------------------------------------------------------------- #
def _extract_steps(text: str) -> list[str]:
    steps: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _STEP_HEAD_RE.match(line)
        if m:
            steps.append(line[m.end():].strip())
    if not steps:
        steps = [ln.strip() for ln in (text or "").splitlines() if ln.strip()][:_MAX_STEPS]
    return steps[:_MAX_STEPS]


def _match_action(step: str, actions: dict[str, dict]) -> tuple[str | None, float, str]:
    """关键词匹配：返回 (action_id, confidence, basis)；无匹配返回 (None, 0.0, "")。"""
    tokens = _step_tokens(step)
    best_id, best_score, best_action = None, 0, None
    for aid, act in actions.items():
        kws = _action_keywords(act)
        score = len(tokens & kws)
        if (act.get("name") or "").strip() and (act.get("name").strip() in step):
            score += 4  # 名称整词命中显著加权
        if score > best_score:
            best_id, best_score, best_action = aid, score, act
    if not best_id or best_score < 3:
        return None, 0.0, ""  # 阈值 3：低于此视为未 grounded（宁缺勿错，缺口走 capability_gap）
    conf = 0.9 if best_score >= 6 else min(0.85, 0.5 + 0.05 * best_score)
    return best_id, round(conf, 2), f"与 Action「{best_action.get('name') or best_id}」关键词匹配（得分 {best_score}）"


def _deterministic_fragment(text: str, actions: dict[str, dict], origin: str) -> dict:
    """确定性片段：线性 start → [confirm?] → action/gap → … → end。"""
    nodes: list[dict] = [{"id": "start", "kind": "start", "label": "开始", "grounding": None, "origin": origin}]
    edges: list[dict] = []
    steps = _extract_steps(text)
    prev = "start"
    seq = 0
    for step in steps:
        seq += 1
        aid, conf, basis = _match_action(step, actions)
        act = actions.get(aid) if aid else None
        if aid and act and act.get("risk_level") in ("high", "medium"):
            cid = f"cf{seq}"
            nodes.append({"id": cid, "kind": "confirm",
                          "label": f"确认：{step[:20]}"[:40],
                          "confirm_title": f"执行「{act.get('name') or aid}」前的人工确认",
                          "failure_policy": "abort",
                          "grounding": {"basis": basis + "（中/高危自动加确认）", "confidence": conf},
                          "origin": origin})
            edges.append({"id": f"e{seq}a", "from": prev, "to": cid})
            prev = cid
        if aid:
            nid = f"n{seq}"
            nodes.append({"id": nid, "kind": "action", "label": (act.get("name") or aid)[:40],
                          "action_id": aid, "params": {},
                          "failure_policy": "retry", "retry_max": 2,
                          "grounding": {"basis": basis, "confidence": conf}, "origin": origin})
        else:
            nid = f"g{seq}"
            nodes.append({"id": nid, "kind": "capability_gap",
                          "label": f"未覆盖步骤 {seq}"[:40],
                          "gap_reason": f"无法 grounded 的步骤：{step[:80]}",
                          "grounding": None, "origin": origin})
        edges.append({"id": f"e{seq}b", "from": prev, "to": nid})
        prev = nid
    nodes.append({"id": "end", "kind": "end", "label": "结束", "grounding": None, "origin": origin})
    edges.append({"id": f"e{seq + 1}", "from": prev, "to": "end"})
    return {"nodes": nodes, "edges": edges}


# --------------------------------------------------------------------------- #
# 草稿组装 / 生成入口
# --------------------------------------------------------------------------- #
def _assemble_spec(name: str, description: str, frag: dict, gen_note: str | None,
                   origin: str) -> dict:
    """把片段组装为可入库的 WorkflowCreate spec（trigger 固定 manual；草稿不发布）。"""
    nodes = [dict(n) for n in frag.get("nodes") or []]
    edges = [dict(e) for e in frag.get("edges") or []]
    for n in nodes:
        n.setdefault("origin", origin)
    spec: dict[str, Any] = {
        "name": name, "description": description,
        "trigger": {"type": "manual"},
        "nodes": nodes, "edges": edges,
    }
    if gen_note:
        spec["description"] = (description + f"｜{gen_note}").strip("｜") if description else gen_note
    return spec


def generate_draft(project_id: str, req: dict, actor: dict | None) -> dict:
    """SOP/skill 文本 → 编排草稿（status=draft，不发布）；代码内容整段拒绝（INV-3）。"""
    from core.storage import JsonStore
    import audit as audit_svc
    text = (req.get("text") or "").strip()
    source_type = req.get("source_type") or "sop"
    name = (req.get("name") or "").strip()
    if not text:
        raise ApiError("VALIDATION_ERROR", "SOP/skill 文本不能为空", http_status=422)
    violations = check_no_code_redline(text, "源文本")
    if violations:
        raise ApiError("CODE_REJECTED", "源文本含代码/脚本内容，整段拒绝（INV-3）："
                       + "；".join(violations[:5]), http_status=422)
    actions = {a["action_id"]: a for a in JsonStore("actions", project_id=project_id).all()
               if a.get("action_id")}
    if not actions:
        raise ApiError("NO_ACTIONS", "项目尚无可用 Action，无法生成编排草稿", http_status=422)
    frag = _ai_fragment(text, source_type, actions, project_id)
    if frag is None or not _fragment_valid(frag, actions):
        note = "AI 生成失败/输出非法，已回退确定性解析" if frag is not None else \
            "AI 不可用（未配置/失败），已回退确定性解析"
        frag = _deterministic_fragment(text, actions, origin=source_type)
    else:
        note = "AI 生成"
    for n in frag.get("nodes") or []:
        if isinstance(n, dict):
            n.setdefault("origin", "ai")
    spec = _assemble_spec(name or f"生成草稿：{text[:16]}",
                          f"由{('SOP' if source_type == 'sop' else 'skill')}文本生成（{note}）",
                          frag, None, source_type)
    rec = orchestrator.create(project_id, spec, actor=actor)
    gaps = [n["id"] for n in (rec.get("nodes") or []) if n.get("kind") == "capability_gap"]
    audit_svc.record(action="workflow_draft_generated", actor=(actor or {}).get("username") or "-",
                     project_id=project_id, target_type="workflow", target_id=rec.get("id"),
                     after={"source_type": source_type, "source": note,
                            "nodes": len(rec.get("nodes") or []), "gaps": gaps})
    return rec


def generate_fragment(project_id: str, wf_id: str, req: dict, actor: dict | None) -> dict:
    """AI 局部补全：意图 → 片段，插入现有编排锚点（仅限已绑定 Action 集合，不自动发布）。"""
    from core.storage import JsonStore
    import audit as audit_svc
    wf = orchestrator.get(project_id, wf_id)
    if wf.get("status") == "published":
        raise ApiError("WORKFLOW_PUBLISHED", "已发布编排不可修改，请先下线（un-publish）", http_status=409)
    intent = (req.get("intent") or "").strip()
    if not intent:
        raise ApiError("VALIDATION_ERROR", "补全意图（intent）不能为空", http_status=422)
    violations = check_no_code_redline(intent, "意图")
    if violations:
        raise ApiError("CODE_REJECTED", "意图含代码/脚本内容，拒绝（INV-3）：" + "；".join(violations[:5]),
                       http_status=422)
    # 可见 Action 集合限定为当前编排已绑定的 Action（不允许全局引用，防虚构/越权）
    bound = {n.get("action_id") for n in wf.get("nodes") or []
             if n.get("kind") == "action" and n.get("action_id")}
    store = JsonStore("actions", project_id=project_id)
    actions = {aid: store.get(aid) for aid in bound if store.get(aid)}
    if not actions:
        raise ApiError("NO_ACTIONS", "当前编排未绑定任何 Action，无可补全范围", http_status=422)
    frag = _ai_fragment(intent, "ai_fragment", actions, project_id, intent_mode=True)
    if frag is None or not _fragment_valid(frag, actions):
        raise ApiError("FRAGMENT_GEN_FAILED",
                       "AI 局部补全失败（不可用或输出非法：虚构 Action/非法节点/非法条件）；请重试或手动编辑",
                       http_status=422)
    new_nodes, new_edges = _insert_fragment(wf, frag, req)
    spec: dict[str, Any] = {"name": wf.get("name"), "description": wf.get("description") or "",
                            "trigger": wf.get("trigger") or {"type": "manual"},
                            "nodes": new_nodes, "edges": new_edges}
    rec = orchestrator.update(project_id, wf_id, spec, actor=actor)
    audit_svc.record(action="workflow_fragment_generated", actor=(actor or {}).get("username") or "-",
                     project_id=project_id, target_type="workflow", target_id=wf_id,
                     after={"intent": intent[:200], "after_node": req.get("after_node"),
                            "branch_node": req.get("branch_node"), "added_nodes": len(frag.get("nodes") or [])})
    return rec


def _insert_fragment(wf: dict, frag: dict, req: dict) -> tuple[list[dict], list[dict]]:
    """把片段插入现有 DAG：after_node 之后 / branch 端口之后 / 默认挂在末端节点后。

    片段内部 id 加唯一后缀避免冲突；锚点出边被替换为「锚点→片段起点、片段终点→原下游」。
    """
    suffix = uuid.uuid4().hex[:6]
    ren = {n["id"]: f"{n['id']}-{suffix}" for n in frag.get("nodes") or []}
    nodes = [dict(n, id=ren[n["id"]]) for n in frag.get("nodes") or []]
    for n in nodes:
        if isinstance(n.get("grounding"), dict):
            n["grounding"] = dict(n["grounding"])
            n["grounding"]["origin"] = "ai_fragment"
        n["origin"] = "ai_fragment"
    edges: list[dict] = []
    for i, e in enumerate(frag.get("edges") or []):
        e2 = {"id": f"ef-{i}-{suffix}", "from": ren.get(e.get("from")), "to": ren.get(e.get("to"))}
        if e.get("from_port"):
            e2["from_port"] = e["from_port"]
        edges.append(e2)
    by_id = {n["id"]: n for n in nodes}
    in_deg = {n["id"]: 0 for n in nodes}
    out_deg = {n["id"]: 0 for n in nodes}
    for e in edges:
        in_deg[e["to"]] = in_deg.get(e["to"], 0) + 1
        out_deg[e["from"]] = out_deg.get(e["from"], 0) + 1
    starts = [n for n in nodes if in_deg[n["id"]] == 0]
    ends = [n for n in nodes if out_deg[n["id"]] == 0]
    frag_start = starts[0]["id"] if starts else nodes[0]["id"]
    frag_end = ends[0]["id"] if ends else nodes[-1]["id"]

    wf_nodes: list[dict] = [dict(n) for n in wf.get("nodes") or []]
    wf_edges: list[dict] = [dict(e) for e in wf.get("edges") or []]
    anchor = req.get("after_node")
    if not anchor and req.get("branch_node") and req.get("port"):
        anchor = req["branch_node"]
    if not anchor:
        # 默认挂末端：无出边且非 end 的节点
        has_out = {e.get("from") for e in wf_edges}
        tail = [n["id"] for n in wf_nodes if n.get("kind") != "end" and n["id"] not in has_out]
        if not tail:
            raise ApiError("NO_ANCHOR", "编排中可插入的末端节点不存在，请显式指定 after_node/branch_node",
                           http_status=422)
        anchor = tail[0]
    if anchor not in {n["id"] for n in wf_nodes}:
        raise ApiError("NO_ANCHOR", f"锚点节点「{anchor}」不存在", http_status=422)
    branch_port = req.get("port") if req.get("branch_node") and req.get("port") else None
    downstream = [e["to"] for e in wf_edges
                  if e.get("from") == anchor and (branch_port in (None, e.get("from_port")))]
    if not downstream:
        raise ApiError("NO_ANCHOR", f"锚点节点「{anchor}」无下游边，无法插入片段", http_status=422)
    wf_edges = [e for e in wf_edges
                if not (e.get("from") == anchor and (branch_port in (None, e.get("from_port"))))]
    wf_edges.append({"id": f"ef-in-{suffix}", "from": anchor, "to": frag_start,
                     **({"from_port": branch_port} if branch_port else {})})
    for d in downstream:
        wf_edges.append({"id": f"ef-out-{d}-{suffix}", "from": frag_end, "to": d})
    return wf_nodes + nodes, wf_edges + edges
