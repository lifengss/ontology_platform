"""cdc：V2-12（M3-D）流式 CDC 消费端 + 连接健康视图支撑。

设计口径（docs/V2-需求列表.md V2-12 / docs/V2-测试大纲.md §12）：
- CDC 是**数据面独立通道**（直连源系统 /cdc/* 端点，治理身份=连接自带凭据），
  **不走 MCP 工具协议**（MCP 只承接查询类/回写类调用）。
- 三种变更通道：binlog（模拟订阅，长轮询 /cdc/subscribe）/ 消息队列（/cdc/mq，服务端 offset）/
  增量游标拉取（/cdc/cursor，lsn 游标）；事件 {lsn, ts, op, key, before, after}，LSN 单调。
- 增量归化二分流：**低风险自动入库**（键增/改/删 + 关系边，直接更新关系快照）/
  **人工确认**（schema_change、键字段缺失等异常 → 候选队列 source_type="cdc" + V2-11 漂移联动）。
- **只更新关系快照，不同步内容值**（TP-12-03 负向断言）：CDC 写入/更新实例时
  仅落 键属性 + 关系属性 + _source（kind="cdc"），事件携带的内容值属性一律不写。
- 幂等：event.lsn <= last_lsn → 跳过（重复投递 dup / 乱序旧事件 stale），不重放（EP-12-02）。
- 告警/熔断/限流：事件滞后（lag）/断流（stalled）阈值告警；连续失败熔断（指数退避，
  半开探测恢复）；单批上限 + 批间停顿消化积压（EP-12-03，不击穿平台）。
- 通道边界（TP-12-05）：源系统无变更通道（409 CDC_NOT_SUPPORTED 或显式 channel="none"）
  → supported=False、健康视图「CDC 未支持」，新鲜度降级为**周期拉取 + 联邦读**
  （fallback_pull 全量读 → 低风险归化键集）。
- V2-11 联动（TP-12-08）：门禁（snapshot_auto_update=stopped 期间不消费）、
  review 事件触发 check_connection(source="cdc")（事件字段结构 = 实时漂移信号，检测通道②）。

本模块所有函数**永不向主流程抛错**：轮询失败记轮状态/统计/熔断，不阻断 API 主流程。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from audit import record as audit_record
from config import get_settings
from core.instance_store import get_instance_store
from core.storage import JsonStore
from dictionary import resolve_field_map
from logging_setup import get_logger
from mcp_client import McpClient, _auth_header
from schema_drift import assert_connection_allowed, check_connection, key_source_field

logger = get_logger("cdc")

CHANNELS = ("binlog", "mq", "cursor")
_CHANNEL_PATH = {"binlog": "/cdc/subscribe", "mq": "/cdc/mq", "cursor": "/cdc/cursor"}
_ALERT_RING = 20


class CdcNotSupported(Exception):
    """源系统无变更通道 / 通道不匹配（mock 返回 409 CDC_NOT_SUPPORTED）。"""

    def __init__(self, supported_channel: str | None) -> None:
        super().__init__(f"CDC_NOT_SUPPORTED (supported={supported_channel})")
        self.supported_channel = supported_channel


class CdcFetchError(Exception):
    """CDC 通道拉取失败（网络/HTTP 错误）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _conn_store(project_id: str) -> JsonStore:
    return JsonStore("mcp_connections", project_id)


def _endpoint_origin(endpoint: str) -> str:
    """连接 endpoint 取 origin：数据面端点（/read、/schema、/cdc/*）统一挂在服务根下。

    endpoint 可能是服务根（mock）或 根+/mcp、根+/read（真实部署 MCP 路径），
    与 mcp_client.probe_schema 同口径只取 scheme://host[:port]。
    """
    ep = (endpoint or "").strip()
    if not ep:
        raise CdcFetchError("连接缺少 endpoint")
    parts = urlsplit(ep)
    if not parts.scheme or not parts.netloc:
        raise CdcFetchError(f"连接 endpoint 不是合法 URL: {endpoint}")
    return f"{parts.scheme}://{parts.netloc}"


