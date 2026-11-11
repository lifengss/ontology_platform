"""V2-11（M3-A）：schema 漂移检测与分级处置。

检测通道（全部复用同一管道 ``check_connection``，仅 source 不同）：
- periodic:   周期 schema 探测（只拉字段结构、不拉数据值；TP-11-06）
- ingest:     快照更新（入库读取）时的结构比对（TP-11-07；M3-D CDC 事件沿用本通道）
- federation: 联邦读异常反馈——缺失字段/类型不一致仅作漂移信号，不阻断查询（TP-11-08）
- manual:     连接健康视图手动触发（复用同一管道，检查后距上次时间清零；TP-11-09）

分级（对照 field_map 与对象业务主键 stable_key）：
- key 漂移（stable_key 对应源字段缺失/类型变化）→ 停该连接快照自动更新 + 立即告警（含明细列表）
- 非键属性漂移（映射字段缺失/类型变化）→ 标记该属性「漂移中」，联邦读返回显式标记
  （不静默放行/不当作有效旧值）；快照更新可继续 + 带告警（TP-11-02）
- 未映射新字段（源端出现映射外新字段）→ 不阻断，仅提示补映射（TP-11-03）

恢复：仅人工确认（resolve）清除漂移标记/恢复快照自动更新，不允许自动恢复（TP-11-05）。
门禁：key 漂移停止期间，该连接的快照更新（入库读取/CDC）被拒（TP-11-01/04）。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from config import get_settings
from core.responses import ApiError
from core.storage import JsonStore
from dictionary import resolve_field_map
from logging_setup import get_logger
from mcp_client import McpClient

logger = get_logger("schema_drift")
_client = McpClient()

_NUM_TYPES = {"number", "float", "integer", "double", "int", "long"}
_DATE_TYPES = {"date", "datetime", "timestamp"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _drift_store(project_id: str) -> JsonStore:
    return JsonStore("schema_drifts", project_id=project_id)


def _conn_store(project_id: str) -> JsonStore:
    return JsonStore("mcp_connections", project_id=project_id)


def _type_family(t: str | None) -> str:
    t = (t or "string").strip().lower()
    if t in _NUM_TYPES:
        return "number"
    if t in _DATE_TYPES:
        return "date"
    if t == "boolean":
        return "boolean"
    return "string"


def _connection_id_of(connection: dict[str, Any]) -> str:
    return connection.get("id") or connection.get("connection_id") or ""


def _property_types(project_id: str, ontology_id: str | None) -> dict[str, str]:
    """ontology 属性 id → data_type（类型漂移比对的期望侧）。"""
    if not ontology_id:
        return {}
    ont = JsonStore("ontologies", project_id=project_id).get(ontology_id)
    out: dict[str, str] = {}
    for o in (ont or {}).get("objects", []):
        for a in o.get("attributes", []):
            if a.get("id"):
                out[a["id"]] = a.get("data_type") or "string"
    return out


def probe_schema(connection: dict[str, Any]) -> dict[str, Any]:
    """schema 探测（只拉结构、不拉值）：委托 mcp_client，并归一 fields 列表。"""
    probed = _client.probe_schema(connection)
    fields = probed.get("fields") or []
    norm = []
    for f in fields:
        if isinstance(f, dict) and f.get("name"):
            norm.append({"name": str(f["name"]), "type": str(f.get("type") or "string")})
        elif isinstance(f, str):
            norm.append({"name": f, "type": "string"})
    return {
        "fields": norm,
        "key_field": probed.get("key_field"),
        "schema_version": probed.get("schema_version"),
    }


def key_source_field(project_id: str, connection: dict[str, Any], field_map: dict[str, str]) -> str | None:
    """对象业务主键（stable_key）对应的源端字段名；无法确定时返回 None。"""
    if not field_map:
        return None
    maps = connection.get("maps_to_ontology") or {}
    ontology_id = maps.get("ontology_id")
    object_type = maps.get("object_type")
    if not ontology_id or not object_type:
        return None
    ont = JsonStore("ontologies", project_id=project_id).get(ontology_id)
    obj = next((o for o in (ont or {}).get("objects", []) if o.get("id") == object_type), None)
    if not obj:
        return None
    stable_key = obj.get("stable_key")
    if not stable_key:
        return None
    for src, prop in field_map.items():
        if prop == stable_key:
            return src
    return None


def compare_schema(
    probed: dict[str, Any],
    field_map: dict[str, str],
    key_src: str | None,
    prop_types: dict[str, str],
) -> list[dict[str, Any]]:
    """探测到的源端结构 vs 映射期望 → 分级漂移项列表。

    kinds:
      key_missing / key_type_changed    severity=key
      attr_missing / attr_type_changed  severity=attribute
      new_unmapped                      severity=unmapped
    说明：「键属性重命名」表现为 旧键缺失(key_missing)＋新字段未映射(new_unmapped) 组合。
    """
    items: list[dict[str, Any]] = []
    probed_fields = {f["name"]: f.get("type") for f in probed.get("fields", [])}
    for src, prop in field_map.items():
        if src not in probed_fields:
            if key_src and src == key_src:
                items.append({"field": src, "property": prop, "kind": "key_missing",
                              "severity": "key", "expected": "present", "actual": "missing"})
            else:
                items.append({"field": src, "property": prop, "kind": "attr_missing",
                              "severity": "attribute", "expected": "present", "actual": "missing"})
            continue
        expected = prop_types.get(prop)
        if expected and _type_family(probed_fields[src]) != _type_family(expected):
            kind = "key_type_changed" if (key_src and src == key_src) else "attr_type_changed"
            items.append({"field": src, "property": prop, "kind": kind,
                          "severity": "key" if kind.startswith("key_") else "attribute",
                          "expected": str(expected), "actual": str(probed_fields[src])})
    mapped = set(field_map.keys())
    for name in probed_fields:
        if name not in mapped and name != (probed.get("key_field") or ""):
            items.append({"field": name, "property": None, "kind": "new_unmapped",
                          "severity": "unmapped", "expected": None, "actual": "present"})
    return items


def _severity_of(items: list[dict[str, Any]]) -> str | None:
    sevs = {i.get("severity") for i in items}
    if "key" in sevs:
        return "key"
    if "attribute" in sevs:
        return "attribute"
    if "unmapped" in sevs:
        return "unmapped"
    return None


def _upsert_open(
    project_id: str,
    connection: dict[str, Any],
    items: list[dict[str, Any]],
    source: str,
) -> tuple[dict[str, Any], bool]:
    """把新漂移项并入该连接唯一 open 记录（无则新建）；返回 (rec, created)。"""
    conn_id = _connection_id_of(connection)
    dstore = _drift_store(project_id)
    open_rec = next((d for d in dstore.all() if d.get("connection_id") == conn_id and d.get("status") == "open"), None)
    if open_rec:
        existing = {(i.get("field"), i.get("kind")) for i in open_rec.get("items", [])}
        added = [i for i in items if (i["field"], i["kind"]) not in existing]
        if added:
            dstore.update(open_rec["id"], {
                "items": list(open_rec.get("items", [])) + added,
                "detected_at": _now_iso(),
                "last_detected_source": source,
            })
            open_rec = dstore.get(open_rec["id"]) or open_rec
        return open_rec, False
    rec = {
        "id": uuid.uuid4().hex,
        "connection_id": conn_id,
        "detected_at": _now_iso(),
        "source": source,
        "last_detected_source": source,
        "items": items,
        "status": "open",
        "resolved_at": None,
        "resolved_by": None,
    }
    dstore.insert(rec)
    return rec, True


def _alert(project_id: str, connection: dict[str, Any], rec: dict[str, Any], source: str, actor: str) -> None:
    """漂移告警：审计留痕（M2-D 统一审计）＋分级日志。明细列表随 detail 携带。"""
    import audit

    items = rec.get("items", [])
    severity = _severity_of(items)
    if severity is None:
        return
    audit.record(
        action="schema_drift_detected",
        actor=actor,
        project_id=project_id,
        target_type="mcp_connection",
        target_id=_connection_id_of(connection),
        detail={
            "source": source,
            "severity": severity,
            "stopped": severity == "key",
            "items": items,
        },
    )
    if severity == "key":
        logger.error("M3-A schema 漂移（键属性）project=%s connection=%s → 停快照自动更新: %s",
                     project_id, connection.get("display_name"),
                     [i["field"] for i in items if i.get("severity") == "key"])
    elif severity == "attribute":
        logger.warning("M3-A schema 漂移（非键属性）project=%s connection=%s: %s",
                       project_id, connection.get("display_name"),
                       [i["field"] for i in items if i.get("severity") == "attribute"])
    else:
        logger.info("M3-A schema 漂移（未映射新字段，仅提示）project=%s connection=%s: %s",
                    project_id, connection.get("display_name"),
                    [i["field"] for i in items if i.get("severity") == "unmapped"])


def check_connection(project_id: str, connection_id: str, source: str = "manual", actor: str = "-") -> dict[str, Any]:
    """V2-11 统一检测管道：探测 → 比对 → 分级 → 落记录 → 处置（停更/告警）→ 健康字段更新。"""
    import audit

    cstore = _conn_store(project_id)
    conn = cstore.get(connection_id)
    if not conn:
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    t0 = time.perf_counter()
    try:
        probed = probe_schema(conn)
    except ApiError as e:
        ms = int((time.perf_counter() - t0) * 1000)
        cstore.update(connection_id, {
            "last_schema_check_at": _now_iso(),
            "last_schema_check_ok": False,
            "last_schema_check_ms": ms,
            "last_schema_check_error": str(e),
        })
        # EP-11-01：探测失败 ≠ 漂移（源不可达/鉴权失败不得误报为结构漂移）
        logger.warning("schema 探测失败（不判定漂移）conn=%s source=%s: %s", connection_id, source, e)
        if source == "manual":
            audit.record(action="schema_check_manual", actor=actor, project_id=project_id,
                         target_type="mcp_connection", target_id=connection_id,
                         detail={"ok": False, "source": source, "error": str(e)})
        return {"status": "probe_failed", "connection_id": connection_id,
                "error": str(e), "severity": None, "items": []}
    ms = int((time.perf_counter() - t0) * 1000)

    field_map, _ = resolve_field_map(project_id, conn)
    key_src = key_source_field(project_id, conn, field_map)
    prop_types = _property_types(project_id, (conn.get("maps_to_ontology") or {}).get("ontology_id"))
    items = compare_schema(probed, field_map, key_src, prop_types) if field_map else []

    severity = None
    if items:
        rec, _created = _upsert_open(project_id, conn, items, source=source)
        severity = _severity_of(rec.get("items", []))
        if severity == "key" and conn.get("snapshot_auto_update") != "stopped":
            cstore.update(connection_id, {"snapshot_auto_update": "stopped"})
        _alert(project_id, conn, rec, source, actor)

    cstore.update(connection_id, {
        "last_schema_check_at": _now_iso(),
        "last_schema_check_ok": True,
        "last_schema_check_ms": ms,
        "last_schema_check_error": None,
        "last_schema_version": probed.get("schema_version"),
    })
    if source == "manual":
        audit.record(action="schema_check_manual", actor=actor, project_id=project_id,
                     target_type="mcp_connection", target_id=connection_id,
                     detail={"ok": True, "source": source, "severity": severity, "item_count": len(items)})
    logger.info("M3-A schema check conn=%s source=%s severity=%s new_items=%d ms=%d",
                connection_id, source, severity, len(items), ms)
    cur = cstore.get(connection_id) or {}
    return {"status": "ok", "connection_id": connection_id, "severity": severity,
            "items": items, "snapshot_auto_update": cur.get("snapshot_auto_update") or "active"}


def resolve_drift(project_id: str, connection_id: str, drift_id: str, actor: str) -> dict[str, Any]:
    """V2-11 恢复：人工确认（唯一恢复路径，不允许自动恢复）。

    确认一条漂移后：该记录置 resolved；若该连接已无 open 漂移 → 恢复快照自动更新。
    """
    import audit

    dstore = _drift_store(project_id)
    rec = dstore.get(drift_id)
    if not rec or rec.get("connection_id") != connection_id:
        raise ApiError("NOT_FOUND", "漂移记录不存在或不属于该连接", http_status=404)
    if rec.get("status") == "resolved":
        raise ApiError("ALREADY_RESOLVED", "该漂移已人工确认恢复，不可重复操作", http_status=409)
    dstore.update(drift_id, {"status": "resolved", "resolved_at": _now_iso(), "resolved_by": actor})
    remaining = [d for d in dstore.all() if d.get("connection_id") == connection_id and d.get("status") == "open"]
    resumed = False
    if not remaining:
        cstore = _conn_store(project_id)
        cur = cstore.get(connection_id)
        if cur and cur.get("snapshot_auto_update") == "stopped":
            cstore.update(connection_id, {"snapshot_auto_update": "active"})
            resumed = True
    audit.record(action="schema_drift_resolved", actor=actor, project_id=project_id,
                 target_type="mcp_connection", target_id=connection_id,
                 detail={"drift_id": drift_id, "resumed": resumed, "remaining_open": len(remaining)})
    logger.info("M3-A schema 漂移人工确认恢复 conn=%s drift=%s resumed=%s actor=%s",
                connection_id, drift_id, resumed, actor)
    return {"drift_id": drift_id, "status": "resolved", "resolved_by": actor,
            "resumed_snapshot_auto_update": resumed, "remaining_open": len(remaining)}


def assert_connection_allowed(project_id: str, connection_id: str) -> None:
    """V2-11 门禁：key 漂移停止期间拒绝该连接的快照更新（入库读取/CDC）。"""
    conn = _conn_store(project_id).get(connection_id)
    if not conn or conn.get("snapshot_auto_update") != "stopped":
        return
    open_recs = [d for d in _drift_store(project_id).all()
                 if d.get("connection_id") == connection_id and d.get("status") == "open"]
    key_fields = sorted({i.get("field") for d in open_recs for i in d.get("items", []) if i.get("severity") == "key"})
    raise ApiError(
        "SCHEMA_DRIFT_STOPPED",
        f"连接「{conn.get('display_name') or connection_id}」存在键属性 schema 漂移（键字段 {key_fields or '未知'}），"
        f"快照自动更新已停止；请修正映射后人工确认恢复："
        f"POST /api/mcp/connections/{connection_id}/drifts/<drift_id>/resolve",
        http_status=409,
    )


def open_drifts(project_id: str, connection_id: str) -> list[dict[str, Any]]:
    return [d for d in _drift_store(project_id).all()
            if d.get("connection_id") == connection_id and d.get("status") == "open"]


def open_attribute_marks(project_id: str, connection_id: str) -> dict[str, dict[str, Any]]:
    """该连接 open 的非键属性漂移 {源字段: item}——联邦读显式标记用（TP-11-02/EP-11-03）。"""
    out: dict[str, dict[str, Any]] = {}
    for d in open_drifts(project_id, connection_id):
        for i in d.get("items", []):
            if i.get("severity") == "attribute" and i.get("field"):
                out.setdefault(i["field"], i)
    return out


def field_feedback(
    project_id: str,
    connection: dict[str, Any],
    records: list[dict[str, Any]],
    actor: str = "-",
    source: str = "ingest",
) -> None:
    """V2-11 TP-11-07/08：快照更新时结构比对 / 联邦读异常反馈。

    仅在 records 非空时触发（源端空数据无法判定字段缺失）。
    只检测「缺失」类漂移（键缺失→key；映射非键缺失→attribute）；类型比对走探测通道。
    本函数永不抛错、不阻断当前读/查询（下次快照更新由门禁拦截）。
    """
    if not records:
        return
    try:
        field_map, _ = resolve_field_map(project_id, connection)
        if not field_map:
            return
        present: set[str] = set()
        for r in records:
            if isinstance(r, dict):
                present.update(r.keys())
        missing = [src for src in field_map if src not in present]
        if not missing:
            return
        key_src = key_source_field(project_id, connection, field_map)
        items = []
        for src in missing:
            if key_src and src == key_src:
                items.append({"field": src, "property": field_map[src], "kind": "key_missing",
                              "severity": "key", "expected": "present", "actual": "missing"})
            else:
                items.append({"field": src, "property": field_map[src], "kind": "attr_missing",
                              "severity": "attribute", "expected": "present", "actual": "missing"})
        rec, _created = _upsert_open(project_id, connection, items, source=source)
        conn = _conn_store(project_id).get(_connection_id_of(connection)) or connection
        if _severity_of(rec.get("items", [])) == "key" and conn.get("snapshot_auto_update") != "stopped":
            _conn_store(project_id).update(_connection_id_of(connection), {"snapshot_auto_update": "stopped"})
        _alert(project_id, conn, rec, source, actor)
    except Exception:  # noqa: BLE001——反馈信号永不阻断查询/入库主流程
        logger.exception("M3-A field_feedback 失败（不阻断主流程）conn=%s source=%s",
                         _connection_id_of(connection), source)


def health_info(project_id: str, connection_id: str) -> dict[str, Any]:
    """V2-11 连接健康视图（schema 检查部分）：上次检查时间/距上次(秒)/周期/超阈值标记。

    成功率/熔断/ CDC 状态 随 M3-D（V2-12）补齐，先以占位返回。
    """
    settings = get_settings()
    conn = _conn_store(project_id).get(connection_id)
    if not conn:
        raise ApiError("NOT_FOUND", "连接不存在", http_status=404)
    interval = max(1, settings.schema_drift_probe_interval_seconds)
    threshold = int(settings.schema_drift_stale_multiplier * interval)
    last = conn.get("last_schema_check_at")
    age_seconds: int | None = None
    stale = False
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            age_seconds = max(0, int((datetime.now(timezone.utc) - last_dt).total_seconds()))
            stale = age_seconds > threshold
        except ValueError:
            age_seconds = None
    return {
        "connection_id": connection_id,
        "last_schema_check_at": last,
        "age_seconds": age_seconds,
        "never_checked": last is None,
        "probe_interval_seconds": interval,
        "stale_threshold_seconds": threshold,
        "stale": stale,
        "last_schema_check_ok": conn.get("last_schema_check_ok"),
        "last_schema_check_ms": conn.get("last_schema_check_ms"),
        "last_schema_check_error": conn.get("last_schema_check_error"),
        "last_schema_version": conn.get("last_schema_version"),
        "snapshot_auto_update": conn.get("snapshot_auto_update") or "active",
        "open_drifts": len(open_drifts(project_id, connection_id)),
        "cdc_status": "planned_m3d",
    }


def _probe_loop() -> None:
    """周期 schema 探测后台循环：遍历 项目×http 连接，复用同一管道（source=periodic）。"""
    settings = get_settings()
    logger.info("M3-A schema 漂移周期探测启动：interval=%ss", settings.schema_drift_probe_interval_seconds)
    while True:
        try:
            for proj in JsonStore("projects").all():
                pid = proj.get("id")
                if not pid:
                    continue
                for conn in _conn_store(pid).all():
                    if conn.get("transport") not in ("http", "streamable_http"):
                        continue
                    try:
                        check_connection(pid, conn["id"], source="periodic", actor="scheduler")
                    except Exception:  # noqa: BLE001
                        logger.warning("周期 schema 探测失败 conn=%s project=%s", conn.get("id"), pid, exc_info=True)
        except Exception:  # noqa: BLE001
            logger.exception("M3-A 周期探测 sweep 失败")
        time.sleep(max(1, get_settings().schema_drift_probe_interval_seconds))
