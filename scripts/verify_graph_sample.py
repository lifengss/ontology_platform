# -*- coding: utf-8 -*-
"""验证 Viz._sample_instances 均匀抽样 + graph_data 抽样元信息（合成数据，不依赖服务）。"""
import sys, types
from types import SimpleNamespace
from collections import Counter

# stub 掉 config / logging_setup / core.instance_store，避免导入真配置
_fake_store_records = []
sys.modules["config"] = SimpleNamespace(get_settings=lambda: SimpleNamespace(data_dir="d:/tmp/nonexist"))
sys.modules["logging_setup"] = SimpleNamespace(get_logger=lambda name: types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None, debug=lambda *a, **k: None))
sys.modules["core"] = types.ModuleType("core")
sys.modules["core.instance_store"] = SimpleNamespace(
    get_instance_store=lambda pid=None: SimpleNamespace(all=lambda: _fake_store_records))

sys.path.insert(0, "d:/self_coding/ontology_platform/backend")
from viz import Viz

v = Viz()
ok = True

def check(name, cond, detail=""):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name + (" " + detail if detail else ""))
    if not cond:
        ok = False

def mk(obj, n):
    return [{"object_type": obj, "instance": {"id": f"{obj}_{i}"}, "tx_id": f"tx_{obj}_{i}"} for i in range(n)]

# 案例 1：3 对象（1372/500/425），max_total=300, per_object=50 → 每桶顶到 50 上限，共 150
data = mk("after_sales", 1372) + mk("goods", 500) + mk("order", 425)
got = v._sample_instances(data, 300)
c = Counter(r["object_type"] for r in got)
check("case1 总数=150（受 per_object 上限约束）", len(got) == 150, f"got={len(got)}")
check("case1 每对象=50", c.get("after_sales") == 50 and c.get("goods") == 50 and c.get("order") == 50, str(dict(c)))

# 案例 2：17 对象（模拟真实企业本体，规模悬殊），max_total=300, per=50 → 300 条、均匀
objs = [f"obj_{i:02d}" for i in range(17)]
data2 = []
for i, o in enumerate(objs):
    data2 += mk(o, [1372, 244, 198, 144, 93, 53, 40, 30, 20, 10, 6, 3, 2, 1, 1, 1, 1][i])
got2 = v._sample_instances(data2, 300)
c2 = Counter(r["object_type"] for r in got2)
check("case2 总数=300", len(got2) == 300, f"got={len(got2)}")
check("case2 无对象超过 50", all(n <= 50 for n in c2.values()), str(dict(c2)))
check("case2 17 对象均有代表", len(c2) == 17, f"covered={len(c2)}")
# 均匀性：大对象（总量 >= 12）应分得 >= 12；小对象全量入图（取其全部）即正确
total_by_obj = Counter(r["object_type"] for r in data2)
check("case2 均匀（大对象 >= 12，小对象全量）",
      all((total_by_obj[o] < 12) or (c2[o] >= 12) for o in c2),
      str(sorted(c2.values())))

# 案例 3：确定性（同输入两次结果一致）
check("case3 确定性", got2 == v._sample_instances(data2, 300))

# 案例 4：graph_data 元信息（knowledge 模式，max_instances=10）
_onto = {"id": "o1", "name": "测试本体", "objects": [{"id": "obj_a", "name": "对象A", "parent": None, "relations": []}]}
_fake_store_records[:] = [dict(r, ontology_id="o1") for r in mk("obj_a", 100)]
r = v.graph_data("o1", _onto, mode="knowledge", project_id="p1", max_instances=10)
check("case4 truncated=true", r["truncated"] is True)
check("case4 total_instances=100", r["total_instances"] == 100, str(r["total_instances"]))
check("case4 shown=10", r["shown"] == 10, str(r["shown"]))
inst_nodes = [n for n in r["nodes"] if n["kind"] == "instance"]
check("case4 实例节点=10", len(inst_nodes) == 10, f"got={len(inst_nodes)}")
check("case4 tabs.knowledge 含 meta", r["tabs"]["knowledge"]["shown"] == 10 and r["tabs"]["knowledge"]["truncated"] is True)

# 案例 5：max_instances=0（全部）→ 不抽样
_fake_store_records[:] = [dict(r, ontology_id="o1") for r in mk("obj_a", 100)]
r2 = v.graph_data("o1", _onto, mode="knowledge", project_id="p1", max_instances=0)
check("case5 全部：truncated=false", r2["truncated"] is False)
check("case5 全部：shown=100", r2["shown"] == 100, str(r2["shown"]))

# 案例 6：实例数未超上限 → 不抽样
_fake_store_records[:] = [dict(r, ontology_id="o1") for r in mk("obj_a", 100)]
r3 = v.graph_data("o1", _onto, mode="knowledge", project_id="p1", max_instances=300)
check("case6 未超限：truncated=false, shown=100", r3["truncated"] is False and r3["shown"] == 100)

print("RESULT:", "ALL_PASS" if ok else "HAS_FAILURE")
sys.exit(0 if ok else 1)
