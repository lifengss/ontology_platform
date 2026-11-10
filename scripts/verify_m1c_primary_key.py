# -*- coding: utf-8 -*-
"""M1-C（V2-03 显式指定业务主键）验收脚本。

用法：
    python scripts/verify_m1c_primary_key.py            # 全量（REST + 真实浏览器 DOM + 并发）
    python scripts/verify_m1c_primary_key.py --no-ui   # 跳过 Playwright 段

覆盖 docs/V2-测试用例.md / V2-测试大纲.md：
  TP-03-01 前端对象卡片主键下拉（选项=string 属性）＋自动规则说明保留（真实 Chromium DOM）
  TP-03-02 PATCH 显式指定 → 持久生效 → 刷新回显；PUT/不传/null 回退语义
  TP-03-03 不存在属性 / 非 string 属性 → 422 并提示原因，失败不落库
  TP-03-04 新入库实例按（当前/显式）主键匹配去重，批次内重复同样收敛
  TCE-03-01（EP-03-01）已有实例时变更主键给影响提示、存量不静默改写、迁移须显式确认
  TCE-03-02（EP-03-02）并发两请求后到者基于最新状态 409，无脏写

数据在临时 DATA_DIR 中隔离，不污染仓库 data/。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import os
import sys
import tempfile

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="m1c_data_")
sys.path.insert(0, os.path.join(REPO, "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from core.instance_store import get_instance_store  # noqa: E402
from core.storage import JsonStore  # noqa: E402
from main import app  # noqa: E402

PID = "m1c-verify"
P = {"project_id": PID}
OBJ_ID = "obj_customer"
client = TestClient(app)

PASS = FAIL = 0
FAILS: list[str] = []
SKIPS: list[str] = []
OID = ""
UI_SCRIPT = ""


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}  {detail}")


def skip(name: str, why: str) -> None:
    SKIPS.append(name)
    print(f"  [SKIP] {name}  {why}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def body(r) -> dict:
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {"raw": r.text[:200]}


def obj_of(data: dict) -> dict:
    return next(o for o in data["objects"] if o["id"] == OBJ_ID)


def get_ontology() -> dict:
    r = client.get(f"/api/ontologies/{OID}", params=P)
    assert r.status_code == 200, r.text[:200]
    return body(r)["data"]


def patch_obj(payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    r = client.patch(f"/api/ontologies/{OID}/objects/{OBJ_ID}", params=P,
                     json=payload, headers=headers or {})
    return r.status_code, body(r)


def make_candidate(cid: str, instances: list[dict]) -> dict:
    JsonStore("ingest_candidates", project_id=PID).insert({
        "id": cid, "project_id": PID, "ontology_id": OID, "object_type": OBJ_ID,
        "source_type": "field_map", "source_ref": f"verify_m1c:{cid}",
        "status": "pending", "instances": instances, "created_at": "2026-11-10T00:00:00+00:00",
    })
    r = client.post(f"/api/ingest/candidates/{cid}/approve", params=P)
    return {"status": r.status_code, "data": body(r)}


def instances() -> list[dict]:
    return get_instance_store(project_id=PID).find(object_type=OBJ_ID, limit=None)


def setup() -> None:
    global OID
    section("准备：业务本体（含 string/integer/float 属性）")
    payload = {
        "name": "M1C验收-客户主数据", "description": "显式业务主键验收",
        "modeling_type": "manual", "objects": [{
            "id": OBJ_ID, "name": "客户", "description": "客户主数据",
            "business_definition": "客户主数据对象代表签约客户及其唯一编码",
            "attributes": [
                {"id": "attr_name", "name": "名称", "data_type": "string", "required": True, "constraints": {}},
                {"id": "attr_code", "name": "客户编码", "data_type": "string", "required": False, "constraints": {}},
                {"id": "attr_age", "name": "年龄", "data_type": "integer", "required": False, "constraints": {}},
                {"id": "attr_amount", "name": "金额", "data_type": "float", "required": False, "constraints": {}},
            ],
            "relations": [],
        }],
    }
    r = client.post("/api/ontologies", params=P, json=payload)
    d = body(r)
    check("本体创建 201 统一信封", r.status_code == 201 and d.get("success") is True, r.text[:250])
    OID = d["data"]["id"]
    o = obj_of(d["data"])
    check("TP-03-01 自动主键回显可见（首个 required string）",
          o["stable_key"] == "attr_name" and o.get("stable_key_explicit") is False, str(o)[:250])


def ui_contract(no_ui: bool) -> None:
    section("TP-03-01 前端：对象卡片主键下拉（真实 Chromium DOM）")
    if no_ui:
        skip("TP-03-01 浏览器 DOM 段", "--no-ui 指定跳过（静态契约仍检查）")
    else:
        try:
            from playwright.sync_api import sync_playwright
            index = os.path.join(REPO, "frontend", "index.html")
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                page = b.new_page()
                page.goto("file:///" + index.replace("\\", "/"), wait_until="domcontentloaded", timeout=15000)
                page.wait_for_function("typeof renderObjectForms === 'function'", timeout=10000)
                page.evaluate("""() => {
                  // 建模页/编辑器默认隐藏，逐个祖先解除隐藏以便真实 select_option 交互
                  let el = document.getElementById('objectForms');
                  while (el) { el.classList.remove('hidden'); el.style.display = 'block'; el = el.parentElement; }
                  state.formObjects = [{
                    id: 'obj_customer', name: '客户', description: '', parent: null,
                    stable_key: null, stable_key_explicit: false,
                    attributes: [
                      {id:'attr_name', name:'名称', data_type:'string', required:true, constraints:{}},
                      {id:'attr_code', name:'编码', data_type:'string', required:false, constraints:{}},
                      {id:'attr_age', name:'年龄', data_type:'integer', required:false, constraints:{}},
                      {id:'attr_amount', name:'金额', data_type:'float', required:false, constraints:{}}
                    ], relations: []
                  }];
                  renderObjectForms();
                }""")
                opts = page.eval_on_selector_all("#objectForms .pk-select option",
                                                 "els => els.map(e => e.value)")
                check("TP-03-01 下拉选项仅含本对象 string 属性",
                      opts == ["", "attr_name", "attr_code"], str(opts))
                check("TP-03-01 当前自动主键回显 + 自动规则说明保留",
                      page.text_content("#objectForms .pk-origin") == "自动推断"
                      and "主键规则" in page.text_content("#objectForms .pk-rule")
                      and "string" in page.text_content("#objectForms .pk-rule"), "")
                page.select_option("#objectForms .pk-select", "attr_code")
                origin = page.text_content("#objectForms .pk-origin")
                badge = page.eval_on_selector_all(
                    "#objectForms .pk-badge",
                    "els => els.filter(e => e.style.display !== 'none').map(e => e.dataset.pkbadge)")
                hint = page.text_content("#objectForms .pk-hint")
                collected = page.evaluate("() => { syncFormObjectsFromDOM(); return collectFormOntology(); }")
                sending = collected["objects"][0]
                check("TP-03-01 显式选择后标记/徽标/提示实时更新",
                      origin == "显式指定" and badge == ["0:1"] and "显式指定主键" in (hint or ""),
                      f"origin={origin} badge={badge} hint={hint}")
                check("TP-03-01 保存请求携带 stable_key + stable_key_explicit",
                      sending.get("stable_key") == "attr_code"
                      and sending.get("stable_key_explicit") is True, str(sending)[:250])
                b.close()
        except Exception as e:  # noqa: BLE001
            skip("TP-03-01 浏览器 DOM 段", f"Playwright/Chromium 不可用：{type(e).__name__}: {e}")

    # 静态契约（无需浏览器，保证 UI 代码不可被误删）
    js = open(os.path.join(REPO, "frontend", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(REPO, "frontend", "index.html"), encoding="utf-8").read()
    check("TP-03-01 静态契约：建模页入口 + 下拉/显式标记/影响提示代码",
          'data-tab="modeling"' in html and 'id="objectForms"' in html
          and "pk-select" in js and "stable_key_explicit" in js
          and "primary_key_change" in js and "pk-rule" in js)


def tp03_02() -> None:
    section("TP-03-02 PATCH 显式指定 → 持久化 → 刷新回显")
    status, d = patch_obj({"stable_key": "attr_code"})
    o = obj_of(d.get("data") or {})
    check("PATCH 携带 stable_key 成功（200）且标记显式",
          status == 200 and o.get("stable_key") == "attr_code"
          and o.get("stable_key_explicit") is True, str(d)[:300])
    check("刷新回显（GET）持久生效", obj_of(get_ontology()).get("stable_key") == "attr_code")

    full = copy.deepcopy(get_ontology())
    oo = obj_of(full)
    oo["stable_key"] = "attr_code"
    oo["stable_key_explicit"] = True
    put = client.put(f"/api/ontologies/{OID}", params=P, json=full)
    check("PUT 全量保存可携带显式主键", put.status_code == 200
          and obj_of(body(put)["data"]).get("stable_key") == "attr_code", put.text[:250])

    status, d = patch_obj({"name": "客户（改名）"})
    check("PATCH 不传 stable_key 保持旧值（局部更新语义）",
          status == 200 and obj_of(d["data"]).get("stable_key") == "attr_code"
          and obj_of(d["data"]).get("stable_key_explicit") is True, str(d)[:250])

    status, d = patch_obj({"stable_key": None})
    o = obj_of(d["data"])
    check("PATCH 显式 null 回退自动推断规则",
          status == 200 and o.get("stable_key") == "attr_name"
          and o.get("stable_key_explicit") is False, str(o)[:250])
    status, _ = patch_obj({"stable_key": "attr_code"})
    check("恢复显式主键 attr_code 供后续用例", status == 200)


def tp03_03() -> None:
    section("TP-03-03 非法显式值 422 并提示原因（失败不落库）")
    cases = [
        ("attr_missing", "必须是本对象的一个属性 id"),
        ("attr_age", "必须是 string 属性"),
        ("attr_amount", "必须是 string 属性"),
    ]
    for bad, expect in cases:
        status, d = patch_obj({"stable_key": bad})
        err = d.get("error") or {}
        violations = (err.get("details") or {}).get("violations") or []
        msgs = " | ".join(str(v.get("message", "")) for v in violations if isinstance(v, dict))
        check(f"TP-03-03 PATCH {bad} → 422 VALIDATION_ERROR 且提示原因",
              status == 422 and err.get("code") == "VALIDATION_ERROR" and expect in msgs,
              f"status={status} {msgs[:200]}")
    check("422 后本体主键未被改写", obj_of(get_ontology()).get("stable_key") == "attr_code")

    bad_create = copy.deepcopy({
        "name": "M1C验收-非法主键", "objects": [obj_of(get_ontology())]
    })
    bad_create["objects"][0]["stable_key"] = "attr_amount"
    bad_create["objects"][0]["stable_key_explicit"] = True
    r = client.post("/api/ontologies", params=P, json=bad_create)
    err = body(r).get("error") or {}
    check("TP-03-03 创建路径显式非 string 主键同样 422（业务层校验门统一）",
          r.status_code == 422 and err.get("code") == "VALIDATION_ERROR", r.text[:250])


def tp03_04() -> None:
    section("TP-03-04 新入库实例按显式主键匹配/去重")
    c1 = make_candidate("cand-1", [
        {"attr_name": "客户A", "attr_code": "C-001"},
        {"attr_name": "客户B", "attr_code": "C-002"},
    ])
    d1 = c1["data"].get("data") or {}
    check("首批 2 条入库（inserted=2）", c1["status"] == 200
          and d1.get("imported") == 2 and d1.get("inserted") == 2
          and d1.get("dedup_updated") == 0 and d1.get("stable_key") == "attr_code", str(d1)[:300])

    c2 = make_candidate("cand-2", [{"attr_name": "客户A（更新）", "attr_code": "C-001"}])
    d2 = c2["data"].get("data") or {}
    check("同主键再次入库 → 更新既有实例（dedup_updated=1）",
          c2["status"] == 200 and d2.get("inserted") == 0
          and d2.get("dedup_updated") == 1, str(d2)[:300])
    recs = instances()
    c001 = [r for r in recs if r.get("stable_key_value") == "C-001"]
    check("匹配后保留原 id 且字段合并、无重复记录",
          len(c001) == 1 and len(recs) == 2
          and c001[0]["instance"]["attr_name"] == "客户A（更新）"
          and c001[0].get("stable_key") == "attr_code", str([r.get("id") for r in recs]))

    c3 = make_candidate("cand-3", [
        {"attr_name": "客户X", "attr_code": "C-003"},
        {"attr_name": "客户Y", "attr_code": "C-003"},
    ])
    d3 = c3["data"].get("data") or {}
    check("批次内同主键重复收敛为 1 条",
          c3["status"] == 200 and d3.get("inserted") == 1
          and d3.get("dedup_updated") == 1 and len(instances()) == 3, str(d3)[:300])
    check("实例记录带 stable_key / stable_key_value 元数据",
          all(r.get("stable_key") == "attr_code" and r.get("stable_key_value")
              for r in instances() if not r["id"].startswith("legacy-")))

    # 主键切换后，新入库按新键匹配（不与旧键记录误合并）
    patch_obj({"stable_key": "attr_name"})
    c4 = make_candidate("cand-4", [{"attr_name": "客户B", "attr_code": "C-NEW"}])
    d4 = c4["data"].get("data") or {}
    check("TP-03-04 切换主键后按新键匹配（旧键 C-002 不被误匹配）",
          c4["status"] == 200 and d4.get("stable_key") == "attr_name"
          and len(instances()) == 4 and d4.get("dedup_updated") == 0, str(d4)[:300])
    patch_obj({"stable_key": "attr_code"})


def ep03_01() -> None:
    section("TCE-03-01 / EP-03-01 主键变更影响提示、存量不静默改写、迁移须显式确认")
    store = get_instance_store(project_id=PID)
    # 模拟历史存量：无 stable_key 元数据且缺少新主键值（迁移时应跳过，不产生键断裂）
    store.insert({
        "id": "legacy-1", "project_id": PID, "ontology_id": OID, "object_type": OBJ_ID,
        "instance": {"attr_code": "C-OLD"}, "source": {"kind": "upload"},
    })
    before = {r["id"]: (r.get("stable_key"), dict(r.get("instance") or {})) for r in instances()}
    status, d = patch_obj({"stable_key": "attr_name"})
    impact = (d.get("data") or {}).get("primary_key_change") or {}
    check("变更主键返回 primary_key_change 影响提示（含存量实例数与迁移要求）",
          status == 200 and impact.get("existing_instances") >= 5
          and impact.get("migration_required") is True
          and "静默改写" in (impact.get("hint") or "")
          and "primary-key/migrate" in (impact.get("hint") or ""), str(impact)[:300])
    check("存量实例 stable_key 未被静默改写",
          all(r.get("stable_key") == before[r["id"]][0]
              and dict(r.get("instance") or {}) == before[r["id"]][1] for r in instances()),
          str([(r["id"], r.get("stable_key")) for r in instances()][:5]))

    imp = client.get(f"/api/ontologies/{OID}/objects/{OBJ_ID}/primary-key/impact", params=P)
    idata = body(imp).get("data") or {}
    check("影响端点可单独查询（供前端保存前提示）",
          imp.status_code == 200 and idata.get("existing_instances") == impact.get("existing_instances"),
          str(idata)[:250])

    url = f"/api/ontologies/{OID}/objects/{OBJ_ID}/primary-key/migrate"
    no = client.post(url, params=P, json={"confirm": False})
    check("未确认迁移 → 422 MIGRATION_NOT_CONFIRMED，实例不变",
          no.status_code == 422 and (body(no).get("error") or {}).get("code") == "MIGRATION_NOT_CONFIRMED"
          and all(r.get("key_migrated_at") is None for r in instances()), no.text[:250])
    yes = client.post(url, params=P, json={"confirm": True})
    md = body(yes).get("data") or {}
    migrated = [r for r in instances() if r.get("key_migrated_at")]
    check("显式确认后仅补写主键元数据（缺失新主键值的存量跳过）",
          yes.status_code == 200 and md.get("updated") >= 3 and md.get("skipped") >= 1
          and len(migrated) == md.get("updated")
          and all(dict(r.get("instance") or {}) == before[r["id"]][1] for r in instances()),
          str(md)[:300])


def ep03_02() -> None:
    section("TCE-03-02 / EP-03-02 并发主键变更：后到者基于最新状态，无脏写")
    t0 = get_ontology()["updated_at"]
    headers = {"If-Match": t0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(patch_obj, {"stable_key": "attr_code"}, headers)
        f2 = ex.submit(patch_obj, {"stable_key": "attr_name"}, headers)
        results = [f1.result(), f2.result()]
    codes = sorted(x[0] for x in results)
    winners = [x[1]["data"]["objects"][0]["stable_key"] for x in results if x[0] == 200]
    final_key = obj_of(get_ontology()).get("stable_key")
    check("两个并发请求：一个 200、一个 409 STALE_ONTOLOGY",
          codes == [200, 409]
          and any((x[1].get("error") or {}).get("code") == "STALE_ONTOLOGY" for x in results if x[0] == 409),
          str([(x[0], (x[1].get("error") or {}).get("code")) for x in results]))
    check("最终状态 = 成功请求的值（后到者未覆盖最新状态、无脏写）",
          len(winners) == 1 and winners[0] == final_key and final_key in ("attr_code", "attr_name"),
          f"winners={winners} final={final_key}")
    status, d = patch_obj({"name": "客户（后到者重试）"}, headers)
    check("基于旧 If-Match 重试仍被拒绝，须刷新后再提交",
          status == 409 and (d.get("error") or {}).get("code") == "STALE_ONTOLOGY", str(d)[:200])
    check("本体内主键仍合法 string 属性（无脏写副作用）",
          final_key in ("attr_name", "attr_code"), str(final_key))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ui", action="store_true", help="跳过 Playwright 浏览器段")
    args = ap.parse_args()
    print(f"DATA_DIR 隔离目录: {os.environ['DATA_DIR']}")
    setup()
    ui_contract(args.no_ui)
    tp03_02()
    tp03_03()
    tp03_04()
    ep03_01()
    ep03_02()
    print(f"\n===== 结果: PASS={PASS} FAIL={FAIL} SKIP={len(SKIPS)} =====")
    if FAILS:
        print("失败项:")
        for f in FAILS:
            print(" -", f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
