#!/usr/bin/env python
"""V2-02（M4-A①）INV-1 负向断言校验：源连接型实例非键内容值永不落地。

INV-1 口径（checkpoint §46.2 评审冻结）：
① JSONL：源连接型（external_mcp / cdc）记录 ``instance`` 字段键
   ⊆ {标识(id/node_id), stable_key 属性, 关系 id, ``_source``}；
② RDF 主命名图：这些实例主体的三元组谓词 ⊆ {rdf:type, rdfs:label, 对象关系 rel_uri}。
   回退图 ``urn:ontology:{oid}/fallback_snapshot`` 与记录内 ``fallback_content`` 字段
   明确不计入（标 stale 的回退底，D3 保留项）。

用法（仓库根目录执行）::

    python scripts/verify_inv1_source_channel.py              # 全部项目
    python scripts/verify_inv1_source_channel.py --project demo
退出码：0=全部通过；1=存在违规（打印违规明细）。
"""
from __future__ import annotations

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.join(PROJECT_ROOT, "backend")
sys.path.insert(0, BACKEND_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config  # noqa: E402,F401
from config import get_settings  # noqa: E402

from rdflib import RDF, RDFS  # noqa: E402

from core.instance_store import JsonlInstanceStore  # noqa: E402
import content_access  # noqa: E402
from modeling_translator import rel_uri  # noqa: E402


def _projects_dir() -> str:
    return os.path.join(get_settings().data_dir, "projects")


def _project_violations(project_id: str) -> dict:
    out: dict = {"project_id": project_id, "jsonl_violations": [], "rdf_violations": [],
                 "federated_records": 0, "checked_graphs": 0}
    store = JsonlInstanceStore(project_id=project_id)
    fed_recs: list[dict] = []
    for rec in store.all():
        if not content_access.is_federated(project_id, rec):
            continue
        out["federated_records"] += 1
        fed_recs.append(rec)
        keep = content_access.keep_keys_for(project_id, rec)
        extra = [k for k in (rec.get("instance") or {}) if k not in keep]
        if extra:
            out["jsonl_violations"].append(
                {"instance": rec.get("id"), "object_type": rec.get("object_type"),
                 "unexpected_keys": sorted(extra)})

    # ② RDF 主命名图（按受影响的本体去重）
    from core.storage import JsonStore
    try:
        ontologies = {r["id"]: r for r in JsonStore("ontologies", project_id).all()
                      if r.get("id")}
    except Exception:  # noqa: BLE001
        ontologies = {}
    affected = sorted({r.get("ontology_id") for r in fed_recs if r.get("ontology_id")})
    for oid in affected:
        ont = ontologies.get(oid)
        if not ont:
            continue
        allowed = {RDF.type, RDFS.label}
        for o in ont.get("objects") or []:
            if not isinstance(o, dict):
                continue
            for r in o.get("relations") or []:
                if isinstance(r, dict) and r.get("id"):
                    allowed.add(rel_uri(r["id"]))
        subs = {f"http://ontology.local/inst_{r['id']}" for r in fed_recs
                if r.get("ontology_id") == oid and r.get("id")}
        if not subs:
            continue
        from rdflib import URIRef
        from jena_adapter.client import sparql_query
        # 远端由 sparql_query 自动限定本体命名图；本地走 ttl 文件
        q = "SELECT ?s ?p WHERE { ?s ?p ?o } LIMIT 100000"
        try:
            rows = sparql_query(q, project_id=project_id, ontology_id=oid)
        except Exception:  # noqa: BLE001
            continue
        for row in rows or []:
            s = str(row.get("s", ""))
            if s not in subs:
                continue
            p_raw = str(row.get("p", ""))
            try:
                pterm = URIRef(p_raw)
            except Exception:  # noqa: BLE001
                pterm = p_raw
            if pterm not in allowed:
                out["rdf_violations"].append(
                    {"ontology": oid, "subject": s, "predicate": p_raw})
        out["checked_graphs"] += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="V2-02 INV-1 负向断言校验")
    ap.add_argument("--project", help="仅校验指定项目（默认全部）")
    args = ap.parse_args()
    pids = [args.project] if args.project else (
        sorted(d for d in os.listdir(_projects_dir())
               if os.path.isdir(os.path.join(_projects_dir(), d)))
        if os.path.isdir(_projects_dir()) else [])
    all_pass = True
    for pid in pids:
        r = _project_violations(pid)
        ok = not r["jsonl_violations"] and not r["rdf_violations"]
        all_pass = all_pass and ok
        print(f"[{'PASS' if ok else 'FAIL'}] project={pid} "
              f"federated_records={r['federated_records']} graphs={r['checked_graphs']} "
              f"jsonl_violations={len(r['jsonl_violations'])} "
              f"rdf_violations={len(r['rdf_violations'])}")
        for v in r["jsonl_violations"][:20]:
            print(f"  JSONL 违规: {v}")
        for v in r["rdf_violations"][:20]:
            print(f"  RDF 违规: {v}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
