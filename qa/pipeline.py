"""问答流水线：检索 → 扩展 → 上下文 → 生成 → 引用 → 追问。"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Sequence

from domain.errors import ModelUnavailable
from domain.models import (
    Answer,
    AnswerOptions,
    Chunk,
    CitationCheck,
    Evidence,
    MemoryStats,
    ScoredChunk,
    SearchResult,
    Source,
    StepStatus,
    TraceEvent,
)
from domain.ports import LLM
from .trace import TraceBuilder, context_io, generation_io
from retrieval.service import RetrievalService

from .citation import followup_suggestions, validate_citations
from .context import build, build_context


SystemPrompt = Callable[[], str] | str


class RagPipeline:
    def __init__(
        self,
        retrieval: RetrievalService,
        llm: LLM,
        *,
        system_prompt: SystemPrompt,
        max_tokens: int = 8000,
        max_context_chunks: int = 30,
        max_sources: int = 10,
        max_prompt_tokens: int = 12000,
        max_history_turns: int = 3,
        followup_suggest: bool = True,
        prompt_version: str = "",
    ) -> None:
        self._retrieval = retrieval
        self._llm = llm
        self._system_prompt = system_prompt if callable(system_prompt) else (lambda: system_prompt)
        self._max_tokens = int(max_tokens)
        self._max_context_chunks = int(max_context_chunks)
        self._max_sources = int(max_sources)
        self._max_prompt_tokens = int(max_prompt_tokens)
        self._max_history_turns = int(max_history_turns)
        self._followup_suggest = bool(followup_suggest)
        self._prompt_version = str(prompt_version or "")

    # ---------- 对外 ----------

    def answer(self, question: str, options: AnswerOptions | None = None) -> Answer:
        options = options or AnswerOptions()
        search = self.retrieve(question, options)
        return self.generate(
            question,
            search,
            options,
            resolved_query=question,
            prefix_trace=search.trace,
        )

    def retrieve(self, question: str, options: AnswerOptions | None = None) -> SearchResult:
        options = options or AnswerOptions()
        builder = TraceBuilder()
        started = time.perf_counter()

        search = self._retrieval.search(
            question,
            raw_question=options.raw_question,
            entity_terms=options.entities,
            doc_boost=options.doc_boost,
            repeat_ids=options.repeat_ids,
            repeat_penalty=options.repeat_penalty,
            use_rewrite=options.rewrite,
        )
        elapsed = _ms(started)

        dual = bool(
            options.raw_question
            and options.raw_question.strip()
            and options.raw_question.strip() != question.strip()
        )
        builder.add(
            "accept",
            StepStatus.OK,
            summary=f"有效 {len(question.strip())} 字 · 改写{'启用' if options.rewrite else '关闭'}",
            detail=f"查询：{question}",
            ms=elapsed,
            ops=[
                f"接收问题：「{question}」",
                f"规范化：去除首尾空白，有效长度 {len(question.strip())} 字",
                "检索策略：BM25 Top-10 + 语义 Top-20",
                f"查询改写：{'启用' if options.rewrite else '关闭'}",
            ],
        )
        builder.add(
            "bm25",
            StepStatus.OK if search.bm25_count else StepStatus.EMPTY,
            summary=f"命中 {search.bm25_count} 条" + ("（双路：原文 + 改写）" if dual else ""),
            detail=f"查询：{question}" + (f" ｜ 原文：{options.raw_question}" if dual else ""),
            items=[_hit_item(hit) for hit in search.bm25_hits],
            ops=[
                "对问题分词，在 BM25 索引上计算词频 / 逆文档频率得分",
                f"输出：{search.bm25_count} 条候选",
            ],
        )

        semantic_status = StepStatus.OK if search.semantic_count else StepStatus.EMPTY
        if search.degraded and "embedding" in search.reason:
            semantic_status = StepStatus.ERROR
        builder.add(
            "semantic",
            semantic_status,
            summary=(f"命中 {search.semantic_count} 条" if semantic_status is not StepStatus.ERROR
                     else "embedding 服务不可用，本路跳过"),
            detail=search.reason if semantic_status is StepStatus.ERROR else "",
            items=[_hit_item(hit) for hit in search.semantic_hits],
            ops=[
                "调用 embedding 接口，把问题编码为归一化向量",
                "与索引中全部知识块向量做点积（余弦相似度）",
                f"输出：{search.semantic_count} 条候选",
            ],
        )

        if not options.rewrite:
            rewrite_status = StepStatus.SKIP
            rewrite_summary = "跳过（未启用改写）"
        elif search.rewrite_count:
            rewrite_status = StepStatus.OK
            rewrite_summary = f"改写语义命中 {search.rewrite_count} 条"
        else:
            rewrite_status = StepStatus.EMPTY
            rewrite_summary = "改写未产生新的召回"
        builder.add(
            "rewrite",
            rewrite_status,
            summary=rewrite_summary,
            detail=f"原问题：{question}",
            items=[_hit_item(hit) for hit in search.rewrite_hits],
            ops=["调用 LLM 改写问题", "对改写结果再次做向量检索"],
        )

        builder.add(
            "merge",
            StepStatus.OK if search.fused_count else StepStatus.EMPTY,
            summary=f"合并去重后 {search.fused_count} 条（BM25:语义:改写 = 1:2:1）",
            items=[_hit_item(hit) for hit in search.hits[:15]],
            ops=[
                f"接收三路候选：BM25 {search.bm25_count} / 语义 {search.semantic_count} / 改写 {search.rewrite_count}",
                "按 chunk_id 去重，同一知识块得分累加",
                "按权重 1 : 2 : 1 加权，wiki/knowledge ×1.15",
                "应用会话软加权后输出",
            ],
        )
        return replace(search, trace=builder.trace)

    def expand(self, search: SearchResult) -> list[Chunk]:
        return self._retrieval.expand(search.hits, limit=self._max_context_chunks)

    def generate(
        self,
        question: str,
        search: SearchResult,
        options: AnswerOptions | None = None,
        *,
        resolved_query: str | None = None,
        prefix_trace: Sequence[TraceEvent] = (),
    ) -> Answer:
        options = options or AnswerOptions()
        builder = TraceBuilder()
        builder.extend(prefix_trace)

        started = time.perf_counter()
        selected = self._retrieval.expand(search.hits, limit=self._max_context_chunks)
        builder.add(
            "expand",
            StepStatus.OK if selected else StepStatus.EMPTY,
            summary=f"融合 {len(search.hits)} 条 → 选取 {len(selected)} 个知识块（每文件 摘要 + top2 正文）",
            items=[_chunk_item(chunk) for chunk in selected],
            ms=_ms(started),
            ops=[
                f"输入：融合排序后的 {len(search.hits)} 条候选",
                "按 doc_path 分组，每组补回摘要块 + 正文 Top2",
                f"输出：{len(selected)} 个知识块",
            ],
        )

        if not selected:
            builder.add(
                "generate",
                StepStatus.SKIP,
                summary="未检索到相关内容，跳过生成",
                ops=["上下文为空", "返回兜底提示，不调用 LLM"],
            )
            return Answer(
                text="当前知识库中未找到相关内容。",
                sources=(),
                evidence=(),
                citations=CitationCheck(ok=True),
                followups=(),
                trace=builder.trace,
                memory=MemoryStats(
                    turns_used=len(options.history),
                    summary_used=bool(options.summary),
                ),
            )

        evidence = tuple(
            Evidence(evidence_id=f"E{index}", chunk=chunk, score=0.0, rank=index)
            for index, chunk in enumerate(selected, 1)
        )
        sources = _sources(selected, self._max_sources)

        query_for_prompt = resolved_query or question
        system_prompt = self._system_prompt()
        started = time.perf_counter()
        if options.multiturn or options.history or options.summary:
            raw_question = options.raw_question or (
                question if query_for_prompt != question else None
            )
            built = build(
                system_prompt,
                options.summary,
                options.history,
                selected,
                query_for_prompt,
                raw_question=raw_question,
                budget=self._max_prompt_tokens,
                max_turns=self._max_history_turns,
            )
            system = built.system
            messages = list(built.messages)
            user_prompt = messages[-1]["content"]
            context = built.evidence_text
            memory = MemoryStats(
                turns_used=len(options.history),
                summary_used=bool(options.summary),
                prompt_tokens=built.prompt_tokens,
                truncated=built.truncated,
            )
        else:
            context = build_context(selected)
            user_prompt = (
                f"## 检索到的知识库内容\n\n{context}\n\n"
                f"## 用户问题\n{question}"
            )
            system = system_prompt
            messages = [{"role": "user", "content": user_prompt}]
            memory = MemoryStats()

        builder.add(
            "context",
            StepStatus.OK if context.strip() else StepStatus.EMPTY,
            summary=f"{len(selected)} 个知识块 · {len(context)} 字符",
            detail="来源行格式：[来源: 标题 | 类型 | 路径 | 时间]",
            ms=_ms(started),
            ops=[
                "为每个知识块前置来源行",
                "按父子扩展顺序拼接正文与空行",
                f"输出：{len(selected)} 块 · {len(context)} 字符上下文",
            ],
            io=context_io(question, selected, context),
        )

        started = time.perf_counter()
        try:
            answer = self._llm.chat(messages, system=system, max_tokens=self._max_tokens)
        except Exception as exc:
            builder.add(
                "generate",
                StepStatus.ERROR,
                summary=f"生成失败：{exc}",
                detail=_model_detail(self._llm, self._max_tokens, self._prompt_version),
                ms=_ms(started),
                ops=["调用模型失败", "本次未产生答案"],
                io=generation_io(
                    question, context, system, user_prompt,
                    str(getattr(self._llm, "model", "unknown")),
                    self._max_tokens, sources=[_source_item(source) for source in sources],
                    error=exc,
                ),
            )
            raise ModelUnavailable(
                "答案生成失败",
                detail={"model": getattr(self._llm, "model", "unknown")},
            ) from exc

        answer = answer or ""
        elapsed = _ms(started)
        builder.add(
            "generate",
            StepStatus.OK if answer.strip() else StepStatus.EMPTY,
            summary=f"生成 {len(answer)} 字",
            detail=_model_detail(self._llm, self._max_tokens, self._prompt_version),
            ms=elapsed,
            items=[_source_item(source) for source in sources],
            ops=[
                f"拼装 prompt：上下文 {len(context)} 字符 + 用户问题「{query_for_prompt}」",
                "调用模型完成答案生成",
                f"返回 {len(answer)} 字，耗时 {elapsed}ms",
            ],
            io=generation_io(
                question, context, system, user_prompt,
                str(getattr(self._llm, "model", "unknown")),
                self._max_tokens, answer=answer, elapsed_ms=elapsed,
                sources=[_source_item(source) for source in sources],
            ),
        )

        citations = validate_citations(answer, evidence)
        builder.add(
            "cite",
            StepStatus.OK if citations.ok else StepStatus.EMPTY,
            summary=("引用校验通过" if citations.ok else
                     f"引用异常：invalid={list(citations.invalid)} missing={citations.missing}"),
            ops=["只允许引用本轮证据编号 [E1]..[En]", "校验答案中的引用标记"],
        )
        followups = followup_suggestions(
            evidence,
            options.entities,
            enabled=self._followup_suggest,
        )
        return Answer(
            text=answer,
            sources=sources,
            evidence=evidence,
            citations=citations,
            followups=followups,
            trace=builder.trace,
            memory=memory,
        )


def _model_detail(llm, max_tokens: int, prompt_version: str = "") -> str:
    detail = f"模型 {getattr(llm, 'model', 'unknown')} · max_tokens {max_tokens}"
    if prompt_version:
        detail += f" · prompt {prompt_version}"
    return detail


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _hit_item(hit: ScoredChunk) -> dict:
    return {
        "title": hit.chunk.title,
        "path": hit.chunk.doc_path,
        "type": hit.chunk.kind.value,
        "chunk_id": hit.chunk.chunk_id,
        "score": round(float(hit.score), 4),
        "snippet": hit.chunk.text[:200],
    }


def _chunk_item(chunk: Chunk) -> dict:
    return {
        "title": chunk.title,
        "path": chunk.doc_path,
        "type": chunk.kind.value,
        "chunk_id": chunk.chunk_id,
        "score": None,
        "snippet": chunk.text[:200],
    }


def _source_item(source: Source) -> dict:
    return {"title": source.title, "path": source.path, "type": source.kind}


def _sources(selected: Sequence[Chunk], limit: int) -> tuple[Source, ...]:
    sources: list[Source] = []
    seen: set[tuple[str, str]] = set()
    for chunk in selected:
        key = (chunk.title, chunk.doc_path)
        if key in seen:
            continue
        seen.add(key)
        sources.append(Source(title=chunk.title, path=chunk.doc_path, kind=chunk.kind.value))
    return tuple(sources[:limit])
