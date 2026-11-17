"""P0 行为快照：单轮问答输出。

快照只锁“结构 + 内容 + 顺序”，不锁耗时（ms 归一化为 0）。
任何字段漂移都应让本测试失败，而不是等到线上。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import bootstrap as server

SNAPSHOT = Path(__file__).parent / "snapshots" / "single_turn.json"


def _normalise(value):
    if isinstance(value, dict):
        return {
            key: (0 if key in {"ms", "latency_ms"} else _normalise(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    return value


def test_single_turn_snapshot(fake_index, fake_llm):
    answer, sources, steps = server.answer_question(
        "孙悟空是谁？", fake_llm, use_rewrite=False
    )
    actual = _normalise({
        "question": "孙悟空是谁？",
        "answer": answer,
        "sources": sources,
        "steps": steps,
    })

    if os.environ.get("UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        pytest.skip("snapshot updated")

    assert SNAPSHOT.exists(), (
        f"缺少快照文件 {SNAPSHOT}。首次生成："
        "UPDATE_SNAPSHOT=1 .venv/bin/python -m pytest tests/test_single_turn_snapshot.py -q"
    )
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert actual == expected


def test_single_turn_v2_contract(fake_index, fake_llm):
    answer, sources, steps = server.answer_question(
        "孙悟空是谁？", fake_llm, use_rewrite=False
    )
    keys = [step["key"] for step in steps]
    assert "须菩提" in answer
    assert sources
    assert "cite" in keys
    assert {"accept", "bm25", "semantic", "expand", "context", "generate"} <= set(keys)


def test_single_turn_has_required_contract(fake_index, fake_llm):
    answer, sources, steps = server.answer_question(
        "孙悟空是谁？", fake_llm, use_rewrite=False
    )
    assert answer and "须菩提" in answer
    assert sources and all({"title", "path", "type"} <= set(s) for s in sources)
    keys = [step["key"] for step in steps]
    assert keys[0] == "accept"
    assert "bm25" in keys and "semantic" in keys
    assert "expand" in keys and "context" in keys and "generate" in keys
    for step in steps:
        assert {"key", "title", "status", "icon", "summary", "detail",
                "items", "ms", "ops", "io"} <= set(step)
