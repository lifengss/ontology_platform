"""GAP-16 外部 MCP 并行读取＋按源超时＋TTL 缓存。

- McpClient._source_timeout：连接 timeout_seconds 覆盖全局（缺省回落）；
- content_access.federate_values_many：线程池并行批量回源，结果与逐条一致、单条异常不拖垮整批；
- federate_values 连接 cache_ttl_seconds>0 时命中缓存（cache_hit=True，不回源），缺省 0 每次真实回源；
- 并行与串行总时延对比（4 个模拟慢源，并行≈max 而非 sum）。
mock 架构：真实项目＋JsonStore 写入 mcp_connections/ontologies（渠道判定与对象定义为权威口径），
仅拦截 _governance_read（不发起真实 HTTP）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _authenv import auth_module_guard
import main as m
from core.storage import JsonStore
from fastapi.testclient import TestClient

_guard = auth_module_guard()
app = m.app
client = TestClient(app)

_PID = ""


def setup_module():
    global _PID
    r = client.post("/api/projects", json={"name": f"G16-{int(time.time() * 1000)}", "description": ""})
    _PID = r.json()["data"]["id"]
    JsonStore("ontologies", project_id=_PID).insert({
        "id": "oX", "name": "G16 本体",
        "objects": [{
            "id": "obj_x", "name": "X", "stable_key": "attr_code",
            "attributes": [
                {"id": "attr_code", "name": "编码", "data_type": "string", "required": True, "constraints": {}},
                {"id": "attr_amount", "name": "金额", "data_type": "float", "required": False, "constraints": {}},
            ],
            "relations": [],
        }],
    })


def teardown_module():
    import content_access
    with content_access._FED_READ_CACHE_LOCK:
        content_access._FED_READ_CACHE.clear()
    try:
        if _PID:
            client.delete(f"/api/projects/{_PID}")
    except Exception:
        pass


def _conn(conn_id, cache_ttl=0, **extra):
    c = {"id": conn_id, "display_name": f"conn-{conn_id}", "transport": "http",
         "endpoint": f"http://mock/{conn_id}", "read_tool": "read",
         "maps_to_ontology": {"object_type": "obj_x",
                              "field_map": {"code": "attr_code", "amount": "attr_amount"}}}
    if cache_ttl:
        c["cache_ttl_seconds"] = cache_ttl
    c.update(extra)
    JsonStore("mcp_connections", project_id=_PID).insert(c)
    return c


def _mk_rec(rid, conn_id, key_val):
    return {"id": rid, "object_type": "obj_x", "ontology_id": "oX", "channel": "external_mcp",
            "instance": {"id": rid, "attr_code": key_val,
                         "_source": {"kind": "field_map", "connection_id": conn_id}},
            "stable_key_value": key_val}


class TestSourceTimeout:
    def test_source_timeout_override_and_default(self):
        from mcp_client import McpClient

        mc = McpClient()
        global_t = mc.settings.mcp_timeout_seconds
        assert mc._source_timeout({}) == global_t
        assert mc._source_timeout({"timeout_seconds": 3}) == 3
        assert mc._source_timeout({"timeout_seconds": "5"}) == 5
        assert mc._source_timeout({"timeout_seconds": 0}) == global_t
        assert mc._source_timeout({"timeout_seconds": "bad"}) == global_t


class TestParallelFederation:
    def test_many_matches_and_isolates_errors(self):
        """federate_values_many：结果键齐全、模式 federated；单条异常→unavailable 不拖垮整批。"""
        import content_access

        _conn("c_ok")
        JsonStore("mcp_connections", project_id=_PID).insert(
            {"id": "boom", "display_name": "boom", "transport": "http", "endpoint": "http://mock/boom",
             "read_tool": "read", "maps_to_ontology": {"object_type": "obj_x",
                                                       "field_map": {"code": "attr_code"}}})

        def fake_governance(project_id, conn, tool, params):
            if conn["id"] == "boom":
                raise RuntimeError("源端炸了")
            return [{"code": params["code"], "amount": 100}]

        recs = [_mk_rec("r1", "c_ok", "K1"), _mk_rec("r2", "boom", "K2")]
        assert content_access.is_federated(_PID, recs[0])
        orig_gov = content_access._governance_read
        content_access._governance_read = fake_governance
        try:
            res = content_access.federate_values_many(_PID, recs, attr_ids=["attr_amount"])
            assert set(res.keys()) == {"r1", "r2"}
            assert res["r1"][0] == {"attr_amount": 100}
            assert res["r1"][1]["mode"] == "federated"
            assert res["r2"][1]["mode"] == "unavailable"  # 单条异常隔离
            assert content_access.federate_values_many(_PID, []) == {}
        finally:
            content_access._governance_read = orig_gov

    def test_parallel_latency_vs_serial(self):
        """4 个 0.3s 慢源批量回源：并行总时延显著小于串行相加（≈max 而非 sum）。"""
        import content_access

        for i in range(4):
            _conn(f"c{i}")

        def fake_governance(project_id, conn, tool, params):
            time.sleep(0.3)
            return [{"code": params["code"], "amount": 1}]

        recs = [_mk_rec(f"r{i}", f"c{i}", f"K{i}") for i in range(4)]
        orig_gov = content_access._governance_read
        content_access._governance_read = fake_governance
        try:
            t0 = time.monotonic()
            res = content_access.federate_values_many(_PID, recs, attr_ids=["attr_amount"])
            par = time.monotonic() - t0
            assert len(res) == 4 and all(res[f"r{i}"][1]["mode"] == "federated" for i in range(4))
            # 并行 ≈ max(0.3)=0.3+开销；串行 ≈ 4×0.3=1.2。给 0.75s 阈值（留 CI 余量）
            assert par < 0.75, f"并行回源耗时 {par:.2f}s，应≈单源而非串行相加"
        finally:
            content_access._governance_read = orig_gov


class TestFedReadCache:
    def test_cache_ttl_hit_and_default_off(self):
        """cache_ttl_seconds>0 → 第二次读命中缓存不再回源；缺省 0 → 每次真实回源。"""
        import content_access

        _conn("c0")
        _conn("c_ttl", cache_ttl=60)
        read_calls = []

        def fake_governance(project_id, conn, tool, params):
            read_calls.append(1)
            return [{"code": params["code"], "amount": 42}]

        rec0 = _mk_rec("rc0", "c0", "K0")
        recT = _mk_rec("rcT", "c_ttl", "KT")
        orig_gov = content_access._governance_read
        content_access._governance_read = fake_governance
        try:
            # 缺省连接：两次读都真实回源
            content_access.federate_values(_PID, rec0, attr_ids=["attr_amount"])
            content_access.federate_values(_PID, rec0, attr_ids=["attr_amount"])
            assert len(read_calls) == 2
            # TTL 连接：第二次命中缓存
            v1, m1 = content_access.federate_values(_PID, recT, attr_ids=["attr_amount"])
            assert m1["mode"] == "federated" and "cache_hit" not in m1
            n_before = len(read_calls)
            v2, m2 = content_access.federate_values(_PID, recT, attr_ids=["attr_amount"])
            assert m2.get("cache_hit") is True
            assert len(read_calls) == n_before  # 未回源
            assert v1 == v2 == {"attr_amount": 42}
            # 不同属性子集命中同一缓存条目（全量值缓存）
            v3, _ = content_access.federate_values(_PID, recT, attr_ids=["attr_code", "attr_amount"])
            assert v3["attr_code"] == "KT"
        finally:
            content_access._governance_read = orig_gov