def effective_channel(conn: dict[str, Any]) -> str:
    """连接生效 CDC 通道：cfg.channel=auto → 已验证的 supported_channel，否则返回内部
    标记 "probe"（poll_connection 走 _auto_probe 自动探测，源端 409 提示优先）；
    none → 显式不启用；显式通道 → 原样。"""
    cdc = conn.get("cdc") or {}
    ch = cdc.get("channel") or "auto"
    if ch == "auto":
        hint = cdc.get("supported_channel")
        return hint if hint in CHANNELS else "probe"
    if ch == "none":
        return "none"
    return ch if ch in CHANNELS else "probe"


def _auto_probe(conn: dict[str, Any], cdc: dict[str, Any], last_lsn: int) -> tuple[str, dict[str, Any]]:
    """auto 通道探测：源端 409 提示的通道优先，其余按固定顺序 binlog→mq→cursor 逐一探测。

    成功：返回 (channel, 首批数据) 并把已验证通道写入 cdc["supported_channel"]（下轮走快路径）；
    全部 409：抛 CdcNotSupported（源端声明无变更通道 → not_supported 降级路径）；
    全部连接失败：抛最后一次 CdcFetchError（走 error/熔断路径）。
    探测失败不单独告警：由调用方统一落 not_supported/error 状态与统计。
    """
    candidates: list[str] = []
    hint = cdc.get("last_supported_channel")
    if hint in CHANNELS:
        candidates.append(hint)
    for c in CHANNELS:
        if c not in candidates:
            candidates.append(c)
    tried: list[str] = []
    last_hint: str | None = None
    last_fetch_err: CdcFetchError | None = None
    for cand in candidates:
        if cand in tried:
            continue
        tried.append(cand)
        data = None
        follow_hint: str | None = None
        try:
            data = _fetch_events(conn, cand, last_lsn)
        except CdcNotSupported as e:
            last_hint = e.supported_channel
            cdc["last_supported_channel"] = e.supported_channel
            follow_hint = e.supported_channel
        except CdcFetchError as e:
            last_fetch_err = e
        if follow_hint is not None and follow_hint in CHANNELS and follow_hint not in tried:
            # 源端明确告知另一通道：立即跟随（不再排队），一次探测机会
            tried.append(follow_hint)
            try:
                data = _fetch_events(conn, follow_hint, last_lsn)
                cdc.update({"supported_channel": follow_hint, "supported": True})
                return follow_hint, data
            except CdcNotSupported as e:
                last_hint = e.supported_channel
                cdc["last_supported_channel"] = e.supported_channel
                continue
            except CdcFetchError as e:
                last_fetch_err = e
                continue
        if data is not None:
            cdc.update({"supported_channel": cand, "supported": True})
            return cand, data
    if last_fetch_err is not None:
        raise last_fetch_err
    raise CdcNotSupported(last_hint or "none")


def _fetch_events(conn: dict[str, Any], channel: str, since_lsn: int) -> dict[str, Any]:
    """按通道拉取一批事件。数据面直连（治理身份），不经 MCP 工具协议。"""
    if channel not in CHANNELS:
        raise CdcFetchError(f"无法确定 CDC 通道（channel={channel}）")
    settings = get_settings()
    url = _endpoint_origin(conn.get("endpoint")) + _CHANNEL_PATH[channel]
    headers = _auth_header(conn.get("auth") or {})
    if channel == "binlog":
        body: dict[str, Any] = {"since": since_lsn, "wait_ms": settings.cdc_subscribe_wait_ms}
    elif channel == "mq":
        body = {"max": settings.cdc_max_batch}
    else:
        body = {"since": since_lsn, "limit": settings.cdc_max_batch}
    timeout = max(5.0, settings.cdc_subscribe_wait_ms / 1000.0 + 10.0)
    try:
        with httpx.Client(timeout=timeout, verify=False) as client:
            r = client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise CdcFetchError(f"{type(e).__name__}: {e}") from e
    if r.status_code == 409:
        try:
            sup = (r.json() or {}).get("supported_channel")
        except Exception:  # noqa: BLE001
            sup = None
        raise CdcNotSupported(sup)
    if r.status_code >= 400:
        raise CdcFetchError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json() or {}
    except Exception as e:  # noqa: BLE001
        raise CdcFetchError(f"响应非 JSON: {r.text[:200]}") from e


