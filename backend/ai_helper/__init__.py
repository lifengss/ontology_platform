"""ai_helper：供建模/逆向使用的 LLM 辅助函数（与 ingestion 的 _call_llm 解耦）。"""
from __future__ import annotations

import json
import re
from typing import Any

from .client import call_llm, test_connection
from .config import LLMConfig, get_llm_config, reload_llm_config
from .prompts import render_ontology_generation
from logging_setup import get_logger
from modeling_translator import attr_uri, class_uri

logger = get_logger("ai_helper")


def _deterministic_nl_to_sparql(natural_language: str, ontology: dict[str, Any]) -> str:
    """LLM 不可达或返回非法时，按关键词生成一个合法的兜底 SPARQL。"""
    objects = ontology.get("objects", [])
    if not objects:
        return "SELECT ?s WHERE { ?s a <http://ontology.local/Thing> } LIMIT 10"

    q = natural_language.lower()
    # 常见业务关键词到对象 ID 的偏好映射
    keyword_object_prefs: dict[str, str] = {
        "阶段": "Stage",
        "stage": "Stage",
        "供应商": "Supplier",
        "supply": "Supplier",
        "订单": "PurchaseOrder",
        "order": "PurchaseOrder",
        "计划": "PurchasePlan",
        "plan": "PurchasePlan",
        "物料": "Material",
        "material": "Material",
        "库存": "Inventory",
        "inventory": "Inventory",
        "异常": "Exception",
        "exception": "Exception",
        "审批": "ApprovalRecord",
    }
    for kw, obj_id in keyword_object_prefs.items():
        if kw in q and any(o["id"] == obj_id for o in objects):
            target = next(o for o in objects if o["id"] == obj_id)
            break
    else:
        # 按对象名/ID 与问题中的关键词匹配度排序
        def _score(o: dict[str, Any]) -> int:
            score = 0
            for n in (o.get("name", ""), o["id"]):
                if not n:
                    continue
                n_lower = n.lower()
                # 对象名/ID 是否包含问题中的连续子串（如"订单"在"采购订单(PO)"中）
                # 简单按 2-4 字窗口匹配
                for length in range(min(4, len(q)), 1, -1):
                    for i in range(len(q) - length + 1):
                        if q[i:i + length] in n_lower:
                            score += length
            return score

        scored = sorted(objects, key=_score, reverse=True)
        target = scored[0] if _score(scored[0]) > 0 else objects[0]

    obj_uri = f"<{class_uri(target['id'])}>"
    attrs = target.get("attributes", [])
    var_list = "?s"
    triples = f"?s a {obj_uri} ."
    for i, a in enumerate(attrs[:3]):
        var = f"?v{i}"
        var_list += " " + var
        triples += f" ?s <{attr_uri(a['id'])}> {var} ."
    return f"SELECT {var_list} WHERE {{ {triples} }} LIMIT 50"


def _normalize_sparql(content: str) -> str:
    """清理 LLM 输出中的 markdown 围栏与前导声明（PREFIX/注释），返回规整后的查询文本。

    PREFIX 是合法 SPARQL 前缀声明（LLM 常按 prompt 的前缀约定输出），不算非法内容，
    执行侧 client._ensure_prefixes 会按实际情况补齐缺失声明。
    """
    code_match = re.search(r"```(?:sparql)?\s*([\s\S]*?)```", content, re.IGNORECASE)
    if code_match:
        content = code_match.group(1).strip()
    content = re.sub(r"^```(?:sparql)?\s*", "", content, flags=re.IGNORECASE)
    content = re.sub(r"```$", "", content)
    content = re.sub(r"^(?:\s*PREFIX\s+\S+:\s*<[^>]+>\s*)+", "", content, flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r"^(?:\s*#.*\n?)+", "", content)
    return content.strip()


