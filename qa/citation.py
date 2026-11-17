"""引用校验与追问建议。"""
from __future__ import annotations

import re
from typing import Mapping, Sequence

from domain.models import Citation, CitationCheck, Evidence


CITE_RE = re.compile(r"\[(E\d+)\]")


def _field(item: object, key: str, default: object = "") -> object:
    if isinstance(item, Evidence):
        return getattr(item.chunk, key, default)
    if isinstance(item, Mapping):
        return item.get(key, default)
    value = getattr(item, key, default)
    return default if value is None else value


def validate_citations(answer: str, evidence: Sequence[object]) -> CitationCheck:
    """只认本轮证据编号，防止模型引用历史或编造。"""
    valid = {f"E{index}" for index in range(1, len(evidence or []) + 1)}
    cited = set(CITE_RE.findall(answer or ""))
    invalid = sorted(cited - valid)
    missing = bool(evidence) and not (cited & valid) and len(answer or "") > 200
    return CitationCheck(ok=not invalid and not missing,
                         invalid=tuple(invalid), missing=missing)


def citations_from_answer(answer: str, evidence: Sequence[object]) -> tuple[Citation, ...]:
    """把答案中的 [E?] 映射为可落库的引用条目。"""
    output: list[Citation] = []
    seen: set[tuple[object, object]] = set()
    for token in CITE_RE.findall(answer or ""):
        try:
            index = int(token[1:]) - 1
        except ValueError:
            continue
        if index < 0 or index >= len(evidence):
            continue
        item = evidence[index]
        key = (_field(item, "doc_path", ""), _field(item, "chunk_id", ""))
        if key in seen:
            continue
        seen.add(key)
        output.append(Citation(
            id=f"E{index + 1}",
            title=str(_field(item, "title", "")),
            doc_path=str(_field(item, "doc_path", "")),
            chunk_id=str(_field(item, "chunk_id", "")),
        ))
    return tuple(output)


def followup_suggestions(
    evidence: Sequence[object],
    entities: Sequence[str] = (),
    *,
    enabled: bool = True,
    limit: int = 3,
) -> tuple[str, ...]:
    if not enabled:
        return ()

    titles: list[str] = []
    for item in evidence or []:
        title = str(_field(item, "title", "") or "")
        if title and title not in titles:
            titles.append(title)

    suggestions = [f"《{title}》的核心要点是什么？" for title in titles[:2]]
    if entities:
        suggestions.append(f"{entities[0]}还有哪些相关内容？")
    return tuple(suggestions[:limit])
