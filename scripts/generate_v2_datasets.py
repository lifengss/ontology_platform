# -*- coding: utf-8 -*-
"""V2 测试数据集生成器（VS2-00，确定性，seed=42 可复跑）。

产出目录：data/datasets/v2/
  1. maintenance_cost_2297.xlsx + maintenance_cost_ontology.ttl
     —— 维保成本本体 2297 条实例（上传型）＋本体定义；
        Excel 参照＝仓库根 成本清单_维保服务产品_复算版.xlsx（TC-04-01 基线）。
  2. dirty_test_ontology.ttl + dirty_batch.xlsx + dirty_manifest.json
     —— V2-21 脏数据批（缺键/重复键/越枚举/越界/悬空关系）。
  3. supplier_alignment_{a,b}_ontology.ttl + supplier_alignment_{a,b}_instances.xlsx
     + supplier_alignment_manifest.json
     —— 双本体"供应商"对齐集（1200/1380，重叠 1000）。
  4. benchmark_graph.ttl + benchmark_manifest.json
     —— 基准规模推理/校验集（数千类/属性、数万实例三元组＋SHACL）。

用法：
  python scripts/generate_v2_datasets.py [--out data/datasets/v2]
                                         [--runtime-root backend/data/projects]
                                         [--runtime-project 5cd5ccc06b604ed7829f99983f52e963]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from typing import Any, Dict, List

import openpyxl

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO, "data", "datasets", "v2")
DEFAULT_RUNTIME_PROJECT = "5cd5ccc06b604ed7829f99983f52e963"
SEED = 42

# ---------------------------------------------------------------------------
# 1) 维保成本 2297 条（运行期 JSONL -> 多 Sheet Excel）
# ---------------------------------------------------------------------------

OBJ_ORDER = [
    "obj_maintenance_product",
    "obj_aftersales_labor_baseline",
    "obj_spare_aux_baseline",
    "obj_mgmt_cost_baseline",
    "obj_onsite_rate_baseline",
    "obj_sla_coefficient_baseline",
    "obj_person_day_baseline",
    "obj_impl_labor_baseline",
    "obj_resident_cost_baseline",
    "obj_travel_cost_baseline",
    "obj_part_failure_baseline",
    "obj_material",
    "obj_machine_model",
    "obj_product_line",
    "obj_region",
    "obj_customer",
    "obj_exchange_rate",
]


def gen_maintenance(out_dir: str, runtime_root: str, project_id: str) -> Dict[str, Any]:
    inst_dir = os.path.join(runtime_root, project_id, "instances")
    ttl_src = os.path.join(runtime_root, project_id,
                           "ontology_1f2ea950c2914a69a994da880e676531.ttl")
    ttl_dst = os.path.join(out_dir, "maintenance_cost_ontology.ttl")
    with open(ttl_src, "r", encoding="utf-8") as f:
        shutil_copy = f.read()
    with open(ttl_dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(shutil_copy)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    per_obj: Dict[str, int] = {}
    objs = [o for o in OBJ_ORDER
            if os.path.exists(os.path.join(inst_dir, o + ".jsonl"))]
    for obj in objs:
        rows: List[Dict[str, Any]] = []
        with open(os.path.join(inst_dir, obj + ".jsonl"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                inst = json.loads(line)["instance"]
                rows.append({k: v for k, v in inst.items()
                             if k.startswith("attr_")})
        per_obj[obj] = len(rows)
        # 列序：首现顺序（确定性）
        cols: List[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        ws = wb.create_sheet(obj[:31])
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c) for c in cols])
    xlsx = os.path.join(out_dir, "maintenance_cost_2297.xlsx")
    wb.save(xlsx)
    return {
        "total": sum(per_obj.values()),
        "per_object": per_obj,
        "xlsx": os.path.relpath(xlsx, REPO),
        "ontology_ttl": os.path.relpath(ttl_dst, REPO),
        "excel_reference": "成本清单_维保服务产品_复算版.xlsx（仓库根）",
    }


# ---------------------------------------------------------------------------
# 2) 脏数据批（V2-21：缺键/重复键/越枚举/越界/悬空关系）
# ---------------------------------------------------------------------------

DIRTY_TTL = """@prefix ont: <http://ontology.local/dirty/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ont:obj_test_item a owl:Class ;
    rdfs:label "测试物料" ;
    rdfs:comment "V2-21 数据健康度脏数据批测试对象" ;
    rdfs:subClassOf owl:Thing .

