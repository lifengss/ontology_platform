"""11 项修复全量端到端回归测试（API 层）。

覆盖：
  2.2  本体创建/更新业务层校验明细（details.violations）
  1.1  实例列表信封（GET /instances）
  1.2  单对象上下文导入（upload 带 object_type）
  1.3  多对象批量导入（upload 不带 object_type → 按 sheet 建多个候选）
  1.4  候选生命周期：batch-reject / revert / batch-approve / DELETE 规则
  3.1  业务影响推理（R1 直接引用 + R2 时间链传导）
  4.1  Action display-only 门（confirm 不外呼）
  5.1  MCP 按本体发布清单（publish 多本体 / 按本体下线 / status）
  5.2  MCP 多项目共存（全局 status）

运行：python scripts/e2e_regression_test.py
依赖：requests、pandas/openpyxl
"""
from __future__ import annotations

import io
import json
import sys
import uuid

import requests

BASE = "http://localhost:8000"
PASS = 0
FAIL = 0
FAILURES: list[str] = []


def api(method: str, path: str, *, project: str | None = None, **kwargs):
    if project is not None:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}project_id={project}"
    url = f"{BASE}{path}"
    kwargs.setdefault("timeout", 120)
    r = requests.request(method, url, **kwargs)
    try:
        data = r.json()
    except Exception:
        data = {"_raw": r.text[:500]}
    return r.status_code, data


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")
    return cond


def make_test_excel(sheets: dict[str, list[dict]]) -> tuple[str, bytes]:
    """生成多 sheet xlsx 字节流：{sheet名: rows}。sheet 名即对象 id（后端按 sheet 名匹配对象）。"""
    import pandas as pd
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet_name, rows in sheets.items():
            df = pd.DataFrame(rows)
            df.to_excel(writer, sheet_name=sheet_name, index=False)
    return "e2e_test.xlsx", buf.getvalue()


def create_ontology(project: str) -> dict:
    """Order(stable_key=order_code, 日期属性 approve_date) + PlanNode(N:1 关系 + due_date)。"""
    ontology = {
        "name": f"E2E回归_{uuid.uuid4().hex[:6]}",
        "description": "E2E regression test ontology",
        "namespace": f"e2e_{uuid.uuid4().hex[:6]}",
        "objects": [
            {
                "id": "Order",
                "name": "订单",
                "stable_key": "order_code",
                "attributes": [
                    {"id": "order_code", "name": "订单号", "type": "string", "required": True},
                    {"id": "order_name", "name": "订单名称", "type": "string"},
                    {"id": "approve_date", "name": "审批日期", "type": "date"},
                ],
                "relations": [],
            },
            {
                "id": "PlanNode",
                "name": "计划节点",
                "stable_key": "node_code",
                "attributes": [
                    {"id": "node_code", "name": "节点编号", "type": "string", "required": True},
                    {"id": "node_name", "name": "节点名称", "type": "string"},
                    {"id": "due_date", "name": "截止时间", "type": "date"},
                ],
                "relations": [
                    {"id": "belongs_to_order", "name": "所属订单", "target_object": "Order", "cardinality": "many_to_one"},
                ],
            },
        ],
    }
    status, data = api("POST", "/api/ontologies", project=project, json=ontology)
    assert status == 201, f"create ontology failed: {status} {data}"
    return data["data"]


def bad_ontology_payload() -> dict:
    """两个对象同 id（业务层校验应拦截）。"""
    return {
        "name": f"E2E非法本体_{uuid.uuid4().hex[:6]}",
        "description": "invalid: duplicate object id",
        "namespace": f"e2e_bad_{uuid.uuid4().hex[:6]}",
        "objects": [
            {"id": "Dup", "name": "对象1", "stable_key": "k",
             "attributes": [{"id": "k", "name": "键", "type": "string", "required": True}], "relations": []},
            {"id": "Dup", "name": "对象2", "stable_key": "k",
             "attributes": [{"id": "k", "name": "键", "type": "string", "required": True}], "relations": []},
        ],
    }


