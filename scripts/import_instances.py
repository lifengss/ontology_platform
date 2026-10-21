"""命令行：上传 Excel/CSV 生成本体实例候选，并可选择自动审批落库。

用法示例：
    # 普通上传（按 sheet 名/文件名推断对象类型）
    python scripts/import_instances.py \
        --base-url http://localhost:8000 \
        --ontology-id <id> \
        --file data/instances.xlsx \
        --project-id default

    # 智能字段映射（LLM 推断）
    python scripts/import_instances.py \
        --base-url http://localhost:8000 \
        --ontology-id <id> \
        --file data/instances.xlsx \
        --smart \
        --auto-apply

    # 上传后自动审批落库
    python scripts/import_instances.py \
        --base-url http://localhost:8000 \
        --ontology-id <id> \
        --file data/instances.csv \
        --object-type obj_customer \
        --field-map '{"客户名称":"attr_name","行业":"attr_industry"}' \
        --approve
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests


def _call_approve(base_url: str, project_id: str, candidate_id: str) -> dict | None:
    url = f"{base_url.rstrip('/')}/api/ingest/candidates/{candidate_id}/approve"
    resp = requests.post(url, params={"project_id": project_id}, timeout=120)
    if resp.status_code != 200:
        print(f"审批失败 {resp.status_code}: {resp.text}", file=sys.stderr)
        return None
    return resp.json().get("data")


def main() -> int:
    parser = argparse.ArgumentParser(description="实例导入命令行工具")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API 基地址")
    parser.add_argument("--ontology-id", required=True, help="目标本体 ID")
    parser.add_argument("--object-type", default=None, help="目标对象类型（CSV 必填/Excel 可推断）")
    parser.add_argument("--file", required=True, type=Path, help="待上传的 .xlsx 或 .csv")
    parser.add_argument("--project-id", default="default", help="项目 ID")
    parser.add_argument("--field-map", default=None, help="显式字段映射 JSON，如 '{"表头":"attr_id"}'")
    parser.add_argument("--smart", action="store_true", help="使用 LLM 自动推断字段映射")
    parser.add_argument("--auto-apply", action="store_true", help="智能映射时直接生成候选（否则只返回映射）")
    parser.add_argument("--llm-endpoint", default=None, help="智能映射 LLM 端点")
    parser.add_argument("--model", default=None, help="智能映射 LLM 模型")
    parser.add_argument("--approve", action="store_true", help="生成候选后自动审批落库")
    args = parser.parse_args()

    if not args.file.exists():
        print(f"文件不存在: {args.file}", file=sys.stderr)
        return 1

    base_url = args.base_url.rstrip("/")
    params = {"ontology_id": args.ontology_id, "project_id": args.project_id}
    if args.object_type:
        params["object_type"] = args.object_type

    with open(args.file, "rb") as f:
        files = {"file": (args.file.name, f, "application/octet-stream")}
        if args.smart:
            url = f"{base_url}/api/ingest/upload-smart"
            data = {"auto_apply": args.auto_apply}
            if args.llm_endpoint:
                data["llm_endpoint"] = args.llm_endpoint
            if args.model:
                data["model"] = args.model
            resp = requests.post(url, params=params, data=data, files=files, timeout=300)
        else:
            url = f"{base_url}/api/ingest/upload"
            data: dict[str, str] = {}
            if args.field_map:
                data["field_map"] = args.field_map
            resp = requests.post(url, params=params, data=data, files=files, timeout=300)

    print(f"status: {resp.status_code}")
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        print(resp.text)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if resp.status_code >= 400:
        return 1

    data_section = payload.get("data", {})
    candidates = data_section.get("candidates", [])
    mappings = data_section.get("mappings", [])

    if args.smart and not args.auto_apply:
        print(f"智能映射结果共 {len(mappings)} 组，请确认后再带 --auto-apply 导入")
        return 0

    if not candidates:
        print("未生成候选实例", file=sys.stderr)
        return 1

    print(f"生成候选: {len(candidates)} 组")
    if not args.approve:
        return 0

    total_imported = 0
    for cand in candidates:
        cid = cand["id"]
        result = _call_approve(base_url, args.project_id, cid)
        if result is None:
            return 1
        print(f"candidate {cid}: imported={result.get('imported')}, rejected={result.get('rejected')}, violations={len(result.get('violations', []))}")
        total_imported += result.get("imported", 0)
        # 给日志/存储落盘留一点时间
        time.sleep(0.05)
    print(f"总计导入实例: {total_imported}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