ont:attr_item_code a owl:DatatypeProperty ;
    rdfs:label "物料编码" ;
    rdfs:domain ont:obj_test_item ;
    rdfs:range xsd:string .

ont:attr_item_name a owl:DatatypeProperty ;
    rdfs:label "物料名称" ;
    rdfs:domain ont:obj_test_item ;
    rdfs:range xsd:string .

ont:attr_category a owl:DatatypeProperty ;
    rdfs:label "分类" ;
    rdfs:domain ont:obj_test_item ;
    rdfs:range xsd:string .

ont:attr_quantity a owl:DatatypeProperty ;
    rdfs:label "数量" ;
    rdfs:domain ont:obj_test_item ;
    rdfs:range xsd:integer .

ont:attr_unit_price a owl:DatatypeProperty ;
    rdfs:label "单价" ;
    rdfs:domain ont:obj_test_item ;
    rdfs:range xsd:float .

ont:attr_parent_code a owl:DatatypeProperty ;
    rdfs:label "父项编码" ;
    rdfs:domain ont:obj_test_item ;
    rdfs:range xsd:string .

ont:shape_test_item a sh:NodeShape ;
    sh:targetClass ont:obj_test_item ;
    sh:property [ sh:path ont:attr_item_code ; sh:minCount 1 ; sh:pattern "^ITM-\\\\d{5}$" ] ;
    sh:property [ sh:path ont:attr_category ; sh:in ( "A" "B" "C" ) ] ;
    sh:property [ sh:path ont:attr_quantity ; sh:min 0 ; sh:max 1000000 ] ;
    sh:property [ sh:path ont:attr_unit_price ; sh:min 0 ] .
"""

CITIES5 = ["A", "B", "C"]


def gen_dirty(out_dir: str) -> Dict[str, Any]:
    rng = random.Random(SEED)
    n = 200
    rows: List[Dict[str, Any]] = []
    dirt: Dict[str, List[int]] = {
        "missing_key": [], "duplicate_key": [], "enum_violation": [],
        "out_of_range": [], "dangling_relation": [],
    }
    for i in range(1, n + 1):
        rows.append({
            "item_code": f"ITM-{i:05d}",
            "item_name": f"物料{i:05d}",
            "category": rng.choice(CITIES5),
            "quantity": rng.randint(1, 1000),
            "unit_price": round(rng.uniform(0.5, 999.9), 2),
            "parent_code": None,
        })
    # 缺键 10 条（行 10,30,...,190 位置 => 行号 11..191 含表头偏移；此处按数据行号记录）
    for j, idx in enumerate(range(9, n, 20)):  # 数据行 10,30,50,...,190
        rows[idx]["item_code"] = None
        dirt["missing_key"].append(idx + 1)
    # 重复键 8 条：把行 181..188 的编码改成前面已存在的编码
    dup_targets = [101, 102, 103, 104, 105, 106, 107, 108]
    for idx, tgt in zip(range(180, 188), dup_targets):
        rows[idx]["item_code"] = f"ITM-{tgt:05d}"
        dirt["duplicate_key"].append(idx + 1)
    # 越枚举 10 条：行 161..170 的 category 改为非法值
    for idx in range(160, 170):
        rows[idx]["category"] = "X"
        dirt["enum_violation"].append(idx + 1)
    # 越界 10 条：行 141..150 的 quantity 负数或超上限
    for idx in range(140, 150):
        rows[idx]["quantity"] = -5 if idx % 2 == 0 else 2000000
        dirt["out_of_range"].append(idx + 1)
    # 悬空关系 5 条：行 111..115 的 parent_code 指向不存在的编码
    for idx in range(110, 115):
        rows[idx]["parent_code"] = f"ITM-{90000 + idx:05d}"
        dirt["dangling_relation"].append(idx + 1)

    ttl = os.path.join(out_dir, "dirty_test_ontology.ttl")
    with open(ttl, "w", encoding="utf-8", newline="\n") as f:
        f.write(DIRTY_TTL)
    xlsx = os.path.join(out_dir, "dirty_batch.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "obj_test_item"
    cols = ["item_code", "item_name", "category", "quantity", "unit_price",
            "parent_code"]
    ws.append(cols)
    for r in rows:
        ws.append([r[c] for c in cols])
    wb.save(xlsx)

    manifest = {
        "rows": n,
        "dirt_row_numbers": dirt,  # 数据行号（不含表头）
        "dirt_counts": {k: len(v) for k, v in dirt.items()},
        "clean_rows": n - sum(len(v) for v in dirt.values()),
        "parent_ref_rule": "parent_code 指向同对象 item_code；悬空=指向不存在编码",
    }
    with open(os.path.join(out_dir, "dirty_manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


# ---------------------------------------------------------------------------
# 3) 双本体"供应商"对齐集（1200/1380，重叠 1000）
# ---------------------------------------------------------------------------

# 口径解释（大纲"键重叠 1200/1380"）：本体 A 1200 实例、本体 B 1380 实例，
# 键集合重叠 1000（A 独有 200、B 独有 380）。重叠区细分（互不相交）：
#   共同来源 150（source_ref 同为 shared-erp）/ 值冲突 120 /
#   语义相似对 40（同名实体不同写法）/ 干净一致 690。

SUPPLIER_TTL_TPL = """@prefix ont: <http://ontology.local/{ns}/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ont:{obj} a owl:Class ;
    rdfs:label "{label}" ;
    rdfs:subClassOf owl:Thing .