def preview_candidate(project: str, ont_id: str, object_type: str, records: list[dict], field_map: dict[str, str]) -> str:
    payload = {
        "ontology_id": ont_id,
        "object_type": object_type,
        "source": {"kind": "field_map", "records": records},
        "mapping": {"field_map": field_map, "source_ref": "e2e-regression"},
    }
    status, data = api("POST", "/api/ingest/preview", project=project, json=payload)
    assert status == 201, f"preview failed: {status} {data}"
    return data["data"]["id"]


def main():
    print("=" * 64)
    print("0. 健康检查（前置门禁：三个依赖服务必须全部可达，否则判测试不通过）")
    status, data = api("GET", "/health")
    if not check("health", status == 200, f"status={status}"):
        sys.exit(1)
    deps = (data.get("data") or {}).get("dependencies") or {}
    # 后端三依赖（Jena/Neo4j/Postgres）均已安装：启动后仍不可达即环境/启动失败。
    # 例外：仅「兜底降级逻辑」专项测试允许 degraded，本回归套件不允许。
    bad = {k: v for k, v in deps.items() if v != "healthy"}
    if not check("deps_all_reachable", not bad,
                 f"degraded deps={deps} —— 请先执行 scripts/start-deps.ps1 启动依赖服务"):
        sys.exit(1)

    # 独立测试项目（不影响现有数据；顺带作为 5.2 多项目共存的第二项目）
    status, data = api("POST", "/api/projects", json={"name": f"E2E回归项目_{uuid.uuid4().hex[:6]}", "description": "auto"})
    if not check("create_project", status in (200, 201), f"status={status} data={data}"):
        sys.exit(1)
    project_id = data["data"]["id"]
    print(f"  project_id={project_id}")

    # ------------------------------------------------------------------
    print("\n2.2 业务层校验明细（创建 + 更新路径）")
    status, data = api("POST", "/api/ontologies", project=project_id, json=bad_ontology_payload())
    check(
        "create_invalid_ontology_422",
        status == 422 and isinstance(data.get("error", {}).get("details", {}).get("violations"), list)
        and len(data["error"]["details"]["violations"]) > 0,
        f"status={status} data={json.dumps(data, ensure_ascii=False)[:200]}",
    )
    print(f"  violations={data.get('error', {}).get('details', {}).get('violations')}")

    ont = create_ontology(project_id)
    ont_id = ont["id"]
    bad_objects = [
        {"id": "Order", "name": "订单", "stable_key": "order_code",
         "attributes": [{"id": "order_code", "name": "订单号", "type": "string", "required": True}], "relations": []},
        {"id": "Order", "name": "订单重名", "stable_key": "order_code",
         "attributes": [{"id": "order_code", "name": "订单号", "type": "string", "required": True}], "relations": []},
    ]
    # update 需要完整 Ontology 体（schema 必填 name）
    bad_body = json.loads(json.dumps(ont))
    bad_body["objects"] = bad_objects
    status, data = api("PUT", f"/api/ontologies/{ont_id}", project=project_id, json=bad_body)
    check(
        "update_invalid_ontology_422",
        status == 422 and isinstance(data.get("error", {}).get("details", {}).get("violations"), list)
        and len(data["error"]["details"]["violations"]) > 0,
        f"status={status} data={json.dumps(data, ensure_ascii=False)[:200]}",
    )
    print(f"  violations={data.get('error', {}).get('details', {}).get('violations')}")
    good_body = json.loads(json.dumps(ont))
    good_body["description"] = "e2e updated"
    status, data = api("PUT", f"/api/ontologies/{ont_id}", project=project_id, json=good_body)
    check("update_valid_ok", status == 200 and data.get("data", {}).get("description") == "e2e updated", f"status={status}")

    # ------------------------------------------------------------------
    print("\n1.4 候选生命周期（reject/revert/approve/delete 规则）")
    order_fm = {"订单号": "order_code", "订单名称": "order_name", "审批日期": "approve_date"}
    c1 = preview_candidate(project_id, ont_id, "Order",
                           [{"订单号": "SO-A", "订单名称": "订单A", "审批日期": "2026-10-01"}], order_fm)
    c2 = preview_candidate(project_id, ont_id, "Order",
                           [{"订单号": "SO-B", "订单名称": "订单B", "审批日期": "2026-10-01"}], order_fm)
    c3 = preview_candidate(project_id, ont_id, "Order",
                           [{"订单号": "SO-C", "订单名称": "订单C", "审批日期": "2026-10-01"}], order_fm)
    check("preview_create_3_candidates", all([c1, c2, c3]), f"{c1} {c2} {c3}")

    status, data = api("POST", "/api/ingest/candidates/batch-reject", project=project_id,
                       json={"candidate_ids": [c1, c2]})
    check("batch_reject", status == 200 and data["data"]["affected"] == 2 and data["data"]["skipped"] == 0,
          f"status={status} data={data.get('data')}")

    status, data = api("POST", f"/api/ingest/candidates/{c1}/revert", project=project_id)
    check("revert_rejected_to_pending", status == 200 and data["data"]["status"] == "pending",
          f"status={status} data={data.get('data')}")

    status, data = api("POST", f"/api/ingest/candidates/{c1}/revert", project=project_id)
    check("revert_pending_forbidden", status == 403, f"status={status} data={data.get('error')}")

    status, data = api("DELETE", f"/api/ingest/candidates/{c1}", project=project_id)
    check("delete_pending_forbidden", status == 403, f"status={status} data={data.get('error')}")

    status, data = api("POST", "/api/ingest/candidates/batch-approve", project=project_id,
                       json={"candidate_ids": [c1, c2, c3]})
    d = data.get("data", {})
    check("batch_approve", status == 200 and d.get("affected") == 2 and d.get("skipped") == 1,
          f"status={status} data={d}")

    status, data = api("DELETE", "/api/ingest/candidates/batch", project=project_id,
                       json={"candidate_ids": [c2, c3, "nonexist"]})
    d = data.get("data", {})
    check("batch_delete", status == 200 and sorted(d.get("deleted", [])) == sorted([c2, c3])
          and len(d.get("skipped", [])) == 1, f"status={status} data={d}")

    # ------------------------------------------------------------------
    print("\n1.1 实例列表信封")
    status, data = api("GET", f"/api/ingest/instances?ontology_id={ont_id}&pageSize=50", project=project_id)
    items = (data.get("data") or {}).get("items", [])
    check("instances_envelope", status == 200 and "items" in (data.get("data") or {}) and len(items) == 2,
          f"status={status} count={len(items)} data={json.dumps(data.get('data', {}), ensure_ascii=False)[:200]}")

    # ------------------------------------------------------------------
    print("\n1.2 单对象上下文导入（Excel + object_type）")
    fname, fbytes = make_test_excel({
        "Order": [
            {"order_code": "SO-EX-1", "order_name": "Excel订单1", "approve_date": "2026-10-05"},
            {"order_code": "SO-EX-2", "order_name": "Excel订单2", "approve_date": "2026-10-06"},
        ]
    })
    status, data = api("POST", f"/api/ingest/upload?ontology_id={ont_id}", project=project_id,
                       data={"object_type": "Order"},
                       files={"file": (fname, fbytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    d = data.get("data", {})
    up_cands = d.get("candidates") or []
    check("single_object_upload", status == 201 and d.get("total") == 1
          and up_cands and up_cands[0].get("object_type") == "Order" and len(up_cands[0].get("instances", [])) == 2,
          f"status={status} data={json.dumps(data, ensure_ascii=False)[:300]}")
    # 批准 → 实例入库（供 3.1 引用链使用）
    if up_cands:
        status, data = api("POST", f"/api/ingest/candidates/{up_cands[0]['id']}/approve", project=project_id)
        check("approve_uploaded_order", status == 200 and data["data"].get("imported") == 2,
              f"status={status} data={data.get('data')}")

    # ------------------------------------------------------------------
    print("\n1.3 多对象批量导入（不带 object_type，按 sheet 建候选）")
    fname2, fbytes2 = make_test_excel({
        "Order": [{"order_code": "SO-BAT-1", "order_name": "批量订单", "approve_date": "2026-10-07"}],
        "PlanNode": [{"node_code": "PN-BAT-1", "node_name": "批量节点", "due_date": "2026-10-25", "belongs_to_order": "SO-EX-1"}],
    })
    status, data = api("POST", f"/api/ingest/upload?ontology_id={ont_id}", project=project_id,
                       files={"file": (fname2, fbytes2, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    d = data.get("data", {})
    up2 = d.get("candidates") or []
    types = sorted(x.get("object_type", "") for x in up2)
    check("global_upload_multi_candidate", status == 201 and d.get("total") == 2 and types == ["Order", "PlanNode"],
          f"status={status} types={types} data={json.dumps(data, ensure_ascii=False)[:300]}")

    # ------------------------------------------------------------------
    print("\n3.1 业务影响推理（R1 直接引用 + R2 时间链传导）")
    node_fm = {"node_code": "node_code", "node_name": "node_name", "due_date": "due_date", "belongs_to_order": "belongs_to_order"}
    pn_cand = preview_candidate(project_id, ont_id, "PlanNode", [
        {"node_code": "PN-1", "node_name": "生产准备", "due_date": "2026-10-20", "belongs_to_order": "SO-EX-1"},
        {"node_code": "PN-2", "node_name": "独立节点", "due_date": "2026-11-01"},
    ], node_fm)
    status, data = api("POST", f"/api/ingest/candidates/{pn_cand}/approve", project=project_id)
    check("approve_plan_nodes", status == 200 and data["data"].get("imported") == 2,
          f"status={status} data={data.get('data')}")

    status, data = api("GET", f"/api/ingest/instances?ontology_id={ont_id}&pageSize=100", project=project_id)
    items = (data.get("data") or {}).get("items", [])
    so1 = next((i for i in items if (i.get("instance") or i.get("props") or {}).get("order_code") == "SO-EX-1"), None)
    check("find_source_instance", so1 is not None, f"items={len(items)}")
    if so1:
        status, data = api("POST", "/api/inference/impact", project=project_id, json={
            "ontology_id": ont_id, "instance_id": so1["id"],
            "attribute": "approve_date", "new_value": "2026-10-08",  # 2026-10-05 → +3 天
        })
        d = data.get("data", {})
        impacted = d.get("impacted", [])
        hit = next((x for x in impacted if x.get("object_type") == "PlanNode" and x.get("label") == "生产准备"), None)
        check("impact_api_ok", status == 200, f"status={status} data={json.dumps(data, ensure_ascii=False)[:200]}")
        check("impact_direct_ref", hit is not None,
              f"impacted={[x.get('label') for x in impacted]}")
        if hit:
            inc = hit.get("inferred_changes", [])
            check("impact_time_chain", any(c.get("attribute") == "due_date" and c.get("inferred") == "2026-10-23" for c in inc),
                  f"inferred={inc}")
            check("impact_reason", "直接引用" in (hit.get("reason") or ""), f"reason={hit.get('reason')}")
        check("impact_explanation", "影响" in (d.get("explanation") or ""), f"explanation={d.get('explanation')}")
        print(f"  explanation={d.get('explanation')}")

    status, data = api("POST", "/api/inference/impact", project=project_id, json={"ontology_id": ont_id})
    check("impact_param_422", status == 422, f"status={status}")

    # ------------------------------------------------------------------
    print("\n4.1 Action display-only 门")
    action_payload = {
        "name": "E2E mock 回写",
        "target_connection": "",
        "invoke": {"kind": "webhook", "url": "http://localhost:9999/should-not-be-called", "method": "POST"},
        "input_map": {"code": "$.code"},
        "idempotency_key": "e2e::{code}",
        "confirmation": "human_required",
    }
    status, data = api("POST", "/api/actions", project=project_id, json=action_payload)
    check("create_action", status == 201, f"status={status} data={json.dumps(data, ensure_ascii=False)[:200]}")
    action_id = (data.get("data") or {}).get("action_id")
    status, data = api("POST", f"/api/actions/{action_id}/execute", project=project_id,
                       json={"input": {"code": "E2E-1"}})
    token = (data.get("data") or {}).get("confirmation_token")
    check("action_execute_token", status == 200 and bool(token), f"status={status} data={json.dumps(data, ensure_ascii=False)[:200]}")
    status, data = api("POST", f"/api/actions/{action_id}/confirm", project=project_id,
                       json={"confirmation_token": token})
    ext = (data.get("data") or {}).get("external_response") or {}
    check("confirm_display_only", status == 200 and ext.get("display_only") is True,
          f"status={status} ext={ext}")
    print(f"  external_response={ext}")

    # ------------------------------------------------------------------
    print("\n5.1/5.2 MCP 按本体发布清单 + 多项目共存")
    ont2 = create_ontology(project_id)
    status, data = api("POST", f"/api/mcp/server/publish?ontology_id={ont_id}", project=project_id, json={})
    check("mcp_publish_1", status == 200 and (data.get("data") or {}).get("published") is True,
          f"status={status} data={data.get('data')}")
    status, data = api("POST", f"/api/mcp/server/publish?ontology_id={ont2['id']}", project=project_id, json={})
    d = data.get("data", {})
    check("mcp_publish_2", status == 200 and len(d.get("published_ontologies", [])) == 2,
          f"status={status} data={json.dumps(d, ensure_ascii=False)[:300]}")

    status, data = api("GET", "/api/mcp/server/status", project=project_id)
    d = data.get("data", {})
    check("mcp_status_ontology_list", d.get("published") is True
          and len(d.get("published_ontologies", [])) == 2
          and d.get("endpoint") == f"/mcp/{project_id}",
          f"data={json.dumps(d, ensure_ascii=False)[:300]}")

    # 5.2 多项目共存：对 default 项目发布一个本体（若存在），全局 status 应列出多项目
    st, proj_data = api("GET", "/api/ontologies?page=1&pageSize=1")
    default_ont = ((proj_data.get("data") or {}).get("items") or [{}])[0]
    default_pid = default_ont.get("project_id") or "default"
    coexist_count = 1
    if default_ont.get("id"):
        status, data = api("POST", f"/api/mcp/server/publish?ontology_id={default_ont['id']}",
                           project=default_pid, json={})
        if status == 200:
            coexist_count = 2
    status, data = api("GET", "/api/mcp/server/status")  # 全局（不带 project_id）
    d = data.get("data", {})
    pids = [p["project_id"] for p in d.get("published_projects", [])]
    check("mcp_multi_project_coexist", project_id in pids and len(pids) == coexist_count,
          f"pids={pids} expect_coexist={coexist_count}")
    print(f"  published_projects={json.dumps(d.get('published_projects'), ensure_ascii=False)[:400]}")

    # 5.1 按本体下线
    status, data = api("POST", f"/api/mcp/server/unpublish?ontology_id={ont2['id']}", project=project_id, json={})
    status, data = api("GET", "/api/mcp/server/status", project=project_id)
    d = data.get("data", {})
    check("mcp_unpublish_one_ontology", d.get("published") is True and len(d.get("published_ontologies", [])) == 1,
          f"data={json.dumps(d, ensure_ascii=False)[:300]}")
    status, data = api("POST", "/api/mcp/server/unpublish", project=project_id, json={})
    status, data = api("GET", "/api/mcp/server/status", project=project_id)
    check("mcp_unpublish_project", (data.get("data") or {}).get("published") is False,
          f"data={data.get('data')}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 64)
    print(f"结果：PASS={PASS} FAIL={FAIL}")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
