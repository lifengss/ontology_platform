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
                    "attributes": [
                        {"id": "attr_name", "name": "名称", "data_type": "string", "required": True, "constraints": {}},
                        {"id": "attr_industry", "name": "行业", "data_type": "string", "required": False, "constraints": {}},
                    ],
                    "relations": [],
                },
                {
                    "id": "obj_contact",
                    "name": "联系人",
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
                    "attributes": [{"id": "attr_cname", "name": "品类名", "data_type": "string", "required": True, "constraints": {}}],
                    "relations": [],
                },
                {
                    "id": "obj_spu",
                    "name": "SPU",
                    "attributes": [{"id": "attr_title", "name": "标题", "data_type": "string", "required": True, "constraints": {}}],
                    "relations": [
                        {"id": "rel_category", "name": "所属品类", "target_object": "obj_category", "cardinality": "many_to_one"},
                    ],
                },
            ],
            "constraints": [],
        },
    },
]
