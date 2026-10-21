"""从 A0~A11 样例数据 Excel 中一次性抽取本体补充，合并到 deepseek 基础本体并发布。"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any

import openpyxl

DATA_DIR = Path("D:/inspur/智能化转型部/运营类/黑客松/赛题池/L6采购计划流程本体建模/赛题一-采购计划制定流程赛题数据包/01_样例数据/核心基础数据集")
BASE_ONTOLOGY_ID = "119a402fcb3c4fc686b5a6e2cbce923c"
PROJECT_ID = "bb4ad461eb13438aab65110c9c8c6dac"
API_BASE = "http://localhost:8000"


def api_get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def api_post(path: str, body: dict[str, Any], params: dict[str, str] | None = None) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, method="POST", data=data)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def load_base_ontology() -> dict[str, Any]:
    j = api_get(f"/api/ontologies/{BASE_ONTOLOGY_ID}", {"project_id": PROJECT_ID})
    return j["data"]


def excel_to_text(path: Path) -> str:
    wb = openpyxl.load_workbook(path, data_only=True)
    lines: list[str] = []
    for sheet in wb.worksheets:
        lines.append(f"### 文件：{path.name} / Sheet：{sheet.title}")
        rows = list(sheet.iter_rows(values_only=True))
        if rows:
            lines.append("表头：" + " | ".join(str(c) if c is not None else "" for c in rows[0]))
            lines.append("样例行（最多3行）：")
            for row in rows[1:4]:
                line = " | ".join(str(c) if c is not None else "" for c in row)
                if line.strip():
                    lines.append(line)
        lines.append("")
    return "\n".join(lines)


def build_prompt(base_objects: list[dict[str, Any]], excel_text: str) -> str:
    base_summary = "\n".join(
        f"- {o['id']}（{o['name']}）"
        for o in base_objects
    )
    return (
        "你是企业业务本体建模专家。现有基础本体和若干业务样例数据表格，"
        "请补充缺失的对象、属性、关系，输出一个更完整的 JSON 本体。\n\n"
        "要求：\n"
        "1. 必须且只能返回 JSON，顶层字段为 name, description, objects, constraints。\n"
        "2. objects 中每个对象必须有 id, name, business_definition, attributes, relations。\n"
        "3. id 必须使用小写英文字母+数字+下划线，形如 obj_purchase_order / attr_order_no / rel_order_contains_line，禁止中文、空格、obj_0/obj_1 等无意义 id。\n"
        "4. 优先复用下面列出的已有对象 id 和名称；只有样例数据中出现、但基础本体没有的对象才新建。\n"
        "5. data_type 只能是 string / integer / float / boolean / date 之一。\n"
        "6. cardinality 只能是 one_to_one / one_to_many / many_to_one / many_to_many 之一。\n"
        "7. 禁止把 prompt 中的文字、表头说明、或 Excel 文件名当作对象。\n\n"
        "基础本体对象（优先复用）：\n" + base_summary + "\n\n"
        "业务样例数据表格：\n" + excel_text + "\n\n"
        "请输出完整 JSON 本体："
    )


def call_from_text(text: str) -> dict[str, Any]:
    j = api_post("/api/ontologies/from-text", {"text": text}, {"project_id": PROJECT_ID})
    return j["data"]


def is_valid_object(obj: dict[str, Any]) -> bool:
    """过滤垃圾对象。"""
    name = obj.get("name", "")
    oid = obj.get("id", "")
    if not name or not oid:
        return False
    # 排除 prompt 碎片
    if any(k in name for k in ("以下是", "业务样", "例数据表", "文件：", "Sheet")):
        return False
    if " " in oid or any(ord(c) > 127 for c in oid):
        return False
    if not re.match(r"^[a-z][a-z0-9_]*$", oid):
        return False
    return True


def normalize_object_ids(ontology: dict[str, Any]) -> dict[str, Any]:
    """把不合规的对象 id 重新规范化；同时清理空对象。"""
    valid_objects = [o for o in ontology.get("objects", []) if is_valid_object(o)]
    ontology["objects"] = valid_objects
    return ontology


def save_as_draft(ontology: dict[str, Any]) -> dict[str, Any]:
    body = {
        "name": ontology.get("name", "L6采购计划完整本体"),
        "description": ontology.get("description", "从 Word 文档 + A0~A11 样例数据自动抽取合并"),
        "modeling_type": "reverse",
        "objects": ontology.get("objects", []),
        "constraints": ontology.get("constraints", []),
    }
    j = api_post("/api/ontologies", body, {"project_id": PROJECT_ID})
    return j["data"]


def publish_ontology(oid: str) -> dict[str, Any]:
    j = api_post(f"/api/ontologies/{oid}/publish", {}, {"project_id": PROJECT_ID})
    return j["data"]


def main() -> None:
    base = load_base_ontology()
    base_objects = base.get("objects", [])
    print(f"基础本体对象数: {len(base_objects)}")

    parts: list[str] = []
    for path in sorted(DATA_DIR.glob("*.xlsx")):
        parts.append(excel_to_text(path))
    excel_text = "\n".join(parts)

    prompt = build_prompt(base_objects, excel_text)
    print(f"\nPrompt 长度: {len(prompt)} 字符")
    print("正在调用 LLM 抽取补充本体...")
    supplement = call_from_text(prompt)
    supplement = normalize_object_ids(supplement)

    print(f"补充本体对象数: {len(supplement.get('objects', []))}")
    for obj in supplement.get("objects", []):
        print(f"  + {obj['id']} | {obj['name']}")

    # 简单合并：以 base 为主，把 supplement 里 base 没有的对象追加进去
    base_obj_ids = {o["id"] for o in base_objects}
    new_objects = [o for o in supplement.get("objects", []) if o["id"] not in base_obj_ids]
    merged = {**base}
    merged["objects"] = base_objects + new_objects

    print(f"\n合并后对象数: {len(merged['objects'])}")
    merged["name"] = "L6采购计划完整自动本体-v2"
    merged["description"] = "由 Word 说明材料 + A0~A11 样例数据经 LLM 一次性抽取合并生成"

    draft = save_as_draft(merged)
    print(f"\n已保存草稿: {draft['id']}")
    published = publish_ontology(draft["id"])
    print(f"已发布本体: {published['id']} / {published['name']}")
    print(f"最终对象数: {len(published.get('objects', []))}")
    for obj in published.get("objects", []):
        print(f"  - {obj['id']} | {obj['name']}")


if __name__ == "__main__":
    main()