ont:attr_sup_code a owl:DatatypeProperty ;
    rdfs:label "供应商编码" ;
    rdfs:domain ont:{obj} ;
    rdfs:range xsd:string .

ont:attr_sup_name a owl:DatatypeProperty ;
    rdfs:label "供应商名称" ;
    rdfs:domain ont:{obj} ;
    rdfs:range xsd:string .

ont:attr_reg_capital a owl:DatatypeProperty ;
    rdfs:label "注册资本" ;
    rdfs:domain ont:{obj} ;
    rdfs:range xsd:float .

ont:attr_city a owl:DatatypeProperty ;
    rdfs:label "城市" ;
    rdfs:domain ont:{obj} ;
    rdfs:range xsd:string .

ont:attr_source_ref a owl:DatatypeProperty ;
    rdfs:label "来源系统" ;
    rdfs:domain ont:{obj} ;
    rdfs:range xsd:string .
{extra_prop}
"""

SUPPLIER_CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉",
                   "西安", "南京", "苏州"]


def gen_alignment(out_dir: str) -> Dict[str, Any]:
    rng = random.Random(SEED)
    n_a, n_b, n_overlap = 1200, 1380, 1000

    # 重叠区键 1..1000 的分组（互不相交）
    overlap = list(range(1, n_overlap + 1))
    common_src = set(rng.sample(overlap, 150))
    conflict = set(rng.sample([c for c in overlap if c not in common_src], 120))
    semantic = set(rng.sample([c for c in overlap if c not in common_src
                               and c not in conflict], 40))

    def base_name(i: int) -> str:
        return f"供应商{i:04d}有限公司"

    rows_a: List[Dict[str, Any]] = []
    rows_b: List[Dict[str, Any]] = []
    # 键分段：重叠 SUP-000001..001000 / A 独有 SUP-100001..100200 /
    #        B 独有 SUP-200001..200380
    for i in range(1, n_overlap + 1):
        city = SUPPLIER_CITIES[i % len(SUPPLIER_CITIES)]
        capital = round(rng.uniform(1e4, 5e7), 2)
        src_a = "shared-erp" if i in common_src else "sys-a"
        src_b = "shared-erp" if i in common_src else "sys-b"
        name = base_name(i)
        rows_a.append({
            "sup_code": f"SUP-{i:06d}", "sup_name": name,
            "reg_capital": capital, "city": city, "source_ref": src_a,
        })
        b_name, b_capital, b_city = name, capital, city
        if i in conflict:
            if i % 2 == 0:
                b_capital = round(capital * 1.1, 2)
            else:
                b_city = SUPPLIER_CITIES[(SUPPLIER_CITIES.index(city) + 1)
                                         % len(SUPPLIER_CITIES)]
        if i in semantic:
            b_name = f"供应商{i:04d}科技"
        rows_b.append({
            "sup_code": f"SUP-{i:06d}", "sup_name": b_name,
            "reg_capital": b_capital, "city": b_city, "source_ref": src_b,
            "sales_region": rng.choice(["东区", "南区", "北区", "西部"]),
        })
    for j in range(1, n_a - n_overlap + 1):
        rows_a.append({
            "sup_code": f"SUP-{100000 + j:06d}", "sup_name": f"供应商A{j:04d}有限公司",
            "reg_capital": round(rng.uniform(1e4, 5e7), 2),
            "city": rng.choice(SUPPLIER_CITIES), "source_ref": "sys-a",
        })
    for j in range(1, n_b - n_overlap + 1):
        rows_b.append({
            "sup_code": f"SUP-{200000 + j:06d}", "sup_name": f"供应商B{j:04d}有限公司",
            "reg_capital": round(rng.uniform(1e4, 5e7), 2),
            "city": rng.choice(SUPPLIER_CITIES), "source_ref": "sys-b",
            "sales_region": rng.choice(["东区", "南区", "北区", "西部"]),
        })

    ttl_a = os.path.join(out_dir, "supplier_alignment_a_ontology.ttl")
    with open(ttl_a, "w", encoding="utf-8", newline="\n") as f:
        f.write(SUPPLIER_TTL_TPL.format(
            ns="aligna", obj="procurement_supplier", label="采购供应商",
            extra_prop=""))
    ttl_b = os.path.join(out_dir, "supplier_alignment_b_ontology.ttl")
    extra = (
        "\nont:attr_sales_region a owl:DatatypeProperty ;\n"
        "    rdfs:label \"销售区域\" ;\n"
        "    rdfs:domain ont:sales_supplier ;\n"
        "    rdfs:range xsd:string .\n")
    with open(ttl_b, "w", encoding="utf-8", newline="\n") as f:
        f.write(SUPPLIER_TTL_TPL.format(
            ns="alignb", obj="sales_supplier", label="销售供应商",
            extra_prop=extra))

    def save_rows(xlsx: str, sheet: str, rows: List[Dict[str, Any]]) -> None:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet[:31]
        ws.append(list(rows[0].keys()))
        for r in rows:
            ws.append([r[c] for c in rows[0]])
        wb.save(xlsx)

    save_rows(os.path.join(out_dir, "supplier_alignment_a_instances.xlsx"),
              "procurement_supplier", rows_a)
    save_rows(os.path.join(out_dir, "supplier_alignment_b_instances.xlsx"),
              "sales_supplier", rows_b)

    manifest = {
        "ontology_a": {"object": "procurement_supplier", "instances": n_a,
                       "ttl": "supplier_alignment_a_ontology.ttl"},
        "ontology_b": {"object": "sales_supplier", "instances": n_b,
                       "ttl": "supplier_alignment_b_ontology.ttl",
                       "extra_attr": "sales_region（结构性相似样本：属性集不完全一致）"},
        "overlap": n_overlap,
        "a_only": n_a - n_overlap,
        "b_only": n_b - n_overlap,
        "overlap_breakdown": {
            "common_source": sorted(common_src),
            "value_conflict": sorted(conflict),
            "semantic_similarity_pair": sorted(semantic),
            "clean_identical": n_overlap - 150 - 120 - 40,
        },
        "key_rule": "重叠 SUP-000001..SUP-001000（i=1..1000）；"
                    "A 独有 SUP-100001..SUP-100200；B 独有 SUP-200001..SUP-200380",
        "note": "共同来源组 source_ref 双端均为 shared-erp；值冲突组 120 条"
               "（偶数 i 注册资本差 10%、奇数 i 城市不同）；语义相似 40 对"
               "（B 端名称缺'有限公司'后缀）。",
    }
    with open(os.path.join(out_dir, "supplier_alignment_manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


# ---------------------------------------------------------------------------
# 4) 基准规模推理/校验集（数千类/属性、数万实例三元组 + SHACL）
# ---------------------------------------------------------------------------

N_ROOT = 1
N_L1 = 20
N_L2 = 100
N_L3 = 1879          # 类合计 2000
N_PROPS = 6000
N_INST = 8000
N_SHAPES = 100


def gen_benchmark(out_dir: str) -> Dict[str, Any]:
    rng = random.Random(SEED)
    lines: List[str] = [
        "@prefix ont: <http://ontology.local/bench/> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix sh: <http://www.w3.org/ns/shacl#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "",
    ]
    # 类层级：root(1) -> L1(20) -> L2(100) -> L3(1879)
    lines.append("ont:BenchRoot a owl:Class ; rdfs:label 'BenchRoot' ;"
                 " rdfs:subClassOf owl:Thing .")
    for i in range(1, N_L1 + 1):
        lines.append(f"ont:bcls_l1_{i:03d} a owl:Class ;"
                     f" rdfs:subClassOf ont:BenchRoot .")
    for i in range(1, N_L2 + 1):
        lines.append(f"ont:bcls_l2_{i:03d} a owl:Class ;"
                     f" rdfs:subClassOf ont:bcls_l1_{(i - 1) % N_L1 + 1:03d} .")
    for i in range(1, N_L3 + 1):
        lines.append(f"ont:bcls_l3_{i:04d} a owl:Class ;"
                     f" rdfs:subClassOf ont:bcls_l2_{(i - 1) % N_L2 + 1:03d} .")
    # 属性 6000（domain 轮转挂到 L2 类）
    for i in range(1, N_PROPS + 1):
        dom = (i - 1) % N_L2 + 1
        rng_x = "xsd:string" if i % 2 else "xsd:float"
        lines.append(f"ont:battr_{i:05d} a owl:DatatypeProperty ;"
                     f" rdfs:domain ont:bcls_l2_{dom:03d} ;"
                     f" rdfs:range {rng_x} .")
    # SHACL 形状（前 100 个 L2 类）
    for i in range(1, N_SHAPES + 1):
        lines.append(
            f"ont:bshape_{i:03d} a sh:NodeShape ;"
            f" sh:targetClass ont:bcls_l2_{i:03d} ;"
            f" sh:property [ sh:path ont:battr_{i:05d} ; sh:minCount 1 ] ;"
            f" sh:property [ sh:path ont:battr_{(i % N_PROPS) + 1:05d} ;"
            f" sh:datatype xsd:float ] .")
    # 实例 8000：每条 = rdf:type + 3 属性 ≈ 4 三元组
    inst_triples = 0
    for i in range(1, N_INST + 1):
        cls = (i - 1) % N_L3 + 1
        p1 = (i * 7) % N_PROPS + 1
        p2 = (i * 13) % N_PROPS + 1
        p3 = (i * 29) % N_PROPS + 1
        v = round(rng.uniform(-100, 1000), 3)
        lines.append(
            f"ont:binst_{i:06d} a ont:bcls_l3_{cls:04d} ;"
            f" ont:battr_{p1:05d} {i} ;"
            f" ont:battr_{p2:05d} {v} ;"
            f" ont:battr_{p3:05d} 'val_{i}' .")
        inst_triples += 4
    ttl = os.path.join(out_dir, "benchmark_graph.ttl")
    with open(ttl, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    total_classes = N_ROOT + N_L1 + N_L2 + N_L3
    manifest = {
        "classes": total_classes,
        "class_hierarchy": {"root": N_ROOT, "l1": N_L1, "l2": N_L2, "l3": N_L3},
        "datatype_properties": N_PROPS,
        "instances": N_INST,
        "instance_triples": inst_triples,
        "shacl_shapes": N_SHAPES,
        "total_triples_approx": (total_classes + N_PROPS + N_SHAPES * 3
                                 + inst_triples),
        "purpose": "V2-16 远程推理 / V2-17 批量 SHACL 校验 性能基准（数千类/属性、"
                   "数万实例三元组）",
    }
    with open(os.path.join(out_dir, "benchmark_manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

README = """# V2 测试数据集（VS2-00 产出，seed=42 确定性可复跑）

