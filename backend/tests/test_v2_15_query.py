"""V2-15 开放查询接口（M4-B）：L5 回归。

覆盖 TC-15-01~07 + TCE-15-01~03：
- TC-15-01 SPARQL 命名图查询（本图仅：schema＋关系快照＋上传型值）
- TC-15-02 REST（对象类型＋条件＋分页）＋ GraphQL（schema 由本体动态生成）
- TC-15-03 三接口均受 V2-05 权限约束（401/403）
- TC-15-04 命名图隔离（禁跨图访问）
- TC-15-05 数据边界（源连接型非键内容不返回＋边界提示走 MCP 联邦读）
- TC-15-06 返回字段来源标记（schema/snapshot/upload）
- TC-15-07 上传型内容读权限（V2-06 显式开放：默认不可读、开放后可读）
- TCE-15-01 非法/超大查询（语法错误标准语义；行数上限拦截）
- TCE-15-02 查询未发布本体（404 不暴露存在性）
- TCE-15-03 introspection 探测（仅已发布 schema）
"""
import json
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from _authenv import auth_module_guard
from main import app
from core.instance_store import get_instance_store
from core.storage import JsonStore

pytestmark = pytest.mark.auth
_guard = auth_module_guard()  # GAP-20：模块边界隔离鉴权环境（本模块含 TC-15-03/07 鉴权用例）

PID = "v215-m4b"
ONT: str | None = None      # 已发布本体（_setup 赋值）
DRAFT: str | None = None    # 未发布（draft）本体（_setup 赋值）

c = TestClient(app)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _mk_oid() -> str:
    return "o" + uuid.uuid4().hex[:12]


