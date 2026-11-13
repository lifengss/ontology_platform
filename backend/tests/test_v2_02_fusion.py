"""V2-02 跨本体等价与融合 — M4-A③ 融合申请/会签/物化 + 虚拟融合查询 + 版本管理测试

验收面（docs/V2-测试大纲.md TP-02）：
- TP-02-01(P0) 同 stable_key 合并一条（双来源标记）、异键多条
- TP-02-02(P0) 冲突双值并存 / 权威源取值，绝不静默覆盖
- TP-02-03(P0 负向) 融合不改源本体快照、融合查询纯只读、源连接内容永远源端取
- TP-02-05(P1) 融合本体版本历史 / 语义 diff / 回滚 / 消费方固定版本引用
- TP-02-11(P1) 虚拟融合查询：快照 join＋分渠道取数＋合并去重，实例/快照不动
- TP-02-13 会签流（同 owner 即签即生效、一票否决、同源防重）

执行：cd backend && python -m pytest tests/test_v2_02_fusion.py -v
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient

from core.instance_store import JsonlInstanceStore
from core.storage import JsonStore
from main import app
from tests.mock_mcp_http import start_mock

PID = "v202-m4a3"
P = {"project_id": PID}
_ctx: dict = {}


def _now() -> str:
    return "2026-11-13T00:00:00+08:00"


def _attr(aid: str, aname: str, dtype: str = "string", required: bool = True) -> dict:
    return {"id": aid, "name": aname, "data_type": dtype, "required": required}


def _onto(name: str, objects: list[dict]) -> dict:
    return {"name": name, "objects": objects, "constraints": []}


def _mk_upload(rid: str, ont_id: str, object_id: str, key_val: str, inst: dict) -> dict:
    return {
        "id": rid,
        "ontology_id": ont_id,
        "object_type": object_id,
        "stable_key_value": key_val,
        "instance": inst,
        "imported_at": _now(),
    }


def _snap(c: TestClient, oid: str) -> str:
    return json.dumps(c.get(f"/api/ontologies/{oid}", params=P).json()["data"],
                      sort_keys=True, ensure_ascii=False)


def _fus_object(ont: dict) -> dict:
    for o in ont.get("objects") or []:
        if str(o.get("id", "")).startswith("fus_"):
            return o
    raise AssertionError("融合本体中未找到 fus_* 对象")


class TestV202Fusion:
    @classmethod
    def setup_class(cls) -> None:
        cls.c = TestClient(app)
        cls.store = JsonlInstanceStore(PID)

    # ------------------------------------------------------------------ #
    # 准备：对齐声明（含冲突裁决）→ 融合申请（自动推导）→ 会签
    # ------------------------------------------------------------------ #
    def test_00_declaration_and_fusion_request(self) -> None:
        c = self.c
        r = c.post("/api/projects", json={"name": "V2-02 M4-A3 跨本体融合"})
        assert r.status_code == 201, r.text
        _ctx["project"] = r.json()["data"]["id"]

        # dev 模式用户库为空 → 本体 owner "dev" 会被判「账号缺位」；进程内预建账号使
        # 会签可走「同 owner 即签即生效」（dev 同时是两侧 owner）
        import auth as auth_svc
        if not auth_svc.find_user("dev"):
            auth_svc.create_user("dev", "dev-pw-123", "admin")

        r = c.post("/api/ontologies", params=P, json=_onto("供应商主数据A", [
            {"id": "obj_supp", "name": "供应商", "business_definition": "供应商实体（A侧）",
             "stable_key": "code", "attributes": [
                 _attr("code", "供应商编码"),
                 _attr("name", "供应商名称"),
                 _attr("phone", "联系电话")], "relations": []},
            {"id": "obj_note", "name": "备注", "business_definition": "供应商备注（未映射，验证透传）",
             "stable_key": "note_code", "attributes": [
                 _attr("note_code", "备注编码"),
                 _attr("text", "内容")], "relations": []},
        ]))
        assert r.status_code == 201, r.text
        _ctx["ontA"] = r.json()["data"]["id"]

        r = c.post("/api/ontologies", params=P, json=_onto("供应商主数据B", [
            {"id": "obj_supp_b", "name": "供应商", "business_definition": "供应商实体（B侧）",
             "stable_key": "code", "attributes": [
                 _attr("code", "供应商编码"),
                 _attr("name", "供应商名称"),
                 _attr("phone", "联系电话", dtype="integer", required=False)], "relations": []},
        ]))
        assert r.status_code == 201, r.text
        _ctx["ontB"] = r.json()["data"]["id"]

        # 发布（融合要求源本体均 active）
        for oid in (_ctx["ontA"], _ctx["ontB"]):
            r = c.post(f"/api/ontologies/{oid}/publish", params=P)
            assert r.status_code == 200, r.text
            assert r.json()["data"]["status"] == "active"

        # 对齐声明：code/name/phone 三对；phone 值域(integer vs string)＋必填冲突
        r = c.post("/api/alignment/declarations", params=P, json={
            "ontology_a": _ctx["ontA"], "ontology_b": _ctx["ontB"],
            "object_pairs": [{
                "object_a": "obj_supp", "object_b": "obj_supp_b",
                "attribute_pairs": [
                    {"attr_a": "code", "attr_b": "code"},
                    {"attr_a": "name", "attr_b": "name"},
                    {"attr_a": "phone", "attr_b": "phone"},
                ],
            }],
        })
        assert r.status_code == 201, r.text
        decl = r.json()["data"]
        _ctx["decl_id"] = decl["id"]
        assert decl["status"] == "pending"
        conflicts = decl.get("conflicts") or []
        assert {x.get("type") for x in conflicts} == {"required", "value_domain"}, conflicts
        # 冲突裁决：均取 a 侧（A 为权威源）
        for idx in range(len(conflicts)):
            r = c.post(f"/api/alignment/declarations/{_ctx['decl_id']}/conflicts/{idx}/resolve",
                       params=P, json={"resolution": "a"})
            assert r.status_code == 200, r.text
        # 会签：dev 同时是两侧 owner → 即签即生效
        r = c.post(f"/api/alignment/declarations/{_ctx['decl_id']}/sign", params=P,
                   json={"decision": "approve", "comment": "会签通过"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "approved"

        # 融合申请：等价映射由已生效声明自动推导
        r = c.post("/api/fusion/requests", params=P,
                   json={"name": "供应商主数据融合", "source_ontologies": [_ctx["ontA"], _ctx["ontB"]]})
        assert r.status_code == 200, r.text
        req = r.json()["data"]
        _ctx["fusion_id"] = req["id"]
        assert req["status"] == "pending"
        groups = req.get("object_groups") or []
        assert len(groups) == 1
        g = groups[0]
        assert {o["object_id"] for o in g["objects"]} == {"obj_supp", "obj_supp_b"}
        assert {m["canonical_id"] for m in g["attribute_mappings"]} == {
            "canon_code", "canon_name", "canon_phone"}
        # phone 冲突已裁决取 a 侧 → 权威源=A
        assert g["authoritative"].get("canon_phone") == _ctx["ontA"]
        # 会签方=全部源本体 owner（含发起人）
        assert {s["ontology_id"] for s in req["signers_required"]} == {_ctx["ontA"], _ctx["ontB"]}
        # 同源防重
        dup = c.post("/api/fusion/requests", params=P,
                     json={"source_ontologies": [_ctx["ontA"], _ctx["ontB"]]})
        assert dup.status_code == 409
        # 列表（信封结构）
        lst = c.get("/api/fusion/requests", params=P)
        assert lst.status_code == 200
        assert lst.json()["data"]["total"] == 1
        # 会签 → 生效（同 owner 一次即签即生效）
        r = c.post(f"/api/fusion/requests/{req['id']}/sign", params=P,
                   json={"decision": "approve", "comment": "各方同意融合"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "approved"
        _ctx["fusion_req"] = r.json()["data"]

    # ------------------------------------------------------------------ #
    # 执行融合：物化派生本体（版本 1.0；无变化重执行不变版本）
    # ------------------------------------------------------------------ #
    def test_01_execute_fusion_materializes(self) -> None:
        c = self.c
        # TP-02-12：物化默认关闭——默认执行仅虚拟执行（不产生物理本体）
        r = c.post(f"/api/fusion/requests/{_ctx['fusion_id']}/execute", params=P, json={})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["materialized"] is False and d["virtual"] is True
        assert d["would_create"] is True and d["would_change"] is True
        assert d["model"]["objects"], "虚拟执行须返回融合模型供预览"
        _nont_ont_before = c.get("/api/ontologies", params=P).json()["data"]["total"]

        # 显式 materialize=true 才物化派生本体（版本 1.0）
        r = c.post(f"/api/fusion/requests/{_ctx['fusion_id']}/execute", params=P,
                   json={"materialize": True})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        _ctx["fusion_ont"] = d["ontology_id"]
        assert d["version"] == "1.0"
        assert d["initial"] is True and d["changed"] is True and d["materialized"] is True
        assert c.get("/api/ontologies", params=P).json()["data"]["total"] == _nont_ont_before + 1

        g = c.get(f"/api/ontologies/{_ctx['fusion_ont']}", params=P).json()["data"]
        assert g.get("fusion", {}).get("derived") is True
        # TP-02-12：显式"派生数据，非权威"标记
        assert g["fusion"].get("authoritative") is False
        assert "非权威" in (g["fusion"].get("derived_note") or "")
        assert g["fusion"]["fusion_id"] == _ctx["fusion_id"]
        assert g["status"] == "active"
        fused = [o for o in g["objects"] if str(o.get("id", "")).startswith("fus_")]
        passthrough = [o for o in g["objects"] if str(o.get("id", "")).startswith("pt_")]
        assert len(fused) == 1 and len(passthrough) == 1, [o["id"] for o in g["objects"]]
        fo = fused[0]
        # 融合对象 stable_key 取源侧 stable_key 对应 canonical（code → canon_code）
        assert fo["stable_key"] == "canon_code"
        assert all(a.get("fusion_sources") for a in fo["attributes"])
        _ctx["fusion_obj_id"] = fo["id"]
        _ctx["pt_obj_id"] = passthrough[0]["id"]

        # 源本体无变化重执行 → 版本不变（无空版本）
        r = c.post(f"/api/fusion/requests/{_ctx['fusion_id']}/execute", params=P,
                   json={"materialize": True})
        assert r.status_code == 200
        assert r.json()["data"]["changed"] is False
        assert r.json()["data"]["version"] == "1.0"

    # ------------------------------------------------------------------ #
    # TP-02-01/02/11 虚拟融合查询 + TP-02-03 只读断言
    # ------------------------------------------------------------------ #
    def test_02_fusion_query_merge(self) -> None:
        c = self.c
        store = self.store
        store.insert(_mk_upload("fu-a1", _ctx["ontA"], "obj_supp", "S001",
                                {"code": "S001", "name": "甲", "phone": "138"}))
        store.insert(_mk_upload("fu-a2", _ctx["ontA"], "obj_supp", "S002",
                                {"code": "S002", "name": "乙", "phone": "139"}))
        store.insert(_mk_upload("fu-a3", _ctx["ontA"], "obj_note", "N1",
                                {"note_code": "N1", "text": "备注内容"}))
        store.insert(_mk_upload("fu-b1", _ctx["ontB"], "obj_supp_b", "S001",
                                {"code": "S001", "name": "甲B", "phone": "137"}))
        store.insert(_mk_upload("fu-b2", _ctx["ontB"], "obj_supp_b", "S003",
                                {"code": "S003", "name": "丙", "phone": "136"}))

        # TP-02-03：查询前快照源本体与实例存储
        _ctx["src_before"] = {oid: _snap(c, oid)
                              for oid in (_ctx["ontA"], _ctx["ontB"], _ctx["fusion_ont"])}
        _ctx["inst_before"] = json.dumps(store.all(), sort_keys=True, ensure_ascii=False)

        r = c.post("/api/fusion/query", params=P, json={"fusion_id": _ctx["fusion_id"]})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        _ctx["query1"] = d
        assert d["version"] == "1.0"
        assert d["total"] == 4  # S001/S002/S003（融合对象）+ N1（透传对象）
        by_key = {it["stable_key_value"]: it for it in d["items"]}

        # TP-02-01：同键合并一条，双来源标记
        it1 = by_key["S001"]
        assert len(it1["sources"]) == 2
        assert {s["ontology_id"] for s in it1["sources"]} == {_ctx["ontA"], _ctx["ontB"]}
        cc = it1["merged"]["canon_code"]
        assert cc["value"] == "S001"
        assert set(cc.get("sources") or []) == {_ctx["ontA"], _ctx["ontB"]}  # 双来源
        # TP-02-02：phone 有冲突裁决 → 权威源 A 取值 138，双值全量保留（不静默覆盖）
        ph = it1["merged"]["canon_phone"]
        assert ph["conflict"] is True
        assert ph["value"] == "138" and ph["source"] == _ctx["ontA"]
        assert ph["values"] == {_ctx["ontA"]: "138", _ctx["ontB"]: "137"}
        # name 无权威决策 → 双值并存
        nm = it1["merged"]["canon_name"]
        assert nm["conflict"] is True and nm.get("authoritative_missing") is True
        assert nm["values"] == {_ctx["ontA"]: "甲", _ctx["ontB"]: "甲B"}
        assert set(it1["conflicts"]) == {"canon_phone", "canon_name"}
        # 异键多条、单源标记
        assert by_key["S002"]["merged"]["canon_name"] == {
            "value": "乙", "source": _ctx["ontA"]}
        assert len(by_key["S003"]["sources"]) == 1
        assert by_key["S003"]["sources"][0]["ontology_id"] == _ctx["ontB"]
        assert by_key["S003"]["merged"]["canon_name"]["value"] == "丙"
        # TP-02-14：键不重合保持独立记录（显式 independent 标记＋来源本体标记），不强行合并
        assert by_key["S001"]["independent"] is False            # 双源合并
        assert by_key["S002"]["independent"] is True
        assert by_key["S002"]["sources"][0]["ontology_id"] == _ctx["ontA"]
        assert by_key["S003"]["independent"] is True
        # 透传对象：未映射对象不合并，单源直出
        n1 = by_key["N1"]
        assert n1["object"] == _ctx["pt_obj_id"]
        assert n1["independent"] is True
        assert len(n1["sources"]) == 1
        # 分渠道取数：上传型=本地（不回源、不 stale）
        assert all(s.get("content_mode") == "local" for s in it1["sources"])
        assert all(not s.get("error") for s in it1["sources"])

        # 过滤：stable_key 精确 / 融合对象
        f1 = c.post("/api/fusion/query", params=P,
                    json={"fusion_id": _ctx["fusion_id"], "stable_key_value": "S001"}).json()["data"]
        assert f1["total"] == 1
        f2 = c.post("/api/fusion/query", params=P,
                    json={"fusion_id": _ctx["fusion_id"],
                          "object_type": _ctx["fusion_obj_id"]}).json()["data"]
        assert f2["total"] == 3

        # 幂等 + TP-02-03 负向：查询不改源本体快照 / 实例存储
        r2 = c.post("/api/fusion/query", params=P, json={"fusion_id": _ctx["fusion_id"]}).json()["data"]
        assert r2["total"] == d["total"]
        for oid, snap in _ctx["src_before"].items():
            assert _snap(c, oid) == snap, f"源本体快照被融合查询修改：{oid}"
        assert json.dumps(store.all(), sort_keys=True, ensure_ascii=False) == _ctx["inst_before"], \
            "实例存储被融合查询修改"

    # ------------------------------------------------------------------ #
    # TP-02-03：源连接内容永远源端取（联邦渠道；源端变化查询结果随之变化）
    # ------------------------------------------------------------------ #
    def test_03_federated_content_from_source(self) -> None:
        c = self.c
        os.environ["V202_FU_TOKEN"] = "secret-123"
        mock = start_mock([{"supp_code": "S100", "name": "丁", "phone": "150"}])
        try:
            r = c.post("/api/mcp/connections", params=P, json={
                "display_name": "融合联邦源（mock）",
                "transport": "http",
                "endpoint": mock.read_url,
                "auth": {"type": "bearer_token", "bearer_token_ref": "${V202_FU_TOKEN}"},
                "tool_allowlist": ["read"],
                "maps_to_ontology": {
                    "ontology_id": _ctx["ontA"],
                    "object_type": "obj_supp",
                    "field_map": {"supp_code": "code", "name": "name", "phone": "phone"},
                    "read_tool": "read",
                },
            })
            assert r.status_code == 201, r.text
            conn_id = r.json()["data"]["connection_id"]

            r = c.post(f"/api/mcp/connections/{conn_id}/read", params=P)
            assert r.status_code == 200, r.text
            cand = r.json()["data"]["candidate_id"]
            r = c.post(f"/api/ingest/candidates/{cand}/approve", params=P)
            assert r.status_code == 200, r.text
            assert r.json()["data"]["imported"] == 1
            rows = c.get("/api/ingest/instances",
                         params={**P, "ontology_id": _ctx["ontA"],
                                 "object_type": "obj_supp"}).json()["data"]["items"]
            fed_id = next(x["id"] for x in rows if (x.get("instance") or {}).get("code") == "S100")

            # 审批剥离：本地无内容值（INV-1）→ 融合查询必须回源
            rec = self.store.get(fed_id)
            assert "phone" not in (rec.get("instance") or {})
            assert "name" not in (rec.get("instance") or {})

            def _q_s100() -> dict:
                q = c.post("/api/fusion/query", params=P,
                           json={"fusion_id": _ctx["fusion_id"], "stable_key_value": "S100"})
                assert q.status_code == 200, q.text
                d = q.json()["data"]
                assert d["total"] == 1
                return d["items"][0]

            it = _q_s100()
            assert it["merged"]["canon_phone"]["value"] == "150"
            src = next(s for s in it["sources"] if s["ontology_id"] == _ctx["ontA"])
            assert src["channel"] == "external_mcp"
            assert src["content_mode"] == "federated"
            assert not src.get("error")

            # 源端变化 → 查询结果随之变化（永远源端取，非本地快照）
            mock.read_records[0]["phone"] = "151"
            it2 = _q_s100()
            assert it2["merged"]["canon_phone"]["value"] == "151"
            # 取数不回写本地（快照仍剥离）
            rec2 = self.store.get(fed_id)
            assert "phone" not in (rec2.get("instance") or {})
        finally:
            mock.stop()

        # 源不可达 → 显式降级留痕（不抛错给用户、不静默返回旧值）
        q = c.post("/api/fusion/query", params=P,
                   json={"fusion_id": _ctx["fusion_id"], "stable_key_value": "S100"})
        assert q.status_code == 200, q.text
        it3 = q.json()["data"]["items"][0]
        src3 = next(s for s in it3["sources"] if s["ontology_id"] == _ctx["ontA"])
        assert src3.get("error"), "源端取数失败应显式留痕"
        assert it3["merged"].get("canon_phone") is None

    # ------------------------------------------------------------------ #
    # TP-02-05：版本历史 / 语义 diff / 回滚 / 固定版本引用
    # ------------------------------------------------------------------ #
    def test_04_versions_diff_rollback_fixed(self) -> None:
        c = self.c
        v0 = c.get(f"/api/ontologies/{_ctx['fusion_ont']}/versions", params=P).json()["data"]
        assert v0["current"] == "1.0"
        assert [x["version"] for x in v0["items"]] == ["1.0"]

        # 等价映射调整（name 移出等价映射 → 降级为两个单源 canonical）＝版本演化
        r = c.put(f"/api/fusion/requests/{_ctx['fusion_id']}/mappings", params=P, json={
            "object_groups": [{
                "canonical_name": "供应商",
                "objects": [
                    {"ontology_id": _ctx["ontA"], "object_id": "obj_supp"},
                    {"ontology_id": _ctx["ontB"], "object_id": "obj_supp_b"},
                ],
                "attribute_pairs": [
                    [{"ontology_id": _ctx["ontA"], "attr_id": "code"},
                     {"ontology_id": _ctx["ontB"], "attr_id": "code"}],
                    [{"ontology_id": _ctx["ontA"], "attr_id": "phone"},
                     {"ontology_id": _ctx["ontB"], "attr_id": "phone"}],
                ],
            }],
        })
        assert r.status_code == 200, r.text
        res = r.json()["data"]["version_result"]
        assert res["changed"] is True
        new_ver = res["version"]
        assert new_ver != "1.0" and int(new_ver.split(".")[0]) >= 2
        _ctx["new_ver"] = new_ver

        cur = c.get(f"/api/ontologies/{_ctx['fusion_ont']}", params=P).json()["data"]
        attr_ids = {a["id"] for a in _fus_object(cur)["attributes"]}
        assert "canon_name" not in attr_ids
        assert "canon_code" in attr_ids and "canon_phone" in attr_ids

        # 版本历史（append-only）
        v1 = c.get(f"/api/ontologies/{_ctx['fusion_ont']}/versions", params=P).json()["data"]
        assert v1["current"] == new_ver
        assert {x["version"] for x in v1["items"]} == {"1.0", new_ver}

        # 语义 diff
        d = c.get(f"/api/ontologies/{_ctx['fusion_ont']}/versions/diff",
                  params={**P, "from": "1.0", "to": new_ver})
        assert d.status_code == 200, d.text
        diff = d.json()["data"]["diff"]
        kinds = {(ch["kind"], ch.get("attribute_id")) for ch in diff["changes"]}
        assert ("attribute_removed", "canon_name") in kinds
        assert diff["compatible"] is False

        # 消费方固定版本引用：1.0 查询仍按旧 TBox（canon_name 双值冲突）
        q1 = c.post("/api/fusion/query", params=P,
                    json={"fusion_id": _ctx["fusion_id"], "stable_key_value": "S001",
                          "version": "1.0"}).json()["data"]
        assert q1["version"] == "1.0"
        assert q1["items"][0]["merged"]["canon_name"]["conflict"] is True
        # 当前版本查询：canon_name 已不在 TBox
        q2 = c.post("/api/fusion/query", params=P,
                    json={"fusion_id": _ctx["fusion_id"], "stable_key_value": "S001"}).json()["data"]
        assert q2["version"] == new_ver
        assert "canon_name" not in q2["items"][0]["merged"]

        # 回滚到 1.0：append-only 产生新版本，历史不删不改
        rb = c.post(f"/api/ontologies/{_ctx['fusion_ont']}/versions/1.0/rollback",
                    params=P, json={"note": "M4-A3 测试回滚"})
        assert rb.status_code == 200, rb.text
        rb_ver = rb.json()["data"]["version"]
        assert rb_ver != new_ver
        cur2 = c.get(f"/api/ontologies/{_ctx['fusion_ont']}", params=P).json()["data"]
        assert "canon_name" in {a["id"] for a in _fus_object(cur2)["attributes"]}
        # 历史版本快照仍可查且未变
        g2 = c.get(f"/api/ontologies/{_ctx['fusion_ont']}/versions/1.0", params=P).json()["data"]
        assert "canon_name" in {a["id"] for a in _fus_object(g2["model"])["attributes"]}
        # 固定引用回滚后的版本可查
        q3 = c.post("/api/fusion/query", params=P,
                    json={"fusion_id": _ctx["fusion_id"], "stable_key_value": "S001",
                          "version": rb_ver})
        assert q3.status_code == 200
        assert q3.json()["data"]["version"] == rb_ver
        # 回滚到当前版本 → 409
        cur_now = c.get(f"/api/ontologies/{_ctx['fusion_ont']}/versions",
                        params=P).json()["data"]["current"]
        bad = c.post(f"/api/ontologies/{_ctx['fusion_ont']}/versions/{cur_now}/rollback",
                     params=P, json={})
        assert bad.status_code == 409
        # append-only：三个版本全部保留
        v2 = c.get(f"/api/ontologies/{_ctx['fusion_ont']}/versions", params=P).json()["data"]
        assert {x["version"] for x in v2["items"]} == {"1.0", new_ver, rb_ver}

    # ------------------------------------------------------------------ #
    # MCP/治理通道：发布态边界 + TOOLS_CONTRACT
    # ------------------------------------------------------------------ #
    def test_05_mcp_governance_channel(self) -> None:
        from mcp_server import TOOLS_CONTRACT
        import fusion as fu

        assert "fusion_requests" in TOOLS_CONTRACT
        assert "fusion_query" in TOOLS_CONTRACT
        assert TOOLS_CONTRACT["fusion_requests"]["side_effect"] is False
        assert TOOLS_CONTRACT["fusion_query"]["side_effect"] is False

        # 治理身份（user=None）查询
        d = fu.fusion_query(PID, None, fusion_id=_ctx["fusion_id"])
        assert d["total"] >= 4 and d["version"]

        # 源本体下线 → MCP 通道不可见（发布态边界）
        # 注意：JsonStore.insert 是追加而非按 id 覆盖，改状态必须用 update，
        # 否则库里同时存在同 id 的 active 与 draft 两条，发布态边界校验失效
        ont = JsonStore("ontologies", PID)
        rec = ont.get(_ctx["ontB"])
        try:
            ont.update(_ctx["ontB"], {"status": "draft", "published_at": None})
            try:
                fu.fusion_query(PID, None, fusion_id=_ctx["fusion_id"])
                raise AssertionError("源本体未发布时 MCP 通道应被阻断")
            except fu.FusionError as e:
                assert e.http_status == 404
        finally:
            ont.update(_ctx["ontB"], {"status": "active",
                                      "published_at": rec.get("published_at") or _now()})
        # 重新发布后恢复
        d2 = fu.fusion_query(PID, None, fusion_id=_ctx["fusion_id"])
        assert d2["total"] >= 4
