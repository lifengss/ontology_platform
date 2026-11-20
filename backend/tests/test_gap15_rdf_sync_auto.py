"""GAP-15 发布/导入后自动 rdf-sync＋一致性对账。

- ingestion.approve 落库后自动触发 RDF 同步（本地 TTL mtime 前进，无需手动 /rdf-sync）；
- check_consistency：本地 TTL 落后于模型/实例数据 → stale=True；sync 后恢复；
- GET /api/graph/{id}/consistency 端点契约；_reconcile_once 自动修复漂移。
"""
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _authenv import auth_module_guard
import main as m
from core.instance_store import get_instance_store
from fastapi.testclient import TestClient
from jena_adapter import rdf_sync
from jena_adapter.client import local_ttl_path

_guard = auth_module_guard()  # 本模块无鉴权用例，保持鉴权关
app = m.app
client = TestClient(app)


def _setup_ontology() -> tuple[str, str]:
    r = client.post("/api/projects", json={"name": f"G15-{int(time.time() * 1000)}", "description": ""})
    pid = r.json()["data"]["id"]
    body = {
        "name": "G15 本体", "modeling_type": "manual", "version": "1",
        "objects": [{
            "id": "obj_p", "name": "产品", "stable_key": "attr_code",
            "attributes": [{"id": "attr_code", "name": "编码", "data_type": "string",
                            "required": True, "constraints": {"pattern": "^P[0-9]{4}$"}}],
            "relations": [],
        }],
        "constraints": [], "functions": [],
    }
    r = client.post("/api/ontologies", params={"project_id": pid}, json=body)
    assert r.status_code == 201, r.text
    return pid, r.json()["data"]["id"]


def _upload_and_approve(pid: str, oid: str):
    csv = io.BytesIO("编码\nP0001\nP0002\n".encode("utf-8"))
    r = client.post("/api/ingest/upload", params={"project_id": pid, "ontology_id": oid, "object_type": "obj_p"},
                    files={"file": ("rows.csv", csv, "text/csv")})
    assert r.status_code == 201, r.text
    cands = r.json()["data"]["candidates"]
    assert cands, r.text
    cid = cands[0]["id"]
    r2 = client.post(f"/api/ingest/candidates/{cid}/approve", params={"project_id": pid})
    assert r2.status_code == 200, r2.text
    return r2.json()["data"]


class TestApproveAutoSync:
    def test_approve_touches_local_ttl(self):
        """approve 落库后本地 TTL 自动重建（mtime 前进），图谱即时反映最新数据。"""
        pid, oid = _setup_ontology()
        ttl = local_ttl_path(oid, pid)
        assert ttl and ttl.endswith(".ttl")
        t_before = time.time()
        time.sleep(0.02)
        res = _upload_and_approve(pid, oid)
        assert res["imported"] == 2
        import os
        assert os.path.exists(ttl), "approve 后本地 TTL 应已自动写入"
        assert os.path.getmtime(ttl) >= t_before - 1.0
        info = rdf_sync.check_consistency(pid, oid)
        assert info["stale"] is False  # 落库与同步原子衔接，无漂移


class TestConsistency:
    def test_stale_detect_and_repair(self):
        """直插实例（绕过同步）→ 检出 stale → sync 后恢复 → _reconcile_once 亦能修复。"""
        pid, oid = _setup_ontology()
        _upload_and_approve(pid, oid)
        time.sleep(0.02)
        # 绕过 RDF 同步直插实例（模拟外部写入/历史缺口）
        store = get_instance_store(pid)
        store.insert({"ontology_id": oid, "object_type": "obj_p",
                      "instance": {"id": "p_man", "attr_code": "P9999"}})
        info = rdf_sync.check_consistency(pid, oid)
        assert info["stale"] is True and info["drift_seconds"] > 0
        rdf_sync.sync_ontology(pid, oid)
        assert rdf_sync.check_consistency(pid, oid)["stale"] is False

    def test_reconcile_once_repairs(self):
        pid, oid = _setup_ontology()
        _upload_and_approve(pid, oid)
        time.sleep(0.02)
        get_instance_store(pid).insert({"ontology_id": oid, "object_type": "obj_p",
                                        "instance": {"id": "p_man2", "attr_code": "P8888"}})
        assert rdf_sync.check_consistency(pid, oid)["stale"] is True
        results = rdf_sync._reconcile_once()
        mine = [r for r in results if r.get("ontology_id") == oid]
        assert mine and (mine[-1].get("repaired") is True or mine[-1]["stale"] is False)
        assert rdf_sync.check_consistency(pid, oid)["stale"] is False

    def test_consistency_endpoint(self):
        pid, oid = _setup_ontology()
        r = client.get(f"/api/graph/{oid}/consistency", params={"project_id": pid})
        assert r.status_code == 200
        d = r.json()["data"]
        assert {"stale", "ttl_path", "ttl_exists", "data_mtime", "drift_seconds"} <= set(d.keys())
        # 项目隔离
        r2 = client.get("/api/projects", params={"pageSize": 1})
        r3 = client.get(f"/api/graph/{oid}/consistency", params={"project_id": "ghost"})
        assert r3.status_code == 404


def teardown_module():
    try:
        r = client.get("/api/projects", params={"pageSize": 100})
        for it in r.json()["data"]["items"]:
            if (it.get("name") or "").startswith("G15-"):
                client.delete(f"/api/projects/{it['id']}")
    except Exception:
        pass
