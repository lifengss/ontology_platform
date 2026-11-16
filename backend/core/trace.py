"""计算过程链 trace 服务（V2-22 / M4-J）。

设计口径（docs/V2-功能需求定义.md V2-22；验收 TC-22-01~05、TCE-22-01~03）：

1. 统一过程链结构。data.trace = {
       "trace_id": str,          # 本次执行唯一标识（每次执行一条记录，append-only）
       "steps": [ {seq, type, description, details, started_at, finished_at} ]
   }
   type ∈ data_fetch（从哪个实例的哪个属性取了什么值）/
          capability（哪个能力、版本、输入、lookup 命中、输出值）/
          action（哪个动作、入参、外部响应摘要、结果）/
          compute（和哪个实例的哪个属性、什么操作）/
          result（最终返回）。

2. 两层并存、互不替代（用户决策 2026-11-12）：
   - 实例级  能力「__capability__」最新血缘快照（覆盖式合理，服务当前值血缘）保留不动；
   - 执行级  process trace 历史（append-only）本模块新增。

3. 持久化独立于 include_trace 参数（用户决策 2026-11-12）：
   - include_trace 只控制「响应里是否返回过程」；
   - 落库是服务端审计义务，恒开（否则调用方不传参数时证据丢失，过程审计无据可查）。

4. 证据链关联：trace_id ↔ run_id ↔ action log_id ↔ 审计事件（事件级审计与过程级证据互查）。

5. 访问控制与体积：
   - trace 查看/导出走审计权限分级（仅管理员）；
   - step details 含实例属性值/动作入参（可能敏感）：集合类字段限量（前 N 条 + total）、大值截断。

非目标：不回填历史执行 trace（本需求实施后新执行起生效）；不跨租户共享 trace；
敏感字段脱敏规则另行决策（V1 全值 + 仅管理员）。
"""
from __future__ import annotations

import uuid
from typing import Any, Callable

from core.storage import JsonStore

# 集合类字段限量（前 N 条 + total，V2-22 访问控制与体积）
MAX_COLLECTION_ITEMS = 20
# 大值截断（超长字符串/大对象不落 detail，仅记字节大小）
MAX_STRING_LEN = 2000
MAX_DETAIL_BYTES = 20000

# trace step 类型（V2-22 统一结构）
TYPE_DATA_FETCH = "data_fetch"
TYPE_CAPABILITY = "capability"
TYPE_ACTION = "action"
TYPE_COMPUTE = "compute"
TYPE_RESULT = "result"

_KNOWN_TYPES = (TYPE_DATA_FETCH, TYPE_CAPABILITY, TYPE_ACTION, TYPE_COMPUTE, TYPE_RESULT)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _truncate_scalar(value: Any) -> Any:
    """大值截断：超长字符串截断；集合只保留前 N 条并附 total。"""
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) <= MAX_STRING_LEN:
            return value
        return {"truncated": True, "size": len(value),
                "preview": value[:MAX_STRING_LEN]}
    if isinstance(value, dict):
        return {k: _truncate_scalar(v) for k, v in list(value.items())[:MAX_COLLECTION_ITEMS]}
    if isinstance(value, (list, tuple)):
        truncated = len(value) > MAX_COLLECTION_ITEMS
        items = [_truncate_scalar(v) for v in value[:MAX_COLLECTION_ITEMS]]
        return {"items": items, "total": len(value), "truncated": truncated}
    if isinstance(value, (int, float, bool)):
        return value
    return str(value)


def detail_size_ok(details: dict[str, Any]) -> bool:
    """粗略估计 detail 序列化后体积是否超限（超限时落库截断，Response 仍全量）。"""
    try:
        import json

        return len(json.dumps(details, ensure_ascii=False)) <= MAX_DETAIL_BYTES
    except Exception:
        return False


def _safe_details(details: dict[str, Any] | None, limit: bool = False) -> dict[str, Any]:
    """构造落库/返回的 details。limit=True 时做集合类限量与截断（仅用于落库）。"""
    if not details:
        return {}
    if not limit:
        return dict(details)
    return {k: _truncate_scalar(v) for k, v in details.items()}


