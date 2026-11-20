"""GAP-14 JSON 实例存储文件级缓存 — 读加速与一致性保证。

- 缓存挂在模块级（get_instance_store 每次新建实例，实例级缓存会失效）；
- 失效：mtime_ns/size 变化（含外部写入）＋ 写侧主动失效；
- 读侧返回浅拷贝：调用方变更不污染缓存；写后重读立即反映最新数据。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _authenv import auth_module_guard
import main as m
from core.instance_store import JsonlInstanceStore, get_instance_store
from fastapi.testclient import TestClient

_guard = auth_module_guard()
app = m.app
client = TestClient(app)


class TestFileCache:
    def test_cache_hit_and_consistency(self, tmp_path):
        """同数据两次读：缓存命中且内容一致；跨 store 实例（get_instance_store 每次新建）也命中。"""
        s1 = JsonlInstanceStore(project_id="g14", data_dir=str(tmp_path))
        s1.insert({"object_type": "obj_a", "instance": {"id": "a1", "name": "n1"}})
        s1.insert({"object_type": "obj_a", "instance": {"id": "a2", "name": "n2"}})
        assert s1.count("obj_a") == 2
        r1 = s1.find(object_type="obj_a", limit=None)
        # 新 store 实例（模拟每次 get_instance_store 新建）第二次读 → 缓存命中
        s2 = JsonlInstanceStore(project_id="g14", data_dir=str(tmp_path))
        r2 = s2.find(object_type="obj_a", limit=None)
        assert [x["id"] for x in r2] == [x["id"] for x in r1]
        from core.instance_store import _FILE_CACHE
        assert any(k.endswith("obj_a.jsonl") for k in _FILE_CACHE)

    def test_external_write_invalidates(self, tmp_path):
        """外部直写文件（模拟他进程）→ mtime/size 变化 → 下次读到新数据。"""
        s = JsonlInstanceStore(project_id="g14x", data_dir=str(tmp_path))
        s.insert({"object_type": "obj_b", "instance": {"id": "b1"}})
        assert s.count("obj_b") == 1
        time.sleep(0.02)
        # 绕过 store 直写文件（外部写入）
        import json as _json
        p = tmp_path / "projects" / "g14x" / "instances" / "obj_b.jsonl"
        rec = {"id": "b2", "object_type": "obj_b", "instance": {"id": "b2"}}
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        assert s.count("obj_b") == 2  # 缓存失效，重新加载

    def test_mutation_isolation(self, tmp_path):
        """读返回浅拷贝：调用方修改记录不影响后续读（缓存不被污染）。"""
        s = JsonlInstanceStore(project_id="g14m", data_dir=str(tmp_path))
        s.insert({"object_type": "obj_c", "instance": {"id": "c1", "name": "原值"}})
        rec = s.find(object_type="obj_c", limit=None)[0]
        rec["instance"]["name"] = "被篡改"  # 嵌套 dict 就地改
        rec2 = s.find(object_type="obj_c", limit=None)[0]
        assert rec2["instance"]["name"] == "原值"  # 缓存原数据未受影响

    def test_update_delete_reflect(self, tmp_path):
        """update/delete 走缓存路径后，重读立即反映最新状态。"""
        s = JsonlInstanceStore(project_id="g14u", data_dir=str(tmp_path))
        rec = s.insert({"object_type": "obj_d", "instance": {"id": "d1", "name": "v1"}})
        rid = rec["id"]
        assert s.find(object_type="obj_d", limit=None)[0]["instance"]["name"] == "v1"
        s.update(rid, {"instance": {"id": "d1", "name": "v2"}})
        assert s.find(object_type="obj_d", limit=None)[0]["instance"]["name"] == "v2"
        assert s.delete(rid) is True
        assert s.find(object_type="obj_d", limit=None) == []
        assert s.count("obj_d") == 0

    def test_api_listing_consistent(self):
        """平台实例查询接口（经缓存路径）列表与计数一致。"""
        from core.instance_store import get_instance_store
        r = client.post("/api/projects", json={"name": "g14-api", "description": ""})
        pid = r.json()["data"]["id"]
        store = get_instance_store(pid)
        for i in range(5):
            store.insert({"ontology_id": "oX", "object_type": "obj_e", "instance": {"id": f"e{i}"}})
        j = client.get("/api/ingest/instances", params={"project_id": pid, "object_type": "obj_e", "pageSize": 50}).json()
        assert j["data"]["total"] == 5 and len(j["data"]["items"]) == 5


def teardown_module():
    try:
        r = client.get("/api/projects", params={"pageSize": 100})
        for it in r.json()["data"]["items"]:
            if it.get("name") == "g14-api":
                client.delete(f"/api/projects/{it['id']}")
    except Exception:
        pass
