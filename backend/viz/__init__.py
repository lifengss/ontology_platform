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
        max_instances: int = 300,
    ) -> dict[str, Any]:
        """返回 schema 或 knowledge 视图数据。

        max_instances：knowledge 视图实例抽样上限（0 = 不限制）。
        全量实例入图（如 2297 节点 × 300 tick 力导向）首屏必然卡顿且炸开，
        默认按对象类型均匀抽样，元信息见 truncated/total_instances/shown。
        """
        schema_nodes, schema_edges = self._schema_graph(ontology)
        kg_nodes, kg_edges, kg_meta = self._knowledge_graph(
            ontology_id, ontology, schema_nodes, schema_edges, project_id, max_instances=max_instances
        )
        nodes, edges = (kg_nodes, kg_edges) if mode == "knowledge" else (schema_nodes, schema_edges)
        return {
            "mode": mode,
            "nodes": nodes,
            "edges": edges,
            **kg_meta,
            "tabs": {
                "schema": {"nodes": schema_nodes, "edges": schema_edges},
                "knowledge": {"nodes": kg_nodes, "edges": kg_edges, **kg_meta},
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
        max_instances: int = 300,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """knowledge 视图 = schema 骨架 + 实例节点 + instance_of 边。

        max_instances > 0 时按对象类型均匀抽样（轮询，每对象最多 per_object 条），
        返回值第三项为抽样元信息 {truncated, total_instances, shown}。
        """
        nodes = [dict(n) for n in schema_nodes]
        edges = [dict(e) for e in schema_edges]
        node_ids = {n["id"] for n in nodes}

        instances = self._load_instances(ontology_id, project_id)
        total_instances = len(instances)
        if max_instances and 0 < max_instances < total_instances:
            instances = self._sample_instances(instances, max_instances)
        meta = {
            "truncated": len(instances) < total_instances,
            "total_instances": total_instances,
            "shown": len(instances),
        }
        for tx_idx, rec in enumerate(instances):
            inst_id, node, edge = self._instance_entry(rec, tx_idx)
            object_type = node["object_type"]
            if object_type and object_type not in node_ids:
                node_ids.add(object_type)
                nodes.append({
                    "id": object_type,
                    "label": object_type,
                    "kind": "object",
                    "source": _INTERNAL,
                    "missing": True,
                    "note": "该对象类型仅有实例数据，尚未在建模侧定义",
                })
            nodes.append(node)
            if edge:
                edges.append(edge)
        return nodes, edges, meta

    def _instance_entry(self, rec: dict[str, Any], tx_idx: int) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        """构造单个实例的节点与 instance_of 边（id 口径单一来源）。

        返回 (inst_id, node, edge)；edge 为 None 表示实例缺少 object_type。
        """
        object_type = rec.get("object_type")
        inst = rec.get("instance", {})
        raw_source = rec.get("source") or inst.get("_source", {})
        source_type = raw_source if isinstance(raw_source, str) else (raw_source.get("kind") or raw_source.get("source_type"))
        origin = _origin_from_source(source_type)
        tx_id = rec.get("tx_id", f"tx_{tx_idx}")

        # 实例节点 id：优先使用实例自带的 id，否则按 tx 内索引生成
        inst_id = inst.get("id") or f"inst_{tx_id}_{tx_idx}"
        node = {
            "id": inst_id,
            "label": self._instance_label(inst, object_type),
            "kind": "instance",
            "object_type": object_type,
            "source": origin,
            "tx_id": tx_id,
        }
        edge = None
        if object_type:
            edge = {
                "id": f"instance_of_{inst_id}",
                "source": inst_id,
                "target": object_type,
                "label": "instance_of",
                "kind": "instance_of",
                "origin": origin,
            }
        return inst_id, node, edge

    def neighbors(
        self,
        ontology_id: str,
        ontology: dict[str, Any],
        node_id: str,
        mode: str = "schema",
        project_id: str | None = None,
        depth: int = 1,
        limit: int = 50,
    ) -> dict[str, Any] | None:
        """按需邻居查询（GAP-12）：避免大图一次拉全图。

        - 对象节点：沿关系边 BFS（depth 1~2，无向）；knowledge 视图附焦点对象的实例抽样。
        - 实例节点：所属对象节点＋同对象其他实例抽样（limit 截断）。
        返回 {node, nodes, edges, truncated, total}；节点不存在返回 None（路由层 404）。
        """
        schema_nodes, schema_edges = self._schema_graph(ontology)
        schema_by_id = {n["id"]: n for n in schema_nodes}
        depth = max(1, min(int(depth or 1), 2))
        limit = max(0, int(limit or 0))

        focus = schema_by_id.get(node_id)
        if focus is not None:
            # 对象节点：关系边无向 BFS
            visited = {node_id}
            frontier = [node_id]
            edges_out: list[dict[str, Any]] = []
            for _ in range(depth):
                nxt: list[str] = []
                for nid in frontier:
                    for e in schema_edges:
                        if e.get("kind") != "relation":
                            continue
                        other = e["target"] if e["source"] == nid else (e["source"] if e["target"] == nid else None)
                        if other and other not in visited:
                            visited.add(other)
                            nxt.append(other)
                            edges_out.append(e)
                if not nxt:
                    break
                frontier = nxt
            nodes_out = [dict(schema_by_id[i]) for i in visited if i != node_id]
            truncated = False
            total = len(nodes_out)
            if mode == "knowledge":
                records = self._load_instances(ontology_id, project_id)
                entries = [(rec, *self._instance_entry(rec, idx)) for idx, rec in enumerate(records)]
                entries = [t for t in entries if t[0].get("object_type") == node_id]
                entries.sort(key=lambda t: str((t[0].get("instance") or {}).get("id") or t[0].get("tx_id") or ""))
                total = len(entries)
                if limit and total > limit:
                    entries = entries[:limit]
                    truncated = True
                for _rec, _iid, inode, iedge in entries:
                    nodes_out.append(inode)
                    if iedge:
                        edges_out.append(iedge)
            return {"node": focus, "nodes": nodes_out, "edges": edges_out, "truncated": truncated, "total": total}

        # 实例节点：所属对象＋同对象其他实例抽样
        records = self._load_instances(ontology_id, project_id)
        entries = [(rec, *self._instance_entry(rec, idx)) for idx, rec in enumerate(records)]
        focus_t = next((t for t in entries if t[1] == node_id), None)
        if focus_t is None:
            return None
        focus_rec, _fid, focus_node, focus_edge = focus_t
        nodes_out: list[dict[str, Any]] = []
        edges_out: list[dict[str, Any]] = [focus_edge] if focus_edge else []
        object_type = focus_rec.get("object_type")
        truncated = False
        total = 0
        if object_type:
            if object_type in schema_by_id:
                nodes_out.append(dict(schema_by_id[object_type]))
            else:
                nodes_out.append({
                    "id": object_type, "label": object_type, "kind": "object",
                    "source": _INTERNAL, "missing": True,
                    "note": "该对象类型仅有实例数据，尚未在建模侧定义",
                })
            siblings = [t for t in entries if t[0].get("object_type") == object_type]
            siblings.sort(key=lambda t: str((t[0].get("instance") or {}).get("id") or t[0].get("tx_id") or ""))
            total = len(siblings)
            if limit and total > limit:
                siblings = siblings[:limit]
                truncated = True
            for _rec, sid, snode, sedge in siblings:
                if sid == node_id:
                    continue
                nodes_out.append(snode)
                if sedge:
                    edges_out.append(sedge)
        return {"node": focus_node, "nodes": nodes_out, "edges": edges_out, "truncated": truncated, "total": total}

    # knowledge 视图抽样：每对象最多 _PER_OBJECT_LIMIT 条，全局 max_total 条
    _PER_OBJECT_LIMIT = 50

    def _sample_instances(
        self,
        instances: list[dict[str, Any]],
        max_total: int,
        per_object: int | None = None,
    ) -> list[dict[str, Any]]:
        """按对象类型均匀抽样（round-robin 轮询，确定性可复现）。

        大 hub 对象（如一个对象下挂 1372 实例）无法在力导向里紧凑，
        均匀抽样保证各对象都有代表节点入图，而不是被字母序靠前的对象占满。
        """
        if per_object is None:
            per_object = self._PER_OBJECT_LIMIT
        buckets: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for rec in instances:
            ot = rec.get("object_type") or "__unknown__"
            if ot not in buckets:
                buckets[ot] = []
                order.append(ot)
            buckets[ot].append(rec)
        # 桶内按稳定键排序，保证多次请求抽样结果一致
        for ot in order:
            buckets[ot].sort(key=lambda r: str((r.get("instance") or {}).get("id") or r.get("tx_id") or ""))
        chosen: list[dict[str, Any]] = []
        idx = {ot: 0 for ot in order}
        while len(chosen) < max_total:
            progressed = False
            for ot in order:
                if len(chosen) >= max_total:
                    break
                i = idx[ot]
                lst = buckets[ot]
                if i >= len(lst) or i >= per_object:
                    continue
                chosen.append(lst[i])
                idx[ot] = i + 1
                progressed = True
            if not progressed:
                break
        return chosen

    def _load_instances(self, ontology_id: str, project_id: str | None = None) -> list[dict[str, Any]]:
        """读取实例：以新 InstanceStore 为权威（前端 list/get/edit/delete 皆用它），
        仅当权威为空时回退旧 Neo4j 降级产物（历史数据），避免同一实例被读成两套。
        按 ontology_id 过滤。"""
        # 权威：按对象类型分文件的 JSONL（GAP-17：只读快照路径，免每请求全量物化拷贝；
        # records 仅做过滤/构造图节点，不修改记录内容）
        records: list[dict[str, Any]] = []
        try:
            for rec in get_instance_store(project_id).snapshot()[1]:
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
