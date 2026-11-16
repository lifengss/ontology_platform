"""HF-13：批量入库（approve）去重扫描 O(n²) 修复回归。

复现（hotfix §14.8）：_approve_one 对候选内每一行都调 _match_by_stable_key →
instance_store.find(limit=None) 整对象全表扫描。批量 1000 行单批 >180s 超时，
且随对象增长线性恶化；万级导入（D1 122,500 行）不可行。

修复：
- _approve_one 落库前一次性读取该对象类型全部记录，构建
  (ontology_id, stable_key, str(stable_key_value)) 内存索引，逐行 O(1) 命中；
  本批新增的带主键记录同步进索引（保持批次内重复收敛语义）。
- _write_file 的 os.replace 退避重试增至 5 次（应付文件锁偶发 WinError）。
- _read_file 已对损坏 JSONL 行容错跳过（写回时顺带清理）。

覆盖：
1. 去重更新：同 stable_key 二次 approve → dedup=1 / inserted=0，实例合并保留原 id。
2. 批次内重复收敛：单候选两条相同 stable_key → 只有 1 条入库。
3. 无主键元数据存量不参与匹配（不被静默改写）。
4. 索引函数 O(1) 命中等价于原全表扫描语义。
5. 损坏 JSONL 行读取容错 + 写回清理。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from api.router_ingest import (  # noqa: E402
    _build_stable_key_index,
    _match_by_stable_key,
    _stable_key_tuple,
)
from core.instance_store import JsonlInstanceStore  # noqa: E402
from main import app  # noqa: E402

PID = "hf13-approve-dedup"
P = {"project_id": PID}
c = TestClient(app)
_ctx: dict = {}


def _ontology() -> dict:
    return {
        "name": "HF-13 去重本体",
        "description": "approve 去重扫描 O(n²) 修复验证",
        "modeling_type": "manual",
        "objects": [
            {
                "id": "obj_item",
                "name": "物料",
                "business_definition": "物料主档",
                "stable_key": "attr_code",
                "attributes": [
                    {"id": "attr_code", "name": "编码", "data_type": "string",
                     "required": True, "constraints": {}},
                    {"id": "attr_name", "name": "名称", "data_type": "string",
                     "required": True, "constraints": {}},
                    {"id": "attr_qty", "name": "数量", "data_type": "float",
                     "required": False, "constraints": {}},
                ],
                "relations": [],
            },
        ],
        "constraints": [],
    }


def _preview(oid: str, records: list[dict]) -> str:
    """field_map 预览建候选，返回 candidate_id。"""
    r = c.post("/api/ingest/preview", params=P, json={
        "ontology_id": oid,
        "object_type": "obj_item",
        "source": {"kind": "field_map", "records": records},
        "mapping": {"field_map": {"attr_code": "attr_code", "attr_name": "attr_name",
                                  "attr_qty": "attr_qty"}},
    })
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


class TestHF13ApproveDedup:
    def test_01_setup(self) -> None:
        r = c.post("/api/ontologies", params=P, json=_ontology())
        assert r.status_code == 201, r.text
        _ctx["oid"] = r.json()["data"]["id"]

    def test_02_dedup_update_same_stable_key(self) -> None:
        """同一 stable_key 二次 approve：第二次命中去重 → update 而非追加。"""
        oid = _ctx["oid"]
        cid1 = _preview(oid, [{"attr_code": "C-A", "attr_name": "A-旧", "attr_qty": "1.0"}])
        r = c.post(f"/api/ingest/candidates/{cid1}/approve", params=P)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["imported"] == 1
        assert r.json()["data"]["inserted"] == 1
        assert r.json()["data"]["dedup_updated"] == 0

        # 同 key 不同内容
        cid2 = _preview(oid, [{"attr_code": "C-A", "attr_name": "A-新", "attr_qty": "2.0"}])
        r = c.post(f"/api/ingest/candidates/{cid2}/approve", params=P)
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["imported"] == 1
        assert d["inserted"] == 0, d
        assert d["dedup_updated"] == 1, d

        # 实例被合并（取新值），而非追加第二条
        r = c.get("/api/ingest/instances", params={**P, "ontology_id": oid})
        assert r.status_code == 200, r.text
        items = r.json()["data"]["items"]
        got = [it for it in items if it.get("instance", {}).get("attr_code") == "C-A"]
        assert len(got) == 1, (len(got), items)
        assert got[0]["instance"].get("attr_name") == "A-新"

    def test_03_batch_internal_duplicate_converges(self) -> None:
        """单候选内两条相同 stable_key：只入库 1 条（批次内重复收敛）。"""
        oid = _ctx["oid"]
        # 用全新 key，避免与 test_02 命中（test_02 已入库 C-A）
        cid = _preview(oid, [
            {"attr_code": "C-B", "attr_name": "B-1", "attr_qty": "1.0"},
            {"attr_code": "C-B", "attr_name": "B-2", "attr_qty": "2.0"},
        ])
        r = c.post(f"/api/ingest/candidates/{cid}/approve", params=P)
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["inserted"] == 1, d
        assert d["dedup_updated"] == 1, d

        r = c.get("/api/ingest/instances", params={**P, "ontology_id": oid})
        items = r.json()["data"]["items"]
        got = [it for it in items if it.get("instance", {}).get("attr_code") == "C-B"]
        assert len(got) == 1, (len(got), items)

    def test_04_index_lookup_equivalent_to_full_scan(self) -> None:
        """索引命中语义 = 原 _match_by_stable_key 全表扫描（含 ontology_id 归一/str 化比较）。"""
        store = JsonlInstanceStore(project_id="hf13-index-unit")
        recs = [
            {"id": "r1", "ontology_id": "o1", "object_type": "obj_item",
             "stable_key": "attr_code", "stable_key_value": "K1",
             "instance": {"attr_code": "K1", "attr_name": "n1"}},
            {"id": "r2", "ontology_id": "o1", "object_type": "obj_item",
             "stable_key": "attr_code", "stable_key_value": 2,  # int vs str
             "instance": {"attr_code": 2, "attr_name": "n2"}},
            # 无主键元数据的存量：不参与匹配
            {"id": "r3", "ontology_id": "o1", "object_type": "obj_item",
             "instance": {"attr_code": "K3", "attr_name": "n3"}},
        ]
        store.insert(recs[0])
        store.insert(recs[1])
        store.insert(recs[2])

        idx = _build_stable_key_index(store, "o1", "obj_item", "attr_code")
        # int 2 与 str "2" 命中（str 化比较）
        assert _match_by_stable_key(store, "o1", "obj_item", "attr_code", "2", index=idx)["id"] == "r2"
        # 存量缺 ontology_id 归一为当前 o1
        assert _match_by_stable_key(store, "o1", "obj_item", "attr_code", "K1", index=idx)["id"] == "r1"
        # 无主键元数据（r3）不入索引 → 不命中
        assert _match_by_stable_key(store, "o1", "obj_item", "attr_code", "K3", index=idx) is None
        # 与全表扫描回退结果一致
        full = _match_by_stable_key(store, "o1", "obj_item", "attr_code", "K1")
        assert full["id"] == "r1"

    def test_05_corrupt_json_line_tolerated(self) -> None:
        """损坏 JSONL 行（并发写撕裂残行）读取容错，写回不崩溃且顺带清理。"""
        store = JsonlInstanceStore(project_id="hf13-corrupt")
        obj_file = store._path_for("obj_item")
        good = json.dumps({"id": "ok1", "object_type": "obj_item",
                           "instance": {"attr_code": "K-Ok", "attr_name": "ok"}},
                          ensure_ascii=False)
        # 模拟并发 append 撕裂出的残行（00"} / +00:00"} 之类截断）
        with open(obj_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(good + "\n")
            f.write('{"id":"bad1","object_type":"obj_item","instance":{"attr_code":"x00"} \n')
            f.write('{"id":"bad2","object_type":"obj_item","instance":{"attr_code":"x",'+ "00\"}\n")
        records = store.find(object_type="obj_item", limit=None)
        assert len(records) == 1, records  # 仅好行被读入，坏行被跳过

        # 触发写回（update）→ 坏行被过滤后写回（顺带清理）
        store.update("ok1", {"instance": {"attr_code": "K-Ok", "attr_name": "ok-updated"}})
        cleaned = store.find(object_type="obj_item", limit=None)
        assert len(cleaned) == 1
        assert cleaned[0]["instance"]["attr_name"] == "ok-updated"
        # 磁盘上不再有坏行
        text = obj_file.read_text(encoding="utf-8")
        assert "bad1" not in text and "bad2" not in text
