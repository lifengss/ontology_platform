"""GAP-01 验收：PUT 实例默认差量更新，replace=true 显式整体替换。

- PUT /api/ingest/instances/{id} 默认（replace=false）：仅覆盖请求体中的非空字段，
  空值字段（None/空串/空数组）保留原值，并在 warnings/preserved_keys 中留痕；
- replace=true：整体替换（清空请求体未携带字段），并强制严格校验；
- PUT /api/ingest/instances/batch 与单条 PUT 语义一致；
- 变更日志保留 PUT/BATCH 操作标记（replace 模式追加 /replace 后缀）。

运行：cd backend && python -m pytest tests/test_gap01_instance_put.py -q
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from _authenv import auth_module_guard
from core.instance_store import get_instance_store
from main import app

_guard = auth_module_guard()  # GAP-20：模块边界隔离（本模块无鉴权用例，保持鉴权关）

c = TestClient(app)
PID = "gap01-proj-" + uuid.uuid4().hex[:8]

ONT: str | None = None


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ok(resp) -> dict:
    assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("success") is True, body
    return body["data"]


def _setup() -> None:
    global ONT
    if ONT:
        return
    resp = c.post("/api/ontologies", params={"project_id": PID}, json={
        "name": "GAP01验收-订单",
        "objects": [
            {"id": "Order", "name": "订单", "stable_key": "orderCode",
             "attributes": [
                 {"id": "orderCode", "name": "订单号", "data_type": "string", "required": True},
                 {"id": "amount", "name": "金额", "data_type": "float"},
                 {"id": "note", "name": "备注", "data_type": "string"},
             ],
             "relations": []},
        ],
    })
    ONT = _ok(resp)["id"]
    _ok(c.post(f"/api/ontologies/{ONT}/publish", params={"project_id": PID}))


def _insert(rec_id: str, inst: dict) -> None:
    rec = {
        "id": rec_id, "project_id": PID, "ontology_id": ONT, "object_type": "Order",
        "stable_key": "orderCode", "stable_key_value": inst.get("orderCode"),
        "channel": "upload", "instance": inst, "imported_at": _now(),
    }
    get_instance_store(PID).insert(rec)


def _put(iid: str, inst: dict, **params) -> dict:
    q = {"project_id": PID, **params}
    return _ok(c.put(f"/api/ingest/instances/{iid}", params=q, json={"instance": inst}))


class TestGap01InstancePut:
    def test_01_put_diff_preserves_unset_fields(self):
        """默认 PUT：只更新请求体携带的非空字段，未携带字段保留原值。"""
        _setup()
        iid = "gap01-i1"
        _insert(iid, {"orderCode": "G01-001", "amount": 10.0, "note": "原备注"})
        data = _put(iid, {"amount": 99.0})
        assert data["instance"]["amount"] == 99.0
        assert data["instance"]["note"] == "原备注"          # 未携带 → 保留
        assert data["instance"]["orderCode"] == "G01-001"    # 未携带 → 保留
        assert data["replace_mode"] is False

    def test_02_put_diff_explicit_empty_kept_and_traced(self):
        """默认 PUT：显式空值字段保留原值，并在 preserved_keys/warnings 留痕。"""
        iid = "gap01-i1"
        data = _put(iid, {"note": ""})
        assert data["instance"]["note"] == "原备注"
        assert "note" in data["preserved_keys"]
        assert any("差量模式" in w for w in data["warnings"])

    def test_03_put_replace_clears_unset_fields(self):
        """replace=true：整体替换，请求体未携带的字段被清空。"""
        iid = "gap01-i2"
        _insert(iid, {"orderCode": "G01-002", "amount": 5.0, "note": "要被清空"})
        data = _put(iid, {"orderCode": "G01-002"}, replace=True)
        assert data["replace_mode"] is True
        assert data["instance"]["orderCode"] == "G01-002"
        assert "amount" not in data["instance"]
        assert "note" not in data["instance"]

    def test_04_put_404(self):
        """PUT 不存在的实例 → 404 NOT_FOUND。"""
        resp = c.put("/api/ingest/instances/gap01-missing",
                     params={"project_id": PID}, json={"instance": {"a": 1}})
        assert resp.status_code == 404
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "NOT_FOUND"

    def test_05_batch_diff_and_replace_semantics(self):
        """batch 与单条 PUT 一致：默认差量保留、replace=true 整体替换。"""
        iid = "gap01-i3"
        _insert(iid, {"orderCode": "G01-003", "amount": 1.0, "note": "batch原值"})
        data = _ok(c.put("/api/ingest/instances/batch", params={"project_id": PID},
                         json={"items": [{"id": iid, "instance": {"amount": 2.0}}]}))
        assert data["updated"] == 1 and data["replace_mode"] is False
        inst = get_instance_store(PID).get(iid)["instance"]
        assert inst["amount"] == 2.0 and inst["note"] == "batch原值"

        data = _ok(c.put("/api/ingest/instances/batch",
                         params={"project_id": PID, "replace": "true"},
                         json={"items": [{"id": iid, "instance": {"orderCode": "G01-003"}}]}))
        assert data["replace_mode"] is True
        inst = get_instance_store(PID).get(iid)["instance"]
        assert "amount" not in inst and "note" not in inst

    def test_06_change_log_mode_traced(self):
        """变更日志：diff 模式 op=PUT，replace 模式 op=PUT/replace（操作标记不丢）。"""
        from core.storage import JsonStore
        iid = "gap01-i4"
        _insert(iid, {"orderCode": "G01-004", "note": "日志验收"})
        _put(iid, {"amount": 7.0})
        _put(iid, {"orderCode": "G01-004"}, replace=True)
        logs = JsonStore("instance_change_logs", project_id=PID).all()
        ops = [l.get("operation") for l in logs if l.get("instance_id") == iid]
        assert "PUT" in ops
        assert "PUT/replace" in ops
