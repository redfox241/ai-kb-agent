"""父子捆绑：按文件分组，摘要块 + 正文 top-2。"""
from __future__ import annotations

from typing import Mapping, Sequence

from domain.models import Chunk, ScoredChunk


def expand_parents(
    hits: Sequence[ScoredChunk],
    summary_by_doc: Mapping[str, Chunk],
    limit: int,
) -> list[Chunk]:
    by_doc: dict[str, list[ScoredChunk]] = {}
    for hit in hits:
        by_doc.setdefault(hit.chunk.doc_path, []).append(hit)

    selected: list[Chunk] = []
    seen: set[str] = set()
    for doc_path, items in by_doc.items():
        items.sort(key=lambda item: -item.score)
        summary = summary_by_doc.get(doc_path)
        if summary is not None and summary.chunk_id not in seen:
            selected.append(summary)
            seen.add(summary.chunk_id)
        for hit in items[:2]:
            if hit.chunk.chunk_id not in seen:
                selected.append(hit.chunk)
                seen.add(hit.chunk.chunk_id)
    return selected[:limit]
