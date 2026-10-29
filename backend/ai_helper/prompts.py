"""提示词模板加载与渲染。"""
from __future__ import annotations

from ai_helper.config import get_llm_config


# 注意：渲染用 .replace("{text}", ...)（非 .format()），示例 JSON 的花括号必须写单括号。
# 硬性规则与后端 blocking_violations 的 error 级条目一一对应，缺任一条生成结果会被 422 拒收。
DEFAULT_ONTOLOGY_GENERATION_PROMPT = """你是企业业务本体建模专家。请把用户给出的业务描述建模为"业务对象类型"层本体，并严格遵守下列契约。

【只建模类型层，不建模实例】
只输出"对象类型/属性/关系"三类定义。禁止输出具体的人名、工号、部门编码、示例数据、实例清单，禁止出现 entities/instances/records/data 等字段。

【输出格式】只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释。结构必须严格如下（字段名不许增删改）：
{
  "name": "本体中文名称",
  "description": "一句话说明该本体覆盖的业务范围",
  "objects": [
    {
      "id": "obj_xxx",
      "name": "对象中文名",
      "business_definition": "一句话说明该对象在业务中代表什么、边界在哪（10~40字，必填）",
      "stable_key": "attr_xxx",
      "parent": null,
      "attributes": [
        {"id": "attr_xxx", "name": "属性中文名", "data_type": "string", "required": true, "constraints": {}}
      ],
      "relations": [
        {"id": "rel_xxx", "name": "关系中文名", "target_object": "obj_yyy", "cardinality": "many_to_one", "inverse_name": "反向关系名"}
      ]
    }
  ],
  "constraints": []
}

【硬性规则】（违反会被系统直接拒绝）
1. 顶层只允许 name、description、objects、constraints 四个字段。
2. objects 非空；每个对象必须含 id、name、business_definition、stable_key、attributes、relations 六项。
3. id 用小写字母+下划线，全局唯一：对象 obj_ 前缀、属性 attr_ 前缀、关系 rel_ 前缀。
4. data_type 只能是 string / integer / float / boolean / date / datetime 之一。
5. cardinality 只能是 one_to_one / one_to_many / many_to_one / many_to_many 之一。
6. relations 的 target_object 必须是 objects 中已存在的 id；同一对象内属性 id、关系 id 不得重复。
7. stable_key 必须是本对象 attributes 中某个 id，且该属性 required=true、data_type=string（如编码/名称类）。
8. 同一属性 id 被多个对象复用时 data_type 必须一致；建议属性 id 带对象语义前缀以保证全局唯一。
9. 属性 id、关系 id 禁止使用 password / secret / token / api_key / raw_key / key 等敏感字段名。
10. 规模上限：对象 3~12 个，每个对象属性 3~6 个、关系 0~4 个。constraints 固定为空数组。缩进用 2 空格，不要写长句。

【建模方法】
1. 先识别业务描述中的组织、岗位角色、业务单据、活动等名词作为候选对象。
2. 组织层级（部→处→岗位）用 parent 字段表达上下级，不要用关系重复表达同一层级；无上级填 null。
3. 岗位职责（AI专家、AIBP、AI接口人等）建成独立对象，用关系指向其所属组织对象。
4. 同类重复的事物只建一个对象类型：例如多个业务部门共用一种"AI接口人"岗位时，只建一个 AI接口人 对象，用"所属部门"关系指向部门对象，不要为每个部门各建一个同名对象。
5. 业务协作内容（技术支持、方案评审、协助开发等）若需要沉淀为管理对象，可建"协作事项"对象，并用关系关联提供方与需求方。

业务描述：
{text}
"""


def get_ontology_generation_prompt() -> str:
    cfg = get_llm_config()
    return cfg.get_prompt("ontology_generation") or DEFAULT_ONTOLOGY_GENERATION_PROMPT


def render_ontology_generation(text: str) -> str:
    return get_ontology_generation_prompt().replace("{text}", text[:12000])
