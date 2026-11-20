"""GAP-05 验收：导入校验失败 → 结构化模型缺口 + 一键修复重发布。

- 上传表头未匹配（全部/部分）→ 422 details.model_gaps / 候选 model_gaps（missing_attribute）
- approve SHACL 门禁失败 → result.model_gaps（枚举缺值/约束过紧/必填/正则/长度/区间）
- POST /api/ingest/model-gaps/fix → 兼容变更走 changesets 管线升小版本直接发布；
  不兼容项 skipped；修复后可继续导入（重新审批 / 重新上传）。

运行：cd backend && python -m pytest tests/test_gap05_model_gaps.py -q
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from _authenv import auth_module_guard
from main import app

_guard = auth_module_guard()  # GAP-20：模块边界隔离（本模块无鉴权用例，保持鉴权关）

c = TestClient(app)
PID = "gap05-proj-" + uuid.uuid4().hex[:8]

ONT: str | None = None
ONT_DRAFT: str | None = None

ORDER_OBJECT = {
    "id": "Order", "name": "订单", "stable_key": "orderCode",
    "attributes": [
        {"id": "orderCode", "name": "订单号", "data_type": "string", "required": True},
        {"id": "status", "name": "状态", "data_type": "string",
         "constraints": {"enum": ["created", "paid", "shipped"]}},
        {"id": "note", "name": "备注", "data_type": "string",
         "constraints": {"max_length": 10}},
        {"id": "amount", "name": "金额", "data_type": "float",
         "constraints": {"minimum": 0}},
        {"id": "sku", "name": "SKU编码", "data_type": "string",
         "constraints": {"pattern": "^[A-Z0-9]+$"}},
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
    global ONT, ONT_DRAFT
    if ONT:
        return
    resp = c.post("/api/ontologies", params={"project_id": PID}, json={
        "name": "GAP05验收-订单", "objects": [ORDER_OBJECT]})
    ONT = _ok(resp)["id"]
    _ok(c.post(f"/api/ontologies/{ONT}/publish", params={"project_id": PID}))
    # 草稿态本体（用于 not_active 负向用例）
    resp = c.post("/api/ontologies", params={"project_id": PID}, json={
        "name": "GAP05验收-草稿", "objects": [ORDER_OBJECT]})
    ONT_DRAFT = _ok(resp)["id"]


def _preview(records: list[dict]) -> str:
    fm = {k: k for k in records[0].keys()}
    data = _ok(c.post("/api/ingest/preview", params={"project_id": PID}, json={
        "ontology_id": ONT,
        "source": {"kind": "field_map", "records": records},
        "mapping": {"object_type": "Order", "field_map": fm},
    }))
    return data["id"]


def _approve(cid: str) -> dict:
    return _ok(c.post(f"/api/ingest/candidates/{cid}/approve",
                      params={"project_id": PID}, json={}))


def _fix(gaps: list[dict], ontology_id: str | None = None) -> dict:
    return _ok(c.post("/api/ingest/model-gaps/fix", params={"project_id": PID}, json={
        "ontology_id": ontology_id or ONT, "object_type": "Order", "gaps": gaps}))


def _upload_csv(name: str, content: str, object_type: str = "Order"):
    # ontology_id/object_type 为查询参数，file 为 multipart 表单
    return c.post("/api/ingest/upload",
                  params={"project_id": PID, "ontology_id": ONT, "object_type": object_type},
                  files={"file": (name, content.encode("utf-8"), "text/csv")})


def _gaps_of(result: dict, kind: str, attr: str | None = None) -> list[dict]:
    out = [g for g in (result.get("model_gaps") or []) if g.get("kind") == kind]
    if attr:
        out = [g for g in out if (g.get("attribute_id") == attr or g.get("attribute_name") == attr)]
    return out


class TestGap05ModelGaps:
    def test_01_upload_unknown_header_422_with_gaps(self):
        """CSV 表头全部未匹配 → 422 且 details.model_gaps 给出缺字段缺口＋推断类型。"""
        _setup()
        resp = _upload_csv("gap05-a.csv", "extraCol\n123\n456\n")
        assert resp.status_code == 422
        err = resp.json()["error"]
        gaps = (err.get("details") or {}).get("model_gaps") or []
        assert gaps and gaps[0]["kind"] == "missing_attribute"
        assert gaps[0]["attribute_name"] == "extraCol"
        assert gaps[0]["auto_fixable"] is True
        assert gaps[0]["fix"]["action"] == "add_attribute"
        assert gaps[0]["fix"]["params"]["data_type"] == "integer"  # 样本全为整数

    def test_02_upload_partial_match_candidate_gaps(self):
        """部分匹配 → 候选创建成功，且候选携带缺字段缺口（未匹配列数据不落候选）。"""
        _setup()
        resp = _upload_csv("gap05-b.csv", "orderCode,extraCol\nO-1,x-9\n")
        data = _ok(resp)
        cand = data["candidates"][0]
        gaps = cand.get("model_gaps") or []
        assert gaps and gaps[0]["attribute_name"] == "extraCol"
        assert "orderCode" in cand["instances"][0]
        assert "extraCol" not in cand["instances"][0]  # 未匹配列未导入

    def test_03_approve_enum_value_gap(self):
        """枚举外取值 → approve 拦截且 model_gaps 给出补枚举值缺口。"""
        _setup()
        cid = _preview([{"orderCode": "O-2", "status": "refunded", "customer": "C"}])
        result = _approve(cid)
        assert result["imported"] == 0
        assert result["violations"]
        g = _gaps_of(result, "enum_value_missing", "status")
        assert g and g[0]["fix"]["action"] == "add_enum_values"
        assert g[0]["fix"]["params"]["values"] == ["refunded"]

    def test_04_approve_max_length_gap(self):
        """超长取值 → 上调最大长度缺口（放宽到数据实际长度）。"""
        _setup()
        cid = _preview([{"orderCode": "O-3", "note": "12345678901", "customer": "C"}])
        result = _approve(cid)
        g = _gaps_of(result, "constraint_too_strict", "note")
        assert g and g[0]["fix"]["action"] == "relax_max_length"
        assert g[0]["fix"]["params"]["max_length"] == 11

    def test_05_approve_pattern_gap(self):
        """正则不匹配 → 解除正则约束缺口。"""
        _setup()
        cid = _preview([{"orderCode": "O-4", "sku": "abc-123", "customer": "C"}])
        result = _approve(cid)
        g = _gaps_of(result, "constraint_too_strict", "sku")
        assert g and g[0]["fix"]["action"] == "remove_pattern"

    def test_06_approve_range_gap(self):
        """数值越下界 → 下调最小值缺口（放宽到数据实际最小值）。"""
        _setup()
        cid = _preview([{"orderCode": "O-5", "amount": -5, "customer": "C"}])
        result = _approve(cid)
        g = _gaps_of(result, "constraint_too_strict", "amount")
        assert g and g[0]["fix"]["action"] == "relax_minimum"
        assert g[0]["fix"]["params"]["minimum"] == -5

    def test_07_approve_required_gap(self):
        """必填属性缺失 → 必填改可选缺口。"""
        _setup()
        cid = _preview([{"orderCode": "O-6"}])  # 缺 customer
        result = _approve(cid)
        g = _gaps_of(result, "constraint_too_strict", "customer")
        assert g and g[0]["fix"]["action"] == "unset_required"

    def test_08_fix_publish_then_reapprove(self):
        """一键修复（补枚举）→ 兼容变更升小版本直接发布 → 重新审批成功入库。"""
        _setup()
        cid = _preview([{"orderCode": "O-8", "status": "refunded", "customer": "C"}])
        gaps = _gaps_of(_approve(cid), "enum_value_missing", "status")
        assert gaps
        before = _ok(c.get(f"/api/ontologies/{ONT}", params={"project_id": PID}))
        data = _fix(gaps)
        assert data.get("applied")
        assert not any(s.get("reason") == "枚举值已存在" for s in data.get("skipped") or [])
        after = _ok(c.get(f"/api/ontologies/{ONT}", params={"project_id": PID}))
        before_v = before.get("version") or (before.get("versions") or [{}])[-1].get("version")
        after_v = after.get("version") or (after.get("versions") or [{}])[-1].get("version")
        assert after_v != before_v  # 版本已 bump
        status_obj = next(a for a in after["objects"][0]["attributes"] if a["id"] == "status")
        assert "refunded" in (status_obj.get("constraints") or {}).get("enum")
        result = _approve(cid)  # 重新审批继续导入
        assert result["imported"] >= 1
        assert not result["violations"]

    def test_09_fix_noop_422(self):
        """全部为需人工处理项 → 422 MODEL_GAP_NOOP。"""
        _setup()
        resp = c.post("/api/ingest/model-gaps/fix", params={"project_id": PID}, json={
            "ontology_id": ONT, "object_type": "Order",
            "gaps": [{"kind": "type_mismatch", "attribute_id": "amount",
                      "attribute_name": "金额", "auto_fixable": False, "fix": None}]})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MODEL_GAP_NOOP"

    def test_10_fix_not_active_422(self):
        """草稿态本体 → 422 MODEL_GAP_MANUAL（不走兼容变更通道）。"""
        _setup()
        resp = c.post("/api/ingest/model-gaps/fix", params={"project_id": PID}, json={
            "ontology_id": ONT_DRAFT, "object_type": "Order",
            "gaps": [{"kind": "missing_attribute", "attribute_name": "x", "auto_fixable": True,
                      "fix": {"action": "add_attribute", "params": {"name": "x", "data_type": "string"}}}]})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "MODEL_GAP_MANUAL"

    def test_11_missing_field_fix_and_reupload(self):
        """缺字段一键修复 → 重新上传同一文件 → 列数据完整导入且无缺口。"""
        _setup()
        data = _ok(_upload_csv("gap05-c.csv", "orderCode,extraCol\nO-9,x-1\n"))
        gaps = data["candidates"][0].get("model_gaps") or []
        assert gaps and gaps[0]["attribute_name"] == "extraCol"
        fix = _fix(gaps)
        assert fix.get("applied")
        data2 = _ok(_upload_csv("gap05-c.csv", "orderCode,extraCol\nO-9,x-1\n"))
        cand = data2["candidates"][0]
        assert not cand.get("model_gaps")
        assert cand["instances"][0].get("extraCol") == "x-1"

    def test_12_analyze_unknown_and_manual(self):
        """单测：未识别 path → unknown（人工）；无约束可放宽的类型不符 → type_mismatch（人工）。"""
        from model_gap import analyze_model_gaps
        onto = {"objects": [{"id": "Order", "name": "订单", "attributes": [
            {"id": "amount", "name": "金额", "data_type": "float"}]}]}
        gaps = analyze_model_gaps(onto, "Order",
                                   [{"severity": "Violation", "path": "ghostAttr",
                                     "message": "对象级违规"}],
                                   [{"amount": "abc"}])
        kinds = {g["kind"] for g in gaps}
        assert "unknown" in kinds
        assert all(g["auto_fixable"] is False for g in gaps if g["kind"] == "unknown")
        gaps2 = analyze_model_gaps(onto, "Order",
                                   [{"severity": "Violation", "path": "amount",
                                     "message": "类型不符"}],
                                   [{"amount": "abc"}])
        assert gaps2 and gaps2[0]["kind"] == "type_mismatch"
        assert gaps2[0]["auto_fixable"] is False

    def test_13_apply_gaps_dedupe_and_skip(self):
        """单测：重复枚举值跳过；同名属性不重复新增。"""
        from model_gap import apply_gaps_to_model
        model = {"name": "t", "objects": [{"id": "Order", "name": "订单", "attributes": [
            {"id": "status", "name": "状态", "data_type": "string",
             "constraints": {"enum": ["a"]}},
            {"id": "remark", "name": "备注2", "data_type": "string"},
        ]}]}
        gaps = [
            {"kind": "enum_value_missing", "attribute_id": "status", "attribute_name": "状态",
             "auto_fixable": True, "fix": {"action": "add_enum_values", "params": {"values": ["a", "b"]}}},
            {"kind": "missing_attribute", "attribute_id": None, "attribute_name": "备注2",
             "auto_fixable": True, "fix": {"action": "add_attribute",
                                           "params": {"name": "备注2", "data_type": "string"}}},
        ]
        new_model, applied, skipped = apply_gaps_to_model(model, "Order", gaps)
        assert applied  # 补枚举 b
        assert len(skipped) == 1  # 同名属性已存在
        # 原模型不被修改（深拷贝）
        assert model["objects"][0]["attributes"][0]["constraints"]["enum"] == ["a"]
