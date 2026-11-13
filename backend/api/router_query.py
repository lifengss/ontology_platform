"""V2-15 开放查询接口（M4-B）：SPARQL / 通用 REST / GraphQL。

设计口径（docs/V2-功能需求定义.md V2-15；验收 TC-15-01~07、TCE-15-01~03）：

1. 数据边界（INV-5，V2-02 INV-1 派生）：开放接口只暴露**本地图数据**
   （schema + 关系快照 + 上传型实例值）。源连接型实例的非键内容**不做**
   开放接口内联联邦读（无逐查看者凭据语义）——需要内容走
   MCP 联邦读路径（平台能力通道，V2-04/V2-14）或直连源系统；
   行级返回数据边界提示。
2. 字段来源标记：schema / snapshot（关系快照：业务键、类型、关系）/
   upload（上传型实例值）。REST 行级 `_field_sources`、GraphQL 行级
   `_fieldSources`、SPARQL 响应级 `provenance` 图例。
3. 权限（V2-05）：三接口均受全局鉴权过滤（未认证 401 / 权限不足 403，
   POST /sparql 与 /graphql 经 READ_ONLY_POST_RE 映射 read 动作）＋
   schema 可见性（`tenant.ensure_schema_visible`，跨项目不可见 404 不泄露）。
4. 仅已发布本体（status=active）可查询；不存在 / 未发布 / 草稿 →
   **同一 404 文案**（不暴露存在性与状态细节，TCE-15-02）。
5. SPARQL 护栏（TCE-15-01）：仅 SELECT；语法错误走标准语义
   422 QUERY_SYNTAX_ERROR；禁 GRAPH/FROM/NAMED/SERVICE 子句
   （平台自动限定目标本体命名图，防跨图探测，TC-15-04）；
   行数上限强制（默认 1000 / 上限 5000）；远程引擎 4xx → 422、
   超时 → 504、其他远程故障 → 502。
6. GraphQL：schema 由目标本体**动态生成**（对象/属性/关系 + 每对象
   实例类型），introspection 只能看到已发布本体的 schema
   （未发布本体连 schema 都不可得，TCE-15-03）；实例查询带
   limit/offset 护栏（默认 20 / 上限 200）；数据边界与 V2-06
   上传型内容读权限（显式开放才可读）同 REST 行级语义。
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from api.dependencies import project_id_param
import audit as audit_svc
import content_access
import tenant
from core.instance_store import get_instance_store
from core.responses import ApiError, list_payload, parse_pagination, success
from core.storage import JsonStore
from jena_adapter.client import graph_uri_for, sparql_query
from logging_setup import get_logger

logger = get_logger("router_query")

router = APIRouter(prefix="/api/query", tags=["query"])

# ---- 护栏参数（TCE-15-01：行数上限拦截；GraphQL 同口径） ----
SPARQL_DEFAULT_LIMIT = 1000
SPARQL_MAX_LIMIT = 5000
GRAPHQL_DEFAULT_LIMIT = 20
GRAPHQL_MAX_LIMIT = 200
GRAPHQL_MAX_OFFSET = 10000

# ---- 数据边界文案（INV-5） ----
BOUNDARY_HINT_FEDERATED = (
    "源连接型实例的非键内容不包含在开放接口内（V2-02 INV-1 / V2-15 数据边界）： "
    "如需内容，请走 MCP 联邦读路径（平台能力通道）或直连源系统。"
)
BOUNDARY_HINT_CLOSED = (
    "该上传型实例内容未对你开放（V2-06 显式开放规则）：仅可见业务键与关系；"
    "请向上传者经实例详情页开放内容。"
)

# SPARQL 命名图数据三类来源图例（TC-15-06：schema / snapshot / upload）
SPARQL_PROVENANCE = {
    "schema": "schema 三元组：本体对象/属性/关系定义（类、属性、关系声明）",
    "snapshot": "关系快照三元组：实例业务键（rdfs:label）、类型（rdf:type）、关系边",
    "upload": "上传型实例值三元组：上传通道属性值（值全部本地落图）",
}

# 未发布/不存在统一 404 文案（TCE-15-02：不暴露存在性与状态细节）
_NOT_PUBLISHED_MSG = "本体不存在或未发布"

# SPARQL 跨图/外联子句禁词（TC-15-04 命名图隔离）：
# 平台经 jena_adapter._wrap_graph_filter 自动限定目标本体命名图，
# 用户查询内再出现 GRAPH/FROM/NAMED/SERVICE 即视为跨图/外联尝试。
_FORBIDDEN_SPARQL_CLAUSES = (
    (re.compile(r"\bGRAPH\b", re.IGNORECASE), "GRAPH"),
    (re.compile(r"\bFROM\b", re.IGNORECASE), "FROM"),
    (re.compile(r"\bNAMED\b", re.IGNORECASE), "NAMED"),
    (re.compile(r"\bSERVICE\b", re.IGNORECASE), "SERVICE"),
)

_LIMIT_TAIL_RE = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.IGNORECASE)
_GQL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# 可见性 / 发布状态（数据边界入口）
# ---------------------------------------------------------------------------

def _get_visible_ontology(project_id: str, ontology_id: str, request: Request) -> dict:
    """存在 + schema 可见 + 已发布（status=active）。

    不存在 / 跨项目不可见 / 草稿未发布 → 同一 404 文案（不暴露存在性，TCE-15-02）。
    """
    ont = JsonStore("ontologies", project_id).get(ontology_id)
    if not ont:
        raise ApiError("NOT_FOUND", _NOT_PUBLISHED_MSG, http_status=404)
    tenant.ensure_schema_visible(request, project_id, ont)
    if ont.get("status") != "active":
        raise ApiError("NOT_FOUND", _NOT_PUBLISHED_MSG, http_status=404)
    return ont


# ---------------------------------------------------------------------------
# SPARQL（SELECT-only，命名图隔离 + 护栏）
# ---------------------------------------------------------------------------

def _validate_sparql(query_text: str) -> None:
    """语法 / 类型 / 跨图隔离校验。

    - 语法错误 → 422 QUERY_SYNTAX_ERROR（标准 SPARQL 错误语义，TCE-15-01）
    - 非 SELECT → 422 QUERY_TYPE_NOT_ALLOWED（开放接口只读；
      ASK/CONSTRUCT 在 file 模式适配层无统一语义，暂不开放）
    - GRAPH/FROM/NAMED/SERVICE 子句 → 422 CROSS_GRAPH_FORBIDDEN（TC-15-04）
    """
    # 跨图/外联子句先于解析拦截（fail-closed：即使该子句在解析器中语法非法，
    # 也统一按 CROSS_GRAPH_FORBIDDEN 拒绝，避免把隔离问题暴露为普通语法错误）
    for rx, kw in _FORBIDDEN_SPARQL_CLAUSES:
        if rx.search(query_text):
            raise ApiError(
                "CROSS_GRAPH_FORBIDDEN",
                f"开放查询接口禁止使用 {kw} 子句：平台已自动限定目标本体命名图，不支持跨图/外联查询",
                http_status=422,
            )

    from rdflib.plugins.sparql.parser import parseQuery  # 延迟导入，失败不拖累模块加载

    try:
        parsed = parseQuery(query_text)
    except Exception as e:  # pyparsing.exceptions.ParseException
        raise ApiError(
            "QUERY_SYNTAX_ERROR",
            "SPARQL 语法错误: " + " ".join(str(e).split())[:300],
            http_status=422,
        )
    body = parsed[1] if len(parsed) > 1 else parsed
    name = getattr(body, "name", None) or ""  # CompValue.name: SelectQuery/AskQuery/...
    if name != "SelectQuery":
        raise ApiError(
            "QUERY_TYPE_NOT_ALLOWED",
            "开放查询接口仅支持 SELECT 查询（只读；ASK/CONSTRUCT 暂不开放）",
            http_status=422,
        )


def _enforce_limit(query_text: str) -> tuple[str, int]:
    """行数上限护栏（TCE-15-01）：无 LIMIT 注入默认值；超出上限钳制。"""
    q = query_text.rstrip()
    m = _LIMIT_TAIL_RE.search(q)
    if m:
        user_limit = int(m.group(1))
        eff = min(user_limit, SPARQL_MAX_LIMIT)
        if eff != user_limit:
            q = q[: m.start(1)] + str(eff) + q[m.end(1):]
        return q, eff
    return q + f" LIMIT {SPARQL_DEFAULT_LIMIT}", SPARQL_DEFAULT_LIMIT


@router.post("/sparql")
def query_sparql(payload: dict, request: Request,
                 project_id: str = Depends(project_id_param)):
    """SPARQL 查询（按本体命名图隔离，只读 SELECT）。

    请求体: {"ontology_id": str, "query": str}
    响应 data: {graph, limit, count, results, provenance, data_boundary}
    """
    ontology_id = (payload or {}).get("ontology_id")
    query_text = (payload or {}).get("query")
    if not ontology_id or not isinstance(query_text, str) or not query_text.strip():
        raise ApiError("VALIDATION_ERROR", "ontology_id 与 query 为必填项", http_status=422)
    ont = _get_visible_ontology(project_id, ontology_id, request)
    _validate_sparql(query_text)
    query_final, eff_limit = _enforce_limit(query_text)
    try:
        rows = sparql_query(query_final, project_id=project_id, ontology_id=ontology_id)
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            raise ApiError("QUERY_SYNTAX_ERROR",
                           f"远程 RDF 引擎查询错误（HTTP {e.code}）", http_status=422)
        raise ApiError("QUERY_EXECUTION_ERROR", "远程 RDF 引擎执行失败", http_status=502)
    except (socket.timeout, TimeoutError):
        raise ApiError("QUERY_TIMEOUT", "查询超时：请缩小查询范围或降低行数", http_status=504)
    except Exception as e:  # 本地/远程执行异常（含 Fuseki 不可达且无本地文件等）
        raise ApiError("QUERY_EXECUTION_ERROR", f"查询执行失败: {e}", http_status=502)
    audit_svc.record_from_request(
        request, "query_sparql", project_id=project_id,
        target_type="ontology", target_id=ontology_id, target_name=ont.get("name", ""),
        after={"rows": len(rows), "limit": eff_limit},
    )
    return success({
        "graph": graph_uri_for(ontology_id),
        "limit": eff_limit,
        "count": len(rows),
        "results": rows,
        "provenance": SPARQL_PROVENANCE,
        "data_boundary": BOUNDARY_HINT_FEDERATED,
    })


# ---------------------------------------------------------------------------
# 通用 REST 查询（对象类型 + 条件 + 分页，行级来源标记 + 数据边界提示）
# ---------------------------------------------------------------------------

def _build_row(project_id: str, user: Optional[dict], ont: dict, rec: dict) -> dict:
    """开放接口行：字段来源标记（schema/snapshot/upload）＋数据边界提示。

    - 源连接型：仅关系快照（业务键/关系，_field_sources=snapshot）＋
      非键字段边界提示（INV-5：非键内容不在开放接口内）。
    - 上传型且内容对其可见（V2-06：所有者/admin/显式开放）：
      全量值（_field_sources=upload）。
    - 上传型且内容不可见：仅业务键/关系快照＋限制提示（_content_restricted）。
    """
    inst = rec.get("instance") or {}
    objs = {o.get("id"): o for o in (ont.get("objects") or [])
            if isinstance(o, dict) and o.get("id")}
    obj = objs.get(rec.get("object_type")) or {}
    key_attr = obj.get("stable_key") or ""
    attr_ids = [a.get("id") for a in (obj.get("attributes") or [])
                if isinstance(a, dict) and a.get("id")]
    fed = content_access.is_federated(project_id, rec)

    if fed:
        keep = content_access.keep_keys_for(project_id, rec)
        content = {k: v for k, v in inst.items() if k in keep and k != "_source"}
        sources = {k: "snapshot" for k in content}
        boundary = {
            "restricted_fields": [a for a in attr_ids if a and a != key_attr and a not in content],
            "hint": BOUNDARY_HINT_FEDERATED,
        }
        restricted = False
    elif tenant.content_visible(user, project_id, rec, ont):
        content = {k: v for k, v in inst.items() if k != "_source"}
        sources = {k: "upload" for k in content}
        boundary = None
        restricted = False
    else:
        keep = content_access.keep_keys_for(project_id, rec)
        content = {k: v for k, v in inst.items() if k in keep and k != "_source"}
        sources = {k: "snapshot" for k in content}
        boundary = {
            "restricted_fields": [a for a in attr_ids if a and a not in content],
            "hint": BOUNDARY_HINT_CLOSED,
        }
        restricted = True

    row: dict[str, Any] = {
        "id": rec.get("id"),
        "ontology_id": rec.get("ontology_id"),
        "object_type": rec.get("object_type"),
        # 对象定义来自本体 schema（来源标记 schema）
        "object": {
            "id": obj.get("id"), "name": obj.get("name"),
            "stable_key": key_attr, "source": "schema",
        },
        "channel": "federated" if fed else "upload",
        "stable_key_value": rec.get("stable_key_value"),
        "imported_at": rec.get("imported_at"),
        "instance": content,
        "_field_sources": sources,
        "_content_boundary": boundary,
    }
    if restricted:
        row["_content_restricted"] = True
    return row


def _iter_ontology_instances(project_id: str, ontology_id: str,
                             object_type: Optional[str],
                             conditions: dict[str, Any]) -> list[dict]:
    """按本体/对象类型/实例字段条件过滤（实例内容过滤在记录 instance 层匹配）。"""
    recs = get_instance_store(project_id).find(filters={"ontology_id": ontology_id}, limit=None)
    out = []
    for rec in recs or []:
        if object_type and rec.get("object_type") != object_type:
            continue
        inst = rec.get("instance") or {}
        if any(inst.get(k) != v for k, v in conditions.items()):
            continue
        out.append(rec)
    return out


def _parse_filter(flt: Optional[str]) -> dict[str, Any]:
    if not flt:
        return {}
    try:
        cond = json.loads(flt)
    except Exception:
        raise ApiError("VALIDATION_ERROR", "filter 须为 JSON 对象字符串", http_status=422)
    if not isinstance(cond, dict):
        raise ApiError("VALIDATION_ERROR", "filter 须为 JSON 对象字符串", http_status=422)
    return cond


@router.get("/instances")
def query_instances(request: Request,
                    project_id: str = Depends(project_id_param),
                    ontology_id: str = Query(...),
                    object_type: Optional[str] = Query(default=None),
                    filter: Optional[str] = Query(default=None, alias="filter"),
                    page: int = Query(default=1, ge=1),
                    pageSize: int = Query(default=50, ge=1)):
    """通用 REST 查询（对象类型 + 条件 + 分页）。

    行级字段来源标记（`_field_sources`: schema/snapshot/upload）与
    数据边界提示（`_content_boundary`）；上传型内容读权限按 V2-06 显式开放。
    """
    ont = _get_visible_ontology(project_id, ontology_id, request)
    user = tenant.current_user(request)
    conditions = _parse_filter(filter)
    recs = _iter_ontology_instances(project_id, ontology_id, object_type, conditions)
    rows = [_build_row(project_id, user, ont, rec) for rec in recs]
    p, ps = parse_pagination(page, pageSize)
    total = len(rows)
    paged = rows[(p - 1) * ps: (p - 1) * ps + ps]
    audit_svc.record_from_request(
        request, "query_instances", project_id=project_id,
        target_type="ontology", target_id=ontology_id, target_name=ont.get("name", ""),
        after={"total": total, "page": p, "pageSize": ps, "object_type": object_type},
    )
    return success(list_payload(paged, total, p, ps))


# ---------------------------------------------------------------------------
# GraphQL（schema 由本体动态生成；introspection 仅已发布）
# ---------------------------------------------------------------------------

def _gql_name(raw: str, fallback: str) -> str:
    """对象/属性 id → 合法 GraphQL 名字（非法字符转下划线，空则回退）。"""
    if raw and _GQL_NAME_RE.match(raw):
        return raw
    safe = re.sub(r"[^A-Za-z0-9_]", "_", raw or "")
    if safe and not safe[0].isdigit():
        return safe
    return fallback


_GQL_TYPE_MAP = {
    "string": "String",
    "text": "String",
    "date": "String",
    "datetime": "String",
    "timestamp": "String",
    "integer": "Int",
    "int": "Int",
    "float": "Float",
    "number": "Float",
    "decimal": "Float",
    "boolean": "Boolean",
    "bool": "Boolean",
}


def _build_graphql(ont: dict):
    """由已发布本体动态构建 GraphQL schema（对象/属性/关系 + 每对象实例类型）。

    返回 (schema, type_by_object)：type_by_object 映射对象 id → (类型名, 属性映射)。
    introspection 只能看到本 schema（仅含已发布本体内容，TCE-15-03）。
    """
    import graphql as gql
    from graphql import (
        GraphQLArgument, GraphQLBoolean, GraphQLField, GraphQLFloat, GraphQLInt,
        GraphQLList, GraphQLNonNull, GraphQLObjectType, GraphQLSchema,
        GraphQLString,
    )

    field_source_t = GraphQLObjectType("FieldSource", {
        "name": GraphQLField(GraphQLString),
        "source": GraphQLField(GraphQLString),
    })
    content_boundary_t = GraphQLObjectType("ContentBoundary", {
        "restrictedFields": GraphQLField(GraphQLList(GraphQLNonNull(GraphQLString))),
        "hint": GraphQLField(GraphQLString),
    })
    attribute_t = GraphQLObjectType("Attribute", {
        "id": GraphQLField(GraphQLNonNull(GraphQLString)),
        "name": GraphQLField(GraphQLString),
        "dataType": GraphQLField(GraphQLString),
        "required": GraphQLField(GraphQLBoolean),
    })
    relation_t = GraphQLObjectType("Relation", {
        "id": GraphQLField(GraphQLNonNull(GraphQLString)),
        "name": GraphQLField(GraphQLString),
        "targetObject": GraphQLField(GraphQLString),
        "cardinality": GraphQLField(GraphQLString),
    })
    object_type_t = GraphQLObjectType("ObjectType", {
        "id": GraphQLField(GraphQLNonNull(GraphQLString)),
        "name": GraphQLField(GraphQLString),
        "stableKey": GraphQLField(GraphQLString),
        "attributes": GraphQLField(GraphQLList(GraphQLNonNull(attribute_t))),
        "relations": GraphQLField(GraphQLList(GraphQLNonNull(relation_t))),
    })
    ontology_t = GraphQLObjectType("Ontology", {
        "id": GraphQLField(GraphQLNonNull(GraphQLString)),
        "name": GraphQLField(GraphQLString),
        "version": GraphQLField(GraphQLString),
        "publishedVersion": GraphQLField(GraphQLString),
        "objectTypes": GraphQLField(GraphQLList(GraphQLNonNull(object_type_t))),
    })

    gql_string = GraphQLString
    gql_int = GraphQLInt
    gql_float = GraphQLFloat
    gql_bool = GraphQLBoolean

    def _coerce(gtype: str, value: Any) -> Any:
        """按声明类型做 best-effort 转换（失败返回 None，不抛 GraphQL 字段错误）。"""
        if value is None:
            return None
        try:
            if gtype == "Int":
                return int(value)
            if gtype == "Float":
                return float(value)
            if gtype == "Boolean":
                return bool(value)
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            return value
        except (TypeError, ValueError):
            return None

    def _boundary_view(row: dict, _info) -> Optional[dict]:
        b = (row or {}).get("_content_boundary")
        if not b:
            return None
        return {"restrictedFields": b.get("restricted_fields") or [],
                "hint": b.get("hint") or ""}

    instance_fields_common = {
        "id": GraphQLField(GraphQLNonNull(GraphQLString),
                           resolve=lambda row, _i: (row or {}).get("id")),
        "channel": GraphQLField(GraphQLString,
                                resolve=lambda row, _i: (row or {}).get("channel")),
        "stableKey": GraphQLField(GraphQLString,
                                  resolve=lambda row, _i: (row or {}).get("stable_key_value")),
        "_fieldSources": GraphQLField(
            GraphQLList(GraphQLNonNull(field_source_t)),
            resolve=lambda row, _i: [
                {"name": k, "source": v}
                for k, v in ((row or {}).get("_field_sources") or {}).items()]),
        "_contentBoundary": GraphQLField(content_boundary_t, resolve=_boundary_view),
    }

    query_fields: dict[str, GraphQLField] = {
        "ontology": GraphQLField(
            ontology_t, resolve=lambda _r, info: info.context["ontology_view"]),
    }
    type_by_object: dict[str, tuple[str, dict[str, str], list[str]]] = {}
    objects = [o for o in (ont.get("objects") or []) if isinstance(o, dict) and o.get("id")]
    for idx, obj in enumerate(objects):
        obj_id = obj["id"]
        tname = _gql_name(obj_id, f"Obj{idx}")
        attr_ids = [a.get("id") for a in (obj.get("attributes") or [])
                    if isinstance(a, dict) and a.get("id")]
        rel_ids = [r.get("id") for r in (obj.get("relations") or [])
                   if isinstance(r, dict) and r.get("id")]
        fields: dict[str, GraphQLField] = dict(instance_fields_common)
        attr_field_names: dict[str, str] = {}
        for a_idx, attr in enumerate(obj.get("attributes") or []):
            if not isinstance(attr, dict) or not attr.get("id"):
                continue
            f = _gql_name(attr["id"], f"Attr{a_idx}")
            if f in fields:
                continue
            gtype_name = _GQL_TYPE_MAP.get(str(attr.get("data_type") or "string").lower(), "String")
            gtype = {"Int": gql_int, "Float": gql_float,
                     "Boolean": gql_bool, "String": gql_string}[gtype_name]

            def _resolver(row, _info, _aid=attr["id"], _gt=gtype_name):
                return _coerce(_gt, ((row or {}).get("instance") or {}).get(_aid))

            fields[f] = GraphQLField(gtype, resolve=_resolver)
            attr_field_names[attr["id"]] = f
        for r_idx, rel in enumerate(obj.get("relations") or []):
            if not isinstance(rel, dict) or not rel.get("id"):
                continue
            f = _gql_name(rel["id"], f"Rel{r_idx}")
            if f in fields:
                continue

            def _rel_resolver(row, _info, _rid=rel["id"]):
                v = ((row or {}).get("instance") or {}).get(_rid)
                return None if v is None else str(v)

            fields[f] = GraphQLField(GraphQLString, resolve=_rel_resolver)
        obj_type = GraphQLObjectType(tname, fields)
        type_by_object[obj_id] = (tname, attr_field_names, rel_ids)

        def _instances_resolver(_r, info, _ot=obj_id,
                                id=None, filter=None, limit=None, offset=None):
            return info.context["instances_for"](_ot, {
                "id": id, "filter": filter, "limit": limit, "offset": offset,
            })

        qname = _gql_name(obj_id, f"Query{idx}")
        if qname in query_fields:  # 与 ontology 或其他对象字段重名 → 加后缀
            qname = f"{qname}_list"
        query_fields[qname] = GraphQLField(
            GraphQLList(GraphQLNonNull(obj_type)),
            args={
                "id": GraphQLArgument(GraphQLString),
                "filter": GraphQLArgument(GraphQLString),
                "limit": GraphQLArgument(GraphQLInt),
                "offset": GraphQLArgument(GraphQLInt),
            },
            resolve=_instances_resolver,
        )

    query_t = GraphQLObjectType("Query", query_fields)
    return GraphQLSchema(query=query_t, types=[field_source_t, content_boundary_t,
                                               attribute_t, relation_t, object_type_t,
                                               ontology_t]), type_by_object


class _GraphQLCtx:
    """GraphQL 解析上下文（闭包携带项目/用户/本体/实例行构建器）。"""

    def __init__(self, project_id: str, user: Optional[dict], ont: dict):
        self.project_id = project_id
        self.user = user
        self.ont = ont
        self._store = get_instance_store(project_id)

    @property
    def ontology_view(self) -> dict:
        objs = []
        for o in (self.ont.get("objects") or []):
            if not isinstance(o, dict) or not o.get("id"):
                continue
            objs.append({
                "id": o.get("id"),
                "name": o.get("name") or o.get("id"),
                "stableKey": o.get("stable_key") or "",
                "attributes": [
                    {"id": a.get("id"), "name": a.get("name") or a.get("id"),
                     "dataType": a.get("data_type") or "string",
                     "required": bool(a.get("required"))}
                    for a in (o.get("attributes") or [])
                    if isinstance(a, dict) and a.get("id")
                ],
                "relations": [
                    {"id": r.get("id"), "name": r.get("name") or r.get("id"),
                     "targetObject": r.get("target_object") or "",
                     "cardinality": r.get("cardinality") or ""}
                    for r in (o.get("relations") or [])
                    if isinstance(r, dict) and r.get("id")
                ],
            })
        return {
            "id": self.ont.get("id"),
            "name": self.ont.get("name") or "",
            "version": self.ont.get("version") or "",
            "publishedVersion": self.ont.get("published_version") or "",
            "objectTypes": objs,
        }

    def instances_for(self, object_type: str, args: dict) -> list[dict]:
        limit = args.get("limit") or GRAPHQL_DEFAULT_LIMIT
        limit = max(1, min(int(limit), GRAPHQL_MAX_LIMIT))
        offset = args.get("offset") or 0
        offset = max(0, min(int(offset), GRAPHQL_MAX_OFFSET))
        try:
            cond = _parse_filter(args.get("filter"))
        except ApiError:
            cond = {}
        recs = _iter_ontology_instances(self.project_id, self.ont.get("id"),
                                        object_type, cond)
        if args.get("id"):
            recs = [r for r in recs if r.get("id") == args.get("id")]
        return [_build_row(self.project_id, self.user, self.ont, rec)
                for rec in recs[offset: offset + limit]]


@router.post("/graphql")
def query_graphql(payload: dict, request: Request,
                  project_id: str = Depends(project_id_param)):
    """GraphQL 查询（schema 由已发布本体动态生成）。

    请求体: {"ontology_id": str, "query": str, "variables": {...}?}
    响应为标准 GraphQL 信封 {"data": ..., "errors": [...]}
    （协议原生语义；introspection 仅暴露本已发布本体 schema，TCE-15-03）。
    """
    from graphql import graphql_sync

    ontology_id = (payload or {}).get("ontology_id")
    query_text = (payload or {}).get("query")
    variables = (payload or {}).get("variables") or {}
    if not ontology_id or not isinstance(query_text, str) or not query_text.strip():
        raise ApiError("VALIDATION_ERROR", "ontology_id 与 query 为必填项", http_status=422)
    ont = _get_visible_ontology(project_id, ontology_id, request)
    user = tenant.current_user(request)
    schema, _ = _build_graphql(ont)
    ctx = _GraphQLCtx(project_id, user, ont)
    try:
        result = graphql_sync(
            schema, query_text,
            root_value=None,
            context_value={
                "ontology_view": ctx.ontology_view,
                "instances_for": ctx.instances_for,
            },
            variable_values=variables,
        )
    except Exception as e:
        # graphql_sync 对解析错误返回 error 结果而非抛异常；此处兜底
        return JSONResponse(content={"data": None, "errors": [{"message": str(e)}]})
    audit_svc.record_from_request(
        request, "query_graphql", project_id=project_id,
        target_type="ontology", target_id=ontology_id, target_name=ont.get("name", ""),
        after={"has_errors": bool(getattr(result, "errors", None))},
    )
    return JSONResponse(content={
        "data": result.data if result.data is not None else None,
        "errors": [{"message": e.message} for e in (result.errors or [])],
    })
