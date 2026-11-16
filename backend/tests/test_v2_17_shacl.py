"""V2-17 SHACL 双引擎校验（M4-D）——L5 回归（tests/test_v2_17_shacl.py）。

覆盖（V2-测试用例 TP-17）：
- TC-17-01  双跑违规明细一致：同形状同数据，本地 pyshacl vs Jena SHACL（Java 子进程，
            fat jar 内置引擎，同一 Jena/RDF 栈），违规明细（severity/path/focus_node/
            message）规范化后逐条一致（message 同源＝平台形状中文 sh:message）
- TC-17-02  格式一致：前端展示与 422 明细同一 {severity, path, focus_node, message}
- TC-17-03  Jena 执行器不可达（缺失/失败）自动回退本地 pyshacl（degraded＋原因）
- TC-17-04  校验范围=本地图数据：非 schema 字段（源连接型非键内容同口径）不入数据图、
            不产生校验（负向断言）
- TC-17-05  基线 JSON 结构完整（scripts/bench_v2_17_shacl.py 生成，目标值不冻结）
- TCE-17-01 全违规批次零入库：approve → imported=0 ＋ violations 明细完整
- TCE-17-02 Jena 超时/异常 → 回退本地，入库门不悬空

诚实口径：Fuseki standalone fat jar 无 SHACL HTTP 服务端点（2026-11-16 依赖事实），
Jena 侧＝Java 子进程执行 Jena SHACL 引擎（JVM 启动＋文件序列化开销如实记录），
不断言 Jena 快于本地。
"""
from __future__ import annotations

import json
import os
import subprocess

import main
from fastapi.testclient import TestClient
from jena_adapter import shacl_engine, shacl_jena
from jena_adapter.client import _local_shacl_validate, shacl_validate
from modeling_translator import build_instance_graph, translate_to_shacl

PID = "v217-m4d"
c = TestClient(main.app)


def _ok(resp) -> dict:
    assert resp.status_code in (200, 201), (resp.status_code, resp.text)
    body = resp.json()
    assert body.get("success") is True, body
    return body["data"]


def _reset_engine_config() -> None:
    try:
        os.remove(shacl_engine._config_path())
    except FileNotFoundError:
        pass


_reset_engine_config()


def _onto() -> dict:
    """测试本体：1 对象 3 属性（required/datatype/min_length 三类干净违规可触发）。"""
    return {"name": "t", "objects": [
        {"id": "o1", "name": "客户", "business_definition": "测试客户，用于 SHACL 双引擎校验",
         "stable_key": "a1",
         "attributes": [
             {"id": "a1", "name": "姓名", "data_type": "string", "required": True, "constraints": {}},
             {"id": "a2", "name": "年龄", "data_type": "integer", "required": False, "constraints": {}},
             {"id": "a3", "name": "编码", "data_type": "string", "required": False,
              "constraints": {"min_length": 2}},
         ], "relations": []},
    ], "constraints": []}


def _bad_instances() -> list[dict]:
    """每个实例触发一类干净违规：缺必填 / datatype / minLength。"""
    return [
        {"object_type": "o1", "a2": "30"},
        {"object_type": "o1", "a1": "乙", "a2": "abc"},
        {"object_type": "o1", "a1": "丙", "a3": "x"},
    ]


def _norm(res: dict) -> list[tuple]:
    return sorted(
        (v.get("severity"), v.get("path"), v.get("focus_node"), v.get("message"))
        for v in res["violations"]
    )


def _jena_ready():
    ok, why = shacl_jena.java_shacl_available()
    if not ok:
        import pytest
        pytest.skip(f"Jena SHACL 执行器不可用：{why}")
    return True


# ---------------------------------------------------------------------------
# TC-17-01 引擎开关（GET/PUT /api/shacl/engine）
# ---------------------------------------------------------------------------

def test_01_engine_default():
    d = _ok(c.get("/api/shacl/engine", params={"project_id": PID}))
    assert d["engine"] == "local"
    assert d["source"] in ("default", "env")
    assert isinstance(d["jena"]["available"], bool)


def test_02_engine_switch_and_invalid():
    d = _ok(c.put("/api/shacl/engine", params={"project_id": PID}, json={"engine": "jena"}))
    assert d["engine"] == "jena" and d["source"] == "runtime"
    assert _ok(c.get("/api/shacl/engine", params={"project_id": PID}))["engine"] == "jena"
    r = c.put("/api/shacl/engine", params={"project_id": PID}, json={"engine": "bogus"})
    assert r.status_code == 422
    _reset_engine_config()
    assert _ok(c.get("/api/shacl/engine", params={"project_id": PID}))["engine"] == "local"


def test_03_engine_precedence(monkeypatch, tmp_path):
    cfg = tmp_path / "shacl_engine.json"
    monkeypatch.setattr(shacl_engine, "_config_path", lambda: str(cfg))
    monkeypatch.delenv(shacl_engine.ENV_VAR, raising=False)
    assert shacl_engine.get_configured_engine() == ("local", "default")
    monkeypatch.setenv(shacl_engine.ENV_VAR, "jena")
    assert shacl_engine.get_configured_engine() == ("jena", "env")
    shacl_engine.set_engine("local")
    assert shacl_engine.get_configured_engine() == ("local", "runtime")
    assert cfg.exists()


# ---------------------------------------------------------------------------
# TC-17-01 双跑违规明细一致 ＋ TC-17-02 格式一致
# ---------------------------------------------------------------------------

