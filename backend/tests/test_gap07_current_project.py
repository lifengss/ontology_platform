"""GAP-07 验收：无「当前项目」全局上下文 → 服务端默认项目＋请求统一收敛。

- 显式传 project_id 的请求 → 记录为该身份「最近使用的项目」（仅值变化时写盘）
- 省略 project_id → 回退身份最近项目；无记录/不可读 → default
- GET /api/projects 返回 default_project（当前身份默认项目）
- 前端已有全局项目选择器＋localStorage＋api() 统一注入（既有能力，本模块验证后端契约）

运行：cd backend && python -m pytest tests/test_gap07_current_project.py -q
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from _authenv import auth_module_guard
from core.last_project import touch_last_project
from main import app

_guard = auth_module_guard()  # GAP-20：模块边界隔离（本模块无鉴权用例，保持鉴权关）

c = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _restore_default_after_module():
    """模块结束（含中途失败）把 dev 身份最近项目恢复 default，避免污染其它测试模块的省略回退。"""
    yield
    touch_last_project("dev", "default")

ONT_A: str | None = None
ONT_B: str | None = None
PROJ_A: str | None = None
PROJ_B: str | None = None


def _ok(resp) -> dict:
    assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("success") is True, body
    return body["data"]


def _new_project(name: str) -> str:
    data = _ok(c.post("/api/projects", json={"name": name, "description": "GAP-07 验收"}))
    return data["id"]


def _new_ontology(project_id: str, name: str) -> str:
    resp = c.post("/api/ontologies", params={"project_id": project_id}, json={
        "name": name,
        "objects": [{
            "id": "Item", "name": "条目", "stable_key": "code",
            "attributes": [{"id": "code", "name": "编码", "data_type": "string", "required": True}],
            "relations": [],
        }],
    })
    return _ok(resp)["id"]


class TestGap07CurrentProject:
    def test_01_explicit_project_touches_last_used(self):
        """显式传 project_id 的请求被记录为该身份最近使用项目。"""
        global PROJ_A, ONT_A
        PROJ_A = _new_project("GAP07-甲-" + uuid.uuid4().hex[:6])
        ONT_A = _new_ontology(PROJ_A, "GAP07-本体甲")
        # 显式访问项目 A（touch A）
        assert c.get("/api/ontologies", params={"project_id": PROJ_A}).status_code == 200
        # 省略 project_id → 回退 A
        data = _ok(c.get("/api/ontologies"))
        ids = [o["id"] for o in data["items"]]
        assert ONT_A in ids, "省略 project_id 应回退到最近使用项目 A"

    def test_02_projects_response_default_project(self):
        """GET /api/projects 返回当前身份默认项目 = 最近使用且可读的项目。"""
        data = _ok(c.get("/api/projects", params={"pageSize": 100}))
        assert data["default_project"] == PROJ_A

    def test_03_switch_project_converges(self):
        """切换到项目 B 后，省略 project_id 的全站请求收敛到 B（不再需要逐接口传参）。"""
        global PROJ_B, ONT_B
        PROJ_B = _new_project("GAP07-乙-" + uuid.uuid4().hex[:6])
        ONT_B = _new_ontology(PROJ_B, "GAP07-本体乙")
        assert c.get("/api/ontologies", params={"project_id": PROJ_B}).status_code == 200
        # 省略 → 回退 B（A 的本体不可见）
        data = _ok(c.get("/api/ontologies"))
        ids = [o["id"] for o in data["items"]]
        assert ONT_B in ids
        assert ONT_A not in ids
        data2 = _ok(c.get("/api/projects", params={"pageSize": 100}))
        assert data2["default_project"] == PROJ_B

    def test_04_invalid_last_project_falls_back_default(self):
        """最近项目不存在 → 安全回退 default（不 500、不泄露存在性）。

        注：list_ontologies 对未知 project_id 无 404 分支（JsonStore 懒建 → 200 空列表），
        显式访问本身照常成功并 touch 该 id；省略回退时存在性校验拦截 → default。
        """
        assert c.get("/api/ontologies", params={"project_id": "no-such-project-xyz"}).status_code == 200
        # 省略 → 最近项目不可读 → default（200 且不含 A/B 本体）
        data = _ok(c.get("/api/ontologies"))
        ids = [o["id"] for o in data["items"]]
        assert ONT_A not in ids and ONT_B not in ids
        data2 = _ok(c.get("/api/projects", params={"pageSize": 100}))
        assert data2["default_project"] in (None, "default")
        # 恢复：显式访问 default，避免污染其它测试模块的项目回退
        assert c.get("/api/ontologies", params={"project_id": "default"}).status_code == 200
