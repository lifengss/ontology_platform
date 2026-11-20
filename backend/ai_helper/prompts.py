"""提示词模板加载与渲染。"""
from __future__ import annotations

from ai_helper.config import get_llm_config


# 注意：渲染用 .replace("{text}", ...)（非 .format()），示例 JSON 的花括号必须写单括号。
# 硬性规则与后端 blocking_violations 的 error 级条目一一对应，缺任一条生成结果会被 422 拒收。
DEFAULT_ONTOLOGY_GENERATION_PROMPT = """你是企业业务本体建模专家。请把用户给出的业务描述建模为"业务对象类型"层本体。

【关键要求】请直接、紧凑地输出最终 JSON 结果，不要展开冗长思考，不要在字段外添加任何解释或代码块。对象数量按业务描述实际聚类取，通常 5~20 个对象（不要为了凑数硬造）。

【只建模类型层，不建模实例】
禁止输出具体的人名、工号、部门编码、示例数据、实例清单，禁止出现 entities/instances/records/data 等字段。

【输出格式】只输出一个 JSON 对象，结构严格如下（字段名不许增删改）：
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
  "constraints": [
    {"id": "uniq_xxx", "name": "唯一约束名", "scope": "obj_xxx", "rule": "unique", "params": {"attributes": ["attr_xxx"]}}
  ],
  "functions": [
    {"function_id": "fn_xxx", "name": "函数名", "expression": "公式表达式", "inputs": ["attr_xxx"], "output_type": "float", "side_effect": false}
  ]
}

【硬性规则】（违反会被系统直接拒绝）
1. 顶层只允许 name、description、objects、constraints、functions 五个字段。
2. objects 非空；每个对象必须含 id、name、business_definition、stable_key、attributes、relations 六项（对象内不要放 constraints/functions，它们放在顶层）。
3. id 用小写字母+下划线，全局唯一：对象 obj_ 前缀、属性 attr_ 前缀、关系 rel_ 前缀、唯一约束 uniq_ 前缀、函数 fn_ 前缀。
4. data_type 只能是 string / integer / float / boolean / date / datetime 之一。
5. cardinality 只能是 one_to_one / one_to_many / many_to_one / many_to_many 之一。
6. relations 的 target_object 必须是 objects 中已存在的 id；同一对象内属性 id、关系 id 不得重复。
7. stable_key 必须是本对象 attributes 中某个 id，且该属性 required=true、data_type=string（如编码/名称类）。
8. 同一属性 id 被多个对象复用时 data_type 必须一致；建议属性 id 带对象语义前缀以保证全局唯一。
9. 属性 id、关系 id 禁止使用 password / secret / token / api_key / raw_key / key 等敏感字段名。
10. 若业务描述含业务编码/编号等唯一标识，必须在顶层 constraints 用 rule="unique"（scope 指向对象 id，params.attributes 列出唯一属性 id）表达唯一约束；若业务描述含成本核算/计算公式（如"成本 = 单价 × 数量"、"总价 = 人工 + 备件 + 管理"），必须在顶层 functions 给出公式（expression 用属性 id 的算术/函数表达式，inputs 声明参与属性 id，output_type 为结果类型）。
11. 属性级 constraints 若含 enum 枚举，enum 必须为非空字符串数组（列出实际枚举值）；若不确定枚举值，就不要在 constraints 里写 enum 字段，避免空数组被拒绝。

【建模方法】直接按业务描述聚类对象，用 relations 表达对象归属，用 parent 表达组织层级；同类重复只建一个对象类型。
1. 识别业务描述中的组织、岗位角色、业务单据、活动等名词作为候选对象。
2. 成本核算/计算公式务必沉淀到顶层 functions；业务唯一标识（编码/编号）务必沉淀到顶层 constraints 的唯一约束。

业务描述：
{text}
"""


def get_ontology_generation_prompt() -> str:
    cfg = get_llm_config()
    return cfg.get_prompt("ontology_generation") or DEFAULT_ONTOLOGY_GENERATION_PROMPT


def render_ontology_generation(text: str) -> str:
    return get_ontology_generation_prompt().replace("{text}", text[:12000])


# ---------------------------------------------------------------------------
# 能力（Capability）生成：LLM + 本体上下文 → opl-expr/v1 可执行能力定义
# ---------------------------------------------------------------------------
CAPABILITY_GENERATION_PROMPT = """你是企业业务本体建模专家。请结合给定的本体对象模型与业务描述，把其中的计算公式（能力）建模为平台的「能力（capability）」，用 opl-expr/v1 表达式表达。

【关键要求】直接、紧凑地输出最终 JSON 结果，不要展开冗长思考，不要添加任何解释或代码块。能力数量按业务描述实际公式数取（通常 2~8 个）。

【输入】你会看到：
- 本体对象模型（objects 的 id/name/attributes/relations）
- 业务描述

【输出格式】只输出一个 JSON 对象，结构严格如下（字段名不许增删改）：
{
  "capabilities": [
    {
      "name": "cap_xxx",
      "display_name": "能力中文名",
      "description": "一句话说明该能力计算什么",
      "kind": "derive",
      "implementation": "expression",
      "expression": "this.attr_xxx * this.attr_yyy",
      "object_type": "obj_xxx",
      "output_attribute": "attr_xxx"
    }
  ]
}

【硬性规则】（违反会被系统直接拒绝）
1. name 只能小写字母/数字/下划线（如 cap_total_cost），项目内唯一。
2. object_type 指能力执行的目标对象类型（通常是核算主体/最核心对象，如"维保服务产品"），必须是 objects 中已存在的 id。
3. output_attribute 指计算写回该对象的目标属性 id（若该对象尚未有此属性，可省略或写 null）。
4. kind 只能是 derive / assert / act；implementation 只能是 expression。
5. expression 用 opl-expr/v1 方言：
   - 本对象属性用 this.<属性id>（如 this.attr_total_cost）；
   - 跨对象取值用 lookup('<对象id>', {<维度属性id>: <值>}, '<取值属性id>')；
   - 引用其他能力用 ref('<能力name>'), 引用指定版本 ref('<能力name>', '<版本>')；
   - 条件/空值用 if(条件, 真值, 假值)、if_null(a, b)、coalesce(a, b)；
   - 只允许出现在白名单内的函数：if/if_null/coalesce/lookup/ref/abs/round/min/max/str/contains/starts_with/len/split/regex_match/upper/lower/date_add/date_diff/sum/map/filter/group_by/lookup_first/coalesce_lookup/lookup_all。
6. expression 只能引用本对象（this.）或经 lookup/ref 取到的属性；不要直接写未定义的裸标识符（如 attr_xxx 必须写成 this.attr_xxx）。
7. 只输出成绩单结构，不要输出 markdown 代码块。

【建模方法】
1. 从业务描述中识别所有计算公式（如"成本 = 单价 × 数量"、"总价 = 人工 + 备件 + 管理"）。
2. 每个公式建一个 capability，用 lookup/this/ref 表达数据来源；被其他公式引用的公式用 ref() 引用。
3. 无法用现有对象属性表达输入时，可在 expression 里用 lookup 从关联对象取值，不要编造不存在的对象。

本体对象模型：
{schema}

业务描述：
{text}
"""


def render_capability_generation(text: str, schema: str) -> str:
    cfg = get_llm_config()
    prompt = cfg.get_prompt("capability_generation") or CAPABILITY_GENERATION_PROMPT
    return prompt.replace("{schema}", schema).replace("{text}", text[:12000])
