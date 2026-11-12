"""V2-02 跨本体对齐（M4-A②）：L5 回归。

覆盖 TP-02-04/06/07/08/13 + EP-02-01/02/04/06（MCP 同等暴露）：
- 四类信号（key overlap / common origin / semantic / structural）＋置信度加权＋证据
- 可见性约束：候选对须请求者对本体双方同时可见
- 会签状态机：同 owner 即签即生效 / 多 owner 一票否决 / owner 缺位 admin 代行
  （proxy_reason 必填＋alignment_proxy_sign 审计）/ 共享等级不携带审批权
- 属性冲突（值域 / 必填 / 主键）：未决策不可批准 → 显式权威源决策 → 通过
- 被拒对不重提，证据显著变化（delta>=0.20）才重提（repropose=True）
- MCP TOOLS_CONTRACT 只读工具＋两个 stdio 桥同步包装
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import alignment
from main import app

c = TestClient(app)
_ctx: dict = {}

AUTH_ENV_KEYS = ("ONTOLOGY_AUTH_ENABLED",
                 "ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD",
                 "ONTOLOGY_PBKDF2_ITERATIONS")


@pytest.fixture(scope="module", autouse=True)
def _restore_auth_env():
    prev = {k: os.environ.get(k) for k in AUTH_ENV_KEYS}
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _p() -> dict:
    return {"project_id": _ctx["project"]}


def _hdr(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _attr(aid: str, name: str, dtype: str = "string", required: bool = True) -> dict:
    return {"id": aid, "name": name, "data_type": dtype, "required": required,
            "constraints": {}}


def _onto_body(name: str, obj_id: str, obj_name: str, attrs: list,
               stable_key: str | None = None) -> dict:
    return {"name": name, "objects": [{
        "id": obj_id, "name": obj_name, "business_definition": f"{obj_name} 测试对象",
        "stable_key": stable_key or (attrs[0]["id"] if attrs else None),
        "attributes": attrs, "relations": [],
    }], "constraints": []}


SUPP_ATTRS = [_attr("attr_code", "供应商编码"),
              _attr("attr_name", "供应商名称"),
              _attr("attr_phone", "电话", "string", True)]
# B 侧电话：integer 且非必填 → 值域＋必填两类冲突
SUPP_ATTRS_B = [_attr("attr_code", "供应商编码"),
                _attr("attr_name", "供应商名称"),
                _attr("attr_phone", "电话", "integer", False)]


class TestV202M4bAlignment:
    def test_01_four_signals_unit(self):
        """TP-02-06：四类信号精确计算＋置信度加权（证据非空）。"""
        oa = {"id": "obj_a", "name": "客户", "business_definition": "客户主数据",
              "stable_key": "attr_code",
              "attributes": [{"id": "attr_code", "name": "客户编码"},
                             {"id": "attr_name", "name": "客户名称"},
                             {"id": "attr_phone", "name": "电话"}],
              "relations": []}
        ob = {"id": "obj_b", "name": "客户", "business_definition": "客户主数据",
              "stable_key": "attr_code",
              "attributes": [{"id": "attr_code", "name": "客户编码"},
                             {"id": "attr_name", "name": "客户名称"},
                             {"id": "attr_mail", "name": "邮箱"}],
              "relations": []}
        ont_a, ont_b = {"id": "ont_a", "name": "A"}, {"id": "ont_b", "name": "B"}
        sig_a = ({"obj_a": {"C1", "C2", "C3"}}, {})
        sig_b = ({"obj_b": {"C2", "C3", "C4"}}, {})
        s = alignment.object_pair_signals(ont_a, oa, ont_b, ob, sig_a, sig_b)
        assert set(s["signals"]) == {"key_overlap", "common_origin", "semantic", "structural"}
        # Jaccard({C1,C2,C3},{C2,C3,C4}) = 2/4
        assert s["signals"]["key_overlap"] == pytest.approx(0.5)
        assert s["signals"]["common_origin"] == 0
        assert s["signals"]["semantic"] > 0.5
        assert s["signals"]["structural"] > 0
        assert s["evidence"]["key_overlap"]
        assert "2" in s["evidence"]["key_overlap"]
        # 置信度 = 信号加权（权重 0.40/0.25/0.20/0.15）
        exp = round(sum(alignment.SIGNAL_WEIGHTS[k] * v
                        for k, v in s["signals"].items()), 4)
        assert s["confidence"] == exp
        # 共同来源信号
        sig_a2 = ({"obj_a": {"C1"}}, {"obj_a": {("http", "conn1")}})
        sig_b2 = ({"obj_b": {"C1"}}, {"obj_b": {("http", "conn1")}})
        s2 = alignment.object_pair_signals(ont_a, oa, ont_b, ob, sig_a2, sig_b2)
        assert s2["signals"]["common_origin"] == 1
        ev_origin = s2["evidence"]["common_origin"]
        ev_origin = ev_origin if isinstance(ev_origin, str) else " | ".join(map(str, ev_origin))
        assert "conn1" in ev_origin

    def test_02_candidates_dev_mode(self):
        """EP-02-01/02：候选检测 e2e（dev 模式）＋声明参数校验（TP-02-13 后备通道）。"""
        r = c.post("/api/projects", json={"name": "M4-A2 跨本体对齐"})
        assert r.status_code == 201, r.text
        _ctx["project"] = r.json()["data"]["id"]

        ra = c.post("/api/ontologies", params=_p(),
                    json=_onto_body("客户主数据A", "obj_cust_a", "客户", SUPP_ATTRS))
        assert ra.status_code == 201, ra.text
        _ctx["ont_a"] = ra.json()["data"]["id"]
        rb = c.post("/api/ontologies", params=_p(),
                    json=_onto_body("客户主数据B", "obj_cust_b", "客户", SUPP_ATTRS_B))
        assert rb.status_code == 201, rb.text
        _ctx["ont_b"] = rb.json()["data"]["id"]

        r = c.post("/api/alignment/candidates", params=_p())
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["scanned_pairs"] == 1
        cand = d["candidates"][0]
        assert {cand["ontology_a"], cand["ontology_b"]} == {_ctx["ont_a"], _ctx["ont_b"]}
        assert cand["confidence"] > 0 and cand["repropose"] is False
        op = cand["object_pairs"][0]
        assert {op["object_a"], op["object_b"]} == {"obj_cust_a", "obj_cust_b"}
        assert set(op["signals"]) == {"key_overlap", "common_origin", "semantic", "structural"}
        assert op["evidence"]["semantic"]

        # 声明校验：自身对 / 空对象对 / 不存在对象
        base = {"ontology_a": _ctx["ont_a"], "ontology_b": _ctx["ont_a"],
                "object_pairs": [{"object_a": "obj_cust_a", "object_b": "obj_cust_a"}]}
        r = c.post("/api/alignment/declarations", params=_p(), json=base)
        assert r.status_code == 422 and r.json()["error"]["code"] == "BAD_PAIR"
        r = c.post("/api/alignment/declarations", params=_p(), json={
            "ontology_a": _ctx["ont_a"], "ontology_b": _ctx["ont_b"], "object_pairs": []})
        assert r.status_code == 422 and r.json()["error"]["code"] == "BAD_BODY"
        r = c.post("/api/alignment/declarations", params=_p(), json={
            "ontology_a": _ctx["ont_a"], "ontology_b": _ctx["ont_b"],
            "object_pairs": [{"object_a": "obj_cust_a", "object_b": "nope"}]})
        assert r.status_code == 404
        r = c.get("/api/alignment/declarations/nonexistent", params=_p())
        assert r.status_code == 404
        r = c.get("/api/alignment/declarations", params=_p())
        assert r.json()["data"]["total"] == 0

    def test_03_auth_users(self):
        """启用鉴权：bootstrap admin ＋ 多角色用户（TP-02-08 前置）。"""
        os.environ["ONTOLOGY_AUTH_ENABLED"] = "true"
        os.environ["ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD"] = "m4b-admin-pw"
        os.environ["ONTOLOGY_PBKDF2_ITERATIONS"] = "10000"
        r = c.post("/api/auth/login", params={"mode": "bearer"},
                   json={"username": "admin", "password": "m4b-admin-pw"})
        assert r.status_code == 200, r.text
        _ctx["admin"] = r.json()["data"]["token"]
        _ctx["tok_admin"] = _ctx["admin"]
        for uname, role in (("owner_a", "modeler"), ("owner_b", "modeler"),
                            ("viewer", "viewer"), ("ghost", "modeler"),
                            ("modeler_x", "modeler")):
            r = c.post("/api/auth/users", headers=_hdr(_ctx["admin"]),
                       json={"username": uname, "password": f"pw-{uname}", "role": role})
            assert r.status_code == 201, r.text
            _ctx[f"u_{uname}"] = r.json()["data"]
            r = c.post("/api/auth/login", params={"mode": "bearer"},
                       json={"username": uname, "password": f"pw-{uname}"})
            assert r.status_code == 200, r.text
            _ctx[f"tok_{uname}"] = r.json()["data"]["token"]
        # 未认证被拒（401，不泄露存在性）
        r = c.post("/api/alignment/candidates", params=_p())
        assert r.status_code == 401

    def test_04_visibility_constraint(self):
        """TP-02-07：候选对须请求者对本体双方同时可见。"""
        ra = c.post("/api/ontologies", params=_p(), headers=_hdr(_ctx["tok_owner_a"]),
                    json=_onto_body("供应商数据A", "obj_supp_a", "供应商", SUPP_ATTRS))
        assert ra.status_code == 201, ra.text
        _ctx["ca"] = ra.json()["data"]["id"]
        rb = c.post("/api/ontologies", params=_p(), headers=_hdr(_ctx["tok_owner_b"]),
                    json=_onto_body("供应商数据B", "obj_supp_b", "供应商", SUPP_ATTRS_B))
        assert rb.status_code == 201, rb.text
        _ctx["cb"] = rb.json()["data"]["id"]

        # 双方 private：owner_a 只见自己 CA → CA⇄CB 对不出现
        r = c.post("/api/alignment/candidates", params=_p(), headers=_hdr(_ctx["tok_owner_a"]))
        assert r.status_code == 200, r.text
        d_a = r.json()["data"]
        assert _ctx["ca"] in d_a["visible_ontologies"]
        assert _ctx["cb"] not in d_a["visible_ontologies"]
        cand_pairs_a = [{x["ontology_a"], x["ontology_b"]} for x in d_a["candidates"]]
        assert {_ctx["ca"], _ctx["cb"]} not in cand_pairs_a

        # CA 升 department：owner_b 双方可见 → CA⇄CB 出现；owner_a 仍不可见 CB → 不出现
        r = c.patch(f"/api/ontologies/{_ctx['ca']}/sharing", params=_p(),
                    headers=_hdr(_ctx["tok_owner_a"]), json={"sharing": "department"})
        assert r.status_code == 200, r.text
        d_b = c.post("/api/alignment/candidates", params=_p(),
                     headers=_hdr(_ctx["tok_owner_b"])).json()["data"]
        assert {_ctx["ca"], _ctx["cb"]} in [{x["ontology_a"], x["ontology_b"]}
                                            for x in d_b["candidates"]]
        d_a2 = c.post("/api/alignment/candidates", params=_p(),
                      headers=_hdr(_ctx["tok_owner_a"])).json()["data"]
        assert {_ctx["ca"], _ctx["cb"]} not in [{x["ontology_a"], x["ontology_b"]}
                                                for x in d_a2["candidates"]]

        # CD private(owner_b)：owner_b 可见 CA⇄CD；owner_a 不可见 CD → 不出现
        rc = c.post("/api/ontologies", params=_p(), headers=_hdr(_ctx["tok_owner_b"]),
                    json=_onto_body("供应商数据C", "obj_supp_c", "供应商", SUPP_ATTRS_B))
        assert rc.status_code == 201, rc.text
        _ctx["cd"] = rc.json()["data"]["id"]
        d_a3 = c.post("/api/alignment/candidates", params=_p(),
                      headers=_hdr(_ctx["tok_owner_a"])).json()["data"]
        assert {_ctx["ca"], _ctx["cd"]} not in [{x["ontology_a"], x["ontology_b"]}
                                                for x in d_a3["candidates"]]
        d_b2 = c.post("/api/alignment/candidates", params=_p(),
                      headers=_hdr(_ctx["tok_owner_b"])).json()["data"]
        assert {_ctx["ca"], _ctx["cd"]} in [{x["ontology_a"], x["ontology_b"]}
                                            for x in d_b2["candidates"]]

        # 只读角色（viewer）可发起候选检测（只读 POST），且只见 department 级 CA
        r = c.post("/api/alignment/candidates", params=_p(), headers=_hdr(_ctx["tok_viewer"]))
        assert r.status_code == 200, r.text
        vis = set(r.json()["data"]["visible_ontologies"])
        assert _ctx["ca"] in vis and _ctx["cb"] not in vis and _ctx["cd"] not in vis

    def test_05_declaration_conflict_sign_flow(self):
        """TP-02-04/08/13：手动声明 → 属性冲突决策 → 会签状态机（一票否决）＋审计。"""
        # CB 升 department（提交人须对本体双方同时可见，否则 404 不泄露存在性）
        r = c.patch(f"/api/ontologies/{_ctx['cb']}/sharing", params=_p(),
                    headers=_hdr(_ctx["tok_owner_b"]), json={"sharing": "department"})
        assert r.status_code == 200, r.text
        body = {"ontology_a": _ctx["ca"], "ontology_b": _ctx["cb"],
                "object_pairs": [{"object_a": "obj_supp_a", "object_b": "obj_supp_b",
                                  "attribute_pairs": [{"attr_a": "attr_phone",
                                                       "attr_b": "attr_phone"}]}],
                "candidate": {"confidence": 0.5}}
        # viewer 声明 → 403（write 动作）
        r = c.post("/api/alignment/declarations", params=_p(),
                   headers=_hdr(_ctx["tok_viewer"]), json=body)
        assert r.status_code == 403
        r = c.post("/api/alignment/declarations", params=_p(),
                   headers=_hdr(_ctx["tok_owner_a"]), json=body)
        assert r.status_code == 201, r.text
        d = r.json()["data"]
        _ctx["decl1"] = d["id"]
        assert d["status"] == "pending" and d["submitted_by"] == "owner_a"
        # 冲突：value_domain(string vs integer) ＋ required(True vs False)
        assert sorted(x["type"] for x in d["conflicts"]) == ["required", "value_domain"]
        assert all(x["resolution"] is None for x in d["conflicts"])
        assert {(s["owner"], s["ontology_id"]) for s in d["signers_required"]} == \
            {("owner_a", _ctx["ca"]), ("owner_b", _ctx["cb"])}
        assert all(s["present"] for s in d["signers_required"])

        # 同对 pending 再声明 → 409
        r = c.post("/api/alignment/declarations", params=_p(),
                   headers=_hdr(_ctx["tok_owner_a"]), json=body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "DECLARATION_IN_PROGRESS"

        # 冲突未决策先批准 → 422
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/sign", params=_p(),
                   headers=_hdr(_ctx["tok_owner_a"]), json={"decision": "approve"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "CONFLICT_RESOLUTION_REQUIRED"

        # 冲突决策权限：viewer（middleware write 403）/ 无关 modeler（RESOLVE_NOT_ALLOWED）
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/conflicts/0/resolve",
                   params=_p(), headers=_hdr(_ctx["tok_viewer"]), json={"resolution": "a"})
        assert r.status_code == 403
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/conflicts/0/resolve",
                   params=_p(), headers=_hdr(_ctx["tok_modeler_x"]), json={"resolution": "a"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "RESOLVE_NOT_ALLOWED"

        # owner_a 决策两个冲突（权威源 a / b）
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/conflicts/0/resolve",
                   params=_p(), headers=_hdr(_ctx["tok_owner_a"]), json={"resolution": "a"})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["conflicts"][0]["resolution"] == "a"
        assert r.json()["data"]["conflicts"][0]["resolved_by"] == "owner_a"
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/conflicts/1/resolve",
                   params=_p(), headers=_hdr(_ctx["tok_owner_a"]), json={"resolution": "b"})
        assert r.json()["data"]["conflicts"][1]["resolution"] == "b"
        # 非法决议值 → 422
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/conflicts/0/resolve",
                   params=_p(), headers=_hdr(_ctx["tok_owner_a"]), json={"resolution": "c"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "BAD_RESOLUTION"
        # 越界索引 → 422
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/conflicts/9/resolve",
                   params=_p(), headers=_hdr(_ctx["tok_owner_a"]), json={"resolution": "a"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "CONFLICT_NOT_FOUND"

        # owner_a 批准 → 仍 pending（owner_b 未签）
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/sign", params=_p(),
                   headers=_hdr(_ctx["tok_owner_a"]), json={"decision": "approve"})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["status"] == "pending"
        assert [(s["owner"], s["ontology_id"]) for s in d["signatures"]] == \
            [("owner_a", _ctx["ca"])]

        # owner_b 拒绝 → 一票否决
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/sign", params=_p(),
                   headers=_hdr(_ctx["tok_owner_b"]),
                   json={"decision": "reject", "comment": "电话值域不统一，先治理再对齐"})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["status"] == "rejected" and d["decided_by"] == "owner_b"
        assert "治理" in d["reject_reason"]

        # 终态：再签 → 403；撤回 → 403
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/sign", params=_p(),
                   headers=_hdr(_ctx["tok_admin"]),
                   json={"decision": "approve", "proxy_reason": "x"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "DECLARATION_NOT_PENDING"
        r = c.post(f"/api/alignment/declarations/{_ctx['decl1']}/withdraw", params=_p(),
                   headers=_hdr(_ctx["tok_owner_a"]))
        assert r.status_code == 403

        # 审计留痕
        for action in ("alignment_declare", "alignment_sign_denied",
                       "alignment_sign_reject", "alignment_conflict_resolve"):
            r = c.get("/api/audit",
                      params={"action": action, "project_id": _ctx["project"]},
                      headers=_hdr(_ctx["tok_admin"]))
            assert r.status_code == 200, r.text
            items = r.json()["data"]["items"]
            assert any(i["target_id"] == _ctx["decl1"] for i in items), action

    def test_06_same_owner_immediate(self):
        """TP-02-08：同 owner 持两侧本体——一次会签满足两条 (owner, ontology_id) 即生效。"""
        ra = c.post("/api/ontologies", params=_p(), headers=_hdr(_ctx["tok_owner_a"]),
                    json=_onto_body("产品数据E", "obj_prod_e", "产品",
                                    [_attr("attr_code", "产品编码"), _attr("attr_name", "产品名称")]))
        assert ra.status_code == 201, ra.text
        _ctx["ce"] = ra.json()["data"]["id"]
        body = {"ontology_a": _ctx["ca"], "ontology_b": _ctx["ce"],
                "object_pairs": [{"object_a": "obj_supp_a", "object_b": "obj_prod_e"}]}
        r = c.post("/api/alignment/declarations", params=_p(),
                   headers=_hdr(_ctx["tok_owner_a"]), json=body)
        assert r.status_code == 201, r.text
        d = r.json()["data"]
        _ctx["decl2"] = d["id"]
        assert {(s["owner"], s["ontology_id"]) for s in d["signers_required"]} == \
            {("owner_a", _ctx["ca"]), ("owner_a", _ctx["ce"])}
        assert d["conflicts"] == []
        r = c.post(f"/api/alignment/declarations/{_ctx['decl2']}/sign", params=_p(),
                   headers=_hdr(_ctx["tok_owner_a"]), json={"decision": "approve"})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["status"] == "approved" and d["decided_by"] == "owner_a"
        assert {(s["owner"], s["ontology_id"]) for s in d["signatures"]} == \
            {("owner_a", _ctx["ca"]), ("owner_a", _ctx["ce"])}

    def test_07_owner_absent_admin_proxy(self):
        """TP-02-08：owner 缺位 → admin 代行（proxy_reason 必填＋审计）；共享等级不携带审批权。"""
        rg = c.post("/api/ontologies", params=_p(), headers=_hdr(_ctx["tok_ghost"]),
                    json=_onto_body("仓库数据G", "obj_wh_g", "仓库",
                                    [_attr("attr_code", "仓库编码"), _attr("attr_name", "仓库名称")]))
        assert rg.status_code == 201, rg.text
        _ctx["cg"] = rg.json()["data"]["id"]
        # CG 升 department（否则 owner_a 不可见，声明将 404 不泄露存在性）
        r = c.patch(f"/api/ontologies/{_ctx['cg']}/sharing", params=_p(),
                    headers=_hdr(_ctx["tok_ghost"]), json={"sharing": "department"})
        assert r.status_code == 200, r.text
        # owner 账号缺位（进程内删除，模拟账号注销）
        import auth as auth_svc
        ghost = next(u for u in auth_svc.list_users() if u["username"] == "ghost")
        auth_svc._store("auth_users").delete(ghost["id"])
        r = c.get("/api/alignment/declarations", params=_p(), headers=_hdr(_ctx["tok_ghost"]))
        assert r.status_code == 401  # 旧令牌失效（用户态实时查询）

        body = {"ontology_a": _ctx["cg"], "ontology_b": _ctx["ca"],
                "object_pairs": [{"object_a": "obj_wh_g", "object_b": "obj_supp_a"}]}
        r = c.post("/api/alignment/declarations", params=_p(),
                   headers=_hdr(_ctx["tok_owner_a"]), json=body)
        assert r.status_code == 201, r.text
        d = r.json()["data"]
        _ctx["decl3"] = d["id"]
        by_owner = {s["owner"]: s for s in d["signers_required"]}
        assert by_owner["ghost"]["present"] is False
        assert by_owner["owner_a"]["present"] is True

        # owner_a 批准自己份额 → 仍 pending（ghost 份额缺位）
        r = c.post(f"/api/alignment/declarations/{_ctx['decl3']}/sign", params=_p(),
                   headers=_hdr(_ctx["tok_owner_a"]), json={"decision": "approve"})
        assert r.status_code == 200 and r.json()["data"]["status"] == "pending"

        # modeler_x 在 shared_with 内（可见）但非 owner → 无审批权（共享等级不携带审批权）
        r = c.patch(f"/api/ontologies/{_ctx['cg']}/sharing", params=_p(),
                    headers=_hdr(_ctx["tok_admin"]),
                    json={"sharing": "project", "shared_with": ["modeler_x"]})
        assert r.status_code == 200, r.text
        r = c.post(f"/api/alignment/declarations/{_ctx['decl3']}/sign", params=_p(),
                   headers=_hdr(_ctx["tok_modeler_x"]),
                   json={"decision": "approve", "proxy_reason": "自称代行"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "SIGNER_NOT_ALLOWED"

        # admin 代行缺原因 → 422
        r = c.post(f"/api/alignment/declarations/{_ctx['decl3']}/sign", params=_p(),
                   headers=_hdr(_ctx["tok_admin"]), json={"decision": "approve"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "PROXY_REASON_REQUIRED"
        # admin 代行有原因 → 全签齐生效
        r = c.post(f"/api/alignment/declarations/{_ctx['decl3']}/sign", params=_p(),
                   headers=_hdr(_ctx["tok_admin"]),
                   json={"decision": "approve", "proxy_reason": "ghost 账号已注销，平台管理员代行"})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["status"] == "approved" and d["decided_by"] == "admin"
        proxy_sig = [s for s in d["signatures"] if s["role"] == "tenant_admin_proxy"]
        assert len(proxy_sig) == 1
        assert proxy_sig[0]["owner"] == "admin" and proxy_sig[0]["signed_for"] == "ghost"
        assert proxy_sig[0]["proxy_reason"]
        r = c.get("/api/audit",
                  params={"action": "alignment_proxy_sign", "project_id": _ctx["project"]},
                  headers=_hdr(_ctx["tok_admin"]))
        assert any(i["target_id"] == _ctx["decl3"]
                   for i in r.json()["data"]["items"])

    def test_08_rejected_repropose_rule(self):
        """TP-02-06：被拒对不重提；证据显著变化（delta>=REPROPOSE_DELTA）才重提。"""
        pid = "v202-m4b-x"
        store = alignment._store(pid)
        for r_ in store.all():
            store.delete(r_["id"])
        onts = [
            {"id": "ox", "name": "X", "owner": "dev", "sharing": "public",
             "objects": [{"id": "objx", "name": "客户", "stable_key": "attr_k",
                          "attributes": [{"id": "attr_k", "name": "编码"}], "relations": []}]},
            {"id": "oy", "name": "Y", "owner": "dev", "sharing": "public",
             "objects": [{"id": "objy", "name": "客户", "stable_key": "attr_k",
                          "attributes": [{"id": "attr_k", "name": "编码"}], "relations": []}]},
        ]
        conf = alignment.detect_candidates(None, {"id": pid}, onts)["candidates"][0]["confidence"]
        assert conf > 0
        # rejected ＋ 证据未显著变化 → 不重提
        store.insert({"id": "d1", "ontology_a": "ox", "ontology_b": "oy",
                      "status": "rejected", "last_confidence": conf,
                      "submitted_at": "2026-11-13T00:00:00Z"})
        assert alignment.detect_candidates(None, {"id": pid}, onts)["candidates"] == []
        # rejected ＋ 证据显著变化（delta >= 0.20）→ 重提且 repropose=True
        store.update("d1", {"last_confidence": round(conf - alignment.REPROPOSE_DELTA - 0.05, 4)})
        out = alignment.detect_candidates(None, {"id": pid}, onts)
        assert len(out["candidates"]) == 1
        assert out["candidates"][0]["repropose"] is True
        # pending / approved → 永不重提
        store.update("d1", {"status": "pending"})
        assert alignment.detect_candidates(None, {"id": pid}, onts)["candidates"] == []
        store.update("d1", {"status": "approved"})
        assert alignment.detect_candidates(None, {"id": pid}, onts)["candidates"] == []

    def test_09_mcp_exposure(self):
        """EP-02-01/02/04/06 MCP 同等暴露：TOOLS_CONTRACT 只读工具＋stdio 桥包装＋MCP 语义。"""
        from mcp_server import TOOLS_CONTRACT
        for name in ("alignment_candidates", "alignment_declarations"):
            meta = TOOLS_CONTRACT[name]
            assert meta["side_effect"] is False
            assert meta["reads"] and not meta["writes"]
        here = Path(__file__).resolve().parents[1]
        for f in ("mcp_stdio_bridge.py", "mcp_stdio_bridge_v2.py"):
            src = (here / f).read_text(encoding="utf-8")
            assert "def alignment_candidates(" in src
            assert "def alignment_declarations(" in src
            assert '_client.call("alignment_candidates"' in src
            assert '_client.call("alignment_declarations"' in src
        # MCP 通道语义：user=None 跳过用户可见性（访问边界=发布态，调用方预筛）
        out = alignment.detect_candidates(None, {"id": "v202-m4b-x"}, [
            {"id": "ox2", "name": "Z",
             "objects": [{"id": "o1", "name": "客户", "stable_key": "attr_k",
                          "attributes": [{"id": "attr_k", "name": "编码"}], "relations": []}]},
            {"id": "oy2", "name": "W",
             "objects": [{"id": "o2", "name": "客户", "stable_key": "attr_k",
                          "attributes": [{"id": "attr_k", "name": "编码"}], "relations": []}]},
        ])
        assert sorted(out["visible_ontologies"]) == ["ox2", "oy2"]
        assert out["candidates"] and out["candidates"][0]["confidence"] > 0
