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
    data = None
    for _ in range(40):
        r2 = c.get(f"/api/ontologies/jobs/{job_id}")
        data = r2.json()["data"]
        if data["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.5)
    assert data["status"] == "done"
    assert data["draft_id"]
    # GAP-11：进度/阶段字段，完成后 progress 达 100
    assert data["progress"] == 100
    assert data["stage"]


def test_publish_draft():
    draft = c.post("/api/ontologies/from-text", json={"text": "商品有SKU和库存。"}).json()["data"]
    assert draft["status"] == "draft"
    r = c.post(f"/api/ontologies/{draft['id']}/publish")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "active"


def test_publish_only_draft():
    draft = c.post("/api/ontologies", json={"name": "直接创建", "objects": [], "constraints": []}).json()["data"]
    assert draft["status"] == "draft"
    # 第一次发布成功
    r1 = c.post(f"/api/ontologies/{draft['id']}/publish")
    assert r1.status_code == 200
    assert r1.json()["data"]["status"] == "active"
    # 重复发布 active 状态应拒绝
    r2 = c.post(f"/api/ontologies/{draft['id']}/publish")
    assert r2.status_code == 422
