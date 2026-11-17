"""纯标准库的 LLM JSON 输出解析。"""
from __future__ import annotations

import json
import re


def parse_json(text):
    """多层兜底解析 JSON：raw_decode → 去 markdown 围栏 → 修复 trailing comma → 正则抽取对象。"""
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    fixed = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        return json.loads(fixed)
    except Exception:
        pass
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, cleaned, re.S)
        if match:
            try:
                return json.loads(re.sub(r",\s*([}\]])", r"\1", match.group(0)))
            except Exception:
                continue
    return None
