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
        source: str = "internal",
        ontology_id: str | None = None,
    ) -> str:
        """写入实例节点+关系，并打 source 溯源标记。返回事务 id。"""
        if self._is_reachable():
            return self._write_to_neo4j(object_type, instances, source, ontology_id)
        return self._write_to_file(object_type, instances, source, ontology_id)

    def _write_to_neo4j(
        self, object_type: str, instances: list[dict[str, Any]], source: str, ontology_id: str | None
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
                    props["_source"] = source
                    props["_tx_id"] = tx_id
                    props["_ontology_id"] = ontology_id
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
        logger.info("Neo4j 写入 instances count=%d object_type=%s tx=%s", len(instances), object_type, tx_id)
        return tx_id

    def _write_to_file(
        self, object_type: str, instances: list[dict[str, Any]], source: str, ontology_id: str | None
    ) -> str:
        """Neo4j 不可达时，以 JSONL 形式追加到 data_dir/instances.jsonl。"""
        tx_id = uuid.uuid4().hex
        path = os.path.join(self.settings.data_dir, "instances.jsonl")
        os.makedirs(self.settings.data_dir, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            for i, inst in enumerate(instances):
                record = {
                    "ontology_id": ontology_id,
                    "object_type": object_type,
                    "instance": inst,
                    "source": source,
                    "tx_id": tx_id,
                    "imported_at": _now(),
                }
                f.write(__import__("json").dumps(record, ensure_ascii=False) + "\n")
        logger.info("Neo4j 不可达，实例落本地 JSONL path=%s count=%d tx=%s", path, len(instances), tx_id)
        return tx_id

    def read_graph(self, ontology_id: str) -> dict[str, Any]:
        logger.info("read_graph ontology_id=%s (VS4 可视化切片实现)", ontology_id)
        raise NotImplementedError("图谱读取在 VS4 可视化切片实现")

    def health(self) -> dict[str, str]:
        if self._is_reachable():
            return {"neo4j": "healthy"}
        return {"neo4j": "unavailable"}
