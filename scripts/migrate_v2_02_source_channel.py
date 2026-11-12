#!/usr/bin/env python
"""V2-02（M4-A①）存量源连接型实例迁移脚本（方案 D3，用户 2026-11-12 定稿）。

对存量**源连接型**实例（external_mcp 审批入库 / CDC 流式）做一次性归一化：

1. 备份：迁移前完整复制 ``data/projects/<pid>/instances/`` 目录（可原样还原）。
2. JSONL 剥离：非键内容值（排除 标识/stable_key/关系边/``_source`` 键域）移入记录内
   ``fallback_content``（回退底，明确非 ``instance`` 字段），标 ``content_as_of``，
   补显式 ``channel`` 与 ``content_policy=federated``。
3. RDF 回退图（仅 Fuseki 可达时）：这些实例的非键值三元组从主命名图移入
   ``urn:ontology:{oid}/fallback_snapshot`` 回退图（不参与常规查询/推理/图谱），
   随后 ``sync_ontology`` 重建主命名图（值不再出现在主图，INV-1 ②）。
   文件降级模式：主图（ttl）由 sync_ontology 重写即自动无值，回退底以 JSONL 为准。
4. 审计：每项目一条 ``v2_02_migration`` 事件（迁移量/备份/结果）。

幂等：已迁移记录（无非键值 + 已标记）跳过；回退图先按 subject 清空再写入。

用法（仓库根目录执行）::

    python scripts/migrate_v2_02_source_channel.py                    # 全部项目
    python scripts/migrate_v2_02_source_channel.py --project demo     # 指定项目
    python scripts/migrate_v2_02_source_channel.py --dry-run          # 只报告不写
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.join(PROJECT_ROOT, "backend")
sys.path.insert(0, BACKEND_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config  # noqa: E402,F401
from config import get_settings  # noqa: E402

from core.storage import JsonStore  # noqa: E402
from core.instance_store import JsonlInstanceStore  # noqa: E402
import audit as audit_svc  # noqa: E402
import content_access  # noqa: E402
from jena_adapter.rdf_sync import graph_uri_for, sync_ontology  # noqa: E402
from modeling_translator import attr_uri, typed_literal  # noqa: E402

FALLBACK_SUFFIX = "/fallback_snapshot"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _projects_dir() -> str:
    return os.path.join(get_settings().data_dir, "projects")


def _jena_update(update: str) -> bool:
    """执行 SPARQL UPDATE；Fuseki 不可达返回 False（文件降级，由调用方处理）。"""
    s = get_settings()
    url = f"{s.jena_base_url.rstrip('/')}/{s.jena_dataset}/update"
    req = urllib.request.Request(
        url, data=update.encode("utf-8"), method="POST",
        headers={"Content-Type": "application/sparql-update"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status < 400
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def _non_key_value_triples(rec: dict, non_key: dict, obj: dict | None) -> str:
    """旧非键值 → N-Triples（与 build_instance_triples 的写法一致：attr_uri + typed_literal）。

    只重建"当时会写入主图"的三元组：属性在对象 attributes 内且值非 None。
    关系（obj.relations）保留主图不迁移；未在定义中的键当时就没写图。
    """
    if not obj:
        return ""
    attr_dt = {a.get("id"): a.get("data_type", "string")
               for a in (obj.get("attributes") or []) if isinstance(a, dict) and a.get("id")}
    subj = f"http://ontology.local/inst_{rec.get('id')}"
    lines = []
    for k, v in non_key.items():
        if v is None or k not in attr_dt:
            continue
        lines.append(f"<{subj}> <{attr_uri(k)}> {typed_literal(v, attr_dt[k]).n3()} .")
    return "\n".join(lines)


def migrate_project(project_id: str, *, dry_run: bool = False) -> dict:
    report: dict = {
        "project_id": project_id, "dry_run": dry_run, "ts": _now(),
        "backup": None, "scanned": 0, "federated": 0,
        "migrated_records": 0, "records_with_values": 0,
        "values_moved": 0, "rdf": [], "skipped": [],
    }
    store = JsonlInstanceStore(project_id=project_id)
    recs = store.all()
    report["scanned"] = len(recs)
    ontologies: dict = {}
    try:
        ontologies = {r["id"]: r for r in JsonStore("ontologies", project_id).all() if r.get("id")}
    except Exception:  # noqa: BLE001
        pass

    # 1) 备份（迁移前完整副本）
    if not dry_run:
        inst_dir = os.path.join(_projects_dir(), project_id, "instances")
        if os.path.isdir(inst_dir):
            ts = time.strftime("%Y%m%d_%H%M%S")
            backup = f"{inst_dir}_backup_v2_02_{ts}"
            shutil.copytree(inst_dir, backup)
            report["backup"] = backup

    # 2) JSONL 剥离
    moved: list[tuple[dict, dict, str]] = []  # (new_rec, non_key, channel)
    for rec in recs:
        ch = content_access.channel_of(project_id, rec)
        if ch not in content_access.FEDERATED_CHANNELS:
            continue
        report["federated"] += 1
        inst = rec.get("instance") or {}
        keep = content_access.keep_keys_for(project_id, rec)
        non_key = {k: v for k, v in inst.items() if k not in keep}
        already = (rec.get("channel") == ch and rec.get("content_policy") == "federated"
                   and not non_key)
        if already:
            report["skipped"].append({"id": rec.get("id"), "reason": "already_migrated"})
            continue
        new_rec = dict(rec)
        new_rec["instance"] = {k: v for k, v in inst.items() if k in keep}
        if non_key:
            merged_fb = dict(rec.get("fallback_content") or {})
            merged_fb.update(non_key)
            new_rec["fallback_content"] = merged_fb
            new_rec["content_as_of"] = rec.get("imported_at") or _now()
            report["records_with_values"] += 1
            report["values_moved"] += len(non_key)
        new_rec["channel"] = ch
        new_rec["content_policy"] = "federated"
        if not dry_run:
            store.update(rec["id"], new_rec)
        report["migrated_records"] += 1
        moved.append((new_rec, non_key, ch))

    # 3) RDF 回退图 + 主图重建
    affected_onts = sorted({r.get("ontology_id") for r, _, _ in moved if r.get("ontology_id")})
    if not dry_run:
        from jena_adapter.client import health as jena_health
        try:
            reachable = jena_health().get("jena") == "healthy"
        except Exception:  # noqa: BLE001
            reachable = False
        if reachable:
            for oid in affected_onts:
                ont = ontologies.get(oid)
                objects = {o.get("id"): o for o in (ont.get("objects") or [])
                           if isinstance(o, dict) and o.get("id")} if ont else {}
                fallback_graph = graph_uri_for(oid) + FALLBACK_SUFFIX
                delete_parts, insert_parts = [], []
                for rec, non_key, _ch in moved:
                    if rec.get("ontology_id") != oid or not non_key:
                        continue
                    nt = _non_key_value_triples(rec, non_key, objects.get(rec.get("object_type")))
                    if not nt:
                        continue
                    subj = f"http://ontology.local/inst_{rec.get('id')}"
                    delete_parts.append(
                        f"DELETE WHERE {{ GRAPH <{fallback_graph}> {{ <{subj}> ?p ?o . }} }}")
                    insert_parts.append(
                        f"INSERT DATA {{ GRAPH <{fallback_graph}> {{\n{nt}\n}} }}")
                if delete_parts or insert_parts:
                    update = " ;\n".join(delete_parts + insert_parts)
                    ok = _jena_update(update)
                    report["rdf"].append({"ontology_id": oid, "fallback_moved": ok})
                # 主图重建（值消失，INV-1 ②）
                try:
                    r = sync_ontology(project_id, oid)
                    report["rdf"].append({"ontology_id": oid, "main_rebuilt": True,
                                          "triples": r.get("triples")})
                except Exception as e:  # noqa: BLE001
                    report["rdf"].append({"ontology_id": oid, "main_rebuilt": False,
                                          "error": str(e)})
        else:
            for oid in affected_onts:
                # 文件降级：ttl 全量重写即无值；回退底以 JSONL fallback_content 为准
                try:
                    r = sync_ontology(project_id, oid)
                    report["rdf"].append({"ontology_id": oid, "mode": "file",
                                          "fallback_graph": "skipped(jsonl)",
                                          "triples": r.get("triples")})
                except Exception as e:  # noqa: BLE001
                    report["rdf"].append({"ontology_id": oid, "error": str(e)})

    # 4) 审计
    if not dry_run:
        try:
            audit_svc.record(
                action="v2_02_migration", actor="platform", actor_role="governance",
                project_id=project_id, target_type="instances",
                target_id=f"v2_02:{project_id}",
                detail={"migrated_records": report["migrated_records"],
                        "records_with_values": report["records_with_values"],
                        "values_moved": report["values_moved"],
                        "backup": report["backup"], "rdf": report["rdf"]},
            )
        except Exception:  # noqa: BLE001
            pass
        rpt_path = os.path.join(_projects_dir(), project_id,
                                f"v2_02_migration_report_{time.strftime('%Y%m%d_%H%M%S')}.json")
        with open(rpt_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        report["report_path"] = rpt_path
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="V2-02 存量源连接型实例迁移（D3）")
    ap.add_argument("--project", help="仅迁移指定项目（默认全部）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写任何数据")
    args = ap.parse_args()

    pids = [args.project] if args.project else (
        sorted(d for d in os.listdir(_projects_dir())
               if os.path.isdir(os.path.join(_projects_dir(), d)))
        if os.path.isdir(_projects_dir()) else [])
    if not pids:
        print("未发现项目（data/projects 为空）")
        return 0
    all_reports = [migrate_project(pid, dry_run=args.dry_run) for pid in pids]
    print(json.dumps(all_reports, ensure_ascii=False, indent=2))
    bad = [r for r in all_reports if r["migrated_records"] and "error" in json.dumps(r["rdf"])]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
