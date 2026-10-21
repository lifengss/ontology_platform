"""命令行：基于现有本体上传文档做增量增强。

用法示例：
    python scripts/enrich_ontology.py \
        --base-url http://localhost:8000 \
        --ontology-id <id> \
        --file docs/补充材料.docx \
        --project-id default \
        --auto-save \
        --instructions "重点关注供应商相关对象"

默认仅做 preview，加 --auto-save 才写回本体。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="本体增量增强命令行工具")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API 基地址")
    parser.add_argument("--ontology-id", required=True, help="目标本体 ID")
    parser.add_argument("--file", required=True, type=Path, help="待上传的 docx/xlsx/csv/txt/pdf")
    parser.add_argument("--project-id", default="default", help="项目 ID")
    parser.add_argument("--namespace", default=None, help="命名空间")
    parser.add_argument("--instructions", default=None, help="额外指示")
    parser.add_argument("--llm-endpoint", default=None, help="LLM 端点")
    parser.add_argument("--model", default=None, help="LLM 模型")
    parser.add_argument("--auto-save", action="store_true", help="直接保存增强结果")
    args = parser.parse_args()

    if not args.file.exists():
        print(f"文件不存在: {args.file}", file=sys.stderr)
        return 1

    url = f"{args.base_url.rstrip('/')}/api/ontologies/{args.ontology_id}/enrich"
    params: dict[str, str] = {"project_id": args.project_id}
    if args.namespace:
        params["namespace"] = args.namespace

    data: dict[str, str | bool] = {"auto_save": args.auto_save}
    if args.instructions:
        data["instructions"] = args.instructions
    if args.llm_endpoint:
        data["llm_endpoint"] = args.llm_endpoint
    if args.model:
        data["model"] = args.model

    with open(args.file, "rb") as f:
        files = {"file": (args.file.name, f, "application/octet-stream")}
        resp = requests.post(url, params=params, data=data, files=files, timeout=300)

    print(f"status: {resp.status_code}")
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        print(resp.text)
        return 1

    print(payload)
    if resp.status_code >= 400:
        return 1

    summary = payload.get("data", {}).get("summary", {})
    print(
        f"变更摘要: +对象 {len(summary.get('added_objects', []))}, "
        f"+属性 {len(summary.get('added_attributes', []))}, "
        f"+关系 {len(summary.get('added_relations', []))}, "
        f"更新对象 {len(summary.get('updated_objects', []))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
