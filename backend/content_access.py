"""V2-02 实例数据模型——内容访问路由（M4-A①，方案 D3）。

源连接型实例（external_mcp / cdc）**非键内容值永不落地**（INV-1）：

- 入库（防新增）：external_mcp 候选审批通过时，SHACL 门禁仍用候选全量值做边界校验，
  落库前剥离非键内容值，只持久化关系快照（stable_key 值 + 关系边 + ``_source`` 治理元数据）；
  CDC（M3-D）入库本就只写键 + 关系边。
- 读取（联邦读）：任何非键属性值读取以**访问者身份**（鉴权启用，V2-14i 凭据映射，
  缺失即 403 不回落匿名）或**治理身份**（MCP 通道 / 鉴权关闭；连接自带凭据，与
  M3-C ``_governance_sample`` 同口径）按键值回源，复用 ``identity_federation.federated_read``
  与 ``mcp_client.McpClient``（V2-01 联邦护栏：鉴权/超时/熔断/重试）。
- 回退（D3 回退底）：源端不可达时——迁移前存量（本地仍有值）→ ``legacy_local``（stale）；
  迁移后存量（值在 ``fallback_content`` 回退字段）→ ``fallback``（stale，标 as_of）；
  新实例 → ``unavailable``（不造值）。

INV-1 负向断言口径（scripts/verify_inv1_source_channel.py）：
① 源连接型记录 ``instance`` 字段键 ⊆ {stable_key 属性, 关系 id, ``_source``}；
② 主命名图中这些实例主体的三元组 ⊆ {rdf:type, rdfs:label, 关系边}。
回退图 ``urn:ontology:{oid}/fallback_snapshot`` 与 ``fallback_content`` 字段不计入（明确标 stale）。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from config import get_settings
from core.responses import ApiError
from core.storage import JsonStore
from logging_setup import get_logger

logger = get_logger("content_access")

CHANNEL_UPLOAD = "upload"
CHANNEL_EXTERNAL_MCP = "external_mcp"
CHANNEL_CDC = "cdc"
FEDERATED_CHANNELS = (CHANNEL_EXTERNAL_MCP, CHANNEL_CDC)

_MCP_CONNS = "mcp_connections"
_ONTOLOGIES = "ontologies"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 渠道判定
# ---------------------------------------------------------------------------

def _candidates_map(project_id: str) -> dict[str, dict[str, Any]]:
    """candidate_id → 候选记录（一次读全文件，批量判定用）。"""
    try:
        rows = JsonStore("ingest_candidates", project_id).all()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        cid = r.get("id")
        if cid:
            out[cid] = r
    return out


def _connection_ids_set(project_id: str) -> set[str]:
    """项目内真实 MCP 连接 id 集合（与 data_quality._valid_connection_ids 同口径）。"""
    try:
        return {c.get("id") for c in JsonStore(_MCP_CONNS, project_id).all() if c.get("id")}
    except Exception:  # noqa: BLE001
        return set()


def _channel_of_with_map(
    project_id: str,
    rec: dict[str, Any],
    cands: Optional[dict[str, dict[str, Any]]] = None,
    conn_ids: Optional[set[str]] = None,
) -> str:
    """实例渠道判定（按优先级）：

    ① 显式 ``channel`` 字段（M4-A 新写入：external_mcp / cdc / upload）；
    ② ``_source.kind=cdc``（CDC 直写/确认候选，含存量数据）；
    ③ ``_source.connection_id`` ∈ 真实连接集合——源连接型的**权威口径**
      （ingestion.apply_field_map 对上传/MCP 两条通道恒写 kind="field_map"，
      上传实例的 connection_id=文件名；与 data_quality._is_source_conn 一致）；
    ④ 候选 source_type=external_mcp（存量兜底，候选记录仍在时）；
    ⑤ upload。
    """
    ch = rec.get("channel")
    if ch in (CHANNEL_UPLOAD, CHANNEL_EXTERNAL_MCP, CHANNEL_CDC):
        return ch
    inst = rec.get("instance") or {}
    src = inst.get("_source") or {}
    if src.get("kind") == "cdc":
        return CHANNEL_CDC
    src_cid = src.get("connection_id")
    if isinstance(src_cid, str) and src_cid:
        if conn_ids is None:
            conn_ids = _connection_ids_set(project_id)
        if src_cid in conn_ids:
            return CHANNEL_EXTERNAL_MCP
    cid = rec.get("candidate_id")
    if cid:
        cand = (cands or {}).get(cid)
        if cand is None:
            try:
                cand = JsonStore("ingest_candidates", project_id).get(cid)
            except Exception:  # noqa: BLE001
                cand = None
        if cand and cand.get("source_type") == CHANNEL_EXTERNAL_MCP:
            return CHANNEL_EXTERNAL_MCP
    return CHANNEL_UPLOAD


def channel_of(project_id: str, rec: dict[str, Any]) -> str:
    """单条实例的渠道（upload / external_mcp / cdc）。"""
    return _channel_of_with_map(project_id, rec)


def is_federated(project_id: str, rec: dict[str, Any]) -> bool:
    """是否源连接型（内容值不落本地、读取走联邦读）。"""
    return _channel_of_with_map(project_id, rec) in FEDERATED_CHANNELS


def annotate_rows(project_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """列表读取路径的轻量标注：联邦渠道行加 ``content_mode=federated``，不做逐行回源。

    返回新记录（不修改原对象）；上传型标注 ``content_mode=local``（附加字段，向后兼容）。
    """
    if not rows:
        return rows
    cands = _candidates_map(project_id)
    conn_ids = _connection_ids_set(project_id)
    out: list[dict[str, Any]] = []
    for rec in rows:
        ch = _channel_of_with_map(project_id, rec, cands, conn_ids)
        r = dict(rec)
        r["content_mode"] = "federated" if ch in FEDERATED_CHANNELS else "local"
        out.append(r)
    return out


def connection_id_of(rec: dict[str, Any]) -> str | None:
    """实例的来源连接 id（``_source.connection_id``）。"""
    inst = rec.get("instance") or {}
    src = inst.get("_source") or {}
    cid = src.get("connection_id")
    return cid if isinstance(cid, str) and cid else None


# ---------------------------------------------------------------------------
# 对象定义与关系快照
# ---------------------------------------------------------------------------

def _ontology_of(project_id: str, rec: dict[str, Any]) -> Optional[dict[str, Any]]:
    oid = rec.get("ontology_id")
    if not oid:
        return None
    try:
        return JsonStore(_ONTOLOGIES, project_id).get(oid)
    except Exception:  # noqa: BLE001
        return None


def object_def_for(project_id: str, rec: dict[str, Any]) -> Optional[dict[str, Any]]:
    """实例对应对象定义（stable_key / attributes / relations）。"""
    ont = _ontology_of(project_id, rec)
    if not ont:
        return None
    ot = rec.get("object_type")
    for o in ont.get("objects") or []:
        if isinstance(o, dict) and o.get("id") == ot:
            return o
    return None


def _key_and_relation_attrs(obj: dict[str, Any]) -> tuple[str, list[str]]:
    """(stable_key 属性 id, 关系 id 列表)。关系在对象定义的 ``relations`` 中（独立于 attributes）。"""
    key = obj.get("stable_key") or ""
    rels = [r.get("id") for r in (obj.get("relations") or []) if isinstance(r, dict) and r.get("id")]
    return key, rels


# 实例标识字段（治理身份/定位用，非业务内容值，随关系快照保留）
_IDENTITY_KEYS = ("id", "node_id")


def keep_keys_for(project_id: str, rec: dict[str, Any]) -> set[str]:
    """关系快照保留键域：标识字段 + stable_key 属性 + 关系 id + ``_source``。"""
    keep = set(_IDENTITY_KEYS) | {"_source"}
    obj = object_def_for(project_id, rec)
    if not obj:
        return keep
    key, rels = _key_and_relation_attrs(obj)
    if key:
        keep.add(key)
    keep.update(rels)
    return keep


def strip_content_for_storage(project_id: str, rec: dict[str, Any]) -> dict[str, Any]:
    """入库剥离：返回只含 键值 + 关系边 + 标识 + ``_source`` 的 instance dict（源连接型用）。

    无法解析对象定义时保守返回原值（宁可多落，不破坏入库）——仅上传型会走到该分支。
    """
    inst = rec.get("instance") or {}
    if not object_def_for(project_id, rec):
        return dict(inst)
    keep = keep_keys_for(project_id, rec)
    return {k: v for k, v in inst.items() if k in keep}


def relation_snapshot(project_id: str, rec: dict[str, Any]) -> dict[str, Any]:
    """读取路径的关系快照视图（本地可显示的键/关系/治理元数据）。"""
    return strip_content_for_storage(project_id, rec)


# ---------------------------------------------------------------------------
# 联邦读（内容值按需回源）
# ---------------------------------------------------------------------------

def _load_connection(project_id: str, conn_id: str) -> Optional[dict[str, Any]]:
    try:
        return JsonStore(_MCP_CONNS, project_id).get(conn_id)
    except Exception:  # noqa: BLE001
        return None


def _field_map_of(conn: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """→ (ontology_id, {源字段名: 属性 id})。

    连接的 ``maps_to_ontology.field_map`` 本身即「源字段 → 属性 id」方向
    （dictionary.resolve_field_map 与 ingestion.apply_field_map 同口径），无需取反。
    """
    mto = conn.get("maps_to_ontology") or {}
    fmap: dict[str, str] = {}
    for src_field, attr_id in (mto.get("field_map") or {}).items():
        if attr_id:
            fmap[str(src_field)] = str(attr_id)
    return str(mto.get("ontology_id") or ""), fmap


def _governance_read(
    project_id: str, conn: dict[str, Any], tool: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """治理身份读源端（连接自带凭据）；审计留痕。"""
    from mcp_client import McpClient
    rows = McpClient().invoke_tool(conn, tool, params)
    try:
        import audit as audit_svc
        audit_svc.record(
            action="content_governance_read", actor="platform", actor_role="governance",
            project_id=project_id, target_type="instance_source",
            target_id=conn.get("id", ""),
            detail={"tool": tool, "key_params": {k: v for k, v in list(params.items())[:3]},
                    "rows": len(rows) if isinstance(rows, list) else 0},
        )
    except Exception:  # noqa: BLE001
        pass
    return rows if isinstance(rows, list) else []


def _viewer_read(
    project_id: str, user_id: str, conn: dict[str, Any], tool: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """访问者身份读源端（V2-14i 凭据映射；无映射 → 403，不回落匿名）。

    ``federated_read`` 返回 ``{source_connection, resolved_as, tool, result}``
    信封，记录列表在 ``result`` 中（http transport 为 list）。
    """
    from identity_federation import federated_read
    res = federated_read(
        project_id, user_id=user_id, connection=conn, tool=tool, params=params,
    )
    result = (res or {}).get("result")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    return []


def _local_legacy_values(project_id: str, rec: dict[str, Any]) -> dict[str, Any]:
    """迁移前存量的本地非键值（D3 回退底的第一形态）：排除关系快照键域。"""
    inst = rec.get("instance") or {}
    keep = keep_keys_for(project_id, rec)
    return {k: v for k, v in inst.items() if k not in keep}


def _fallback_values(rec: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """迁移后的回退底（``fallback_content`` 字段 + ``content_as_of``）。"""
    fb = rec.get("fallback_content")
    if not isinstance(fb, dict):
        return {}, None
    as_of = rec.get("content_as_of")
    return fb, (as_of if isinstance(as_of, str) and as_of else None)


def federate_values(
    project_id: str,
    rec: dict[str, Any],
    *,
    user_id: Optional[str] = None,
    attr_ids: Optional[list[str]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按键值回源取内容值。返回 ``(values, meta)``。

    - values: 属性 id → 值（源端最新；合并调用方自行处理）。
    - meta:   {mode, source_connection, connection_name, resolved_as, stale, as_of,
               fetched_at, error?}
      mode ∈ federated / legacy_local / fallback / unavailable
    唯一硬错误：鉴权启用且访问者无凭据映射 → ApiError 403（V2-14i，不回落匿名）。
    """
    inst = rec.get("instance") or {}
    obj = object_def_for(project_id, rec)
    key = (obj or {}).get("stable_key") or ""
    key_value = rec.get("stable_key_value")
    if key_value is None and key:
        key_value = inst.get(key)

    meta: dict[str, Any] = {
        "mode": "unavailable",
        "source_connection": None,
        "connection_name": None,
        "resolved_as": None,
        "stale": False,
        "as_of": None,
        "fetched_at": _now_iso(),
    }
    conn_id = connection_id_of(rec)
    if not conn_id:
        # 无来源连接：存量本地值（回退底）或不可用
        legacy = _local_legacy_values(project_id, rec)
        if legacy:
            meta.update(mode="legacy_local", stale=True,
                        as_of=rec.get("imported_at"), resolved_as="local_legacy")
            return {a: legacy.get(a) for a in attr_ids} if attr_ids else legacy, meta
        fb, as_of = _fallback_values(rec)
        if fb:
            meta.update(mode="fallback", stale=True, as_of=as_of, resolved_as="fallback_snapshot")
            return {a: fb.get(a) for a in attr_ids} if attr_ids else fb, meta
        meta["error"] = "实例无来源连接且无本地回退值"
        return {}, meta

    conn = _load_connection(project_id, conn_id)
    if not conn:
        legacy = _local_legacy_values(project_id, rec)
        if legacy:
            meta.update(mode="legacy_local", stale=True,
                        as_of=rec.get("imported_at"), resolved_as="local_legacy")
            return {a: legacy.get(a) for a in attr_ids} if attr_ids else legacy, meta
        fb, as_of = _fallback_values(rec)
        if fb:
            meta.update(mode="fallback", stale=True, as_of=as_of, resolved_as="fallback_snapshot")
            return {a: fb.get(a) for a in attr_ids} if attr_ids else fb, meta
        meta.update(source_connection=conn_id, error="来源连接不存在")
        return {}, meta

    meta["source_connection"] = conn_id
    meta["connection_name"] = conn.get("display_name") or conn.get("name")
    read_tool = conn.get("read_tool") or (conn.get("maps_to_ontology") or {}).get("read_tool")
    if not read_tool or key_value is None:
        meta["error"] = "缺 read_tool 或稳定键值，无法按键回源"
        legacy = _local_legacy_values(project_id, rec)
        if legacy:
            meta.update(mode="legacy_local", stale=True,
                        as_of=rec.get("imported_at"), resolved_as="local_legacy")
            return {a: legacy.get(a) for a in attr_ids} if attr_ids else legacy, meta
        fb, as_of = _fallback_values(rec)
        if fb:
            meta.update(mode="fallback", stale=True, as_of=as_of, resolved_as="fallback_snapshot")
            return {a: fb.get(a) for a in attr_ids} if attr_ids else fb, meta
        return {}, meta

    _, fmap = _field_map_of(conn)
    src_key_field = next((sf for sf, aid in fmap.items() if aid == key), None)
    if not src_key_field:
        meta["error"] = f"稳定键属性 {key!r} 无源字段映射"
        fb, as_of = _fallback_values(rec)
        if fb:
            meta.update(mode="fallback", stale=True, as_of=as_of, resolved_as="fallback_snapshot")
            return {a: fb.get(a) for a in attr_ids} if attr_ids else fb, meta
        legacy = _local_legacy_values(project_id, rec)
        if legacy:
            meta.update(mode="legacy_local", stale=True,
                        as_of=rec.get("imported_at"), resolved_as="local_legacy")
            return {a: legacy.get(a) for a in attr_ids} if attr_ids else legacy, meta
        return {}, meta

    params = {src_key_field: key_value}
    viewer_mode = bool(user_id)
    if viewer_mode:
        try:
            from tenant import bypass
            if bypass():
                viewer_mode = False
        except Exception:  # noqa: BLE001
            pass

    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    fetch_error: Optional[str] = None
    try:
        if viewer_mode:
            assert user_id  # for type-narrowing
            rows = _viewer_read(project_id, user_id, conn, read_tool, params)
            meta["resolved_as"] = "viewer"
        else:
            rows = _governance_read(project_id, conn, read_tool, params)
            meta["resolved_as"] = "governance"
    except ApiError as e:
        fetch_error = e.message
    except Exception as e:  # noqa: BLE001
        # V2-14i 硬边界：访问者无凭据映射（IdentityMappingMissing，403）不得被吞成回退底，
        # 必须上抛为 403（不回落匿名/默认账号）
        if viewer_mode:
            from identity_federation import IdentityError
            if isinstance(e, IdentityError) and getattr(e, "http_status", 0) == 403:
                msg = e.args[1] if len(e.args) > 1 else "访问者对来源连接无凭据映射"
                raise ApiError("IDENTITY_MAPPING_MISSING", msg, http_status=403) from e
        fetch_error = f"回源异常: {e}"

    if fetch_error is None and rows:
        # 源端应按键过滤；对不支持参数过滤的源（部分 mock/老接口返回全量）做客户端按键复核，
        # 防止把别的实例的值错配到本实例
        row = None
        for r0 in rows:
            if isinstance(r0, dict) and r0.get(src_key_field) == key_value:
                row = r0
                break
        if row is not None:
            values: dict[str, Any] = {}
            for src_field, aid in fmap.items():
                if src_field in row:
                    values[aid] = row[src_field]
            if attr_ids:
                values = {a: values.get(a) for a in attr_ids}
            fetched = _now_iso()
            meta.update(mode="federated", stale=False, fetched_at=fetched, as_of=fetched,
                        elapsed_ms=int((time.monotonic() - started) * 1000))
            # V2-10 旁路操作日志（M4-G）：外部读取（联邦成功回源）事件，旁路不影响主流程。
            try:
                import sidecar_log as sidecar_svc
                sidecar_svc.record(
                    type_=sidecar_svc.TYPE_EXTERNAL_READ, actor=(user_id or "-"),
                    project_id=project_id, target_type="source_connection",
                    target_id=conn_id,
                    payload={"instance_id": rec.get("id"),
                             "key_field": src_key_field, "key_value": key_value,
                             "read_tool": read_tool, "attr_ids": attr_ids,
                             "viewer_mode": bool(user_id), "resolved_as": meta.get("resolved_as")},
                    result={"values": {k: v for k, v in values.items() if isinstance(v, (str, int, float, bool, type(None)))}})
            except Exception:  # noqa: BLE001  旁路失败不影响主流程（TCE-10-01）
                pass
            return values, meta

    # 回源失败/无行/键不匹配 → 回退底
    legacy = _local_legacy_values(project_id, rec)
    if legacy:
        meta.update(mode="legacy_local", stale=True, as_of=rec.get("imported_at"),
                    resolved_as="local_legacy", error=fetch_error or "源端无此键")
        return {a: legacy.get(a) for a in attr_ids} if attr_ids else legacy, meta
    fb, as_of = _fallback_values(rec)
    if fb:
        meta.update(mode="fallback", stale=True, as_of=as_of,
                    resolved_as="fallback_snapshot", error=fetch_error or "源端无此键")
        return {a: fb.get(a) for a in attr_ids} if attr_ids else fb, meta
    meta["error"] = fetch_error or "源端无此键"
    return {}, meta


def merge_record_content(
    project_id: str,
    rec: dict[str, Any],
    *,
    user_id: Optional[str] = None,
    attr_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """详情读取路径：联邦渠道记录回源合并内容值（附加 ``content_resolution``，不修改原对象）。

    上传型记录原样返回（附加 ``content_mode=local``）。
    """
    if not is_federated(project_id, rec):
        r = dict(rec)
        r.setdefault("content_mode", "local")
        return r
    values, meta = federate_values(project_id, rec, user_id=user_id, attr_ids=attr_ids)
    inst = dict(rec.get("instance") or {})
    inst.update(values)
    r = dict(rec)
    r["instance"] = inst
    # 顶层状态跟随回源解析结果（federated/unavailable/fallback/legacy_local）
    r["content_mode"] = meta.get("mode") or "federated"
    r["content_resolution"] = meta
    return r


__all__ = [
    "CHANNEL_UPLOAD", "CHANNEL_EXTERNAL_MCP", "CHANNEL_CDC", "FEDERATED_CHANNELS",
    "channel_of", "is_federated", "annotate_rows", "connection_id_of",
    "strip_content_for_storage", "relation_snapshot",
    "federate_values", "merge_record_content",
]
