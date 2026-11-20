"""GAP-13 示例项目一条龙 — 空项目一键预置示例本体＋示例实例。

- POST /api/projects/{id}/seed-example：复用内置约束示范模板创建 draft 本体＋每对象 3 条确定性实例；
- 幂等保护：非空项目 409 EXAMPLE_EXISTS；归档项目 400；未知项目 404；
- 实例经 InstanceStore 可查、属性值落在约束区间内（enum/pattern/数值/长度）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _authenv import auth_module_guard
import main as m
from fastapi.testclient import TestClient

_guard = auth_module_guard()  # 本模块无鉴权用例，保持鉴权关
app = m.app
client = TestClient(app)


def _new_project() -> str:
    r = client.post("/api/projects", json={"name": f"示例一条龙-{int(time.time() * 1000)}", "description": ""})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _seed(pid: str):
    return client.post(f"/api/projects/{pid}/seed-example")


class TestSeedExample:
    def test_seed_empty_project(self):
        """空项目 seed：201，返回本体＋实例计数；本体落库为 draft；实例可查且值合法。"""
        pid = _new_project()
        r = _seed(pid)
        assert r.status_code == 201, r.text
        d = r.json()["data"]
        assert d["objects"] >= 2 and d["instances"] >= 6
        assert d["template_id"] == "tpl_order_sample"
        # 本体存在且为 draft
        o = client.get(f"/api/ontologies/{d['ontology_id']}", params={"project_id": pid}).json()["data"]
        assert o["status"] == "draft" and len(o["objects"]) >= 2
        # 实例经平台实例查询接口可查（object_type 维度）
        r2 = client.get("/api/ingest/instances", params={"project_id": pid, "pageSize": 100})
        assert r2.status_code == 200
        items = r2.json()["data"]["items"]
        by_type = {}
        for it in items:
            by_type.setdefault(it.get("object_type"), 0)
            by_type[it.get("object_type")] += 1
        assert by_type.get("obj_order") == 3 and by_type.get("obj_customer") == 3
        # 约束合法性抽查：pattern/enum/数值区间
        order = next(it for it in items if it.get("object_type") == "obj_order")
        inst = order["instance"]
        import re
        assert re.match(r"^[A-Z]{2}[0-9]{8,12}$", inst["attr_order_no"])
        assert inst["attr_status"] in ["待付款", "已付款", "已发货", "已完成", "已取消"]
        assert 0 <= inst["attr_amount"] <= 99999999 and 1 <= inst["attr_qty"] <= 10000

    def test_seed_twice_conflict_409(self):
        """重复 seed → 409 EXAMPLE_EXISTS（示例数据仅向空项目提供）。"""
        pid = _new_project()
        assert _seed(pid).status_code == 201
        r = _seed(pid)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "EXAMPLE_EXISTS"

    def test_seed_unknown_project_404(self):
        r = _seed("ghost_project")
        assert r.status_code == 404

    def test_seed_archived_400(self):
        pid = _new_project()
        assert client.post(f"/api/projects/{pid}/archive").status_code == 200
        r = _seed(pid)
        assert r.status_code == 400

    def test_seed_project_isolation(self):
        """seed 只写入目标项目：他项目查不到该本体/实例。"""
        pid = _new_project()
        d = _seed(pid).json()["data"]
        other = _new_project()
        r = client.get(f"/api/ontologies/{d['ontology_id']}", params={"project_id": other})
        assert r.status_code == 404
        r2 = client.get("/api/ingest/instances", params={"project_id": other, "pageSize": 100})
        items = r2.json()["data"]["items"]
        assert all(it.get("object_type") not in ("obj_order", "obj_customer") or
                   it.get("ontology_id") != d["ontology_id"] for it in items)


def teardown_module():
    """清理测试项目（级联删本体/实例）。"""
    import re
    try:
        r = client.get("/api/projects", params={"pageSize": 100})
        for it in r.json()["data"]["items"]:
            if (it.get("name") or "").startswith("示例一条龙-"):
                client.delete(f"/api/projects/{it['id']}")
    except Exception:
        pass
