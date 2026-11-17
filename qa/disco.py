"""Disco 问答服务：检索 → 话语分析 → 上下文 → 生成。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from domain.models import (
    Answer,
    AnswerOptions,
    Evidence,
    CitationCheck,
    Source,
    StepStatus,
)
from .trace import TraceBuilder, context_io, generation_io

from .citation import validate_citations
from .context import build_context
from .pipeline import RagPipeline


@dataclass(frozen=True)
class DiscoResult:
    answer: Answer
    disco: Mapping[str, object]


def _disco_to_text(disco: object) -> str:
    if not isinstance(disco, Mapping):
        return str(disco or "")
    parts = []
    for key, label in (("trees", "论证树"), ("graph", "关系网"), ("outline", "提纲")):
        value = str(disco.get(key) or "").strip()
        if value:
            parts.append(f"### {label}\n{value}")
    return "\n\n".join(parts)


class DiscoService:
    def __init__(
        self,
        pipeline: RagPipeline,
        llm,
        disco_analyze: Callable,
        *,
        system_prompt: Callable[[], str],
        top_n: int = 15,
        max_sources: int = 10,
        max_tokens: int = 8000,
    ) -> None:
        self._pipeline = pipeline
        self._llm = llm
        self._disco_analyze = disco_analyze
        self._system_prompt = system_prompt
        self._top_n = int(top_n)
        self._max_sources = int(max_sources)
        self._max_tokens = int(max_tokens)

    def answer(self, question: str) -> DiscoResult:
        builder = TraceBuilder()
        search = self._pipeline.retrieve(question, AnswerOptions(rewrite=True))
        builder.extend(search.trace)
        selected = self._pipeline.expand(search)
        builder.add(
            "expand",
            StepStatus.OK if selected else StepStatus.EMPTY,
            summary=f"融合 {len(search.hits)} 条 → 选取 {len(selected)} 个知识块",
            items=[_chunk_item(chunk) for chunk in selected],
            ops=[
                f"输入：融合排序后的 {len(search.hits)} 条候选",
                "按 doc_path 分组，每组补回摘要块 + 正文 Top2",
                f"输出：{len(selected)} 个知识块",
            ],
        )
        if not selected:
            builder.add("generate", StepStatus.SKIP,
                        summary="未检索到相关内容，跳过生成")
            return DiscoResult(
                answer=Answer(
                    text="当前知识库中未找到相关内容。",
                    sources=(), evidence=(),
                    citations=CitationCheck(ok=True),
                    trace=builder.trace,
                ),
                disco={},
            )

        evidence = tuple(
            Evidence(f"E{index}", chunk, 0.0, index)
            for index, chunk in enumerate(selected, 1)
        )
        disco_data = self._disco_analyze(question, selected[: self._top_n])
        disco_text = _disco_to_text(disco_data)
        builder.add(
            "disco",
            StepStatus.OK,
            summary=f"基于 top{len(selected[: self._top_n])} 知识块生成论证树 / 关系网 / 提纲",
            detail=disco_text[:600],
            ops=["取知识块作为话语分析输入", "调用 LLM 生成论证树、关系网与回答提纲"],
        )

        context = build_context(selected)
        builder.add(
            "context",
            StepStatus.OK if context.strip() else StepStatus.EMPTY,
            summary=f"{len(selected)} 个知识块 · {len(context)} 字符",
            io=context_io(question, selected, context),
            ops=["为每个知识块前置来源行", "拼接上下文"],
        )

        sources = _sources(selected, self._max_sources)
        system = self._system_prompt()
        user_prompt = (
            f"## 检索到的知识库内容\n\n{context}\n\n"
            f"## 话语分析\n{disco_text}\n\n## 用户问题\n{question}"
        )
        import time
        started = time.perf_counter()
        try:
            answer = self._llm.chat(
                [{"role": "user", "content": user_prompt}],
                system=system,
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            builder.add(
                "generate", StepStatus.ERROR,
                summary=f"生成失败：{exc}",
                io=generation_io(
                    question, context, system, user_prompt,
                    getattr(self._llm, "model", "unknown"), self._max_tokens,
                    sources=[_source_item(source) for source in sources], error=exc,
                ),
            )
            raise
        elapsed = int((time.perf_counter() - started) * 1000)
        answer = answer or ""
        builder.add(
            "generate", StepStatus.OK if answer.strip() else StepStatus.EMPTY,
            summary=f"生成 {len(answer)} 字",
            ms=elapsed,
            items=[_source_item(source) for source in sources],
            io=generation_io(
                question, context, system, user_prompt,
                getattr(self._llm, "model", "unknown"), self._max_tokens,
                answer=answer, elapsed_ms=elapsed,
                sources=[_source_item(source) for source in sources],
            ),
        )
        citations = validate_citations(answer, evidence)
        builder.add(
            "cite", StepStatus.OK if citations.ok else StepStatus.EMPTY,
            summary="引用校验通过" if citations.ok else "引用异常",
        )
        return DiscoResult(
            answer=Answer(
                text=answer,
                sources=sources,
                evidence=evidence,
                citations=citations,
                trace=builder.trace,
            ),
            disco=disco_data if isinstance(disco_data, Mapping) else {},
        )


def _chunk_item(chunk) -> dict:
    return {
        "title": chunk.title, "path": chunk.doc_path, "type": chunk.kind.value,
        "chunk_id": chunk.chunk_id, "score": None, "snippet": chunk.text[:200],
    }


def _source_item(source: Source) -> dict:
    return {"title": source.title, "path": source.path, "type": source.kind}


def _sources(selected, limit: int) -> tuple[Source, ...]:
    output: list[Source] = []
    seen: set[tuple[str, str]] = set()
    for chunk in selected:
        key = (chunk.title, chunk.doc_path)
        if key in seen:
            continue
        seen.add(key)
        output.append(Source(chunk.title, chunk.doc_path, chunk.kind.value))
    return tuple(output[:limit])
