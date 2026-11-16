"""V2-16 Fuseki 远程推理（M4-C）——L5 回归（tests/test_v2_16_reasoning.py）。

覆盖（V2-测试用例 TP-16）：
- TC-16-01  引擎开关：缺省 local / 运行时切换 fuseki / 非法值 422（TCE-16-01）
- TC-16-02  两模式一致性：mock Fuseki（OWL2-RL 物化近似）远程推理链路 == 本地（逐字段）
- TC-16-03  自动降级：不可达 / update 500 / query 500 → degraded=true＋原因码＋中文提示，本地链路完整
- TC-16-04  推理图范围：源连接型实例仅 rdf:type/稳定键 label/关系快照在图内（INV-1 ② 同口径）；
            非键内容越界可检出（负向）
- TP-16-05  时延基准：scripts/bench_v2_16_reasoning.py（微基准＋真实 Fuseki 可选，基线 JSON 归档）

测试本体（4 对象，三规则族全覆盖，本地链路确定 13 步）：
  Person(人) ← Employee(员工) ← Engineer(工程师)；Dept(部门)
  属性：personId/name(Person)、empId/salary(Employee)、engId(Engineer)、deptCode/budget(Dept)
  关系：knows→Person(inverse_name 被认识)、worksIn→Dept
  预期步骤：rule1×1（knowsInv inverseOf knows）、rule2×1（Engineer⊆Person）、
            rule3 domain×9（Person 上 3 属性×{Employee,Engineer}＋Employee 上 3 属性×{Engineer}）、
            rule3 range×2（knows range Person×{Employee,Engineer}）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import main
from core.instance_store import get_instance_store
from core.storage import JsonStore
from modeling_translator import attr_uri
from reasoning import engine as rengine
from tests.mock_fuseki import MockFuseki
from fastapi.testclient import TestClient

PID = "v216-m4c"
c = TestClient(main.app)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ok(resp) -> dict:
    assert resp.status_code in (200, 201), (resp.status_code, resp.text)
    body = resp.json()
    assert body.get("success") is True, body
    return body["data"]


def _insert(rec_id: str, object_type: str, inst: dict, channel: str = "upload",
            stable_key: str = "empId") -> None:
    rec = {
        "id": rec_id, "project_id": PID, "ontology_id": OID, "object_type": object_type,
        "stable_key": stable_key, "stable_key_value": inst.get(stable_key),
        "channel": channel, "instance": inst, "imported_at": _now(),
    }
    get_instance_store(PID).insert(rec)


OBJECTS = [
    {"id": "Person", "name": "人员", "stable_key": "personId",
     "attributes": [{"id": "personId", "name": "人员ID", "data_type": "string"},
                    {"id": "name", "name": "姓名", "data_type": "string"}],
     "relations": [{"id": "knows", "name": "认识", "target_object": "Person",
                    "inverse_name": "被认识"}]},
    {"id": "Employee", "name": "员工", "stable_key": "empId", "parent": "Person",
     "attributes": [{"id": "empId", "name": "工号", "data_type": "string"},
                    {"id": "salary", "name": "薪资", "data_type": "float"}],
     "relations": [{"id": "worksIn", "name": "所属部门", "target_object": "Dept"}]},
    {"id": "Engineer", "name": "工程师", "stable_key": "engId", "parent": "Employee",
     "attributes": [{"id": "engId", "name": "工程师号", "data_type": "string"}]},
    {"id": "Dept", "name": "部门", "stable_key": "deptCode",
     "attributes": [{"id": "deptCode", "name": "部门编码", "data_type": "string"},
                    {"id": "budget", "name": "预算", "data_type": "float"}]},
]

OID = ""
REC: dict = {}


def _setup() -> None:
    global OID, REC
    # 引擎运行时配置复位（缺省态断言不受并行/历史影响）
    try:
        os.remove(rengine._config_path())
    except FileNotFoundError:
        pass
    if OID and JsonStore("ontologies", PID).get(OID):
        REC = JsonStore("ontologies", PID).get(OID)
        return  # 幂等
    body = _ok(c.post("/api/ontologies", params={"project_id": PID},
                      json={"name": "V2-16 推理测试本体", "objects": OBJECTS}))
    OID = body["id"] if isinstance(body, dict) and body.get("id") else body["ontology_id"]
    REC = JsonStore("ontologies", PID).get(OID)
    _insert("v216-u1", "Employee", {"empId": "E001", "salary": 12.5, "worksIn": "D01"})
    _insert("v216-d1", "Dept", {"deptCode": "D01", "budget": 9.9},
            stable_key="deptCode")
    _insert("v216-f1", "Employee",
            {"empId": "E002", "worksIn": "D01",
             "_source": {"kind": "field_map", "connection_id": "conn-v216"}},
            channel="external_mcp")


_setup()


# ---------------------------------------------------------------------------
# TC-16-01 引擎开关
# ---------------------------------------------------------------------------

def test_01_engine_default():
    d = _ok(c.get("/api/reasoning/engine", params={"project_id": PID}))
    assert d["engine"] == "local"
    assert d["source"] in ("default", "env")
    assert d["fuseki"]["dataset"], d["fuseki"]
    assert isinstance(d["fuseki"]["reachable"], bool)


def test_02_engine_switch():
    d = _ok(c.put("/api/reasoning/engine", params={"project_id": PID},
                  json={"engine": "fuseki"}))
    assert d["engine"] == "fuseki" and d["source"] == "runtime"
    d = _ok(c.get("/api/reasoning/engine", params={"project_id": PID}))
    assert d["engine"] == "fuseki" and d["source"] == "runtime"
    # 非法值 → 422（TCE-16-01）
    r = c.put("/api/reasoning/engine", params={"project_id": PID},
              json={"engine": "bogus"})
    assert r.status_code == 422
    # 复位 local
    d = _ok(c.put("/api/reasoning/engine", params={"project_id": PID},
                  json={"engine": "local"}))
    assert d["engine"] == "local"


# ---------------------------------------------------------------------------
# TC-16-02 两模式一致性（本地基线 ＋ 远程 mock）
# ---------------------------------------------------------------------------

def test_03_inference_local_api():
    d = _ok(c.get(f"/api/ontologies/{OID}/inference",
                  params={"project_id": PID, "engine": "local"}))
    assert d["engine"] == "local" and d["degraded"] is False
    steps = d["steps"]
    assert len(steps) == 13, [s["conclusion"] for s in steps]
    rules = [s["rule"] for s in steps]
    assert rules.count("owl:inverseOf 对称性") == 1
    assert rules.count("RDFS subClassOf 传递闭包") == 1
    assert rules.count("属性域随子类继承") == 9
    assert rules.count("属性值域随子类继承") == 2
    assert any(s["conclusion"] == "Engineer⊆Person" for s in steps)
    assert any(s["conclusion"] == "knowsInv inverseOf knows" for s in steps)
    for s in steps:  # 中文推断描述齐全
        assert s["rule_zh"] and s["premises_zh"] and s["conclusion_zh"]
        assert s["order"]


def test_04_inference_bad_engine_422():
    r = c.get(f"/api/ontologies/{OID}/inference",
              params={"project_id": PID, "engine": "wat"})
    assert r.status_code == 422


def test_05_remote_consistency():
    mock = MockFuseki()
    try:
        local = rengine.run_inference(REC, PID, OID, engine="local")
        remote = rengine.run_inference(REC, PID, OID, engine="fuseki",
                                       base_url=mock.base_url, dataset="ontology")
        assert local["engine"] == "local"
        assert remote["engine"] == "fuseki" and remote["degraded"] is False
        # 推理链路逐字段一致（TP-16-02 核心断言）
        assert remote["steps"] == local["steps"]
        assert len(remote["steps"]) == 13
        # 图信息：本地命名图（schema＋实例）推送到命名图
        g = remote["graph"]
        assert g["uri"] == f"urn:ontology:{OID}"
        assert g["triples"] > g["schema_triples"] > 0
        assert g["triples"] == g["schema_triples"] + g["instance_triples"]
        r = remote["remote"]
        assert r["pushed_triples"] == g["triples"]
        assert r["closure_triples"] >= g["triples"]
        assert r["extra_inferences"] >= 0
        # 混合管道契约：闭包由平台核心计算（closure_computation=local），
        # 派生三元组写回远端物化（materialized_triples），COUNT 读回验证（verified）
        assert r["closure_computation"] == "local"
        assert r["verified"] is True
        assert r["materialized_triples"] >= 0
        assert r["closure_triples"] == g["triples"] + r["materialized_triples"]
        # 混合管道 = 2 次 UPDATE（推送 explicit ＋ 写回 derived）＋ 1 次 COUNT 查询
        assert mock.update_count == 2 and mock.query_count == 1
    finally:
        mock.close()


# ---------------------------------------------------------------------------
# TC-16-03 自动降级
# ---------------------------------------------------------------------------

def test_06_degrade_unreachable():
    local = rengine.run_inference(REC, PID, OID, engine="local")
    out = rengine.run_inference(REC, PID, OID, engine="fuseki",
                                base_url="http://127.0.0.1:9", dataset="ontology",
                                timeout=5.0)
    assert out["engine"] == "local" and out["requested_engine"] == "fuseki"
    assert out["degraded"] is True
    assert out["degrade_reason"] == "fuseki_unreachable"
    assert out["hint_zh"] and "Fuseki" in out["hint_zh"]
    assert out["steps"] == local["steps"]  # 降级后本地链路完整
    assert out["graph"] is None and out["remote"] is None


def test_07_degrade_update_failed():
    mock = MockFuseki(fail_update=True)
    try:
        out = rengine.run_inference(REC, PID, OID, engine="fuseki",
                                    base_url=mock.base_url, dataset="ontology")
        assert out["degraded"] is True
        assert out["degrade_reason"] == "fuseki_update_failed"
        assert out["engine"] == "local" and out["steps"]
    finally:
        mock.close()


def test_08_degrade_query_failed():
    mock = MockFuseki(fail_query=True)
    try:
        out = rengine.run_inference(REC, PID, OID, engine="fuseki",
                                    base_url=mock.base_url, dataset="ontology")
        assert out["degraded"] is True
        assert out["degrade_reason"] == "fuseki_query_failed"
        assert out["engine"] == "local" and out["steps"]
    finally:
        mock.close()


def test_08b_degrade_verify_failed():
    """读回验证失败（远端计数 ≠ 闭包计数，远端存储不可信）→ 降级本地。"""
    mock = MockFuseki(fail_verify=True)
    try:
        out = rengine.run_inference(REC, PID, OID, engine="fuseki",
                                    base_url=mock.base_url, dataset="ontology")
        assert out["degraded"] is True
        assert out["degrade_reason"] == "fuseki_verify_failed"
        assert out["engine"] == "local" and out["steps"]
        assert out["remote"] is None
    finally:
        mock.close()


# ---------------------------------------------------------------------------
# TC-16-04 推理图范围
# ---------------------------------------------------------------------------

def test_09_scope_ok():
    d = _ok(c.get(f"/api/reasoning/scope/{OID}", params={"project_id": PID}))
    assert d["ok"] is True and d["violations"] == []
    assert d["graph"]["uri"] == f"urn:ontology:{OID}"
    assert d["instances"] == {"total": 3, "upload": 2, "source_connection": 1}
    st = d["stats"]
    assert st["type_label_triples"] == 6      # 3 实例 × (rdf:type ＋ 稳定键 label)
    assert st["relation_edge_triples"] == 2   # u1.worksIn ＋ f1.worksIn
    assert st["upload_value_triples"] == 4    # 上传型全值：u1.empId/u1.salary ＋ d1.deptCode/d1.budget
    assert st["other_triples"] == 0


def test_10_scope_violation():
    # 越界模拟：源连接型实例携带非键属性值进入图 → 必须检出
    _insert("v216-f2", "Employee",
            {"empId": "E003", "salary": 9.9,
             "_source": {"kind": "field_map", "connection_id": "conn-v216"}},
            channel="external_mcp")
    d = _ok(c.get(f"/api/reasoning/scope/{OID}", params={"project_id": PID}))
    assert d["ok"] is False
    assert len(d["violations"]) == 1
    v = d["violations"][0]
    assert v["predicate"] == str(attr_uri("salary"))
    assert "urn" not in v["predicate"] and "v216-f2" in v["subject"]
    assert d["instances"]["source_connection"] == 2
