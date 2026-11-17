"""PR-10：原生 Rewrite 契约测试。"""
from __future__ import annotations

import json

import pytest

from domain import Intent, RewriteResult, Turn
from qa.rewrite import NativeRewriter, needs_rewrite


WINDOW = [Turn(
    turn_id=1,
    question="孙悟空是谁？",
    resolved_query="孙悟空是谁？",
    intent=Intent.NEW_TOPIC,
    answer="须菩提祖师。[E1]",
)]


class FailingLLM:
    model = "fail"

    def chat(self, *args, **kwargs):
        raise RuntimeError("llm down")


class BadIntentLLM:
    model = "bad"

    def chat(self, *args, **kwargs):
        return json.dumps({
            "resolved_query": "孙悟空的师父是谁？",
            "intent": "weird",
            "entities": ["孙悟空"],
            "filters": {},
            "needs_clarification": False,
            "clarification_question": "",
            "confidence": 0.8,
        })


class TopicCopyLLM:
    """模拟小模型把上一轮 resolved_query 原样复制过来的行为。"""

    model = "topic-copy"

    def __init__(self, confidence: float = 0.0) -> None:
        self.confidence = confidence

    def chat_json(self, *args, **kwargs):
        return {
            "resolved_query": "《山海经·东山经》中有哪些著名的神兽",
            "intent": "follow_up",
            "entities": ["东山经"],
            "filters": {},
            "needs_clarification": False,
            "clarification_question": "",
            "confidence": self.confidence,
        }


WINDOW_DONG = [Turn(
    turn_id=11,
    question="东山经有哪些？",
    resolved_query="《山海经·东山经》中有哪些著名的神兽",
    intent=Intent.NEW_TOPIC,
    answer="狪狪、犰狳、朱獳等。",
)]


def test_needs_rewrite_contract():
    assert needs_rewrite("他师父呢？", False) is False
    assert needs_rewrite("他师父呢？", True) is True
    assert needs_rewrite("请完整介绍一下长征的历史背景和主要经过", True) is False
    assert needs_rewrite("介绍下连杰", True) is False
    assert needs_rewrite("介绍下它", True) is True
    assert needs_rewrite("那还有吗？", True) is True
    assert needs_rewrite("他师父呢？", True, "correction") is True
    assert needs_rewrite("他师父呢？", True, mode="off") is False
    assert needs_rewrite("他师父呢？", True, mode="always") is True


def test_resolve_contract(fake_llm):
    result = NativeRewriter().resolve(
        "他师父呢？", WINDOW, "", ["孙悟空"], fake_llm, mode="auto"
    )
    assert isinstance(result, RewriteResult)
    assert result.resolved_query == "孙悟空的师父是谁？"
    assert result.intent is Intent.FOLLOW_UP
    assert result.entities == ("孙悟空",)
    assert result.skipped is False


def test_no_history_skips_rewrite(fake_llm):
    result = NativeRewriter().resolve("孙悟空是谁？", [], "", [], fake_llm)
    assert result.skipped is True
    assert result.resolved_query == "孙悟空是谁？"


def test_self_contained_new_entity_starts_new_topic(fake_llm):
    result = NativeRewriter().resolve(
        "介绍下连杰", WINDOW_DONG, "", ["东山经"], fake_llm, mode="auto"
    )
    assert result.resolved_query == "介绍下连杰"
    assert result.intent is Intent.NEW_TOPIC
    assert result.entities == ()
    assert result.skipped is False


def test_topic_copy_with_zero_confidence_falls_back_to_raw():
    result = NativeRewriter().resolve(
        "连杰是谁", WINDOW_DONG, "", ["东山经"], TopicCopyLLM(confidence=0.0),
        mode="auto",
    )
    assert result.resolved_query == "连杰是谁"
    assert result.intent is Intent.NEW_TOPIC
    assert result.entities == ()


def test_rewrite_dropping_user_entity_is_rejected():
    result = NativeRewriter().resolve(
        "连杰的事迹", WINDOW_DONG, "", ["东山经"], TopicCopyLLM(confidence=0.9),
        mode="auto",
    )
    assert result.resolved_query == "连杰的事迹"
    assert result.intent is Intent.NEW_TOPIC
    assert result.entities == ()


def test_llm_failure_falls_back():
    result = NativeRewriter().resolve(
        "他师父呢？", WINDOW, "", ["孙悟空"], FailingLLM()
    )
    assert result.failed is True
    assert result.intent is Intent.FOLLOW_UP
    assert "孙悟空" in result.resolved_query


def test_llm_failure_does_not_concatenate_history():
    result = NativeRewriter().resolve(
        "主要领导人", WINDOW, "", ["孙悟空"], FailingLLM()
    )
    assert result.failed is True
    # 非代词问题必须保持原文，不能拼成“孙悟空是谁？ 主要领导人”
    assert result.resolved_query == "主要领导人"
    assert result.entities == ()


def test_new_topic_clears_old_entities():
    result = RewriteResult(
        resolved_query="解放战争著名的战役",
        intent=Intent.NEW_TOPIC,
        entities=("解放战争",),
        skipped=False,
    )
    upsert, delete = NativeRewriter().update_entity_slots(
        ["长征", "抗日战争"], result, 3
    )
    assert upsert == ["解放战争"]
    assert delete == ["长征", "抗日战争"]


def test_update_entity_slots_correction():
    correction = RewriteResult(
        resolved_query="猪八戒的师父是谁？",
        intent=Intent.CORRECTION,
        entities=("猪八戒",),
        skipped=False,
    )
    upsert, delete = NativeRewriter().update_entity_slots(["孙悟空"], correction, 2)
    assert upsert == ["猪八戒"]
    assert delete == ["孙悟空"]


def test_invalid_intent_normalizes():
    result = NativeRewriter().resolve(
        "他师父呢？", WINDOW, "", ["孙悟空"], BadIntentLLM()
    )
    assert result.intent is Intent.FOLLOW_UP
