"""reasoning.engine：V2-16 双引擎推理调度（local rdflib / Fuseki remote）＋自动降级＋推理图范围检查（M4-C）。

- 引擎开关（TC-16-01）：运行时配置文件 data/reasoning_engine.json（PUT /api/reasoning/engine 写入，最高优先级）
  > 环境变量 ONTOLOGY_REASONING_ENGINE（部署期默认）> 缺省 local；
- 远程管道（TC-16-02，混合模式）：Jena 5.1.0 无法为 Fuseki 数据集挂载物化推理器
  （TDB2/TDB1/通用/内存装配器均不支持，OWL2-RL 模块不存在，DatasetRDFS 未注册——
  详见 fuseki-config.ttl 头部依赖事实），因此远程管道为：
  本地构建命名图（schema＋关系快照＋上传型值三元组，build_full_graph 同口径）
  → 推送 Fuseki 命名图（先清空再载入，持久化）→ 闭包由平台推理核心计算
  （local_materialize，与本地引擎同一闭包集，两模式输出逐字段一致）
  → 派生三元组写回远端命名图（推理结果在远端物化，SPARQL 可直接查）
  → COUNT 读回验证（远端计数 ≠ 闭包计数 → 验证失败 → 降级）
  → 与本地共用 reasoning._canonical_steps 渲染推理链路（含中文推断描述）；
- 自动降级（TC-16-03）：远程任一环节失败（不可达/更新失败/查询失败/超时/读回验证失败）
  → 自动降级本地，响应 degraded=true＋degrade_reason＋hint_zh（前端展示提示）；
- 推理图范围（TC-16-04）：check_reasoning_scope 断言源连接型实例进入推理图的三元组
  仅 rdf:type/稳定键 rdfs:label/关系快照（V2-16 范围负向断言）。
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from rdflib import OWL, RDFS, RDF, Graph, URIRef

from config import get_settings
from logging_setup import get_logger
from modeling_translator import attr_uri, rel_uri, translate_to_turtle
from reasoning import _canonical_steps, inference_trace, local_materialize

logger = get_logger("reasoning.engine")

_VALID_ENGINES = ("local", "fuseki")

_DEGRADE_HINT_ZH = (
    "Fuseki 远端服务当前不可用（或读回验证失败），已自动降级为本地推理；"
    "本地闭包与远端物化结果同由平台推理核心计算，结果一致，可稍后重试或检查 Fuseki 服务状态。"
)


class FusekiEngineError(Exception):
    """远程推理失败（reason 为机器可读原因码，供响应 degrade_reason 与前端提示）。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------------
# 引擎开关（TC-16-01）
# ---------------------------------------------------------------------------

def _config_path() -> str:
    return os.path.join(get_settings().data_dir, "reasoning_engine.json")


def get_configured_engine() -> tuple[str, str]:
    """解析生效引擎。返回 (engine, source)：source ∈ runtime | env | default。"""
    p = _config_path()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                v = (json.load(f) or {}).get("engine")
            if v in _VALID_ENGINES:
                return v, "runtime"
        except (OSError, ValueError):
            pass
    env = os.environ.get("ONTOLOGY_REASONING_ENGINE", "").strip().lower()
    if env in _VALID_ENGINES:
        return env, "env"
    return "local", "default"


def set_engine(engine: str) -> dict:
    """运行时切换推理引擎（原子写）。"""
    if engine not in _VALID_ENGINES:
        raise ValueError("engine 必须是 local 或 fuseki")
    p = _config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"engine": engine, "updated_at": datetime.now().isoformat(timespec="seconds")},
                  f, ensure_ascii=False)
    os.replace(tmp, p)
    logger.info("V2-16 推理引擎切换 engine=%s", engine)
    return {"engine": engine, "source": "runtime"}


# ---------------------------------------------------------------------------
# 推理图构建（本地命名图，与 rdf_sync 同一口径）
# ---------------------------------------------------------------------------

