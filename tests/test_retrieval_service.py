"""PR-06：检索层契约测试。"""
from __future__ import annotations

import pytest

from domain import ScoredChunk
from ingestion.index_service import IndexSnapshot
from infra.index_files import chunk_from_dict
from retrieval.bm25 import BM25Index, tokenize
from retrieval.expansion import expand_parents
from retrieval.fusion import apply_session_boost, fuse
from retrieval.service import RetrievalService
from retrieval.vector import semantic_search


class FakeEmbedder:
    model = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def embed(self, text: str) -> list[float]:
        if self.fail:
            raise RuntimeError("embedding down")
        return [1.0] + [0.0] * 7

    def embed_many(self, texts):
        return [self.embed(text) for text in texts]


def _domain_chunks(state: dict):
    return tuple(chunk_from_dict(chunk) for chunk in state["chunks"])


def _snapshot(state: dict, chunks, bm25=None):
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    return IndexSnapshot(
        version="test",
        chunks=chunks,
        embeddings=state["emb_matrix"],
        bm25=bm25 or BM25Index.build([chunk.text for chunk in chunks]),
        summary_by_doc={
            doc_path: by_id[chunk["chunk_id"]]
            for doc_path, chunk in state["summary_by_doc"].items()
        },
        built_at="test",
        meta={},
    )


def _ids(hits):
    return [hit.chunk.chunk_id for hit in hits]


def test_tokenize_contract():
    tokens = tokenize("孙悟空 RAG 2026")
    assert "孙悟空" in tokens
    assert "悟空" in tokens
    assert "rag" in tokens
    assert "2026" in tokens


def test_bm25_returns_relevant_chunks(fake_index):
    chunks = _domain_chunks(fake_index)
    bm25 = BM25Index.build([chunk.text for chunk in chunks])
    hits = bm25.search("孙悟空是谁？", 10)
    assert hits
    assert all(0 <= index < len(chunks) for index, _ in hits)


def test_semantic_search_filters_threshold(fake_index):
    chunks = _domain_chunks(fake_index)
    hits = semantic_search(
        chunks, fake_index["emb_matrix"], [1.0] + [0.0] * 7, 20, 0.3
    )
    assert hits
    assert all("孙悟空" in hit.chunk.doc_path for hit in hits)


def test_fuse_and_session_boost(fake_index):
    chunks = _domain_chunks(fake_index)
    bm25 = BM25Index.build([chunk.text for chunk in chunks])
    bm25_hits = [
        ScoredChunk(chunks[index], score, "bm25")
        for index, score in bm25.search("孙悟空是谁？", 10)
    ]
    semantic_hits = semantic_search(
        chunks, fake_index["emb_matrix"], [1.0] + [0.0] * 7, 20, 0.3
    )
    fused = fuse([bm25_hits, semantic_hits, []], chunks)
    assert fused
    assert fused[0].score >= fused[-1].score

    boosted = apply_session_boost(
        fused,
        entity_terms=("孙悟空",),
        doc_boost={"wiki/sources/孙悟空.md": 1.1},
        repeat_ids={"wiki/sources/孙悟空.md#detail0"},
        repeat_penalty=0.85,
    )
    assert _ids(boosted)
    assert boosted[0].score >= fused[-1].score


def test_expand_parents_adds_summary(fake_index):
    chunks = _domain_chunks(fake_index)
    bm25 = BM25Index.build([chunk.text for chunk in chunks])
    hits = [
        ScoredChunk(chunks[index], score, "bm25")
        for index, score in bm25.search("孙悟空是谁？", 10)
    ]
    summary = {
        doc_path: chunk_from_dict(chunk)
        for doc_path, chunk in fake_index["summary_by_doc"].items()
    }
    selected = expand_parents(hits, summary, 30)
    assert selected
    assert any(chunk.kind.value == "summary" for chunk in selected)


@pytest.mark.parametrize("case", ["basic", "dual", "session", "rewrite"])
def test_retrieval_service_contract(fake_index, case):
    chunks = _domain_chunks(fake_index)
    service = RetrievalService(lambda: _snapshot(fake_index, chunks), FakeEmbedder())

    if case == "dual":
        result = service.search("孙悟空的师父是谁？", raw_question="他师父呢？")
    elif case == "session":
        result = service.search(
            "孙悟空是谁？",
            entity_terms=("孙悟空",),
            doc_boost={"wiki/sources/孙悟空.md": 1.1},
            repeat_ids={"wiki/sources/孙悟空.md#detail0"},
            repeat_penalty=0.85,
        )
    elif case == "rewrite":
        result = service.search(
            "孙悟空的师父是谁？",
            use_rewrite=True,
            rewrite_query=lambda _: "菩提祖师",
        )
    else:
        result = service.search("孙悟空是谁？")

    assert result.hits
    assert result.degraded is False


def test_retrieval_service_degrades_to_bm25(fake_index):
    chunks = _domain_chunks(fake_index)
    service = RetrievalService(
        lambda: _snapshot(fake_index, chunks),
        FakeEmbedder(fail=True),
    )
    result = service.search("孙悟空是谁？")
    assert result.degraded is True
    assert result.hits
