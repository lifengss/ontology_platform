"""RDF 同步：把本体全量图（schema + 实例）重建并写入 RDF 存储。

- Fuseki 可达：每个本体写入独立命名图 urn:ontology:{ontology_id}（先清空再载入，等效原子替换）；
- 不可达：降级本地文件 data/projects/<pid>/ontology_<oid>.ttl（同样包含实例三元组，
  与远端语义一致，sparql_query 的文件降级分支可直接查到实例）。

查询侧配套：client._sparql_remote 用 _wrap_graph_filter 把查询限定到对应命名图。
触发点：本体创建/更新/对象编辑/LLM 补全（router_ontologies）、实例审批/修改/删除
（router_ingest）、手动回填 POST /api/graph/{id}/rdf-sync、全量回填 scripts/sync_rdf_all.py。
"""
from __future__ import annotations

import os
import urllib.request

from rdflib import Graph, Literal, RDF, RDFS, URIRef, XSD

from config import get_settings
from core.instance_store import JsonlInstanceStore
from core.storage import JsonStore
from jena_adapter.client import FILE_MODE, REMOTE_MODE, _is_reachable
from logging_setup import get_logger
from modeling_translator import attr_uri, class_uri, rel_uri, translate_to_turtle

logger = get_logger("jena_adapter.rdf_sync")

_TIMEOUT = 30  # 秒

# 实例记录里非业务属性的字段（不作为三元组）
_RESERVED_KEYS = {"id", "_source"}


def graph_uri_for(ontology_id: str) -> str:
    """本体在 Fuseki 中的命名图标识。"""
    return f"urn:ontology:{ontology_id}"


def _typed_literal(value, data_type: str):
    """按声明的数据类型生成带 xsd 类型的字面量；未知类型/转换失败回退字符串字面量。"""
    try:
        if data_type == "integer":
            return Literal(int(float(str(value))))
        if data_type == "float":
            return Literal(float(value))
        if data_type == "boolean":
            if isinstance(value, str):
                return Literal(value.strip().lower() in ("1", "true", "yes", "是"))
            return Literal(bool(value))
        if data_type == "date":
            return Literal(str(value), datatype=XSD.date)
        if data_type == "datetime":
            return Literal(str(value), datatype=XSD.dateTime)
    except (TypeError, ValueError):
        pass
    return Literal(str(value))


def build_instance_triples(ontology: dict, instances: list[dict]) -> Graph:
    """实例记录 → ABox 三元组图（稳定 URI，供 SPARQL 持久查询）。

    - 主体：http://ontology.local/inst_{记录id}
    - 类型：与 schema 一致的类 URI
    - 属性：与 schema 一致的属性 URI → 带类型字面量
    - 关系：目标值能解析到目标对象实例的 stable_key 时指向其实例主体，否则字面量
    """
    g = Graph()
    objects = {o["id"]: o for o in ontology.get("objects", []) if isinstance(o, dict) and o.get("id")}

    def subj_of(rec: dict) -> URIRef:
        return URIRef(f"http://ontology.local/inst_{rec['id']}")

    # (对象类型, stable_key 值) → 实例主体，用于关系值解析
    key_index: dict[tuple, URIRef] = {}
    for rec in instances:
        obj = objects.get(rec.get("object_type"))
        sk = obj.get("stable_key") if obj else None
        val = (rec.get("instance") or {}).get(sk) if sk else None
        if val is not None:
            key_index[(rec["object_type"], str(val))] = subj_of(rec)

    for rec in instances:
        obj = objects.get(rec.get("object_type"))
        if not obj or not rec.get("id"):
            continue
        inst = rec.get("instance") or {}
        s = subj_of(rec)
        g.add((s, RDF.type, URIRef(class_uri(obj["id"]))))
        sk_val = inst.get(obj["stable_key"]) if obj.get("stable_key") else None
        if sk_val is not None:
            g.add((s, RDFS.label, Literal(str(sk_val))))

        attr_ids = {a["id"]: a for a in obj.get("attributes", []) if isinstance(a, dict) and a.get("id")}
        rel_ids = {r["id"]: r for r in obj.get("relations", []) if isinstance(r, dict) and r.get("id")}
        for k, v in inst.items():
            if k in _RESERVED_KEYS or v is None:
                continue
            if k in attr_ids:
                g.add((s, URIRef(attr_uri(k)), _typed_literal(v, attr_ids[k].get("data_type", "string"))))
            elif k in rel_ids:
                target_obj = rel_ids[k].get("target_object")
                resolved = key_index.get((target_obj, str(v))) if target_obj else None
                g.add((s, URIRef(rel_uri(k)), resolved if resolved else Literal(str(v))))
    return g


