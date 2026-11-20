"""GAP-03 验收：查询/推理/SHACL 本地快路径 ＋ 引擎可切换 ＋ 降级 ＋ 双写 ＋ 性能基线。

覆盖（docs/本体平台功能增补清单.md GAP-03）：
- 缺省本地快路径：无运行时配置、无环境变量 → 查询引擎 local（本地 TTL + rdflib）；
- 优先级：运行时配置（PUT /api/query/engine）> 环境变量 ONTOLOGY_QUERY_ENGINE > 缺省 local；
- GET/PUT /api/query/engine 状态与切换（非法值 422）；
- 本地快路径 SPARQL 可查已发布本体数据（双写产出的本地 TTL）；
- 引擎=fuseki 但 Fuseki 不可达 → 自动降级本地，结果与本地快路径一致；
- 双写（GAP-03/GAP-15）：sync_ontology 本地 TTL **始终**写入；可达时 mode=dual；
- 本地 SPARQL 结果缓存：同 mtime 内命中不重解析；TTL 过期重算；数据变化即失效；
- perf 埋点：run_inference 记 reasoning_local / shacl_validate 记 shacl_local / shacl_jena；
- perf 基线：freeze 写入（scope→baseline_ms+target_ms）、record 超目标/超基线只告警不抛。
"""
from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient
from rdflib import Graph

from _authenv import auth_module_guard
from main import app
from core.instance_store import get_instance_store
from core.storage import JsonStore
from config import get_settings
import perf
import jena_adapter.client as jc
import jena_adapter.rdf_sync as rdf_sync
from jena_adapter import shacl_engine, shacl_jena
import reasoning.engine as rengine

pytestmark = pytest.mark.auth
_guard = auth_module_guard()  # GAP-20：模块边界隔离鉴权环境

PID = "gap03-m4b"
ONT: str | None = None
REC: dict = {}

c = TestClient(app)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ok(resp) -> dict:
    assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("success") is True, body
    return body["data"]


def _cfg_path() -> str:
    return os.path.join(get_settings().data_dir, jc._QUERY_CONFIG_FILE)


def _insert(rec_id: str, object_type: str, inst: dict, stable_key: str = "orderCode") -> None:
    rec = {
        "id": rec_id, "project_id": PID, "ontology_id": ONT, "object_type": object_type,
        "stable_key": stable_key, "stable_key_value": inst.get(stable_key),
        "channel": "upload", "instance": inst, "imported_at": _now(),
    }
    get_instance_store(PID).insert(rec)


OBJECTS = [
    {"id": "Order", "name": "订单", "stable_key": "orderCode",
     "attributes": [
         {"id": "orderCode", "name": "订单号", "data_type": "string", "required": True},
         {"id": "amount", "name": "金额", "data_type": "float"},
     ],
     "relations": [
         {"id": "customer", "name": "客户", "target_object": "Customer",
          "cardinality": "many_to_one"},
     ]},
    {"id": "Customer", "name": "客户", "stable_key": "customerCode",
     "attributes": [
         {"id": "customerCode", "name": "客户号", "data_type": "string", "required": True},
         {"id": "name", "name": "名称", "data_type": "string"},
     ],
     "relations": []},
]


def _setup() -> None:
    global ONT, REC
    # 查询引擎运行时配置复位（缺省态断言不受历史影响）
    try:
        os.remove(_cfg_path())
    except FileNotFoundError:
        pass
    if ONT and JsonStore("ontologies", PID).get(ONT):
        REC = JsonStore("ontologies", PID).get(ONT)
        return  # 幂等
    body = _ok(c.post("/api/ontologies", params={"project_id": PID},
                      json={"name": "GAP03 查询引擎测试本体", "objects": OBJECTS}))
    ONT = body["id"]
    _ok(c.post(f"/api/ontologies/{ONT}/publish", params={"project_id": PID}))
    REC = JsonStore("ontologies", PID).get(ONT)
    _insert("gap03-u1", "Order", {"orderCode": "Q3-U1", "amount": 100.5, "customer": "C-1"})
    _insert("gap03-c1", "Customer", {"customerCode": "C-1", "name": "Alice"},
            stable_key="customerCode")
    # 双写：本地 TTL 始终产出（查询快路径数据源）
    _ok(c.post(f"/api/graph/{ONT}/rdf-sync", params={"project_id": PID}))


_setup()


def _count_query() -> str:
    return "SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 100"


# ------------------------------------------------- 1. 缺省本地快路径

