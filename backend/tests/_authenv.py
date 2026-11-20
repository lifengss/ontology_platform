"""鉴权环境模块级隔离工具（GAP-20/22）。

背景：pytest 全量顺序运行时，某测试模块把 ONTOLOGY_AUTH_ENABLED 置 true 后
不在模块边界恢复，进程内残留态使后续模块的无鉴权用例批量 401
（曾致 test_user_friendly/test_v2_02_alignment 共 10 项失败）。

约定：凡触碰 ONTOLOGY_AUTH_ENABLED 的测试模块一律使用 `auth_module_guard()`：
- 模块开始（setup）：保存原环境 → 强制进入已知清洁态（默认清空＝鉴权关），
  消除「前一模块残留 auth=true」的跨模块污染，保证本模块起始状态确定；
- 模块结束（teardown）：恢复为模块前状态。

含鉴权用例的模块应同时挂 `pytestmark = pytest.mark.auth`，
使基线回归可用 `python -m pytest tests/ -q -m "not auth"` 独立运行（GAP-22）。
"""
from __future__ import annotations

import os

import pytest

AUTH_ENV_KEYS = ("ONTOLOGY_AUTH_ENABLED",
                 "ONTOLOGY_BOOTSTRAP_ADMIN_PASSWORD",
                 "ONTOLOGY_PBKDF2_ITERATIONS")


def auth_module_guard(initial: dict | None = None):
    """生成 module 级 autouse fixture：模块边界上隔离鉴权环境。

    注意：`auth.auth_enabled()` 为 fail-closed——key 缺失默认**启用**鉴权，
    因此清洁态必须显式写 `ONTOLOGY_AUTH_ENABLED=false`（与 conftest 测试态豁免一致），
    不能靠删除 key 表达「鉴权关」。

    initial: 模块开始强制的已知初始环境（缺省＝鉴权关闭；覆盖默认时须自带 ONTOLOGY_AUTH_ENABLED）。
    用法：模块顶层 `_guard = auth_module_guard()`（可传 initial 字典）。
    """
    initial = dict(initial or {})
    initial.setdefault("ONTOLOGY_AUTH_ENABLED", "false")

    @pytest.fixture(scope="module", autouse=True)
    def _guard():
        prev = {k: os.environ.get(k) for k in AUTH_ENV_KEYS}
        for k in AUTH_ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in initial.items():
            os.environ[k] = v
        yield
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return _guard
