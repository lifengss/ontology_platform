"""模板管理（VS5）：可复用本体模板库。"""
from __future__ import annotations

from typing import Any

from core.responses import ApiError
from logging_setup import get_logger

logger = get_logger("templates")

# 内置模板：可作为 demo 直接实例化
BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "tpl_customer_basic",
        "name": "基础客户本体",
        "description": "客户、联系人、订单的最小可用本体",
        "industry": "通用",
        "tags": ["CRM", "销售"],
        "model": {
            "name": "客户本体",
            "modeling_type": "manual",
            "version": "1",
            "objects": [
                {
                    "id": "obj_customer",
                    "name": "客户",
                    "business_definition": "已达成交易或潜在交易意向的企业或个人主体",
                    "stable_key": "attr_name",
                    "attributes": [
                        {"id": "attr_name", "name": "名称", "data_type": "string", "required": True, "constraints": {}},
                        {"id": "attr_industry", "name": "行业", "data_type": "string", "required": False, "constraints": {}},
                    ],
                    "relations": [],
                },
                {
                    "id": "obj_contact",
                    "name": "联系人",
                    "business_definition": "客户方负责沟通对接的具体人员",
                    "stable_key": "attr_cname",
                    "attributes": [
                        {"id": "attr_cname", "name": "姓名", "data_type": "string", "required": True, "constraints": {}},
                        {"id": "attr_phone", "name": "电话", "data_type": "string", "required": False, "constraints": {}},
                    ],
                    "relations": [
                        {"id": "rel_customer", "name": "所属客户", "target_object": "obj_customer", "cardinality": "many_to_one"},
                    ],
                },
                {
                    "id": "obj_order",
                    "name": "订单",
                    "business_definition": "客户购买产品或服务产生的交易单据",
                    "stable_key": "attr_amount",
                    "attributes": [
                        {"id": "attr_amount", "name": "金额", "data_type": "float", "required": True, "constraints": {"minimum": 0}},
                    ],
                    "relations": [
                        {"id": "rel_buyer", "name": "购买方", "target_object": "obj_customer", "cardinality": "many_to_one"},
                    ],
                },
            ],
            "constraints": [],
        },
    },
    {
        "id": "tpl_product_catalog",
        "name": "产品目录本体",
        "description": "品类、SPU、SKU 层级结构",
        "industry": "电商",
        "tags": ["商品", " catalog"],
        "model": {
            "name": "产品目录本体",
            "modeling_type": "manual",
            "version": "1",
            "objects": [
                {
                    "id": "obj_category",
                    "name": "品类",
                    "business_definition": "商品分类体系中的一级分类节点",
                    "stable_key": "attr_cname",
                    "attributes": [{"id": "attr_cname", "name": "品类名", "data_type": "string", "required": True, "constraints": {}}],
                    "relations": [],
                },
                {
                    "id": "obj_spu",
                    "name": "SPU",
                    "business_definition": "标准化产品单元，同一 SPU 共享主属性",
                    "stable_key": "attr_title",
                    "attributes": [{"id": "attr_title", "name": "标题", "data_type": "string", "required": True, "constraints": {}}],
                    "relations": [
                        {"id": "rel_category", "name": "所属品类", "target_object": "obj_category", "cardinality": "many_to_one"},
                    ],
                },
            ],
            "constraints": [],
        },
    },
    # GAP-04：约束示范模板——覆盖 pattern/enum/min/max/min_length/max_length 全部约束类型，
    # 供新用户一键复用后按业务修改，直观展示「约束怎么写」。
    {
        "id": "tpl_order_sample",
        "name": "订单履约示例（约束示范）",
        "description": "订单+客户两个对象，含正则/枚举/数值区间/长度区间等约束示例，一键复用后可按业务修改",
        "industry": "通用",
        "tags": ["示例", "约束", "订单"],
        "model": {
            "name": "订单履约示例本体",
            "modeling_type": "manual",
            "version": "1",
            "objects": [
                {
                    "id": "obj_order",
                    "name": "订单",
                    "business_definition": "客户或渠道发起的一次采购/服务交易请求单据",
                    "stable_key": "attr_order_no",
                    "attributes": [
                        {"id": "attr_order_no", "name": "订单编号", "data_type": "string", "required": True,
                         "constraints": {"pattern": "^[A-Z]{2}[0-9]{8,12}$", "min_length": 10, "max_length": 14}},
                        {"id": "attr_status", "name": "订单状态", "data_type": "string", "required": True,
                         "constraints": {"enum": ["待付款", "已付款", "已发货", "已完成", "已取消"]}},
                        {"id": "attr_amount", "name": "订单金额", "data_type": "float", "required": True,
                         "constraints": {"minimum": 0, "maximum": 99999999}},
                        {"id": "attr_qty", "name": "数量", "data_type": "integer", "required": True,
                         "constraints": {"minimum": 1, "maximum": 10000}},
                        {"id": "attr_created", "name": "下单时间", "data_type": "date", "required": True,
                         "constraints": {}},
                        {"id": "attr_remark", "name": "备注", "data_type": "string", "required": False,
                         "constraints": {"max_length": 200}},
                    ],
                    "relations": [
                        {"id": "rel_buyer", "name": "购买方", "target_object": "obj_customer", "cardinality": "many_to_one"},
                    ],
                },
                {
                    "id": "obj_customer",
                    "name": "客户",
                    "business_definition": "下单方企业或个人主体，是订单的责任归属方",
                    "stable_key": "attr_cust_code",
                    "attributes": [
                        {"id": "attr_cust_code", "name": "客户编码", "data_type": "string", "required": True,
                         "constraints": {"min_length": 3, "max_length": 32, "pattern": "^[A-Z0-9_]+$"}},
                        {"id": "attr_phone", "name": "联系电话", "data_type": "string", "required": False,
                         "constraints": {"pattern": "^1[3-9][0-9]{9}$", "min_length": 11, "max_length": 11}},
                        {"id": "attr_level", "name": "客户等级", "data_type": "string", "required": False,
                         "constraints": {"enum": ["普通", "银卡", "金卡", "钻石"]}},
                    ],
                    "relations": [],
                },
            ],
            "constraints": [],
        },
    },
]
