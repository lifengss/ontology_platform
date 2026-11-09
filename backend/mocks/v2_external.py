"""V2 可控外部 MCP 数据源 mock（VS2-00，测试大纲 §4.1）。

设计要点
--------
1. 调用形态与 ``backend.mcp_client``（http transport）完全兼容：
   - ``GET /health``  健康探测
   - ``GET /schema``  结构探测（只返回字段结构，不返回值 —— V2-11 schema 探测用）
   - ``POST /read``   ``{"tool": "read", "params": {...}}`` -> ``{"records": [...]}``
   - ``POST /write``  回写（记录调用流水 —— V2-09 审计回放对账 / V2-13 回写用）
   tool 名不敏感：read/read_all/query/schema/write 均按语义路由。
2. 数据面鉴权：``Authorization: Bearer <token>``（或裸 token），token->行范围映射
   由管理面热切换（权限切换能力；身份联邦 M2-B 的平台侧映射在此以 token 代入）。
   未知 token -> 401（平台侧 401 不重试、阻断）。
3. 五类可控注入（管理面，X-Mock-Admin 头）：
   - 时延：config.delay_ms（数据面/ CDC 面响应前 sleep）
   - 故障：config.fail_rate 随机 -> config.error_status（默认 500）
   - 断流：config.outage -> 数据面/CDC 面接受后直接断开连接（无响应）
   - schema 热变：/admin/schema rename|add|remove|change_type，schema_version 递增，
     并发 schema_change 事件进 CDC 日志
   - CDC 通道切换：config.cdc_channel ∈ none|binlog|mq|cursor；
     不支持的通道端点返回 409 CDC_NOT_SUPPORTED
4. CDC 三通道共用同一份事件日志（单调 LSN），保证同一变更序列可经任一通道
   完整重放（幂等/不重不漏断言、通道切换一致性断言）：
   - ``POST /cdc/cursor``    增量游标：``{"since": lsn, "limit": n}``
   - ``POST /cdc/mq``        MQ topic 模拟：``{"max": n}``，服务端维护 offset
   - ``POST /cdc/subscribe`` binlog 模拟长轮询：``{"since": lsn, "wait_ms": n}``
   变更来源：/write、/admin/data upsert|remove、schema 热变。
5. 全部状态进程内（threading.Lock 保护），测试可内嵌线程启动、可热重置
   （/admin/data reset 回到种子基线）。

端口约定（独立运行）：crm=18101, plm=18102, reservoir=18103（run_all.py）。
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

ADMIN_KEY_DEFAULT = "mock-admin-2026"

# ---------------------------------------------------------------------------
# 种子数据：三系统 PN 联邦查询集（确定性，seed=42）
# ---------------------------------------------------------------------------

REGIONS = ["华东", "华南", "华北", "西南"]
CRM_STATUSES = ["active", "expired", "pending"]
PLM_LIFECYCLES = ["design", "production", "eol"]
RSR_LEVELS = ["high", "normal", "low"]


@dataclass
class ThreeSystemSeed:
    """三系统种子：600 个 PN 的分布（详见 data/datasets/v2/README.md）。"""

    crm_records: List[Dict[str, Any]]
    plm_records: List[Dict[str, Any]]
    reservoir_records: List[Dict[str, Any]]
    conflict_keys: List[str] = field(default_factory=list)
    distribution: Dict[str, int] = field(default_factory=dict)


def build_three_system_seed(seed: int = 42, total_keys: int = 600) -> ThreeSystemSeed:
    """确定性生成三系统 PN 数据。

    分布（600 键）：
    - 400 键三系统共有
    - 100 键恰好两系统（crm+plm 33 / plm+reservoir 33 / crm+reservoir 34）
    - 50 键单系统（crm 17 / plm 17 / reservoir 16）
    值冲突：从三系统共有键中取 50 个，各系统"名称"属性互不一致（TP-01-07）。
    """
    rng = random.Random(seed)
    keys = [f"PN-{100000 + i}" for i in range(total_keys)]
    rng.shuffle(keys)

    in_all = keys[:400]
    two_groups = [
        ("crm_plm", keys[400:433]),
        ("plm_res", keys[433:466]),
        ("crm_res", keys[466:500]),
    ]
    single_groups = [
        ("crm", keys[500:517]),
        ("plm", keys[517:534]),
        ("reservoir", keys[534:550]),
    ]
    conflict_keys = sorted(rng.sample(in_all, 50))

    def base_name(k: str) -> str:
        return f"产品{int(k.split('-')[1])}"

    crm, plm, reservoir = [], [], []

    def emit(k: str, in_crm: bool, in_plm: bool, in_rsr: bool) -> None:
        conflicted = k in conflict_keys
        name_a = base_name(k)
        name_b = f"{name_a}(甲)" if conflicted else name_a
        name_c = f"{name_a}(乙)" if conflicted else name_a
        if in_crm:
            crm.append({
                "PN": k,
                "product_name": name_a,
                "customer_name": f"客户{rng.randint(1, 200)}",
                "region": rng.choice(REGIONS),
                "contract_amount": round(rng.uniform(1e4, 5e6), 2),
                "contract_status": rng.choice(CRM_STATUSES),
            })
        if in_plm:
            plm.append({
                "PN码": k,
                "产品名称": name_b,
                "product_model": f"MOD-{rng.randint(100, 999)}",
                "version": f"v{rng.randint(1, 5)}.{rng.randint(0, 9)}",
                "life_cycle": rng.choice(PLM_LIFECYCLES),
            })
        if in_rsr:
            reservoir.append({
                "PN_Num": k,
                "PN名称": name_c,
                "capacity_mw": round(rng.uniform(0.5, 500.0), 3),
                "reservoir_level": rng.choice(RSR_LEVELS),
                "design_year": rng.randint(1995, 2025),
            })

    for k in in_all:
        emit(k, True, True, True)
    for _, ks in two_groups:
        for k in ks:
            if ks is two_groups[0][1]:
                emit(k, True, True, False)
            elif ks is two_groups[1][1]:
                emit(k, False, True, True)
            else:
                emit(k, True, False, True)
    for _, ks in single_groups:
        for k in ks:
            emit(k, ks is single_groups[0][1], ks is single_groups[1][1],
                 ks is single_groups[2][1])

    distribution = {
        "total_keys": total_keys,
        "in_all_three": len(in_all),
        "in_two": {g: len(ks) for g, ks in two_groups},
        "in_single": {g: len(ks) for g, ks in single_groups},
        "conflict_keys": len(conflict_keys),
        "row_counts": {"crm": len(crm), "plm": len(plm), "reservoir": len(reservoir)},
    }
    return ThreeSystemSeed(crm, plm, reservoir, conflict_keys, distribution)


def build_default_tokens(seed: ThreeSystemSeed) -> Dict[str, Dict[str, Any]]:
    """默认权限 token：tok-full=全量；tok-even/tok-odd=按 PN 数字奇偶的行子集。

    子集确定性与系统无关（按 PN 数值），三系统同一 token 可见同一 PN 集合，
    便于身份联邦"同一本体 A/B 范围不同"类断言。
    """
    def pn_num(k: str) -> int:
        return int(str(k).rsplit("-", 1)[-1])

    tokens: Dict[str, Dict[str, Any]] = {
        "tok-full": {"scope": "all", "keys": []},
    }
    all_keys = [r["PN"] for r in seed.crm_records] \
        + [r["PN码"] for r in seed.plm_records] \
        + [r["PN_Num"] for r in seed.reservoir_records]
    even = sorted({k for k in all_keys if pn_num(k) % 2 == 0})
    odd = sorted({k for k in all_keys if pn_num(k) % 2 == 1})
    tokens["tok-even"] = {"scope": "subset", "keys": even}
    tokens["tok-odd"] = {"scope": "subset", "keys": odd}
    tokens["tok-empty"] = {"scope": "none", "keys": []}
    return tokens


# ---------------------------------------------------------------------------
# 可控 mock 系统
# ---------------------------------------------------------------------------

class ControllableMockSystem:
    """单个可控外部系统（CRM/PLM/水库）。

    ``start(port=0)`` 内嵌线程启动（port=0 自动分配，返回实际端口）。
    """

    def __init__(
        self,
        name: str,
        key_field: str,
        fields: List[tuple],
        records: List[Dict[str, Any]],
        tokens: Optional[Dict[str, Dict[str, Any]]] = None,
        cdc_channel: str = "cursor",
        admin_key: str = ADMIN_KEY_DEFAULT,
    ) -> None:
        self.name = name
        self.key_field = key_field
        self.fields: List[Dict[str, str]] = [{"name": n, "type": t} for n, t in fields]
        self._records: List[Dict[str, Any]] = [dict(r) for r in records]
        self._baseline: List[Dict[str, Any]] = [dict(r) for r in records]
        self.tokens: Dict[str, Dict[str, Any]] = dict(tokens or {})
        self.admin_key = admin_key

        self.schema_version = 1
        self.config: Dict[str, Any] = {
            "delay_ms": 0,
            "fail_rate": 0.0,
            "error_status": 500,
            "outage": False,
            "cdc_channel": cdc_channel,
            "subscribe_timeout_ms": 2000,
        }
        # CDC 事件日志（单调 LSN）与 MQ offset
        self._events: List[Dict[str, Any]] = []
        self._lsn = 0
        self._mq_offset = 0
        self._cond = threading.Condition()
        self.write_calls: List[Dict[str, Any]] = []
        self.read_calls = 0
        self.error_injections = 0

        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 0

    # ---------------- 内部工具 ----------------

    def _emit_event(self, op: str, key: Any, before: Any = None, after: Any = None) -> int:
        with self._cond:
            self._lsn += 1
            ev = {
                "lsn": self._lsn,
                "ts": time.time(),
                "op": op,
                "key": key,
                "before": before,
                "after": after,
            }
            self._events.append(ev)
            self._cond.notify_all()
            return self._lsn

    def _filtered_records(self, scope: Dict[str, Any]) -> List[Dict[str, Any]]:
        if scope.get("scope") == "all":
            return [dict(r) for r in self._records]
        if scope.get("scope") == "none":
            return []
        keys = set(scope.get("keys") or [])
        return [dict(r) for r in self._records if r.get(self.key_field) in keys]

    def _record_by_key(self, key: Any) -> Optional[Dict[str, Any]]:
        for r in self._records:
            if r.get(self.key_field) == key:
                return r
        return None

    # ---------------- 生命周期 ----------------

    def start(self, port: int = 0, host: str = "127.0.0.1") -> int:
        system = self
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            # ---- 基础 ----
            def log_message(self, fmt, *args):  # 静默
                pass

            def _send_json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                    return data if isinstance(data, dict) else {}
                except (ValueError, UnicodeDecodeError):
                    return {}

            def _token(self) -> Optional[str]:
                auth = self.headers.get("Authorization") or ""
                if auth.lower().startswith("bearer "):
                    return auth[7:].strip()
                return auth.strip() or None

            def _admin_ok(self) -> bool:
                return self.headers.get("X-Mock-Admin") == outer.admin_key

            def _outage_or_fail(self) -> bool:
                """时延/故障/断流注入；返回 True 表示连接已处置（不再继续）。"""
                cfg = outer.config
                if cfg.get("delay_ms"):
                    time.sleep(max(0, cfg["delay_ms"]) / 1000.0)
                if cfg.get("outage"):
                    # 断流：接受后直接断开，无响应（客户端表现为超时/连接重置）
                    time.sleep(0.05)
                    self.close_connection = True
                    return True
                if cfg.get("fail_rate") and random.random() < cfg["fail_rate"]:
                    outer.error_injections += 1
                    self._send_json(int(cfg.get("error_status", 500)),
                                    {"error": "INJECTED_FAILURE", "system": outer.name})
                    return True
                return False

            # ---- 路由 ----
            def do_GET(self):  # noqa: N802
                if self.path.rstrip("/") == "/health":
                    self._send_json(200, {"status": "ok", "system": outer.name,
                                          "schema_version": outer.schema_version})
                elif self.path.rstrip("/") == "/schema":
                    if self._outage_or_fail():
                        return
                    self._send_json(200, {
                        "system": outer.name,
                        "schema_version": outer.schema_version,
                        "key_field": outer.key_field,
                        "fields": list(outer.fields),
                    })
                elif self.path.rstrip("/") == "/_admin/state":
                    if not self._admin_ok():
                        self._send_json(403, {"error": "ADMIN_FORBIDDEN"})
                        return
                    with system._cond:
                        events_tail = list(system._events[-100:])
                        lsn = system._lsn
                    self._send_json(200, {
                        "system": outer.name,
                        "config": dict(outer.config),
                        "schema_version": outer.schema_version,
                        "fields": list(outer.fields),
                        "record_count": len(outer._records),
                        "lsn": lsn,
                        "event_count": len(system._events),
                        "mq_offset": system._mq_offset,
                        "read_calls": outer.read_calls,
                        "error_injections": outer.error_injections,
                        "write_calls": outer.write_calls[-100:],
                        "events_tail": events_tail,
                    })
                else:
                    self._send_json(404, {"error": "NOT_FOUND", "path": self.path})

            def do_POST(self):  # noqa: N802
                path = self.path.rstrip("/")
                body = self._read_body()

                # ---- 管理面（不需要 Bearer）----
                if path.startswith("/_admin/"):
                    if not self._admin_ok():
                        self._send_json(403, {"error": "ADMIN_FORBIDDEN"})
                        return
                    self._handle_admin(path, body)
                    return

                # ---- CDC 面（也走数据面鉴权）----
                if path.startswith("/cdc/"):
                    tok = self._token()
                    scope = outer.tokens.get(tok) if tok else None
                    if scope is None:
                        self._send_json(401, {"error": "UNAUTHORIZED"})
                        return
                    if self._outage_or_fail():
                        return
                    chan = outer.config.get("cdc_channel", "cursor")
                    allowed = {
                        "/cdc/cursor": "cursor",
                        "/cdc/mq": "mq",
                        "/cdc/subscribe": "binlog",
                    }
                    if chan == "none" or allowed.get(path) != chan:
                        self._send_json(409, {
                            "error": "CDC_NOT_SUPPORTED",
                            "supported_channel": chan,
                        })
                        return
                    if path == "/cdc/cursor":
                        self._send_json(200, self._cdc_cursor(body))
                    elif path == "/cdc/mq":
                        self._send_json(200, self._cdc_mq(body))
                    else:
                        self._send_json(200, self._cdc_subscribe(body))
                    return

                # ---- 数据面 ----
                if path in ("/read", "/write", "/schema"):
                    tok = self._token()
                    scope = outer.tokens.get(tok) if tok else None
                    if scope is None:
                        self._send_json(401, {"error": "UNAUTHORIZED"})
                        return
                    if self._outage_or_fail():
                        return
                    tool = str(body.get("tool") or path.lstrip("/"))
                    params = body.get("params") or {}
                    if path == "/read" or tool in ("read", "read_all", "query", "read_batch"):
                        outer.read_calls += 1
                        self._send_json(200, self._do_read(scope, params))
                    elif tool == "schema":
                        self._send_json(200, {
                            "system": outer.name,
                            "schema_version": outer.schema_version,
                            "key_field": outer.key_field,
                            "fields": list(outer.fields),
                        })
                    else:  # write
                        status, payload = self._do_write(tok, scope, params)
                        self._send_json(status, payload)
                    return

                self._send_json(404, {"error": "NOT_FOUND", "path": path})

            # ---- 数据面动作 ----
            def _do_read(self, scope: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
                rows = system._filtered_records(scope)
                kf = outer.key_field
                # 单键过滤
                if kf in params and params[kf] is not None:
                    rows = [r for r in rows if r.get(kf) == params[kf]]
                # 任意字段等值过滤（平台侧过滤下推）
                for k, v in params.items():
                    if k in ("limit", kf):
                        continue
                    if k in {f["name"] for f in outer.fields}:
                        rows = [r for r in rows if r.get(k) == v]
                limit = params.get("limit")
                if isinstance(limit, int) and limit > 0:
                    rows = rows[:limit]
                return {"records": rows, "count": len(rows)}

            def _do_write(self, tok: str, scope: Dict[str, Any],
                          params: Dict[str, Any]) -> tuple:
                key = params.get(outer.key_field)
                if key is None:
                    return 400, {"error": "MISSING_KEY",
                                 "key_field": outer.key_field}
                outer.write_calls.append({
                    "ts": time.time(), "token": tok, "payload": params,
                })
                existing = system._record_by_key(key)
                if existing is None:
                    rec = {f["name"]: params.get(f["name"]) for f in outer.fields}
                    system._records.append(rec)
                    system._emit_event("create", key, after=rec)
                    return 200, {"ok": True, "key": key, "op": "create"}
                before = dict(existing)
                for f in outer.fields:
                    if f["name"] in params:
                        existing[f["name"]] = params[f["name"]]
                system._emit_event("update", key, before=before, after=dict(existing))
                return 200, {"ok": True, "key": key, "op": "update"}

            # ---- CDC 动作（持锁读取事件日志）----
            def _cdc_cursor(self, body: Dict[str, Any]) -> Dict[str, Any]:
                since = int(body.get("since") or 0)
                limit = int(body.get("limit") or 100)
                with system._cond:
                    evs = [e for e in system._events if e["lsn"] > since][:limit]
                nxt = evs[-1]["lsn"] if evs else since
                return {"events": evs, "next_cursor": nxt}

            def _cdc_mq(self, body: Dict[str, Any]) -> Dict[str, Any]:
                max_n = int(body.get("max") or 100)
                with system._cond:
                    offset = system._mq_offset
                    evs = [e for e in system._events if e["lsn"] > offset][:max_n]
                    if evs:
                        system._mq_offset = evs[-1]["lsn"]
                return {"events": evs, "next_offset": system._mq_offset}

            def _cdc_subscribe(self, body: Dict[str, Any]) -> Dict[str, Any]:
                since = int(body.get("since") or 0)
                wait_ms = int(body.get("wait_ms")
                              or outer.config.get("subscribe_timeout_ms", 2000))
                wait_ms = min(max(wait_ms, 0), 15000)
                deadline = time.time() + wait_ms / 1000.0
                with system._cond:
                    while True:
                        evs = [e for e in system._events if e["lsn"] > since]
                        if evs:
                            return {"events": evs, "next_cursor": evs[-1]["lsn"]}
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            return {"events": [], "next_cursor": since}
                        system._cond.wait(min(remaining, 0.2))

            def _handle_admin(self, path: str, body: Dict[str, Any]) -> None:
                p = path[len("/_admin/"):]
                if p == "config":
                    with system._cond:
                        for k, v in body.items():
                            if k in outer.config:
                                outer.config[k] = v
                    self._send_json(200, {"ok": True, "config": dict(outer.config)})
                elif p == "schema":
                    self._admin_schema(body)
                elif p == "identity":
                    self._admin_identity(body)
                elif p == "data":
                    self._admin_data(body)
                elif p == "cdc":
                    self._admin_cdc(body)
                else:
                    self._send_json(404, {"error": "NOT_FOUND", "path": path})

            def _admin_schema(self, body: Dict[str, Any]) -> None:
                action = body.get("action")
                fld = body.get("field")
                names = {f["name"] for f in outer.fields}
                if action == "rename" and fld and fld in names:
                    new = body.get("new_field")
                    if not new or new in names:
                        self._send_json(400, {"error": "INVALID_RENAME"})
                        return
                    for f in outer.fields:
                        if f["name"] == fld:
                            f["name"] = new
                    for r in system._records:
                        if fld in r:
                            r[new] = r.pop(fld)
                elif action == "add" and fld and fld not in names:
                    outer.fields.append({"name": fld, "type": body.get("type", "string")})
                elif action == "remove" and fld and fld in names and fld != outer.key_field:
                    outer.fields = [f for f in outer.fields if f["name"] != fld]
                    for r in system._records:
                        r.pop(fld, None)
                elif action == "change_type" and fld and fld in names:
                    for f in outer.fields:
                        if f["name"] == fld:
                            f["type"] = body.get("type", f["type"])
                else:
                    self._send_json(400, {"error": "INVALID_SCHEMA_OP", "body": body})
                    return
                outer.schema_version += 1
                system._emit_event("schema_change", fld,
                                   after={"action": action,
                                          "fields": [f["name"] for f in outer.fields]})
                self._send_json(200, {"ok": True, "schema_version": outer.schema_version,
                                      "fields": list(outer.fields)})

            def _admin_identity(self, body: Dict[str, Any]) -> None:
                action = body.get("action", "set")
                tok = body.get("token")
                if not tok:
                    self._send_json(400, {"error": "MISSING_TOKEN"})
                    return
                if action == "set":
                    outer.tokens[tok] = {
                        "scope": body.get("scope", "none"),
                        "keys": list(body.get("keys") or []),
                    }
                elif action == "remove":
                    outer.tokens.pop(tok, None)
                else:
                    self._send_json(400, {"error": "INVALID_ACTION"})
                    return
                self._send_json(200, {"ok": True, "tokens": list(outer.tokens)})

            def _admin_data(self, body: Dict[str, Any]) -> None:
                action = body.get("action")
                if action == "reset":
                    system._records = [dict(r) for r in outer._baseline]
                    self._send_json(200, {"ok": True, "record_count": len(system._records)})
                elif action == "upsert":
                    recs = body.get("records") or []
                    ops = []
                    for params in recs:
                        key = params.get(outer.key_field)
                        if key is None:
                            continue
                        existing = system._record_by_key(key)
                        if existing is None:
                            rec = {f["name"]: params.get(f["name"]) for f in outer.fields}
                            system._records.append(rec)
                            system._emit_event("create", key, after=rec)
                            ops.append("create")
                        else:
                            before = dict(existing)
                            for f in outer.fields:
                                if f["name"] in params:
                                    existing[f["name"]] = params[f["name"]]
                            system._emit_event("update", key, before=before,
                                               after=dict(existing))
                            ops.append("update")
                    self._send_json(200, {"ok": True, "ops": ops})
                elif action == "remove":
                    removed = []
                    for key in body.get("keys") or []:
                        rec = system._record_by_key(key)
                        if rec is not None:
                            system._records.remove(rec)
                            system._emit_event("remove", key, before=rec)
                            removed.append(key)
                    self._send_json(200, {"ok": True, "removed": removed})
                else:
                    self._send_json(400, {"error": "INVALID_ACTION"})

            def _admin_cdc(self, body: Dict[str, Any]) -> None:
                action = body.get("action")
                if action == "reset_offset":
                    with system._cond:
                        system._mq_offset = 0
                    self._send_json(200, {"ok": True, "mq_offset": 0})
                elif action == "reset":
                    with system._cond:
                        system._events.clear()
                        system._lsn = 0
                        system._mq_offset = 0
                    self._send_json(200, {"ok": True, "lsn": 0, "event_count": 0})
                else:
                    self._send_json(400, {"error": "INVALID_ACTION"})

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
            name=f"mock-{self.name}")
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


# ---------------------------------------------------------------------------
# 预置三系统（字段名各异：PN / PN码 / PN_Num）
# ---------------------------------------------------------------------------

CRM_FIELDS = [
    ("PN", "string"),
    ("product_name", "string"),
    ("customer_name", "string"),
    ("region", "string"),
    ("contract_amount", "decimal"),
    ("contract_status", "enum"),
]
PLM_FIELDS = [
    ("PN码", "string"),
    ("产品名称", "string"),
    ("product_model", "string"),
    ("version", "string"),
    ("life_cycle", "enum"),
]
RESERVOIR_FIELDS = [
    ("PN_Num", "string"),
    ("PN名称", "string"),
    ("capacity_mw", "decimal"),
    ("reservoir_level", "enum"),
    ("design_year", "integer"),
]


def make_crm(seed: int = 42, **kw) -> ControllableMockSystem:
    s = build_three_system_seed(seed=seed)
    return ControllableMockSystem(
        "crm", "PN", CRM_FIELDS, s.crm_records,
        tokens=build_default_tokens(s), cdc_channel="cursor", **kw)


def make_plm(seed: int = 42, **kw) -> ControllableMockSystem:
    s = build_three_system_seed(seed=seed)
    return ControllableMockSystem(
        "plm", "PN码", PLM_FIELDS, s.plm_records,
        tokens=build_default_tokens(s), cdc_channel="mq", **kw)


def make_reservoir(seed: int = 42, **kw) -> ControllableMockSystem:
    s = build_three_system_seed(seed=seed)
    return ControllableMockSystem(
        "reservoir", "PN_Num", RESERVOIR_FIELDS, s.reservoir_records,
        tokens=build_default_tokens(s), cdc_channel="binlog", **kw)


def make_all(seed: int = 42) -> Dict[str, ControllableMockSystem]:
    return {"crm": make_crm(seed), "plm": make_plm(seed),
            "reservoir": make_reservoir(seed)}
