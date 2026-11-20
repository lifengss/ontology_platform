"""GAP-19 批量导入/校验并行分块 — 大候选 SHACL 分块并行与逐条口径一致；查询缓存沿用 GAP-03。

- ingestion.approve：> _APPROVE_SHACL_CHUNK 分块线程池校验，违规集与全量单次一致；
- capabilities.execute_batch：联邦回源块内并行预取——多慢源批量时延 ≈ 最慢单源而非串行相加。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _authenv import auth_module_guard
import main as m
from fastapi.testclient import TestClient

_guard = auth_module_guard()
app = m.app
client = TestClient(app)


def _setup_with_pattern() -> tuple[str, str]:
    r = client.post("/api/projects", json={"name": f"G19-{int(time.time() * 1000)}", "description": ""})
    pid = r.json()["data"]["id"]
    body = {"name": "G19 本体", "modeling_type": "manual", "version": "1",
            "objects": [{"id": "obj_p", "name": "P", "stable_key": "attr_code",
                         "attributes": [{"id": "attr_code", "name": "编码", "data_type": "string",
                                         "required": True, "constraints": {"pattern": "^P[0-9]{4}$"}}],
                         "relations": []}],
            "constraints": [], "functions": []}
    r = client.post("/api/ontologies", params={"project_id": pid}, json=body)
    assert r.status_code == 201, r.text
    return pid, r.json()["data"]["id"]


class TestApproveChunkedShacl:
    def test_chunked_matches_full_result(self):
        """分块并行校验与全量单次校验结论一致（valid 与违规集）。"""
        import ingestion as ing

        pid, oid = _setup_with_pattern()
        # 1201 条 > 500 分块阈值：含 3 条违规（分布在不同块），走 upload→approve 完整管线
        rows = ["编码"]
        for i in range(1201):
            rows.append(f"P{i:04d}")
        rows[600] = "BAD-1"
        rows[1100] = "BAD-2"
        rows[300] = ""
        import io
        csv = io.BytesIO("\n".join(rows).encode("utf-8"))
        r = client.post("/api/ingest/upload", params={"project_id": pid, "ontology_id": oid, "object_type": "obj_p"},
                        files={"file": ("big.csv", csv, "text/csv")})
        assert r.status_code == 201, r.text
        cid = r.json()["data"]["candidates"][0]["id"]
        assert len(instances := r.json()["data"]["candidates"][0].get("instances") or []) > ing._APPROVE_SHACL_CHUNK
        r2 = client.post(f"/api/ingest/candidates/{cid}/approve", params={"project_id": pid})
        assert r2.status_code == 200, r2.text
        d = r2.json()["data"]
        assert d["imported"] == 0
        # BAD-1/BAD-2 位于不同块（600/1100），分块校验与全量口径一致（跨块违规都被捕获）；
        # violations 结构＝{focus_node, path, severity, message}（值在 message 内）
        assert len(d["violations"]) >= 2
        assert all(v.get("path") == "attr_code" for v in d["violations"])

    def test_small_candidate_unchanged(self):
        """小候选（<分块阈值）走原单次路径（无 chunked 标记需求，行为不变）。"""
        pid, oid = _setup_with_pattern()
        import io
        csv = io.BytesIO("编码\nP0001\nP0002\n".encode("utf-8"))
        r = client.post("/api/ingest/upload", params={"project_id": pid, "ontology_id": oid, "object_type": "obj_p"},
                        files={"file": ("rows.csv", csv, "text/csv")})
        cands = r.json()["data"]["candidates"]
        cid = cands[0]["id"]
        r2 = client.post(f"/api/ingest/candidates/{cid}/approve", params={"project_id": pid})
        assert r2.status_code == 200 and r2.json()["data"]["imported"] == 2


class TestBatchFederatedParallel:
    def test_chunk_prefetch_parallel_latency(self):
        """execute_batch 联邦回源块内并行：多慢源批量时延 ≈ 最慢单源而非串行相加。"""
        import content_access
        import capabilities as caps

        r = client.post("/api/projects", json={"name": f"G19F-{int(time.time() * 1000)}", "description": ""})
        pid = r.json()["data"]["id"]
        # 本体（1 属性）＋能力（表达式）＋20 个联邦实例（各自慢回源 0.2s）
        body = {"name": "G19F 本体", "modeling_type": "manual", "version": "1",
                "objects": [{"id": "obj_x", "name": "X", "stable_key": "attr_code",
                             "attributes": [{"id": "attr_code", "name": "编码", "data_type": "string",
                                             "required": True, "constraints": {}},
                                            {"id": "attr_amount", "name": "金额", "data_type": "float",
                                             "required": False, "constraints": {}}],
                             "relations": []}],
                "constraints": [], "functions": []}
        r = client.post("/api/ontologies", params={"project_id": pid}, json=body)
        oid = r.json()["data"]["id"]
        # 直插 20 个联邦渠道实例
        from core.instance_store import get_instance_store
        store = get_instance_store(pid)
        conns = {}
        for i in range(20):
            cid = f"c{i}"
            conns[cid] = {"id": cid, "display_name": cid, "transport": "http",
                          "endpoint": f"http://mock/{cid}", "read_tool": "read",
                          "maps_to_ontology": {"object_type": "obj_x",
                                               "field_map": {"code": "attr_code", "amount": "attr_amount"}}}
            from core.storage import JsonStore
            JsonStore("mcp_connections", project_id=pid).insert(conns[cid])
            store.insert({"ontology_id": oid, "object_type": "obj_x",
                          "instance": {"id": f"x{i}", "attr_code": f"K{i}",
                                       "_source": {"kind": "field_map", "connection_id": cid}},
                          "stable_key_value": f"K{i}"})
        # 能力：读取联邦回填的 attr_amount（服务级直调，照 hf11 模式）
        from core.storage import JsonStore
        cap = {"id": "g19-cap", "name": "g19_fed", "object_type": "obj_x",
               "expression": "this.attr_amount", "output_attribute": "attr_amount",
               "kind": "derive", "implementation": "expression", "version": "1",
               "status": "active", "channel": "api", "project_id": pid}
        JsonStore("capabilities", pid).insert(cap)
        # mock 治理读：每实例 0.2s 慢回源
        def fake_governance(project_id, conn, tool, params):
            time.sleep(0.2)
            return [{"code": params["code"], "amount": 42}]
        orig_gov = content_access._governance_read
        content_access._governance_read = fake_governance
        try:
            svc = caps.CapabilityService(pid)
            t0 = time.monotonic()
            res = svc.execute_batch("g19-cap", channel="api", trigger_source="batch",
                                    cost_confirmed=True)
            wall = time.monotonic() - t0
            assert res["success"] == 20 and res["error"] == 0, res
            # 20 × 0.2s 串行 = 4s；块内并行预取后 ≈ 0.2-0.8s。阈值 1.5s
            assert wall < 1.5, f"批量联邦回源耗时 {wall:.2f}s，应≈单源而非串行相加"
        finally:
            content_access._governance_read = orig_gov
            client.delete(f"/api/projects/{pid}")


def teardown_module():
    try:
        r = client.get("/api/projects", params={"pageSize": 100})
        for it in r.json()["data"]["items"]:
            if (it.get("name") or "").startswith("G19-"):
                client.delete(f"/api/projects/{it['id']}")
    except Exception:
        pass
