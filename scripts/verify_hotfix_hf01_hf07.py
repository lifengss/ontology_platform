# -*- coding: utf-8 -*-
"""hotfix HF-01~HF-07 合成数据验收脚本。

    python scripts/verify_hotfix_hf01_hf07.py

边界：
- 只验证平台代码修复后的能力，使用临时 DATA_DIR 与合成实例；不导入成本基线 Excel，
  也不宣称 AB62=17.082 / O3=209.83 等业务数值通过。
- 默认关闭鉴权，覆盖 REST / 服务层 / 表达式层；Fuseki 不可达时验证文件降级回退，
  远程 SPARQL 增量更新语法本身不在本脚本启动外部服务的范围内。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="ontology_hotfix_")
os.environ.update({
    "DATA_DIR": os.path.join(TMP, "data"),
    "SECRETS_DIR": os.path.join(TMP, "secrets"),
    "LOG_DIR": os.path.join(TMP, "logs"),
    "JENA_BASE_URL": "http://127.0.0.1:9",
    "ONTOLOGY_AUTH_ENABLED": "false",
})
sys.path.insert(0, os.path.join(REPO, "backend"))

import api.router_ingest as ingest_router  # noqa: E402
import jena_adapter.rdf_sync as rdf_sync_mod  # noqa: E402
from capabilities import CapabilityService  # noqa: E402
from capabilities.expr_parser import WHITELIST_FUNCTIONS, parse, validate  # noqa: E402
from core.instance_store import get_instance_store  # noqa: E402
from core.storage import JsonStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from ingestion import approve  # noqa: E402
from main import app  # noqa: E402

PID = "hf-verify"
ONT = "ont-hotfix-main"
ONT_A = "ont-hotfix-a"
ONT_B = "ont-hotfix-b"

PRODUCT_ID = "prod-1"
PRODUCT_B_ID = "prod-ont-b"
BASE1 = "base-1"
BASE2 = "base-2"
BASE3 = "base-3"
MAT_U1 = "mat-u1"
MAT_U2 = "mat-u2"

PASS = FAIL = 0
SYNC_CALLS: list[dict[str, Any]] = []
ORIG_SYNC = None
STORE = None
SVC: CapabilityService | None = None
ONTOLOGY: dict[str, Any] | None = None
CAP_HF04 = ""


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} :: {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def run_section(title: str, fn) -> None:
    section(title)
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        check(f"{title} 未抛异常", False, repr(e))
        traceback.print_exc()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env(resp) -> dict[str, Any]:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def data(resp) -> dict[str, Any]:
    return env(resp).get("data") or {}


def has_error(resp, code: str) -> bool:
    return code in json.dumps(env(resp), ensure_ascii=False)


def near(a: Any, b: float, tol: float = 1e-9) -> bool:
    return isinstance(a, (int, float)) and abs(float(a) - b) <= tol


def make_ontology() -> dict[str, Any]:
    def attrs(items: list[tuple[str, str, bool, dict | None]]):
        return [{
            "id": aid,
            "name": aid,
            "data_type": dtype,
            "required": required,
            "constraints": cons or {},
        } for aid, dtype, required, cons in items]

    objects = [
        {
            "id": "obj_material", "name": "物料", "stable_key": "attr_pn",
            "business_definition": "合成物料",
            "attributes": attrs([
                ("attr_pn", "string", True, None),
                ("attr_factory", "string", True, None),
                ("attr_price", "float", False, None),
                ("attr_priority", "float", False, None),
            ]),
            "relations": [],
        },
        {
            "id": "obj_baseline", "name": "基线", "stable_key": "attr_key",
            "business_definition": "合成基线",
            "attributes": attrs([
                ("attr_key", "string", True, None),
                ("attr_value", "float", True, {"minimum": 0}),
            ]),
            "relations": [],
        },
        {
            "id": "obj_product", "name": "产品", "stable_key": "attr_key",
            "business_definition": "合成产品",
            "attributes": attrs([
                ("attr_key", "string", True, None),
                ("attr_calc", "float", False, None),
                ("attr_service_term", "string", False, None),
                ("attr_bom_items", "string", False, None),
            ]),
            "relations": [],
        },
        {
            "id": "obj_bom", "name": "BOM", "stable_key": "attr_group_key",
            "business_definition": "合成 BOM 行",
            "attributes": attrs([
                ("attr_group_key", "string", False, None),
                ("attr_line_cost", "float", False, None),
                ("attr_priority", "float", False, None),
            ]),
            "relations": [],
        },
        {
            "id": "obj_missing_group", "name": "未装配分组", "stable_key": "attr_value",
            "business_definition": "合法空分组",
            "attributes": attrs([("attr_value", "float", False, None)]),
            "relations": [],
        },
    ]
    return {
        "id": ONT,
        "name": "hotfix 合成本体",
        "modeling_type": "manual",
        "status": "active",
        "objects": objects,
        "constraints": [{
            "id": "cst_material_composite_unique",
            "name": "PN+工厂复合唯一",
            "scope": "obj_material",
            "rule": "unique",
            "params": {"attributes": ["attr_pn", "attr_factory"]},
        }],
    }


def insert_instance(store, rid: str, otype: str, payload: dict[str, Any],
                    ontology_id: str | None = ONT) -> None:
    instance = {"object_type": otype, **payload}
    rec: dict[str, Any] = {
        "id": rid,
        "project_id": PID,
        "object_type": otype,
        "instance": instance,
        "imported_at": now(),
    }
    if ontology_id is not None:
        rec["ontology_id"] = ontology_id
    store.insert(rec)


def setup() -> None:
    global STORE, SVC, ONTOLOGY
    ONTOLOGY = make_ontology()
    JsonStore("ontologies", project_id=PID).insert(ONTOLOGY)
    STORE = get_instance_store(project_id=PID)
    SVC = CapabilityService(PID)

    insert_instance(STORE, PRODUCT_ID, "obj_product", {
        "attr_key": "BASE-1", "_source": {"kind": "manual"},
    })
    insert_instance(STORE, PRODUCT_B_ID, "obj_product", {
        "attr_key": "BASE-1",
    }, ontology_id=ONT_B)

    for rid, key, value in ((BASE1, "BASE-1", 10), (BASE2, "BASE-2", 20),
                            (BASE3, "BASE-3", 30)):
        insert_instance(STORE, rid, "obj_baseline", {
            "attr_key": key, "attr_value": value,
            "_source": {"kind": "manual", "raw_ref": rid},
        })

    boms = [
        ("bom-none", {"attr_line_cost": 1.5, "attr_priority": 1}),
        ("bom-g1-a", {"attr_group_key": "G1", "attr_line_cost": 2.0, "attr_priority": 1}),
        ("bom-g1-b", {"attr_group_key": "G1", "attr_line_cost": 3.0, "attr_priority": 2}),
        ("bom-g2", {"attr_group_key": "G2", "attr_line_cost": 4.0, "attr_priority": 3}),
    ]
    for rid, payload in boms:
        insert_instance(STORE, rid, "obj_bom", payload)

    insert_instance(STORE, "mat-1600", "obj_material", {
        "attr_pn": "P1", "attr_factory": "1600", "attr_price": 20, "attr_priority": 2,
    })
    insert_instance(STORE, "mat-1100", "obj_material", {
        "attr_pn": "P1", "attr_factory": "1100", "attr_price": 10, "attr_priority": 10,
    })
    insert_instance(STORE, "mat-a", "obj_material", {
        "attr_pn": "PN-X", "attr_factory": "A", "attr_price": 100, "attr_priority": 1,
    }, ontology_id=ONT_A)
    insert_instance(STORE, "mat-b", "obj_material", {
        "attr_pn": "PN-X", "attr_factory": "B", "attr_price": 200, "attr_priority": 1,
    }, ontology_id=ONT_B)
    insert_instance(STORE, "mat-legacy", "obj_material", {
        "attr_pn": "PN-LEGACY", "attr_factory": "L", "attr_price": 77, "attr_priority": 1,
    }, ontology_id=None)

    insert_instance(STORE, MAT_U1, "obj_material", {
        "attr_pn": "PN-U", "attr_factory": "F-U", "attr_price": 1, "attr_priority": 1,
    })
    insert_instance(STORE, MAT_U2, "obj_material", {
        "attr_pn": "PN-U", "attr_factory": "F-OTHER", "attr_price": 2, "attr_priority": 1,
    })
    insert_instance(STORE, "mat-existing", "obj_material", {
        "attr_pn": "PN-EX", "attr_factory": "F-EX", "attr_price": 3, "attr_priority": 1,
    })


def create_cap(name: str, expression: str, ontology_id: str | None = None,
               output_attribute: str | None = None) -> str:
    assert SVC is not None
    rec = SVC.create_capability({
        "name": name,
        "object_type": "obj_product",
        "expression": expression,
        "ontology_id": ontology_id,
        "output_attribute": output_attribute,
    })
    return rec["id"]


def exec_cap(cap_id: str, dry_run: bool = True,
             inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    assert SVC is not None
    return SVC.execute(cap_id, PRODUCT_ID, dry_run=dry_run, inputs=inputs)


def fake_sync(project_id: str, ontology_id: str | None, *,
              instance_id: str | None = None, deleted: bool = False) -> None:
    SYNC_CALLS.append({
        "project_id": project_id,
        "ontology_id": ontology_id,
        "instance_id": instance_id,
        "deleted": deleted,
    })


def static_section() -> None:
    needed = [
        "len", "split", "regex_match", "upper", "lower", "date_add", "date_diff",
        "sum", "map", "filter", "group_by", "lookup_first", "coalesce_lookup", "lookup_all",
    ]
    check("HF-03/06/07 新内置均已进入白名单",
          all(fn in WHITELIST_FUNCTIONS for fn in needed), str(needed))

    ok_exprs = [
        "lookup_first('obj', {}, 'value', 'priority', true)",
        "coalesce_lookup('obj', 'value', {'factory': '1600'}, {'factory': '1100'})",
        "sum(map(filter(lookup_all('obj_bom', {}, null), 'attr_group_key', null), 'attr_line_cost'))",
        "group_by(lookup_all('obj_bom', {}, null), 'attr_group_key', 'attr_line_cost', 'count')",
        "upper(split('四年7×24×4H', '年', 0))",
    ]
    for expr in ok_exprs:
        issues = validate(parse(expr))
        check(f"定义期静态校验通过: {expr[:42]}", issues == [], str(issues))

    bad_cases = [
        ("map(this.items, attr_name)", "动态字段名被拦截"),
        ("group_by(this.items, 'group_key', 'value', 'avg')", "非法 group_by 聚合被拦截"),
        ("lookup_first('obj', {}, 'value', 'priority', 1)", "非布尔 descending 被拦截"),
        ("coalesce_lookup('obj', 'value', dims)", "非字典字面量维度被拦截"),
        ("evil()", "未知函数仍被拦截"),
    ]
    for expr, name in bad_cases:
        issues = validate(parse(expr))
        check(name, bool(issues), str(issues))

    js_path = os.path.join(REPO, "frontend", "app.js")
    with open(js_path, encoding="utf-8") as f:
        js = f.read()
    start = js.index("async function saveInstanceEdit")
    end = js.index("async function deleteInstance", start)
    block = js[start:end]
    check("前端字段级编辑走 PATCH 同一校验链路",
          "method: 'PATCH'" in block and "method: 'PUT'" not in block)


def service_section() -> None:
    global CAP_HF04
    section_caps: dict[str, str] = {}

    CAP_HF04 = create_cap(
        "cap_hf04",
        "lookup('obj_baseline', {'attr_key': this.attr_key}, 'attr_value')",
        ontology_id=ONT,
        output_attribute="attr_calc",
    )
    assert SVC is not None
    batch = SVC.execute_batch(CAP_HF04, dry_run=True)
    check("HF-05 execute_batch 按能力 ontology_id 过滤 records",
          batch.get("total") == 1, str(batch.get("total")))

    section_caps["hf06"] = create_cap(
        "cap_hf06",
        "upper(split(this.attr_service_term, '年', 0)) + '|' + lower('ARMA') + '|' "
        "+ str(len(this.attr_service_term)) + '|' + str(date_add('2026-01-31', 1, 'month')) "
        "+ '|' + str(date_diff('2026-03-01', '2026-02-28')) + '|' "
        "+ str(regex_match(this.attr_service_term, '7×24'))",
    )
    r = exec_cap(section_caps["hf06"], inputs={"attr_service_term": "四年7×24×4H"})
    check("HF-06 字符串解析 / 日期运算结果正确",
          r.get("value") == "四|arma|9|2026-02-28|1|True", str(r.get("value")))
    check("HF-06 inputs 临时覆盖不落库",
          r.get("inputs_persisted") is False and r.get("input_source") == "federated_read_override")

    section_caps["hf07_sum"] = create_cap(
        "cap_hf07_sum",
        "sum(map(filter(lookup_all('obj_bom', {}, null), 'attr_group_key', null), 'attr_line_cost'))",
    )
    r = exec_cap(section_caps["hf07_sum"])
    check("HF-07 map/filter/sum 合成聚合", near(r.get("value"), 1.5), str(r.get("value")))

    section_caps["hf07_empty"] = create_cap(
        "cap_hf07_empty",
        "sum(lookup_all('obj_missing_group', {}, 'attr_value'))",
    )
    r = exec_cap(section_caps["hf07_empty"])
    check("HF-07 未装配分组 0 命中合法且聚合为 0",
          r.get("status") == "success" and near(r.get("value"), 0),
          str(r.get("error_code") or r.get("value")))

    section_caps["hf07_group"] = create_cap(
        "cap_hf07_group",
        "group_by(lookup_all('obj_bom', {}, null), 'attr_group_key', 'attr_line_cost', 'count')",
    )
    r = exec_cap(section_caps["hf07_group"])
    grouped = r.get("value") or {}
    check("HF-07 group_by 跳过空分组并计数",
          grouped.get("G1") == 2 and grouped.get("G2") == 1, str(grouped))

    section_caps["hf03_first"] = create_cap(
        "cap_hf03_first",
        "lookup_first('obj_material', {'attr_pn': 'P1'}, 'attr_price', 'attr_priority', true)",
    )
    r = exec_cap(section_caps["hf03_first"])
    lookups = (r.get("trace") or {}).get("lookups") or []
    check("HF-03 lookup_first 数值降序优先级（10>2，非字典序）",
          near(r.get("value"), 10), str(r.get("value")))
    check("HF-03 lookup_first trace 保留排序前命中数",
          bool(lookups) and lookups[0].get("matched_count") == 2,
          str(lookups[:1]))
    check("HF-03 lookup_first selection_rule 可审计",
          bool(lookups) and str(lookups[0].get("selection_rule", "")).startswith("first:attr_priority:desc"),
          str(lookups[:1]))

    section_caps["hf03_coalesce"] = create_cap(
        "cap_hf03_coalesce",
        "coalesce_lookup('obj_material', 'attr_price', "
        "{'attr_pn': 'P1', 'attr_factory': '1600'}, "
        "{'attr_pn': 'P1', 'attr_factory': '1100'})",
    )
    r = exec_cap(section_caps["hf03_coalesce"])
    check("HF-03 coalesce_lookup 优先 1600", near(r.get("value"), 20), str(r.get("value")))

    section_caps["hf03_fallback"] = create_cap(
        "cap_hf03_fallback",
        "coalesce_lookup('obj_material', 'attr_price', "
        "{'attr_pn': 'P1', 'attr_factory': '9999'}, "
        "{'attr_pn': 'P1', 'attr_factory': '1100'})",
    )
    r = exec_cap(section_caps["hf03_fallback"])
    check("HF-03 coalesce_lookup 缺失回退 1100", near(r.get("value"), 10), str(r.get("value")))

    section_caps["hf03_strict"] = create_cap(
        "cap_hf03_strict",
        "lookup('obj_material', {'attr_pn': 'P1'}, 'attr_price')",
    )
    r = exec_cap(section_caps["hf03_strict"])
    check("HF-03 默认 lookup 仍严格歧义报错",
          r.get("status") == "error" and r.get("error_code") == "LOOKUP_AMBIGUOUS",
          str(r.get("error_code")))

    section_caps["hf05_scoped"] = create_cap(
        "cap_hf05_scoped",
        "lookup('obj_material', {'attr_pn': 'PN-X'}, 'attr_price')",
        ontology_id=ONT_A,
    )
    r = exec_cap(section_caps["hf05_scoped"])
    check("HF-05 lookup 按 ontology_id 过滤", near(r.get("value"), 100), str(r.get("value")))

    section_caps["hf05_legacy"] = create_cap(
        "cap_hf05_legacy",
        "lookup('obj_material', {'attr_pn': 'PN-LEGACY'}, 'attr_price')",
        ontology_id=ONT_A,
    )
    r = exec_cap(section_caps["hf05_legacy"])
    check("HF-05 历史无 ontology_id 实例兼容归入当前本体",
          near(r.get("value"), 77), str(r.get("value")))

    section_caps["hf05_unscoped"] = create_cap(
        "cap_hf05_unscoped",
        "lookup('obj_material', {'attr_pn': 'PN-X'}, 'attr_price')",
    )
    r = exec_cap(section_caps["hf05_unscoped"])
    check("HF-05 能力未指定 ontology_id 时保持兼容回退（多本体命中仍歧义）",
          r.get("status") == "error" and r.get("error_code") == "LOOKUP_AMBIGUOUS",
          str(r.get("error_code")))


def hf04_section(client: TestClient) -> None:
    assert SVC is not None and STORE is not None
    res = SVC.execute(CAP_HF04, PRODUCT_ID, dry_run=False)
    check("HF-04 写回 source_fingerprint / fresh 标记",
          res.get("status") == "success"
          and bool(((STORE.get(PRODUCT_ID) or {}).get("instance") or {})
                   .get("__capability__", {}).get("source_fingerprint"))
          and ((STORE.get(PRODUCT_ID) or {}).get("instance") or {})
              .get("__capability__", {}).get("result_freshness") == "fresh",
          str(res.get("error_code") or res.get("value")))

    body = {"instance": {
        "object_type": "obj_baseline",
        "attr_key": "BASE-1",
        "attr_value": 11,
        "_source": {"kind": "manual", "raw_ref": BASE1},
    }}
    SYNC_CALLS.clear()
    r = client.put(
        f"/api/ingest/instances/{BASE1}",
        params={"project_id": PID, "defer_rdf_sync": "true", "reason": "HF-04 基线热修改"},
        json=body,
    )
    d = data(r)
    check("HF-04 基线更新返回受影响结果数量",
          r.status_code == 200 and int(d.get("stale_marked") or 0) >= 1,
          f"status={r.status_code} data={d} body={env(r)}")
    check("HF-04 defer_rdf_sync=true 不触发 RDF 同步",
          len(SYNC_CALLS) == 0, str(SYNC_CALLS))

    prod = STORE.get(PRODUCT_ID) or {}
    meta = (prod.get("instance") or {}).get("__capability__") or {}
    check("HF-04 结果实例被物理标记 stale",
          meta.get("stale") is True and meta.get("result_freshness") == "stale",
          str(meta))

    r = client.get(
        f"/api/capabilities/{CAP_HF04}/affected-instances",
        params={"project_id": PID, "instance_id": BASE1},
    )
    d = data(r)
    check("HF-04 affected-instances 端点返回受影响清单",
          r.status_code == 200 and int(d.get("total") or 0) >= 1
          and int(d.get("stale") or 0) >= 1,
          f"status={r.status_code} data={d}")


def _baseline_body(value: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "object_type": "obj_baseline",
        "attr_key": "BASE-1",
        "attr_value": value,
        "_source": {"kind": "manual", "raw_ref": BASE1},
    }
    if extra:
        payload.update(extra)
    return {"instance": payload}


def hf01_hf02_section(client: TestClient) -> None:
    params = {"project_id": PID, "defer_rdf_sync": "true"}

    r = client.put(f"/api/ingest/instances/{BASE1}", params=params,
                   json=_baseline_body(-1))
    check("HF-01 SHACL 越界值拒绝（minimum=0）",
          r.status_code == 422 and has_error(r, "VALIDATION_ERROR"),
          f"status={r.status_code} body={env(r)}")

    strict_params = {**params, "strict_fields": "true"}
    r = client.put(f"/api/ingest/instances/{BASE1}", params=strict_params,
                   json=_baseline_body(12, {"attr_unknown": "x"}))
    check("HF-01 strict_fields=true 拒绝未知字段",
          r.status_code == 422 and has_error(r, "VALIDATION_ERROR"),
          f"status={r.status_code} body={env(r)}")

    r = client.put(f"/api/ingest/instances/{BASE1}", params=params,
                   json=_baseline_body(12, {"attr_unknown": "x"}))
    d = data(r)
    check("HF-01 默认未知字段显式告警且不打断派生维度",
          r.status_code == 200 and any("未知字段" in w for w in d.get("warnings") or []),
          f"status={r.status_code} warnings={d.get('warnings')}")

    patch_params = {**params, "reason": "HF-01 字段级编辑"}
    r = client.patch(f"/api/ingest/instances/{BASE1}", params=patch_params,
                     json={"patch": {"attr_value": 13}})
    d = data(r)
    check("HF-01 PATCH 字段级更新保留 _source 血缘",
          r.status_code == 200
          and (d.get("instance") or {}).get("_source", {}).get("kind") == "manual"
          and (d.get("instance") or {}).get("attr_value") == 13,
          f"status={r.status_code} data={d}")

    batch_items = {"items": [
        {"id": BASE1, "instance": _baseline_body(14)["instance"], "reason": "batch-1"},
        {"id": BASE2, "instance": {
            "object_type": "obj_baseline", "attr_key": "BASE-2", "attr_value": 21,
            "_source": {"kind": "manual", "raw_ref": BASE2},
        }},
    ]}
    SYNC_CALLS.clear()
    r = client.put("/api/ingest/instances/batch",
                   params={"project_id": PID, "defer_rdf_sync": "true"},
                   json=batch_items)
    d = data(r)
    check("HF-02 批量更新接口生效且 defer 不触发 RDF 同步",
          r.status_code == 200 and d.get("updated") == 2
          and d.get("rdf_sync_deferred") is True and len(SYNC_CALLS) == 0,
          f"status={r.status_code} data={d} sync={SYNC_CALLS}")

    logs_resp = client.get(f"/api/ingest/instances/{BASE1}/change-logs",
                           params={"project_id": PID, "pageSize": 50})
    logs = (data(logs_resp).get("items") or [])
    check("HF-01 变更日志端点返回 before/after/diff/operator/reason",
          logs_resp.status_code == 200 and logs
          and any("diff" in row and "operator" in row and "reason" in row for row in logs)
          and {"PUT", "PATCH", "BATCH"}.intersection({row.get("operation") for row in logs}),
          f"status={logs_resp.status_code} total={data(logs_resp).get('total')}")

    SYNC_CALLS.clear()
    r = client.put(f"/api/ingest/instances/{BASE2}",
                   params={"project_id": PID},
                   json={"instance": {
                       "object_type": "obj_baseline", "attr_key": "BASE-2", "attr_value": 22,
                       "_source": {"kind": "manual", "raw_ref": BASE2},
                   }})
    check("HF-02 单实例 PUT 默认走增量 RDF 同步",
          r.status_code == 200 and len(SYNC_CALLS) == 1
          and SYNC_CALLS[0].get("instance_id") == BASE2
          and SYNC_CALLS[0].get("deleted") is False,
          f"status={r.status_code} sync={SYNC_CALLS}")

    SYNC_CALLS.clear()
    r = client.put("/api/ingest/instances/batch",
                   params={"project_id": PID}, json=batch_items)
    d = data(r)
    check("HF-02 批量更新末尾按本体只同步一次",
          r.status_code == 200 and len(SYNC_CALLS) == 1
          and SYNC_CALLS[0].get("instance_id") is None
          and d.get("ontologies_synced") == [ONT],
          f"status={r.status_code} data={d} sync={SYNC_CALLS}")

    SYNC_CALLS.clear()
    r = client.delete(f"/api/ingest/instances/{BASE3}",
                      params={"project_id": PID})
    check("HF-02 删除实例按 subject 增量同步",
          r.status_code == 200 and len(SYNC_CALLS) == 1
          and SYNC_CALLS[0].get("instance_id") == BASE3
          and SYNC_CALLS[0].get("deleted") is True,
          f"status={r.status_code} sync={SYNC_CALLS}")


def hf03_composite_section(client: TestClient) -> None:
    assert ONTOLOGY is not None
    dup_instance = {
        "object_type": "obj_material",
        "attr_pn": "PN-DUP", "attr_factory": "F1",
        "attr_price": 1, "attr_priority": 1,
    }
    candidate = {
        "status": "pending",
        "project_id": PID,
        "ontology_id": ONT,
        "object_type": "obj_material",
        "source_type": "internal",
        "source_ref": "synthetic",
        "instances": [dict(dup_instance), dict(dup_instance)],
    }
    res = approve(candidate, ONTOLOGY, PID)
    violations = res.get("violations") or []
    check("HF-03 approve 业务层拦截本批复合唯一冲突",
          any("复合唯一" in str(v.get("message")) for v in violations),
          str(violations[:2]))

    existing_clone = {
        "object_type": "obj_material",
        "attr_pn": "PN-EX", "attr_factory": "F-EX",
        "attr_price": 9, "attr_priority": 1,
    }
    candidate2 = {
        "status": "pending", "project_id": PID, "ontology_id": ONT,
        "object_type": "obj_material", "source_type": "internal",
        "source_ref": "synthetic", "instances": [existing_clone],
    }
    res = approve(candidate2, ONTOLOGY, PID)
    violations = res.get("violations") or []
    check("HF-03 approve 同时检查已入库实例",
          any("复合唯一" in str(v.get("message")) for v in violations),
          str(violations[:2]))

    r = client.put(
        f"/api/ingest/instances/{MAT_U2}",
        params={"project_id": PID, "defer_rdf_sync": "true"},
        json={"instance": {
            "object_type": "obj_material",
            "attr_pn": "PN-U", "attr_factory": "F-U",
            "attr_price": 2, "attr_priority": 1,
        }},
    )
    check("HF-03 实例更新复用复合唯一校验",
          r.status_code == 422 and has_error(r, "VALIDATION_ERROR"),
          f"status={r.status_code} body={env(r)}")


def hf02_rdf_section() -> None:
    check("HF-02 rdf_sync 暴露 sync_instance 单实例接口",
          callable(getattr(rdf_sync_mod, "sync_instance", None)))
    res = rdf_sync_mod.sync_instance(PID, ONT, BASE1, deleted=False)
    check("HF-02 文件降级正确性优先（回退全量重写）",
          res.get("incremental") is False
          and res.get("fallback") == "file_mode_full_rewrite",
          str(res))


def main() -> int:
    global ORIG_SYNC
    try:
        section("setup")
        try:
            setup()
            check("合成本体/实例/服务初始化", True)
        except Exception as e:  # noqa: BLE001
            check("合成本体/实例/服务初始化", False, repr(e))
            traceback.print_exc()
            return 1

        ORIG_SYNC = ingest_router._sync_rdf
        ingest_router._sync_rdf = fake_sync

        with TestClient(app) as client:
            run_section("定义期静态校验与前端 PATCH", static_section)
            run_section("HF-03/05/06/07 服务层与表达式", service_section)
            run_section("HF-04 基线失效通知", lambda: hf04_section(client))
            run_section("HF-01/02 实例更新、审计与 RDF 同步", lambda: hf01_hf02_section(client))
            run_section("HF-03 复合唯一业务校验", lambda: hf03_composite_section(client))
            run_section("HF-02 RDF 单实例同步降级", hf02_rdf_section)

        print(f"\nRESULT: PASS={PASS} FAIL={FAIL}")
        return 0 if FAIL == 0 else 1
    finally:
        if ORIG_SYNC is not None:
            ingest_router._sync_rdf = ORIG_SYNC
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