def _object_of(project_id: str, conn: dict[str, Any]) -> dict[str, Any] | None:
    maps = conn.get("maps_to_ontology") or {}
    ont = JsonStore("ontologies", project_id=project_id).get(maps.get("ontology_id") or "")
    obj = next((o for o in (ont or {}).get("objects", []) if o.get("id") == maps.get("object_type")), None)
    return obj or None


def normalize_event(project_id: str, conn: dict[str, Any], ev: dict[str, Any]) -> dict[str, Any]:
    """增量归化：CDC 事件 → 低风险自动入库 / 人工确认 二分流（V2-12 增量归化口径）。

    低风险 = op ∈ create/update/remove 且键源字段可得（只更新关系快照：键 + 关系边）；
    人工确认 = schema_change 事件 / 键字段缺失（结构异常，交候选队列 + V2-11 漂移联动）。
    内容值属性永不进入归化输出（不同步内容值）。
    """
    op = ev.get("op")
    maps = conn.get("maps_to_ontology") or {}
    field_map, _ = resolve_field_map(project_id, conn)
    obj = _object_of(project_id, conn)
    key_attr = (obj or {}).get("stable_key")
    key_src = key_source_field(project_id, conn, field_map or {})
    relation_ids = {r.get("id") for r in (obj or {}).get("relations", []) if r.get("id")}
    out: dict[str, Any] = {
        "op": op, "key_attr": key_attr, "key_src": key_src,
        "key": None, "relation_attrs": {}, "risk": "low", "reason": None,
    }
    if op == "schema_change":
        out["risk"] = "review"
        out["reason"] = "schema_change 事件（V2-11 检测通道②：事件字段结构实时漂移信号）"
        return out
    base = ev.get("after") if op in ("create", "update") else ev.get("before")
    base = base if isinstance(base, dict) else {}
    key = base.get(key_src) if key_src else None
    out["key"] = key
    if key is None:
        out["risk"] = "review"
        out["reason"] = f"事件缺少键源字段 {key_src!r}（结构异常，需人工确认并触发漂移检测）"
        return out
    if op not in ("create", "update", "remove"):
        out["risk"] = "review"
        out["reason"] = f"未知操作类型 {op!r}"
        return out
    if op in ("create", "update"):
        for src, attr in (field_map or {}).items():
            if attr in relation_ids and src in base and base[src] is not None:
                out["relation_attrs"][attr] = base[src]
    return out


def _apply_snapshot(project_id: str, conn: dict[str, Any], norm: dict[str, Any],
                    ev: dict[str, Any], channel: str) -> str:
    """低风险自动入库：只更新关系快照（键增/改/删 + 关系边），**不写内容值**（TP-12-03）。

    返回 "inserted" | "updated" | "deleted" | "noop"。
    """
    maps = conn.get("maps_to_ontology") or {}
    object_type = maps.get("object_type")
    key_val = norm["key"]
    if object_type is None or norm.get("key_attr") is None or key_val is None:
        return "noop"
    istore = get_instance_store(project_id)
    key_str = str(key_val)
    src: dict[str, Any] = {
        "kind": "cdc", "connection_id": conn.get("id"), "channel": channel,
        "fetched_at": _now_iso(), "event_lsn": ev.get("lsn"), "event_ts": ev.get("ts"),
        "op": ev.get("op"),
    }
    existing = istore.find(object_type=object_type, filters={"stable_key_value": key_str})
    if norm["op"] == "remove":
        if not existing:
            return "noop"
        for rec in existing:
            istore.delete(rec["id"])
        return "deleted"
    inst = {norm["key_attr"]: key_val, **norm["relation_attrs"], "_source": src}
    if existing:
        rec = existing[0]
        old_inst = rec.get("instance") or {}
        # 保留既有内容值（非 CDC 写入），仅刷新 键 + 关系边 + _source
        merged = {**old_inst, **inst}
        istore.update(rec["id"], {"instance": merged, "stable_key_value": key_str,
                                  "updated_at": _now_iso()})
        return "updated"
    istore.insert({
        "id": uuid.uuid4().hex, "project_id": project_id,
        "ontology_id": maps.get("ontology_id"), "object_type": object_type,
        "candidate_id": None, "instance": inst, "imported_at": _now_iso(),
        "tx_id": None, "stable_key": norm["key_attr"], "stable_key_value": key_str,
    })
    return "inserted"


