"""部门空间（namespace）管理测试：CRUD、默认空间保护、非空禁删、旧数据自动回填。"""
from fastapi.testclient import TestClient

from main import app

c = TestClient(app)


def _mk_project() -> str:
    r = c.post("/api/projects", json={"name": "ns-test"})
    assert r.status_code in (200, 201)
    return r.json()["data"]["id"]


def _onto_body() -> dict:
    return {
        "name": "空间测试本体",
        "objects": [
            {
                "id": "obj_t",
                "stable_key": "attr_t",
                "business_definition": "空间测试对象",
                "name": "测试对象",
                "attributes": [{"id": "attr_t", "name": "名称", "data_type": "string", "required": True, "constraints": {}}],
                "relations": [],
            },
        ],
        "constraints": [],
    }


def test_namespace_crud_lifecycle():
    pid = _mk_project()
    try:
        # 列表：初始仅默认空间
        r = c.get("/api/namespaces", params={"project_id": pid})
        assert r.status_code == 200
        items = r.json()["data"]["items"]
        assert [i["id"] for i in items] == [pid]
        assert items[0]["is_default"] is True
        assert items[0]["name"] == "默认空间"
        assert items[0]["ontology_count"] == 0

        # 新建（自定义 ID）
        r = c.post("/api/namespaces", params={"project_id": pid},
                   json={"id": "sales", "name": "销售部", "description": "CRM 域"})
        assert r.status_code == 201
        assert r.json()["data"]["id"] == "sales"
        assert r.json()["data"]["name"] == "销售部"
        assert r.json()["data"]["is_default"] is False

        # 冲突 / 非法 ID / 占用默认 ID（hex 项目 ID 可能数字开头，先被格式校验拦为 400）
        assert c.post("/api/namespaces", params={"project_id": pid},
                      json={"id": "sales", "name": "x"}).status_code == 409
        assert c.post("/api/namespaces", params={"project_id": pid},
                      json={"id": "1bad", "name": "x"}).status_code == 400
        assert c.post("/api/namespaces", params={"project_id": pid},
                      json={"id": pid, "name": "x"}).status_code in (400, 409)

        # 重命名（仅显示名，ID 不变）
        r = c.put("/api/namespaces/sales", params={"project_id": pid}, json={"name": "销售部(改)"})
        assert r.status_code == 200
        assert r.json()["data"]["id"] == "sales"
        assert r.json()["data"]["name"] == "销售部(改)"

        # 默认空间保护：不可改、不可删
        assert c.put(f"/api/namespaces/{pid}", params={"project_id": pid}, json={"name": "x"}).status_code == 409
        assert c.delete(f"/api/namespaces/{pid}", params={"project_id": pid}).status_code == 409

        # 空空间可删；不存在 404
        assert c.delete("/api/namespaces/sales", params={"project_id": pid}).status_code == 200
        assert c.delete("/api/namespaces/sales", params={"project_id": pid}).status_code == 404
    finally:
        c.delete("/api/projects/" + pid)


def test_namespace_not_empty_and_backfill():
    pid = _mk_project()
    try:
        # 旧数据回填：不注册直接建带 namespace 的本体，列表应自动补录（derived）
        r = c.post("/api/ontologies", params={"project_id": pid, "namespace": "legacy"}, json=_onto_body())
        assert r.status_code == 201
        r = c.get("/api/namespaces", params={"project_id": pid})
        by_id = {i["id"]: i for i in r.json()["data"]["items"]}
        assert "legacy" in by_id
        assert by_id["legacy"]["derived"] is True
        assert by_id["legacy"]["ontology_count"] == 1

        # 非空空间禁删
        r = c.delete("/api/namespaces/legacy", params={"project_id": pid})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "NAMESPACE_NOT_EMPTY"
        assert r.json()["error"]["details"]["ontology_count"] == 1
    finally:
        c.delete("/api/projects/" + pid)
