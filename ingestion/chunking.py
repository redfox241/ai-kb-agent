"""分块原语：摘要块文本 + 正文分块。

与 server.py 的 build_page_summary / chunk_text_v2 保持等价；
迁移期由 tests/test_ingestion_primitives.py 做新旧对照。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from .frontmatter import clean_text


DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100
MIN_CHUNK_CHARS = 50


def build_page_summary(
    meta: Mapping[str, Any],
    body: str,
    title: str,
    doc_path: str,
) -> str:
    """生成摘要 chunk 文本：标题 + 标签/别名 + 正文开头 300 字。"""
    tags = meta.get("tags")
    if isinstance(tags, list):
        tags = "、".join(str(tag) for tag in tags)

    parts = [f"# {title}"]
    if tags:
        parts.append(f"> 标签：{tags}")
    aliases = meta.get("aliases")
    if aliases:
        parts.append(f"> 别名：{'、'.join(str(alias) for alias in aliases)}")

    lead = clean_text(body)[:300]
    if lead:
        parts.append(lead)
    return "\n\n".join(parts)


def _split_window(text: str, size: int, overlap: int) -> list[str]:
    """超长句硬切：固定窗口 + overlap；最后一片对齐到句尾，避免产生碎片。"""
    step = max(1, size - overlap)
    pieces: list[str] = []
    start = 0
    while start + size < len(text):
        pieces.append(text[start:start + size])
        start += step
    pieces.append(text[-size:] if len(text) >= size else text)
    return pieces


def chunk_text(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """正文分块：整篇短则不切；按 ## 标题切段；段过长按句子边界细切。

    与旧实现一致：
    - 段/整篇 <= chunk_size 直接保留
    - 长段按中英文句末标点切分，overlap 从上一块尾部带入
    - 无标点的超长句按固定窗口硬切，保证不超过 embedding 上下文
    - 丢弃 < 50 字的碎片
    """
    chunk_size = chunk_size or DEFAULT_CHUNK_SIZE
    overlap = overlap or DEFAULT_CHUNK_OVERLAP
    text = clean_text(text)
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    parts: list[str] = []
    sections = re.split(r"(?=^##\s)", text, flags=re.M)
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) <= chunk_size:
            parts.append(section)
            continue

        sentences = re.split(r"(?<=[。！？；.!?])\s*", section)
        current = ""
        for sentence in sentences:
            if len(sentence) > chunk_size:
                # 单句本身就超限（常见于转写稿/无标点长段）：硬切。
                if current.strip():
                    parts.append(current)
                current = ""
                parts.extend(_split_window(sentence, chunk_size, overlap))
                continue
            if current and len(current) + len(sentence) > chunk_size:
                parts.append(current)
                room = chunk_size - len(sentence)
                tail = current[-overlap:] if overlap else ""
                current = (tail[-room:] if room > 0 else "") + sentence
            else:
                current += sentence
        if current.strip():
            parts.append(current)

    return [part for part in parts if len(part.strip()) >= MIN_CHUNK_CHARS]
