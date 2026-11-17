"""PR-08：ChatService 与旧 chat_service.chat 的行为对照。"""
from __future__ import annotations

from typing import Sequence

import bootstrap as server
import os
from infra.sqlite_sessions import SqliteSessionRepository
from domain import (
    Answer,
    AnswerOptions,
    CitationCheck,
    Intent,
    JobHandle,
    RewriteResult,
    SearchResult,
)
from ingestion.index_service import IndexSnapshot
from infra.index_files import chunk_from_dict
from qa.multiturn import ChatService
from qa.pipeline import RagPipeline
from qa.rewrite import NativeRewriter
from retrieval.bm25 import BM25Index
from retrieval.service import RetrievalService


class FakeEmbedder:
    model = "fake"

    def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 7

    def embed_many(self, texts):
        return [self.embed(text) for text in texts]


class StubRewriter:
    def __init__(self, result: RewriteResult):
        self.result = result

    def resolve(self, question, window, summary, entities, llm, mode="auto", intent_hint=None):
        return self.result

    def update_entity_slots(self, prev_entities, result, turn_id):
        return list(result.entities), []


class StubPipeline:
    def __init__(self, hits=(), answer_text="当前知识库中未找到相关内容。"):
        self.hits = tuple(hits)
        self.answer_text = answer_text
        self.generated = 0

    def retrieve(self, question, options=None):
        return SearchResult(hits=self.hits)

    def generate(self, question, search, options=None, *, resolved_query=None, prefix_trace=()):
        self.generated += 1
        return Answer(
            text=self.answer_text,
            sources=(),
            evidence=(),
            citations=CitationCheck(ok=True),
            trace=tuple(prefix_trace),
        )


class SyncJobs:
    def __init__(self):
        self.jobs = []

    def start(self):
        return None

    def submit(self, name, fn):
        self.jobs.append(name)
        fn()
        return JobHandle(id=str(len(self.jobs)), name=name)

    def shutdown(self, wait=True):
        return None


def _repo():
    repo = SqliteSessionRepository(os.environ["RAG_SESSION_DB"])
    repo.migrate()
    return repo


def _chunks(state):
    return tuple(chunk_from_dict(chunk) for chunk in state["chunks"])


def _snapshot(state, threshold=0.3):
    chunks = _chunks(state)
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    return IndexSnapshot(
        version="test",
        chunks=chunks,
        embeddings=state["emb_matrix"],
        bm25=BM25Index.build([chunk.text for chunk in chunks]),
        summary_by_doc={
            doc: by_id[chunk["chunk_id"]]
            for doc, chunk in state["summary_by_doc"].items()
        },
        built_at="test",
        meta={},
    )


def _service(state, llm, *, threshold=0.3, jobs=None):
    retrieval = RetrievalService(
        lambda: _snapshot(state, threshold),
        FakeEmbedder(),
        similarity_threshold=threshold,
    )
    pipeline = RagPipeline(
        retrieval, llm, system_prompt=lambda: server.effective_prompt(), max_tokens=8000
    )
    repo = _repo()
    return ChatService(
        pipeline, repo, NativeRewriter(),
        jobs=jobs,
        max_history_turns=3,
        repeat_penalty=0.85,
        summary_every=6,
    )


def test_first_turn(fake_index, fake_llm, temp_session_db):
    service = _service(fake_index, fake_llm)
    result = service.chat(
        "孙悟空是谁？", fake_llm, session_id="ses_new_chat_1", channel=8000
    )
    from api.presenters import chat_result_to_legacy
    from infra.trace_presenter import TracePresenter
    legacy = chat_result_to_legacy(result, TracePresenter().render)

    assert result.turn_id == 1
    assert "须菩提" in legacy["answer"]
    assert legacy["resolved_query"] == "孙悟空是谁？"
    assert legacy["intent"] == "new_topic"
    assert legacy["sources"]
    assert legacy["citations"]["ok"] is True
    assert {"accept", "bm25", "semantic", "merge", "expand", "context", "generate"} <= {
        step["key"] for step in legacy["steps"]
    }


def test_second_turn(fake_index, fake_llm, temp_session_db):
    service = _service(fake_index, fake_llm)
    first = service.chat(
        "孙悟空是谁？", fake_llm, session_id="ses_new_chat_2", channel=8000
    )
    second = service.chat(
        "他师父呢？", fake_llm, session_id="ses_new_chat_2", channel=8000
    )

    assert first.turn_id == 1
    assert second.turn_id == 2
    assert second.resolved_query == "孙悟空的师父是谁？"
    assert second.answer.memory.turns_used == 1

    repo = _repo()
    assert repo.get("ses_new_chat_2").turn_count == 2
    assert "孙悟空" in repo.entities("ses_new_chat_2")
    assert len(repo.traces("ses_new_chat_2")) == 2
    assert len(repo.window("ses_new_chat_2", 5)) == 2

def test_clarification_short_circuits_generation(fake_index, fake_llm, temp_session_db):
    rewriter = StubRewriter(RewriteResult(
        resolved_query="孙悟空",
        intent=Intent.FOLLOW_UP,
        entities=("孙悟空", "猪八戒"),
        needs_clarification=True,
        clarification_question="你指的是孙悟空还是猪八戒？",
        confidence=0.5,
    ))
    pipeline = StubPipeline(hits=())
    repo = _repo()
    service = ChatService(pipeline, repo, rewriter)

    result = service.chat("他呢？", fake_llm, session_id="ses_clarify_1", channel=8000)

    assert result.answer.text == "你指的是孙悟空还是猪八戒？"
    assert result.flags.get("clarified") == 1
    assert pipeline.generated == 0
    assert repo.get("ses_clarify_1").turn_count == 1


def test_no_evidence_persists_fallback(fake_index, fake_llm, temp_session_db):
    service = _service(fake_index, fake_llm, threshold=1.1)
    result = service.chat(
        "完全不存在的问题", fake_llm, session_id="ses_noevi_1", channel=8000
    )

    assert result.answer.text == "当前知识库中未找到相关内容。"
    assert result.flags.get("no_evidence") == 1
    assert _repo().get("ses_noevi_1").turn_count == 1


def test_summary_job_runs_when_threshold_reached(fake_index, fake_llm, temp_session_db):
    jobs = SyncJobs()
    retrieval = RetrievalService(lambda: _snapshot(fake_index), FakeEmbedder())
    pipeline = RagPipeline(retrieval, fake_llm, system_prompt="SYS", max_tokens=8000)
    repo = _repo()
    service = ChatService(
        pipeline, repo, NativeRewriter(), jobs=jobs, summary_every=1
    )

    service.chat("孙悟空是谁？", fake_llm, session_id="ses_summary_1", channel=8000)

    assert "summary" in jobs.jobs
    session = repo.get("ses_summary_1")
    assert session.summary
    assert session.summary_upto_turn == 1
