"""PR-04：摄入原语契约测试。"""
from __future__ import annotations

from ingestion.chunking import build_page_summary, chunk_text
from ingestion.frontmatter import clean_text, parse_frontmatter


def test_parse_frontmatter_valid():
    meta, body = parse_frontmatter("---\ntitle: 测试\ntags: [a]\n---\n\n正文")
    assert meta["title"] == "测试"
    assert meta["tags"] == ["a"]
    assert body.strip() == "正文"


def test_parse_frontmatter_invalid_falls_back():
    for text in ("---\n[unclosed\n---\nbody", "---\n- a\n- b\n---\nbody", "没有 frontmatter"):
        meta, body = parse_frontmatter(text)
        assert isinstance(meta, dict)
        assert body


def test_clean_text_compresses_blank_lines():
    assert clean_text("a\n\n\n\nb") == "a\n\nb"
    assert clean_text("  a  ") == "a"


def test_build_page_summary_includes_tags_and_aliases():
    summary = build_page_summary(
        {"tags": ["西游", "人物"], "aliases": ["齐天大圣", "行者"]},
        "孙悟空，师父须菩提祖师。" * 40,
        "孙悟空",
        "wiki/孙悟空.md",
    )
    assert summary.startswith("# 孙悟空")
    assert "> 标签：西游、人物" in summary
    assert "> 别名：齐天大圣、行者" in summary


def test_chunk_text_short_and_long():
    assert chunk_text("短文本", 800, 100) == ["短文本"]
    long_text = "## 第一节\n\n" + "内容。" * 500
    chunks = chunk_text(long_text, 200, 40)
    assert len(chunks) > 1
    assert all(len(chunk) >= 50 for chunk in chunks)


def test_chunk_text_hard_splits_unpunctuated_long_text():
    # 转写稿/无标点长段：不能整段塞给 embedding，必须按窗口硬切。
    text = "字" * 5000
    chunks = chunk_text(text, 800, 100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 800 for chunk in chunks)
    assert chunks[0] == "字" * 800
    assert sum(len(chunk) for chunk in chunks) >= len(text)


def test_chunk_text_empty():
    assert chunk_text("", 200, 40) == []
    assert chunk_text("   ", 200, 40) == []