def build_full_graph(ontology: dict, instances: list[dict]) -> Graph:
    """schema 图 + 实例图（与 translate_to_turtle 同一 URI 命名空间约定）。"""
    g = Graph()
    g.parse(data=translate_to_turtle(ontology).encode("utf-8"), format="turtle")
    g += build_instance_triples(ontology, instances)
    return g


def _replace_named_graph(graph_uri: str, nt_triples: str) -> None:
    """替换指定命名图内容：清空 → INSERT DATA（N-Triples 无前缀，数据块内必然合法）。"""
    settings = get_settings()
    dataset = settings.jena_dataset
    url = f"{settings.jena_base_url.rstrip('/')}/{dataset}/update"
    update = (
        f"DELETE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o . }} }} WHERE {{}} ;\n"
        f"INSERT DATA {{ GRAPH <{graph_uri}> {{\n{nt_triples.strip()}\n}} }}"
    )
    req = urllib.request.Request(
        url, data=update.encode("utf-8"), method="POST",
        headers={"Content-Type": "application/sparql-update"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"Fuseki update 失败 status={resp.status}")


def sync_ontology(project_id: str, ontology_id: str) -> dict:
    """重建本体全量图并写入 RDF 存储。返回同步摘要。"""
    store = JsonStore("ontologies", project_id=project_id)
    rec = store.get(ontology_id)
    if not rec:
        raise ValueError(f"本体不存在: {ontology_id}")
    instances = JsonlInstanceStore(project_id=project_id).find(
        filters={"ontology_id": ontology_id}, limit=100000)
    g = build_full_graph(rec, instances)

    if _is_reachable():
        _replace_named_graph(graph_uri_for(ontology_id), g.serialize(format="nt"))
        result: dict = {"mode": REMOTE_MODE, "dataset": get_settings().jena_dataset,
                        "graph": graph_uri_for(ontology_id)}
        logger.info("rdf_sync remote ontology_id=%s project_id=%s triples=%d instances=%d",
                    ontology_id, project_id, len(g), len(instances))
    else:
        settings = get_settings()
        base = os.path.join(settings.data_dir, "projects", project_id) if project_id else settings.data_dir
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, f"ontology_{ontology_id}.ttl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(g.serialize(format="turtle"))
        result = {"mode": FILE_MODE, "path": path}
        logger.info("rdf_sync file ontology_id=%s project_id=%s triples=%d instances=%d path=%s",
                    ontology_id, project_id, len(g), len(instances), path)

    result.update({
        "triples": len(g),
        "instances": len(instances),
        "objects": len([o for o in rec.get("objects", []) if isinstance(o, dict) and o.get("id")]),
    })
    return result


def sync_all_projects(project_id: str | None = None) -> list[dict]:
    """全量回填：指定（或全部）项目下所有本体重建并写入。单条失败不中断。"""
    settings = get_settings()
    projects_dir = os.path.join(settings.data_dir, "projects")
    if project_id:
        pids = [project_id]
    elif os.path.isdir(projects_dir):
        pids = sorted(d for d in os.listdir(projects_dir)
                      if os.path.isdir(os.path.join(projects_dir, d)))
    else:
        pids = []

    results = []
    for pid in pids:
        for rec in JsonStore("ontologies", project_id=pid).all():
            item = {"project_id": pid, "ontology_id": rec.get("id"), "name": rec.get("name")}
            try:
                item.update(sync_ontology(pid, rec["id"]))
            except Exception as e:  # noqa: BLE001 - 回填需逐条容错
                item["error"] = str(e)
                logger.warning("rdf_sync 回填失败 project_id=%s ontology_id=%s: %s",
                               pid, rec.get("id"), e)
            results.append(item)
    return results
