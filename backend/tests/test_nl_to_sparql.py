"""nl_to_sparql 单测：LLM 输出归一化（PREFIX 前导/markdown 围栏）与 fallback 判定。

背景：LLM 按 prompt 前缀约定输出 `PREFIX ont: <...>` 前导行，旧校验器要求查询以
SELECT 开头，把合法查询误判为非法而丢弃；现应容忍前导声明并显式返回来源。
"""
import pytest

import ai_helper
from ai_helper import nl_to_sparql

ONT = {
    "objects": [
        {
            "id": "Activity",
            "name": "流程活动",
            "attributes": [
                {"id": "activityCode", "name": "活动编号", "data_type": "string"},
                {"id": "leadingRole", "name": "主导角色", "data_type": "string"},
            ],
            "relations": [{"id": "hasLeadingRole", "name": "主导角色", "target_object": "Role"}],
        },
        {"id": "Role", "name": "业务角色", "attributes": [{"id": "roleCode", "name": "角色缩写", "data_type": "string"}], "relations": []},
    ]
}


def _patch_llm(monkeypatch, content):
    def fake_call_llm(prompt, **kwargs):
        return {"choices": [{"message": {"content": content}}]}

    # nl_to_sparql 使用 ai_helper 包命名空间中的 call_llm（顶层 from .client import）
    monkeypatch.setattr(ai_helper, "call_llm", fake_call_llm)


def test_accepts_prefix_prolog(monkeypatch):
    """带 PREFIX 前导行的合法 SPARQL 不应被误判为非法。"""
    _patch_llm(monkeypatch, "PREFIX ont: <http://ontology.local/>\nSELECT ?role WHERE {\n  ?role a ont:Role\n}")
    sparql, source = nl_to_sparql("有哪些角色？", ONT)
    assert source == "llm"
    assert sparql.upper().startswith("SELECT")
    assert "ont:Role" in sparql
    assert "LIMIT 50" in sparql  # 无 LIMIT 时自动补齐


def test_accepts_markdown_code_fence(monkeypatch):
    _patch_llm(monkeypatch, "```sparql\nSELECT ?s WHERE { ?s a ont:Role } LIMIT 10\n```")
    sparql, source = nl_to_sparql("列出角色", ONT)
    assert source == "llm"
    assert sparql.upper().startswith("SELECT")
    assert "LIMIT 10" in sparql  # 已有 LIMIT 不重复追加


def test_rejects_explanation_only(monkeypatch):
    _patch_llm(monkeypatch, "好的，以下是查询：由于数据模型限制，我无法完成。")
    sparql, source = nl_to_sparql("每个活动的主导角色", ONT)
    assert source == "fallback"
    assert sparql.upper().startswith("SELECT")  # fallback 也是合法 SPARQL


def test_rejects_empty_content(monkeypatch):
    """仅 reasoning 无 content 的输出视为不可信。"""
    def fake_call_llm(prompt, **kwargs):
        return {"choices": [{"message": {"content": None, "reasoning": "thinking..."}}]}

    monkeypatch.setattr(ai_helper, "call_llm", fake_call_llm)
    sparql, source = nl_to_sparql("有哪些活动？", ONT)
    assert source == "fallback"


def test_llm_unreachable_fallback(monkeypatch):
    def fake_call_llm(prompt, **kwargs):
        raise RuntimeError("The read operation timed out")

    monkeypatch.setattr(ai_helper, "call_llm", fake_call_llm)
    sparql, source = nl_to_sparql("列出所有阶段", ONT)
    assert source == "fallback"
    assert sparql.upper().startswith("SELECT")


def test_fallback_prefers_keyword_object(monkeypatch):
    """确定性兜底按关键词映射命中对象（阶段→Stage 类映射不存在时按名称打分）。"""
    def fake_call_llm(prompt, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(ai_helper, "call_llm", fake_call_llm)
    sparql, source = nl_to_sparql("列出所有活动", ONT)
    assert source == "fallback"
    assert "Activity" in sparql
