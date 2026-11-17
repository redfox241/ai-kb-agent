"""向量召回：归一化点积 + 阈值过滤。"""
from __future__ import annotations

from typing import Literal, Sequence

import numpy as np

from domain.models import Chunk, ScoredChunk


def semantic_search(
    chunks: Sequence[Chunk],
    embeddings: np.ndarray,
    query_vector: Sequence[float],
    top_k: int,
    threshold: float,
    *,
    source: Literal["semantic", "rewrite"] = "semantic",
) -> list[ScoredChunk]:
    if embeddings.ndim != 2 or embeddings.shape[0] == 0:
        return []
    if embeddings.shape[0] != len(chunks):
        raise ValueError("chunks 与 embeddings 行数不一致")

    query = np.asarray(query_vector, dtype=np.float32)
    norm = float(np.linalg.norm(query)) or 1.0
    query = query / norm
    scores = embeddings @ query

    indices = np.argsort(-scores)[:top_k]
    results: list[ScoredChunk] = []
    for index in indices:
        score = float(scores[index])
        if score >= threshold:
            results.append(ScoredChunk(chunk=chunks[int(index)], score=score, source=source))
    return results
