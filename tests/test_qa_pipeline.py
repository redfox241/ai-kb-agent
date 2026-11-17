"""PR-07：RagPipeline 契约测试。"""
from __future__ import annotations

import pytest

from domain import AnswerOptions, Chunk, Intent, ModelUnavailable, Turn
from ingestion.index_service import IndexSnapshot
from infra.index_files import chunk_from_dict
from qa.pipeline import RagPipeline
from retrieval.bm25 import BM25Index
from retrieval.service import RetrievalService


class FakeEmbedder:
    model = "fake"

    def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 7

    def embed_many(self, texts):
        return [self.embed(text) for text in texts]


class BadCitationLLM:
    model = "bad"

    def chat(self, messages, *, system=None, max_tokens=None, temperature=None,
             thinking=None):
        return "答案[E9]"


class FailingLLM:
    model = "fail"

    def chat(self, messages, *, system=None, max_tokens=None, temperature=None,
             thinking=None):
        raise RuntimeError("boom")


def _domain_chunks(state: dict) -> tuple[Chunk, ...]:
    return tuple(chunk_from_dict(chunk) for chunk in state["chunks"])


def _snapshot(state: dict, chunks: tuple[Chunk, ...]) -> IndexSnapshot:
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    return IndexSnapshot(
        version="test",
        chunks=chunks,
        embeddings=state["emb_matrix"],
        bm25=BM25Index.build([chunk.text for chunk in chunks]),
        summary_by_doc={
            doc_path: by_id[chunk["chunk_id"]]
            for doc_path, chunk in state["summary_by_doc"].items()
        },
        built_at="test",
        meta={},
    )


def _pipeline(fake_index, llm, *, threshold: float = 0.3) -> RagPipeline:
    chunks = _domain_chunks(fake_index)
    retrieval = RetrievalService(
        lambda: _snapshot(fake_index, chunks),
        FakeEmbedder(),
        similarity_threshold=threshold,
    )
    return RagPipeline(
        retrieval,
        llm,
        system_prompt="SYS",
        max_tokens=8000,
        max_context_chunks=30,
        max_sources=10,
        max_prompt_tokens=12000,
        max_history_turns=3,
    )


def test_single_turn_contract(fake_index, fake_llm):
    pipeline = _pipeline(fake_index, fake_llm)
    answer = pipeline.answer("孙悟空是谁？", AnswerOptions(rewrite=False))

    assert "须菩提" in answer.text
    assert answer.sources
    assert answer.evidence
    assert answer.citations.ok is True
    keys = [event.step for event in answer.trace]
    assert {"accept", "bm25", "semantic", "merge", "expand", "context",
            "generate", "cite"} <= set(keys)


def test_multi_turn_contract(fake_index, fake_llm):
    pipeline = _pipeline(fake_index, fake_llm)
    history = (Turn(
        turn_id=1,
        session_id="ses_pipeline_1",
        question="孙悟空是谁？",
        resolved_query="孙悟空是谁？",
        intent=Intent.NEW_TOPIC,
        answer="须菩提祖师。[E1]",
    ),)
    answer = pipeline.answer(
        "孙悟空的师父是谁？",
        AnswerOptions(
            rewrite=False,
            raw_question="他师父呢？",
            entities=("孙悟空",),
            history=history,
            summary="摘要",
            multiturn=True,
        ),
    )

    assert answer.text
    assert answer.memory.turns_used == 1
    assert answer.memory.summary_used is True
    assert answer.memory.prompt_tokens is not None
    generation = [call for call in fake_llm.calls if call["system"] != ""]
    assert generation


def test_empty_evidence_returns_fallback(fake_index, fake_llm):
    pipeline = _pipeline(fake_index, fake_llm, threshold=1.1)
    answer = pipeline.answer("完全不存在的问题", AnswerOptions(rewrite=False))
    assert answer.text == "当前知识库中未找到相关内容。"
    assert answer.sources == ()
    assert answer.evidence == ()


def test_invalid_citation_is_reported(fake_index):
    pipeline = _pipeline(fake_index, BadCitationLLM())
    answer = pipeline.answer("孙悟空是谁？", AnswerOptions(rewrite=False))
    assert answer.citations.ok is False
    assert "E9" in answer.citations.invalid


def test_llm_failure_maps_to_domain_error(fake_index):
    pipeline = _pipeline(fake_index, FailingLLM())
    with pytest.raises(ModelUnavailable):
        pipeline.answer("孙悟空是谁？", AnswerOptions(rewrite=False))
