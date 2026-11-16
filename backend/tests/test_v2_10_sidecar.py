"""V2-10 JSONL append-only 旁路操作日志（M4-G）——测试（tests/test_v2_10_sidecar.py）。

覆盖（V2-测试用例 TP-10）：
- TC-10-01  本体变更 / Action 执行 / 外部读取 → 统一 append-only 旁路日志（schema 完整、不覆盖主存储）
- TC-10-02  按天回放：重建当日 Action 回写序列（追加序，供与外部系统对账）
- TC-10-03  append-only + 哈希链：篡改任一行 → 链断裂可检测；接口无修改/删除
- TCE-10-01 旁路写失败不影响主流程：record 写抛异常被吞，主流程照常完成

所有记录通过 monkeypatch 重定向 `sidecar_log._log_path` 到临时文件，避免污染 backend/data。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from api import router_ontologies as ont_router  # noqa: F401  (确保路由已挂载)
import sidecar_log as sc

c = TestClient(main.app)

# 三类事件类型
C = sc.TYPE_ONTOLOGY_CHANGE
A = sc.TYPE_ACTION_EXECUTION
R = sc.TYPE_EXTERNAL_READ


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    """把 sidecar 日志重定向到 tmp 目录，每个测试独立。"""
    target = tmp_path / "sidecar.jsonl"
    monkeypatch.setattr(sc, "_log_path", lambda: target)
    return target


# ----------------------------------------------------------------------
# TC-10-01 本体变更 / Action 执行 / 外部读取 → 统一 append-only 旁路日志
# ----------------------------------------------------------------------
def test_10_01_three_event_types_append_only(log_dir):
    """三类事件写入同一 JSONL，schema 完整，逐行追加。"""
    sc.record(C, actor="alice", project_id="p1", target_type="ontology",
              target_id="ont-1", payload={"action": "create", "name": "订单"})
    sc.record(A, actor="bob", project_id="p1", target_type="action",
              target_id="act-1", payload={"action_id": "act-1", "kind": "webhook"},
              result={"executed": True, "outcome": "success"})
    sc.record(R, actor="carol", project_id="p1", target_type="source_connection",
              target_id="conn-9", payload={"key_field": "code", "key_value": "C1"},
              result={"values": {"amount": 12.5}})

    lines = log_dir.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3  # 追加，非覆盖
    recs = [json.loads(l) for l in lines]
    types = [r["type"] for r in recs]
    assert types == [C, A, R]
    # 统一 schema 必需字段
    for r in recs:
        for k in ("id", "time", "actor", "type", "project_id",
                  "target_type", "target_id", "payload", "result", "prev_hash"):
            assert k in r, f"缺字段 {k}: {r!r}"
        assert r["time"], "time 须有值"
    # 首行 prev_hash 为创世值
    assert recs[0]["prev_hash"] == "0" * 64
    # 后续行 prev_hash = 前一行哈希
    h0 = sc._line_hash(lines[0])
    assert recs[1]["prev_hash"] == h0
    h1 = sc._line_hash(lines[1])
    assert recs[2]["prev_hash"] == h1


def test_10_01_api_trigger_ontology_change(log_dir):
    """真实 API 创建本体 → 触发 ontology_change 旁路事件（验证接入点生效）。

    本体写真实 data 目录（与 test_ontologies_api 一致）；sidecar 经 monkeypatch 写 tmp，
    故读取 tmp 即可验证 create_ontology 的接入点。
    """
    payload = {
        "name": "旁路测试本体",
        "modeling_type": "manual",
        "objects": [
            {"id": "obj_c",
             "stable_key": "attr_name",
             "business_definition": "测试", "name": "客户",
             "attributes": [{"id": "attr_name", "name": "名称", "data_type": "string",
                             "required": True, "constraints": {}}],
             "relations": []},
        ],
        "constraints": [],
    }
    r = c.post("/api/ontologies", json=payload)
    assert r.status_code == 201, r.text

    # 创建本体后，sidecar 应有一条 ontology_change 记录（type/actor 完整）
    res = sc.query(type_=C)
    assert res["total"] >= 1
    rec = next((x for x in res["items"] if x["payload"]["action"] == "create"), None)
    assert rec is not None
    assert rec["type"] == C
    assert rec["target_type"] == "ontology"


def test_10_01_query_filter(log_dir):
    """组合检索：按类型/操作者/项目/按天过滤。"""
    sc.record(C, actor="alice", project_id="p1", target_type="ontology", target_id="o1")
    sc.record(C, actor="alice", project_id="p1", target_type="ontology", target_id="o2")
    sc.record(A, actor="bob", project_id="p2", target_type="action", target_id="a1")

    res = sc.query(type_=C)
    assert res["total"] == 2
    res = sc.query(type_=C, actor="alice")
    assert res["total"] == 2
    res = sc.query(type_=C, project_id="p1")
    assert res["total"] == 2
    res = sc.query(type_=C, project_id="p2")
    assert res["total"] == 0
    res = sc.query(type_=A)
    assert res["total"] == 1


# ----------------------------------------------------------------------
# TC-10-02 按天回放
# ----------------------------------------------------------------------
def test_10_02_replay_day_order(log_dir):
    """按天回放：当日 Action 事件按追加序返回（供对账）。"""
    sc.record(A, actor="alice", project_id="p1", target_type="action", target_id="a1")
    sc.record(C, actor="alice", project_id="p1", target_type="ontology", target_id="o1")
    sc.record(A, actor="bob", project_id="p1", target_type="action", target_id="a2")

    import datetime
    day = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
    replay = sc.replay_day(day, type_=A)
    assert [r["target_id"] for r in replay] == ["a1", "a2"]  # 追加序，只含 Action

    # 没有任何记录的日期 → 空
    assert sc.replay_day("1999-01-01", type_=A) == []


def test_10_02_stats(log_dir):
    """统计：按类型/操作者计数。"""
    sc.record(C, actor="alice", project_id="p1")
    sc.record(C, actor="alice", project_id="p1")
    sc.record(A, actor="bob", project_id="p1")
    st = sc.stats()
    assert st["total"] == 3
    assert st["by_type"] == {C: 2, A: 1}
    assert st["by_actor"] == {"alice": 2, "bob": 1}


# ----------------------------------------------------------------------
# TC-10-03 append-only + 哈希链
# ----------------------------------------------------------------------
def test_10_03_verify_chain_ok_and_tamper(log_dir):
    """哈希链：未篡改 → ok；篡改任一行 → 断裂可检测。"""
    sc.record(C, actor="alice", project_id="p1", target_id="o1")
    sc.record(A, actor="bob", project_id="p1", target_id="a1")
    sc.record(R, actor="carol", project_id="p1", target_id="conn1")

    v = sc.verify_chain()
    assert v["ok"] is True
    assert v["checked"] == 3

    # 篡改第 2 行内容（改 target_id）→ 该行哈希变化，第 3 行 prev_hash 不再匹配
    lines = log_dir.read_text(encoding="utf-8").strip().splitlines()
    tampered = json.loads(lines[1])
    tampered["target_id"] = "TAMPERED"
    lines[1] = json.dumps(tampered, ensure_ascii=False)
    log_dir.write_text("\n".join(lines) + "\n", encoding="utf-8")
    v2 = sc.verify_chain()
    assert v2["ok"] is False
    assert v2["broken_at"] == 3  # 第 3 行的 prev_hash 与已改的第 2 行哈希不一致


def test_10_03_immutable_api(log_dir):
    """旁路日志接口无修改/删除：PUT/PATCH/DELETE → 405。"""
    sc.record(C, actor="alice", project_id="p1", target_id="o1")
    for method in ("put", "patch", "delete"):
        resp = getattr(c, method)("/api/sidecar/whatever")
        assert resp.status_code == 405, f"{method} 应 405，实际 {resp.status_code}"
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "SIDECAR_IMMUTABLE"


def test_10_03_read_api(log_dir):
    """读取/统计/回放/校验 API 正常返回。"""
    sc.record(C, actor="alice", project_id="p1", target_id="o1")
    sc.record(A, actor="bob", project_id="p1", target_id="a1")

    r = c.get("/api/sidecar", params={"type": C})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 1

    r = c.get("/api/sidecar/stats")
    assert r.status_code == 200
    st = r.json()["data"]
    assert st["total"] == 2

    import datetime
    day = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
    r = c.get("/api/sidecar/replay", params={"day": day})
    assert r.status_code == 200
    rp = r.json()["data"]
    assert rp["day"] == day
    assert rp["total"] == 1  # 默认只回放 Action

    r = c.get("/api/sidecar/verify")
    assert r.status_code == 200
    assert r.json()["data"]["ok"] is True


# ----------------------------------------------------------------------
# TCE-10-01 旁路写失败不影响主流程
# ----------------------------------------------------------------------
def test_10_e1_write_failure_does_not_break(log_dir, monkeypatch):
    """record 写失败（如磁盘/权限异常）被吞掉，不抛出也不伪造。"""
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(sc, "_log_path", boom)
    # 不应抛异常（旁路）
    rec = sc.record(C, actor="alice", project_id="p1", target_id="o1")
    assert rec is not None  # 即使写失败也返回记录对象（主流程可用）
    assert rec["type"] == C
    # 没有写坏任何东西
    assert not log_dir.exists() or log_dir.read_text(encoding="utf-8").strip() == ""


def test_10_02_action_writeback_writes_sidecar(log_dir):
    """Action 执行（_do_writeback 成功路径）→ 写入 action_execution 旁路事件。"""
    from action_engine import ActionEngine

    eng = ActionEngine()
    action_def = {
        "action_id": "act-10-2",
        "name": "同步",
        "invoke": {"kind": "webhook", "url": "http://127.0.0.1:1/hook", "method": "POST"},
        "risk_level": "low",
    }
    pending = {
        "action": action_def,
        "planned_payload": {"id": "x"},
        "input": {},
        "project_id": "p10",
        "risk_level": "low",
        "display_only": True,   # 不真实外呼（writeback_enabled=False 默认），但走统一管线
        "auto": True,
        "evaluation": None,
    }
    result = eng._do_writeback(pending, actor={"username": "alice"}, trigger_source="api")
    # display_only=True 下 executed 应为 True（模拟视为成功）
    assert result.get("executed") is True, result
    # sidecar 应有一条 action_execution 记录
    res = sc.query(type_=A, project_id="p10")
    assert res["total"] >= 1
    rec = res["items"][0]
    assert rec["target_id"] == "act-10-2"
    assert rec["payload"]["action_id"] == "act-10-2"
    assert rec["result"]["outcome"] == "simulated"


def test_10_e1_action_still_runs_on_sidecar_failure(monkeypatch):
    """Action 执行路径：sidecar 写失败时，ActionEngine 主流程（_do_writeback）不受影响。

    让 `_log_path` 抛异常（模拟磁盘/权限失败），`sidecar_log.record` 的内部
    try/except 会吞掉；ActionEngine 对 sidecar 调用另有外层 try/except 兜底，主流程照常完成。
    """
    from action_engine import ActionEngine

    def boom(*a, **k):
        raise OSError("sidecar disk")

    monkeypatch.setattr(sc, "_log_path", boom)
    monkeypatch.setattr(sc, "_CHAIN_GENESIS", "0" * 64)

    eng = ActionEngine()
    action_def = {
        "action_id": "act-e1",
        "name": "同步",
        "invoke": {"kind": "webhook", "url": "http://127.0.0.1:1/hook", "method": "POST"},
        "risk_level": "low",
    }
    pending = {
        "action": action_def,
        "planned_payload": {"id": "x"},
        "input": {},
        "project_id": "p1",
        "risk_level": "low",
        "display_only": True,
        "auto": True,
        "evaluation": None,
    }
    # display_only=True 时 _do_writeback 走模拟且会触发 sidecar；sidecar 失败不崩即通过
    result = eng._do_writeback(pending, actor={"username": "alice"}, trigger_source="api")
    assert "executed" in result  # 主流程返回了结果（不受 sidecar 失败影响）


def test_10_e1_geo_write_failure_swallowed(monkeypatch):
    """本体变更/外部读取路径中 sidecar 写失败同样被吞（record 内 try/except）。"""
    def boom(*a, **k):
        raise OSError("sidecar disk")

    monkeypatch.setattr(sc, "_log_path", boom)
    class _Req:
        state = type("S", (), {"auth_user": {"username": "alice", "role": "admin"}})()
    # record_from_request → record → _log_path boom → 被 record 内部 try/except 吞掉，不抛
    rec = sc.record_from_request(_Req(), C, project_id="p1", target_type="ontology", target_id="o1")
    assert rec["type"] == C
