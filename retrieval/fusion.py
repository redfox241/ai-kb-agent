"""融合排序与会话软加权。"""
from __future__ import annotations

from typing import Mapping, Sequence

from domain.models import Chunk, ScoredChunk


WEIGHTS = (1.0, 2.0, 1.0)  # BM25 : semantic : rewrite-semantic
BOOST_PREFIXES = ("wiki/", "knowledge/")
BOOST = 1.15


def fuse(
    lists: Sequence[Sequence[ScoredChunk]],
    chunks: Sequence[Chunk],
    *,
    weights: Sequence[float] = WEIGHTS,
    boost_prefixes: Sequence[str] = BOOST_PREFIXES,
    boost: float = BOOST,
) -> list[ScoredChunk]:
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    aggregated: dict[str, float] = {}

    for hits, weight in zip(lists, weights):
        for hit in hits:
            doc_path = hit.chunk.doc_path
            hit_boost = boost if doc_path.startswith(tuple(boost_prefixes)) else 1.0
            aggregated[hit.chunk.chunk_id] = (
                aggregated.get(hit.chunk.chunk_id, 0.0) + hit.score * weight * hit_boost
            )

    ranked = sorted(aggregated.items(), key=lambda item: -item[1])
    return [
        ScoredChunk(chunk=by_id[chunk_id], score=score, source="fused")
        for chunk_id, score in ranked
        if chunk_id in by_id
    ]


def apply_session_boost(
    hits: Sequence[ScoredChunk],
    *,
    entity_terms: Sequence[str] = (),
    doc_boost: Mapping[str, float] | None = None,
    repeat_ids: Sequence[str] | set[str] | frozenset[str] = (),
    repeat_penalty: float = 1.0,
) -> list[ScoredChunk]:
    entity_terms = set(entity_terms or ())
    doc_boost = dict(doc_boost or {})
    repeat_ids = set(repeat_ids or ())

    if not entity_terms and not doc_boost and not (repeat_ids and repeat_penalty < 1.0):
        return list(hits)

    boosted: list[ScoredChunk] = []
    for hit in hits:
        score = hit.score
        chunk = hit.chunk
        if chunk.title in entity_terms or chunk.doc_path in entity_terms:
            score *= 1.15
        for prefix, weight in doc_boost.items():
            if chunk.doc_path.startswith(prefix):
                score *= weight
                break
        if repeat_ids and chunk.chunk_id in repeat_ids:
            score *= repeat_penalty
        boosted.append(ScoredChunk(chunk=chunk, score=score, source=hit.source))

    return sorted(boosted, key=lambda item: -item.score)
