"""viz：三视图数据（本体视图 / 知识图谱视图(外部溯源着色) / 推理链路视图）。VS4。"""
from __future__ import annotations

import json
import os
from typing import Any

from config import get_settings
from logging_setup import get_logger

logger = get_logger("viz")

# 来源着色：内部建模 vs 外部归化
_INTERNAL = "internal"
_EXTERNAL = "external_mcp"


def _origin_from_source(source_type: str | None) -> str:
    """归化来源统一标记为 external_mcp，便于前端着色。"""
    if source_type in ("llm", "field_map", "external_mcp"):
        return _EXTERNAL
    return _INTERNAL


class Viz:
    def graph_data(self, ontology_id: str, ontology: dict[str, Any]) -> dict[str, Any]:
        """返回合并图谱 + schema/knowledge 分 tab。"""
        schema_nodes, schema_edges = self._schema_graph(ontology)
        kg_nodes, kg_edges = self._knowledge_graph(ontology_id, ontology, schema_nodes, schema_edges)
        return {
            "nodes": kg_nodes,
            "edges": kg_edges,
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
        for o in ontology.get("objects", []):
            nodes.append({
                "id": o["id"],
                "label": o.get("name", o["id"]),
                "kind": "object",
                "source": _INTERNAL,
            })
            for rel in o.get("relations", []):
                edges.append({
                    "id": rel["id"],
                    "source": o["id"],
                    "target": rel.get("target_object"),
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
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        nodes = [dict(n) for n in schema_nodes]
        edges = [dict(e) for e in schema_edges]

        instances = self._load_instances(ontology_id)
        for tx_idx, rec in enumerate(instances):
            object_type = rec.get("object_type")
            inst = rec.get("instance", {})
            source_type = rec.get("source") or inst.get("_source", {}).get("source_type")
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
                edges.append({
                    "id": f"instance_of_{inst_id}",
                    "source": inst_id,
                    "target": object_type,
                    "label": "instance_of",
                    "kind": "instance_of",
                    "origin": origin,
                })
        return nodes, edges

    def _load_instances(self, ontology_id: str) -> list[dict[str, Any]]:
        """读取 Neo4j 文件降级产物 data/instances.jsonl（VS3 落库），按 ontology_id 过滤。"""
        path = os.path.join(get_settings().data_dir, "instances.jsonl")
        if not os.path.exists(path):
            return []
        records: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("ontology_id") == ontology_id:
                        records.append(rec)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 instances.jsonl 失败: %s", e)
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