再生成：`python scripts/generate_v2_datasets.py`
验收：`python scripts/verify_vs2_00_mock.py`

| 文件 | 用途 | 关联用例 |
|---|---|---|
| maintenance_cost_ontology.ttl | 维保成本本体（17 对象）定义 | TC-04-01 |
| maintenance_cost_2297.xlsx | 2297 条实例（17 Sheet，上传型） | TC-04-01 |
| 仓库根 成本清单_维保服务产品_复算版.xlsx | Excel 参照（33 行成本清单＋L1/L2 分级） | TC-04-01 |
| dirty_test_ontology.ttl / dirty_batch.xlsx / dirty_manifest.json | 脏数据批 200 行 | TC-21-01~04 |
| supplier_alignment_{a,b}_*.ttl/.xlsx / supplier_alignment_manifest.json | 双本体供应商对齐集 | TC-02-02~06, TC-21-08 |
| benchmark_graph.ttl / benchmark_manifest.json | 基准规模推理/校验集 | TC-16-05, TC-17-05 |

## 1) 六成本公式口径（TC-04-01，来自《成本核算说明》权威原文，P9 报告 §2.1）

| 项 | 公式 |
|---|---|
| E 人工成本 | `E = 现场服务率 D × 单台人工成本基线 C` |
| K 备件辅助（国内） | `K = H × G` |
| K 备件辅助（海外） | `K = H×G + I×J×1.1×1.02` |
| N 管理成本 | `N = 管理成本基线 L × 现场服务率 M` |
| O 成本合计 | `O = E + F + K + N` |
| S 成本占比 | `S = O / R` |