def nl_to_sparql(natural_language: str, ontology: dict[str, Any]) -> tuple[str, str]:
    """自然语言 → SPARQL（O-05）。返回 (sparql, source)，source 为 "llm" 或 "fallback"。

    LLM 不可达/超时/返回非法时使用确定性 fallback（模板查询仅能近似回答，不能理解条件语义）。
    """
    objects = ontology.get("objects", [])
    lines = ["本体的业务对象与属性："]
    # 限制 prompt 长度，避免低端 LLM 过载不输出 content
    max_objects = 30
    for o in objects[:max_objects]:
        attrs = ", ".join(f"{a['id']}({a.get('name','')})" for a in o.get("attributes", []))
        lines.append(f"- {o['id']} ({o.get('name', '')}): {attrs}")
        rels = ", ".join(
            f"{r['id']}->{r.get('target_object')}"
            for r in o.get("relations", [])
            if r.get("target_object")
        )
        if rels:
            lines.append(f"  关系: {rels}")
    schema_text = "\n".join(lines)

    prompt = (
        "你是知识图谱查询专家。请根据下面的本体，把用户问题转成 SPARQL SELECT 查询。\n\n"
        f"{schema_text}\n\n"
        "前缀 ont: <http://ontology.local/> 已声明，直接使用即可，不要重复声明，不要使用其它前缀。\n"
        "URI 约定：\n"
        "- 对象类型：ont:{对象ID}（如 ont:Activity）\n"
        "- 属性属性：ont:{属性ID}（与上面列出的属性 ID 一致，不含对象前缀，如 ont:activityName）\n"
        "- 关系属性：ont:{关系ID}（如 ont:belongsToStage）\n"
        "- 实例主体：http://ontology.local/inst_{实例ID}，其 a 为对象类型 URI\n\n"
        "数据约定（务必遵守）：\n"
        "1. 属性值与关系值通常都是字面量（字符串/数字），例如 leadingRole 的值是字符串'采购经理'，"
        "不是角色实例 URI。禁止写 ?a ont:attrName ?x . ?x a ont:SomeClass 这种把属性值当节点连接的查询；"
        "问题含具体值条件（如'主导角色是采购经理'）时用 ?a ont:leadingRole \"采购经理\" 或 FILTER 表达。\n"
        "2. 只有查询两个对象实例之间的关联（如'活动属于哪个阶段'）时，才用关系属性连接两个节点："
        "?a ont:belongsToStage ?b . ?b a ont:Stage . 注意关系值也可能是字面量，不确定时优先单对象查询。\n"
        "3. 只使用上面本体列表中真实出现的对象 ID、属性 ID、关系 ID，不要编造。\n"
        "4. 查询末尾加 LIMIT 50（用户未明确指定数量时）。\n\n"
        f"用户问题：{natural_language}\n\n"
        "只输出 SPARQL SELECT 查询文本，不要解释，不要 markdown 代码块，不要 PREFIX 声明行。"
    )
    try:
        # NL 查询对实时性要求高，但 8s 实测会偶发读超时（同模型延迟 0.8~5s 波动），放宽到 20s 换取成功率
        result = call_llm(prompt, temperature=0.1, max_tokens=1024, timeout=20)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 不可达，nl_to_sparql 使用确定性 fallback: question=%r err=%s", natural_language, e)
        return _deterministic_nl_to_sparql(natural_language, ontology), "fallback"

    message = result.get("choices", [{}])[0].get("message") or {}
    content = message.get("content")
    # 某些 LLM 把思考过程放在 reasoning，不输出 content；此类输出不可信，直接 fallback
    if not content:
        logger.warning("LLM content 为空（可能仅有 reasoning），使用 deterministic fallback: question=%r", natural_language)
        return _deterministic_nl_to_sparql(natural_language, ontology), "fallback"

    content = _normalize_sparql(str(content))
    # 基础语法校验：必须以 SELECT 开头，且包含 WHERE { ... }（前导 PREFIX 已在 normalize 中剥离）
    if not content or not re.match(r"^SELECT\s+", content, re.IGNORECASE) or "{" not in content or "}" not in content:
        logger.warning("LLM 返回非法 SPARQL，使用 fallback: question=%r content=%.300s", natural_language, str(content))
        return _deterministic_nl_to_sparql(natural_language, ontology), "fallback"
    if not re.search(r"\bLIMIT\s+\d+", content, re.IGNORECASE):
        content += " LIMIT 50"
    logger.info("nl_to_sparql llm ok question=%r length=%d", natural_language, len(content))
    return content, "llm"


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


def generate_ontology_from_text(
    text: str,
    endpoint: str | None = None,
    model: str | None = None,
    fallback: bool = True,
) -> dict[str, Any]:
    prompt = render_ontology_generation(text)
    try:
        # 本体生成需要完整 JSON，不设置 max_tokens 避免被截断；使用较低温度提升稳定性
        result = call_llm(prompt, base_url=endpoint, model=model, temperature=0.1, max_tokens=8192)
        content = result["choices"][0]["message"]["content"]
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
    if not objects:
        first = re.split(r"[，。、；]", text)[0]
        tokens = re.findall(r"[\u4e00-\u9fff]{2,4}", first)
        for i, tok in enumerate(tokens[:3]):
            objects.append({
                "id": f"obj_{i}",
                "name": tok,
                "attributes": [{"id": "attr_name", "name": "名称", "data_type": "string", "required": True, "constraints": {}}],
                "relations": [],
            })
    return {"name": "文档抽取本体", "description": text[:200], "objects": objects, "constraints": []}


__all__ = [
    "call_llm",
    "test_connection",
    "extract_json",
    "generate_ontology_from_text",
    "generate_ontology_from_document",
    "nl_to_sparql",
    "deterministic_extract_ontology",
    "LLMConfig",
    "get_llm_config",
    "reload_llm_config",
]