def _create_review_candidate(project_id: str, conn: dict[str, Any], ev: dict[str, Any],
                             norm: dict[str, Any]) -> dict[str, Any]:
    """非低风险事件 → 候选队列（source_type="cdc"，人工确认复用 V2 审批链路）+ V2-11 漂移联动。"""
    maps = conn.get("maps_to_ontology") or {}
    inst: dict[str, Any] = {}
    if norm.get("key") is not None and norm.get("key_attr"):
        inst[norm["key_attr"]] = norm["key"]
    inst.update(norm.get("relation_attrs") or {})
    inst["_source"] = {
        "kind": "cdc", "connection_id": conn.get("id"), "fetched_at": _now_iso(),
        "event_lsn": ev.get("lsn"), "event_ts": ev.get("ts"), "op": ev.get("op"),
        "cdc_reason": norm.get("reason"),
    }
    cand = {
        "id": f"cdc-{uuid.uuid4().hex[:16]}", "project_id": project_id,
        "ontology_id": maps.get("ontology_id"), "object_type": maps.get("object_type"),
        "source_type": "cdc", "source_ref": conn.get("id"),
        "instances": [inst] if inst else [],
        "status": "pending",
        "mapping": {"field_map_source": "explicit"},
        "cdc_event": ev, "cdc_reason": norm.get("reason"),
        "created_at": _now_iso(), "updated_at": _now_iso(),
    }
    JsonStore("ingest_candidates", project_id).insert(cand)
    audit_record(action="cdc_review_created", actor="cdc_consumer", project_id=project_id,
                 target_type="mcp_connection", target_id=conn.get("id"),
                 detail={"candidate_id": cand["id"], "lsn": ev.get("lsn"),
                         "op": ev.get("op"), "reason": norm.get("reason")})
    # V2-11 联动：事件字段结构作为实时漂移信号（检测通道②），失败不阻断主流程
    try:
        check_connection(project_id, conn.get("id") or "", source="cdc", actor="cdc_consumer")
    except Exception:  # noqa: BLE001
        logger.warning("CDC review 事件触发 schema 检测失败 conn=%s（不阻断）", conn.get("id"), exc_info=True)
    return cand


def _circuit_on_fail(circuit: dict[str, Any], settings) -> str | None:
    circuit["fail_count"] = int(circuit.get("fail_count", 0)) + 1
    if circuit.get("state") != "open" and circuit["fail_count"] >= settings.cdc_circuit_threshold:
        opens = int(circuit.get("open_count", 0))
        cooldown = settings.cdc_circuit_cooldown_seconds * (2 ** opens)
        circuit.update(state="open", opened_at=_now_iso(),
                       next_retry_at=time.time() + cooldown,
                       cooldown_seconds=cooldown, open_count=opens + 1)
        return "open"
    return None


def _circuit_on_success(circuit: dict[str, Any]) -> str | None:
    if circuit.get("state") in ("open", "half_open"):
        circuit.update(state="closed", fail_count=0, next_retry_at=None, opened_at=None)
        return "closed"
    circuit["fail_count"] = 0
    return None


