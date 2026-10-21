"""手工解析 B0 三 sheet 构造标准本体，然后跑通后续流程。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests

BASE = "http://localhost:8000"
CONTEST = Path(r"D:/inspur/智能化转型部/运营类/黑客松/赛题池/L6采购计划流程本体建模/赛题一-采购计划制定流程赛题数据包")


def api(method: str, path: str, **kwargs):
    kwargs.setdefault("timeout", 30)
    r = requests.request(method, f"{BASE}{path}", **kwargs)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text}


def build_ontology_from_b0():
    b0 = CONTEST / "01_样例数据/输出样例与模板/B0_参考本体_采购计划.xlsx"
    xls = pd.ExcelFile(b0)
    sheets = {s: pd.read_excel(xls, sheet_name=s) for s in xls.sheet_names}

    # 1. 类层次
    df_cls = sheets["1-类层次"]
    classes = {}
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

    # 2. 对象属性（关系）
    df_rel = sheets["2-对象属性(关系)"]
    rels_by_domain: dict[str, list] = {}
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

    # 3. 数据属性
    df_attr = sheets["3-数据属性"]
    attrs_by_class: dict[str, list] = {}
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

    # 组装
    objects = []
    for cls_name, obj in classes.items():
        obj["relations"] = rels_by_domain.get(cls_name, [])
        obj["attributes"] = attrs_by_class.get(cls_name, [])
        # 父类不在同一本体中则清空（translator 目前只支持业务层 parent 引用对象 id）
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
    print("1. 从 B0 三 sheet 构造成标准本体 payload")
    payload = build_ontology_from_b0()
    print(f"   objects={len(payload['objects'])}")
    # 统计
    rel_count = sum(len(o["relations"]) for o in payload["objects"])
    attr_count = sum(len(o["attributes"]) for o in payload["objects"])
    print(f"   relations={rel_count} attributes={attr_count}")

    print("\n2. 创建本体（直接 POST，模拟人工修订后）")
    status, data = api("POST", "/api/ontologies", json=payload)
    if status != 201 or not data.get("success"):
        print("  FAIL", status, data)
        return
    oid = data["data"]["id"]
    print(f"   ontology_id={oid}")

    print("\n3. 直接创建的本体状态（PRD 期望 draft→publish；当前 create 默认 active，需后续修复）")
    status_get, data_get = api("GET", f"/api/ontologies/{oid}")
    current_status = data_get["data"].get("status") if data_get.get("success") else "unknown"
    print(f"   current status={current_status}")

    # 检查 Activity 属性
    activity_obj = next((o for o in payload["objects"] if o["id"] == "Activity"), None)
    print(f"   Activity attributes before patch: {[a['id'] for a in activity_obj['attributes']]}")

    # 模拟人工修订：把 A0 表列补齐到 Activity 属性
    a0_columns = ["activityCode", "activityName", "stageName", "leadingRole", "participantRole", "inputDoc", "outputDoc", "description"]
    a0_labels = {"activityCode": "活动编号", "activityName": "活动名称", "stageName": "阶段", "leadingRole": "主导角色",
                 "participantRole": "参与角色", "inputDoc": "输入", "outputDoc": "输出", "description": "描述"}
    existing_attr_ids = {a["id"] for a in activity_obj["attributes"]}
    for col in a0_columns:
        if col not in existing_attr_ids:
            activity_obj["attributes"].append({
                "id": col, "name": a0_labels[col], "data_type": "string", "required": False, "constraints": {}
            })
    # 需要重新 PUT 更新本体以体现人工修订
    print("\n4. 人工修订本体（补齐 Activity 属性）")
    status, data = api("PUT", f"/api/ontologies/{oid}", json=payload)
    print(f"   updated={data.get('success')}")

    print("\n5. 校验本体")
    status, data = api("POST", f"/api/ontologies/{oid}/validate", json={"level": "full"})
    print(f"   valid={data['data'].get('valid') if data.get('success') else False} violations={len(data['data'].get('violations', []))}")

    print("\n5. 字段映射归化 A0 活动清单 → Activity")
    a0 = CONTEST / "01_样例数据/核心基础数据集/A0_L6采购计划流程活动清单.xlsx"
    df = pd.read_excel(a0, dtype={"活动编号": str})
    raw_records = df.where(pd.notna(df), None).to_dict(orient="records")[:3]
    records = []
    for r in raw_records:
        clean = {}
        for k, v in r.items():
            if v is None:
                clean[k] = None
            elif isinstance(v, float):
                # 处理 pandas NaN
                if pd.isna(v):
                    clean[k] = None
                else:
                    clean[k] = v
            else:
                clean[k] = v
        records.append(clean)
    field_map = {
        "活动编号": "activityCode",
        "活动名称": "activityName",
        "阶段": "stageName",
        "主导角色": "leadingRole",
        "参与角色": "participantRole",
        "输入": "inputDoc",
        "输出": "outputDoc",
        "描述": "description",
    }
    ingest_payload = {
        "ontology_id": oid,
        "object_type": "Activity",
        "source": {"kind": "field_map", "records": records},
        "mapping": {"object_type": "Activity", "field_map": field_map},
    }
    status, data = api("POST", "/api/ingest/preview", json=ingest_payload)
    if not data.get("success"):
        print("  FAIL", status, data)
        return
    cid = data["data"]["id"]
    print(f"   candidate_id={cid} instances={len(data['data'].get('instances', []))}")

    print("\n6. 审批候选")
    status, data = api("POST", f"/api/ingest/candidates/{cid}/approve")
    if data.get("success"):
        print(f"   imported={data['data'].get('imported')} rejected={data['data'].get('rejected')} violations={data['data'].get('violations')}")
    else:
        print(f"  FAIL", status, data)

    print("\n7. 发布 MCP Server")
    status, data = api("POST", "/api/mcp/server/publish", params={"ontology_id": oid})
    print(f"   published={data['data'].get('published') if data.get('success') else False} tools={data['data'].get('tools') if data.get('success') else data}")

    print("\n8. Action dry-run")
    action_payload = {
        "name": "同步L6活动到外部",
        "target_connection": "",
        "invoke": {"kind": "webhook", "url": "http://localhost:9999/mock", "method": "POST"},
        "input_map": {"activityCode": "$.activityCode", "activityName": "$.activityName"},
        "idempotency_key": "activity::{activityCode}",
        "confirmation": "human_required",
    }
    status, data = api("POST", "/api/actions", json=action_payload)
    if not data.get("success"):
        print("  FAIL", status, data)
        return
    action_id = data["data"]["action_id"]
    status, data = api("POST", f"/api/actions/{action_id}/execute", json={"input": {"activityCode": "010", "activityName": "上传散件需求"}, "dry_run": True})
    print(f"   dry_run result={data['data'] if data.get('success') else data}")

    print("\n9. 导出本体 turtle")
    status, data = api("POST", f"/api/ontologies/{oid}/export", json={"format": "turtle"})
    if data.get("success"):
        print(f"   filename={data['data']['filename']} content_len={len(data['data']['content'])}")
    else:
        print("  FAIL", data)


if __name__ == "__main__":
    main()
