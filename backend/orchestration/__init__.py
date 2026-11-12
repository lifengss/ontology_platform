"""V2-14 Action 编排（M3-F）——no-code 编排引擎。

核心设计（V2-14 功能需求 + TC-14/EP-14 用例对齐）：
- 节点类型仅 start/end/action/branch/confirm/capability_gap；代码/脚本/表达式节点一律拒绝（INV-3）。
- 参数值仅三种来源：literal / field_ref（node:<id>.<path>）/ trigger_ctx（trigger.<path>）。
- 分支条件仅受限算子（ALLOWED_OPS）+ AND/OR；字段缺失/类型不符 → 节点报错＋原因，
  绝不静默落到默认分支（EP-14-02）。
- 失败策略（action 节点）：retry（重试至 retry_max 耗尽 → 节点失败，运行失败）/
  skip（标记 skipped 继续下游）/ abort（运行中止，下游不执行）。
- 执行走确定性路径：引擎自身不调用 LLM（TP-14-10）；Action 层风险预评估（V2-13）复用。
- 触发：manual / event（required_ctx 缺项拒绝启动，不半启动，EP-14-03）/ cron（5 段表达式）。
- 运行持久化（JsonStore）＋中断恢复：启动时 running → interrupted；resume 从最后完成节点
  确定性重入（重放节点由 Action 层幂等键去重，TCEE-04）。
- 试运行（dry-run）：复用 Action 层 dry-run 机制，逐节点输入/输出预览，
  风险节点展示待确认状态；不产生外部调用。
- 能力缺口（capability_gap 节点 / 未 grounded 生成步骤）：存在即阻断发布（EP-14-05）。

审计事件（target_type=workflow / workflow_run）：
  workflow_create / workflow_update / workflow_publish / workflow_publish_blocked /
  workflow_draft_generated / workflow_fragment_generated / workflow_run_started /
  workflow_node_failed / workflow_run_finished / workflow_run_confirmed /
  workflow_run_rejected / workflow_run_interrupted / workflow_run_resumed
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

import audit as audit_svc
from core.responses import ApiError
from core.storage import JsonStore
from schemas.orchestration import (
    ALLOWED_NODE_KINDS,
    ALLOWED_OPS,
    ALLOWED_VALUE_TYPES,
    CODE_PATTERN_TOKENS,
)

logger = logging.getLogger(__name__)

RUN_TERMINAL = {"succeeded", "failed", "aborted", "cancelled"}
NODE_TERMINAL = {"success", "skipped", "not_reached", "failed"}
#: 条件字段路径：node:<id>[.field...] 或 trigger[.field...]
_FIELD_PATH_RE = re.compile(r"^(?:node:[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*|trigger(?:\.[A-Za-z0-9_\-]+)*)$")
#: 节点 dict 顶层禁止键（no-code 红线，INV-3）
_FORBIDDEN_NODE_KEYS = {"code", "script", "expression", "expr", "eval", "command", "shell", "javascript", "python", "onerror"}


class ConditionError(Exception):
    """分支条件求值错误（字段缺失/类型不符/无匹配分支）——节点报错，不静默走默认分支。"""


class NodeFailure(Exception):
    """节点执行失败（参数缺失/Action 不存在/门禁失败/外部失败重试耗尽/能力缺口）。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _contains_code(token: str, obj: Any, path: str, found: list[str]) -> None:
    """递归扫描 dict 键名与字符串值，发现代码/脚本特征即记录（INV-3 拒绝）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in _FORBIDDEN_NODE_KEYS:
                found.append(f"{path}.{k}（禁止的 code/脚本字段）")
            else:
                _contains_code(token, v, f"{path}.{k}", found)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _contains_code(token, v, f"{path}[{i}]", found)
    elif isinstance(obj, str):
        low = obj.lower()
        for tok in CODE_PATTERN_TOKENS:
            if tok in low:
                found.append(f"{path}（疑似代码内容：{tok.strip()}）")
                return


def check_no_code_redline(obj: Any, what: str = "请求体") -> list[str]:
    """INV-3 no-code 红线检查：返回违规描述列表（空 = 通过）。"""
    found: list[str] = []
    _contains_code(what, obj, what, found)
    return found


def _validate_field_path(path: Any) -> str | None:
    """校验字段引用路径合法；返回错误描述或 None。"""
    if not isinstance(path, str) or not _FIELD_PATH_RE.match(path):
        return f"非法字段引用路径「{path!r}」：仅支持 node:<节点ID>[.字段] 或 trigger[.字段]"
    return None


def _node_output_ctx(external_response: Any) -> dict:
    """节点输出上下文（字段引用 node:<id>.body.<字段> 与状态查询用）。

    外部回写契约保持 {"status", "body": str} 不变；此处把 body 若为 JSON 对象则
    解析为 dict，便于分支条件/下游参数按 node:<id>.body.<字段> 引用外部返回值。
    """
    if not isinstance(external_response, dict):
        return {}
    ctx = dict(external_response)
    body = ctx.get("body")
    if isinstance(body, str) and body.strip():
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            ctx["body"] = parsed
    return ctx


def resolve_field(ctx: dict[str, Any], path: str) -> tuple[bool, Any]:
    """从执行上下文解析字段引用。返回 (found, value)。"""
    if path.startswith("node:"):
        rest = path[5:]
        parts = rest.split(".")
        node_id = parts[0]
        if node_id not in ctx.get("nodes", {}):
            return False, None
        cur: Any = ctx["nodes"][node_id]
        for p in parts[1:]:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return False, None
        return True, cur
    # trigger[.field...]
    parts = path.split(".")
    cur = ctx.get("trigger")
    if parts == ["trigger"]:
        return isinstance(cur, dict), (cur or {})
    for p in parts[1:]:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return False, None
    return True, cur


def _type_mismatch(left: Any, value: Any) -> bool:
    """eq/ne 标量类型一致性：str 与 数字 互比视为类型不符（不隐式转换）。"""
    left_num = _is_number(left)
    value_num = _is_number(value)
    if left_num != value_num:
        if isinstance(left, str) or isinstance(value, str):
            return True
    return False


def eval_rule(rule: dict[str, Any], ctx: dict[str, Any]) -> bool:
    """单条条件规则求值（严格类型检查：缺失/类型不符 → ConditionError，EP-14-02）。"""
    field = rule.get("field")
    op = rule.get("op")
    value = rule.get("value")
    if op in ("is_empty", "is_not_empty"):
        found, left = resolve_field(ctx, field)
        if not found:
            raise ConditionError(f"字段缺失：{field}（条件求值不静默，节点报错）")
        empty = left is None or left == "" or left == [] or left == {}
        return empty if op == "is_empty" else not empty
    found, left = resolve_field(ctx, field)
    if not found:
        raise ConditionError(f"字段缺失：{field}（类型不符/缺失 → 节点报错＋原因，不静默落到默认分支）")
    if op in ("gt", "ge", "lt", "le"):
        if not (_is_number(left) and _is_number(value)):
            raise ConditionError(
                f"类型不符：{field}={left!r}（{type(left).__name__}）/ value={value!r}（{type(value).__name__}），比较要求数字"
            )
        if op == "gt":
            return left > value
        if op == "ge":
            return left >= value
        if op == "lt":
            return left < value
        return left >= value
    if op in ("eq", "ne"):
        if _type_mismatch(left, value):
            raise ConditionError(f"类型不符：{field}={type(left).__name__} 与 value={type(value).__name__} 不可比较")
        same = left == value
        return same if op == "eq" else not same
    if op == "contains":
        if isinstance(left, str):
            return str(value) in left
        if isinstance(left, (list, tuple)):
            return value in left
        raise ConditionError(f"类型不符：contains 要求左值为 str/list，实际 {type(left).__name__}")
    if op == "in_set":
        if not isinstance(value, (list, tuple, set)):
            raise ConditionError(f"类型不符：in_set 的 value 必须是列表，实际 {type(value).__name__}")
        return left in list(value)
    raise ConditionError(f"未知算子：{op}（仅允许 {sorted(ALLOWED_OPS)}）")


def eval_condition(cond: dict[str, Any], ctx: dict[str, Any]) -> bool:
    logic = cond.get("logic", "AND")
    if logic not in ("AND", "OR"):
        raise ConditionError(f"非法逻辑连接词：{logic}（仅允许 AND/OR）")
    rules = cond.get("rules") or []
    if not rules:
        raise ConditionError("条件规则为空（不允许无条件恒真分支，须显式声明默认分支）")
    results = [eval_rule(r, ctx) for r in rules]
    return all(results) if logic == "AND" else any(results)


# --------------------------------------------------------------------------- #
# cron（5 段：分 时 日 月 周）
# --------------------------------------------------------------------------- #
def _cron_field_ok(field: str, lo: int, hi: int) -> bool:
    for part in field.split(","):
        if not part:
            return False
        if part == "*":
            continue
        if part.startswith("*/"):
            step = part[2:]
            if not step.isdigit() or int(step) < 1:
                return False
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit() and lo <= int(a) <= hi and lo <= int(b) <= hi):
                return False
            continue
        if not part.isdigit() or not (lo <= int(part) <= hi):
            return False
    return True


def validate_cron_expr(expr: Any) -> str | None:
    """校验 5 段 cron 表达式；返回错误描述或 None。"""
    if not isinstance(expr, str):
        return "cron 表达式必须是字符串"
    parts = expr.split()
    if len(parts) != 5:
        return f"cron 表达式必须是 5 段（分 时 日 月 周），实际 {len(parts)} 段"
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    for i, (field, (lo, hi)) in enumerate(zip(parts, ranges)):
        if not _cron_field_ok(field, lo, hi):
            names = ["分", "时", "日", "月", "周"]
            return f"cron 第 {i + 1} 段（{names[i]}）不合法：{field!r}"
    return None


def _cron_field_match(field: str, value: int) -> bool:
    for part in field.split(","):
        if part == "*":
            return True
        if part.startswith("*/"):
            step = int(part[2:])
            if value % step == 0:
                return True
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if int(a) <= value <= int(b):
                return True
            continue
        if int(part) == value:
            return True
    return False


def cron_matches(expr: str, dt: datetime) -> bool:
    """5 段 cron 是否匹配给定时刻（周：0=周日）。"""
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    return (
        _cron_field_match(minute, dt.minute)
        and _cron_field_match(hour, dt.hour)
        and _cron_field_match(dom, dt.day)
        and _cron_field_match(month, dt.month)
        and _cron_field_match(dow, dt.weekday() % 7)
    )


# --------------------------------------------------------------------------- #
# 工作流校验（no-code 红线 + 结构）
# --------------------------------------------------------------------------- #
def validate_workflow(wf: dict[str, Any], actions_by_id: dict[str, dict] | None = None,
                      for_publish: bool = False) -> list[str]:
    """校验工作流定义，返回错误列表（空 = 通过）。

    - INV-3 no-code 红线：节点禁止 code/script/表达式字段；参数仅 literal/field_ref/trigger_ctx；
      分支条件仅受限算子 + AND/OR。
    - 结构：恰好一个 start、至少一个 end、边引用存在、branch 端口唯一出边、无环、全部可达。
    - for_publish：额外要求 action 节点引用的 Action 存在且已发布。
    """
    errors: list[str] = []
    nodes: list[dict] = wf.get("nodes") or []
    edges: list[dict] = wf.get("edges") or []
    trigger = wf.get("trigger") or {}

    # --- no-code 红线（对原始 dict 扫描，pydantic extra=forbid 之外的兜底）---
    redline = check_no_code_redline({"nodes": nodes, "edges": edges, "trigger": trigger}, "工作流定义")
    if redline:
        errors.append("no-code 红线（INV-3）：检测到代码/脚本/表达式内容，已拒绝：" + "；".join(redline[:5]))

    ids = [n.get("id") for n in nodes]
    id_set = set(ids)
    if len(ids) != len(id_set):
        errors.append("节点 ID 重复")
    starts = [n for n in nodes if n.get("kind") == "start"]
    ends = [n for n in nodes if n.get("kind") == "end"]
    if len(starts) != 1:
        errors.append(f"必须恰好一个 start 节点，实际 {len(starts)}")
    if not ends:
        errors.append("必须至少一个 end 节点")

    for n in nodes:
        nid, kind = n.get("id"), n.get("kind")
        if kind not in ALLOWED_NODE_KINDS:
            errors.append(f"节点 {nid}：不支持的节点类型「{kind}」（no-code 红线，代码/脚本节点被拒绝）")
            continue
        # 参数校验（action 节点）
        for pname, pspec in (n.get("params") or {}).items():
            vt = pspec.get("value_type") if isinstance(pspec, dict) else None
            if vt not in ALLOWED_VALUE_TYPES:
                errors.append(f"节点 {nid}：参数 {pname} 取值来源「{vt}」不合法（仅 literal/field_ref/trigger_ctx，INV-3）")
                continue
            val = pspec.get("value")
            if vt in ("field_ref", "trigger_ctx"):
                expected = "node:<节点ID>" if vt == "field_ref" else "trigger"
                if not isinstance(val, str) or (
                    (vt == "field_ref" and not val.startswith("node:"))
                    or (vt == "trigger_ctx" and not val.startswith("trigger"))
                ):
                    errors.append(f"节点 {nid}：参数 {pname} 的 {vt} 值「{val!r}」不合法（须为 {expected}[.字段] 路径）")
            elif vt == "literal":
                found: list[str] = []
                _contains_code(pname, val, f"节点{nid}参数{pname}", found)
                if found:
                    errors.append(f"节点 {nid}：参数 {pname} 含疑似代码内容（INV-3）：" + "；".join(found[:3]))
        # 分支校验
        if kind == "branch":
            branches = n.get("branches") or {}
            if not branches:
                errors.append(f"节点 {nid}：branch 至少需要一个分支端口")
            default_cnt = 0
            for pid, spec in branches.items():
                is_default = bool(spec.get("is_default"))
                cond = spec.get("condition")
                if is_default:
                    default_cnt += 1
                    continue
                if not isinstance(cond, dict):
                    errors.append(f"节点 {nid} 端口 {pid}：非默认分支必须显式声明条件（不允许无条件分支静默兜底）")
                    continue
                logic = cond.get("logic", "AND")
                if logic not in ("AND", "OR"):
                    errors.append(f"节点 {nid} 端口 {pid}：逻辑连接词「{logic}」不合法（仅 AND/OR）")
                rules = cond.get("rules") or []
                if not rules:
                    errors.append(f"节点 {nid} 端口 {pid}：条件规则为空（须显式默认分支兜底）")
                for r in rules:
                    if not isinstance(r, dict):
                        errors.append(f"节点 {nid} 端口 {pid}：规则必须为对象")
                        continue
                    fp_err = _validate_field_path(r.get("field"))
                    if fp_err:
                        errors.append(f"节点 {nid} 端口 {pid}：{fp_err}")
                    if r.get("op") not in ALLOWED_OPS:
                        errors.append(f"节点 {nid} 端口 {pid}：算子「{r.get('op')}」不合法（仅 {sorted(ALLOWED_OPS)}，INV-3）")
            if default_cnt > 1:
                errors.append(f"节点 {nid}：branch 最多一个默认分支（is_default）")
        # action 节点引用校验
        if kind == "action":
            if not n.get("action_id"):
                errors.append(f"节点 {nid}：action 节点必须引用 action_id")
            elif actions_by_id is not None:
                act = actions_by_id.get(n["action_id"])
                if act is None:
                    errors.append(f"节点 {nid}：引用的 Action「{n['action_id']}」不存在")
                elif for_publish and act.get("status") not in (None, "active", "published"):
                    errors.append(f"节点 {nid}：引用的 Action「{n['action_id']}」未处于可用状态（{act.get('status')}）")
            if n.get("failure_policy") not in ("retry", "skip", "abort"):
                errors.append(f"节点 {nid}：失败策略「{n.get('failure_policy')}」不合法（仅 retry/skip/abort）")
            rm = n.get("retry_max", 2)
            if not isinstance(rm, int) or not (1 <= rm <= 10):
                errors.append(f"节点 {nid}：retry_max 必须在 1~10 之间")

    # --- 边与图结构 ---
    edge_keys = set()
    for e in edges:
        f, t, port = e.get("from"), e.get("to"), e.get("from_port")
        if f not in id_set or t not in id_set:
            errors.append(f"边 {e.get('id', '?')}：引用了不存在的节点（{f} → {t}）")
        key = (f, port)
        if key in edge_keys:
            errors.append(f"边 {e.get('id', '?')}：同一输出端口（{f}:{port}）只能有一条出边")
        edge_keys.add(key)
        if f == t:
            errors.append(f"边 {e.get('id', '?')}：不允许自环")
    # 可达性（从 start BFS）
    if starts:
        adj: dict[str, list[str]] = {i: [] for i in id_set}
        for e in edges:
            if e.get("from") in adj:
                adj[e["from"]].append(e.get("to"))
        seen, stack = set(), [starts[0]["id"]]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(adj.get(cur, []))
        for i in id_set:
            if i not in seen:
                errors.append(f"节点 {i}：从 start 不可达")
        # 环检测（DFS 三色）
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {i: WHITE for i in id_set}

        def _dfs(u: str) -> bool:
            color[u] = GRAY
            for v in adj.get(u, []):
                if color.get(v) == GRAY:
                    return True
                if color.get(v) == WHITE and _dfs(v):
                    return True
            color[u] = BLACK
            return False

        for i in id_set:
            if color.get(i) == WHITE and _dfs(i):
                errors.append("工作流存在环（编排必须是 DAG，循环须用定时/事件重新触发）")
                break

    # --- 触发器 ---
    ttype = trigger.get("type", "manual")
    if ttype not in ("manual", "event", "cron"):
        errors.append(f"触发器类型「{ttype}」不合法（仅 manual/event/cron）")
    if ttype == "event" and not trigger.get("event_topic"):
        errors.append("event 触发必须配置 event_topic")
    if ttype == "cron":
        cerr = validate_cron_expr(trigger.get("cron_expr"))
        if cerr:
            errors.append(f"cron 触发：{cerr}")
    for f in trigger.get("required_ctx") or []:
        if not isinstance(f, str) or not f.strip():
            errors.append("required_ctx 必须为非空字段名（点分路径）")

    return errors


# --------------------------------------------------------------------------- #
# 编排引擎
# --------------------------------------------------------------------------- #
class Orchestrator:
    """no-code 编排引擎（进程内单例 orchestrator）。"""

    # ---------- 存储 ---------- #
    def _wf_store(self, project_id: str) -> JsonStore:
        return JsonStore("workflows", project_id=project_id)

    def _run_store(self, project_id: str) -> JsonStore:
        return JsonStore("workflow_runs", project_id=project_id)

    def _actions_by_id(self, project_id: str) -> dict[str, dict]:
        return {a["action_id"]: a for a in self._action_store(project_id).all() if a.get("action_id")}

    @staticmethod
    def _action_store(project_id: str) -> JsonStore:
        return JsonStore("actions", project_id=project_id)

    # ---------- CRUD ---------- #
    def create(self, project_id: str, spec: dict, actor: dict | None) -> dict:
        wf = {
            "id": f"wf-{uuid.uuid4().hex[:12]}",
            "name": spec.get("name", "未命名编排"),
            "description": spec.get("description", ""),
            "status": "draft",
            "version": 0,
            "nodes": spec.get("nodes") or [],
            "edges": spec.get("edges") or [],
            "trigger": spec.get("trigger") or {"type": "manual"},
            "capability_gaps": self._collect_gaps(spec.get("nodes") or []),
            "created_by": (actor or {}).get("username"),
            "created_at": _now(),
            "updated_at": _now(),
            "published_at": None,
        }
        errors = validate_workflow(wf, self._actions_by_id(project_id), for_publish=False)
        if errors:
            raise ApiError("VALIDATION_ERROR", "编排校验未通过：" + "；".join(errors[:5]), http_status=422)
        rec = self._wf_store(project_id).insert(wf)
        audit_svc.record(action="workflow_create", actor=(actor or {}).get("username") or "-",
                         project_id=project_id, target_type="workflow", target_id=rec["id"],
                         after={"name": wf["name"], "nodes": len(wf["nodes"]),
                                "trigger": wf["trigger"].get("type")})
        return rec

    def get(self, project_id: str, wf_id: str) -> dict:
        rec = self._wf_store(project_id).get(wf_id)
        if not rec:
            raise ApiError("NOT_FOUND", "编排工作流不存在", http_status=404)
        return rec

    def list(self, project_id: str, page: int = 1, page_size: int = 20,
             status: str | None = None) -> list[dict]:
        items = [r for r in self._wf_store(project_id).all()
                 if status in (None, "", r.get("status"))]
        items.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return items[(page - 1) * page_size: (page - 1) * page_size + page_size]

    def update(self, project_id: str, wf_id: str, spec: dict, actor: dict | None) -> dict:
        wf = self.get(project_id, wf_id)
        if wf.get("status") == "published":
            raise ApiError("WORKFLOW_PUBLISHED", "已发布编排不可直接修改（需先降级为草稿 un-publish）", http_status=409)
        patch = {
            "name": spec.get("name", wf.get("name")),
            "description": spec.get("description", wf.get("description", "")),
            "nodes": spec.get("nodes", wf.get("nodes") or []),
            "edges": spec.get("edges", wf.get("edges") or []),
            "trigger": spec.get("trigger", wf.get("trigger") or {"type": "manual"}),
        }
        candidate = {**wf, **patch}
        errors = validate_workflow(candidate, self._actions_by_id(project_id), for_publish=False)
        if errors:
            raise ApiError("VALIDATION_ERROR", "编排校验未通过：" + "；".join(errors[:5]), http_status=422)
        patch["capability_gaps"] = self._collect_gaps(patch["nodes"])
        patch["updated_at"] = _now()
        rec = self._wf_store(project_id).update(wf_id, patch)
        audit_svc.record(action="workflow_update", actor=(actor or {}).get("username") or "-",
                         project_id=project_id, target_type="workflow", target_id=wf_id,
                         after={"nodes": len(patch["nodes"]), "gaps": len(patch["capability_gaps"])})
        return rec

    def delete(self, project_id: str, wf_id: str, actor: dict | None) -> None:
        wf = self.get(project_id, wf_id)
        if wf.get("status") == "published":
            raise ApiError("WORKFLOW_PUBLISHED", "已发布编排不可删除（请归档）", http_status=409)
        self._wf_store(project_id).delete(wf_id)
        audit_svc.record(action="workflow_delete", actor=(actor or {}).get("username") or "-",
                         project_id=project_id, target_type="workflow", target_id=wf_id)

    def un_publish(self, project_id: str, wf_id: str, actor: dict | None) -> dict:
        wf = self.get(project_id, wf_id)
        if wf.get("status") != "published":
            raise ApiError("WORKFLOW_NOT_PUBLISHED", "编排未处于已发布状态", http_status=409)
        rec = self._wf_store(project_id).update(wf_id, {"status": "draft", "updated_at": _now()})
        audit_svc.record(action="workflow_unpublish", actor=(actor or {}).get("username") or "-",
                         project_id=project_id, target_type="workflow", target_id=wf_id)
        return rec

    @staticmethod
    def _collect_gaps(nodes: list[dict]) -> list[dict]:
        gaps = []
        for n in nodes or []:
            if n.get("kind") == "capability_gap":
                gaps.append({"node_id": n.get("id"), "label": n.get("label", ""),
                             "reason": n.get("gap_reason", "未 grounded 的步骤（SOP/skill 生成）")})
        return gaps

    # ---------- 发布（EP-14-05：能力缺口未解决 → 阻断） ---------- #
    def publish(self, project_id: str, wf_id: str, actor: dict | None) -> dict:
        wf = self.get(project_id, wf_id)
        if wf.get("status") == "published":
            raise ApiError("WORKFLOW_PUBLISHED", "编排已处于已发布状态", http_status=409)
        errors = validate_workflow(wf, self._actions_by_id(project_id), for_publish=True)
        if errors:
            raise ApiError("VALIDATION_ERROR", "发布校验未通过：" + "；".join(errors[:5]), http_status=422)
        gaps = self._collect_gaps(wf.get("nodes") or [])
        if gaps:
            # EP-14-05：能力缺口未解决 → 阻断发布，提示未匹配项与原因
            audit_svc.record(action="workflow_publish_blocked", actor=(actor or {}).get("username") or "-",
                             project_id=project_id, target_type="workflow", target_id=wf_id,
                             after={"blocked": True, "gaps": gaps})
            raise ApiError("CAPABILITY_GAP_UNRESOLVED",
                           "存在未解决能力缺口，发布被阻断：" +
                           "；".join(f"{g['label'] or g['node_id']}（{g['reason']}）" for g in gaps),
                           http_status=409)
        rec = self._wf_store(project_id).update(wf_id, {
            "status": "published", "version": (wf.get("version") or 0) + 1,
            "published_at": _now(), "updated_at": _now(), "capability_gaps": [],
        })
        audit_svc.record(action="workflow_publish", actor=(actor or {}).get("username") or "-",
                         project_id=project_id, target_type="workflow", target_id=wf_id,
                         after={"version": rec.get("version"), "nodes": len(wf.get("nodes") or [])})
        return rec

    # ---------- 试运行（dry-run：复用 Action 层 dry-run，无外部调用） ---------- #
    def dry_run(self, project_id: str, wf_id: str, payload: dict,
                sample_outputs: dict[str, dict] | None = None,
                actor: dict | None = None) -> dict:
        from action_engine import engine as action_engine

        wf = self.get(project_id, wf_id)
        sample_outputs = sample_outputs or {}
        preview_errors = validate_workflow(wf, self._actions_by_id(project_id), for_publish=False)
        if preview_errors:
            return {"dry_run": True, "valid": False, "errors": preview_errors, "nodes": []}
        actions = self._actions_by_id(project_id)
        ctx: dict[str, Any] = {"trigger": payload or {}, "nodes": {}}
        node_by_id = {n["id"]: n for n in wf["nodes"]}
        previews: list[dict] = []
        # 拓扑遍历（start 起，分支按样例数据选择端口；未选路径标记 not_reached）
        order: list[str] = []
        dead: set[str] = set()
        seen: set[str] = set()
        stack = [n for n in wf["nodes"] if n["kind"] == "start"]
        adj: dict[str, list[dict]] = {}
        for e in wf["edges"]:
            adj.setdefault(e["from"], []).append(e)

        def _expand(start_id: str) -> None:
            st = [start_id]
            while st:
                cur = st.pop()
                if cur in seen or cur in dead:
                    continue
                if cur not in node_by_id:
                    continue
                seen.add(cur)
                order.append(cur)
                node = node_by_id[cur]
                if node["kind"] != "branch":
                    for e in adj.get(cur, []):
                        st.append(e["to"])
                    continue
                # branch：用样例/触发数据选端口
                ports = node.get("branches") or {}
                selected = None
                for pid, spec in ports.items():
                    if spec.get("is_default"):
                        continue
                    cond = spec.get("condition")
                    if not cond:
                        continue
                    try:
                        if eval_condition(cond, ctx):
                            selected = pid
                            break
                    except ConditionError:
                        selected = "__error__"
                        break
                if selected in (None, "__error__") and any(s.get("is_default") for s in ports.values()):
                    selected = next(p for p, s in ports.items() if s.get("is_default"))
                for e in adj.get(cur, []):
                    if selected in (None, "__error__"):
                        dead.add(e["to"])
                        continue
                    if (e.get("from_port") or None) == (selected or None):
                        st.append(e["to"])
                    else:
                        dead.add(e["to"])

        _expand((node for node in wf["nodes"] if node["kind"] == "start").__next__()["id"])

        for nid in order + [i for i in node_by_id if i not in seen and i not in dead]:
            node = node_by_id[nid]
            kind = node["kind"]
            if nid in dead:
                previews.append({"node_id": nid, "kind": kind, "label": node.get("label", ""),
                                 "status": "not_reached", "note": "当前样例数据下未选中的分支路径（预览）"})
                continue
            if kind == "start":
                previews.append({"node_id": nid, "kind": kind, "label": node.get("label", ""),
                                 "status": "ok", "input": payload or {}, "output": {}})
                continue
            if kind == "end":
                previews.append({"node_id": nid, "kind": kind, "label": node.get("label", ""),
                                 "status": "ok", "output": {}})
                continue
            if kind == "confirm":
                previews.append({"node_id": nid, "kind": kind, "label": node.get("label", ""),
                                 "status": "awaiting_confirmation",
                                 "note": "风险节点待确认状态（dry-run 预览，不真实暂停）"})
                continue
            if kind == "capability_gap":
                previews.append({"node_id": nid, "kind": kind, "label": node.get("label", ""),
                                 "status": "capability_gap", "note": node.get("gap_reason", "")})
                continue
            if kind == "branch":
                ports = node.get("branches") or {}
                entry = {"node_id": nid, "kind": kind, "label": node.get("label", ""),
                         "status": "preview", "ports": {}}
                selected = None
                try:
                    for pid, spec in ports.items():
                        if spec.get("is_default"):
                            continue
                        cond = spec.get("condition")
                        if not cond:
                            continue
                        try:
                            ok = eval_condition(cond, ctx)
                        except ConditionError as ce:
                            entry["status"] = "error"
                            entry["error"] = str(ce)
                            break
                        entry["ports"][pid] = {"matched": ok}
                        if ok:
                            selected = pid
                            break
                    else:
                        if selected is None:
                            if any(s.get("is_default") for s in ports.values()):
                                selected = next(p for p, s in ports.items() if s.get("is_default"))
                            else:
                                entry["status"] = "error"
                                entry["error"] = "无匹配分支且未声明默认分支（不静默兜底）"
                except ConditionError as ce:
                    entry["status"] = "error"
                    entry["error"] = str(ce)
                if entry.get("status") == "preview" and selected:
                    entry["selected_port"] = selected
                previews.append(entry)
                continue
            # action 节点
            act = actions.get(node.get("action_id") or "")
            if not act:
                previews.append({"node_id": nid, "kind": kind, "label": node.get("label", ""),
                                 "status": "error", "error": f"Action「{node.get('action_id')}」不存在"})
                continue
            input_preview: dict[str, Any] = {}
            unresolved: list[str] = []
            for pname, pspec in (node.get("params") or {}).items():
                vt = pspec.get("value_type") if isinstance(pspec, dict) else None
                if vt == "literal":
                    input_preview[pname] = pspec.get("value")
                elif vt in ("field_ref", "trigger_ctx"):
                    found, val = resolve_field(ctx, pspec.get("value"))
                    if found:
                        input_preview[pname] = val
                    else:
                        unresolved.append(f"{pname}（{pspec.get('value')}）")
            res = action_engine.execute(act, {"input": input_preview, "dry_run": True},
                                        project_id=project_id, actor=actor,
                                        trigger_source="api")
            risk = act.get("risk_level") or "high"
            gate = res.get("gate") or {}
            status = "preview_ok" if gate.get("passed", True) else "gate_failed"
            if risk in ("medium", "high"):
                # 风险节点展示待确认状态（真实执行时的行为预告）
                status = "awaiting_confirmation"
            sample = sample_outputs.get(nid)
            previews.append({
                "node_id": nid, "kind": kind, "label": node.get("label", ""),
                "status": status, "risk_level": risk,
                "input": input_preview,
                "unresolved_params": unresolved,
                "gate": gate,
                "planned_payload": res.get("planned_payload"),
                "output_preview": sample if sample is not None else {"dry_run": True, "note": "dry-run 不产生真实输出；可传 sample_outputs 预览下游"},
            })
            ctx["nodes"][nid] = sample if sample is not None else (res.get("planned_payload") or {})
        return {"dry_run": True, "valid": True, "nodes": previews}

    # ---------- 运行生命周期 ---------- #
    def get_run(self, project_id: str, run_id: str) -> dict:
        rec = self._run_store(project_id).get(run_id)
        if not rec:
            raise ApiError("NOT_FOUND", "编排运行不存在", http_status=404)
        return rec

    def list_runs(self, project_id: str, workflow_id: str | None = None,
                  page: int = 1, page_size: int = 20) -> list[dict]:
        items = [r for r in self._run_store(project_id).all()
                 if workflow_id in (None, "", r.get("workflow_id"))]
        items.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return items[(page - 1) * page_size: (page - 1) * page_size + page_size]

    def _wf_of_run(self, project_id: str, run: dict) -> dict:
        wf = self._wf_store(project_id).get(run.get("workflow_id") or "")
        if not wf:
            raise ApiError("NOT_FOUND", "运行对应的工作流不存在", http_status=404)
        return wf

    @staticmethod
    def _init_node_states(wf: dict) -> dict[str, dict]:
        return {n["id"]: {"status": "not_executed", "attempts": 0} for n in wf["nodes"]}

    def _ready_nodes(self, wf: dict, states: dict[str, dict]) -> list[dict]:
        """就绪节点：全部前驱已终态（success/skipped/not_reached）且自身未执行。"""
        node_by_id = {n["id"]: n for n in wf["nodes"]}
        preds: dict[str, list[str]] = {n["id"]: [] for n in wf["nodes"]}
        for e in wf.get("edges") or []:
            if e.get("to") in preds:
                preds[e["to"]].append(e.get("from"))
        ready = []
        for n in wf["nodes"]:
            st = states.get(n["id"]) or {}
            if st.get("status") != "not_executed":
                continue
            plist = preds[n["id"]]
            if n["kind"] == "start" or all(
                (states.get(p) or {}).get("status") in ("success", "skipped", "not_reached")
                for p in plist
            ):
                ready.append(n)
        return ready

    def _mark_downstream_unreached(self, wf: dict, states: dict[str, dict], from_ids: list[str]) -> None:
        """失败/未选分支的下游（无法再被驱动到达的）标记 not_reached——下游不执行。

        根节点（失败节点本身/未选中端口目标）若仍是 not_executed 一并标记；
        无论根节点当前状态如何（已 failed/success），都展开其下游传递标记，
        否则失败节点的下游会残留 not_executed（状态口径不闭合）。
        """
        adj: dict[str, list[str]] = {}
        for e in wf.get("edges") or []:
            adj.setdefault(e.get("from"), []).append(e.get("to"))
        roots = set(from_ids)
        marked: set[str] = set()
        stack = list(from_ids)
        while stack:
            cur = stack.pop()
            if cur in marked:
                continue
            marked.add(cur)
            st = states.get(cur)
            if st and st.get("status") == "not_executed":
                st["status"] = "not_reached"
            # 根节点（必然不可达源）或本 pass 标记的节点才展开下游，避免误伤多父节点活路径
            if cur in roots or (st and st.get("status") == "not_reached"):
                stack.extend(adj.get(cur, []))

    def start_run(self, project_id: str, wf: dict, payload: dict, actor: dict | None,
                  trigger_source: str = "manual", topic: str | None = None) -> dict:
        """启动一次编排运行（同步执行至完成或暂停在确认点）。执行路径确定性（TP-14-10：引擎不调 LLM）。"""
        if wf.get("status") != "published":
            raise ApiError("WORKFLOW_NOT_PUBLISHED", "仅已发布编排可触发执行", http_status=409)
        errors = validate_workflow(wf, self._actions_by_id(project_id), for_publish=True)
        if errors:
            raise ApiError("VALIDATION_ERROR", "编排校验未通过：" + "；".join(errors[:5]), http_status=422)
        # EP-14-03：event 触发 payload 缺 required_ctx → 拒绝启动（不半启动）
        required = (wf.get("trigger") or {}).get("required_ctx") or []
        if trigger_source == "event" and required:
            missing = []
            for f in required:
                found, _ = resolve_field({"trigger": payload or {}}, f if f.startswith("trigger") else f"trigger.{f}")
                if not found:
                    missing.append(f)
            if missing:
                audit_svc.record(action="workflow_run_rejected_start",
                                 actor=(actor or {}).get("username") or "-",
                                 project_id=project_id, target_type="workflow", target_id=wf["id"],
                                 after={"topic": topic, "missing": missing})
                raise ApiError("TRIGGER_CTX_MISSING",
                               f"事件 payload 缺少必需上下文字段：{missing}（拒绝启动，不半启动）",
                               http_status=422)
        run = {
            "run_id": f"run-{uuid.uuid4().hex[:12]}",
            "workflow_id": wf["id"], "workflow_name": wf.get("name"),
            "workflow_version": wf.get("version"),
            "trigger": {"source": trigger_source, "topic": topic, "payload": payload or {}},
            "status": "running", "pending_node": None,
            "node_states": self._init_node_states(wf),
            "started_at": _now(), "finished_at": None,
            "started_by": (actor or {}).get("username"),
        }
        run["id"] = run["run_id"]  # JsonStore 以 id 为主键；get(run_id) 依赖此约定
        self._run_store(project_id).insert(run)
        audit_svc.record(action="workflow_run_started", actor=run["started_by"] or "-",
                         project_id=project_id, target_type="workflow_run", target_id=run["run_id"],
                         after={"workflow_id": wf["id"], "version": wf.get("version"),
                                "source": trigger_source, "topic": topic})
        self._run_loop(project_id, run, wf, actor, trigger_source)
        return self._run_store(project_id).get(run["run_id"]) or run

    def _persist_run(self, project_id: str, run: dict) -> None:
        run["updated_at"] = _now()
        self._run_store(project_id).update(run["run_id"], run)

    def _run_loop(self, project_id: str, run: dict, wf: dict, actor: dict | None,
                  trigger_source: str) -> None:
        """执行主循环：逐节点推进，直至终态（完成/失败/中止）或暂停（awaiting_confirmation）。"""
        ctx: dict[str, Any] = {"trigger": run["trigger"]["payload"] or {}, "nodes": {}}
        for nid, st in run["node_states"].items():
            if st.get("status") == "success" and st.get("output") is not None:
                ctx["nodes"][nid] = st["output"]
            elif st.get("status") == "success" and "output" not in st:
                ctx["nodes"][nid] = {}
        while True:
            if run["status"] in ("awaiting_confirmation",) or run["status"] in RUN_TERMINAL:
                return
            ready = self._ready_nodes(wf, run["node_states"])
            if not ready:
                # 无就绪节点：全部终态 → 成功；否则卡死（结构性问题）
                unfinished = [nid for nid, st in run["node_states"].items()
                              if st.get("status") not in NODE_TERMINAL]
                if not unfinished:
                    self._finish_run(project_id, run, "succeeded")
                else:
                    self._mark_downstream_unreached(wf, run["node_states"], unfinished)
                    self._persist_run(project_id, run)
                    self._finish_run(project_id, run, "failed",
                                     reason="编排卡死：存在前驱永不满足的节点 " + ",".join(unfinished))
                return
            node = ready[0]
            nid = node["id"]
            st = run["node_states"][nid]
            st.update({"status": "running", "started_at": _now(),
                       "attempts": st.get("attempts", 0) + 1})
            self._persist_run(project_id, run)
            try:
                outcome = self._exec_node(project_id, wf, node, st, ctx, run, actor, trigger_source)
            except ConditionError as ce:
                # EP-14-02：分支条件缺失/类型不符 → 节点报错＋原因（运行失败，不静默兜底）
                st.update({"status": "failed", "error": str(ce), "finished_at": _now()})
                self._mark_downstream_unreached(wf, run["node_states"], [nid])
                self._persist_run(project_id, run)
                self._node_failed_audit(project_id, run, node, str(ce))
                self._finish_run(project_id, run, "failed", reason=f"节点 {nid} 条件求值错误：{ce}")
                return
            except NodeFailure as nf:
                # 失败策略处置：skip → 跳过继续；retry 耗尽 / abort → 运行失败/中止（下游不执行）
                if self._apply_node_failure(project_id, run, wf, node, st, str(nf)):
                    continue  # skip：继续驱动下游
                return
            # 节点完成
            if outcome == "paused":
                run["status"] = "awaiting_confirmation"
                run["pending_node"] = nid
                self._persist_run(project_id, run)
                return
            if node["kind"] == "end":
                self._persist_run(project_id, run)
                self._finish_run(project_id, run, "succeeded")
                return
        # 不可达（防御）
        self._finish_run(project_id, run, "failed", reason="执行循环异常退出")

    # ---------- 单节点执行（确定性，引擎自身不调 LLM，TP-14-10） ---------- #
    def _exec_node(self, project_id: str, wf: dict, node: dict, st: dict, ctx: dict,
                   run: dict, actor: dict | None, trigger_source: str) -> str:
        """执行单个节点；成功返回 "ok"，暂停在确认点返回 "paused"。

        条件求值错误抛 ConditionError（EP-14-02）；节点失败抛 NodeFailure（按 failure_policy 处置）。
        """
        kind = node["kind"]
        nid = node["id"]
        if kind == "start":
            st.update({"status": "success", "output": dict(ctx.get("trigger") or {}),
                       "finished_at": _now()})
            ctx["nodes"][nid] = dict(ctx.get("trigger") or {})
            self._persist_run(project_id, run)
            return "ok"
        if kind == "end":
            st.update({"status": "success", "output": {}, "finished_at": _now()})
            ctx["nodes"][nid] = {}
            return "ok"
        if kind == "branch":
            ports: dict[str, dict] = node.get("branches") or {}
            selected: str | None = None
            # 按声明顺序求值非默认端口；字段缺失/类型不符 → ConditionError（不静默落默认）
            for pid, spec in ports.items():
                if spec.get("is_default") or not spec.get("condition"):
                    continue
                if eval_condition(spec["condition"], ctx):
                    selected = pid
                    break
            if selected is None:
                if any(s.get("is_default") for s in ports.values()):
                    selected = next(p for p, s in ports.items() if s.get("is_default"))
                else:
                    raise ConditionError(
                        f"节点 {nid}：无匹配分支且未声明默认分支（字段缺失/类型不符/无匹配均不静默兜底，EP-14-02）")
            st.update({"status": "success", "output": {"selected_port": selected},
                       "finished_at": _now()})
            ctx["nodes"][nid] = {"selected_port": selected}
            # 未选中端口的下游不可达 → not_reached（不执行）
            skipped_from = [e["to"] for e in wf.get("edges") or []
                            if e.get("from") == nid and (e.get("from_port") or None) != selected]
            if skipped_from:
                self._mark_downstream_unreached(wf, run["node_states"], skipped_from)
            self._persist_run(project_id, run)
            return "ok"
        if kind == "confirm":
            # 人工确认控制节点（状态持久化于 run record；重启后由 confirm_node 处理，TCEE-04）
            st.update({"status": "awaiting_confirmation", "output": None,
                       "awaiting_since": _now(),
                       "confirm_title": node.get("confirm_title") or "人工确认",
                       "pending_action": None})
            self._persist_run(project_id, run)
            return "paused"
        if kind == "capability_gap":
            raise NodeFailure(f"节点 {nid} 为能力缺口（{node.get('gap_reason') or '未 grounded 步骤'}），不可执行")
        # ---- action 节点：解析三来源参数 → Action 层执行（V2-13 风险分流） ----
        from action_engine import engine as action_engine
        act = self._actions_by_id(project_id).get(node.get("action_id") or "")
        if not act:
            raise NodeFailure(f"节点 {nid} 引用的 Action「{node.get('action_id') or '空'}」不存在")
        resolved: dict[str, Any] = {}
        unresolved: list[str] = []
        for pname, pspec in (node.get("params") or {}).items():
            spec = pspec if isinstance(pspec, dict) else {"value_type": "literal", "value": pspec}
            vt = spec.get("value_type", "literal")
            if vt == "literal":
                resolved[pname] = spec.get("value")
            elif vt in ("field_ref", "trigger_ctx"):
                found, val = resolve_field(ctx, spec.get("value"))
                if found:
                    resolved[pname] = val
                else:
                    unresolved.append(f"{pname}（{spec.get('value')}）")
            else:
                unresolved.append(f"{pname}（非法取值来源 {vt!r}，仅允许 literal/field_ref/trigger_ctx）")
        if unresolved:
            raise NodeFailure(f"节点 {nid} 参数解析失败（字段引用缺失，不静默落默认值）：{unresolved}")
        retry_max = max(1, int(node.get("retry_max") or 1))
        last_err = ""
        for attempt in range(retry_max):
            res = action_engine.execute(act, {"input": resolved, "dry_run": False},
                                        project_id=project_id, actor=actor,
                                        trigger_source=trigger_source)
            outcome = res.get("outcome")
            st["last_action_result"] = res
            if outcome in ("success", "simulated", "idempotency_hit"):
                node_ctx = _node_output_ctx(res.get("external_response") or {})
                st.update({"status": "success", "output": node_ctx,
                           "finished_at": _now(),
                           "action_outcome": outcome, "log_id": res.get("log_id")})
                ctx["nodes"][nid] = node_ctx
                self._persist_run(project_id, run)
                return "ok"
            if outcome in ("awaiting_confirmation", "escalated_to_human"):
                # 中/高危：挂起等待人工确认（TTL 由 Action 层确认时校验；状态持久化支持重启恢复）
                st.update({"status": "awaiting_confirmation", "output": None,
                           "awaiting_since": _now(),
                           "pending_action": {
                               "action": act, "input": resolved,
                               "idempotency_key": res.get("idempotency_key"),
                               "planned_payload": res.get("planned_payload"),
                               "confirmation_token": res.get("confirmation_token"),
                               "risk_level": res.get("risk_level"),
                               "escalated": bool(res.get("escalated")),
                               "evaluation": res.get("evaluation"),
                           }})
                self._persist_run(project_id, run)
                return "paused"
            last_err = (f"门禁未通过：{res.get('gate_failures')}" if outcome == "gate_failed"
                        else f"回写失败：{(res.get('external_response') or {}).get('error') or outcome}"
                        if outcome == "failed" else str(outcome))
            if attempt < retry_max - 1:
                self._sleep_fn(self._backoff_delay(attempt))
        raise NodeFailure(f"节点 {nid} 重试 {retry_max} 次后仍失败：{last_err}")

    # ---------- 失败策略处置 / 运行收尾 / 审计 ---------- #
    def _apply_node_failure(self, project_id: str, run: dict, wf: dict, node: dict,
                            st: dict, error: str) -> bool:
        """按 failure_policy 处置节点失败。返回 True=skip（继续运行），False=运行已终止。"""
        policy = node.get("failure_policy", "retry") if node["kind"] == "action" else "abort"
        st.update({"error": error, "finished_at": _now()})
        if policy == "skip":
            st.update({"status": "skipped", "skip_policy": "skip"})
            self._persist_run(project_id, run)
            self._node_failed_audit(project_id, run, node, error, note="failure_policy=skip，已跳过继续下游")
            return True
        # retry 耗尽 / abort → 节点失败，运行失败（中止），下游不执行
        st.update({"status": "failed"})
        self._mark_downstream_unreached(wf, run["node_states"], [node["id"]])
        self._persist_run(project_id, run)
        self._node_failed_audit(project_id, run, node, error,
                                note=f"failure_policy={policy}，运行中止，下游不执行")
        self._finish_run(project_id, run, "aborted" if policy == "abort" else "failed",
                         reason=f"节点 {node['id']} 失败：{error}")
        return False

    def _finish_run(self, project_id: str, run: dict, status: str, reason: str | None = None) -> None:
        run["status"] = status
        run["finished_at"] = _now()
        if reason:
            run["finish_reason"] = reason
        run["pending_node"] = None
        self._persist_run(project_id, run)
        summary = {}
        for nid, st in (run.get("node_states") or {}).items():
            s = st.get("status")
            summary[s] = summary.get(s, 0) + 1
        audit_svc.record(action="workflow_run_finished", actor=run.get("started_by") or "-",
                         project_id=project_id, target_type="workflow_run", target_id=run["run_id"],
                         after={"status": status, "reason": reason,
                                "nodes": self._truncate_json(summary)})
        logger.info("orch run finished run_id=%s status=%s reason=%s", run["run_id"], status, reason)

    def _node_failed_audit(self, project_id: str, run: dict, node: dict, error: str,
                           note: str | None = None) -> None:
        audit_svc.record(action="workflow_node_failed", actor=run.get("started_by") or "-",
                         project_id=project_id, target_type="workflow_run", target_id=run["run_id"],
                         after={"node_id": node.get("id"), "kind": node.get("kind"),
                                "error": self._truncate_json(error), "note": note})

    def _truncate_json(self, obj: Any, limit: int = 4000) -> str:
        """审计 detail 防膨胀：序列化后截断。"""
        try:
            s = json.dumps(obj, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            s = str(obj)
        return s if len(s) <= limit else s[:limit] + "…(truncated)"

    def _sleep_fn(self, seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """指数退避：1s、2s、4s…，封顶 8s。"""
        return float(min(2 ** max(0, attempt), 8))

    @staticmethod
    def _ttl_expired(awaiting_since: str | None, ttl_seconds: int) -> bool:
        if not awaiting_since or ttl_seconds <= 0:
            return False
        try:
            dt = datetime.fromisoformat(awaiting_since)
        except ValueError:
            return False
        return (datetime.now().astimezone() - dt).total_seconds() > ttl_seconds

    def _continue_run(self, project_id: str, run: dict, wf: dict, actor: dict | None,
                      trigger_source: str) -> None:
        """确认/恢复后重入执行主循环（ctx 由持久化节点状态重建，保证确定性）。"""
        if run.get("status") in RUN_TERMINAL:
            return
        run["status"] = "running"
        run["pending_node"] = None
        self._persist_run(project_id, run)
        self._run_loop(project_id, run, wf, actor, trigger_source)

    def _pending_node_of(self, project_id: str, run: dict, node_id: str | None) -> tuple[dict, dict]:
        """取当前待确认节点（(node, state)）；不匹配 → 409。"""
        if run.get("status") != "awaiting_confirmation":
            raise ApiError("RUN_NOT_PAUSED", "运行不处于待确认状态", http_status=409)
        pid = node_id or run.get("pending_node")
        if not pid or run.get("pending_node") != pid:
            raise ApiError("NOT_PENDING_NODE",
                           f"节点「{node_id}」不是该运行当前待确认节点（pending={run.get('pending_node')}）",
                           http_status=409)
        wf = self._wf_of_run(project_id, run)
        node = next((n for n in wf.get("nodes") or [] if n.get("id") == pid), None)
        st = (run.get("node_states") or {}).get(pid)
        if node is None or not st:
            raise ApiError("NOT_PENDING_NODE", "待确认节点不存在", http_status=409)
        return node, st

    # ---------- 确认 / 拒绝（人工确认点） ---------- #
    def confirm_node(self, project_id: str, run_id: str, node_id: str | None,
                     actor: dict | None, reason: str = "") -> dict:
        """确认运行暂停的节点（confirm 控制节点 / 中高危 Action 确认点）并继续执行。

        令牌失效恢复（TCEE-04）：服务重启丢失 Action 内存令牌时确定性重放——
        以相同 input＋幂等键重执行，Action 层幂等去重（已生效→idempotency_hit，
        未回写→签发新令牌再确认），不产生重复外部调用。
        """
        from action_engine import engine as action_engine
        from config import get_settings
        run = self.get_run(project_id, run_id)
        node, st = self._pending_node_of(project_id, run, node_id)
        pid = node["id"]
        wf = self._wf_of_run(project_id, run)
        username = (actor or {}).get("username") or "-"
        if self._ttl_expired(st.get("awaiting_since"), get_settings().orch_confirm_ttl_seconds):
            # 确认超时 → 运行取消，留痕（操作者＋原因，EP-13-03 等价）
            st.update({"status": "failed", "error": "确认超时取消（confirmation_timeout）",
                       "finished_at": _now()})
            self._mark_downstream_unreached(wf, run["node_states"], [pid])
            self._finish_run(project_id, run, "cancelled",
                             reason=f"确认超时 confirmation_timeout（操作者 {username} 超时后尝试确认）")
            raise ApiError("CONFIRMATION_TIMEOUT", "确认超时，运行已取消", http_status=409)
        pa = st.get("pending_action")
        if pa:
            # Action 节点确认点：调 Action 层执行真实回写
            res: dict | None = None
            token = pa.get("confirmation_token") or ""
            if token:
                try:
                    res = action_engine.confirm(token, {"action": pa["action"], "project_id": project_id},
                                                actor=actor, trigger_source="api")
                except ApiError:
                    res = None  # 令牌失效（重启丢失）→ 走确定性恢复
            if res is None:
                res = action_engine.execute(pa["action"], {"input": pa.get("input") or {}},
                                            project_id=project_id, actor=actor, trigger_source="api")
                if res.get("confirmation_token"):
                    res = action_engine.confirm(res["confirmation_token"],
                                                {"action": pa["action"], "project_id": project_id},
                                                actor=actor, trigger_source="api")
            st.pop("pending_action", None)
            audit_svc.record(action="workflow_run_confirmed", actor=username,
                             project_id=project_id, target_type="workflow_run", target_id=run_id,
                             after={"node_id": pid, "confirmed": True, "reason": reason,
                                    "outcome": res.get("outcome"), "log_id": res.get("log_id")})
            if res.get("outcome") in ("success", "simulated", "idempotency_hit"):
                st.update({"status": "success", "output": _node_output_ctx(res.get("external_response") or {}),
                           "finished_at": _now(), "action_outcome": res.get("outcome"),
                           "log_id": res.get("log_id"), "confirmed_by": username})
                self._persist_run(project_id, run)
                self._continue_run(project_id, run, wf, actor, "api")
                return self.get_run(project_id, run_id)
            err = ("确认超时取消（confirmation_timeout）" if res.get("outcome") == "cancelled"
                   else f"确认后回写失败：{(res.get('external_response') or {}).get('error') or res.get('outcome')}")
            if self._apply_node_failure(project_id, run, wf, node, st, err):
                self._continue_run(project_id, run, wf, actor, "api")
            return self.get_run(project_id, run_id)
        # confirm 控制节点：记录签核标识（身份+时间戳），节点成功，继续执行
        st.update({"status": "success",
                   "output": {"confirmed_by": username, "confirmed_at": _now(), "reason": reason},
                   "finished_at": _now()})
        self._persist_run(project_id, run)
        audit_svc.record(action="workflow_run_confirmed", actor=username,
                         project_id=project_id, target_type="workflow_run", target_id=run_id,
                         after={"node_id": pid, "confirmed": True, "reason": reason,
                                "kind": "confirm_node"})
        self._continue_run(project_id, run, wf, actor, "api")
        return self.get_run(project_id, run_id)

    def reject_run(self, project_id: str, run_id: str, node_id: str | None, reason: str,
                   actor: dict | None) -> dict:
        """拒绝待确认节点 → 运行取消（不产生外部调用；Action 层 reject 留痕）。"""
        from action_engine import engine as action_engine
        run = self.get_run(project_id, run_id)
        node, st = self._pending_node_of(project_id, run, node_id)
        pid = node["id"]
        wf = self._wf_of_run(project_id, run)
        username = (actor or {}).get("username") or "-"
        pa = st.get("pending_action")
        if pa and pa.get("confirmation_token"):
            try:
                action_engine.reject(pa["confirmation_token"], reason or "编排运行拒绝确认",
                                     actor=actor, project_id=project_id,
                                     expected_action_id=(pa.get("action") or {}).get("action_id"))
            except ApiError:
                pass  # 令牌已失效（重启丢失）——本就未产生外部调用，直接走取消
        st.update({"status": "failed", "error": f"确认被拒绝：{reason or '未填写原因'}",
                   "finished_at": _now()})
        self._mark_downstream_unreached(wf, run["node_states"], [pid])
        self._finish_run(project_id, run, "cancelled",
                         reason=f"确认被拒绝（操作者 {username}）：{reason or '未填写原因'}")
        audit_svc.record(action="workflow_run_rejected", actor=username,
                         project_id=project_id, target_type="workflow_run", target_id=run_id,
                         after={"node_id": pid, "reason": reason or "未填写原因"})
        return self.get_run(project_id, run_id)

    # ---------- 中断恢复 / 启动扫描 ---------- #
    def resume_run(self, project_id: str, run_id: str, actor: dict | None) -> dict:
        """恢复中断运行（TCEE-04）：中断时执行中的节点重置为未执行、确定性重入
        （重放节点由 Action 层幂等键去重）；中断时停在确认点的，保留待确认状态。"""
        run = self.get_run(project_id, run_id)
        if run.get("status") != "interrupted":
            raise ApiError("RUN_NOT_INTERRUPTED", "仅中断（interrupted）运行可恢复", http_status=409)
        wf = self._wf_of_run(project_id, run)
        errors = validate_workflow(wf, self._actions_by_id(project_id), for_publish=True)
        if errors:
            raise ApiError("VALIDATION_ERROR", "恢复前校验未通过（工作流或 Action 依赖已变更）："
                           + "；".join(errors[:5]), http_status=422)
        for st in (run.get("node_states") or {}).values():
            if st.get("status") == "running":
                st["status"] = "not_executed"
                st.pop("error", None)
                st.pop("started_at", None)
        pending = run.get("pending_node")
        if pending and (run.get("node_states") or {}).get(pending, {}).get("status") == "awaiting_confirmation":
            # 停在确认点：保留待确认（确认入口含令牌失效的确定性恢复）
            run["status"] = "awaiting_confirmation"
            self._persist_run(project_id, run)
            audit_svc.record(action="workflow_run_resumed", actor=(actor or {}).get("username") or "-",
                             project_id=project_id, target_type="workflow_run", target_id=run_id,
                             after={"workflow_id": run.get("workflow_id"), "resumed_to": "awaiting_confirmation"})
            return run
        self._persist_run(project_id, run)
        audit_svc.record(action="workflow_run_resumed", actor=(actor or {}).get("username") or "-",
                         project_id=project_id, target_type="workflow_run", target_id=run_id,
                         after={"workflow_id": run.get("workflow_id"), "resumed_to": "running"})
        self._continue_run(project_id, run, wf, actor, (run.get("trigger") or {}).get("source") or "manual")
        return self.get_run(project_id, run_id)

    def recover_interrupted(self) -> list[dict]:
        """服务启动时扫描所有项目：running / awaiting_confirmation 运行 → interrupted 并留痕。

        进程内同步执行模型下，活进程外的 running 只能来自崩溃/重启；awaiting_confirmation
        一并标记为 interrupted 以显式提示需要 resume（确认入口可自动续接）。
        """
        recovered: list[dict] = []
        for p in self._all_project_ids():
            for run in self._run_store(p).all():
                was = run.get("status")
                if was not in ("running", "awaiting_confirmation"):
                    continue
                run["status"] = "interrupted"
                run["interrupted_at"] = _now()
                run["interrupt_reason"] = "服务重启：在途运行中断（可通过 resume 恢复）"
                self._persist_run(p, run)
                audit_svc.record(action="workflow_run_interrupted", actor="system",
                                 project_id=p, target_type="workflow_run", target_id=run.get("run_id"),
                                 after={"was_status": was, "pending_node": run.get("pending_node")})
                recovered.append(run)
        if recovered:
            logger.info("orch recover_interrupted: %d runs marked interrupted", len(recovered))
        return recovered

    @staticmethod
    def _all_project_ids() -> list[str]:
        try:
            from core.project_store import ProjectStore
            return [p.get("id") for p in ProjectStore().list() if p.get("id")]
        except Exception:  # noqa: BLE001 —— 项目枚举失败不阻断调度
            return []

    # ---------- 触发：事件 / cron ---------- #
    def dispatch_event(self, project_id: str, topic: str, payload: dict, actor: dict | None,
                       source: str = "event") -> dict:
        """事件分发：匹配 trigger.type=event 且 event_topic==topic 的已发布工作流并启动。

        EP-14-03：逐工作流做 required_ctx 校验，缺项的工作流被拒绝（不半启动），
        其余匹配工作流正常触发；返回逐工作流结果。
        """
        results: list[dict] = []
        matched = 0
        for wf in self._wf_store(project_id).all():
            trig = wf.get("trigger") or {}
            if wf.get("status") != "published" or trig.get("type") != "event" \
                    or (trig.get("event_topic") or "") != topic:
                continue
            matched += 1
            try:
                run = self.start_run(project_id, wf, payload, actor, trigger_source="event", topic=topic)
                results.append({"workflow_id": wf["id"], "started": True,
                                "run_id": run.get("run_id"), "status": run.get("status")})
            except ApiError as e:
                results.append({"workflow_id": wf["id"], "started": False,
                                "error_code": e.code, "error": e.message})
        return {"topic": topic, "matched_workflows": matched, "results": results}

    def tick_cron(self, now_dt: datetime | None = None) -> list[dict]:
        """cron 调度单 tick：跨项目扫描 cron 触发的已发布工作流（5 段表达式，分钟级去重）。"""
        fired: list[dict] = []
        now_dt = now_dt or datetime.now().astimezone()
        minute_key = now_dt.strftime("%Y-%m-%d %H:%M")
        for p in self._all_project_ids():
            for wf in self._wf_store(p).all():
                trig = wf.get("trigger") or {}
                if wf.get("status") != "published" or trig.get("type") != "cron":
                    continue
                expr = trig.get("cron_expr") or ""
                if not expr or not cron_matches(expr, now_dt):
                    continue
                if wf.get("last_cron_fire") == minute_key:
                    continue  # 同一分钟已触发（调度 tick 周期可能短于 1 分钟）
                self._wf_store(p).update(wf["id"], {"last_cron_fire": minute_key, "updated_at": _now()})
                try:
                    run = self.start_run(p, wf, dict(trig.get("static_payload") or {}),
                                         actor={"username": "system:cron"}, trigger_source="cron")
                    fired.append({"project_id": p, "workflow_id": wf["id"],
                                  "run_id": run.get("run_id"), "status": run.get("status")})
                except ApiError as e:
                    fired.append({"project_id": p, "workflow_id": wf["id"], "started": False,
                                  "error_code": e.code, "error": e.message})
        return fired


# --------------------------------------------------------------------------- #
# 后台 cron 调度器（daemon 线程，进程内单例）
# --------------------------------------------------------------------------- #
_scheduler_lock = threading.Lock()
_scheduler_started = False


def start_scheduler() -> bool:
    """启动后台 cron 调度器；interval<=0（ONTOLOGY_ORCH_SCHEDULER_INTERVAL_SECONDS）禁用。"""
    global _scheduler_started
    from config import get_settings
    interval = int(get_settings().orch_scheduler_interval_seconds)
    if interval <= 0:
        return False
    with _scheduler_lock:
        if _scheduler_started:
            return False
        _scheduler_started = True

        def _loop() -> None:
            while True:
                try:
                    orchestrator.tick_cron()
                except Exception:  # noqa: BLE001
                    logger.exception("orch cron scheduler tick failed")
                time.sleep(interval)

        threading.Thread(target=_loop, name="orch-cron-scheduler", daemon=True).start()
        return True


#: 进程内单例（API / MCP / 调度器统一入口）
orchestrator = Orchestrator()


__all__ = [
    "Orchestrator", "orchestrator", "start_scheduler",
    "ConditionError", "NodeFailure",
    "check_no_code_redline", "validate_workflow", "validate_cron_expr", "cron_matches",
    "eval_condition", "resolve_field",
]