def poll_connection(project_id: str, connection_id: str, actor: str = "scheduler") -> dict[str, Any]:
    """单轮 CDC 消费：门禁 → 熔断 → 分批拉取 → 幂等归化应用 → 统计/告警回写。

    后台循环与 REST 手动 poll 共用本入口。永不抛错：一切失败记入轮状态与统计。
    """
    from conn_stats import record_conn_stat

    settings = get_settings()
    store = _conn_store(project_id)
    conn = store.get(connection_id)
    if not conn:
        return {"status": "not_found", "connection_id": connection_id}
    cdc = dict(conn.get("cdc") or {})
    st: dict[str, Any] = {
        "connection_id": connection_id, "poll_at": _now_iso(), "status": "ok",
        "events": 0, "applied": 0, "skipped_dup": 0, "skipped_stale": 0, "review": 0,
        "batches": 0, "backlog": 0, "error": None, "lag_seconds": None,
    }
    if not cdc.get("enabled"):
        st["status"] = "disabled"
        return st
    # V2-11 门禁：键漂移停快照期间不消费（门禁异常视为停止）
    try:
        assert_connection_allowed(project_id, connection_id)
    except Exception as e:  # noqa: BLE001
        st["status"] = "stopped_drift"
        st["error"] = str(e)[:200]
        cdc["last_poll_at"] = st["poll_at"]
        store.update(connection_id, {"cdc": cdc})
        return st
    channel = effective_channel(conn)
    if channel == "none":
        st["status"] = "not_supported"
        cdc["supported"] = False
        cdc["status"] = "not_supported"
        cdc["last_poll_at"] = st["poll_at"]
        store.update(connection_id, {"cdc": cdc})
        return st

    now = time.time()
    circuit = dict(cdc.get("circuit") or {})
    circuit.setdefault("state", "closed")
    circuit.setdefault("fail_count", 0)
    # 熔断冷却到期 → 半开（放行一次探测）
    if circuit.get("state") == "open" and circuit.get("next_retry_at"):
        if now < circuit["next_retry_at"]:
            st["status"] = "circuit_open"
            st["next_retry_at"] = circuit.get("next_retry_at")
            cdc["last_poll_at"] = st["poll_at"]
            cdc["circuit"] = circuit
            _eval_stale(project_id, connection_id, cdc, now)
            store.update(connection_id, {"cdc": cdc})
            return st
        circuit["state"] = "half_open"

    last_lsn = int(cdc.get("last_lsn") or 0)
    t0 = time.time()
    try:
        batches = 0
        applied = dup = stale = review = 0
        max_event_ts: float | None = None
        probe_data: dict[str, Any] | None = None
        if channel == "probe":
            # auto：自动探测生效通道（源端 409 提示优先），首批评复探测数据
            channel, probe_data = _auto_probe(conn, cdc, last_lsn)
        while batches < settings.cdc_max_batches_per_poll:
            batches += 1
            data = probe_data if probe_data is not None else _fetch_events(conn, channel, last_lsn)
            probe_data = None
            events = data.get("events") or []
            st["events"] += len(events)
            for ev in events:
                try:
                    lsn = int(ev.get("lsn") or 0)
                except (TypeError, ValueError):
                    lsn = 0
                if lsn and lsn <= last_lsn:
                    # 幂等：重复投递（dup）/ 乱序旧事件（stale）→ 跳过，不重放
                    if lsn == last_lsn:
                        dup += 1
                    else:
                        stale += 1
                    continue
                last_lsn = max(last_lsn, lsn)
                try:
                    ev_ts = float(ev.get("ts") or 0)
                except (TypeError, ValueError):
                    ev_ts = 0.0
                if ev_ts:
                    max_event_ts = max(max_event_ts or 0.0, ev_ts)
                norm = normalize_event(project_id, conn, ev)
                if norm["risk"] == "review":
                    _create_review_candidate(project_id, conn, ev, norm)
                    review += 1
                else:
                    _apply_snapshot(project_id, conn, norm, ev, channel)
                    applied += 1
            st["applied"], st["skipped_dup"], st["skipped_stale"], st["review"] = applied, dup, stale, review
            if not events or len(events) < settings.cdc_max_batch:
                break
            if channel == "cursor":
                last_lsn = max(last_lsn, int(data.get("next_cursor") or last_lsn))
            # 积压限流：批间停顿，消化而不击穿
            if settings.cdc_batch_pause_ms > 0:
                time.sleep(settings.cdc_batch_pause_ms / 1000.0)
        st["batches"] = batches
        ms = int((time.time() - t0) * 1000)
        # 熔断成功复位
        transition = _circuit_on_success(circuit)
        if transition:
            audit_record(action="cdc_circuit", actor="cdc_consumer", project_id=project_id,
                         target_type="mcp_connection", target_id=connection_id,
                         detail={"transition": transition, "channel": channel})
        record_conn_stat(project_id, connection_id, "cdc", ok=True, ms=ms)
        st["status"] = "ok"
        # 滞后告警：事件 ts → 应用 超阈值（分钟级承诺，EP-12/TP-12-04）
        if max_event_ts:
            lag = now - max_event_ts
            if lag > settings.cdc_lag_alert_seconds:
                flags = cdc.setdefault("alert_flags", {})
                st["lag_seconds"] = round(lag, 1)
                if not flags.get("lag"):
                    flags["lag"] = True
                    _push_alert(cdc, "lag", f"事件滞后 {lag:.1f}s 超阈值 {settings.cdc_lag_alert_seconds}s")
                    audit_record(action="cdc_alert", actor="cdc_consumer", project_id=project_id,
                                 target_type="mcp_connection", target_id=connection_id,
                                 detail={"type": "lag", "lag_seconds": round(lag, 1)})
            else:
                flags = cdc.get("alert_flags") or {}
                if flags.get("lag"):
                    flags["lag"] = False
                    _push_alert(cdc, "lag_recovered", "滞后回落至阈值内")
        cdc.update({
            "channel": channel, "supported": True, "supported_channel": channel,
            "last_supported_channel": channel,
            "status": "running", "last_lsn": last_lsn, "last_poll_at": st["poll_at"],
            "last_success_at": _now_iso(), "last_circuit_ms": ms,
            "applied_total": int(cdc.get("applied_total", 0)) + applied,
            "skipped_dup_total": int(cdc.get("skipped_dup_total", 0)) + dup,
            "skipped_stale_total": int(cdc.get("skipped_stale_total", 0)) + stale,
            "review_total": int(cdc.get("review_total", 0)) + review,
            "errors_total": int(cdc.get("errors_total", 0)),
        })
        if st["events"]:
            cdc["last_event_lsn"] = last_lsn
            cdc["last_event_at"] = st["poll_at"]
            if max_event_ts:
                cdc["last_event_ts"] = max_event_ts
        if st["applied"] or st["review"]:
            audit_record(action="cdc_batch_applied", actor="cdc_consumer", project_id=project_id,
                         target_type="mcp_connection", target_id=connection_id,
                         detail={"channel": channel, "events": st["events"], "applied": applied,
                                 "skipped_dup": dup, "skipped_stale": stale, "review": review,
                                 "lsn_to": last_lsn})
        st["last_lsn"] = last_lsn
    except CdcNotSupported as e:
        # 通道边界：源系统无变更通道 → supported=False，降级周期拉取 + 联邦读（TP-12-05）
        st["status"] = "not_supported"
        st["error"] = str(e)
        cdc.update({"supported": False, "status": "not_supported", "last_poll_at": st["poll_at"],
                    "last_supported_channel": e.supported_channel,
                    "supported_channel": None,  # 409 为源端权威宣告：失效旧验证通道，auto 按最新提示重探测
                    "circuit": circuit})
        audit_record(action="cdc_not_supported", actor="cdc_consumer", project_id=project_id,
                     target_type="mcp_connection", target_id=connection_id,
                     detail={"channel": channel, "supported_channel": e.supported_channel})
        record_conn_stat(project_id, connection_id, "cdc", ok=False, error=str(e))
    except Exception as e:  # noqa: BLE001——轮询失败不抛出
        st["status"] = "error"
        st["error"] = f"{type(e).__name__}: {e}"[:300]
        cdc["errors_total"] = int(cdc.get("errors_total", 0)) + 1
        cdc["last_poll_at"] = st["poll_at"]
        transition = _circuit_on_fail(circuit, settings)
        if transition == "open":
            _push_alert(cdc, "circuit_open", f"连续失败 {circuit['fail_count']} 次，熔断冷却 {circuit['cooldown_seconds']}s")
            audit_record(action="cdc_circuit", actor="cdc_consumer", project_id=project_id,
                         target_type="mcp_connection", target_id=connection_id,
                         detail={"transition": "open", "fail_count": circuit["fail_count"],
                                 "cooldown_seconds": circuit["cooldown_seconds"],
                                 "error": st["error"]})
            st["status"] = "circuit_open"
        record_conn_stat(project_id, connection_id, "cdc", ok=False, error=st["error"])
        logger.warning("CDC 轮询失败 conn=%s channel=%s: %s", connection_id, channel, st["error"])
    # 断流（stalled）告警：ok/失败两路统一判定（持续无成功消费超阈值，0→1 转换才告警）
    _eval_stale(project_id, connection_id, cdc, time.time())
    cdc["circuit"] = circuit
    store.update(connection_id, {"cdc": cdc})
    return st