**容差口径**：L1（VERIFIED）行＝国内 25 行＋海外三年行，87 项复算与源表逐项一致，
**容差 0（精确复现）**；L2（MOCK）欧洲四年/五年 8 行（r19/r20/r22/r23/r25/r26/r28/r29）
口径待业务确认，**不参与 L1 判定**。参照文件"Golden Dataset说明"Sheet 为准。

## 2) 脏数据批（V2-21 四维）

200 行：缺键 10 / 重复键 8 / 越枚举 10 / 越界 10 / 悬空关系 5 / 干净 167。
逐行位置见 `dirty_manifest.json`（数据行号，不含表头）。

## 3) 双本体"供应商"对齐集（TC-02-02~06）

大纲"键重叠 1200/1380" 解读：**本体 A（procurement_supplier）1200 实例、
本体 B（sales_supplier）1380 实例，键集合重叠 1000**（A 独有 200、B 独有 380；
键分段 SUP-000001..001000 重叠 / SUP-100001.. 仅 A / SUP-200001.. 仅 B）。
重叠区互不相交细分：共同来源 150 / 值冲突 120 / 语义相似对 40 / 干净一致 690。
B 多一个属性 sales_region（结构相似但不全同样本）。明细见
`supplier_alignment_manifest.json`。

## 4) mock 外部系统三系统 PN 联邦查询集（内置于 backend.mocks）

