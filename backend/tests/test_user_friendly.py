"""用户友好建模 + 逆向生成测试（A+C+文档逆向）。"""
from fastapi.testclient import TestClient

from main import app

c = TestClient(app)


def test_from_text_fallback():
    r = c.post("/api/ontologies/from-text", json={"text": "客户可以下多个订单，每个订单包含若干商品，商品有SKU和库存。"})
    assert r.status_code == 201
    data = r.json()["data"]
    assert data["status"] == "draft"
    assert any(o["name"] == "客户" for o in data["objects"])
    assert any(o["name"] == "订单" for o in data["objects"])


def test_from_excel_csv():
    body = """对象,属性,类型,必填,关系目标
客户,名称,string,是,
订单,订单号,string,是,
订单,,,,客户
"""
    files = {"file": ("model.csv", body.encode("utf-8"), "text/csv")}
    r = c.post("/api/ontologies/from-excel", files=files)
    assert r.status_code == 201
    data = r.json()["data"]
    assert data["status"] == "draft"
    names = [o["name"] for o in data["objects"]]
    assert "客户" in names
    assert "订单" in names


def test_from_doc_async():
    files = {"file": ("prd.txt", "客户可以下多个订单。".encode("utf-8"), "text/plain")}
    r = c.post("/api/ontologies/from-doc", files=files)
    assert r.status_code == 202
    job_id = r.json()["data"]["job_id"]

    import time
    for _ in range(20):
        r2 = c.get(f"/api/ontologies/jobs/{job_id}")
        if r2.json()["data"]["status"] != "pending":
            break
        time.sleep(0.5)
    assert r2.json()["data"]["status"] == "done"
    assert r2.json()["data"]["draft_id"]


def test_publish_draft():
    draft = c.post("/api/ontologies/from-text", json={"text": "商品有SKU和库存。"}).json()["data"]
    assert draft["status"] == "draft"
    r = c.post(f"/api/ontologies/{draft['id']}/publish")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "active"


def test_publish_only_draft():
    active = c.post("/api/ontologies", json={"name": "直接创建", "objects": [], "constraints": []}).json()["data"]
    r = c.post(f"/api/ontologies/{active['id']}/publish")
    assert r.status_code == 422
