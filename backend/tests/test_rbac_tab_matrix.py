"""RBAC permission_matrix tab 白名单回归（2026-11-13 修复）。

背景：M3-F「编排」/ M3-C「数据健康度」/ M4-A②「跨本体对齐」三个新 tab 未进
permission_matrix 的 tabs 白名单，前端 applyPermissionMatrix 把不在名单的 tab
加 op-hidden → 「编排」tab 对所有角色不可见（M3-F 冒烟系脚本直接 switchTab 绕过点击未暴露）。
纯函数测试，不依赖运行服务。
"""
from auth import permission_matrix


def _tabs(role: str) -> list[str]:
    return permission_matrix({"id": "u", "username": "x", "role": role, "grants": []})["tabs"]


def test_read_roles_see_quality_and_alignment():
    """quality（只读看板）与 alignment（候选浏览为主）对所有角色可见。"""
    for role in ("viewer", "reviewer", "modeler", "admin"):
        tabs = _tabs(role)
        assert "quality" in tabs, role
        assert "alignment" in tabs, role


def test_orchestration_only_for_write_roles():
    """orchestration（建流/编辑/发布/运行）与 action 同属写侧：viewer/reviewer 不可见。"""
    assert "orchestration" in _tabs("modeler")
    assert "orchestration" in _tabs("admin")
    assert "orchestration" not in _tabs("viewer")
    assert "orchestration" not in _tabs("reviewer")