class TraceBuilder:
    """累积 steps，构造统一 trace 结构（响应随 include_trace 返回；落库逐次 append-only）。"""

    def __init__(self, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or uuid.uuid4().hex
        self.steps: list[dict[str, Any]] = []
        self._seq = 0

    def add(self, step_type: str, description: str,
            details: dict[str, Any] | None = None,
            started_at: str | None = None, finished_at: str | None = None) -> dict[str, Any]:
        """追加一个 step。step_type 严格白名单校验（V2-22 type 枚举）。"""
        if step_type not in _KNOWN_TYPES:
            step_type = TYPE_COMPUTE
        self._seq += 1
        step: dict[str, Any] = {
            "seq": self._seq,
            "type": step_type,
            "description": description,
            "details": dict(details) if details else {},
            "started_at": started_at or _now(),
            "finished_at": finished_at or _now(),
        }
        self.steps.append(step)
        return step

    def to_trace(self, limit: bool = False) -> dict[str, Any]:
        """输出统一 trace 结构。limit=True 对 steps 的 details 做集合限量截断（落库用）。"""
        return {
            "trace_id": self.trace_id,
            "steps": [
                {**s, "details": _safe_details(s.get("details"), limit=limit)}
                for s in self.steps
            ],
        }


def _trace_store(project_id: str | None = None) -> JsonStore:
    """trace_logs 集合（append-only：每次 insert 追加一条记录，不覆盖历史执行）。"""
    return JsonStore("trace_logs", project_id=project_id)


def record_trace(project_id: str | None, trace: dict[str, Any], *,
                 run_id: str | None = None, log_id: str | None = None,
                 audit_event_id: str | None = None,
                 source: str | None = None) -> dict[str, Any]:
    """落库一条过程 trace（恒开，独立于 include_trace 参数）。

    同一 trace 仅落一条记录（按 trace_id 去重，幂等：重复 record 不叠加）。
    证据链关联：trace_id ↔ run_id ↔ action log_id ↔ audit_event_id。
    """
    record = {
        "trace_id": trace.get("trace_id"),
        "steps": _safe_details({"steps": trace.get("steps") or []}, limit=True)["steps"],
        "run_id": run_id,
        "log_id": log_id,
        "audit_event_id": audit_event_id,
        "source": source,
        "recorded_at": _now(),
    }
    store = _trace_store(project_id)
    # 幂等：相同 trace_id 已落库则不重复追加（防止并发/重试叠写）
    for existing in store.all():
        if existing.get("trace_id") == record["trace_id"]:
            return existing
    store.insert(record)
    return record


def make_capability_steps(trace_id: str, cap: dict[str, Any],
                          result: dict[str, Any]) -> TraceBuilder:
    """由能力执行结果（四要素溯源 result['trace'] = inputs/null_inputs/lookups/refs）
    构造统一 steps（data_fetch + capability + compute + result）。"""
    b = TraceBuilder(trace_id)
    inner = (result or {}).get("trace") or {}

    input_steps = []
    for it in (inner.get("inputs") or []):
        input_steps.append({
            "attr": it.get("attr"),
            "value": it.get("value"),
            "source_channel": it.get("source_channel"),
            "source_system": it.get("source_system"),
        })

    if input_steps:
        b.add(TYPE_DATA_FETCH,
              (f"从实例 {result.get('instance_id')} 读取输入属性"
               f"（{', '.join(i['attr'] for i in input_steps if i['attr'])}）"),
              {"instance_id": result.get("instance_id"),
               "object_type": result.get("object_type"),
               "inputs": input_steps,
               "total_inputs": len(input_steps),
               "truncated": len(input_steps) > MAX_COLLECTION_ITEMS})

    for it in (inner.get("null_inputs") or []):
        b.add(TYPE_DATA_FETCH, f"输入属性 {it} 为空（null），参与计算时按空处理",
              {"attr": it, "value": None})

    lookup_count = len(inner.get("lookups") or [])
    for lk in (inner.get("lookups") or []):
        b.add(TYPE_DATA_FETCH, f"lookup 命中：{lk.get('attr') or lk.get('record_id') or ''}",
              {"attr": lk.get("attr"), "record_id": lk.get("record_id"),
               "code": lk.get("code"), "original": lk.get("original")})

    for rf in (inner.get("refs") or []):
        b.add(TYPE_COMPUTE, f"引用子能力 {rf.get('name')}（v{rf.get('version')}）",
              {"name": rf.get("name"), "version": rf.get("version")})

    b.add(TYPE_CAPABILITY,
          f"执行能力「{result.get('capability_name')}」v{result.get('capability_version')}",
          {"capability_id": result.get("capability_id"),
           "capability_name": result.get("capability_name"),
           "capability_version": result.get("capability_version"),
           "instance_id": result.get("instance_id"),
           "object_type": result.get("object_type"),
           "channel": result.get("execution_channel"),
           "status": result.get("status"),
           "lookup_count": lookup_count,
           "lookups": inner.get("lookups") or [],
           "value": result.get("value"),
           "error_code": result.get("error_code"),
           "error_message": result.get("error_message")})

    b.add(TYPE_RESULT,
          f"能力计算结果：{result.get('status')}",
          {"status": result.get("status"),
           "value": result.get("value"),
           "error_code": result.get("error_code"),
           "error_message": result.get("error_message")})
    return b


def make_action_steps(trace_id: str, action: dict[str, Any],
                      result: dict[str, Any]) -> TraceBuilder:
    """由 Action 执行结果构造统一 steps（action 入参/外部响应摘要/结果）。"""
    b = TraceBuilder(trace_id)
    planned = result.get("planned_payload") or {}
    b.add(TYPE_ACTION,
          f"执行动作「{action.get('action_id')}」，风险等级 {result.get('risk_level')}",
          {"action_id": action.get("action_id"),
           "risk_level": result.get("risk_level"),
           "dry_run": bool(result.get("dry_run")),
           "idempotency_key": result.get("idempotency_key"),
           "outcome": result.get("outcome"),
           "planned_payload": planned})

    if result.get("evaluation"):
        b.add(TYPE_ACTION, "中风险 AI 三条件评估", result.get("evaluation"))

    ext = result.get("external_response") or {}
    b.add(TYPE_ACTION,
          f"外部响应摘要：{ext.get('status') if ext else '（未回写/展示模式）'}",
          {"status": ext.get("status") if ext else None,
           "display_only": ext.get("display_only"),
           "error": ext.get("error")})

    if result.get("outcome") in ("awaiting_confirmation", "escalated_to_human",
                                 "cancelled", "circuit_open", "gate_failed"):
        b.add(TYPE_ACTION, f"结果：{result.get('outcome')}",
              {"outcome": result.get("outcome"),
               "confirmation_token": result.get("confirmation_token"),
               "cancel_reason": result.get("cancel_reason"),
               "reject_reason": result.get("reject_reason"),
               "gate_failures": result.get("gate_failures"),
               "retry_after": result.get("retry_after")})

    b.add(TYPE_RESULT, f"动作结果：{result.get('outcome')}",
          {"outcome": result.get("outcome"), "log_id": result.get("log_id"),
           "executed": bool(result.get("executed"))})
    return b


def make_instance_get_steps(trace_id: str, payload: dict[str, Any]) -> TraceBuilder:
    """由 instance_get 返回结果构造统一 steps（data_fetch + result）。"""
    b = TraceBuilder(trace_id)
    b.add(TYPE_DATA_FETCH,
          f"读取实例（{payload.get('object_type') or '全部类型'}），"
          f"共 {payload.get('total')} 条，返回 {payload.get('count')} 条",
          {"ontology_id": payload.get("ontology_id"),
           "object_type": payload.get("object_type"),
           "total": payload.get("total"),
           "count": payload.get("count"),
           "limit": payload.get("limit"),
           "offset": payload.get("offset"),
           "has_more": payload.get("has_more")})
    b.add(TYPE_RESULT, "实例查询结果摘要",
          {"total": payload.get("total"), "count": payload.get("count"),
           "objects": [r.get("object_type") for r in (payload.get("instances") or [])]})
    return b


def make_nl_steps(trace_id: str, source: str, plan: dict[str, Any],
                  outcome: dict[str, Any]) -> TraceBuilder:
    """由 NL 检索/行动执行构造统一 steps（意图解析 → 计划 → 执行 → 结果摘要）。"""
    b = TraceBuilder(trace_id)
    b.add(TYPE_DATA_FETCH, f"意图解析（来源：{source}）",
          {"source": source, "intent": plan.get("intent"),
           "confidence": plan.get("confidence")})
    if plan.get("query"):
        b.add(TYPE_DATA_FETCH, "生成查询计划", {"query": plan.get("query")})
    if plan.get("plan"):
        b.add(TYPE_COMPUTE, "生成编排/执行计划", plan.get("plan"))
    b.add(TYPE_RESULT, "NL 执行结果摘要",
          {"kind": plan.get("kind"), "matched": outcome.get("matched"),
           "total": outcome.get("total"), "status": outcome.get("status"),
           "trace_id": trace_id})
    return b
