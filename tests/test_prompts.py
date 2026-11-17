"""Prompt 资产：版本、严格渲染、受控注入。"""
from __future__ import annotations

import pytest

from qa.prompts import PromptError, default_registry


def test_prompt_versions_are_present():
    registry = default_registry()
    assert registry.version("answer") >= 1
    assert registry.version("rewrite") >= 1
    assert registry.version("summarize") >= 1
    assert registry.version("disco") >= 1


def test_render_is_strict():
    registry = default_registry()
    with pytest.raises(PromptError):
        registry.get("missing")

    # 无变量资产传入变量 → 未知变量报错
    with pytest.raises(PromptError):
        registry.render("answer", question="x")


def test_prompt_rule_is_appended_not_replacing_base():
    import bootstrap as server

    base = server.SYSTEM_PROMPT
    text = server.effective_prompt()
    assert text.startswith(base)
    assert "自动注入" not in text or "补充规则" in text