def _eval_stale(project_id: str, connection_id: str, cdc: dict[str, Any], now: float) -> None:
    """断流（stalled）判定：启用且支持的连接持续无成功消费超 cdc_stale_seconds → 告警。

    在 ok/失败/熔断冷却各路径统一调用（状态存于传入的 cdc dict，由调用方落盘）。
    0→1 转换才告警（防刷屏）；恢复（成功消费）时回落标记并记恢复事件。
    """
    settings = get_settings()
    last_ok = _parse_ts(cdc.get("last_success_at")) or _parse_ts(cdc.get("enabled_at"))
    if last_ok is None:
        return
    idle = now - last_ok
    flags = cdc.setdefault("alert_flags", {})
    if idle > settings.cdc_stale_seconds:
        if not flags.get("stalled"):
            flags["stalled"] = True
            _push_alert(cdc, "stalled", f"超过 {idle:.0f}s 无成功消费（断流/熔断中）")
            audit_record(action="cdc_alert", actor="cdc_consumer", project_id=project_id,
                         target_type="mcp_connection", target_id=connection_id,
                         detail={"type": "stalled", "idle_seconds": int(idle)})
    else:
        if flags.get("stalled"):
            flags["stalled"] = False
            _push_alert(cdc, "stalled_recovered", "消费恢复，断流解除")