def _b(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _ok(resp) -> dict:
    assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("success") is True, body
    return body["data"]


def _gql(resp) -> dict:
    """GraphQL 原生响应体（{data, errors}，非平台信封）。"""
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "data" in body, body
    return body


def _err(resp, want_code: str, want_status: int = 422) -> dict:
    assert resp.status_code == want_status, f"{resp.status_code} != {want_status}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("success") is False, body
    assert body["error"]["code"] == want_code, body["error"]
    return body["error"]


# ---------------------------------------------------------------- 构造数据

def _create_ontology(name: str, objects: list) -> str:
    body = _ok(c.post("/api/ontologies", params={"project_id": PID},
                      json={"name": name, "objects": objects}))
    return body["id"]


def _publish(oid: str) -> None:
    _ok(c.post(f"/api/ontologies/{oid}/publish", params={"project_id": PID}))


def _rdf_sync(oid: str) -> None:
    _ok(c.post(f"/api/graph/{oid}/rdf-sync", params={"project_id": PID}))


def _insert(rec_id: str, object_type: str, inst: dict,
            channel: str = "upload", uploaded_by: str | None = None,
            stable_key: str = "orderCode") -> None:
    rec = {
        "id": rec_id, "project_id": PID, "ontology_id": ONT, "object_type": object_type,
        "stable_key": stable_key, "stable_key_value": inst.get(stable_key),
        "channel": channel, "instance": inst, "imported_at": _now(),
    }
    if uploaded_by:
        rec["uploaded_by"] = uploaded_by
    get_instance_store(PID).insert(rec)


U1, U2, F1 = "v215-u1", "v215-u2", "v215-f1"


def _setup() -> None:
    global ONT, DRAFT
    if ONT and JsonStore("ontologies", PID).get(ONT):
        return  # 幂等（模块内多次调用安全）
    objects = [
        {"id": "Order", "name": "订单", "stable_key": "orderCode",
         "attributes": [
             {"id": "orderCode", "name": "订单号", "data_type": "string", "required": True},
             {"id": "amount", "name": "金额", "data_type": "float"},
             {"id": "note", "name": "备注", "data_type": "string"},
         ],
         "relations": [
             {"id": "customer", "name": "客户", "target_object": "Customer",
              "cardinality": "many_to_one"},
         ]},
        {"id": "Customer", "name": "客户", "stable_key": "customerCode",
         "attributes": [
             {"id": "customerCode", "name": "客户号", "data_type": "string", "required": True},
             {"id": "name", "name": "名称", "data_type": "string"},
         ],
         "relations": []},
    ]
    ONT = _create_ontology("V215 订单本体", objects)
    # 未发布本体（对象名带特征，供 introspection 断言不可见）
    DRAFT = _create_ontology("V215 草稿本体", [
        {"id": "SecretObj", "name": "机密对象", "stable_key": "sk",
         "attributes": [{"id": "sk", "name": "键", "data_type": "string"}],
         "relations": []},
    ])
    _publish(ONT)
    _insert(U1, "Order", {"orderCode": "Q15-U1", "amount": 100.5,
                          "note": "n1", "customer": "C-1"})
    _insert(U2, "Customer", {"customerCode": "C-1", "name": "Alice"},
            stable_key="customerCode")
    _insert(F1, "Order",
            {"orderCode": "Q15-F1", "customer": "C-1",
             "_source": {"kind": "field_map", "connection_id": "conn-x"}},
            channel="external_mcp")
    _rdf_sync(ONT)


_setup()

B = "http://ontology.local/"
Q = {
    "schema": f"SELECT ?p WHERE {{ <{B}Order> ?p ?o }} LIMIT 10",
    "label_u1": f"SELECT ?o WHERE {{ <{B}inst_{U1}> <http://www.w3.org/2000/01/rdf-schema#label> ?o }}",
    "amount_u1": f"SELECT ?o WHERE {{ <{B}inst_{U1}> <{B}amount> ?o }}",
    "amount_f1": f"SELECT ?o WHERE {{ <{B}inst_{F1}> <{B}amount> ?o }}",
    "mixed": "SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 100",
}


def _sparql(query: str, oid: str = ONT) -> dict | None:
    r = c.post("/api/query/sparql", params={"project_id": PID},
               json={"ontology_id": oid, "query": query})
    if r.status_code != 200:
        return None
    return _ok(r)


# ------------------------------------------------- TC-15-01 SPARQL 命名图查询

def test_01_sparql_named_graph():
    d = _sparql(Q["schema"])
    assert d is not None
    assert d["graph"] == f"urn:ontology:{ONT}"
    assert d["count"] > 0, "schema 三元组应存在"
    # 本图仅含本本体数据：上传型值在图内
    d = _sparql(Q["label_u1"])
    assert d["count"] == 1 and d["results"][0]["o"] == "Q15-U1"
    d = _sparql(Q["amount_u1"])
    assert d["count"] == 1
    assert abs(float(str(d["results"][0]["o"])) - 100.5) < 1e-9  # file 模式 XSD float 可能回读为 str
    # 混合数据（schema/snapshot/upload 同图）
    d = _sparql(Q["mixed"])
    subs = {row["s"] for row in d["results"]}
    assert any(s.startswith(B + "inst_") for s in subs), "应含实例（快照/上传）三元组"
    assert not any(s.startswith(B + "inst_") and s == f"{B}Order" for s in subs)
    # 响应级来源图例 + 数据边界
    assert set(d["provenance"].keys()) == {"schema", "snapshot", "upload"}
    assert "MCP 联邦读" in d["data_boundary"]


# ----------------------------------- TC-15-02 REST（对象类型＋条件＋分页）＋GraphQL

def test_02_rest_and_graphql():
    # REST：对象类型 + 分页
    d = _ok(c.get("/api/query/instances",
                  params={"project_id": PID, "ontology_id": ONT,
                          "object_type": "Order", "pageSize": 1}))
    assert d["total"] == 2 and len(d["items"]) == 1
    d = _ok(c.get("/api/query/instances",
                  params={"project_id": PID, "ontology_id": ONT, "pageSize": 50}))
    assert d["total"] == 3
    # REST：条件过滤
    d = _ok(c.get("/api/query/instances",
                  params={"project_id": PID, "ontology_id": ONT,
                          "filter": json.dumps({"orderCode": "Q15-U1"})}))
    assert d["total"] == 1 and d["items"][0]["id"] == U1
    # REST：不存在对象类型 → 空
    d = _ok(c.get("/api/query/instances",
                  params={"project_id": PID, "ontology_id": ONT, "object_type": "Nope"}))
    assert d["total"] == 0

    # GraphQL：schema 由本体动态生成（对象/属性/关系可见）
    gql_schema = """
    query {
      ontology {
        id name
        objectTypes { id name stableKey attributes { id dataType } relations { id targetObject } }
      }
    }
    """
    d = _gql(c.post("/api/query/graphql", params={"project_id": PID},
                   json={"ontology_id": ONT, "query": gql_schema}))
    ont = d["data"]["ontology"]
    assert ont["id"] == ONT
    by_id = {o["id"]: o for o in ont["objectTypes"]}
    assert by_id["Order"]["stableKey"] == "orderCode"
    assert {a["id"]: a["dataType"] for a in by_id["Order"]["attributes"]}["amount"] == "float"
    assert by_id["Order"]["relations"][0]["targetObject"] == "Customer"

    # GraphQL：实例查询（字段按本体属性命名）
    gql_inst = """
    query ($id: String) {
      Order(id: $id) { id orderCode amount customer _fieldSources { name source } }
    }
    """
    d = _gql(c.post("/api/query/graphql", params={"project_id": PID},
                   json={"ontology_id": ONT, "query": gql_inst,
                         "variables": {"id": U1}}))
    rows = d["data"]["Order"]
    assert len(rows) == 1 and rows[0]["orderCode"] == "Q15-U1"


# --------------------------------------------------- TC-15-04 命名图隔离（禁跨图）

def test_04_graph_isolation():
    # GRAPH 子句 → 422 CROSS_GRAPH_FORBIDDEN（远程模式跨图向量）
    for q in (
        f"SELECT ?s ?o WHERE {{ GRAPH <urn:ontology:{DRAFT}> {{ ?s ?p ?o }} }}",
        "SELECT ?s ?o FROM <urn:ontology:other> WHERE { ?s ?p ?o }",
        "SELECT ?s ?o WHERE { ?s ?p ?o } USING NAMED <urn:ontology:other>",
        "SELECT ?s ?o WHERE { SERVICE <http://evil.example/sparql> { ?s ?p ?o } }",
    ):
        e = _err(c.post("/api/query/sparql", params={"project_id": PID},
                        json={"ontology_id": ONT, "query": q}), "CROSS_GRAPH_FORBIDDEN")
        assert "命名图" in e["message"]
    # 目标本体查询只返回本本体数据：实例 subject 只能是本本体三条实例
    # （schema 声明含 SHACL 空节点/rdfs 命名空间 subject，属本图合法内容）
    d = _sparql(Q["mixed"])
    inst_subjects = {row["s"] for row in d["results"]
                     if str(row["s"]).startswith(B + "inst_")}
    assert inst_subjects <= {B + "inst_" + r for r in (U1, U2, F1)}
    assert B + "inst_" + U1 in inst_subjects


# ------------------------------------------------------ TC-15-05 数据边界 INV-5

def test_05_data_boundary():
    # SPARQL：源连接型实例的非键属性不在图内
    d = _sparql(Q["amount_f1"])
    assert d["count"] == 0, "源连接型非键内容不得进入命名图（INV-1/INV-5）"
    # REST：F1 行仅快照字段＋边界提示（走 MCP 联邦读）
    d = _ok(c.get("/api/query/instances",
                  params={"project_id": PID, "ontology_id": ONT, "object_type": "Order"}))
    f1 = next(x for x in d["items"] if x["id"] == F1)
    assert f1["channel"] == "federated"
    assert set(f1["instance"].keys()) == {"orderCode", "customer"}
    assert "amount" not in f1["instance"] and "note" not in f1["instance"]
    assert set(f1["_field_sources"].values()) == {"snapshot"}
    assert f1["_content_boundary"]["restricted_fields"] == ["amount", "note"]
    assert "MCP 联邦读" in f1["_content_boundary"]["hint"]
    # GraphQL：F1 非键字段 null＋边界提示
    d = _gql(c.post("/api/query/graphql", params={"project_id": PID},
                   json={"ontology_id": ONT,
                         "query": "query { Order(id: \"%s\") { id amount "
                                  "_contentBoundary { hint } } }" % F1}))
    assert not d.get("errors"), d.get("errors")
    row = d["data"]["Order"][0]
    assert row["amount"] is None
    assert "MCP 联邦读" in row["_contentBoundary"]["hint"]


# ------------------------------------------------------ TC-15-06 字段来源标记

def test_06_field_sources():
    d = _ok(c.get("/api/query/instances",
                  params={"project_id": PID, "ontology_id": ONT, "object_type": "Order"}))
    u1 = next(x for x in d["items"] if x["id"] == U1)
    # schema 来源标记（对象定义）
    assert u1["object"]["source"] == "schema" and u1["object"]["stable_key"] == "orderCode"
    # dev 态（M2-A 测试态豁免 = admin 全放行）：上传型内容完整可见，标记 upload
    assert u1["instance"].get("amount") == 100.5 and u1["instance"].get("note") == "n1"
    assert u1["_field_sources"] == {
        "orderCode": "upload", "amount": "upload", "note": "upload", "customer": "upload",
    }
    assert "_content_restricted" not in u1
    # V2-06 收紧路径（默认不可读→显式开放）见 test_10（鉴权启用态）
    # SPARQL 响应级来源图例（schema/snapshot/upload 三类）
    d = _sparql(Q["mixed"])
    assert set(d["provenance"].keys()) == {"schema", "snapshot", "upload"}


# -------------------------------------------------- TCE-15-02 未发布 → 404 不暴露存在性

def test_07_unpublished_404():
    r_draft = c.post("/api/query/sparql", params={"project_id": PID},
                     json={"ontology_id": DRAFT, "query": Q["mixed"]})
    r_missing = c.post("/api/query/sparql", params={"project_id": PID},
                       json={"ontology_id": "no-such-ont", "query": Q["mixed"]})
    e1 = _err(r_draft, "NOT_FOUND", want_status=404)
    e2 = _err(r_missing, "NOT_FOUND", want_status=404)
    assert e1["message"] == e2["message"] == "本体不存在或未发布"
    # REST / GraphQL 同语义
    r = c.get("/api/query/instances", params={"project_id": PID, "ontology_id": DRAFT})
    _err(r, "NOT_FOUND", want_status=404)
    r = c.post("/api/query/graphql", params={"project_id": PID},
               json={"ontology_id": DRAFT, "query": "query { ontology { id } }"})
    _err(r, "NOT_FOUND", want_status=404)


# ------------------------------------------- TCE-15-03 introspection 仅已发布 schema

def test_08_introspection_published_only():
    d = _gql(c.post("/api/query/graphql", params={"project_id": PID},
                   json={"ontology_id": ONT,
                         "query": "query { __schema { types { name } } }"}))
    names = {t["name"] for t in d["data"]["__schema"]["types"]}
    # 已发布本体的对象类型可见
    assert "Order" in names and "Customer" in names
    # 动态 schema 结构类型可见
    assert {"Ontology", "ObjectType", "Attribute", "Relation",
            "FieldSource", "ContentBoundary"} <= names
    # 未发布本体的对象类型不可见
    assert "SecretObj" not in names
    # 查询未发布本体 → 404（schema 均不可得）
    _err(c.post("/api/query/graphql", params={"project_id": PID},
                json={"ontology_id": DRAFT,
                      "query": "query { __schema { types { name } } }"}),
         "NOT_FOUND", want_status=404)


# ------------------------------------------------ TCE-15-01 非法/超大查询护栏

def test_09_invalid_and_oversized():
    # 语法错误 → 422 QUERY_SYNTAX_ERROR（标准语义）
    _err(c.post("/api/query/sparql", params={"project_id": PID},
                json={"ontology_id": ONT, "query": "SELECT ?s WHERE {"}),
         "QUERY_SYNTAX_ERROR")
    # 非 SELECT → 422 QUERY_TYPE_NOT_ALLOWED
    _err(c.post("/api/query/sparql", params={"project_id": PID},
                json={"ontology_id": ONT, "query": "ASK { ?s ?p ?o }"}),
         "QUERY_TYPE_NOT_ALLOWED")
    _err(c.post("/api/query/sparql", params={"project_id": PID},
                json={"ontology_id": ONT,
                      "query": "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"}),
         "QUERY_TYPE_NOT_ALLOWED")
    # 缺 LIMIT → 注入默认上限
    d = _sparql("SELECT ?s ?p ?o WHERE { ?s ?p ?o }")
    assert d["limit"] == 1000 and d["count"] <= 1000
    # 超限 LIMIT → 钳制到上限
    d = _sparql("SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 999999")
    assert d["limit"] == 5000 and d["count"] <= 5000
    # 语法错误经标准错误信封（GraphQL 原生语义：errors 数组）
    d = _gql(c.post("/api/query/graphql", params={"project_id": PID},
                   json={"ontology_id": ONT, "query": "query { ontology { id "}))
    assert d.get("errors") and d["data"] is None
    # GraphQL 行数护栏：limit 钳制 200
    d = _gql(c.post("/api/query/graphql", params={"project_id": PID},
                   json={"ontology_id": ONT,
                         "query": "query { Order(limit: 99999) { id } }"}))
    assert len(d["data"]["Order"]) <= 200


# ------------------------------------ TC-15-03 / TC-15-07 鉴权与上传型读权限

def _login(username: str, password: str) -> str:
    r = c.post("/api/auth/login", params={"mode": "bearer"},
               json={"username": username, "password": password})
    return _ok(r)["token"]


def test_10_auth_and_upload_content_permission():
    """TC-15-03（401/403）＋ TC-15-07（上传型内容 V2-06 显式开放）。"""
    # 清空用户存储 → 本测试独占 bootstrap（密码可控）；测后恢复豁免态
    import auth as auth_svc
    ustore = JsonStore("auth_users", project_id="_shared")  # 与 auth._store 同库（全局共享）
    for u in ustore.all():
        ustore.delete(u["id"])
    os.environ["ONTOLOGY_AUTH_ENABLED"] = "true"
    os.environ["ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD"] = "v215-admin-pw"
    os.environ["ONTOLOGY_PBKDF2_ITERATIONS"] = "10000"  # 测试提速
    try:
        auth_svc.ensure_bootstrap()  # /login 为 public 端点不触发 bootstrap，此处显式创建
        admin = _login("admin", "v215-admin-pw")
        hdr = _b(admin)

        def _mk_user(username: str, level: str) -> str:
            _ok(c.post("/api/auth/users", headers=hdr, json={
                "username": username, "password": "v215-pw-123", "role": "viewer",
                "grants": [{"scope": "project", "target": PID, "level": level}],
            }))
            return _login(username, "v215-pw-123")

        tok_none = _mk_user("v215-none", "none")
        tok_read = _mk_user("v215-read", "read")
        tok_up1 = _mk_user("v215-up1", "write")  # 上传者具备项目 write（上传/开放内容均需 write）
        tok_up2 = _mk_user("v215-up2", "read")

        # ---- TC-15-03：无凭据 401 / 权限不足 403（三接口一致） ----
        r = c.post("/api/query/sparql", params={"project_id": PID},
                   json={"ontology_id": ONT, "query": Q["mixed"]})
        assert r.status_code == 401
        r = c.get("/api/query/instances", params={"project_id": PID, "ontology_id": ONT})
        assert r.status_code == 401
        r = c.post("/api/query/graphql", params={"project_id": PID},
                   json={"ontology_id": ONT, "query": "query { ontology { id } }"})
        assert r.status_code == 401

        for h in (_b(tok_none),):
            r = c.post("/api/query/sparql", headers=h, params={"project_id": PID},
                       json={"ontology_id": ONT, "query": Q["mixed"]})
            assert r.status_code == 403, r.text
            r = c.get("/api/query/instances", headers=h,
                      params={"project_id": PID, "ontology_id": ONT})
            assert r.status_code == 403
            r = c.post("/api/query/graphql", headers=h, params={"project_id": PID},
                       json={"ontology_id": ONT, "query": "query { ontology { id } }"})
            assert r.status_code == 403

        # 本体默认 private（V2-06）：admin 提升至 department，同租户 read 用户可见
        _ok(c.patch(f"/api/ontologies/{ONT}/sharing", headers=hdr,
                    params={"project_id": PID},
                    json={"sharing": "department"}))

        # 具备 read 权限 → 200
        for method, url, kw in (
            ("post", "/api/query/sparql",
             {"json": {"ontology_id": ONT, "query": Q["mixed"]}}),
            ("get", "/api/query/instances",
             {"params": {"ontology_id": ONT}}),
            ("post", "/api/query/graphql",
             {"json": {"ontology_id": ONT, "query": "query { ontology { id } }"}}),
        ):
            kw = dict(kw)
            merged_params = {"project_id": PID}
            merged_params.update(kw.pop("params", {}))
            r = getattr(c, method)(url, headers=_b(tok_read),
                                   params=merged_params, **kw)
            assert r.status_code == 200, f"{url}: {r.text[:200]}"

        # ---- TC-15-07：上传型内容读权限（默认不可读 → 显式开放后可读） ----
        U3 = "v215-u3"
        _insert(U3, "Order", {"orderCode": "Q15-U3", "amount": 55.5,
                              "note": "secret-note", "customer": "C-1"},
                uploaded_by="v215-up1")
        # 上传者本人可读（upload 标记）
        d = _ok(c.get("/api/query/instances", headers=_b(tok_up1),
                      params={"project_id": PID, "ontology_id": ONT,
                              "filter": json.dumps({"orderCode": "Q15-U3"})}))
        assert d["total"] == 1
        assert d["items"][0]["instance"].get("amount") == 55.5
        assert d["items"][0]["_field_sources"]["amount"] == "upload"
        # 其他查看者默认不可读 → 仅快照＋限制标记
        d = _ok(c.get("/api/query/instances", headers=_b(tok_up2),
                      params={"project_id": PID, "ontology_id": ONT,
                              "filter": json.dumps({"orderCode": "Q15-U3"})}))
        it = d["items"][0]
        assert it["_content_restricted"] is True
        assert "amount" not in it["instance"] and "note" not in it["instance"]
        assert it["_field_sources"] == {"orderCode": "snapshot", "customer": "snapshot"}
        # 上传者显式开放给 up2 → 可读（upload 标记）
        _ok(c.post(f"/api/ingest/instances/{U3}/content-open", headers=_b(tok_up1),
                   params={"project_id": PID},
                   json={"users": ["v215-up2"]}))
        d = _ok(c.get("/api/query/instances", headers=_b(tok_up2),
                      params={"project_id": PID, "ontology_id": ONT,
                              "filter": json.dumps({"orderCode": "Q15-U3"})}))
        it = d["items"][0]
        assert it["instance"].get("amount") == 55.5
        assert it["_field_sources"]["note"] == "upload"
        assert "_content_restricted" not in it
    finally:
        os.environ["ONTOLOGY_AUTH_ENABLED"] = "false"
