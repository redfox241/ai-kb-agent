"""P1：DiscoService 主路径与边界。"""
from __future__ import annotations

import pytest

from domain import Chunk, ChunkKind, ScoredChunk, SearchResult
from qa.disco import DiscoService


class StubPipeline:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.retrieved = 0

    def retrieve(self, question, options=None):
        self.retrieved += 1
        hits = tuple(ScoredChunk(chunk, 1.0) for chunk in self.chunks)
        return SearchResult(hits=hits)

    def expand(self, search):
        return list(self.chunks)


class FakeLLM:
    model = "fake"

    def __init__(self, answer="答案[E1]", fail=False):
        self.answer = answer
        self.fail = fail

    def chat(self, messages, *, system=None, max_tokens=None, temperature=None,
             thinking=None):
        if self.fail:
            raise RuntimeError("llm down")
        return self.answer


def _chunk():
    return Chunk("a#summary", "wiki/a.md", "甲", ChunkKind.SUMMARY, "甲的摘要")


def _service(chunks, llm, analyzed=None):
    calls = {"disco": 0}

    def disco(question, selected):
        calls["disco"] += 1
        return analyzed or {"trees": "T", "graph": "G", "outline": "O"}

    service = DiscoService(
        StubPipeline(chunks),
        llm,
        disco,
        system_prompt=lambda: "SYS",
        top_n=5,
        max_sources=10,
        max_tokens=8000,
    )
    return service, calls


def test_disco_main_path():
    service, calls = _service([_chunk()], FakeLLM())
    result = service.answer("甲是什么？")

    assert result.answer.text == "答案[E1]"
    assert result.answer.sources
    assert result.answer.citations.ok is True
    assert result.disco["trees"] == "T"
    assert calls["disco"] == 1
    keys = [event.step for event in result.answer.trace]
    assert {"expand", "disco", "context", "generate", "cite"} <= set(keys)


def test_disco_empty_evidence_skips_llm():
    service, calls = _service([], FakeLLM())
    result = service.answer("无法回答")

    assert result.answer.text == "当前知识库中未找到相关内容。"
    assert result.answer.sources == ()
    assert result.disco == {}
    assert calls["disco"] == 0


def test_disco_llm_failure_raises():
    service, _ = _service([_chunk()], FakeLLM(fail=True))
    with pytest.raises(RuntimeError):
        service.answer("甲是什么？")


def test_disco_invalid_citation():
    service, _ = _service([_chunk()], FakeLLM(answer="答案[E9]"))
    result = service.answer("甲是什么？")
    assert result.answer.citations.ok is False
    assert "E9" in result.answer.citations.invalid