def _push_alert(cdc: dict[str, Any], alert_type: str, detail: str) -> None:
    alerts = cdc.get("alerts") if isinstance(cdc.get("alerts"), list) else []
    alerts.append({"ts": _now_iso(), "type": alert_type, "detail": detail})
    cdc["alerts"] = alerts[-_ALERT_RING:]


def fallback_pull(project_id: str, connection_id: str, actor: str = "scheduler") -> dict[str, Any]:
    """CDC 未支持连接的降级新鲜度：周期全量拉取（MCP read 通道）→ 低风险归化更新关系快照。

    频率由 cdc_fallback_pull_seconds 控制；门禁（V2-11 停快照）期间跳过。
    """
    from conn_stats import record_conn_stat

    settings = get_settings()
    store = _conn_store(project_id)
    conn = store.get(connection_id)
    if not conn:
        return {"status": "not_found"}
    cdc = dict(conn.get("cdc") or {})
    st: dict[str, Any] = {"connection_id": connection_id, "status": "ok", "pulled": 0, "applied": 0}
    if not cdc.get("enabled") or cdc.get("supported") is not False:
        st["status"] = "not_degraded"
        return st
    last_pull = _parse_ts(cdc.get("last_fallback_pull_at"))
    if last_pull and (time.time() - last_pull) < settings.cdc_fallback_pull_seconds:
        st["status"] = "not_due"
        return st
    try:
        assert_connection_allowed(project_id, connection_id)
    except Exception as e:  # noqa: BLE001
        st["status"] = "stopped_drift"
        st["error"] = str(e)[:200]
        return st
    try:
        field_map, _ = resolve_field_map(project_id, conn)
        maps = conn.get("maps_to_ontology") or {}
        tool = maps.get("read_tool") or "read"
        eff = {**conn, "maps_to_ontology": {**maps, "field_map": field_map or {}}}
        if not field_map:
            raise CdcFetchError("field_map 为空（未绑定字典词条或显式映射）")
        t0 = time.time()
        raw = McpClient().read(eff, tool, {})
        records = raw.get("records") or []
        st["pulled"] = len(records)
        obj = _object_of(project_id, conn)
        key_attr = (obj or {}).get("stable_key")
        key_src = key_source_field(project_id, conn, field_map)
        relation_ids = {r.get("id") for r in (obj or {}).get("relations", []) if r.get("id")}
        for rec in records:
            if not isinstance(rec, dict):
                continue
            key = rec.get(key_src) if key_src else None
            if key is None:
                continue
            rel = {a: rec[s] for s, a in field_map.items()
                   if a in relation_ids and rec.get(s) is not None}
            norm = {"op": "update", "key_attr": key_attr, "key": key,
                    "relation_attrs": rel, "risk": "low", "reason": None}
            ev = {"lsn": 0, "ts": time.time(), "op": "update"}
            _apply_snapshot(project_id, conn, norm, ev, "fallback_pull")
            st["applied"] += 1
        ms = int((time.time() - t0) * 1000)
        cdc["last_fallback_pull_at"] = _now_iso()
        cdc["last_success_at"] = _now_iso()
        cdc["status"] = "degraded_fallback"
        store.update(connection_id, {"cdc": cdc})
        audit_record(action="cdc_fallback_pull", actor=actor, project_id=project_id,
                     target_type="mcp_connection", target_id=connection_id,
                     detail={"pulled": st["pulled"], "applied": st["applied"], "ms": ms})
        record_conn_stat(project_id, connection_id, "read", ok=True, ms=ms)
    except Exception as e:  # noqa: BLE001——降级拉取失败不抛出
        st["status"] = "error"
        st["error"] = f"{type(e).__name__}: {e}"[:300]
        audit_record(action="cdc_fallback_pull", actor=actor, project_id=project_id,
                     target_type="mcp_connection", target_id=connection_id,
                     detail={"status": "error", "error": st["error"]})
        record_conn_stat(project_id, connection_id, "read", ok=False, error=st["error"])
    return st


