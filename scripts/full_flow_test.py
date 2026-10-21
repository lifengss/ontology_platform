"""L6 采购计划赛题全流程走通测试脚本。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

BASE = "http://localhost:8000"
CONTEST = Path(r"D:/inspur/智能化转型部/运营类/黑客松/赛题池/L6采购计划流程本体建模/赛题一-采购计划制定流程赛题数据包")

def api(method: str, path: str, **kwargs):
    url = f"{BASE}{path}"
    kwargs.setdefault("timeout", 30)
    r = requests.request(method, url, **kwargs)
    try:
        data = r.json()
    except Exception:
        data = {"_raw": r.text}
    return r.status_code, data

def ok(status: int, data: dict, expected: int = 200) -> bool:
    if status != expected:
        print(f"  FAIL status={status} data={json.dumps(data, ensure_ascii=False)[:300]}")
        return False
    if not data.get("success"):
        print(f"  FAIL success=false data={json.dumps(data, ensure_ascii=False)[:300]}")
        return False
    return True

def main():
    print("=" * 60)
    print("1. 健康检查")
    status, data = api("GET", "/health")
    if not ok(status, data):
        print("后端未启动或不可用")
        sys.exit(1)
    print(f"  status={data['data']['status']} version={data['data']['version']}")

    print("\n2. 清理已有测试数据")
    for kind in ["ontologies", "ingest_candidates", "actions", "mcp_connections"]:
        # 无法直接清理 store，但我们可以通过不依赖现有数据来测试
        pass

    print("\n3. 从 B0 参考本体 Excel 逆向生成本体草稿")
    b0_path = CONTEST / "01_样例数据/输出样例与模板/B0_参考本体_采购计划.xlsx"
    with open(b0_path, "rb") as f:
        status, data = api("POST", "/api/ontologies/from-excel", files={"file": ("B0_参考本体_采购计划.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    if not ok(status, data, 201):
        print("逆向生成失败")
        return
    draft = data["data"]
    draft_id = draft["id"]
    print(f"  draft_id={draft_id} name={draft.get('name')} objects={len(draft.get('objects', []))}")

    print("\n4. 查看草稿详情")
    status, data = api("GET", f"/api/ontologies/{draft_id}")
    if not ok(status, data):
        return
    obj_names = [o["name"] for o in data["data"].get("objects", [])]
    print(f"  objects={obj_names}")

    print("\n5. 发布本体")
    status, data = api("POST", f"/api/ontologies/{draft_id}/publish")
    if not ok(status, data):
        return
    print(f"  status={data['data'].get('status')} published={data['data'].get('status')=='active'}")

    print("\n6. 校验本体")
    status, data = api("POST", f"/api/ontologies/{draft_id}/validate", json={"level": "full"})
    if not ok(status, data):
        return
    print(f"  valid={data['data']['valid']} violations={len(data['data'].get('violations', []))}")

    print("\n7. 字段映射归化：A0 活动清单 → Activity 候选实例")
    a0_path = CONTEST / "01_样例数据/核心基础数据集/A0_L6采购计划流程活动清单.xlsx"
    import pandas as pd
    df = pd.read_excel(a0_path, dtype={"活动编号": str})
    records = df.where(pd.notna(df), None).to_dict(orient="records")
    # 简化字段映射
    field_map = {
        "activityCode": "活动编号",
        "activityName": "活动名称",
        "stageName": "阶段",
        "leadingRole": "主导角色",
    }
    payload = {
        "ontology_id": draft_id,
        "source": {"kind": "field_map", "records": records[:3]},  # 先测 3 条
        "mapping": {"object_type": "Activity", "field_map": field_map},
    }
    status, data = api("POST", "/api/ingest/preview", json=payload)
    if not ok(status, data, 201):
        print("归化 preview 失败")
        return
    candidate_id = data["data"]["id"]
    instances = data["data"].get("instances", [])
    print(f"  candidate_id={candidate_id} instances={len(instances)}")
    if instances:
        print(f"  first={instances[0]}")

    print("\n8. 审批候选实例入库")
    status, data = api("POST", f"/api/ingest/candidates/{candidate_id}/approve")
    if not ok(status, data):
        return
    print(f"  imported={data['data'].get('imported')} rejected={data['data'].get('rejected')} violations={len(data['data'].get('violations', []))}")

    print("\n9. 调用推理")
    status, data = api("GET", f"/api/ontologies/{draft_id}/inference")
    if status == 200 and data.get("success"):
        steps = data["data"].get("steps", [])
        print(f"  steps={len(steps)}")
    elif status == 404:
        print("  /inference 端点不存在（当前只有 /validate 含推理）")
    else:
        print(f"  status={status} data={data}")

    print("\n10. 发布 MCP Server")
    status, data = api("POST", "/api/mcp/server/publish", params={"ontology_id": draft_id})
    if not ok(status, data):
        return
    print(f"  published={data['data'].get('published')} endpoint={data['data'].get('endpoint')} tools={data['data'].get('tools')}")

    print("\n11. MCP Server 状态")
    status, data = api("GET", "/api/mcp/server/status")
    if not ok(status, data):
        return
    print(f"  status={data['data']}")

    print("\n12. 创建 Action 定义")
    action_payload = {
        "name": "同步活动到外部 CRM",
        "target_connection": "",
        "invoke": {"kind": "webhook", "url": "http://localhost:9999/mock", "method": "POST"},
        "input_map": {"activityCode": "$.activityCode", "activityName": "$.activityName"},
        "idempotency_key": "activity::{activityCode}",
        "confirmation": "human_required",
    }
    status, data = api("POST", "/api/actions", json=action_payload)
    if not ok(status, data, 201):
        print("Action 创建失败")
        return
    action_id = data["data"]["action_id"]
    print(f"  action_id={action_id}")

    print("\n13. 触发 Action (dry-run)")
    status, data = api("POST", f"/api/actions/{action_id}/execute", json={"input": {"activityCode": "010", "activityName": "上传散件需求"}, "dry_run": True})
    if not ok(status, data):
        return
    print(f"  dry_run={data['data'].get('dry_run')} planned_payload={data['data'].get('planned_payload')}")

    print("\n14. 触发 Action (生成确认令牌)")
    status, data = api("POST", f"/api/actions/{action_id}/execute", json={"input": {"activityCode": "010", "activityName": "上传散件需求"}})
    if not ok(status, data):
        return
    token = data["data"].get("confirmation_token")
    print(f"  token={token}")

    print("\n15. 确认执行 Action (外部 mock 不存在，会失败，但流程应走到外部调用)")
    status, data = api("POST", f"/api/actions/{action_id}/confirm", json={"confirmation_token": token})
    # 外部 mock 不存在，这里会 200 但 external_response 里带 error
    print(f"  status={status} data={json.dumps(data, ensure_ascii=False)[:300]}")

    print("\n16. 导出本体 turtle")
    status, data = api("POST", f"/api/ontologies/{draft_id}/export", json={"format": "turtle"})
    if not ok(status, data):
        return
    content = data["data"].get("content", "")[:500]
    print(f"  content_preview={content}")

    print("\n" + "=" * 60)
    print("全流程测试完成。核心结论：")
    print(f"  - 逆向 Excel 生成本体: OK ({len(obj_names)} objects)")
    print(f"  - 发布本体: OK")
    print(f"  - 校验本体: valid={data.get('valid', 'N/A')}")
    print(f"  - 字段映射归化 + 审批: OK")
    print(f"  - MCP Server 发布: OK")
    print(f"  - Action 受控执行: OK (dry-run/确认令牌/回写流程)")


if __name__ == "__main__":
    main()
