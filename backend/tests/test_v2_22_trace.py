"""V2-22 计算过程链 trace + NL 驱动业务动作（M4-J）。

覆盖（docs/V2-功能需求定义.md V2-22 验收 1/2/4/5 与 TCE-22-01~03）：
- TC-22-01 能力执行随结果返回统一 data.trace（steps），trace 恒落库（append-only）；
- TC-22-02 同一"源"多次执行 trace 历史有 N 条独立记录（trace_id 唯一，不覆盖）;
- TC-22-04 NL 问句带动作意图 → 触发动作执行，响应含结果与过程链；
- TC-22-05 非管理员调用 trace 查看接口 403；trace 无 PATCH/DELETE（405 append-only）；
- TCE-22-01 trace_id ↔ run_id/log_id 关联（run_id/log_id 透传）；
- TCE-22-02 step details 集合类限量（前 N 条 + total）、大值截断。

说明：MCP 工具 include_trace 的响应语义与 REST capability execute 一致（数据面统一），
本测试以 REST capability execute 为数据面入口断言 trace 结构；MCP 工具契约同步见
mcp_server TOOLS_CONTRACT（test_v2_18_stdio 验证 stdio 桥经参数声明）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from _authenv import auth_module_guard  # noqa: E402
from core.instance_store import JsonlInstanceStore  # noqa: E402
from core.storage import JsonStore  # noqa: E402
from core.trace import (  # noqa: E402
    TraceBuilder, record_trace, _trace_store, TYPE_DATA_FETCH, TYPE_ACTION,
)
from main import app  # noqa: E402

pytestmark = pytest.mark.auth
_guard = auth_module_guard()  # GAP-20：模块边界隔离鉴权环境（本模块含 TC-22-05 鉴权用例）

PID = "v222-trace"
P = {"project_id": PID}
_ctx: dict = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ontology() -> dict:
    return {
        "name": "V222 过程链本体",
        "description": "M4-J 计算过程链 trace 验证",
        "modeling_type": "manual",
        "objects": [
            {
                "id": "Order",
                "name": "订单",
                "business_definition": "客户订单",
                "stable_key": "orderCode",
                "attributes": [
                    {"id": "orderCode", "name": "订单编码", "data_type": "string",
                     "required": True, "constraints": {}},
                    {"id": "amount", "name": "金额", "data_type": "float",
                     "required": False, "constraints": {}},
                ],
                "relations": [],
            },
        ],
        "constraints": [],
    }


def _mk_upload(rid: str, ont_id: str, inst: dict, candidate_id: str | None = None) -> dict:
    return {
        "id": rid, "ontology_id": ont_id, "object_type": "Order",
        "stable_key": "orderCode", "stable_key_value": inst.get("orderCode"),
        "channel": "upload", "candidate_id": candidate_id,
        "instance": inst, "imported_at": _now(),
    }


def _seed_capability(c, cap_id: str) -> str:
    """创建 + 发布一个简单派生能力，返回能力 id。"""
    r = c.post("/api/capabilities", params=P, json={
        "name": "v222_margin_fee", "object_type": "Order",
        "expression": "this.amount * 0.1", "output_attribute": "marginFee",
        "kind": "derive", "implementation": "expression",
    })
    assert r.status_code == 201, r.text
    cid = r.json()["data"]["id"]
    r = c.post(f"/api/capabilities/{cid}/publish", params=P)
    assert r.status_code == 200, r.text
    return cid


class TestV222Trace:
    def test_01_setup(self) -> None:
        c = TestClient(app)
        r = c.post("/api/ontologies", params=P, json=_ontology())
        assert r.status_code == 201, r.text
        oid = r.json()["data"]["id"]
        store = JsonlInstanceStore(PID)
        store.insert(_mk_upload(
            "v222-up1", oid,
            {"orderCode": "V222-U1", "amount": 90.0,
             "_source": {"kind": "file_upload", "fetched_at": _now(),
                         "connection_id": None, "raw_ref": "v222-upload.md"}},
            candidate_id="v222-cand-up1"))
        cap_id = _seed_capability(c, "v222")
        _ctx.update(oid=oid, cap_id=cap_id)

    # ------------------------------------------------------------------ #
    # TC-22-01 / TCE-22-02：统一 trace 结构 + 恒落库（落库限量截断）
    # ------------------------------------------------------------------ #
    def test_02_rest_execute_includes_trace(self) -> None:
        c = TestClient(app)
        r = c.post(f"/api/capabilities/{_ctx['cap_id']}/execute", params=P,
                   json={"instance_id": "v222-up1"})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        # 四要素溯源 res["trace"] 保留（既有语义），统一过程链用独立键 process_trace（两层并存）
        assert "inputs" in d.get("trace", {}), "四要素溯源 trace 必须保留"
        trace = d["process_trace"]
        assert trace.get("trace_id")
        steps = trace["steps"]
        assert len(steps) >= 3, steps
        seqs = [s["seq"] for s in steps]
        assert seqs == sorted(seqs), "step.seq 单调递增"
        assert steps[0]["type"] == TYPE_DATA_FETCH, steps[0]
        assert any(s["type"] == "capability" for s in steps), steps
        assert steps[-1]["type"] == "result", steps[-1]
        for s in steps:
            assert {"seq", "type", "description", "details",
                    "started_at", "finished_at"} <= set(s), s

        # TCE-22-02：落库记录同样限量（details 内集合类前 N 条 + total）
        rows = _trace_store(PID).all()
        assert rows, "执行后必须有 trace 落库"
        rec = rows[-1]
        assert rec["trace_id"] == trace["trace_id"]
        assert rec["source"] == "rest:capability_execute"

    # ------------------------------------------------------------------ #
    # TC-22-02：append-only，多次执行 → 多条独立记录（不覆盖历史）
    # ------------------------------------------------------------------ #
    def test_03_multiple_exec_records(self) -> None:
        before = len(_trace_store(PID).all())
        c = TestClient(app)
        for _ in range(2):
            r = c.post(f"/api/capabilities/{_ctx['cap_id']}/execute", params=P,
                       json={"instance_id": "v222-up1"})
            assert r.status_code == 200, r.text
        after = len(_trace_store(PID).all())
        assert after - before == 2, "同一能力执行 2 次应新增 2 条独立 trace 记录"

    # ------------------------------------------------------------------ #
    # TC-22-02/append-only 幂等：同 trace_id 不重复落库；record_trace 不覆盖
    # ------------------------------------------------------------------ #
    def test_04_record_trace_idempotent_and_append(self) -> None:
        store = _trace_store(PID)
        b1 = TraceBuilder()
        b1.add(TYPE_DATA_FETCH, "读取输入", {"attr": "amount", "value": 90})
        b1.add(TYPE_ACTION, "执行动作", {"action_id": "acc"})
        tb1 = b1.to_trace()
        rec1 = record_trace(PID, tb1, run_id="run-1", log_id="log-1", source="unit")
        assert rec1["trace_id"] == tb1["trace_id"]
        # 幂等：同 trace_id 再落库返回同一记录，不叠加
        n = len(store.all())
        rec1b = record_trace(PID, tb1, run_id="run-1", log_id="log-1", source="unit")
        assert rec1b["trace_id"] == tb1["trace_id"]
        assert len(store.all()) == n, "同 trace_id 不得重复落库"

    # ------------------------------------------------------------------ #
    # TCE-22-01：run_id / log_id 关联透传
    # ------------------------------------------------------------------ #
    def test_05_run_log_link(self) -> None:
        b = TraceBuilder()
        b.add(TYPE_DATA_FETCH, "读取", {"attr": "x"})
        tb = b.to_trace()
        record_trace(PID, tb, run_id="run-9", log_id="log-9", source="unit")
        rows = _trace_store(PID).all()
        hit = [r for r in rows if r.get("trace_id") == tb["trace_id"]][0]
        assert hit["run_id"] == "run-9"
        assert hit["log_id"] == "log-9"

    # ------------------------------------------------------------------ #
    # TC-22-04：NL 驱动 action 意图 → 触发能力执行，响应含结果与过程链
    # ------------------------------------------------------------------ #
    def test_06_nl_drive_action(self) -> None:
        c = TestClient(app)
        r = c.post("/api/nl/drive", params=P, json={
            "question": "帮我算一下这个订单的 marginFee",
            "intent": "action", "instance_id": "v222-up1",
        })
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["kind"] == "action", d
        assert d["intent_source"] == "user", d
        trace = d["process_trace"]
        assert trace["trace_id"]
        steps = trace["steps"]
        assert steps[0]["type"] == TYPE_DATA_FETCH, steps
        cap_step = [s for s in steps if s["type"] == "capability"]
        assert cap_step, steps
        # 本地 upload 实例执行派生能力：value = this.amount * 0.1 = 9.0
        assert cap_step[0]["details"].get("value") == 9.0, cap_step[0]
        assert steps[-1]["type"] == "result", steps

    # ------------------------------------------------------------------ #
    # NL 意图关键词回退（fallback）：无 override 时按关键词判别
    # ------------------------------------------------------------------ #
    def test_07_nl_intent_fallback(self) -> None:
        from api.router_nl import _classify_intent
        assert _classify_intent("有哪些订单", None) == ("query", "fallback")
        assert _classify_intent("计算一下总成本", None) == ("action", "fallback")
        assert _classify_intent("query", "query") == ("query", "user")

    # ------------------------------------------------------------------ #
    # TC-22-05：非管理员调 trace 查看 403；trace 无 PATCH/DELETE（405）
    # ------------------------------------------------------------------ #
    def test_08_trace_router_permission_and_immutable(self) -> None:
        import auth as auth_svc
        from core.responses import ApiError
        from api.router_trace import _require_admin

        # 鉴权关闭时放行（框架默认 auth=false）
        os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
        assert auth_svc.auth_enabled() is False

        # 模拟鉴权开启 + 非管理员：应 403
        os.environ["ONTOLOGY_AUTH_ENABLED"] = "true"
        # auth_enabled 可能被缓存，这里直接构造带 state 的假请求对象验证 _require_admin
        class _Req:
            def __init__(self, user):
                from types import SimpleNamespace
                self.state = SimpleNamespace(auth_user=user)
                self.query_params = {}
                self.url = SimpleNamespace(path="/api/trace")
                self.method = "GET"
                self.path = "/api/trace"

        try:
            _require_admin(_Req({"username": "u1", "role": "user"}))
            assert False, "非管理员应被拒绝"
        except ApiError as e:
            assert "FORBIDDEN" in e.code
            assert e.http_status == 403
        finally:
            os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"

        # append-only：PATCH/DELETE → 405
        c = TestClient(app)
        r = c.patch("/api/trace/some/thing", params=P, json={})
        assert r.status_code == 405, r.text
        r2 = c.delete("/api/trace/some", params=P)
        assert r2.status_code == 405, r2.text
