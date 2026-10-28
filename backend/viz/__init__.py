"""viz：三视图数据（本体视图 / 知识图谱视图(外部溯源着色) / 推理链路视图）。VS4。"""
from __future__ import annotations

import json
import os
from typing import Any

from config import get_settings
from core.instance_store import get_instance_store
from logging_setup import get_logger

logger = get_logger("viz")

# 来源着色：内部建模 vs 外部归化
_INTERNAL = "internal"
_EXTERNAL = "external_mcp"


def _origin_from_source(source_type: str | None) -> str:
    """归化来源统一标记为 external_mcp，便于前端着色。

    兼容旧字段 source_type 与新字段 kind。
    """
    if source_type in ("llm", "field_map", "external_mcp"):
        return _EXTERNAL
    return _INTERNAL


class Viz:
    def graph_data(
        self,
        ontology_id: str,
        ontology: dict[str, Any],
        mode: str = "schema",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """返回 schema 或 knowledge 视图数据。"""
        schema_nodes, schema_edges = self._schema_graph(ontology)
        kg_nodes, kg_edges = self._knowledge_graph(ontology_id, ontology, schema_nodes, schema_edges, project_id)
        nodes, edges = (kg_nodes, kg_edges) if mode == "knowledge" else (schema_nodes, schema_edges)
        return {
            "mode": mode,
            "nodes": nodes,
            "edges": edges,
            "tabs": {
                "schema": {"nodes": schema_nodes, "edges": schema_edges},
                "knowledge": {"nodes": kg_nodes, "edges": kg_edges},
            },
            "source_legend": [
                {"source": _INTERNAL, "label": "内部建模", "color": "#4f8cff"},
                {"source": _EXTERNAL, "label": "外部归化", "color": "#ff8a4f"},
            ],
        }

    def _schema_graph(self, ontology: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        object_ids = {o["id"] for o in ontology.get("objects", [])}
        for o in ontology.get("objects", []):
            nodes.append({
                "id": o["id"],
                "label": o.get("name", o["id"]),
                "kind": "object",
                "source": _INTERNAL,
                # 父类（对象继承）：供前端"类树"视图构建层级
                "parent": o.get("parent"),
            })
            for rel in o.get("relations", []):
                target = rel.get("target_object")
                if target and target not in object_ids:
                    # 关系中引用了未建模对象，生成占位节点避免图谱断裂
                    object_ids.add(target)
                    nodes.append({
                        "id": target,
                        "label": target,
                        "kind": "object",
                        "source": _INTERNAL,
                        "missing": True,
                        "note": "该对象仅被关系引用，尚未在建模侧定义",
                    })
                edges.append({
                    "id": rel["id"],
                    "source": o["id"],
                    "target": target,
                    "label": rel.get("name", rel["id"]),
                    "kind": "relation",
                    "origin": _INTERNAL,
                })
        return nodes, edges

    def _knowledge_graph(
        self,
        ontology_id: str,
        ontology: dict[str, Any],
        schema_nodes: list[dict[str, Any]],
        schema_edges: list[dict[str, Any]],
        project_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # knowledge 视图以 schema 为骨架，叠加实例节点与 instance_of 关系
        nodes = [dict(n) for n in schema_nodes]
        edges = [dict(e) for e in schema_edges]
        node_ids = {n["id"] for n in nodes}

        instances = self._load_instances(ontology_id, project_id)
        for tx_idx, rec in enumerate(instances):
            object_type = rec.get("object_type")
            inst = rec.get("instance", {})
            raw_source = rec.get("source") or inst.get("_source", {})
            source_type = raw_source if isinstance(raw_source, str) else (raw_source.get("kind") or raw_source.get("source_type"))
            origin = _origin_from_source(source_type)
            tx_id = rec.get("tx_id", f"tx_{tx_idx}")

            # 实例节点 id：优先使用实例自带的 id，否则按 tx 内索引生成
            inst_id = inst.get("id") or f"inst_{tx_id}_{tx_idx}"
            nodes.append({
                "id": inst_id,
                "label": self._instance_label(inst, object_type),
                "kind": "instance",
                "object_type": object_type,
                "source": origin,
                "tx_id": tx_id,
            })
            if object_type:
                # 若实例所属对象未在 schema 中定义，补占位节点
                if object_type not in node_ids:
                    node_ids.add(object_type)
                    nodes.append({
                        "id": object_type,
                        "label": object_type,
                        "kind": "object",
                        "source": _INTERNAL,
                        "missing": True,
                        "note": "该对象类型仅有实例数据，尚未在建模侧定义",
                    })
                edges.append({
                    "id": f"instance_of_{inst_id}",
                    "source": inst_id,
                    "target": object_type,
                    "label": "instance_of",
                    "kind": "instance_of",
                    "origin": origin,
                })
        return nodes, edges

    def _load_instances(self, ontology_id: str, project_id: str | None = None) -> list[dict[str, Any]]:
        """读取实例：以新 InstanceStore 为权威（前端 list/get/edit/delete 皆用它），
        仅当权威为空时回退旧 Neo4j 降级产物（历史数据），避免同一实例被读成两套。
        按 ontology_id 过滤。"""
        # 权威：按对象类型分文件的 JSONL
        records: list[dict[str, Any]] = []
        try:
            for rec in get_instance_store(project_id).all():
                if rec.get("ontology_id") == ontology_id:
                    records.append(rec)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 InstanceStore 失败: %s", e)
        if records:
            return records

        # 回退：旧存储兼容（项目级单文件 instances.jsonl / instances.json）
        seen_ids: set[str] = set()
        base_dir = os.path.join(get_settings().data_dir, "projects", project_id or "default")
        paths = [
            os.path.join(base_dir, "instances.jsonl"),
            os.path.join(base_dir, "instances.json"),
            os.path.join(get_settings().data_dir, "instances.jsonl"),
            os.path.join(get_settings().data_dir, "instances.json"),
        ]
        for path in paths:
            if not os.path.exists(path):
                continue
            try:
                if path.endswith(".json"):
                    with open(path, "r", encoding="utf-8") as f:
                        batch = json.load(f)
                    if isinstance(batch, list):
                        for rec in batch:
                            if rec.get("ontology_id") == ontology_id:
                                rid = rec.get("id") or json.dumps(rec.get("instance"), sort_keys=True, ensure_ascii=False)
                                if rid not in seen_ids:
                                    seen_ids.add(rid)
                                    records.append(rec)
                else:
                    with open(path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            rec = json.loads(line)
                            if rec.get("ontology_id") == ontology_id:
                                rid = rec.get("id") or json.dumps(rec.get("instance"), sort_keys=True, ensure_ascii=False)
                                if rid not in seen_ids:
                                    seen_ids.add(rid)
                                    records.append(rec)
            except Exception as e:  # noqa: BLE001
                logger.warning("读取 %s 失败: %s", path, e)
        return records

    def _instance_label(self, inst: dict[str, Any], object_type: str | None) -> str:
        """从实例属性中找一个可读的标签。"""
        # 候选属性：name / title / label / 第一个非元数据字符串属性
        for key in ("name", "title", "label", "attr_name"):
            if key in inst and isinstance(inst[key], str):
                return inst[key]
        for k, v in inst.items():
            if not k.startswith("_") and k != "object_type" and isinstance(v, str):
                return v
        return object_type or "instance"
