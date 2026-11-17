"""Markdown frontmatter 解析与文本清洗。

与 server.py 的现有实现保持逐字节等价；迁移期由等价测试锁定。
"""
from __future__ import annotations

import re
from typing import Any

import yaml


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 YAML frontmatter，返回 (meta, body)。

    - 没有 frontmatter 或 YAML 非法时返回 ({}, 原文)
    - meta 不是 dict 时同样回退为空 dict
    """
    if text.startswith("---"):
        match = _FRONTMATTER_RE.match(text)
        if match:
            try:
                meta = yaml.safe_load(match.group(1)) or {}
            except Exception:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            return meta, text[match.end():]
    return {}, text


def clean_text(text: str) -> str:
    """压缩连续空行，并去掉首尾空白。"""
    return re.sub(r"\n{3,}", "\n\n", text).strip()
