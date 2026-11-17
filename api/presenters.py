"""API DTO 转换：领域结果 → 既有响应结构（含 steps 渲染）。

展示层渲染由 infra.TracePresenter 负责，通过 render_trace 回调注入，
避免 api 直接依赖 infra 具体实现。
"""
from __future__ import annotations

from typing import Callable, Sequence

from domain.models import Answer, Source


def sources_to_legacy(sources: Sequence[Source]) -> list[dict]:
    return [
        {"title": source.title, "path": source.path, "type": source.kind}
        for source in sources
    ]


def chat_result_to_legacy(result, render_trace: Callable[[Sequence], list[dict]]) -> dict:
    answer: Answer = result.answer
    return {
        "question": result.question,
        "answer": answer.text,
        "sources": sources_to_legacy(answer.sources),
        "steps": render_trace(answer.trace),
        "session_id": result.session_id,
        "turn_id": result.turn_id,
        "resolved_query": result.resolved_query,
        "intent": result.intent.value,
        "confidence": result.confidence,
        "memory": {
            "turns_used": answer.memory.turns_used,
            "summary_used": answer.memory.summary_used,
            "prompt_tokens": answer.memory.prompt_tokens,
            "truncated": answer.memory.truncated,
        },
        "citations": {
            "ok": answer.citations.ok,
            "invalid": list(answer.citations.invalid),
            "missing": answer.citations.missing,
        },
        "followups": list(answer.followups),
        "flags": dict(result.flags),
        "latency_ms": result.latency_ms,
    }
