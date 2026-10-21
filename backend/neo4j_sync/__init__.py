"""neo4j_sync：ABox 实例写入/读取（TBox 在 Jena，ABox 在 Neo4j）。

VS3 落库目标。生产连接真实 Neo4j；本地开发若 Neo4j 未启动，**优雅降级为 JSONL 文件模式**，保证 VS3 可独立验证。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from config import get_settings
from logging_setup import get_logger

logger = get_logger("neo4j_sync")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Neo4jSync:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._reachable: bool | None = None

    def _is_reachable(self) -> bool:
        if self._reachable is not None:
            return self._reachable
        try:
            import neo4j  # noqa: F401
        except ImportError:
            self._reachable = False
            return False
        try:
            from neo4j import GraphDatabase

            with GraphDatabase.driver(
                self.settings.neo4j_uri,
                auth=(self.settings.neo4j_user, self.settings.neo4j_password),
            ) as driver:
                driver.verify_connectivity()
            self._reachable = True
        except Exception as e:  # noqa: BLE001
            logger.info("Neo4j 不可达，启用文件降级: %s", e)
            self._reachable = False
        return self._reachable

    def write_instances(
        self,
        object_type: str,
        instances: list[dict[str, Any]],
        source: str | dict[str, Any] = "internal",
        ontology_id: str | None = None,
        project_id: str | None = None,
    ) -> str:
        """写入实例节点+关系，并打 source 溯源标记。返回事务 id。"""
        if self._is_reachable():
            return self._write_to_neo4j(object_type, instances, source, ontology_id, project_id)
        return self._write_to_file(object_type, instances, source, ontology_id, project_id)

    def _write_to_neo4j(
        self, object_type: str, instances: list[dict[str, Any]], source: str | dict[str, Any], ontology_id: str | None, project_id: str | None
    ) -> str:
        from neo4j import GraphDatabase

        tx_id = uuid.uuid4().hex
        driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(self.settings.neo4j_user, self.settings.neo4j_password),
        )
        try:
            with driver.session() as session:
                for i, inst in enumerate(instances):
                    node_id = inst.get("id") or f"{object_type}_{tx_id}_{i}"
                    props = {k: v for k, v in inst.items() if not k.startswith("_") and k != "object_type"}
                    # Neo4j 属性只能是原始类型或数组；将 dict source 序列化为 JSON 字符串
                    if isinstance(source, dict):
                        props["_source"] = __import__("json").dumps(source, ensure_ascii=False)
                    else:
                        props["_source"] = source
                    props["_tx_id"] = tx_id
                    props["_ontology_id"] = ontology_id
                    props["_project_id"] = project_id
                    props["_imported_at"] = _now()
                    session.run(
                        "MERGE (n:Instance {object_type: $ot, node_id: $node_id}) "
                        "SET n += $props",
                        ot=object_type,
                        node_id=node_id,
                        props=props,
                    )
        finally:
            driver.close()
        logger.info("Neo4j 写入 instances count=%d object_type=%s project_id=%s tx=%s", len(instances), object_type, project_id, tx_id)
        return tx_id

    def _write_to_file(
        self, object_type: str, instances: list[dict[str, Any]], source: str | dict[str, Any], ontology_id: str | None, project_id: str | None
    ) -> str:
        """Neo4j 不可达时的降级：仅登记事务 id，不落 InstanceStore。

        实例统一由调用方（api/router_ingest.approve）写入 InstanceStore（权威数据源，
        前端列表/编辑/RDF 同步共用），此处再写会造成同一实例双份记录。
        """
        tx_id = uuid.uuid4().hex
        logger.info("Neo4j 不可达，实例交由调用方落库 project_id=%s object_type=%s count=%d tx=%s",
                    project_id or "default", object_type, len(instances), tx_id)
        return tx_id

    def write_relations(
        self,
        object_type: str,
        instances: list[dict[str, Any]],
        ontology: dict[str, Any],
        ontology_id: str | None = None,
        project_id: str | None = None,
    ) -> str:
        """O-13: 根据 ontology relation 定义，把实例中的关系字段物化为 Neo4j 边。

        约定：实例中关系字段名为 relation.id，值为目标对象的业务主键值。
        """
        obj_def = next((o for o in ontology.get("objects", []) if o["id"] == object_type), None)
        if not obj_def:
            return ""
        source_stable_key = obj_def.get("stable_key")
        if not source_stable_key:
            return ""
        relations = obj_def.get("relations", [])
        if not relations:
            return ""

        if self._is_reachable():
            return self._write_relations_to_neo4j(object_type, instances, ontology, relations, source_stable_key, ontology_id, project_id)
        return self._write_relations_to_file(object_type, instances, ontology, relations, source_stable_key, ontology_id, project_id)

    def _write_relations_to_neo4j(
        self, object_type: str, instances: list[dict[str, Any]], ontology: dict[str, Any],
        relations: list[dict[str, Any]], source_stable_key: str, ontology_id: str | None, project_id: str | None
    ) -> str:
        from neo4j import GraphDatabase

        tx_id = uuid.uuid4().hex
        obj_by_id = {o["id"]: o for o in ontology.get("objects", [])}
        driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(self.settings.neo4j_user, self.settings.neo4j_password),
        )
        try:
            with driver.session() as session:
                for inst in instances:
                    source_val = inst.get(source_stable_key)
                    if source_val is None:
                        continue
                    for rel in relations:
                        target_id = rel.get("target_object")
                        target_obj = obj_by_id.get(target_id)
                        if not target_obj:
                            continue
                        target_key = target_obj.get("stable_key")
                        if not target_key:
                            continue
                        rel_value = inst.get(rel["id"])
                        if rel_value is None:
                            continue
                        session.run(
                            "MATCH (src:Instance {object_type: $ot, node_id: $src_val}), "
                            "(dst:Instance {object_type: $tgt_ot, node_id: $tgt_val}) "
                            "MERGE (src)-[r:REL {rel_id: $rel_id}]->(dst) "
                            "SET r._ontology_id = $ontology_id, r._project_id = $project_id, r._tx_id = $tx_id",
                            ot=object_type,
                            src_val=str(source_val),
                            tgt_ot=target_id,
                            tgt_val=str(rel_value),
                            rel_id=rel["id"],
                            ontology_id=ontology_id,
                            project_id=project_id,
                            tx_id=tx_id,
                        )
        finally:
            driver.close()
        logger.info("Neo4j 写入 relations count=%d object_type=%s project_id=%s tx=%s", len(relations), object_type, project_id, tx_id)
        return tx_id

    def _write_relations_to_file(
        self, object_type: str, instances: list[dict[str, Any]], ontology: dict[str, Any],
        relations: list[dict[str, Any]], source_stable_key: str, ontology_id: str | None, project_id: str | None
    ) -> str:
        tx_id = uuid.uuid4().hex
        base_dir = os.path.join(self.settings.data_dir, "projects", project_id or "default")
        os.makedirs(base_dir, exist_ok=True)
        path = os.path.join(base_dir, "relations.jsonl")
        obj_by_id = {o["id"]: o for o in ontology.get("objects", [])}
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            for inst in instances:
                source_val = inst.get(source_stable_key)
                if source_val is None:
                    continue
                for rel in relations:
                    target_obj = obj_by_id.get(rel.get("target_object"))
                    if not target_obj:
                        continue
                    target_key = target_obj.get("stable_key")
                    if not target_key:
                        continue
                    rel_value = inst.get(rel["id"])
                    if rel_value is None:
                        continue
                    rec = {
                        "project_id": project_id,
                        "ontology_id": ontology_id,
                        "source_object_type": object_type,
                        "source_key": source_stable_key,
                        "source_value": source_val,
                        "rel_id": rel["id"],
                        "target_object_type": rel.get("target_object"),
                        "target_key": target_key,
                        "target_value": rel_value,
                        "tx_id": tx_id,
                        "written_at": _now(),
                    }
                    f.write(__import__("json").dumps(rec, ensure_ascii=False) + "\n")
        logger.info("Neo4j 不可达，关系落本地 JSONL path=%s count=%d tx=%s", path, len(relations), tx_id)
        return tx_id

    def read_graph(self, ontology_id: str) -> dict[str, Any]:
        logger.info("read_graph ontology_id=%s (VS4 可视化切片实现)", ontology_id)
        raise NotImplementedError("图谱读取在 VS4 可视化切片实现")

    def health(self) -> dict[str, str]:
        if self._is_reachable():
            return {"neo4j": "healthy"}
        return {"neo4j": "unavailable"}