600 键：三系统共有 400 / 恰好两系统 100 / 单系统 50；同键跨系统值冲突 50。
行计数 crm=484 / plm=483 / reservoir=483。字段名各异：PN / PN码 / PN_Num。
"""


def gen_readme(out_dir: str) -> None:
    with open(os.path.join(out_dir, "README.md"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(README)


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--runtime-root",
                    default=os.path.join(REPO, "backend", "data", "projects"))
    ap.add_argument("--runtime-project", default=DEFAULT_RUNTIME_PROJECT)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print("[1/5] 维保成本 2297 条 ...")
    m1 = gen_maintenance(args.out, args.runtime_root, args.runtime_project)
    print("     total =", m1["total"], "| sheets =", len(m1["per_object"]))
    print("[2/5] 脏数据批 ...")
    m2 = gen_dirty(args.out)
    print("     dirt_counts =", m2["dirt_counts"])
    print("[3/5] 供应商对齐集 ...")
    m3 = gen_alignment(args.out)
    print("     overlap =", m3["overlap"],
          "| breakdown clean =", m3["overlap_breakdown"]["clean_identical"])
    print("[4/5] 基准规模图 ...")
    m4 = gen_benchmark(args.out)
    print("     classes =", m4["classes"], "| props =", m4["datatype_properties"],
          "| inst_triples =", m4["instance_triples"])
    print("[5/5] README ...")
    gen_readme(args.out)
    print("done ->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
