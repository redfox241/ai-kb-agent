"""P1：ThreadJobRunner 生命周期与 parse_json 边界。"""
from __future__ import annotations

import threading

import pytest

from infra.jobs import ThreadJobRunner
from infra.llm import parse_json


def test_thread_job_runner_executes_and_closes():
    runner = ThreadJobRunner(max_workers=2)
    done = threading.Event()
    handle = runner.submit("demo", done.set)

    assert handle.name == "demo"
    assert done.wait(2)
    runner.shutdown(wait=True)

    with pytest.raises(RuntimeError):
        runner.submit("after-close", lambda: None)


def test_parse_json_plain():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_markdown_fence():
    assert parse_json('```json\n{"a": 2}\n```') == {"a": 2}


def test_parse_json_trailing_comma():
    assert parse_json('{"a": 3,}') == {"a": 3}


def test_parse_json_extracts_object():
    assert parse_json('前置说明 {"a": 4} 后置') == {"a": 4}


def test_parse_json_invalid_returns_none():
    assert parse_json("not json") is None
    assert parse_json("") is None
