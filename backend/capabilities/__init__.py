"""能力层（V2-04 首片）：能力注册表 + 版本化 + AST 安全求值执行。

模型（对齐 docs/能力层元模型设计.md，本切片实现 derive × expression 单元格）：
- 能力记录：name（项目内唯一，ref 定位）/ display_name / description / kind /
  implementation / ontology_id / object_type / expression / output_attribute /
  status(draft|active|deprecated) / version / ref_locks / history；
- 版本化：create→draft 1.0.0；publish→active；active 内容变更→自动 minor+1 并发新版
  （history 保留旧版快照）；引用在 publish 时锁定被引用方版本（不自动跟随，旧版可溯源）；
- 引用：ref('name') 或 ref('name', 'x.y.z')；publish 解析并锁存表达式快照；
  被引用方 draft → 422；引用版本不存在 → 422；循环引用 → 422；
- 删除：存在依赖方 → 422 + 依赖清单；
- 执行：单实例 / 批量；四要素溯源（能力版本+输入实例+输入属性+原始值）；
  批内单实例错误不中断（错误码标记）；null 结果显式计数（null≠0）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from core.instance_store import get_instance_store
from core.storage import JsonStore
from logging_setup import get_logger

import content_access
from content_access import is_federated

from .expr_eval import EvalContext, TraceCollector, evaluate
from .expr_parser import (
    E_AMBIGUOUS, ExprError, extract_attr_refs, extract_refs, parse, validate,
)

logger = get_logger("capabilities")

KINDS = ("derive", "assert", "act")
IMPLEMENTATIONS = ("expression", "decision_table", "script", "external")
STATUSES = ("draft", "active", "deprecated")
CHANNELS = ("ui", "api", "mcp")

# V2-04 源连接型渠道（M4-A④ / TP-04-06）：派生值落派生图，不落权威实例存储、不写回源系统
DERIVED_NOTE = "派生数据，非权威（存派生图；不写权威实例存储，不写回源系统）"


class CapabilityApiError(Exception):
    """业务错误 → HTTP 4xx，带稳定错误码。"""

    def __init__(self, code: str, message: str, http_status: int = 422, details: Any = None):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bump_minor(version: str) -> str:
    parts = version.split(".")
    while len(parts) < 3:
        parts.append("0")
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    return f"{major}.{minor + 1}.{patch}"


def _expr_hash(expression: str) -> str:
    return hashlib.sha256(expression.encode("utf-8")).hexdigest()[:16]


def _source_fingerprint(lookups: list[dict[str, Any]]) -> str:
    """HF-04：本次计算依赖的全部 lookup 命中记录与取值的稳定指纹。

    基线实例值变化会改变指纹，从而把「未重算」与「重算后恰好相同」区分开。
    """
    parts = [[str(l.get("matched_record_id")), l.get("value")] for l in (lookups or [])]
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _safe_parse(expression: str):
    try:
        return parse(expression or "")
    except ExprError:
        from .expr_parser import Program
        return Program()


class CapabilityService:
    """项目级能力服务（注册/版本化/校验/发布/执行/溯源）。"""

    # HF-11：execute_batch 内置分块调度，单块实例数（控制单次回写规模与超时）。
    _BATCH_CHUNK = 250
    # HF-11：批量幂等键去重窗口（秒）。同一键在窗口内视为重发，返回已处理标记，
    # 避免客户端超时重发导致后端多个批量并发互锁写回。
    _IDEMPOTENCY_TTL_SEC = 3600

    def __init__(self, project_id: str):
        self.project_id = project_id
        self._store = JsonStore("capabilities", project_id)
        self._instances = get_instance_store(project_id)
        # M4-A④ TP-04-06：派生图（派生值非权威存储；不写实例存储、不写回源系统）
        self._derived = JsonStore("derived_values", project_id)
        # HF-11：批量幂等键 → 最近处理时间（进程内去重，TTL 清理）。
        self._batch_idempotency: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_capabilities(
        self,
        status: Optional[str] = None,
        object_type: Optional[str] = None,
        q: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        out = []
        for rec in self._store.all():
            if status and rec.get("status") != status:
                continue
            if object_type and rec.get("object_type") != object_type:
                continue
            if q:
                hay = f"{rec.get('name', '')} {rec.get('display_name', '')} {rec.get('description', '')}".lower()
                if q.lower() not in hay:
                    continue
            out.append(self._view(rec))
        out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return out

    def get_capability(self, cap_id: str) -> Optional[dict[str, Any]]:
        rec = self._store.get(cap_id)
        return self._view(rec) if rec else None

    def get_by_name(self, name: str) -> Optional[dict[str, Any]]:
        for rec in self._store.all():
            if rec.get("name") == name:
                return rec
        return None

    def list_dependents(self, cap_id: str) -> list[dict[str, Any]]:
        """列出引用该能力的所有其他能力（当前版本引用，含版本锁）。"""
        target = self._store.get(cap_id)
        if not target:
            return []
        tname = target.get("name")
        out = []
        for rec in self._store.all():
            if rec.get("id") == cap_id:
                continue
            names = {r["name"] for r in extract_refs(_safe_parse(rec.get("expression", "")))}
            locked = set((rec.get("ref_locks") or {}).keys())
            if tname in names or tname in locked:
                out.append({
                    "id": rec.get("id"),
                    "name": rec.get("name"),
                    "display_name": rec.get("display_name"),
                    "version": rec.get("version"),
                    "status": rec.get("status"),
                    "locked_version": (rec.get("ref_locks") or {}).get(tname, {}).get("version"),
                })
        return out

    def list_versions(self, cap_id: str) -> list[dict[str, Any]]:
        rec = self._store.get(cap_id)
        if not rec:
            return []
        return list(reversed(rec.get("history", [])))

    @staticmethod
    def _view(rec: dict[str, Any]) -> dict[str, Any]:
        """对外视图：不暴露内部字段。"""
        view = {k: v for k, v in rec.items() if k != "ref_locks"}
        view["ref_locks"] = {
            name: {
                "cap_id": info.get("cap_id"),
                "name": info.get("name"),
                "version": info.get("version"),
            }
            for name, info in (rec.get("ref_locks") or {}).items()
        }
        view["dependent_count"] = None  # 由调用方按需填充
        return view

    # ------------------------------------------------------------------
    # 校验（定义期）
    # ------------------------------------------------------------------

    def _check_expression(self, expression: str, self_name: Optional[str] = None) -> dict[str, Any]:
        """解析 + 静态校验 + 引用收集。解析/静态错误 → CapabilityApiError(422)。"""
        try:
            program = parse(expression)
        except ExprError as e:
            raise CapabilityApiError("SYNTAX_ERROR", f"公式语法错误: {e.message}", 422, e.to_dict()) from e
        issues = validate(program)
        if issues:
            raise CapabilityApiError(
                "VALIDATION_ERROR", "公式定义校验未通过", 422,
                {"issues": issues},
            )
        refs = extract_refs(program)
        if self_name:
            for r in refs:
                if r["name"] == self_name:
                    raise CapabilityApiError("CYCLE", f"能力不能引用自身: {self_name}", 422, r)
        return {"program": program, "refs": refs, "attr_refs": extract_attr_refs(program)}

    def _check_refs_exist(self, refs: list[dict[str, Any]]) -> None:
        """publish 时校验引用目标：存在性 / active 状态 / 指定版本存在 / 循环。"""
        for r in refs:
            target = self.get_by_name(r["name"])
            if target is None:
                raise CapabilityApiError("REF_NOT_FOUND", f"引用能力不存在: {r['name']}", 422, r)
            if r["version"] is not None:
                hit = next((h for h in target.get("history", []) if h.get("version") == r["version"]), None)
                if hit is None:
                    raise CapabilityApiError(
                        "REF_VERSION_NOT_FOUND",
                        f"能力 {r['name']} 不存在版本 {r['version']}（拒绝静默指向最新版本）",
                        422, r,
                    )
            else:
                if target.get("status") != "active":
                    raise CapabilityApiError(
                        "REF_NOT_ACTIVE",
                        f"引用能力 {r['name']} 当前为 {target.get('status')}，仅可引用已发布(active)能力",
                        422, r,
                    )
        self._check_cycle(refs)

    def _check_cycle(self, refs: list[dict[str, Any]]) -> None:
        """循环引用检测：沿 ref 图 DFS，回到自身 → CYCLE。"""
        start = {r["name"] for r in refs}
        if not start:
            return
        visited: set[str] = set()
        stack = list(start)
        while stack:
            name = stack.pop()
            if name in visited:
                continue
            visited.add(name)
            rec = self.get_by_name(name)
            if not rec:
                continue
            try:
                prog = parse(rec.get("expression", ""))
            except ExprError:
                continue
            for r in extract_refs(prog):
                if r["name"] in start:
                    raise CapabilityApiError(
                        "CYCLE", f"检测到循环引用: {name} → {r['name']}", 422,
                        {"chain": sorted(start) + [name, r["name"]]},
                    )
                stack.append(r["name"])

    def _resolve_locks(self, refs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """publish 时锁定引用版本：记录被引用方 cap_id/version/表达式快照。"""
        locks: dict[str, dict[str, Any]] = {}
        for r in refs:
            target = self.get_by_name(r["name"])
            if target is None:
                raise CapabilityApiError("REF_NOT_FOUND", f"引用能力不存在: {r['name']}", 422, r)
            if r["version"] is not None:
                snap = next((h for h in target.get("history", []) if h.get("version") == r["version"]), None)
                if snap is None:
                    raise CapabilityApiError(
                        "REF_VERSION_NOT_FOUND",
                        f"能力 {r['name']} 不存在版本 {r['version']}", 422, r,
                    )
                version, expression = snap["version"], snap["expression"]
            else:
                version, expression = target.get("version"), target.get("expression")
            locks[r["name"]] = {
                "cap_id": target.get("id"),
                "name": r["name"],
                "version": version,
                "expression": expression,
                "expression_hash": _expr_hash(expression),
                # 传递闭包：被引用方自身的锁随快照一起固化，多级 ref() 才能按同一版本求值
                "ref_locks": copy.deepcopy(target.get("ref_locks") or {}),
                "locked_at": _now(),
            }
        return locks

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_capability(self, data: dict[str, Any]) -> dict[str, Any]:
        name = (data.get("name") or "").strip()
        if not name or not _NAME_RE.match(name):
            raise CapabilityApiError("VALIDATION_ERROR", "能力 name 必填，格式 ^[a-z][a-z0-9_]*$", 422)
        if self.get_by_name(name):
            raise CapabilityApiError("DUPLICATE_NAME", f"能力名已存在: {name}", 409)
        kind = data.get("kind") or "derive"
        impl = data.get("implementation") or "expression"
        if kind not in KINDS:
            raise CapabilityApiError("VALIDATION_ERROR", f"kind 必须是 {KINDS}", 422)
        if impl != "expression":
            raise CapabilityApiError("NOT_IMPLEMENTED", "本切片仅实现 implementation=expression（derive×expression）", 422)
        expression = (data.get("expression") or "").strip()
        if not expression:
            raise CapabilityApiError("VALIDATION_ERROR", "expression 必填", 422)
        object_type = (data.get("object_type") or "").strip()
        if not object_type:
            raise CapabilityApiError("VALIDATION_ERROR", "object_type 必填（能力执行的目标对象类型）", 422)
        checked = self._check_expression(expression, self_name=name)

        now = _now()
        record = {
            "id": f"cap-{hashlib.sha1((name + now).encode()).hexdigest()[:16]}",
            "name": name,
            "display_name": (data.get("display_name") or name).strip(),
            "description": (data.get("description") or "").strip(),
            "kind": kind,
            "implementation": impl,
            "ontology_id": data.get("ontology_id"),
            "object_type": object_type,
            "expression": expression,
            "expression_hash": _expr_hash(expression),
            "output_attribute": (data.get("output_attribute") or "").strip() or None,
            "status": "draft",
            "version": "1.0.0",
            "ref_locks": {},
            "history": [{
                "version": "1.0.0",
                "expression": expression,
                "expression_hash": _expr_hash(expression),
                "status": "draft",
                "created_at": now,
                "published_at": None,
            }],
            "created_at": now,
            "updated_at": now,
            "published_at": None,
        }
        return self._store.insert(record)

    def update_capability(self, cap_id: str, data: dict[str, Any]) -> dict[str, Any]:
        rec = self._store.get(cap_id)
        if not rec:
            raise CapabilityApiError("NOT_FOUND", "能力不存在", 404)
        now = _now()
        expression = (data.get("expression") or "").strip() or rec.get("expression")
        object_type = (data.get("object_type") or "").strip() or rec.get("object_type")
        output_attribute = data.get("output_attribute")
        content_changed = (
            expression != rec.get("expression")
            or object_type != rec.get("object_type")
            or (output_attribute is not None and output_attribute != rec.get("output_attribute"))
        )
        checked = self._check_expression(expression, self_name=rec.get("name"))
        patch: dict[str, Any] = {"updated_at": now}
        for field in ("display_name", "description", "ontology_id"):
            if data.get(field) is not None:
                patch[field] = data[field]
        if data.get("name") and data["name"] != rec.get("name"):
            if self.get_by_name(data["name"]):
                raise CapabilityApiError("DUPLICATE_NAME", f"能力名已存在: {data['name']}", 409)
            patch["name"] = data["name"]
        if output_attribute is not None:
            patch["output_attribute"] = output_attribute or None
        patch["expression"] = expression
        patch["object_type"] = object_type
        patch["expression_hash"] = _expr_hash(expression)

        if content_changed and rec.get("status") == "active":
            # active 变更 → 自动发新版（minor+1），重新锁定引用版本
            new_version = _bump_minor(rec.get("version", "1.0.0"))
            self._check_refs_exist(checked["refs"])
            patch["version"] = new_version
            patch["ref_locks"] = self._resolve_locks(checked["refs"])
            patch["published_at"] = now
            history = list(rec.get("history", []))
            history.append({
                "version": new_version,
                "expression": expression,
                "expression_hash": _expr_hash(expression),
                "status": "active",
                "created_at": now,
                "published_at": now,
            })
            patch["history"] = history
        return self._store.update(cap_id, patch) or rec

    def delete_capability(self, cap_id: str) -> None:
        rec = self._store.get(cap_id)
        if not rec:
            raise CapabilityApiError("NOT_FOUND", "能力不存在", 404)
        dependents = self.list_dependents(cap_id)
        if dependents:
            raise CapabilityApiError(
                "HAS_DEPENDENTS",
                f"能力 {rec.get('name')} 被 {len(dependents)} 个能力引用，禁止删除",
                422,
                {"dependents": dependents},
            )
        self._store.delete(cap_id)

    def publish_capability(self, cap_id: str) -> dict[str, Any]:
        rec = self._store.get(cap_id)
        if not rec:
            raise CapabilityApiError("NOT_FOUND", "能力不存在", 404)
        checked = self._check_expression(rec.get("expression", ""), self_name=rec.get("name"))
        self._check_refs_exist(checked["refs"])
        now = _now()
        locks = self._resolve_locks(checked["refs"])
        patch: dict[str, Any] = {
            "status": "active",
            "ref_locks": locks,
            "published_at": now,
            "updated_at": now,
        }
        history = list(rec.get("history", []))
        if history and history[-1].get("status") != "active":
            history[-1]["status"] = "active"
            history[-1]["published_at"] = now
            patch["history"] = history
        updated = self._store.update(cap_id, patch) or rec
        logger.info("capability published: %s v%s refs=%s", updated.get("name"), updated.get("version"),
                    {k: v["version"] for k, v in locks.items()})
        return updated

    def validate_capability(self, cap_id: str, draft_expression: Optional[str] = None) -> dict[str, Any]:
        """定义校验（不落库）。可传入待校验的草稿表达式。"""
        rec = self._store.get(cap_id)
        if not rec:
            raise CapabilityApiError("NOT_FOUND", "能力不存在", 404)
        expression = draft_expression if draft_expression is not None else rec.get("expression", "")
        try:
            checked = self._check_expression(expression, self_name=rec.get("name"))
        except CapabilityApiError as e:
            return {
                "valid": False,
                "issues": e.details.get("issues") if isinstance(e.details, dict) else [e.details or e.message],
                "error_code": e.code,
                "refs": [],
                "attr_refs": [],
            }
        ref_issues = []
        for r in checked["refs"]:
            target = self.get_by_name(r["name"])
            if target is None:
                ref_issues.append({"ref": r["name"], "code": "REF_NOT_FOUND", "message": f"引用能力不存在: {r['name']}"})
            elif r["version"] is not None:
                if not any(h.get("version") == r["version"] for h in target.get("history", [])):
                    ref_issues.append({"ref": r["name"], "code": "REF_VERSION_NOT_FOUND", "message": f"版本 {r['version']} 不存在"})
            elif target.get("status") != "active":
                ref_issues.append({"ref": r["name"], "code": "REF_NOT_ACTIVE", "message": f"引用能力为 {target.get('status')}"})
        return {
            "valid": not ref_issues,
            "issues": ref_issues,
            "refs": checked["refs"],
            "attr_refs": sorted(checked["attr_refs"]),
        }

    # ------------------------------------------------------------------
    # 执行（单实例 / 批量 + 四要素溯源 + 写回）
    # ------------------------------------------------------------------

    def _build_ctx(self, rec: dict[str, Any], inst_record: dict[str, Any],
                   channel: str, trace: Optional[TraceCollector] = None,
                   lookup_cache: Optional[dict[str, list]] = None) -> EvalContext:
        instance = inst_record.get("instance", {}) or {}

        # HF-05：能力记录 ontology_id 可选；未指定则不过滤，历史实例缺该字段视为当前本体。
        cap_ontology_id = rec.get("ontology_id")

        def _in_scope(r: dict[str, Any]) -> bool:
            if not cap_ontology_id:
                return True
            return (r.get("ontology_id") or cap_ontology_id) == cap_ontology_id

        def do_lookup(otype: str, dims: dict[str, Any], attr: Optional[str],
                      tr: TraceCollector, mode: str = "strict",
                      order_by: Optional[str] = None, descending: bool = False,
                      dims_list: Optional[list[dict[str, Any]]] = None) -> Any:
            # 缓存键加入 ontology_id，避免多本体共存时跨本体串池（HF-05）。
            cache_key = (otype, cap_ontology_id or "")
            if lookup_cache is not None and cache_key in lookup_cache:
                pool = lookup_cache[cache_key]
            else:
                pool = [(r, r.get("instance", {}) or {})
                        for r in self._instances.find(object_type=otype, limit=None)
                        if _in_scope(r)]
                if lookup_cache is not None:
                    lookup_cache[cache_key] = pool

            def _match(d: dict[str, Any]):
                if any(v is None for v in d.values()):
                    raise ExprError(E_AMBIGUOUS,
                                    f"lookup 维度值含 null（应显式 if_null 守卫）: {d}", None)
                return [(irec, iinst) for irec, iinst in pool
                        if all(iinst.get(k) == v for k, v in d.items())]

            # V2-02（M4-A①）：lookup 命中源连接型实例且属性无本地值 → 治理身份单值回源
            #（批量联邦读＋成本提示随 M4-A④/TP-04-06）；命中缓存避免同一实例重复回源。
            fed_value_cache: dict[str, Any] = {}
            # GAP-16：命中前并行预取（线程池）——池内源连接型且缺目标属性的实例一次并行回源，
            # 总时延≈最慢单源；_fed_value 懒加载分支保留兜底（预取失败的实例仍可逐条回源）。
            import content_access as _ca
            _need = [r for r, iinst in pool
                     if attr and attr not in iinst
                     and _ca.is_federated(self.project_id, r)
                     and r.get("id") not in fed_value_cache]
            if _need:
                try:
                    for rid, (vals, _pmeta) in _ca.federate_values_many(
                            self.project_id, _need, user_id=None, attr_ids=[attr]).items():
                        fed_value_cache[rid] = vals.get(attr)
                except Exception as e:  # noqa: BLE001 —— 预取失败退回懒加载路径
                    logger.warning("lookup 并行预取联邦值失败（退回逐条回源）: %s", e)

            def _fed_value(irec: dict[str, Any], iinst: dict[str, Any], a: str) -> Any:
                if a in iinst:
                    return iinst.get(a)
                import content_access
                if not content_access.is_federated(self.project_id, irec):
                    return iinst.get(a)
                rid = irec.get("id")
                if rid in fed_value_cache:
                    return fed_value_cache[rid]
                values, _meta = content_access.federate_values(
                    self.project_id, irec, user_id=None, attr_ids=[a])
                v = values.get(a)
                fed_value_cache[rid] = v
                return v

            def _record(matched: list, d: dict[str, Any], rule: str,
                        total_matches: int | None = None) -> Any:
                values = []
                count = len(matched) if total_matches is None else total_matches
                for irec, iinst in matched:
                    value = _fed_value(irec, iinst, attr) if attr else iinst
                    code = iinst.get("attr_baseline_code") or iinst.get("attr_code") \
                        or iinst.get("attr_product_code") or irec.get("id")
                    tr.lookups.append({
                        "object_type": otype,
                        "dims": d,
                        "attr": attr,
                        "matched_record_id": irec.get("id"),
                        "matched_code": code,
                        "value": None if isinstance(value, dict) else value,
                        # HF-03 兼容追加，不改动既有 6 字段
                        "matched_count": count,
                        "selection_rule": rule,
                    })
                    values.append(value)
                return values if mode == "all" else values[0]

            if mode == "coalesce":
                # 按 dims 列表依次尝试；某组多命中仍报歧义（不静默取错值），全未命中显式报错。
                for idx, d in enumerate(dims_list or []):
                    matched = _match(d)
                    if len(matched) == 1:
                        return _record(matched, d, f"coalesce:{idx}")
                    if len(matched) > 1:
                        raise ExprError("LOOKUP_AMBIGUOUS",
                                        f"coalesce_lookup {otype} 第 {idx + 1} 组维度命中 {len(matched)} 条", None)
                raise ExprError("LOOKUP_NOT_FOUND",
                                f"coalesce_lookup {otype} 所有 {len(dims_list or [])} 组维度均 0 命中", None)

            matched = _match(dims)
            if mode == "all":
                # 红线：0 命中是合法的「未装配/无分组」状态，聚合时不得判为 lookup 失败。
                return _record(matched, dims, "all") if matched else []
            if not matched:
                raise ExprError("LOOKUP_NOT_FOUND",
                                f"lookup {otype} 维度 {dims} 0 命中（NOT_FOUND）", None)
            if len(matched) > 1:
                if mode != "first":
                    raise ExprError("LOOKUP_AMBIGUOUS",
                                    f"lookup {otype} 维度 {dims} 命中 {len(matched)} 条（AMBIGUOUS）", None)

                def _sort_key(pair):
                    irec, iinst = pair
                    v = iinst.get(order_by) if order_by else None
                    if v is None:
                        v = irec.get("id") or ""
                    # 数值优先按数值比较；字符串数字也按数字字符串比较，避免 "10" < "2" 的字典序误判。
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        return (0, float(v), "")
                    if isinstance(v, str):
                        try:
                            return (0, float(v), "")
                        except ValueError:
                            return (1, 0.0, v)
                    return (1, 0.0, str(v))

                total_matches = len(matched)
                matched.sort(key=_sort_key, reverse=bool(descending))
                rule = f"first:{order_by or 'record_id'}:{'desc' if descending else 'asc'}"
                return _record(matched[:1], dims, rule, total_matches=total_matches)
            return _record(matched, dims, "strict")

        def do_ref(name: str, version: Optional[str]) -> Any:
            lock = (rec.get("ref_locks") or {}).get(name)
            if not lock:
                raise ExprError("REF_NOT_RESOLVED",
                                f"能力 {name} 未在当前版本中锁定引用（请重新发布以锁定引用版本）", None)
            if version is not None and lock.get("version") != version:
                raise ExprError("REF_NOT_RESOLVED",
                                f"能力 {name} 请求版本 {version} 与锁定版本 {lock.get('version')} 不一致", None)
            try:
                prog = parse(lock.get("expression", ""))
            except ExprError as e:
                raise ExprError("REF_NOT_RESOLVED", f"锁定的 {name} 表达式不可解析: {e.message}", None) from e
            # 子表达式的 ref() 走被引用方当时锁定的版本，而不是引用方自己的锁
            child_rec = dict(rec)
            child_rec["ref_locks"] = copy.deepcopy(lock.get("ref_locks") or {})
            child = self._build_ctx(child_rec, inst_record, channel, trace, lookup_cache)
            return evaluate(prog, child)

        return EvalContext(
            project_id=self.project_id,
            object_type=rec.get("object_type"),
            record_id=inst_record.get("id"),
            instance=instance,
            lookup=do_lookup,
            ref=do_ref,
            trace=trace or TraceCollector(),
        )

    def _execute_one(self, rec: dict[str, Any], inst_record: dict[str, Any], channel: str,
                     lookup_cache: Optional[dict[str, list]] = None) -> dict[str, Any]:
        trace = TraceCollector()
        result: dict[str, Any] = {
            "capability_id": rec.get("id"),
            "capability_name": rec.get("name"),
            "capability_version": rec.get("version"),
            "instance_id": inst_record.get("id"),
            "object_type": rec.get("object_type"),
            "execution_channel": channel,
            "executed_at": _now(),
        }
        try:
            ctx = self._build_ctx(rec, inst_record, channel, trace, lookup_cache)
            prog = parse(rec.get("expression", ""))
            value = evaluate(prog, ctx)
            result.update(status="success", value=value)
        except ExprError as e:
            result.update(status="error", value=None,
                          error_code=e.code, error_message=e.message)
        result["trace"] = trace.to_dict()
        return result

    def execute(self, cap_id: str, instance_id: str, channel: str = "api",
                trigger_source: str = "manual", mcp_only: bool = False,
                dry_run: bool = False,
                inputs: Optional[dict[str, Any]] = None,
                federated: Optional[bool] = None,
                cost_confirmed: bool = False,
                user_id: Optional[str] = None) -> dict[str, Any]:
        rec = self._store.get(cap_id)
        if not rec:
            raise CapabilityApiError("NOT_FOUND", "能力不存在", 404)
        if mcp_only and rec.get("status") != "active":
            raise CapabilityApiError(
                "FORBIDDEN_DRAFT",
                f"MCP 通道仅可执行已发布(active)能力，当前为 {rec.get('status')}", 403)
        inst_record = self._instances.get(instance_id)
        if not inst_record:
            raise CapabilityApiError("INSTANCE_NOT_FOUND", f"实例不存在: {instance_id}", 404)
        if inst_record.get("object_type") != rec.get("object_type"):
            raise CapabilityApiError("TYPE_MISMATCH",
                                     f"实例对象类型 {inst_record.get('object_type')} 与能力目标 {rec.get('object_type')} 不符",
                                     422)
        # M4-A④ TP-04-06：源连接型渠道——平台服务端联邦读（默认按实例渠道自动判定）
        fed_channel = is_federated(self.project_id, inst_record)
        use_federated = fed_channel if federated is None else bool(federated)
        fed_meta: Optional[dict[str, Any]] = None
        if use_federated and not dry_run and inputs is None:
            # 显式 inputs 走 M1-E 外部覆盖模式（外部 Agent 自行联邦读），平台不再代读
            if not fed_channel:
                raise CapabilityApiError("CHANNEL_MISMATCH",
                                         "实例为上传型（本地值），不适用源连接联邦渠道", 422)
            if not cost_confirmed:
                prev = self.preview_federated_cost(cap_id, instance_ids=[instance_id])
                raise CapabilityApiError(
                    "COST_CONFIRMATION_REQUIRED",
                    "源连接型实例执行前需确认联邦读成本（先展示成本提示，再以 cost_confirmed=true 重试；可取消）",
                    409, details=prev)
            attrs = self._required_attrs(rec)
            inputs, fed_meta = self._federate_input(inst_record, attrs, user_id)
        # M1-E / TCE2E-01：联邦读结果只作为本次计算的临时输入覆盖（不落地为实例内容），
        # 写回仍基于原始 inst_record，只写 output_attribute 与 __capability__ 元数据。
        effective = inst_record
        if inputs is not None:
            merged_instance = dict(inst_record.get("instance") or {})
            merged_instance.update(inputs)
            effective = {**inst_record, "instance": merged_instance}
        res = self._execute_one(rec, effective, channel, {})
        if inputs is not None:
            res["input_overrides"] = {k: v for k, v in inputs.items()}
            res["data_gaps"] = sorted(k for k, v in inputs.items() if v is None)
            res["input_source"] = ("server_federated_read" if fed_meta is not None
                                   else "federated_read_override")
            res["inputs_persisted"] = False
        if fed_meta is not None:
            self._annotate_trace_federated(res, fed_meta)
            res["federated"] = {
                "source_connection": fed_meta.get("source_connection"),
                "connection_name": fed_meta.get("connection_name"),
                "resolved_as": fed_meta.get("resolved_as"),
                "fetched_at": fed_meta.get("fetched_at"),
                "as_of": fed_meta.get("as_of"),
                "mode": fed_meta.get("mode"),
            }
        if not dry_run:
            if use_federated and fed_meta is not None:
                # TP-04-06：派生值落派生图（失败不落派生值；不写权威实例存储、不写回源系统）
                res["derived"] = (self._save_derived(rec, inst_record, res, fed_meta, trigger_source)
                                  if res.get("status") == "success" else None)
            else:
                self._write_back(rec, inst_record, res, channel, trigger_source)
        else:
            res["dry_run"] = True
        return res

    def execute_batch(self, cap_id: str, limit: int = 1000, offset: int = 0,
                      instance_ids: Optional[list[str]] = None,
                      channel: str = "api", trigger_source: str = "batch",
                      dry_run: bool = False,
                      federated: Optional[bool] = None,
                      cost_confirmed: bool = False,
                      user_id: Optional[str] = None,
                      idempotency_key: Optional[str] = None) -> dict[str, Any]:
        rec = self._store.get(cap_id)
        if not rec:
            raise CapabilityApiError("NOT_FOUND", "能力不存在", 404)
        # HF-11：批量幂等键去重——同一键在窗口内视为重发（客户端超时后重试），
        # 直接返回「已登记/已处理」标记，避免多个批量并发互锁写回。
        idem_key = f"{self.project_id}:{cap_id}:{idempotency_key}" if idempotency_key else None
        if idem_key:
            now = time.monotonic()
            # 清理过期键（避免无限增长）
            expired = [k for k, ts in self._batch_idempotency.items()
                       if now - ts > self._IDEMPOTENCY_TTL_SEC]
            for k in expired:
                self._batch_idempotency.pop(k, None)
            if idem_key in self._batch_idempotency:
                return {
                    "capability_id": rec.get("id"),
                    "capability_name": rec.get("name"),
                    "capability_version": rec.get("version"),
                    "object_type": rec.get("object_type"),
                    "idempotent": True,
                    "idempotency_key": idempotency_key,
                    "message": "相同批量请求已处理（幂等键命中），本次跳过重复调度",
                }
            self._batch_idempotency[idem_key] = now
        started = time.monotonic()
        if instance_ids:
            records = []
            missing = []
            for iid in instance_ids:
                r = self._instances.get(iid)
                if r is None:
                    missing.append(iid)
                elif r.get("object_type") != rec.get("object_type"):
                    missing.append(iid)
                else:
                    records.append(r)
            if missing:
                raise CapabilityApiError("INSTANCE_NOT_FOUND", f"实例不存在或类型不符: {missing[:5]}", 404)
        else:
            # HF-11：不按 limit 截断——取全部匹配实例，内部按 _BATCH_CHUNK 分块调度，
            # 避免「默认 limit=1000 只跑前 1000 行」导致大批量永远执行不全。
            # offset 仍生效（分页起点）。
            records = self._instances.find(
                object_type=rec.get("object_type"),
                filters={"project_id": self.project_id},
                limit=None, offset=offset,
            )
            # HF-05：同项目多本体共存时只执行当前能力所属本体的实例；
            # 能力未指定 ontology_id 或历史实例缺该字段时保持原行为（兼容 M1-B golden）。
            cap_oid = rec.get("ontology_id")
            if cap_oid:
                records = [r for r in records
                           if (r.get("ontology_id") or cap_oid) == cap_oid]

        # M4-A④ TP-04-06 / EP-04-03：批量联邦读——执行前成本提示（可取消）；
        # 部分源系统失败 → 失败实例显式标记，不产生半口径结果（失败实例无值、无派生值）。
        fed_recs = [r for r in records if is_federated(self.project_id, r)]
        use_federated = bool(fed_recs) if federated is None else bool(federated)
        if use_federated and not dry_run and fed_recs and not cost_confirmed:
            prev = self.preview_federated_cost(cap_id, limit=limit, offset=offset,
                                               instance_ids=instance_ids)
            raise CapabilityApiError(
                "COST_CONFIRMATION_REQUIRED",
                "存在源连接型实例，批量执行前需确认联邦读成本；展示成本提示后以 cost_confirmed=true 重试（可取消）",
                409, details=prev)
        attrs = self._required_attrs(rec) if (use_federated and fed_recs) else []

        items: list[dict[str, Any]] = []
        lookup_cache: dict[str, list] = {}
        n_success = n_error = n_null = 0
        n_fed_success = n_fed_failed = 0
        fed_system_stats: dict[str, dict[str, Any]] = {}
        object_type = rec.get("object_type")
        # HF-11：分块调度——每块执行完批量写回一次（update_many），
        # 既避免单次请求超时，也把「逐实例全 jsonl 重写」降为「每块一次读一次写」。
        pending: dict[str, dict[str, dict[str, Any]]] = {}
        for chunk_start in range(0, len(records), self._BATCH_CHUNK):
            chunk = records[chunk_start:chunk_start + self._BATCH_CHUNK]
            # GAP-19：联邦回源块内并行预取——_federate_input 是逐实例源系统 HTTP 调用，
            # 原块内逐条串行时块时延≈各源时延之和；线程池并行后≈最慢单源。
            # 预取失败按 EP-04-03 语义标记（主循环查表消费；未预取到的走原逐条路径兜底）。
            fed_prefetch: dict[str, tuple] = {}
            if use_federated and not dry_run:
                fed_chunk = [r for r in chunk if is_federated(self.project_id, r)]
                if len(fed_chunk) > 1:
                    def _prefetch(r):
                        try:
                            return r.get("id"), (self._federate_input(r, attrs, user_id), None)
                        except CapabilityApiError as e:
                            # 与主循环解包结构 ((inputs, fed_meta), pre_err) 保持一致
                            return r.get("id"), ((None, None), e)
                    with ThreadPoolExecutor(max_workers=min(8, len(fed_chunk))) as ex:
                        for rid, pair in ex.map(_prefetch, fed_chunk):
                            fed_prefetch[rid] = pair
            for irec in chunk:
                effective = irec
                fed_meta: Optional[dict[str, Any]] = None
                if use_federated and is_federated(self.project_id, irec):
                    pre = fed_prefetch.get(irec.get("id"))
                    if pre is not None:
                        (inputs, fed_meta), pre_err = pre
                    else:
                        try:
                            inputs, fed_meta = self._federate_input(irec, attrs, user_id)
                            pre_err = None
                        except CapabilityApiError as e:
                            pre_err = e
                    if pre_err is not None:
                        e = pre_err
                        # EP-04-03：单实例回源失败 → 标记失败实例，不产生半口径结果
                        n_fed_failed += 1
                        cid, cname = self._fed_conn_info(irec)
                        st = fed_system_stats.setdefault(
                            cid, {"connection_id": cid, "name": cname, "ok": 0, "failed": 0})
                        st["failed"] += 1
                        items.append({
                            "instance_id": irec.get("id"),
                            "status": "federated_failed",
                            "value": None,
                            "error_code": e.code,
                            "error_message": e.message,
                            "capability_version": rec.get("version"),
                            "federated": {"source_connection": cid},
                        })
                        continue
                    merged_instance = dict(irec.get("instance") or {})
                    merged_instance.update(inputs)
                    effective = {**irec, "instance": merged_instance}
                res = self._execute_one(rec, effective, channel, lookup_cache)
                if res["status"] == "success":
                    n_success += 1
                    if res.get("value") is None:
                        n_null += 1
                else:
                    n_error += 1
                derived_entry: Optional[dict[str, Any]] = None
                if not dry_run:
                    if fed_meta is not None:
                        if res.get("status") == "success":
                            n_fed_success += 1
                            derived_entry = self._save_derived(rec, irec, res, fed_meta, trigger_source)
                        cid, cname = self._fed_conn_info(irec)
                        st = fed_system_stats.setdefault(
                            cid, {"connection_id": cid, "name": cname, "ok": 0, "failed": 0})
                        st["ok"] += 1
                    else:
                        # HF-11：累积写回补丁，块末统一 update_many（消除 O(n²)）。
                        patch = self._build_write_back_patch(rec, irec, res, channel, trigger_source)
                        pending.setdefault(object_type, {})[irec.get("id")] = patch
                item = {
                    "instance_id": irec.get("id"),
                    "status": res["status"],
                    "value": res.get("value"),
                    "error_code": res.get("error_code"),
                    "error_message": res.get("error_message"),
                    "capability_version": res.get("capability_version"),
                    "trace": res.get("trace"),
                }
                if fed_meta is not None:
                    self._annotate_trace_federated(res, fed_meta)
                    item["source_system"] = fed_meta.get("connection_name")
                    item["fetched_at"] = fed_meta.get("fetched_at")
                    item["federated"] = {
                        "source_connection": fed_meta.get("source_connection"),
                        "resolved_as": fed_meta.get("resolved_as"),
                    }
                    item["derived"] = derived_entry
                items.append(item)
            # 块末批量写回：一次读 + 一次写（HF-11）。
            if pending.get(object_type):
                self._instances.update_many(object_type, pending.pop(object_type))
                # 缓存键已带 ontology_id，按对象类型清除所有本体分区
                for k in [k for k in lookup_cache if k[0] == object_type]:
                    lookup_cache.pop(k, None)
        duration_ms = int((time.monotonic() - started) * 1000)
        return {
            "capability_id": rec.get("id"),
            "capability_name": rec.get("name"),
            "capability_version": rec.get("version"),
            "object_type": rec.get("object_type"),
            "total": len(records),
            "success": n_success,
            "error": n_error,
            "null": n_null,
            "dry_run": dry_run,
            "execution_channel": channel,
            "duration_ms": duration_ms,
            "items": items,
            "federated": {
                "required": len(fed_recs),
                "success": n_fed_success,
                "failed": n_fed_failed,
                "systems": list(fed_system_stats.values()),
            },
            "partial_failure": n_fed_failed > 0,
        }

    # ------------------------------------------------------------------
    # V2-04 渠道适配 · 源连接型（M4-A④：TP-04-06 / EP-04-03）
    # ------------------------------------------------------------------

    def _required_attrs(self, rec: dict[str, Any]) -> list[str]:
        """表达式静态引用的 this.<attr> 属性集（批量联邦读所需属性）。"""
        try:
            prog = parse(rec.get("expression", ""))
            return sorted(extract_attr_refs(prog))
        except ExprError:
            return []

    def _federate_input(self, irec: dict[str, Any], attrs: list[str],
                        user_id: Optional[str] = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """严格模式联邦读（TP-04-06）：仅 mode=federated 视为成功。

        回退底（legacy_local/fallback/无键）不作为能力输入——
        静默使用降级值会产半口径结果（EP-04-03），故失败一律上抛由调用方标记。
        """
        values, meta = content_access.federate_values(
            self.project_id, irec, user_id=user_id, attr_ids=attrs or None)
        if meta.get("error") or meta.get("mode") != "federated":
            raise CapabilityApiError(
                "CONTENT_UNAVAILABLE",
                f"联邦读失败（源连接不可用/无此键，mode={meta.get('mode')}）: "
                f"{meta.get('error') or '源端无此键'}",                 422,
                details={"mode": meta.get("mode"),
                         "connection_id": content_access.connection_id_of(irec)})
        return values, meta

    def _annotate_trace_federated(self, res: dict[str, Any], meta: dict[str, Any]) -> None:
        """溯源原始值：联邦读输入补记 源系统 + fetched_at（TP-04-06 原始值溯源）。"""
        trace = res.get("trace") or {}
        for entry in trace.get("inputs") or []:
            entry["source_channel"] = "federated_read"
            entry["source_system"] = meta.get("connection_name")
            entry["resolved_as"] = meta.get("resolved_as")
            entry["fetched_at"] = meta.get("fetched_at")
        trace["federated"] = {
            "resolved_as": meta.get("resolved_as"),
            "fetched_at": meta.get("fetched_at"),
            "as_of": meta.get("as_of"),
        }

    def _fed_conn_info(self, irec: dict[str, Any]) -> tuple[str, Optional[str]]:
        """实例来源连接（连接信息存于 instance._source.connection_id）→ (connection_id, 显示名)。"""
        cid = content_access.connection_id_of(irec) or "unknown"
        name = None
        if cid != "unknown":
            conn = content_access._load_connection(self.project_id, cid)
            if conn:
                name = conn.get("display_name") or conn.get("name")
        return cid, name

    def preview_federated_cost(self, cap_id: str, limit: int = 1000, offset: int = 0,
                               instance_ids: Optional[list[str]] = None) -> dict[str, Any]:
        """TP-04-06：批量联邦读成本预览（执行前提示、可取消——纯只读，不产生任何写）。

        键集→所需属性＋按源系统分组的读取次数估算；confirm_required=True 时
        执行端点要求 cost_confirmed=true。
        """
        rec = self._store.get(cap_id)
        if not rec:
            raise CapabilityApiError("NOT_FOUND", "能力不存在", 404)
        if instance_ids:
            records = []
            missing = []
            for iid in instance_ids:
                r = self._instances.get(iid)
                if r is None or r.get("object_type") != rec.get("object_type"):
                    missing.append(iid)
                else:
                    records.append(r)
            if missing:
                raise CapabilityApiError("INSTANCE_NOT_FOUND", f"实例不存在或类型不符: {missing[:5]}", 404)
        else:
            records = self._instances.find(
                object_type=rec.get("object_type"),
                filters={"project_id": self.project_id},
                limit=limit, offset=offset,
            )
            cap_oid = rec.get("ontology_id")
            if cap_oid:
                records = [r for r in records
                           if (r.get("ontology_id") or cap_oid) == cap_oid]
        attrs = self._required_attrs(rec)
        systems: dict[str, dict[str, Any]] = {}
        n_local = 0
        for r in records:
            if is_federated(self.project_id, r):
                cid, cname = self._fed_conn_info(r)
                st = systems.setdefault(
                    cid, {"connection_id": cid, "name": cname, "instances": 0})
                st["instances"] += 1
            else:
                n_local += 1
        total_reads = sum(s["instances"] for s in systems.values())
        return {
            "capability_id": cap_id,
            "capability_name": rec.get("name"),
            "capability_version": rec.get("version"),
            "object_type": rec.get("object_type"),
            "total_instances": len(records),
            "local_instances": n_local,
            "federated_instances": total_reads,
            "systems": list(systems.values()),
            "required_attrs": attrs,
            "total_federated_reads": total_reads,
            "est_scale": total_reads * max(len(attrs), 1),
            "confirm_required": total_reads > 0,
            "cancelable": True,
            "cost_hint": (
                f"批量执行需 {total_reads} 次联邦读（跨 {len(systems)} 个源连接）；"
                "读取受源端权限与限速约束，确认后方可执行（可直接取消）。"
                if total_reads else
                "无源连接型实例，全部本地渠道计算，无联邦读成本。"),
        }

    def list_derived(self, cap_id: str, instance_id: Optional[str] = None,
                     object_type: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        """派生图查询（TP-04-06）：派生值非权威，仅可查不可作为权威内容。"""
        rec = self._store.get(cap_id)
        if not rec:
            raise CapabilityApiError("NOT_FOUND", "能力不存在", 404)
        rows = [r for r in self._derived.all() if r.get("capability_id") == cap_id]
        if instance_id:
            rows = [r for r in rows if r.get("instance_id") == instance_id]
        if object_type:
            rows = [r for r in rows if r.get("object_type") == object_type]
        rows.sort(key=lambda r: r.get("executed_at") or "", reverse=True)
        return rows[:limit]

    def _save_derived(self, rec: dict[str, Any], irec: dict[str, Any], res: dict[str, Any],
                      meta: dict[str, Any], trigger_source: str) -> dict[str, Any]:
        """TP-04-06：派生值落派生图（不落权威实例存储、不写回源系统）。"""
        ts = (res.get("executed_at") or _now()).replace("T", "").replace(":", "")[:15]
        dv = {
            "id": f"dv_{rec.get('id')}_{irec.get('id')}_{ts}",
            "capability_id": rec.get("id"),
            "capability_version": rec.get("version"),
            "instance_id": irec.get("id"),
            "object_type": rec.get("object_type"),
            "output_attribute": rec.get("output_attribute"),
            "value": res.get("value"),
            "derived": True,
            "authoritative": False,
            "derived_note": DERIVED_NOTE,
            "execution_channel": res.get("execution_channel"),
            "trigger_source": trigger_source,
            "executed_at": res.get("executed_at"),
            "source_connection": meta.get("source_connection"),
            "connection_name": meta.get("connection_name"),
            "resolved_as": meta.get("resolved_as"),
            "fetched_at": meta.get("fetched_at"),
            "trace": res.get("trace"),
        }
        self._derived.insert(dv)
        return {"id": dv["id"], "authoritative": False, "note": DERIVED_NOTE}

    def _build_write_back_patch(self, rec: dict[str, Any], inst_record: dict[str, Any],
                                res: dict[str, Any], channel: str,
                                trigger_source: str) -> dict[str, Any]:
        """HF-11：构造写回补丁（不落库），供单实例直接 update / 批量聚合 update_many。

        写回内容：计算值（output_attribute，成功时）+ 能力元数据（6 类字段）+ 溯源。
        源字段不覆盖：仅写 output_attribute 与 __capability__ 元数据键。
        """
        instance = dict(inst_record.get("instance", {}) or {})
        if res["status"] == "success" and rec.get("output_attribute"):
            instance[rec["output_attribute"]] = res.get("value")
        lookups = (res.get("trace") or {}).get("lookups") or []
        instance["__capability__"] = {
            "capability_id": rec.get("id"),
            "capability_name": rec.get("name"),
            "capability_version": res.get("capability_version"),
            "executed_at": res.get("executed_at"),
            "execution_channel": channel,
            "trigger_source": trigger_source,
            "source_instance_id": inst_record.get("id"),
            "execution_status": res["status"],
            "value": res.get("value"),
            "error_code": res.get("error_code"),
            "error_message": res.get("error_message"),
            "trace": res.get("trace"),
            "input_overrides": res.get("input_overrides"),
            "data_gaps": res.get("data_gaps"),
            "inputs_persisted": res.get("inputs_persisted", False),
            # HF-04 追加字段（既有 15 字段保持不变，M1-B 溯源断言不受影响）
            "source_fingerprint": _source_fingerprint(lookups),
            "stale": False,
            "result_freshness": "fresh",
        }
        return {"instance": instance}

    def _write_back(self, rec: dict[str, Any], inst_record: dict[str, Any],
                    res: dict[str, Any], channel: str, trigger_source: str) -> None:
        """写回：计算值（output_attribute，成功时）+ 能力元数据（6 类字段）+ 溯源。
        源字段不覆盖：仅写 output_attribute 与 __capability__ 元数据键。"""
        patch = self._build_write_back_patch(rec, inst_record, res, channel, trigger_source)
        self._instances.update(inst_record.get("id"), patch)

    def affected_instances(self, cap_id: str, baseline_instance_id: str) -> dict[str, Any]:
        """HF-04：反查哪些结果实例的 trace 命中过这条基线，并判定是否 stale。

        读取时按当前基线实例值重算指纹，与写回时的 source_fingerprint 比对；
        历史写回（无指纹）不报 stale，避免误伤存量数据。
        """
        rec = self._store.get(cap_id)
        if not rec:
            raise CapabilityApiError("NOT_FOUND", "能力不存在", 404)
        items: list[dict[str, Any]] = []
        for r in self._instances.find(limit=None):
            if r.get("id") == baseline_instance_id:
                continue
            meta = (r.get("instance") or {}).get("__capability__") or {}
            if meta.get("capability_id") != cap_id:
                continue
            lookups = (meta.get("trace") or {}).get("lookups") or []
            hits = [l for l in lookups if l.get("matched_record_id") == baseline_instance_id]
            if not hits:
                continue
            current: list[dict[str, Any]] = []
            for l in lookups:
                rid, attr = l.get("matched_record_id"), l.get("attr")
                src = self._instances.get(rid) if rid else None
                cur = (src.get("instance") or {}).get(attr) if src and attr else None
                current.append({"matched_record_id": rid, "value": cur})
            current_fp = _source_fingerprint(current)
            stored_fp = meta.get("source_fingerprint")
            stale = bool(stored_fp and stored_fp != current_fp)
            items.append({
                "instance_id": r.get("id"),
                "object_type": r.get("object_type"),
                "value": meta.get("value"),
                "capability_version": meta.get("capability_version"),
                "source_fingerprint": stored_fp,
                "current_fingerprint": current_fp,
                "stale": stale,
                "matched_lookups": len(hits),
            })
        return {
            "capability_id": cap_id,
            "baseline_instance_id": baseline_instance_id,
            "total": len(items),
            "stale": sum(1 for i in items if i["stale"]),
            "items": items,
        }

    def invalidate_for_baseline(self, baseline_instance_id: str) -> dict[str, Any]:
        """HF-04：把命中该基线且指纹已变化的能力写回结果物理标记为 stale。"""
        cap_ids = sorted({c.get("id") for c in self._store.all() if c.get("id")})
        caps: list[str] = []
        seen: set[str] = set()
        for cap_id in cap_ids:
            res = self.affected_instances(cap_id, baseline_instance_id)
            if res.get("items"):
                caps.append(cap_id)
            for item in res.get("items", []):
                iid = item.get("instance_id")
                if not item.get("stale") or not iid or iid in seen:
                    continue
                rec = self._instances.get(iid)
                if not rec:
                    continue
                instance = dict(rec.get("instance") or {})
                meta = dict(instance.get("__capability__") or {})
                meta["stale"] = True
                meta["result_freshness"] = "stale"
                meta["current_fingerprint"] = item.get("current_fingerprint")
                meta["stale_detected_at"] = _now()
                instance["__capability__"] = meta
                self._instances.update(iid, {"instance": instance})
                seen.add(iid)
        return {
            "capabilities": caps,
            "marked": len(seen),
            "instance_ids": sorted(seen),
        }