def test_01_default_local(monkeypatch):
    monkeypatch.delenv("ONTOLOGY_QUERY_ENGINE", raising=False)
    assert not os.path.exists(_cfg_path())  # _setup 已复位
    assert jc.get_query_config() == ("local", "default")
    d = _ok(c.get("/api/query/engine"))
    assert d["engine"] == "local"
    assert d["default"] == "local"
    assert set(d["options"]) == {"local", "fuseki"}


# ------------------------------------------------- 2. 优先级：runtime > env > default

def test_02_priority_runtime_env_default(monkeypatch):
    monkeypatch.delenv("ONTOLOGY_QUERY_ENGINE", raising=False)
    monkeypatch.setenv("ONTOLOGY_QUERY_ENGINE", "fuseki")
    assert jc.get_query_config() == ("fuseki", "env")
    jc.set_query_engine("local")  # 运行时配置压过 env
    assert jc.get_query_config() == ("local", "runtime")
    try:
        os.remove(_cfg_path())
    except FileNotFoundError:
        pass
    assert jc.get_query_config() == ("fuseki", "env")  # 回到 env 层
    with pytest.raises(ValueError):
        jc.set_query_engine("wat")


# ------------------------------------------------- 3. 引擎状态/切换 API

def test_03_engine_api():
    d = _ok(c.get("/api/query/engine"))
    assert d["engine"] in ("local", "fuseki")
    assert d["source"] in ("default", "env", "runtime")
    assert isinstance(d["fuseki_reachable"], bool)
    assert d["cache_ttl_seconds"] > 0

    d = _ok(c.put("/api/query/engine", json={"engine": "fuseki"}))
    assert d["engine"] == "fuseki" and d["source"] == "runtime"
    assert os.path.exists(_cfg_path())
    d = _ok(c.get("/api/query/engine"))
    assert d["engine"] == "fuseki" and d["source"] == "runtime"

    r = c.put("/api/query/engine", json={"engine": "bogus"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    # 复位缺省态（删运行时配置）
    try:
        os.remove(_cfg_path())
    except FileNotFoundError:
        pass


# ------------------------------------------------- 4. 本地快路径 SPARQL（API）

def test_04_local_fast_path_sparql():
    d = _ok(c.post("/api/query/sparql", params={"project_id": PID},
                   json={"ontology_id": ONT, "query": _count_query()}))
    assert d["graph"] == f"urn:ontology:{ONT}"
    assert d["count"] > 0, "双写产出的本地 TTL 应含 schema/实例三元组"


# ------------------------------------------------- 5. fuseki 不可达降级本地

def test_05_fuseki_unreachable_degrades_to_local(monkeypatch):
    monkeypatch.setattr(jc, "_is_reachable", lambda: False)
    jc.set_query_engine("fuseki")
    # 限定命名资源：SHACL sh:or 列表含 blank node，rdflib 每次 parse 重分配
    # bnode 标签（RDF 语义使然），裸 ?s ?p ?o 的 bnode 行不可跨 parse 比较；
    # 且 rdflib 解析器不支持多键 ORDER BY（pyparsing 限制），只用单键
    q = "SELECT ?s WHERE { ?s a ?c } ORDER BY ?s"
    try:
        degraded = jc.sparql_query(q, project_id=PID, ontology_id=ONT)
    finally:
        jc.set_query_engine("local")
    local = jc.sparql_query(q, project_id=PID, ontology_id=ONT)
    assert degraded, "降级后应仍返回本地数据"
    assert degraded == local, "降级结果应与本地快路径一致"


# ------------------------------------------------- 6. 双写：本地 TTL 始终写入

def test_06_dual_write_local_ttl_always(monkeypatch):
    p = jc.local_ttl_path(ONT, PID)
    # Fuseki 不可达 → mode=file，本地 TTL 必须存在
    monkeypatch.setattr(rdf_sync, "_is_reachable", lambda: False)
    r = rdf_sync.sync_ontology(PID, ONT)
    assert r["mode"] == "file" and r["path"] == p
    assert os.path.exists(p) and os.path.getsize(p) > 0
    assert r["triples"] > 0 and r["instances"] >= 2
    # 实际可达时 → mode=dual，且本地 TTL 依然写入（双写核心断言）
    if jc._is_reachable():
        r = rdf_sync.sync_ontology(PID, ONT)
        assert r["mode"] == "dual" and r.get("local_path") == p
        assert os.path.exists(p)


# ------------------------------------------------- 7. 本地 SPARQL 结果缓存

def test_07_local_query_cache(monkeypatch):
    jc._query_cache.clear()
    oid = "gap03-cache"
    path = jc.local_ttl_path(oid, PID)
    q = 'SELECT ?o WHERE { <http://example.org/a> <http://example.org/p> ?o }'

    def _write(v: str) -> None:
        jc.write_local_ttl(oid, f"@prefix ex: <http://example.org/> .\nex:a ex:p \"{v}\" .\n",
                           project_id=PID)

    _write("1")
    rows1 = jc.sparql_query(q, project_id=PID, ontology_id=oid)
    assert rows1 and rows1[0]["o"] == "1"
    assert len(jc._query_cache) == 1

    # 命中：不重新解析 TTL
    calls = {"n": 0}
    orig_parse = jc.Graph.parse

    def spy_parse(self, *a, **k):
        calls["n"] += 1
        return orig_parse(self, *a, **k)

    monkeypatch.setattr(jc.Graph, "parse", spy_parse)
    rows2 = jc.sparql_query(q, project_id=PID, ontology_id=oid)
    assert rows2 == rows1 and calls["n"] == 0, "同 mtime+TTL 内应命中缓存不重解析"

    # TTL 过期（负值→立即过期）：每次重算
    monkeypatch.setattr(jc, "QUERY_CACHE_TTL", -1.0)
    calls["n"] = 0
    jc.sparql_query(q, project_id=PID, ontology_id=oid)
    jc.sparql_query(q, project_id=PID, ontology_id=oid)
    assert calls["n"] == 2, "TTL 过期后应重新计算"

    # 数据变化（write_local_ttl）→ 缓存整体失效
    _write("2")
    rows3 = jc.sparql_query(q, project_id=PID, ontology_id=oid)
    assert rows3 and rows3[0]["o"] == "2"
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ------------------------------------------------- 8. perf 埋点：推理 / SHACL

def test_08_perf_instrumentation_reasoning(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(perf, "record", lambda scope, elapsed: calls.append((scope, elapsed)))
    out = rengine.run_inference(REC, PID, ONT, engine="local")
    assert out["engine"] == "local" and out["steps"] is not None
    local_recs = [(s, e) for s, e in calls if s == "reasoning_local"]
    # 小本体推理可 <1ms，int(ms) 合法为 0 → 只断言观测存在且非负
    assert local_recs and local_recs[0][1] >= 0, "本地推理应记录 reasoning_local 观测"


def test_09_perf_instrumentation_shacl(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(perf, "record", lambda scope, elapsed: calls.append((scope, elapsed)))
    data, shapes = Graph(), Graph()
    jc.shacl_validate(data, shapes)
    assert any(s == "shacl_local" for s, _ in calls), "本地 SHACL 路径应记录 shacl_local"

    # jena 分支（mock 执行器）：记录 shacl_jena 且契约字段齐全
    calls.clear()
    monkeypatch.setattr(shacl_engine, "get_configured_engine", lambda: ("jena", "runtime"))
    monkeypatch.setattr(shacl_jena, "jena_shacl_validate",
                        lambda *a, **k: {"valid": True, "violations": []})
    r = jc.shacl_validate(data, shapes)
    assert r and r["engine"] == "jena" and r["degraded"] is False
    jena_recs = [(s, e) for s, e in calls if s == "shacl_jena"]
    assert jena_recs and jena_recs[0][1] >= 0, "Jena SHACL 路径应记录 shacl_jena 观测"


# ------------------------------------------------- 10. perf 基线 freeze / record

def test_10_perf_freeze_and_record(tmp_path, monkeypatch):
    path = tmp_path / "perf_baseline.json"
    monkeypatch.setattr(perf, "_baseline_path", lambda: str(path))
    perf._baseline_cache = None
    try:
        entry = perf.freeze("sparql_local", 123.456)
        assert entry["baseline_ms"] == 123.456
        assert entry["target_ms"] == 1000.0  # DEFAULT_TARGETS["sparql_local"] * 1000
        assert entry["frozen_at"]
        base = perf.load_baseline()
        assert base["sparql_local"]["baseline_ms"] == 123.456
        assert path.exists()
        # record 永不抛异常：超目标＋超基线×回归系数（只告警）、未知 scope（无目标）
        perf.record("sparql_local", 5.0)
        perf.record("no_such_scope", 0.1)
    finally:
        perf._baseline_cache = None  # 恢复真实基线路径的读取
