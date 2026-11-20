"""GAP-12 图谱按需邻居查询（大图不全量拉取）— viz 单元＋API 契约。

- 对象节点：关系边无向 BFS（depth 1~2）；knowledge 视图附焦点对象实例抽样。
- 实例节点：所属对象＋同对象其他实例抽样（limit 截断 truncated）。
- 未知节点/本体 404；depth/limit 越界 422；项目隔离（他项目本体不可见）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _authenv import auth_module_guard
import main as m
from core.instance_store import get_instance_store
from fastapi.testclient import TestClient
from viz import Viz

_guard = auth_module_guard()  # 本模块无鉴权用例，保持鉴权关
app = m.app
client = TestClient(app)

PROJ = f"图谱邻居-{int(time.time())}"
PROJ_ID = ""
ONTO_ID = ""


def _obj(oid, name, attrs=(), rels=()):
    return {
        "id": oid, "name": name,
        "attributes": [{"id": a[0], "name": a[1], "data_type": "string"} for a in attrs],
        "relations": [{"id": r[0], "name": r[1], "target_object": r[2]} for r in rels],
    }


def _setup():
    global PROJ_ID, ONTO_ID
    r = client.post("/api/projects", json={"name": PROJ, "description": "GAP-12 测试"})
    assert r.status_code in (200, 201), r.text
    PROJ_ID = r.json()["data"]["id"]
    body = {
        "name": "图谱邻居本体",
        "objects": [
            _obj("obj_order", "订单", [("attr_no", "订单号")], [("rel_order_customer", "所属客户", "obj_customer")]),
            _obj("obj_customer", "客户", [("attr_name", "客户名")], [("rel_customer_item", "购买商品", "obj_item")]),
            _obj("obj_item", "商品", [("attr_title", "商品名")], [("rel_item_order", "belonging", "obj_order")]),
        ],
        "constraints": [], "functions": [],
    }
    r = client.post("/api/ontologies", params={"project_id": PROJ_ID}, json=body)
    assert r.status_code in (200, 201), r.text
    ONTO_ID = r.json()["data"]["id"]
    # 直插实例：订单 5 条、客户 1 条、商品 2 条
    store = get_instance_store(PROJ_ID)
    for i in range(5):
        store.insert({"ontology_id": ONTO_ID, "object_type": "obj_order",
                      "instance": {"id": f"ord_{i}", "name": f"订单{i}"}})
    store.insert({"ontology_id": ONTO_ID, "object_type": "obj_customer",
                  "instance": {"id": "cust_0", "name": "客户A"}})
    for i in range(2):
        store.insert({"ontology_id": ONTO_ID, "object_type": "obj_item",
                      "instance": {"id": f"item_{i}", "name": f"商品{i}"}})


def _ontology_dict():
    return client.get(f"/api/ontologies/{ONTO_ID}", params={"project_id": PROJ_ID}).json()["data"]


_viz = Viz()


class TestVizNeighborsUnit:
    """viz.neighbors 纯逻辑单测（不经 HTTP）。"""

    @classmethod
    def setup_class(cls):
        _setup()
        cls.onto = _ontology_dict()

    def test_object_depth1_undirected(self):
        # 订单 depth=1：客户（order→customer）；商品与订单的边方向是 item→order，无向遍历同样命中
        res = _viz.neighbors(ONTO_ID, self.onto, "obj_order", mode="schema", depth=1)
        ids = {n["id"] for n in res["nodes"]}
        assert ids == {"obj_customer", "obj_item"}
        assert res["node"]["id"] == "obj_order"
        assert res["truncated"] is False
        assert all(e["kind"] == "relation" for e in res["edges"])

    def test_object_depth2_chain(self):
        # depth=2：经客户到达其下游商品（customer→item 方向）
        res = _viz.neighbors(ONTO_ID, self.onto, "obj_order", mode="schema", depth=2)
        ids = {n["id"] for n in res["nodes"]}
        assert ids == {"obj_customer", "obj_item"}

    def test_object_knowledge_instances(self):
        # knowledge：关系邻居对象＋焦点对象的实例（limit=0 不截断，5 条全返回）
        res = _viz.neighbors(ONTO_ID, self.onto, "obj_order", mode="knowledge", project_id=PROJ_ID, depth=1, limit=0)
        kinds = {(n["id"], n["kind"]) for n in res["nodes"]}
        assert ("obj_customer", "object") in kinds and ("obj_item", "object") in kinds
        for i in range(5):
            assert (f"ord_{i}", "instance") in kinds
        assert res["total"] == 5
        # instance_of 边指向焦点对象
        assert all(e["target"] == "obj_order" for e in res["edges"] if e["kind"] == "instance_of")

    def test_object_knowledge_limit_truncated(self):
        # limit=2 → 实例只返回 2 条且 truncated=True；抽样确定性（两次调用一致）
        res = _viz.neighbors(ONTO_ID, self.onto, "obj_order", mode="knowledge", project_id=PROJ_ID, depth=1, limit=2)
        insts = [n["id"] for n in res["nodes"] if n["kind"] == "instance"]
        assert len(insts) == 2 and res["truncated"] is True and res["total"] == 5
        res2 = _viz.neighbors(ONTO_ID, self.onto, "obj_order", mode="knowledge", project_id=PROJ_ID, depth=1, limit=2)
        assert [n["id"] for n in res2["nodes"] if n["kind"] == "instance"] == insts

    def test_instance_neighbors(self):
        # 实例节点：所属对象＋同对象其他实例（不混入其他对象类型）
        res = _viz.neighbors(ONTO_ID, self.onto, "ord_0", mode="knowledge", project_id=PROJ_ID, limit=0)
        assert res["node"]["id"] == "ord_0"
        ids = {n["id"] for n in res["nodes"]}
        assert "obj_order" in ids
        for i in range(1, 5):
            assert f"ord_{i}" in ids
        assert "cust_0" not in ids
        assert any(e["source"] == "ord_0" and e["kind"] == "instance_of" for e in res["edges"])

    def test_unknown_node_returns_none(self):
        assert _viz.neighbors(ONTO_ID, self.onto, "no_such_node", mode="schema") is None


class TestNeighborsAPI:
    """HTTP 契约：错误路径、参数校验与项目隔离。"""

    def test_unknown_node_404(self):
        r = client.get(f"/api/graph/{ONTO_ID}/neighbors", params={"project_id": PROJ_ID, "node_id": "ghost"})
        assert r.status_code == 404

    def test_unknown_ontology_404(self):
        r = client.get("/api/graph/ghost_onto/neighbors", params={"project_id": PROJ_ID, "node_id": "obj_order"})
        assert r.status_code == 404

    def test_project_isolation(self):
        # 本体按项目存储：他项目查询同一本体 id → 404（不泄露存在性）
        r = client.post("/api/projects", json={"name": PROJ + "-隔离", "description": ""})
        other_id = r.json()["data"]["id"]
        r = client.get(f"/api/graph/{ONTO_ID}/neighbors", params={"project_id": other_id, "node_id": "obj_order"})
        assert r.status_code == 404

    def test_depth_out_of_range_422(self):
        r = client.get(f"/api/graph/{ONTO_ID}/neighbors",
                       params={"project_id": PROJ_ID, "node_id": "obj_order", "depth": 3})
        assert r.status_code == 422

    def test_limit_out_of_range_422(self):
        r = client.get(f"/api/graph/{ONTO_ID}/neighbors",
                       params={"project_id": PROJ_ID, "node_id": "obj_order", "limit": 500})
        assert r.status_code == 422

    def test_success_envelope(self):
        r = client.get(f"/api/graph/{ONTO_ID}/neighbors",
                       params={"project_id": PROJ_ID, "node_id": "obj_order", "mode": "schema"})
        assert r.status_code == 200
        j = r.json()
        assert j["success"] is True
        d = j["data"]
        assert d["node"]["id"] == "obj_order"
        assert {"nodes", "edges", "truncated", "total"} <= set(d.keys())


def teardown_module():
    """清理测试项目（级联删本体/实例数据）。"""
    try:
        if PROJ_ID:
            client.delete(f"/api/projects/{PROJ_ID}")
    except Exception:
        pass
