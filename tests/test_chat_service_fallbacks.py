"""P1：ChatService 回退、引用 flag、摘要失败分支。"""
from __future__ import annotations

import os

from infra.sqlite_sessions import SqliteSessionRepository
from domain import (
    Answer,
    Chunk,
    ChunkKind,
    CitationCheck,
    Intent,
    JobHandle,
    RewriteResult,
    ScoredChunk,
    SearchResult,
    Turn,
)
from qa.multiturn import ChatService


class StubRewriter:
    def __init__(self, resolved="改写问题", intent=Intent.FOLLOW_UP, entities=("甲",)):
        self.resolved = resolved
        self.intent = intent
        self.entities = entities

    def resolve(self, question, window, summary, entities, llm, mode="auto", intent_hint=None):
        return RewriteResult(
            resolved_query=self.resolved,
            intent=self.intent,
            entities=self.entities,
            skipped=False,
        )

    def update_entity_slots(self, prev_entities, result, turn_id):
        return list(result.entities), []


class StubPipeline:
    def __init__(self, searches, answer=None):
        self.searches = list(searches)
        self.calls = []
        self.answer = answer or Answer(
            text="答案[E1]",
            sources=(),
            evidence=(),
            citations=CitationCheck(ok=True),
        )

    def retrieve(self, question, options=None):
        self.calls.append(question)
        return self.searches.pop(0) if len(self.searches) > 1 else self.searches[0]

    def generate(self, question, search, options=None, *, resolved_query=None, prefix_trace=()):
        return self.answer


class SyncJobs:
    def __init__(self):
        self.names = []

    def start(self):
        return None

    def submit(self, name, fn):
        self.names.append(name)
        fn()
        return JobHandle(id=str(len(self.names)), name=name)

    def shutdown(self, wait=True):
        return None


class SummaryFailLLM:
    model = "fail"

    def chat(self, messages, *, system=None, max_tokens=None, temperature=None,
             thinking=None):
        raise RuntimeError("summary down")


def _hit():
    chunk = Chunk("a#summary", "wiki/a.md", "甲", ChunkKind.SUMMARY, "摘要")
    return ScoredChunk(chunk, 1.0)


def _repo():
    repo = SqliteSessionRepository(os.environ["RAG_SESSION_DB"])
    repo.migrate()
    return repo


def test_fallback_raw():
    pipeline = StubPipeline([SearchResult(hits=()), SearchResult(hits=(_hit(),))])
    service = ChatService(pipeline, _repo(), StubRewriter("改写问题"))

    result = service.chat("原问题", None)

    assert result.flags.get("fallback_raw") == 1
    assert pipeline.calls[:2] == ["改写问题", "原问题"]
    assert result.answer.text == "答案[E1]"


def test_fallback_concat_with_history():
    history = (Turn(
        turn_id=1,
        question="甲是什么？",
        resolved_query="甲是什么？",
        intent=Intent.NEW_TOPIC,
        answer="甲是第一轮回答。",
    ),)
    pipeline = StubPipeline([SearchResult(hits=()), SearchResult(hits=(_hit(),))])
    service = ChatService(pipeline, _repo(), StubRewriter("原问题"))

    result = service.chat("原问题", None, history=history, use_rewrite=False)

    assert result.flags.get("fallback_concat") == 1
    assert pipeline.calls[-1] == "甲是什么？ 原问题"


def test_citation_flags():
    answer = Answer(
        text="答案[E9]",
        sources=(),
        evidence=(),
        citations=CitationCheck(ok=False, invalid=("E9",), missing=True),
    )
    pipeline = StubPipeline([SearchResult(hits=(_hit(),))], answer=answer)
    service = ChatService(pipeline, _repo(), StubRewriter("改写问题"))

    result = service.chat("原问题", None)
    assert result.flags.get("citation_invalid") == 1
    assert result.flags.get("citation_missing") == 1


def test_summary_failure_is_swallowed(temp_session_db):
    jobs = SyncJobs()
    pipeline = StubPipeline([SearchResult(hits=(_hit(),))])
    service = ChatService(
        pipeline, _repo(), StubRewriter("问题"),
        jobs=jobs, summary_every=1,
    )

    result = service.chat("问题", SummaryFailLLM(), session_id="ses_summary_fail")

    assert result.turn_id == 1
    assert "summary" in jobs.names
    assert _repo().get("ses_summary_fail").summary == ""
