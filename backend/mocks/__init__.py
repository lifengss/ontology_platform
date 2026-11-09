"""backend.mocks — V2 可控外部 MCP 数据源 mock（VS2-00）。

供 V2 测试大纲 §4.1 mock 外部 MCP 数据源 使用：
- 3 个系统实例：crm / plm / reservoir（字段名各异，同语义不同名）
- 五类可控注入：时延 / schema 热变 / 权限切换 / 断流与故障 / CDC 通道切换
- 调用形态与 backend.mcp_client（http transport）兼容：POST {"tool","params"} -> {"records":[...]}
- 管理面：X-Mock-Admin 头；数据面：Authorization: Bearer <token>
"""

from .v2_external import (
    ADMIN_KEY_DEFAULT,
    ControllableMockSystem,
    ThreeSystemSeed,
    build_default_tokens,
    build_three_system_seed,
    make_all,
    make_crm,
    make_plm,
    make_reservoir,
)

__all__ = [
    "ADMIN_KEY_DEFAULT",
    "ControllableMockSystem",
    "ThreeSystemSeed",
    "build_default_tokens",
    "build_three_system_seed",
    "make_all",
    "make_crm",
    "make_plm",
    "make_reservoir",
]
