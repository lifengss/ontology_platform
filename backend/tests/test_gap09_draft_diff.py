"""GAP-09 验收：生成草稿 vs 当前模型 差异审核（逐项采纳/拒绝、一键采纳全部）。

- POST /api/ontologies/{id}/draft-diff：生成结果（来源草稿）vs 当前模型（目标）结构化差异，每项带 index
- POST /api/ontologies/{id}/adopt-changes：勾选变更合并进目标（仅 draft 可直编），幂等，合并结果重跑业务层校验
- active 目标 → 409（走变更提案管线）；来源必须是 draft；来源 ≠ 目标

运行：cd backend && python -m pytest tests/test_gap09_draft_diff.py -q
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from _authenv import auth_module_guard
from main import app

_guard = auth_module_guard()  # GAP-20：模块边界隔离（本模块无鉴权用例，保持鉴权关）

c = TestClient(app)

PROJ: str | None = None
ONT_T: str | None = None  # 目标本体（draft）
ONT_S: str | None = None  # 来源（模拟生成草稿，draft）
CHANGES: list[dict] = []


def _ok(resp) -> dict:
    assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.text[:400]}"
    body = resp.json()
    assert body.get("success") is True, body
    return body["data"]


def _err(resp, code: str):
    assert resp.status_code in (400, 404, 409, 422), f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("success") is False, body
    assert body.get("error", {}).get("code") == code, body


def _attr(aid: str, dt: str = "string", req: bool = False, **cons) -> dict:
    a = {"id": aid, "name": aid, "data_type": dt, "required": req}
    if cons:
        a["constraints"] = cons
    return a


def _obj(oid: str, name: str, key: str, attrs: list[dict], rels: list[dict] | None = None) -> dict:
    return {"id": oid, "name": name,
            "business_definition": f"{name}，GAP-09 验收业务对象，边界为单据级记录",
            "stable_key": key, "attributes": attrs, "relations": rels or []}


def _rel(rid: str, target: str) -> dict:
    return {"id": rid, "name": rid, "target_object": target, "cardinality": "many_to_one"}


def _target_model() -> dict:
    return {
        "name": "GAP09-目标本体",
        "objects": [
            _obj("Order", "订单", "order_no", [
                _attr("order_no", req=True),
                _attr("status", req=True, enum=["new", "paid"]),
                _attr("qty", "integer", minimum=1),
                _attr("memo"),
            ], [_rel("has_items", "Product")]),
            _obj("Product", "商品", "sku", [
                _attr("sku", req=True),
                _attr("price", "float"),
            ]),
        ],
        "constraints": [{"id": "c1", "name": "订单唯一", "rule": "unique", "params": {"fields": ["order_no"]}}],
        "functions": [],
    }


def _source_model() -> dict:
    m = _target_model()
    m["name"] = "GAP09-生成草稿"
    order = m["objects"][0]
    # 兼容：枚举增补 shipped；可选新增 remark
    order["attributes"] = [
        _attr("order_no", req=True),
        _attr("status", req=True, enum=["new", "paid", "shipped"]),
        _attr("qty", "integer", minimum=2),  # 不兼容：取值收窄
        _attr("remark"),                     # 兼容：新增可选属性（memo 被删 → 不兼容）
    ]
    order["relations"] = [_rel("has_items", "Product"), _rel("has_notes", "Product")]  # 兼容：新增关系
    m["objects"].append(_obj("Coupon", "优惠券", "code", [_attr("code", req=True)]))  # 兼容：新增对象
    m["constraints"].append({"id": "c2", "name": "数量上限", "rule": "max", "params": {"field": "qty", "value": 99}})
    return m


def _create(name: str, model: dict) -> str:
    body = {"name": name, **{k: v for k, v in model.items() if k != "name"}}
    return _ok(c.post("/api/ontologies", params={"project_id": PROJ}, json=body))["id"]


def _by(changes: list[dict], **kw) -> list[dict]:
    return [ch for ch in changes if all(ch.get(k) == v for k, v in kw.items())]


class TestGap09DraftDiff:
    def test_01_diff_lists_changes_with_indexes(self):
        """draft-diff 返回双方摘要＋带 index 的变更列表，兼容标记正确。"""
        global PROJ, ONT_T, ONT_S, CHANGES
        PROJ = _ok(c.post("/api/projects", json={"name": "GAP09-" + uuid.uuid4().hex[:6]}))["id"]
        ONT_T = _create("GAP09-目标本体", _target_model())
        ONT_S = _create("GAP09-生成草稿", _source_model())

        data = _ok(c.post(f"/api/ontologies/{ONT_T}/draft-diff",
                          json={"source_draft_id": ONT_S}))
        assert data["target"]["id"] == ONT_T and data["target"]["status"] == "draft"
        assert data["source"]["id"] == ONT_S and data["source"]["status"] == "draft"
        diff = data["diff"]
        assert diff["compatible"] is False
        assert diff["version_bump"] == "major"
        CHANGES = diff["changes"]
        assert [ch["index"] for ch in CHANGES] == list(range(len(CHANGES)))

        assert _by(CHANGES, kind="object_added", object_id="Coupon", compatible=True)
        assert _by(CHANGES, kind="attribute_added", attribute_id="remark", compatible=True)
        assert _by(CHANGES, kind="attribute_removed", attribute_id="memo", compatible=False)
        assert _by(CHANGES, kind="constraint_widened", attribute_id="status",
                   constraint_key="enum", compatible=True)
        assert _by(CHANGES, kind="constraint_narrowed", attribute_id="qty",
                   constraint_key="minimum", compatible=False)
        assert _by(CHANGES, kind="relation_added", relation_id="has_notes", compatible=True)
        assert _by(CHANGES, kind="constraint_added", constraint_id="c2", compatible=False)
        assert len(CHANGES) == 7

    def test_02_adopt_compatible_subset_per_item(self):
        """逐项采纳：只勾兼容项 → 枚举/新属性/新关系/新对象合入，收窄与删除项保持原样。"""
        compat_idx = [ch["index"] for ch in CHANGES if ch["compatible"]]
        data = _ok(c.post(f"/api/ontologies/{ONT_T}/adopt-changes",
                          json={"source_draft_id": ONT_S, "adopt": compat_idx}))
        assert data["adopted"] == 4 and data["skipped"] == 0

        got = _ok(c.get(f"/api/ontologies/{ONT_T}", params={"project_id": PROJ}))
        obj = {o["id"]: o for o in got["objects"]}
        order = obj["Order"]
        attrs = {a["id"]: a for a in order["attributes"]}
        assert "Coupon" in obj, "兼容：新对象应合入"
        assert attrs["status"]["constraints"]["enum"] == ["new", "paid", "shipped"], "兼容：枚举增补应合入"
        assert "remark" in attrs, "兼容：新增可选属性应合入"
        assert "memo" in attrs, "未勾采纳：删除属性不应生效"
        assert attrs["qty"]["constraints"]["minimum"] == 1, "未勾采纳：取值收窄不应生效"
        assert "has_notes" in {r["id"] for r in order["relations"]}, "兼容：新增关系应合入"
        assert {x["id"] for x in got["constraints"]} == {"c1"}, "未勾采纳：新增业务约束不应生效"

    def test_03_adopt_remaining_syncs_constraint_fields(self):
        """重新对比后采纳剩余不兼容项（显式确认）→ 目标模型与源完全一致（含字段级约束同步）。

        贴合真实流程：上一次采纳已改动目标，须先 draft-diff 重新取当前 index，再采纳。
        """
        fresh = _ok(c.post(f"/api/ontologies/{ONT_T}/draft-diff",
                           json={"source_draft_id": ONT_S}))["diff"]["changes"]
        # 剩余应恰好是 test_02 未采纳的 3 项不兼容变更
        assert len(fresh) == 3
        assert all(not ch["compatible"] for ch in fresh)
        remain_idx = [ch["index"] for ch in fresh]
        data = _ok(c.post(f"/api/ontologies/{ONT_T}/adopt-changes",
                          json={"source_draft_id": ONT_S, "adopt": remain_idx}))
        assert data["adopted"] == 3 and data["skipped"] == 0

        got = _ok(c.get(f"/api/ontologies/{ONT_T}", params={"project_id": PROJ}))
        obj = {o["id"]: o for o in got["objects"]}
        order = obj["Order"]
        attrs = {a["id"]: a for a in order["attributes"]}
        assert attrs["qty"]["constraints"]["minimum"] == 2, "字段级：minimum 应按变更项单独同步"
        assert attrs["status"]["constraints"]["enum"] == ["new", "paid", "shipped"], "枚举不受 minimum 变更影响"
        assert "memo" not in attrs, "删除属性应生效"
        assert {x["id"] for x in got["constraints"]} == {"c1", "c2"}
        # 整体一致（对象/属性/关系/约束/函数）
        assert len(got["objects"]) == 3
        assert {r["id"] for r in order["relations"]} == {"has_items", "has_notes"}

    def test_04_adopt_is_idempotent(self):
        """已全量同步后重复采纳全部 → 重新 diff 为空：adopted=0、模型不变。"""
        data = _ok(c.post(f"/api/ontologies/{ONT_T}/adopt-changes",
                          json={"source_draft_id": ONT_S, "adopt": "all"}))
        assert data["adopted"] == 0
        assert data["skipped"] == 0
        got = _ok(c.get(f"/api/ontologies/{ONT_T}", params={"project_id": PROJ}))
        assert len(got["objects"]) == 3

    def test_05_error_cases(self):
        """未知来源 404；来源=目标 422；无效序号 422。"""
        _err(c.post(f"/api/ontologies/{ONT_T}/draft-diff", json={"source_draft_id": "no-such-draft"}), "NOT_FOUND")
        _err(c.post(f"/api/ontologies/{ONT_T}/adopt-changes",
                    json={"source_draft_id": ONT_T, "adopt": "all"}), "VALIDATION_ERROR")
        _err(c.post(f"/api/ontologies/{ONT_T}/adopt-changes",
                    json={"source_draft_id": ONT_S, "adopt": [999]}), "VALIDATION_ERROR")

    def test_06_source_must_be_draft(self):
        """来源已发布 → 422（差异审核只面向未发布的生成草稿）。"""
        assert c.post(f"/api/ontologies/{ONT_S}/publish", params={"project_id": PROJ}).status_code in (200, 201)
        _err(c.post(f"/api/ontologies/{ONT_T}/draft-diff", json={"source_draft_id": ONT_S}), "VALIDATION_ERROR")
        _err(c.post(f"/api/ontologies/{ONT_T}/adopt-changes",
                    json={"source_draft_id": ONT_S, "adopt": "all"}), "VALIDATION_ERROR")

    def test_07_published_target_rejected(self):
        """目标已发布 → 409（PUBLISHED_IMMUTABLE，须走变更提案管线）；draft-diff 只读不受影响。"""
        assert c.post(f"/api/ontologies/{ONT_T}/publish", params={"project_id": PROJ}).status_code in (200, 201)
        _err(c.post(f"/api/ontologies/{ONT_T}/adopt-changes",
                    json={"source_draft_id": ONT_S, "adopt": "all"}), "PUBLISHED_IMMUTABLE")
        # 只读差异仍可查（源已发布 → 422 属来源侧约束，故此处不再断言 draft-diff）
        assert c.get(f"/api/ontologies/{ONT_T}", params={"project_id": PROJ}).status_code == 200
