"""PR-07：上下文与引用与旧实现等价。"""
from __future__ import annotations

from qa import context as cb
from infra.index_files import chunk_from_dict
from qa.citation import (
    citations_from_answer,
    followup_suggestions,
    validate_citations,
)
from qa.context import (
    MULTITURN_RULES,
    SUMMARY_SYSTEM,
    build,
    build_context,
    estimate_tokens,
    format_evidence,
    summarize_prompt,
)


EVIDENCE = [
    {"chunk_id": "a#summary", "title": "甲", "type": "summary", "doc_path": "wiki/a.md",
     "created": "2026-01-01", "text": "甲的摘要内容。"},
    {"chunk_id": "a#detail0", "title": "甲", "type": "detail", "doc_path": "wiki/a.md",
     "created": "2026-01-01", "text": "甲的正文内容。"},
]
DOMAIN_EVIDENCE = [chunk_from_dict(item) for item in EVIDENCE]
WINDOW = [
    {"turn_id": 1, "question": "甲是什么？", "answer": "甲是第一轮的回答。"},
    {"turn_id": 2, "question": "它有什么特点？", "answer": "第二轮的回答。"},
    {"turn_id": 3, "question": "再说说细节。", "answer": "第三轮的回答。"},
]


def test_constants_match_legacy():
    assert MULTITURN_RULES == cb.MULTITURN_RULES
    assert SUMMARY_SYSTEM == cb.SUMMARY_SYSTEM


def test_estimate_tokens_matches_legacy():
    for text in ("", "中文六个字", "hello world", "混合 mixed 内容 123"):
        assert estimate_tokens(text) == cb.estimate_tokens(text)


def test_format_evidence_matches_legacy():
    assert format_evidence(DOMAIN_EVIDENCE) == cb.format_evidence(EVIDENCE)


def test_build_context_contract():
    context = build_context(DOMAIN_EVIDENCE)
    assert "[来源: 甲 | summary | wiki/a.md | 2026-01-01]" in context
    assert "[来源: 甲 | detail | wiki/a.md | 2026-01-01]" in context
    assert "甲的摘要内容。" in context
    assert "甲的正文内容。" in context


def test_build_layout_contract():
    """消息布局：首尾 user、角色交替、历史不带证据、当前轮带证据。"""
    built = build("SYS", "会话摘要", WINDOW, DOMAIN_EVIDENCE, "甲的特点是什么？",
                  raw_question="它有什么特点？", budget=12000, max_turns=3)
    roles = [message["role"] for message in built.messages]
    assert roles[0] == "user" and roles[-1] == "user"
    assert all(roles[i] != roles[i - 1] for i in range(1, len(roles)))
    assert all("[E1]" not in message["content"] for message in built.messages[:-1])
    assert "[E1]" in built.messages[-1]["content"]
    assert "多轮对话规则" in built.system
    assert "[来源:" not in built.messages[-1]["content"]
    assert list(built.evidence_ids) == ["a#summary", "a#detail0"]
    assert built.truncated is False


def test_summarize_prompt_matches_legacy():
    assert summarize_prompt("旧摘要", WINDOW) == cb.summarize_prompt("旧摘要", WINDOW)
    assert summarize_prompt("", WINDOW) == cb.summarize_prompt("", WINDOW)


def test_citation_validation_contract():
    assert validate_citations("结论[E1][E2]", DOMAIN_EVIDENCE).ok is True
    bad = validate_citations("结论[E3]", DOMAIN_EVIDENCE)
    assert bad.ok is False and list(bad.invalid) == ["E3"]
    missing = validate_citations("很长的答案" * 100, DOMAIN_EVIDENCE)
    assert missing.missing is True and list(missing.invalid) == []


def test_citations_from_answer_contract():
    answer = "结论[E1][E2][E1][E9]"
    result = citations_from_answer(answer, DOMAIN_EVIDENCE)
    assert [(item.id, item.title, item.doc_path, item.chunk_id) for item in result] == [
        ("E1", "甲", "wiki/a.md", "a#summary"),
        ("E2", "甲", "wiki/a.md", "a#detail0"),
    ]


def test_followup_suggestions_contract():
    result = followup_suggestions(DOMAIN_EVIDENCE, ["甲"])
    assert list(result) == [
        "《甲》的核心要点是什么？",
        "甲还有哪些相关内容？",
    ]


def test_truncate_middle_turns():
    long_window = [{"turn_id": i, "question": f"问题{i}" + "很长的内容" * 40,
                    "answer": f"回答{i}" + "很长的内容" * 40} for i in range(1, 6)]
    built = cb.build("SYS", "", long_window, EVIDENCE, "当前问题", budget=1200,
                     max_turns=5)
    contents = [m["content"] for m in built.messages[:-1]]
    assert built.truncated is True
    # 最近一轮必须保留；更早的中间轮次优先丢弃
    assert any("问题5" in c for c in contents)
    assert len(contents) < 2 * len(long_window)


def test_no_history_single_message():
    built = cb.build("SYS", "", [], EVIDENCE, "单轮问题", budget=4000)
    assert len(built.messages) == 1
    assert built.messages[0]["role"] == "user"
    assert built.truncated is False


def test_budget_caps_evidence():
    big = [{"chunk_id": f"c{i}", "title": f"T{i}", "type": "detail",
            "doc_path": f"wiki/{i}.md", "text": "正文" * 3000} for i in range(6)]
    built = cb.build("SYS", "", [], big, "问题", budget=2000)
    assert len(built.evidence_ids) < len(big)
    assert built.truncated is True
