"""多文档增量建模（确定性对齐＋from-doc 合并目标＋采纳沉淀字典同义词）— 单测＋API。

设计（extract-then-merge）：文档独立抽取成新草稿 → diff/采纳前按
「名称归一化精确 → 数据字典同义词（可选）→ 确定性相似度」把源对象 ID 对齐到目标
（歧义不对齐、不猜测）→ 复用 GAP-09 差异审核逐项人工确认合并；采纳后语义对齐的
名称对沉淀为字典词条同义词（建模与字典互哺闭环）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _authenv import auth_module_guard
import main as m
import dictionary as dict_svc
from draft_align import align_model
from fastapi.testclient import TestClient

_guard = auth_module_guard()  # 本模块无鉴权用例，保持鉴权关
app = m.app
client = TestClient(app)

PROJ = f"多文档合并-{int(time.time())}"
PROJ_ID = ""
PROJ_OTHER = ""
TARGET_ID = ""
SOURCE_ID = ""


def _post_onto(project_id: str, body: dict):
    r = client.post("/api/ontologies", params={"project_id": project_id}, json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["data"]


def _obj(oid, name, attrs=(), rels=()):
    return {
        "id": oid, "name": name,
        "attributes": [{"id": a[0], "name": a[1], "data_type": "string"} for a in attrs],
        "relations": [{"id": r[0], "name": r[1], "target_object": r[2]} for r in rels],
    }


class TestAlignUnit:
    """对齐模块纯函数单测（不依赖应用/LLM）。"""

    def _model(self, objects):
        return {"objects": objects, "constraints": [], "functions": []}

    def test_object_exact_name_align(self):
        target = self._model([_obj("obj_t_order", "订单", [("attr_t_no", "订单号")])])
        source = self._model([_obj("obj_s_order", "订单", [("attr_s_no", "订单号")])])
        aligned, info = align_model(target, source, None)
        assert len(info) == 1
        e = info[0]
        assert e["target_object_id"] == "obj_t_order" and e["method"] == "exact"
        assert e["confidence"] == 1.0
        assert aligned["objects"][0]["id"] == "obj_t_order"
        # 同名属性 ID 漂移同样对齐（避免假的新增+删除属性）
        assert aligned["objects"][0]["attributes"][0]["id"] == "attr_t_no"
        assert e["attributes"][0]["target_attribute_id"] == "attr_t_no"

    def test_object_semantic_align(self):
        # 「订单表」包含「订单」→ 0.75 + 0.25*(2/3) ≈ 0.917 ≥ 0.85
        target = self._model([_obj("obj_t_order", "订单")])
        source = self._model([_obj("obj_s", "订单表")])
        aligned, info = align_model(target, source, None)
        assert len(info) == 1 and info[0]["method"] == "semantic"
        assert 0.85 <= info[0]["confidence"] <= 1.0

    def test_ambiguity_no_align(self):
        # 「客户管理」vs「客户管理系统」(0.917)/「客户管理台」(0.95)：差距 < 0.1 → 歧义不对齐
        target = self._model([
            _obj("a", "客户管理系统"),
            _obj("b", "客户管理台"),
        ])
        source = self._model([_obj("s", "客户管理")])
        aligned, info = align_model(target, source, None)
        assert info == []
        assert aligned["objects"][0]["id"] == "s"  # 保持新增语义，交人工裁决

    def test_no_match_stays_new(self):
        target = self._model([_obj("a", "订单")])
        source = self._model([_obj("s", "退款单")])
        aligned, info = align_model(target, source, None)
        assert info == []
        assert aligned["objects"][0]["id"] == "s"

    def test_relation_remap_and_relation_id_align(self):
        target = self._model([
            _obj("obj_t_order", "订单", rels=[("rel_t1", "所属客户", "obj_t_customer")]),
            _obj("obj_t_customer", "客户"),
        ])
        source = self._model([
            _obj("obj_s_order", "订单", rels=[("rel_s1", "所属客户", "obj_s_customer")]),
            _obj("obj_s_customer", "客户"),
        ])
        aligned, info = align_model(target, source, None)
        assert len(info) == 2  # 订单、客户 均对齐
        by_id = {o["id"]: o for o in aligned["objects"]}
        assert set(by_id) == {"obj_t_order", "obj_t_customer"}
        rel = by_id["obj_t_order"]["relations"][0]
        assert rel["target_object"] == "obj_t_customer"  # 端点重映射
        assert rel["id"] == "rel_t1"  # （名称,目标对象）完全一致 → 关系 ID 对齐

    def test_dictionary_synonym_channel(self):
        # 字典未维护时该通道自然为空；维护后 输入名∈词条同义词 → 高置信对齐
        pid = f"对齐词典-{int(time.time() * 1000)}"
        dict_svc.create_term(pid, {"canonical": "客户", "object_type": "obj_t_customer",
                                   "synonyms": ["买家"]})
        target = self._model([_obj("obj_t_customer", "客户")])
        source = self._model([_obj("obj_s_buyer", "买家")])
        aligned, info = align_model(target, source, pid)
        assert len(info) == 1
        assert info[0]["method"] == "synonym" and info[0]["confidence"] == 1.0

    def test_id_same_skips_object_align_but_aligns_attrs(self):
        # 对象 ID 相同：无对象级对齐记录，但属性 ID 漂移仍按名称对齐
        target = self._model([_obj("obj_x", "订单", [("attr_t_no", "订单号")])])
        source = self._model([_obj("obj_x", "订单", [("attr_s_no", "订单号")])])
        aligned, info = align_model(target, source, None)
        assert info == []
        assert aligned["objects"][0]["attributes"][0]["id"] == "attr_t_no"


class TestFromDocMergeTarget:
    """from-doc 合并目标参数（合并字段在任务创建时登记，与 worker 进度无关）。"""

    def setup_method(self):
        global PROJ_ID, PROJ_OTHER
        if not PROJ_ID:
            r = client.post("/api/projects", json={"name": PROJ})
            assert r.status_code in (200, 201), r.text
            PROJ_ID = r.json()["data"]["id"]
            r2 = client.post("/api/projects", json={"name": PROJ + "-隔离"})
            assert r2.status_code in (200, 201), r2.text
            PROJ_OTHER = r2.json()["data"]["id"]

    def test_merge_target_registered(self):
        global TARGET_ID
        if not TARGET_ID:
            TARGET_ID = _post_onto(PROJ_ID, {
                "name": "目标本体",
                "objects": [
                    _obj("obj_t_order", "订单", [("attr_t_no", "订单号"), ("attr_t_amt", "金额")]),
                    _obj("obj_t_customer", "客户", [("attr_t_name", "姓名")]),
                ],
            })["id"]
        files = {"file": ("prd.txt", "客户可以下多个订单。".encode("utf-8"), "text/plain")}
        data = {"ontology_id": TARGET_ID}
        r = client.post("/api/ontologies/from-doc", params={"project_id": PROJ_ID},
                        files=files, data=data)
        assert r.status_code == 202, r.text
        job_id = r.json()["data"]["job_id"]
        assert r.json()["data"]["merge_target_id"] == TARGET_ID
        rj = client.get(f"/api/ontologies/jobs/{job_id}", params={"project_id": PROJ_ID})
        assert rj.status_code == 200
        rec = rj.json()["data"]
        assert rec["merge_target_id"] == TARGET_ID and rec["merge_target_name"] == "目标本体"

    def test_merge_target_not_found(self):
        files = {"file": ("a.txt", "订单".encode("utf-8"), "text/plain")}
        r = client.post("/api/ontologies/from-doc", params={"project_id": PROJ_ID},
                        files=files, data={"ontology_id": "nonexistent-xyz"})
        assert r.status_code == 404

    def test_merge_target_project_isolation(self):
        other = _post_onto(PROJ_OTHER, {"name": "别项目目标", "objects": [_obj("o1", "订单", [("attr_a", "名称")])]})
        files = {"file": ("a.txt", "订单".encode("utf-8"), "text/plain")}
        r = client.post("/api/ontologies/from-doc", params={"project_id": PROJ_ID},
                        files=files, data={"ontology_id": other["id"]})
        assert r.status_code == 404


class TestDiffAlignAndAdopt:
    """draft-diff 对齐 + adopt 合并 + 同义词沉淀（同一目标本体链式断言）。"""

    @classmethod
    def setup_class(cls):
        global PROJ_ID, TARGET_ID, SOURCE_ID
        if not PROJ_ID:
            r = client.post("/api/projects", json={"name": PROJ})
            assert r.status_code in (200, 201), r.text
            PROJ_ID = r.json()["data"]["id"]
        if not TARGET_ID:
            TARGET_ID = _post_onto(PROJ_ID, {
                "name": "目标本体",
                "objects": [
                    _obj("obj_t_order", "订单", [("attr_t_no", "订单号"), ("attr_t_amt", "金额")]),
                    _obj("obj_t_customer", "客户", [("attr_t_name", "姓名")]),
                ],
            })["id"]
        if not SOURCE_ID:
            SOURCE_ID = _post_onto(PROJ_ID, {
                "name": "文档草稿",
                "objects": [
                    _obj("obj_s_order", "订单",
                         [("attr_s_no", "订单号"), ("attr_s_remark", "备注")],
                         [("rel_s1", "所属客户", "obj_s_customer")]),
                    _obj("obj_s_customer", "客户", [("attr_s_note", "备注")]),
                ],
            })["id"]

    def test_diff_aligned_no_false_changes(self):
        r = client.post(f"/api/ontologies/{TARGET_ID}/draft-diff",
                        params={"project_id": PROJ_ID},
                        json={"source_draft_id": SOURCE_ID, "align": True})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["aligned_count"] == 2  # 订单、客户 名称对齐（ID 不同）
        kinds = {(c["kind"], c.get("object_id")) for c in d["diff"]["changes"]}
        # 对齐后无整对象假变更；真实增量：订单新增「备注」属性＋「所属客户」关系、客户新增「备注」属性；
        # 目标独有属性（金额/姓名）因源未覆盖报删除、客户业务键变化 → 不兼容，前端默认不勾选
        assert d["change_count"] == 6
        assert ("attribute_added", "obj_t_order") in kinds
        assert ("relation_added", "obj_t_order") in kinds
        assert ("attribute_added", "obj_t_customer") in kinds
        assert ("attribute_removed", "obj_t_order") in kinds
        assert ("attribute_removed", "obj_t_customer") in kinds
        assert ("stable_key_changed", "obj_t_customer") in kinds
        # 不得出现对齐假变更：整对象新增/删除
        assert not any(c["kind"] in ("object_added", "object_removed") for c in d["diff"]["changes"])
        # 对齐明细含方法与置信度
        methods = {e["source_object_id"]: e["method"] for e in d["alignment"]}
        assert methods == {"obj_s_order": "exact", "obj_s_customer": "exact"}

    def test_diff_no_align_falls_back_to_id(self):
        r = client.post(f"/api/ontologies/{TARGET_ID}/draft-diff",
                        params={"project_id": PROJ_ID},
                        json={"source_draft_id": SOURCE_ID, "align": False})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["aligned_count"] == 0
        kinds = {c["kind"] for c in d["diff"]["changes"]}
        assert "object_added" in kinds and "object_removed" in kinds  # ID 口径下的假变更

    def test_adopt_applies_aligned_changes(self):
        # 与前端默认一致：仅勾选 compatible 变更（删除属性等不兼容项不勾选）
        rd = client.post(f"/api/ontologies/{TARGET_ID}/draft-diff",
                         params={"project_id": PROJ_ID},
                         json={"source_draft_id": SOURCE_ID, "align": True})
        idx = [c["index"] for c in rd.json()["data"]["diff"]["changes"] if c.get("compatible")]
        r = client.post(f"/api/ontologies/{TARGET_ID}/adopt-changes",
                        params={"project_id": PROJ_ID},
                        json={"source_draft_id": SOURCE_ID, "adopt": idx,
                              "align": True, "note": "多文档合并"})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["adopted"] == 3 and d["aligned_count"] == 2
        got = client.get(f"/api/ontologies/{TARGET_ID}", params={"project_id": PROJ_ID})
        assert got.status_code == 200
        objs = {o["id"]: o for o in got.json()["data"]["objects"]}
        assert {"attr_t_no", "attr_t_amt", "attr_s_remark"} <= \
               {a["id"] for a in objs["obj_t_order"]["attributes"]}
        assert any(rel["id"] == "rel_s1" and rel["target_object"] == "obj_t_customer"
                   for rel in objs["obj_t_order"]["relations"])
        assert {"attr_t_name", "attr_s_note"} <= {a["id"] for a in objs["obj_t_customer"]["attributes"]}
        # 精确对齐无新信息 → 不沉淀同义词
        assert d["synonyms_recorded"] == 0

    def test_adopt_semantic_align_records_synonym(self):
        # 「订单表」与「订单」语义对齐（包含 0.917）→ 采纳后沉淀字典同义词
        sem_src = _post_onto(PROJ_ID, {
            "name": "语义草稿",
            "objects": [
                _obj("obj_sm", "订单表", [("attr_sm", "支付方式"),
                                          ("attr_s_no2", "订单号")],
                     [("rel_sm", "所属客户", "obj_t_customer")]),
            ],
        })["id"]
        before = len(dict_svc.list_terms(PROJ_ID))
        rd = client.post(f"/api/ontologies/{TARGET_ID}/draft-diff",
                         params={"project_id": PROJ_ID},
                         json={"source_draft_id": sem_src, "align": True})
        idx = [c["index"] for c in rd.json()["data"]["diff"]["changes"] if c.get("compatible")]
        r = client.post(f"/api/ontologies/{TARGET_ID}/adopt-changes",
                        params={"project_id": PROJ_ID},
                        json={"source_draft_id": sem_src, "adopt": idx, "align": True})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["aligned_count"] == 1
        assert d["synonyms_recorded"] == 1
        terms = dict_svc.list_terms(PROJ_ID)
        assert len(terms) == before + 1
        t = next(t for t in terms if t["object_type"] == "obj_t_order")
        assert t["canonical"] == "订单" and "订单表" in t["synonyms"]


class TestFromDocE2EDiff:
    """真实 worker：from-doc（fallback 抽取）→ 新草稿 → 与已有本体对齐 diff（E2E）。"""

    @classmethod
    def setup_class(cls):
        global PROJ_ID
        if not PROJ_ID:
            r = client.post("/api/projects", json={"name": PROJ})
            assert r.status_code in (200, 201), r.text
            PROJ_ID = r.json()["data"]["id"]
        # 目标本体：与 fallback 模板同名的对象，但 ID 不同
        cls.target_id = _post_onto(PROJ_ID, {
            "name": "E2E目标",
            "objects": [
                _obj("obj_e2e_order", "订单", [("attr_e2e_no", "订单号")]),
                _obj("obj_e2e_customer", "客户", [("attr_e2e_name", "名称")]),
            ],
        })["id"]

    def test_e2e(self):
        files = {"file": ("prd.txt", "客户可以下多个订单，订单包含商品。".encode("utf-8"), "text/plain")}
        r = client.post("/api/ontologies/from-doc", params={"project_id": PROJ_ID},
                        files=files, data={"ontology_id": self.target_id})
        assert r.status_code == 202, r.text
        job_id = r.json()["data"]["job_id"]
        data = None
        for _ in range(40):
            rj = client.get(f"/api/ontologies/jobs/{job_id}", params={"project_id": PROJ_ID})
            data = rj.json()["data"]
            if data["status"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.25)
        assert data and data["status"] == "done", data
        draft_id = data["draft_id"]
        assert draft_id and data["merge_target_id"] == self.target_id

        rd = client.post(f"/api/ontologies/{self.target_id}/draft-diff",
                         params={"project_id": PROJ_ID},
                         json={"source_draft_id": draft_id, "align": True})
        assert rd.status_code == 200, rd.text
        dd = rd.json()["data"]
        # fallback 抽出的「客户/订单/商品」与目标同名对象对齐（至少订单、客户）
        names = {e["target_object_id"] for e in dd["alignment"]}
        assert "obj_e2e_order" in names and "obj_e2e_customer" in names
        # 无整对象假变更
        assert not any(c["kind"] in ("object_added", "object_removed")
                       for c in dd["diff"]["changes"]
                       if c.get("object_id") in ("obj_e2e_order", "obj_e2e_customer"))