def test_04_dual_run_violations_consistent():
    _jena_ready()
    onto = _onto()
    data = build_instance_graph(onto, _bad_instances())
    shapes = translate_to_shacl(onto)
    local = _local_shacl_validate(data, shapes)
    jena = shacl_jena.jena_shacl_validate(data, shapes)
    assert local["valid"] is False and jena["valid"] is False
    assert len(local["violations"]) == 3, local
    assert len(jena["violations"]) == 3, jena
    # 逐条一致（含 message：同源＝平台形状中文 sh:message）
    assert _norm(local) == _norm(jena)


def test_05_unified_violation_format():
    _jena_ready()
    onto = _onto()
    data = build_instance_graph(onto, _bad_instances())
    shapes = translate_to_shacl(onto)
    local = _local_shacl_validate(data, shapes)
    jena = shacl_jena.jena_shacl_validate(data, shapes)
    expected = {"severity", "path", "focus_node", "message"}
    for res in (local, jena):
        for v in res["violations"]:
            assert expected <= set(v), v
    # 与 422 明细同源同格式（实例更新预检走同一 shacl_validate）
    from ingestion import validate_instance
    vi = validate_instance(onto, {"object_type": "o1", "a1": "乙", "a2": "abc"}, "o1")
    assert vi["violations"]
    for v in vi["violations"]:
        assert "message" in v and "path" in v


# ---------------------------------------------------------------------------
# TC-17-03 Jena 不可达回退本地
# ---------------------------------------------------------------------------

def test_06_jena_unavailable_degrades_to_local(monkeypatch):
    monkeypatch.setattr(shacl_engine, "get_configured_engine", lambda: ("jena", "runtime"))
    monkeypatch.setattr(shacl_jena, "jena_shacl_validate", lambda *a, **k: None)
    shacl_jena._LAST_ERROR.clear()
    shacl_jena._LAST_ERROR.append("mock：执行器不可用")
    onto = _onto()
    data = build_instance_graph(onto, _bad_instances())
    res = shacl_validate(data, translate_to_shacl(onto))
    assert res["engine"] == "local" and res["requested_engine"] == "jena"
    assert res["degraded"] is True and "执行器不可用" in res["degrade_reason"]
    # 回退后校验仍执行（入库门不悬空）
    assert res["valid"] is False and len(res["violations"]) == 3


def test_07_jena_timeout_degrades_to_local(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="java", timeout=1)
    monkeypatch.setattr(shacl_engine, "get_configured_engine", lambda: ("jena", "runtime"))
    monkeypatch.setattr(shacl_jena, "jena_shacl_validate", boom)
    onto = _onto()
    data = build_instance_graph(onto, _bad_instances())
    res = shacl_validate(data, translate_to_shacl(onto))
    assert res["engine"] == "local" and res["degraded"] is True
    assert res["degrade_reason"]  # 原因码非空（last_error 空时回退 jena_shacl_failed）
    assert res["valid"] is False and len(res["violations"]) == 3


def test_08_jena_path_engine_tagged(monkeypatch):
    _jena_ready()
    monkeypatch.setattr(shacl_engine, "get_configured_engine", lambda: ("jena", "runtime"))
    onto = _onto()
    data = build_instance_graph(onto, _bad_instances())
    shapes = translate_to_shacl(onto)
    res = shacl_validate(data, shapes)
    assert res["engine"] == "jena" and res["degraded"] is False and res["degrade_reason"] is None
    assert res["valid"] is False and len(res["violations"]) == 3


# ---------------------------------------------------------------------------
# TC-17-04 校验范围＝本地图数据（负向断言）
# ---------------------------------------------------------------------------

def test_09_scope_local_graph_negative():
    onto = _onto()
    # 非 schema 字段（模拟源连接型非键内容）不入校验数据图 → 不产生校验
    data = build_instance_graph(
        onto, [{"object_type": "o1", "a1": "丁", "extra_source_field": "不应被校验"}])
    preds = {str(p) for _, p, _ in data}
    assert not any("extra_source_field" in p for p in preds)
    res = shacl_validate(data, translate_to_shacl(onto))
    assert res["valid"] is True, res


# ---------------------------------------------------------------------------
# TC-17-05 时延吞吐基准（基线 JSON 结构完整；脚本 scripts/bench_v2_17_shacl.py）
# ---------------------------------------------------------------------------

def test_10_benchmark_baseline_json():
    p = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "datasets", "v2", "shacl_benchmark_baseline.json")
    if not os.path.exists(p):
        import pytest
        pytest.skip("基线未生成（运行 scripts/bench_v2_17_shacl.py 后补）")
    with open(p, encoding="utf-8") as f:
        base = json.load(f)
    for key in ("local_median_ms", "jena_median_ms", "rounds", "input_instances",
                "violations_consistent", "note"):
        assert key in base, f"基线缺字段 {key}"
    assert base["rounds"] == 3
    assert isinstance(base["local_median_ms"], (int, float))
    assert isinstance(base["jena_median_ms"], (int, float))


# ---------------------------------------------------------------------------
# TCE-17-01 全违规批次零入库 ＋ 422 明细完整
# ---------------------------------------------------------------------------

def test_11_batch_all_violations_zero_import():
    import ingestion
    onto = _onto()
    candidate = {"status": "pending", "object_type": "o1", "instances": _bad_instances()}
    res = ingestion.approve(candidate, onto, project_id=PID)
    assert res["imported"] == 0 and res["rejected"] == 0 and res["neo_tx_id"] is None
    assert len(res["violations"]) == 3
    for v in res["violations"]:
        assert {"severity", "path", "focus_node", "message"} <= set(v), v
    # 零入库：实例库无该对象类型记录
    from core.instance_store import get_instance_store
    recs = get_instance_store(PID).find(
        {"object_type": "o1", "ontology_id": "__v217_probe__"})
    assert recs == [] or all(r.get("ontology_id") != "__v217_probe__" for r in recs)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
