"""ai_helper：供建模/逆向使用的 LLM 辅助函数（与 ingestion 的 _call_llm 解耦）。"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from config import get_settings
from core.responses import ApiError
from logging_setup import get_logger

logger = get_logger("ai_helper")


def call_llm(prompt: str, endpoint: str | None = None, model: str | None = None) -> str:
    """调用 OpenAI 兼容 /chat/completions 端点，返回 assistant content 字符串。"""
    url = (endpoint or get_settings().llm_endpoint or "http://localhost:11434/v1").rstrip("/") + "/chat/completions"
    body = {
        "model": model or get_settings().llm_model or "default",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raise ApiError("LLM_ERROR", f"LLM HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}", http_status=502)  # noqa: TRY401
    except Exception as e:
        raise ApiError("LLM_ERROR", f"LLM 调用失败: {e}", http_status=502)  # noqa: TRY401


def extract_json(text: str) -> Any:
    """从可能包含 markdown 的文本中提取 JSON。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        raise


ONTOLOGY_GENERATION_PROMPT = """你是企业业务本体建模专家。请根据以下业务描述，生成一个结构化的本体定义。

业务描述：
{text}

请只返回如下 JSON 格式，不要返回任何解释或 markdown 代码块外的内容：
{
  "name": "本体名称",
  "description": "简短描述",
  "objects": [
    {
      "id": "obj_xxx",
      "name": "对象中文名",
      "attributes": [
        {"id": "attr_xxx", "name": "属性中文名", "data_type": "string", "required": true, "constraints": {}}
      ],
      "relations": [
        {"id": "rel_xxx", "name": "关系中文名", "target_object": "目标对象id", "cardinality": "many_to_one"}
      ]
    }
  ],
  "constraints": []
}

规则：
1. id 使用小写下划线命名，全局唯一（建议以 obj_/attr_/rel_ 前缀区分）。
2. data_type 只能是 string / integer / float / boolean / date 之一。
3. required 为布尔值；constraints 如不确定可留空对象 {}。
4. cardinality 只能是 one_to_one / one_to_many / many_to_one / many_to_many 之一。
5. 尽量覆盖描述中所有核心业务概念。
"""


def generate_ontology_from_text(text: str, endpoint: str | None = None, model: str | None = None, fallback: bool = True) -> dict[str, Any]:
    prompt = ONTOLOGY_GENERATION_PROMPT.replace("{text}", text[:12000])
    try:
        content = call_llm(prompt, endpoint, model)
        return extract_json(content)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 调用失败，使用确定性 fallback 抽取: %s", e)
        if not fallback:
            raise
        return deterministic_extract_ontology(text)


def generate_ontology_from_document(text: str, endpoint: str | None = None, model: str | None = None) -> dict[str, Any]:
    return generate_ontology_from_text(text, endpoint, model, fallback=True)


# ---- 确定性 fallback：LLM 不可达时从关键词生成最小可用本体 ----

FALLBACK_TEMPLATES: dict[str, dict[str, Any]] = {
    "客户": {
        "attributes": [
            {"id": "attr_name", "name": "名称", "data_type": "string", "required": True, "constraints": {}},
            {"id": "attr_phone", "name": "电话", "data_type": "string", "required": False, "constraints": {}},
        ],
        "relations": [{"id": "rel_orders", "name": "拥有订单", "target_object": "obj_order", "cardinality": "one_to_many"}],
    },
    "订单": {
        "attributes": [
            {"id": "attr_no", "name": "订单号", "data_type": "string", "required": True, "constraints": {}},
            {"id": "attr_amount", "name": "金额", "data_type": "float", "required": True, "constraints": {"minimum": 0}},
        ],
        "relations": [
            {"id": "rel_customer", "name": "所属客户", "target_object": "obj_customer", "cardinality": "many_to_one"},
            {"id": "rel_items", "name": "包含商品", "target_object": "obj_product", "cardinality": "one_to_many"},
        ],
    },
    "商品": {
        "attributes": [
            {"id": "attr_title", "name": "标题", "data_type": "string", "required": True, "constraints": {}},
            {"id": "attr_price", "name": "价格", "data_type": "float", "required": True, "constraints": {"minimum": 0}},
        ],
        "relations": [],
    },
    "SKU": {
        "attributes": [
            {"id": "attr_code", "name": "编码", "data_type": "string", "required": True, "constraints": {}},
            {"id": "attr_stock", "name": "库存", "data_type": "integer", "required": True, "constraints": {"minimum": 0}},
        ],
        "relations": [{"id": "rel_product", "name": "所属商品", "target_object": "obj_product", "cardinality": "many_to_one"}],
    },
}


def deterministic_extract_ontology(text: str) -> dict[str, Any]:
    """当 LLM 不可达时，按关键词从文本中抽取常见业务实体，生成最小本体。"""
    objects: list[dict[str, Any]] = []
    seen: set[str] = set()
    for keyword, tmpl in FALLBACK_TEMPLATES.items():
        if keyword in text and keyword not in seen:
            obj_id = "obj_" + keyword
            objects.append({
                "id": obj_id,
                "name": keyword,
                "attributes": tmpl["attributes"],
                "relations": [r for r in tmpl["relations"] if r["target_object"].replace("obj_", "") in text or True],
            })
            seen.add(keyword)
    # 至少保证有一个对象：按第一句话中的前两个名词硬拆
    if not objects:
        first = re.split(r"[，。、；]", text)[0]
        # 简单按常见 2 字/3 字名词硬切
        tokens = re.findall(r"[\u4e00-\u9fff]{2,4}", first)
        for i, tok in enumerate(tokens[:3]):
            objects.append({
                "id": f"obj_{i}",
                "name": tok,
                "attributes": [{"id": "attr_name", "name": "名称", "data_type": "string", "required": True, "constraints": {}}],
                "relations": [],
            })
    return {"name": "文档抽取本体", "description": text[:200], "objects": objects, "constraints": []}
