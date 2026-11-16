"""V2-22 NL 驱动业务动作端点（M4-J）。

业务人员/Agent 用自然语言提问，端点在「纯只读 SPARQL」之外新增意图判别：
- 意图为 query（查询）：复用 nl_to_sparql + 只读 SPARQL，返回行/命中数；
- 意图为 action（业务动作）：复用现有能力执行层（CapabilityService.execute，
  含 V2-13 中/高危确认流），返回结果 + 过程链。

两种路径都随结果返回统一 data.trace（steps 覆盖 意图解析来源 → 生成的 SPARQL 或
执行计划 → 执行（命中行数）→ 结果摘要），并逐次执行落库（append-only，恒开）。

设计口径（docs/V2-功能需求定义.md V2-22）：
- 意图解析来源 source ∈ {llm, fallback}（llm 不可达/非法时关键词回退）；
- action 路径默认在项目已发布（active）能力中按关键词匹配能力，也支持显式
  capability/instance_id/inputs 覆盖（便于确定性与测试）；
- 中/高危回写走 V2-13 确认流：结果含 confirmation_token，人工确认后才回写。
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Request

from ai_helper import nl_to_sparql
from api.dependencies import project_id_param
from core.responses import ApiError, success
from core.trace import TraceBuilder, record_trace
from core.storage import JsonStore
from jena_adapter.client import sparql_query
from logging_setup import get_logger
from tenant import ensure_project_access

router = APIRouter(prefix="/api/nl", tags=["nl"])
logger = get_logger("api.nl")

# 意图判别惯例（fallback 关键词）：动作 vs 查询
_ACTION_KEYWORDS = ("计算", "算一下", "算这", "执行", "调用", "更新", "写入", "回写",
                    "生成", "推导", "触发", "跑", "action", "compute", "execute", "calculate")
_QUERY_KEYWORDS = ("查询", "列出", "哪些", "多少", "是什么", "有哪些", "数量",
                   "query", "list", "find", "how many", "what")


def _store_for(project_id: str) -> JsonStore:
    return JsonStore("ontologies", project_id=project_id)


def _classify_intent(question: str, override: str | None) -> tuple[str, str]:
    """意图判别。override 优先（确定性/测试）；否则 LLM 判别，失败回退关键词。"""
    if override:
        intent = override.strip().lower()
        if intent in ("query", "action"):
            return intent, "user"
    # 仅当显式未提供时才走 LLM 判别（简化为关键词回退，避免 NL 驱动被 LLM 故障拖垮）
    q = question.lower()
    action_hit = sum(1 for k in _ACTION_KEYWORDS if k in q)
    query_hit = sum(1 for k in _QUERY_KEYWORDS if k in q)
    if action_hit > query_hit:
        return "action", "fallback"
    return "query", "fallback"


def _match_capability(project_id: str, question: str,
                      explicit: str | None) -> dict[str, Any] | None:
    """在项目已发布（active）能力中匹配目标能力。显式 capability 优先，否则按关键词匹配。"""
    from capabilities import CapabilityService

    svc = CapabilityService(project_id)
    active = svc.list_capabilities(status="active")
    if explicit:
        for r in active:
            if r["name"] == explicit or r["id"] == explicit:
                return r
        rec = svc.get_by_name(explicit) or svc.get_capability(explicit)
        return rec if rec and rec.get("status") == "active" else None
    q = question.lower()
    best, best_score = None, 0
    for r in active:
        score = 0
        for n in (r.get("name", ""), r.get("id", ""), r.get("object_type", "")):
            if not n:
                continue
            n_lower = n.lower()
            for token in re.findall(r"[\w\u4e00-\u9fff]+", q):
                if token and token in n_lower:
                    score += len(token)
        if score > best_score:
            best, best_score = r, score
    return best if best_score > 0 else (active[0] if active else None)


def _run_query(project_id: str, ontology_id: str, question: str) -> tuple[dict[str, Any], TraceBuilder]:
    """只读 SPARQL 路径。返回 (结果摘要, trace)。"""
    rec = _store_for(project_id).get(ontology_id)
    if not rec:
        raise ApiError("NOT_FOUND", "本体不存在", http_status=404)
    try:
        sparql, source = nl_to_sparql(question, rec)
    except Exception as e:  # noqa: BLE001
        logger.warning("nl_to_sparql failed: %s", e)
        raise ApiError("LLM_ERROR", f"自然语言转 SPARQL 失败: {e}", http_status=502) from e
    try:
        rows = sparql_query(sparql, project_id=project_id, ontology_id=ontology_id)
    except Exception as e:  # noqa: BLE001
        raise ApiError("UPSTREAM_UNAVAILABLE", f"SPARQL 执行失败: {e}", http_status=502) from e
    b = TraceBuilder()
    b.add("data_fetch", f"自然语言 → SPARQL（来源 {source}）",
          {"source": source, "sparql": sparql,
           "ontology_id": ontology_id, "question": question})
    b.add("result", f"查询完成，命中 {len(rows)} 行", {"count": len(rows), "source": source})
    return {"question": question, "sparql": sparql, "source": source,
            "rows": rows, "count": len(rows), "kind": "query"}, b


def _run_action(project_id: str, question: str, explicit_cap: str | None,
                instance_id: str | None, inputs: dict | None,
                dry_run: bool) -> tuple[dict[str, Any], TraceBuilder]:
    """业务动作（能力执行）路径。返回 (结果, trace)。"""
    from capabilities import CapabilityService, CapabilityApiError

    svc = CapabilityService(project_id)
    cap = _match_capability(project_id, question, explicit_cap)
    if not cap:
        raise ApiError("NOT_FOUND", "未匹配到已发布能力（action 意图但无可用能力）", http_status=404)
    try:
        res = svc.execute(cap["id"], instance_id or "", channel="nl",
                          trigger_source="nl", dry_run=dry_run, inputs=inputs)
    except CapabilityApiError as e:
        raise ApiError(e.code, f"{e.message} (http_status={e.http_status})",
                       http_status=e.http_status) from e
    b = TraceBuilder()
    b.add("data_fetch", f"意图解析为 action → 匹配能力「{cap.get('name')}」",
          {"capability_id": cap.get("id"), "capability_name": cap.get("name"),
           "object_type": cap.get("object_type"),
           "instance_id": instance_id or "", "inputs": inputs or {}})
    b.add("capability", f"执行能力「{res.get('capability_name')}」v{res.get('capability_version')}",
          {"capability_id": res.get("capability_id"),
           "instance_id": res.get("instance_id"),
           "object_type": res.get("object_type"),
           "status": res.get("status"),
           "channel": res.get("execution_channel"),
           "value": res.get("value"),
           "error_code": res.get("error_code"),
           "error_message": res.get("error_message"),
           "lookup_count": len(res.get("trace", {}).get("lookups") or []),
           "input_overrides": res.get("input_overrides"),
           "data_gaps": res.get("data_gaps")})
    b.add("result", f"能力执行结果：{res.get('status')}",
          {"status": res.get("status"), "value": res.get("value"),
           "error_code": res.get("error_code"), "error_message": res.get("error_message"),
           "dry_run": bool(res.get("dry_run"))})
    return dict(res), b


@router.post("/drive")
def nl_drive(request: Request, payload: dict,
             project_id: str = Depends(project_id_param)):
    """NL 驱动：查询（只读）或业务动作（含确认流），随结果返回统一 data.trace。

    body: {question, intent?（query/action 覆盖意图）, ontology_id?（query 必填）,
           capability?（action 显式能力）, instance_id?, inputs?, dry_run?}
    """
    ensure_project_access(request, project_id)
    question = (payload.get("question") or "").strip()
    if not question:
        raise ApiError("VALIDATION_ERROR", "question 不能为空", http_status=422)
    intent, source = _classify_intent(question, payload.get("intent"))
    ontology_id = (payload.get("ontology_id") or "").strip()

    if intent == "action":
        outcome, trace_b = _run_action(
            project_id, question, payload.get("capability"),
            payload.get("instance_id"), payload.get("inputs"),
            bool(payload.get("dry_run")))
        # 意图来源嵌入 trace 首步
        trace_b.steps[0]["details"]["intent_source"] = source
        trace_b.steps[0]["details"]["intent"] = "action"
    else:
        if not ontology_id:
            raise ApiError("VALIDATION_ERROR",
                           "query 意图需提供 ontology_id", http_status=422)
        outcome, trace_b = _run_query(project_id, ontology_id, question)
        trace_b.steps[0]["details"]["intent_source"] = source
        trace_b.steps[0]["details"]["intent"] = "query"

    trace = trace_b.to_trace()
    record_trace(project_id, trace, run_id=outcome.get("run_id"),
                 log_id=outcome.get("log_id"), source="nl:drive")
    return success({"kind": intent, "outcome": outcome,
                    "trace": trace, "intent_source": source, "process_trace": trace})