def build_reasoning_graph(ontology: dict, project_id: str, ontology_id: str) -> tuple[Graph, list[dict]]:
    """schema ＋ 全部实例三元组（关系快照＋上传型值）。返回 (图, 实例记录)。

    INV-1 ② 同口径（TC-16-04）：源连接型实例的稳定键值仅以 rdfs:label 入图，
    其自身属性三元组剔除（非键值本应已被入库层 strip，此处纵深防御；
    check_reasoning_scope 对越界三元组做可检测断言）。
    """
    from core.instance_store import JsonlInstanceStore
    from content_access import is_federated
    from jena_adapter.rdf_sync import build_full_graph

    instances = JsonlInstanceStore(project_id=project_id).find(
        filters={"ontology_id": ontology_id}, limit=100000)
    g = build_full_graph(ontology, instances)

    objects = {o["id"]: o for o in ontology.get("objects", [])
               if isinstance(o, dict) and o.get("id")}
    for rec in instances:
        iid = rec.get("id")
        if not iid or not is_federated(project_id, rec):
            continue
        obj = objects.get(rec.get("object_type") or "")
        key = (obj or {}).get("stable_key")
        if not key:
            continue
        subj = URIRef(f"http://ontology.local/inst_{iid}")
        g.remove((subj, URIRef(attr_uri(key)), None))
    return g, instances


def check_reasoning_scope(ontology: dict, project_id: str, ontology_id: str) -> dict:
    """TC-16-04：推理图范围检查（V2-16 负向断言，与 INV-1 ② 同口径）。

    源连接型实例（content_access.is_federated）进入推理图的三元组只允许：
    rdf:type ＋ 稳定键 rdfs:label ＋ 关系快照（rel_uri）；非键内容不得在图内。
    上传型实例：允许属性值（attr_uri）＋关系＋类型/键。
    """
    from content_access import is_federated
    from jena_adapter.rdf_sync import graph_uri_for

    g, instances = build_reasoning_graph(ontology, project_id, ontology_id)
    schema_g = Graph().parse(data=translate_to_turtle(ontology), format="turtle")
    objects = {o["id"]: o for o in ontology.get("objects", [])
               if isinstance(o, dict) and o.get("id")}

    stats = {"type_label_triples": 0, "relation_edge_triples": 0,
             "upload_value_triples": 0, "other_triples": 0}
    violations: list[dict] = []
    n_upload = n_fed = 0
    for rec in instances:
        iid = rec.get("id")
        if not iid:
            continue
        subj = URIRef(f"http://ontology.local/inst_{iid}")
        fed = is_federated(project_id, rec)
        if fed:
            n_fed += 1
        else:
            n_upload += 1
        obj = objects.get(rec.get("object_type") or "")
        attr_uris = {str(URIRef(attr_uri(a["id"]))) for a in obj.get("attributes", [])
                     if isinstance(a, dict) and a.get("id")}
        rel_uris = {str(URIRef(rel_uri(r["id"]))) for r in obj.get("relations", [])
                    if isinstance(r, dict) and r.get("id")}
        allowed = {str(RDF.type), str(RDFS.label)} | rel_uris
        if not fed:
            allowed |= attr_uris
        for s, p, _o in g.triples((subj, None, None)):
            pstr = str(p)
            if pstr in (str(RDF.type), str(RDFS.label)):
                stats["type_label_triples"] += 1
            elif pstr in rel_uris:
                stats["relation_edge_triples"] += 1
            elif pstr in attr_uris:
                stats["upload_value_triples"] += 1
            else:
                stats["other_triples"] += 1
            if pstr not in allowed:
                violations.append({
                    "subject": str(s), "predicate": pstr,
                    "reason_zh": "源连接型实例的非键内容三元组进入推理图"
                                 "（V2-16 范围仅允许 rdf:type/稳定键 label/关系快照）",
                })
    return {
        "ok": not violations,
        "graph": {
            "uri": graph_uri_for(ontology_id),
            "triples": len(g),
            "schema_triples": len(schema_g),
            "instance_triples": len(g) - len(schema_g),
        },
        "instances": {"total": len(instances), "upload": n_upload, "source_connection": n_fed},
        "stats": stats,
        "violations": violations,
    }


