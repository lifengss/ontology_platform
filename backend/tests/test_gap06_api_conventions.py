"""GAP-06 验收：接口约定不直观（object_type 双通道 / field_map 明确提示 / 分页别名 / CSV 空值）。

- preview 的 object_type：body 与 ?object_type= 查询参数双通道（body 优先）
- upload/upload-smart 的 object_type：查询参数与 multipart 表单字段双通道
- preview 的 mapping.field_map 为空 → 422 且给出 identity map 说明＋curl 示例
- CSV/preview 空单元格（""）视为缺失：可选属性缺键、必填走清晰「必填缺失」路径，
  不再以 Literal("") 触发枚举/约束报错
- 分页：误用 page_size（pageSize 别名）/offset（page 别名）不再被静默忽略

运行：cd backend && python -m pytest tests/test_gap06_api_conventions.py -q
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from _authenv import auth_module_guard
from main import app

_guard = auth_module_guard()  # GAP-20：模块边界隔离（本模块无鉴权用例，保持鉴权关）

c = TestClient(app)
PID = "gap06-proj-" + uuid.uuid4().hex[:8]

ONT: str | None = None

ORDER_OBJECT = {
    "id": "Order", "name": "订单", "stable_key": "orderCode",
    "attributes": [
        {"id": "orderCode", "name": "订单号", "data_type": "string", "required": True},
        {"id": "status", "name": "状态", "data_type": "string",
         "constraints": {"enum": ["created", "paid", "shipped"]}},
        {"id": "customer", "name": "客户", "data_type": "string", "required": True},
    ],
    "relations": [],
}


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
        "name": "GAP06验收-订单", "objects": [ORDER_OBJECT]})
    ONT = _ok(resp)["id"]
    _ok(c.post(f"/api/ontologies/{ONT}/publish", params={"project_id": PID}))


def _preview(records: list[dict], mapping: dict | None = None) -> str:
    fm = mapping or {"object_type": "Order", "field_map": {k: k for k in records[0].keys()}}
    data = _ok(c.post("/api/ingest/preview", params={"project_id": PID}, json={
        "ontology_id": ONT,
        "source": {"kind": "field_map", "records": records},
        "mapping": fm,
    }))
    return data["id"]


def _upload_csv(name: str, content: str, object_type: str | None = "Order"):
    params = {"project_id": PID, "ontology_id": ONT}
    if object_type:
        params["object_type"] = object_type
    return c.post("/api/ingest/upload", params=params,
                  files={"file": (name, content.encode("utf-8"), "text/csv")})


class TestGap06ApiConventions:
    def test_01_preview_object_type_query_channel(self):
        """preview：body 不给 object_type，查询参数 ?object_type=Order 兜底生效。"""
        _setup()
        data = _ok(c.post("/api/ingest/preview",
                          params={"project_id": PID, "object_type": "Order"}, json={
                              "ontology_id": ONT,
                              "source": {"kind": "field_map",
                                         "records": [{"orderCode": "O-Q1", "customer": "C"}]},
                              "mapping": {"field_map": {"orderCode": "orderCode", "customer": "customer"}},
                          }))
        assert data["object_type"] == "Order"
        assert data["instances"][0]["orderCode"] == "O-Q1"

    def test_02_preview_empty_field_map_clear_422(self):
        """field_map 为空 → 422，消息含 identity map 说明与 curl 示例（不再只有一句笼统报错）。"""
        _setup()
        resp = c.post("/api/ingest/preview", params={"project_id": PID}, json={
            "ontology_id": ONT, "object_type": "Order",
            "source": {"kind": "field_map", "records": [{"orderCode": "O-Q2"}]},
            "mapping": {},
        })
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert "不能为空" in err["message"]
        assert "identity map" in err["message"]
        assert err.get("suggestion") and "curl" in err["suggestion"]

    def test_03_upload_object_type_form_channel(self):
        """upload：object_type 只放 multipart 表单字段（不放查询参数）→ 正常生成候选。"""
        _setup()
        resp = c.post("/api/ingest/upload",
                      params={"project_id": PID, "ontology_id": ONT},
                      data={"object_type": "Order"},
                      files={"file": ("gap06-form.csv", "orderCode\nO-F1\n".encode("utf-8"), "text/csv")})
        data = _ok(resp)
        assert data["candidates"][0]["object_type"] == "Order"
        assert data["candidates"][0]["instances"][0]["orderCode"] == "O-F1"

    def test_04_upload_csv_empty_cells_dropped(self):
        """CSV 空单元格 → 实例缺键（可选属性不写 ""），杜绝 Literal("") 类 SHACL 报错。"""
        _setup()
        data = _ok(_upload_csv("gap06-empty.csv", "orderCode,status,customer\nO-E1,,\n"))
        inst = data["candidates"][0]["instances"][0]
        assert inst["orderCode"] == "O-E1"
        assert "status" not in inst          # 空枚举列 → 缺键
        assert "customer" not in inst        # 空必填列 → 缺键（走必填缺失路径）
        assert all(v != "" for v in inst.values())

    def test_05_upload_empty_required_clear_violation(self):
        """空必填列审批 → 清晰「必填缺失」违规＋可一键放宽缺口，而非空串枚举报错。"""
        _setup()
        data = _ok(_upload_csv("gap06-empty2.csv", "orderCode,customer\nO-E2,\n"))
        cid = data["candidates"][0]["id"]
        result = _ok(c.post(f"/api/ingest/candidates/{cid}/approve",
                            params={"project_id": PID}, json={}))
        assert result["imported"] == 0
        assert result["violations"]
        gaps = [g for g in (result.get("model_gaps") or []) if g.get("attribute_id") == "customer"]
        assert gaps and gaps[0]["fix"]["action"] == "unset_required"

    def test_06_candidates_page_size_alias(self):
        """误用 page_size（非 pageSize）→ 生效，不再固定返回默认 20 条。"""
        _setup()
        _preview([{"orderCode": "O-P1", "customer": "C"}])
        _preview([{"orderCode": "O-P2", "customer": "C"}])
        data = _ok(c.get("/api/ingest/candidates",
                         params={"project_id": PID, "page_size": 1}))
        assert data["page_size"] == 1
        assert len(data["items"]) == 1
        assert data["total"] >= 2

    def test_07_candidates_offset_alias(self):
        """误用 offset（非 page）→ 换算页码生效（pageSize=1 时 offset=1 → 第 2 页）。"""
        _setup()
        page1 = _ok(c.get("/api/ingest/candidates",
                          params={"project_id": PID, "pageSize": 1, "offset": 0}))
        page2 = _ok(c.get("/api/ingest/candidates",
                          params={"project_id": PID, "pageSize": 1, "offset": 1}))
        assert page2["page"] == 2
        assert page1["items"][0]["id"] != page2["items"][0]["id"]

    def test_08_page_size_wins_over_alias(self):
        """正式名与别名同时给出 → 以正式名 pageSize 为准。"""
        _setup()
        data = _ok(c.get("/api/ingest/candidates",
                         params={"project_id": PID, "pageSize": 1, "page_size": 5}))
        assert data["page_size"] == 1

    def test_09_instances_page_size_alias(self):
        """实例列表（默认 50 条端点）误用 page_size → 生效（验收：不再固定 50 条）。"""
        _setup()
        data = _ok(c.get("/api/ingest/instances",
                         params={"project_id": PID, "page_size": 7}))
        assert data["page_size"] == 7