def _cdc_loop() -> None:
    """CDC 消费后台循环（daemon）：遍历 项目×http 连接，enabled 者消费/降级拉取。"""
    logger.info("M3-D CDC 消费循环启动：interval=%ss", get_settings().cdc_poll_interval_seconds)
    while True:
        try:
            for proj in JsonStore("projects").all():
                pid = proj.get("id")
                if not pid:
                    continue
                for conn in _conn_store(pid).all():
                    if conn.get("transport") not in ("http", "streamable_http"):
                        continue
                    cdc = conn.get("cdc") or {}
                    if not cdc.get("enabled"):
                        continue
                    cid = conn.get("id")
                    try:
                        if cdc.get("supported") is False:
                            fallback_pull(pid, cid)
                        else:
                            poll_connection(pid, cid)
                    except Exception:  # noqa: BLE001
                        logger.warning("CDC 循环处理失败 conn=%s project=%s", cid, pid, exc_info=True)
        except Exception:  # noqa: BLE001
            logger.exception("M3-D CDC 循环 sweep 失败")
        time.sleep(max(1, get_settings().cdc_poll_interval_seconds))


def cdc_status_of(conn: dict[str, Any]) -> str:
    """连接 CDC 状态（健康视图徽章）：disabled/not_supported/stopped_drift/circuit_open/running。"""
    cdc = conn.get("cdc") or {}
    if not cdc.get("enabled"):
        return "disabled"
    if conn.get("snapshot_auto_update") == "stopped":
        return "stopped_drift"
    if cdc.get("supported") is False:
        return "not_supported"
    circuit = cdc.get("circuit") or {}
    if circuit.get("state") == "open":
        return "circuit_open"
    return "running"


def summarize_cdc(cdc: dict[str, Any] | None) -> dict[str, Any]:
    """连接健康视图 CDC 块：通道/游标/最近事件与成功/熔断/积压口径/最近告警。"""
    cdc = cdc or {}
    return {
        "enabled": bool(cdc.get("enabled")),
        "channel": cdc.get("channel"),
        "supported": cdc.get("supported"),
        "supported_channel": cdc.get("supported_channel"),
        "status": cdc.get("status"),
        "last_lsn": cdc.get("last_lsn"),
        "last_event_at": cdc.get("last_event_at"),
        "last_event_lsn": cdc.get("last_event_lsn"),
        "last_success_at": cdc.get("last_success_at"),
        "last_poll_at": cdc.get("last_poll_at"),
        "last_fallback_pull_at": cdc.get("last_fallback_pull_at"),
        "circuit": cdc.get("circuit") or {"state": "closed", "fail_count": 0},
        "applied_total": cdc.get("applied_total", 0),
        "skipped_dup_total": cdc.get("skipped_dup_total", 0),
        "skipped_stale_total": cdc.get("skipped_stale_total", 0),
        "review_total": cdc.get("review_total", 0),
        "errors_total": cdc.get("errors_total", 0),
        "alerts": (cdc.get("alerts") or [])[-10:],
    }
