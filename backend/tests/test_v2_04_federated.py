"""M4-A④：源连接型能力平台服务端联邦读（TP-04-06 / EP-04-03 / TP-21-08）。

TP-04-06：
  1. 执行前成本提示：POST /{cap}/execute/preview（纯只读）＋执行时 409 COST_CONFIRMATION_REQUIRED
     （details 内嵌成本预览；可取消=不重试即可）
  2. 派生值落派生图（derived_values：derived=True / authoritative=False / 源系统 / fetched_at / trace），
     不写权威实例存储、不写回源系统（平台对源系统只读）
  3. trace 原始值含源系统＋fetched_at
EP-04-03：成本确认可取消；部分源失败 → 失败实例标记 federated_failed（无值、无派生），不产生半口径结果
TP-21-08：数据健康快照 extensions 四扩展维度可查（字典映射覆盖率/跨系统同键冲突率/实例对齐率/血缘完整率）
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient  # noqa: E402

from core.instance_store import JsonlInstanceStore  # noqa: E402
from core.storage import JsonStore  # noqa: E402
from main import app  # noqa: E402
from tests.mock_mcp_http import start_mock  # noqa: E402

PID = "v204-m4a4"
P = {"project_id": PID}
_ctx: dict = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ontology() -> dict:
    return {
        "name": "V204 联邦能力本体",
        "description": "M4-A④ 源连接型能力联邦读验证",
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
                    {"id": "customerCode", "name": "客户编码", "data_type": "string",
                     "required": False, "constraints": {}},
                ],
                "relations": [],
            },
        ],
        "constraints": [],
    }


def _mk_upload(rid: str, ont_id: str, inst: dict, candidate_id: str | None = None) -> dict:
    return {
        "id": rid,
        "ontology_id": ont_id,
        "object_type": "Order",
        "stable_key": "orderCode",
        "stable_key_value": inst.get("orderCode"),
        "channel": "upload",
        "candidate_id": candidate_id,
        "instance": inst,
        "imported_at": _now(),
    }


class TestV204M4a4:
    # ------------------------------------------------------------------ #
    # 夹具：本体 + mock ERP 连接（3 条记录）+ 审批入库 + 上传型对照 + 能力
    # ------------------------------------------------------------------ #
    def test_01_setup(self) -> None:
        c = TestClient(app)
        r = c.post("/api/ontologies", params=P, json=_ontology())
        assert r.status_code == 201, r.text
        oid = r.json()["data"]["id"]

        os.environ["V204_MOCK_TOKEN"] = "secret-v204"
        mock = start_mock(read_records=[
            {"order_code": "V204-O1", "amount": 55.5, "customer_code": "C001"},
            {"order_code": "V204-O2", "amount": 66.6, "customer_code": "C002"},
            {"order_code": "V204-O3", "amount": 77.7, "customer_code": "C003"},
        ])
        body = {
            "display_name": "V204 mock ERP",
            "transport": "http",
            "endpoint": mock.read_url,
            "auth": {"type": "bearer_token", "bearer_token_ref": "${V204_MOCK_TOKEN}"},
            "tool_allowlist": ["read"],
            "maps_to_ontology": {
                "ontology_id": oid,
                "object_type": "Order",
                "field_map": {"order_code": "orderCode", "amount": "amount",
                              "customer_code": "customerCode"},
                "read_tool": "read",
            },
        }
        r = c.post("/api/mcp/connections", params=P, json=body)
        assert r.status_code == 201, r.text
        cid = r.json()["data"]["connection_id"]

        r = c.post(f"/api/mcp/connections/{cid}/read", params=P)
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["record_count"] == 3, d
        cand_id = d["candidate_id"]
        r = c.post(f"/api/ingest/candidates/{cand_id}/approve", params=P)
        assert r.status_code == 200, r.text

        r = c.get("/api/ingest/instances", params={**P, "ontology_id": oid})
        assert r.status_code == 200
        items = r.json()["data"]["items"]
        assert len(items) == 3
        by_key = {it["stable_key_value"]: it["id"] for it in items}

        # 上传型对照实例（本地值；走本地渠道）
        store = JsonlInstanceStore(PID)
        up_cand = {"id": "v204-cand-up1", "project_id": PID, "ontology_id": oid,
                   "object_type": "Order", "source_type": "file_upload",
                   "mapping": {"field_map": {"order_code": "orderCode",
                                             "amount": "amount",
                                             "customer_code": "customerCode"}}}
        JsonStore("ingest_candidates", PID).insert(up_cand)
        store.insert(_mk_upload(
            "v204-up1", oid,
            {"orderCode": "V204-U9", "amount": 90.0, "customerCode": "C009",
             "_source": {"kind": "file_upload", "fetched_at": _now(),
                         "connection_id": None, "raw_ref": "v204-upload.md"}},
            candidate_id="v204-cand-up1"))

        # 能力：marginFee = this.amount * 0.1
        r = c.post("/api/capabilities", params=P, json={
            "name": "v204_margin_fee",
            "object_type": "Order",
            "expression": "this.amount * 0.1",
            "output_attribute": "marginFee",
            "kind": "derive",
            "implementation": "expression",
            "ontology_id": oid,
        })
        assert r.status_code == 201, r.text
        cap_id = r.json()["data"]["id"]
        r = c.post(f"/api/capabilities/{cap_id}/publish", params=P)
        assert r.status_code == 200, r.text

        _ctx.update(c=c, oid=oid, cid=cid, cap_id=cap_id, mock=mock, store=store,
                    fed_ids=by_key, up1="v204-up1")

    # ------------------------------------------------------------------ #
    # TP-04-06 ①：执行前成本提示（纯只读预览）
    # ------------------------------------------------------------------ #
    def test_02_preview_cost(self) -> None:
        c, cap_id, cid = _ctx["c"], _ctx["cap_id"], _ctx["cid"]
        r = c.post(f"/api/capabilities/{cap_id}/execute/preview", params=P, json={})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["capability_id"] == cap_id
        assert d["total_instances"] == 4            # 3 联邦 + 1 上传
        assert d["federated_instances"] == 3
        assert d["local_instances"] == 1
        assert len(d["systems"]) == 1
        s = d["systems"][0]
        assert s["connection_id"] == cid
        assert s["name"] == "V204 mock ERP"
        assert s["instances"] == 3
        assert d["required_attrs"] == ["amount"]
        assert d["total_federated_reads"] == 3
        assert d["est_scale"] == 3
        assert d["confirm_required"] is True
        assert d["cancelable"] is True
        assert d["cost_hint"]

        # 指定实例子集
        r = c.post(f"/api/capabilities/{cap_id}/execute/preview", params=P,
                   json={"instance_ids": [_ctx["fed_ids"]["V204-O1"]]})
        assert r.status_code == 200
        d2 = r.json()["data"]
        assert d2["total_federated_reads"] == 1
        assert d2["local_instances"] == 0

    # ------------------------------------------------------------------ #
    # EP-04-03：未确认成本 → 409（可取消＝不重试；未确认前不读源）
    # ------------------------------------------------------------------ #
    def test_03_unconfirmed_409(self) -> None:
        c, cap_id, mock = _ctx["c"], _ctx["cap_id"], _ctx["mock"]
        o1 = _ctx["fed_ids"]["V204-O1"]
        n_reads = len(mock.read_calls)
        r = c.post(f"/api/capabilities/{cap_id}/execute", params=P,
                   json={"instance_id": o1})
        assert r.status_code == 409, r.text
        err = r.json()["error"]
        assert err["code"] == "COST_CONFIRMATION_REQUIRED"
        det = err.get("details") or {}
        assert det.get("confirm_required") is True
        assert det.get("cancelable") is True
        assert det.get("cost_hint")
        assert det.get("total_federated_reads") == 1
        assert len(mock.read_calls) == n_reads, "未确认成本不得读源"

        # 批量同样 409
        rb = c.post(f"/api/capabilities/{cap_id}/execute-batch", params=P, json={})
        assert rb.status_code == 409
        assert rb.json()["error"]["code"] == "COST_CONFIRMATION_REQUIRED"
        assert rb.json()["error"]["details"]["total_federated_reads"] == 3

    # ------------------------------------------------------------------ #
    # TP-04-06 ②③：确认执行 → 成功；trace 含源系统+fetched_at；派生值落派生图
    # ------------------------------------------------------------------ #
    def test_04_confirmed_execute(self) -> None:
        c, cap_id, cid, mock = _ctx["c"], _ctx["cap_id"], _ctx["cid"], _ctx["mock"]
        o1 = _ctx["fed_ids"]["V204-O1"]
        n_reads = len(mock.read_calls)
        r = c.post(f"/api/capabilities/{cap_id}/execute", params=P,
                   json={"instance_id": o1, "cost_confirmed": True})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["status"] == "success"
        assert abs(d["value"] - 5.55) < 1e-9
        assert d["input_source"] == "server_federated_read"
        assert d["inputs_persisted"] is False
        fed = d["federated"]
        assert fed["source_connection"] == cid
        assert fed["connection_name"] == "V204 mock ERP"
        assert fed["mode"] == "federated"
        assert fed["fetched_at"]
        assert fed["resolved_as"] in {"viewer", "governance"}
        # trace 原始值含源系统 + fetched_at
        amt = [e for e in d["trace"]["inputs"] if e.get("attr") == "amount"]
        assert amt, d["trace"]
        assert amt[0]["source_channel"] == "federated_read"
        assert amt[0]["source_system"] == "V204 mock ERP"
        assert amt[0]["fetched_at"]
        assert len(mock.read_calls) == n_reads + 1, "平台应真实回源一次"
        # 派生值落派生图（执行响应只回摘要；完整记录经 GET /derived 查询）
        dv = d.get("derived")
        assert dv and dv["authoritative"] is False
        assert "非权威" in dv.get("note", "")
        assert dv["id"].startswith("dv_")
        _ctx["dv1"] = dv["id"]

    # ------------------------------------------------------------------ #
    # TP-04-06：不写权威实例存储、不写回源系统；派生图可查
    # ------------------------------------------------------------------ #
    def test_05_no_writeback(self) -> None:
        c, cap_id, cid, mock = _ctx["c"], _ctx["cap_id"], _ctx["cid"], _ctx["mock"]
        o1 = _ctx["fed_ids"]["V204-O1"]
        # 权威实例存储未被写入派生值
        r = c.get(f"/api/ingest/instances/{o1}", params=P)
        assert r.status_code == 200
        inst = r.json()["data"]["instance"]
        assert "marginFee" not in inst
        # 源系统未被写回（平台只读）
        recs = mock.read_records
        assert len(recs) == 3
        assert recs[0]["amount"] == 55.5
        # 派生图查询
        r = c.get(f"/api/capabilities/{cap_id}/derived",
                  params={**P, "instance_id": o1})
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["total"] == 1
        it = d["items"][0]
        assert it["id"] == _ctx["dv1"]
        assert abs(it["value"] - 5.55) < 1e-9
        assert it["derived"] is True and it["authoritative"] is False
        assert "非权威" in it.get("derived_note", "")
        assert it["source_connection"] == cid
        assert it["fetched_at"] and it["trace"]
        assert it["capability_id"] == cap_id
        assert it["output_attribute"] == "marginFee"

    # ------------------------------------------------------------------ #
    # 渠道错配（422）＋上传型默认本地渠道＋dry_run 不读源不落派生
    # ------------------------------------------------------------------ #
    def test_06_channel_semantics(self) -> None:
        c, cap_id, mock = _ctx["c"], _ctx["cap_id"], _ctx["mock"]
        up1, o1 = _ctx["up1"], _ctx["fed_ids"]["V204-O1"]
        # 上传型实例强制联邦 → 渠道错配
        r = c.post(f"/api/capabilities/{cap_id}/execute", params=P,
                   json={"instance_id": up1, "federated": True})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "CHANNEL_MISMATCH"
        # 上传型默认走本地渠道（正常执行并写回）
        r = c.post(f"/api/capabilities/{cap_id}/execute", params=P, json={"instance_id": up1})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["status"] == "success"
        assert abs(d["value"] - 9.0) < 1e-9
        assert "federated" not in d
        r = c.get(f"/api/ingest/instances/{up1}", params=P)
        assert "marginFee" in r.json()["data"]["instance"]
        # dry_run：不读源、不落派生（本地预览）
        n_reads = len(mock.read_calls)
        r = c.post(f"/api/capabilities/{cap_id}/execute", params=P,
                   json={"instance_id": o1, "dry_run": True, "cost_confirmed": True})
        assert r.status_code == 200
        assert r.json()["data"]["dry_run"] is True
        assert len(mock.read_calls) == n_reads
        r = c.get(f"/api/capabilities/{cap_id}/derived",
                  params={**P, "instance_id": o1})
        assert r.json()["data"]["total"] == 1  # 无新增

    # ------------------------------------------------------------------ #
    # EP-04-03：部分源失败 → 失败实例标记，不产生半口径结果
    # ------------------------------------------------------------------ #
    def test_07_batch_partial_failure(self) -> None:
        c, cap_id, cid, mock = _ctx["c"], _ctx["cap_id"], _ctx["cid"], _ctx["mock"]
        fed_ids = _ctx["fed_ids"]
        saved = list(mock.read_records)
        try:
            # 源系统删除 O2（回源无此键 → 严格模式失败）
            mock.read_records = [saved[0], saved[2]]
            r = c.post(f"/api/capabilities/{cap_id}/execute-batch", params=P,
                       json={"cost_confirmed": True})
            assert r.status_code == 200, r.text
            d = r.json()["data"]
            assert d["total"] == 4
            fed = d["federated"]
            assert fed["required"] == 3 and fed["success"] == 2 and fed["failed"] == 1
            assert len(fed["systems"]) == 1
            assert fed["systems"][0]["connection_id"] == cid
            assert fed["systems"][0]["ok"] == 2 and fed["systems"][0]["failed"] == 1
            assert d["partial_failure"] is True
            by_id = {it["instance_id"]: it for it in d["items"]}
            it2 = by_id[fed_ids["V204-O2"]]
            assert it2["status"] == "federated_failed"
            assert it2["value"] is None
            assert it2["error_code"] == "CONTENT_UNAVAILABLE"
            assert it2["federated"]["source_connection"] == cid
            assert it2.get("derived") is None or "derived" not in it2
            it1, it3 = by_id[fed_ids["V204-O1"]], by_id[fed_ids["V204-O3"]]
            for it in (it1, it3):
                assert it["status"] == "success"
                assert it["source_system"] == "V204 mock ERP"
                assert it["fetched_at"]
                assert it["federated"]["source_connection"] == cid
                assert it["derived"] and it["derived"]["authoritative"] is False
            iup = by_id[_ctx["up1"]]
            assert iup["status"] == "success" and abs(iup["value"] - 9.0) < 1e-9
            assert "federated" not in iup
            # 失败实例无派生值（不产生半口径）
            r = c.get(f"/api/capabilities/{cap_id}/derived",
                      params={**P, "instance_id": fed_ids["V204-O2"]})
            assert r.json()["data"]["total"] == 0
        finally:
            mock.read_records = saved

    # ------------------------------------------------------------------ #
    # M1-E 显式 inputs 覆盖：平台不代读（读源次数不变、不落派生）
    # ------------------------------------------------------------------ #
    def test_08_explicit_inputs_override(self) -> None:
        c, cap_id, mock = _ctx["c"], _ctx["cap_id"], _ctx["mock"]
        o1 = _ctx["fed_ids"]["V204-O1"]
        n_reads = len(mock.read_calls)
        n_dv_before = c.get(f"/api/capabilities/{cap_id}/derived",
                            params={**P, "instance_id": o1}).json()["data"]["total"]
        r = c.post(f"/api/capabilities/{cap_id}/execute", params=P,
                   json={"instance_id": o1, "inputs": {"amount": 100}})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["status"] == "success"
        assert abs(d["value"] - 10.0) < 1e-9
        assert d["input_source"] == "federated_read_override"
        assert d["inputs_persisted"] is False
        assert d["input_overrides"] == {"amount": 100}
        assert len(mock.read_calls) == n_reads, "显式 inputs 时平台不得代读"
        r = c.get(f"/api/capabilities/{cap_id}/derived",
                  params={**P, "instance_id": o1})
        assert r.json()["data"]["total"] == n_dv_before  # 无新增（覆盖输入不落派生）

    # ------------------------------------------------------------------ #
    # TP-21-08：数据健康快照 extensions 四扩展维度可查
    # ------------------------------------------------------------------ #
    def test_09_tp21_08_extensions(self) -> None:
        c, oid = _ctx["c"], _ctx["oid"]
        # 字典词条：绑定 Order.amount
        r = c.post("/api/dictionary/terms", params=P, json={
            "canonical": "amount_range", "object_type": "Order", "property_id": "amount",
            "synonyms": ["金额"]})
        assert r.status_code == 201, r.text

        r = c.post(f"/api/quality/ontologies/{oid}/evaluate", params=P)
        assert r.status_code == 200, r.text
        ext = r.json()["data"]["extensions"]
        assert set(ext) == {"dictionary_coverage", "cross_system_key_conflict",
                            "instance_alignment", "lineage_completeness"}
        dc = ext["dictionary_coverage"]
        assert dc["total"] == 3 and dc["mapped"] == 1
        assert abs(dc["rate"] - 1 / 3) < 1e-3
        ck = ext["cross_system_key_conflict"]
        assert ck["multi_source_keys"] == 0  # 单连接 → 无多源同键
        assert ck["rate"] is None
        ia = ext["instance_alignment"]
        assert ia["total"] == 1 and ia["rate"] == 0.0  # 无已生效对齐声明
        lc = ext["lineage_completeness"]
        assert lc["total"] == 4 and lc["complete"] == 4
        assert lc["rate"] == 1.0

        # 健康视图可查（同一数据源）
        r = c.get(f"/api/quality/ontologies/{oid}/health", params=P)
        assert r.status_code == 200
