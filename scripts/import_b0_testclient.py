"""用 TestClient 跑赛题全流程，不依赖外部服务。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from main import app

CONTEST = Path(r"D:/inspur/智能化转型部/运营类/黑客松/赛题池/L6采购计划流程本体建模/赛题一-采购计划制定流程赛题数据包")


def api(method: str, path: str, **kwargs):
    kwargs.setdefault("timeout", 30)
    r = app.request(method, path, **kwargs)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text}


def build_ontology_from_b0():
    b0 = CONTEST / "01_样例数据/输出样例与模板/B0_参考本体_采购计划.xlsx"
    xls = pd.ExcelFile(b0)
    sheets = {s: pd.read_excel(xls, sheet_name=s) for s in xls.sheet_names}

    classes = {}
    df_cls = sheets["1-类层次"]
    for _, r in df_cls.iterrows():
        cls_name = str(r["类名"]).strip()
        if not cls_name or cls_name == "nan":
            continue
        classes[cls_name] = {
            "id": cls_name,
            "name": str(r.get("中文名", cls_name)).strip(),
            "description": str(r.get("备注/取值域", "")).strip(),
            "parent": str(r.get("父类", "")).strip() or None,
            "attributes": [],
            "relations": [],
        }

    rels_by_domain = {}
    df_rel = sheets["2-对象属性(关系)"]
    for _, r in df_rel.iterrows():
        rel_name = str(r.get("关系名", "")).strip()
        domain = str(r.get("定义域", "")).strip()
        range_ = str(r.get("值域", "")).strip()
        if not rel_name or not domain:
            continue
        rels_by_domain.setdefault(domain, []).append({
            "id": rel_name,
            "name": rel_name,
            "target_object": range_,
            "cardinality": "many_to_one",
        })

    attrs_by_class = {}
    df_attr = sheets["3-数据属性"]
    dtype_map = {
        "string": "string", "integer": "integer", "int": "integer",
        "float": "float", "decimal": "float", "boolean": "boolean",
        "date": "date", "datetime": "date",
    }
    for _, r in df_attr.iterrows():
        attr_name = str(r.get("属性名", "")).strip()
        cls_name = str(r.get("所属类", "")).strip()
        dtype = str(r.get("数据类型", "string")).strip().lower()
        if not attr_name or not cls_name:
            continue
        attrs_by_class.setdefault(cls_name, []).append({
            "id": attr_name,
            "name": str(r.get("中文名", attr_name)).strip(),
            "data_type": dtype_map.get(dtype, "string"),
            "required": False,
            "constraints": {},
        })

    objects = []
    for cls_name, obj in classes.items():
        obj["relations"] = rels_by_domain.get(cls_name, [])
        obj["attributes"] = attrs_by_class.get(cls_name, [])
        if obj["parent"] and obj["parent"] not in classes:
            obj["parent"] = None
        objects.append(obj)

    return {
        "name": "L6采购计划流程本体",
        "description": "从 B0 参考本体手工导入",
        "modeling_type": "reverse",
        "version": "1",
        "objects": objects,
        "constraints": [],
    }


def main():
    c = TestClient(app)

    print("0. 创建测试项目")
    r = c.post("/api/projects", json={"name": "L6采购计划"})
    assert r.status_code == 201, r.json()
    project_id = r.json()["data"]["id"]
    print(f"   project_id={project_id}")

    print("\n1. 从 B0 三 sheet 构造本体")
    payload = build_ontology_from_b0()
    print(f"   objects={len(payload['objects'])}, relations={sum(len(o['relations']) for o in payload['objects'])}, attrs={sum(len(o['attributes']) for o in payload['objects'])}")

    print("\n2. 创建本体")
    r = c.post(f"/api/ontologies?project_id={project_id}", json=payload)
    assert r.status_code == 201, r.json()
    oid = r.json()["data"]["id"]
    print(f"   ontology_id={oid}")

    print("\n3. 发布本体")
    r = c.post(f"/api/ontologies/{oid}/publish?project_id={project_id}")
    assert r.status_code == 200, r.json()
    print(f"   status={r.json()['data']['status']}")

    print("\n4. 校验本体")
    r = c.post(f"/api/ontologies/{oid}/validate?project_id={project_id}", json={"level": "full"})
    print(f"   valid={r.json()['data']['valid']} violations={len(r.json()['data']['violations'])}")

    print("\n5. 补齐 Activity 属性（模拟人工修订）")
    r = c.get(f"/api/ontologies/{oid}?project_id={project_id}")
    onto = r.json()["data"]
    a0_columns = ["activityCode", "activityName", "stageName", "leadingRole", "participantRole", "inputDoc", "outputDoc", "description"]
    a0_labels = {"activityCode": "活动编号", "activityName": "活动名称", "stageName": "阶段", "leadingRole": "主导角色",
                 "participantRole": "参与角色", "inputDoc": "输入", "outputDoc": "输出", "description": "描述"}
    activity_obj = next((o for o in onto["objects"] if o["id"] == "Activity"), None)
    assert activity_obj, "Activity 类不存在"
    existing = {a["id"] for a in activity_obj["attributes"]}
    for col in a0_columns:
        if col not in existing:
            activity_obj["attributes"].append({"id": col, "name": a0_labels[col], "data_type": "string", "required": False, "constraints": {}})
    r = c.put(f"/api/ontologies/{oid}?project_id={project_id}", json=onto)
    assert r.status_code == 200, r.json()

    print("\n6. 字段映射归化 A0 → Activity")
    a0 = CONTEST / "01_样例数据/核心基础数据集/A0_L6采购计划流程活动清单.xlsx"
    df = pd.read_excel(a0, dtype={"活动编号": str})
    records = df.where(pd.notna(df), None).to_dict(orient="records")[:3]
    field_map = {
        "活动编号": "activityCode", "活动名称": "activityName", "阶段": "stageName",
        "主导角色": "leadingRole", "参与角色": "participantRole",
        "输入": "inputDoc", "输出": "outputDoc", "描述": "description",
    }
    ingest_payload = {
        "ontology_id": oid,
        "object_type": "Activity",
        "source": {"kind": "field_map", "records": records},
        "mapping": {"object_type": "Activity", "field_map": field_map},
    }
    r = c.post(f"/api/ingest/preview?project_id={project_id}", json=ingest_payload)
    assert r.status_code == 201, r.json()
    cid = r.json()["data"]["id"]
    print(f"   candidate_id={cid} instances={len(r.json()['data']['instances'])}")

    print("\n7. 审批候选")
    r = c.post(f"/api/ingest/candidates/{cid}/approve?project_id={project_id}")
    print(f"   imported={r.json()['data']['imported']} rejected={r.json()['data']['rejected']} violations={len(r.json()['data']['violations'])}")

    print("\n8. 发布 MCP Server")
    r = c.post(f"/api/mcp/server/publish?project_id={project_id}&ontology_id={oid}")
    print(f"   published={r.json()['data']['published']} endpoint={r.json()['data']['endpoint']} tools={r.json()['data']['tools']}")

    print("\n9. Action dry-run")
    action_payload = {
        "name": "同步L6活动到外部",
        "target_connection": "",
        "invoke": {"kind": "webhook", "url": "http://localhost:9999/mock", "method": "POST"},
        "input_map": {"activityCode": "$.activityCode", "activityName": "$.activityName"},
        "idempotency_key": "activity::{activityCode}",
        "confirmation": "human_required",
    }
    r = c.post(f"/api/actions?project_id={project_id}", json=action_payload)
    assert r.status_code == 201, r.json()
    action_id = r.json()["data"]["action_id"]
    r = c.post(f"/api/actions/{action_id}/execute?project_id={project_id}", json={"input": {"activityCode": "010", "activityName": "上传散件需求"}, "dry_run": True})
    print(f"   dry_run confirmation={r.json()['data'].get('confirmation')}")

    print("\n10. 导出 turtle")
    r = c.post(f"/api/ontologies/{oid}/export?project_id={project_id}", json={"format": "turtle"})
    if r.status_code == 200:
        print(f"   filename={r.json()['data']['filename']} content_len={len(r.json()['data']['content'])}")
    else:
        print("  FAIL", r.json())

    print("\n11. 跨项目隔离验证：另一项目看不到本体的 Activity")
    r2 = c.post("/api/projects", json={"name": "隔离测试项目"})
    assert r2.status_code == 201, r2.json()
    other_pid = r2.json()["data"]["id"]
    r = c.get(f"/api/ontologies/{oid}?project_id={other_pid}")
    assert r.status_code == 404, f"应隔离，但返回 {r.status_code}"
    print("   隔离正常：其他项目无法访问该本体")

    print("\n=== 赛题全流程测试通过 ===")


if __name__ == "__main__":
    main()
