"""检索用例：三路召回 → 融合 → 会话软加权。"""
from __future__ import annotations

from typing import Callable, Mapping, Protocol, Sequence

import numpy as np

from domain.errors import IndexNotReady
from domain.models import Chunk, ScoredChunk, SearchResult
from domain.ports import Embedder

from .expansion import expand_parents
from .fusion import BOOST, BOOST_PREFIXES, WEIGHTS, apply_session_boost, fuse
from .vector import semantic_search


class BM25Like(Protocol):
    def search(self, query: str, top_k: int) -> list[tuple[int, float]]: ...


class RetrievalIndex(Protocol):
    chunks: Sequence[Chunk]
    embeddings: np.ndarray
    bm25: object | None
    summary_by_doc: Mapping[str, Chunk]


RewriteFn = Callable[[str], str]


class RetrievalService:
    def __init__(
        self,
        index_provider: Callable[[], RetrievalIndex],
        embedder: Embedder,
        *,
        bm25_k: int = 10,
        semantic_k: int = 20,
        rewrite_k: int = 10,
        similarity_threshold: float = 0.3,
        weights: Sequence[float] = WEIGHTS,
        boost_prefixes: Sequence[str] = BOOST_PREFIXES,
        boost: float = BOOST,
    ) -> None:
        self._index_provider = index_provider
        self._embedder = embedder
        self._bm25_k = int(bm25_k)
        self._semantic_k = int(semantic_k)
        self._rewrite_k = int(rewrite_k)
        self._threshold = float(similarity_threshold)
        self._weights = tuple(weights)
        self._boost_prefixes = tuple(boost_prefixes)
        self._boost = float(boost)

    def search(
        self,
        query: str,
        *,
        raw_question: str | None = None,
        entity_terms: Sequence[str] = (),
        doc_boost: Mapping[str, float] | None = None,
        repeat_ids: Sequence[str] | set[str] | frozenset[str] = (),
        repeat_penalty: float = 1.0,
        use_rewrite: bool = False,
        rewrite_query: RewriteFn | None = None,
    ) -> SearchResult:
        index = self._index_provider()
        if not index.chunks or index.embeddings.ndim != 2 or index.embeddings.shape[0] == 0:
            raise IndexNotReady("索引未就绪", detail={"status": "empty"})
        if index.bm25 is None:
            raise IndexNotReady("BM25 索引未就绪", detail={"status": "ready", "reason": "bm25_missing"})

        reasons: list[str] = []

        bm25_hits = self._bm25_hits(index, query, raw_question)

        query_vector: Sequence[float] | None = None
        try:
            query_vector = self._embedder.embed(query)
        except Exception as exc:
            reasons.append(f"embedding: {exc}")

        semantic_hits: list[ScoredChunk] = []
        if query_vector is not None:
            semantic_hits = semantic_search(
                index.chunks,
                index.embeddings,
                query_vector,
                self._semantic_k,
                self._threshold,
            )

        rewrite_hits: list[ScoredChunk] = []
        if use_rewrite and query_vector is not None and len(query) > 8 and rewrite_query is not None:
            try:
                rewritten = (rewrite_query(query) or "").strip()
                if rewritten and rewritten != query.strip():
                    rewrite_hits = semantic_search(
                        index.chunks,
                        index.embeddings,
                        self._embedder.embed(rewritten),
                        self._rewrite_k,
                        self._threshold,
                        source="rewrite",
                    )
            except Exception as exc:
                reasons.append(f"rewrite: {exc}")

        fused = fuse(
            [bm25_hits, semantic_hits, rewrite_hits],
            index.chunks,
            weights=self._weights,
            boost_prefixes=self._boost_prefixes,
            boost=self._boost,
        )
        boosted = apply_session_boost(
            fused,
            entity_terms=entity_terms,
            doc_boost=doc_boost,
            repeat_ids=repeat_ids,
            repeat_penalty=repeat_penalty,
        )
        return SearchResult(
            hits=tuple(boosted),
            degraded=bool(reasons),
            reason="; ".join(reasons),
            bm25_hits=tuple(bm25_hits),
            semantic_hits=tuple(semantic_hits),
            rewrite_hits=tuple(rewrite_hits),
            bm25_count=len(bm25_hits),
            semantic_count=len(semantic_hits),
            rewrite_count=len(rewrite_hits),
            fused_count=len(fused),
        )

    def expand(
        self,
        hits: Sequence[ScoredChunk],
        *,
        limit: int,
    ) -> list[Chunk]:
        index = self._index_provider()
        return expand_parents(hits, index.summary_by_doc, limit)

    def _bm25_hits(
        self,
        index: RetrievalIndex,
        query: str,
        raw_question: str | None,
    ) -> list[ScoredChunk]:
        bm25: BM25Like = index.bm25  # type: ignore[assignment]
        dual = bool(
            raw_question
            and raw_question.strip()
            and raw_question.strip() != query.strip()
        )

        if dual:
            pool: dict[str, ScoredChunk] = {}
            for chunk_index, score in (
                bm25.search(query, self._bm25_k)
                + bm25.search(raw_question or "", self._bm25_k)
            ):
                chunk = index.chunks[chunk_index]
                previous = pool.get(chunk.chunk_id)
                if previous is None or score > previous.score:
                    pool[chunk.chunk_id] = ScoredChunk(chunk=chunk, score=score, source="bm25")
            return sorted(pool.values(), key=lambda hit: -hit.score)[: self._bm25_k]

        return [
            ScoredChunk(chunk=index.chunks[chunk_index], score=score, source="bm25")
            for chunk_index, score in bm25.search(query, self._bm25_k)
        ]
