"""提示词模板加载与渲染。"""
from __future__ import annotations

from ai_helper.config import get_llm_config


DEFAULT_ONTOLOGY_GENERATION_PROMPT = """你是企业业务本体建模专家。请根据以下业务描述，生成一个结构化的本体定义。

业务描述：
{text}

请只返回如下 JSON 格式，不要返回任何解释或 markdown 代码块外的内容。必须严格使用以下字段和结构，禁止额外添加 ontology/classes/properties 等字段：

{{
  "name": "本体中文名称",
  "description": "本体的简短中文描述",
  "objects": [
    {{
      "id": "obj_xxx",
      "name": "对象中文名",
      "business_definition": "该对象的业务语义定义，50字以内",
      "attributes": [
        {{
          "id": "attr_xxx",
          "name": "属性中文名",
          "data_type": "string",
          "required": true,
          "constraints": {{}}
        }}
      ],
      "relations": [
        {{
          "id": "rel_xxx",
          "name": "关系中文名",
          "target_object": "目标对象id",
          "cardinality": "many_to_one"
        }}
      ]
    }}
  ],
  "constraints": []
}}

约束：
1. 必须且只能返回上述 JSON，顶层字段只能是 name、description、objects、constraints。禁止返回 ontology/classes/properties/objectProperties/dataProperties 等 OWL/其它格式。
2. objects 数组不能为空，必须覆盖业务描述中的核心业务实体（如流程阶段、角色、业务对象、单据、物料、供应商、计划等）。
3. 每个 object 必须有 id、name、business_definition、attributes、relations 五个字段。
4. id 使用小写下划线命名，全局唯一，建议以 obj_/attr_/rel_ 前缀区分。
5. data_type 只能是 string / integer / float / boolean / date 之一。
6. required 为布尔值；constraints 如不确定可留空对象 {{}}。
7. cardinality 只能是 one_to_one / one_to_many / many_to_one / many_to_many 之一。
8. target_object 必须指向 objects 中某个真实存在的 id。
9. 每个 object 至少包含一个可唯一标识该对象的属性（如 code/id/no/name），并在业务上作为 stable_key 的首选。
10. 属性应尽可能覆盖业务描述中提到的字段、状态、日期、编码等关键信息。
11. 关系应反映对象之间的真实业务关联，如“采购订单-供应商”、“采购计划-物料”、“角色-岗位”。
"""


def get_ontology_generation_prompt() -> str:
    cfg = get_llm_config()
    return cfg.get_prompt("ontology_generation") or DEFAULT_ONTOLOGY_GENERATION_PROMPT


def render_ontology_generation(text: str) -> str:
    return get_ontology_generation_prompt().replace("{text}", text[:12000])