# ---------------------------------------------------------------------------
# 远程管道（TC-16-02）
# ---------------------------------------------------------------------------

def _http_post(url: str, body: str, headers: dict, timeout: float, fail_reason: str) -> bytes:
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                raise FusekiEngineError(fail_reason, f"status={resp.status}")
            return resp.read()
    except FusekiEngineError:
        raise
    except urllib.error.HTTPError as e:
        raise FusekiEngineError(fail_reason, f"HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as e:
        raise FusekiEngineError("fuseki_unreachable", str(e)) from e


def _http_get(url: str, accept: str, timeout: float) -> bytes:
    try:
        req = urllib.request.Request(url, headers={"Accept": accept})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                raise FusekiEngineError("fuseki_query_failed", f"status={resp.status}")
            return resp.read()
    except FusekiEngineError:
        raise
    except urllib.error.HTTPError as e:
        raise FusekiEngineError("fuseki_query_failed", f"HTTP {e.code}") from e
    except (TimeoutError, socket.timeout) as e:
        raise FusekiEngineError("fuseki_query_timeout", str(e)) from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise FusekiEngineError("fuseki_unreachable", str(e)) from e


def _sparql_count(body: bytes) -> int:
    """SPARQL JSON 结果（SELECT (COUNT(...) AS ?c)）→ 计数值。"""
    doc = json.loads(body.decode("utf-8"))
    for row in doc.get("results", {}).get("bindings", []):
        c = row.get("c")
        if c is not None:
            return int(c.get("value", 0))
    raise FusekiEngineError("fuseki_query_failed", "COUNT 查询未返回 ?c 绑定")


def _schema_derived_count(derived: set[tuple[str, str, str]],
                          orig_set: set[tuple[str, str, str]]) -> int:
    """闭包集中可被三规则族解释的派生三元组数（余下为 OWL2-RL 额外推导）。"""
    inv = str(OWL.inverseOf)
    sco = str(RDFS.subClassOf)
    dom = str(RDFS.domain)
    rng = str(RDFS.range)
    n = 0
    for s, p, o in derived:
        if p == inv and (o, inv, s) in orig_set:
            n += 1
        elif p in (sco, dom, rng):
            n += 1
    return n


def _fuseki_pipeline(ontology: dict, project_id: str, ontology_id: str, focus: str | None,
                     base_url: str | None, dataset: str | None, timeout: float) -> dict:
    """远程混合管道：推送 explicit → 平台核心算闭包 → 派生写回物化 → COUNT 读回验证。

    Jena 5.1.0 的 Fuseki 无法挂载物化推理器（见模块 docstring 与 fuseki-config.ttl），
    故闭包由 local_materialize 计算（与本地引擎同一闭包集 → 两模式 steps 逐字段一致）；
    Fuseki 的价值 = 持久化物化存储（SPARQL 可查推理结果）＋读回一致性验证。
    """
    from jena_adapter.rdf_sync import graph_uri_for

    settings = get_settings()
    base = (base_url or settings.jena_base_url).rstrip("/")
    ds = dataset or settings.jena_dataset

    g, _instances = build_reasoning_graph(ontology, project_id, ontology_id)
    graph_uri = graph_uri_for(ontology_id)
    schema_g = Graph().parse(data=translate_to_turtle(ontology), format="turtle")
    explicit_set = {(str(s), str(p), str(o)) for s, p, o in g}

    # 1) 推送本地命名图（先清空再载入，与 rdf_sync._replace_named_graph 同口径）
    nt = g.serialize(format="nt").strip()
    update = (f"DELETE WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o . }} }} ;\n"
              f"INSERT DATA {{ GRAPH <{graph_uri}> {{\n{nt}\n}} }}")
    _http_post(f"{base}/{ds}/update", update,
               {"Content-Type": "application/sparql-update"}, timeout, "fuseki_update_failed")

    # 2) 闭包 = explicit ∪ 平台推理核心派生（三规则族；remote/local 同一闭包集契约）
    closure_set = local_materialize(g)
    if not closure_set:
        raise FusekiEngineError("fuseki_query_failed", "闭包集为空")
    derived_set = closure_set - explicit_set

    # 3) 派生三元组写回远端命名图（推理结果在远端物化，SPARQL 可直接查；
    #    下次 rdf_sync 全量替换命名图时清除，属预期行为）
    if derived_set:
        der_g = Graph()
        for s, p, o in derived_set:
            der_g.add((URIRef(s), URIRef(p), URIRef(o)))
        der_nt = der_g.serialize(format="nt").strip()
        _http_post(f"{base}/{ds}/update",
                   f"INSERT DATA {{ GRAPH <{graph_uri}> {{\n{der_nt}\n}} }}",
                   {"Content-Type": "application/sparql-update"}, timeout, "fuseki_update_failed")

    # 4) COUNT 读回验证（远端命名图计数必须 == 闭包计数；不符说明远端存储不一致 → 降级）
    query = f"SELECT (COUNT(?s) AS ?c) WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}"
    body = _http_get(f"{base}/{ds}/query?query={urllib.parse.quote(query)}",
                     "application/sparql-results+json", timeout)
    remote_count = _sparql_count(body)
    if remote_count != len(closure_set):
        raise FusekiEngineError(
            "fuseki_verify_failed",
            f"读回验证失败：远端 {remote_count} 条 ≠ 闭包 {len(closure_set)} 条")

    # 5) 与本地共用规范化渲染（闭包集同构 → steps 与 local 引擎逐字段一致）
    steps = _canonical_steps(schema_g, closure_set, focus)
    return {
        "steps": steps,
        "graph": {
            "uri": graph_uri,
            "triples": len(g),
            "schema_triples": len(schema_g),
            "instance_triples": len(g) - len(schema_g),
        },
        "remote": {
            "base_url": base,
            "dataset": ds,
            "graph": graph_uri,
            "pushed_triples": len(g),
            "materialized_triples": len(derived_set),
            "closure_triples": len(closure_set),
            "verified": True,
            "closure_computation": "local",
            "extra_inferences": max(0, len(derived_set) - _schema_derived_count(derived_set, explicit_set)),
        },
    }


# ---------------------------------------------------------------------------
# 对外入口（router 调用）
# ---------------------------------------------------------------------------

def run_inference(ontology: dict, project_id: str, ontology_id: str,
                  focus: str | None = None, engine: str | None = None,
                  base_url: str | None = None, dataset: str | None = None,
                  timeout: float = 60.0) -> dict:
    """双引擎推理入口。engine=None 时按 运行时配置 > 环境变量 > local 解析。

    返回统一结构：engine（实际使用）/ requested_engine / degraded / degrade_reason /
    hint_zh / steps / elapsed_ms / graph / remote（local 模式下 graph/remote 为 None）。
    """
    if engine:
        requested = engine
    else:
        requested, _src = get_configured_engine()

    degrade_reason: str | None = None
    if requested == "fuseki":
        try:
            t0 = time.monotonic()
            data = _fuseki_pipeline(ontology, project_id, ontology_id, focus,
                                    base_url, dataset, timeout)
            data["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
            data.update({"engine": "fuseki", "requested_engine": "fuseki",
                         "degraded": False, "degrade_reason": None, "hint_zh": None})
            return data
        except FusekiEngineError as e:
            degrade_reason = e.reason
            logger.warning("V2-16 Fuseki 推理失败 reason=%s detail=%s，自动降级本地",
                           e.reason, e.detail)

    t0 = time.monotonic()
    steps = inference_trace(ontology, focus)
    return {
        "engine": "local",
        "requested_engine": "fuseki" if requested == "fuseki" else "local",
        "degraded": requested == "fuseki",
        "degrade_reason": degrade_reason if requested == "fuseki" else None,
        "hint_zh": _DEGRADE_HINT_ZH if requested == "fuseki" else None,
        "steps": steps,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "graph": None,
        "remote": None,
    }
