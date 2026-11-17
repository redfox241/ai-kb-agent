"""多轮问答编排：改写 → 检索 → 生成 → 引用 → 落库 → 摘要。

新实现不依赖 server 模块，通过 Pipeline / Rewriter / SessionRepository 注入。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Protocol, Sequence

from domain.models import (
    Answer,
    AnswerOptions,
    Citation,
    CitationCheck,
    Intent,
    MemoryStats,
    RewriteResult,
    SearchResult,
    SessionState,
    StepStatus,
    Turn,
)
from domain.ports import JobRunner, SessionRepository
from .trace import TraceBuilder

from conversations.summarizer import Summarizer

from .citation import citations_from_answer
from .context import estimate_tokens
from .prompts import default_registry


CHAT_CONT_RE = re.compile(r"(继续|展开|详细|再说|然后|为什么|怎么做到|举个例子|具体说说|多说)")


class Rewriter(Protocol):
    def resolve(
        self,
        question: str,
        window: Sequence[object],
        summary: str,
        entities: Sequence[str],
        llm,
        mode: str = "auto",
        intent_hint: Intent | None = None,
    ) -> RewriteResult: ...

    def update_entity_slots(
        self,
        prev_entities: Sequence[str],
        result: RewriteResult,
        turn_id: int,
    ) -> tuple[list[str], list[str]]: ...


class Pipeline(Protocol):
    def retrieve(self, question: str, options: AnswerOptions | None = None) -> SearchResult: ...

    def generate(
        self,
        question: str,
        search: SearchResult,
        options: AnswerOptions | None = None,
        *,
        resolved_query: str | None = None,
        prefix_trace: Sequence[object] = (),
    ) -> Answer: ...


@dataclass(frozen=True)
class ChatResult:
    question: str
    answer: Answer
    session_id: str | None
    turn_id: int | None
    resolved_query: str
    intent: Intent
    confidence: float
    flags: Mapping[str, int]
    citations: tuple[Citation, ...] = ()
    latency_ms: int = 0


class ChatService:
    def __init__(
        self,
        pipeline: Pipeline,
        sessions: SessionRepository,
        rewriter: Rewriter,
        *,
        jobs: JobRunner | None = None,
        max_history_turns: int = 3,
        repeat_penalty: float = 0.85,
        summary_every: int = 6,
        rewrite_llm=None,
    ) -> None:
        self._pipeline = pipeline
        self._sessions = sessions
        self._rewriter = rewriter
        self._jobs = jobs
        self._max_history_turns = int(max_history_turns)
        self._repeat_penalty = float(repeat_penalty)
        self._rewrite_llm = rewrite_llm
        self._summarizer = Summarizer(sessions, jobs=jobs, every=summary_every)

    def chat(
        self,
        question: str,
        llm,
        session_id: str | None = None,
        history: Sequence[object] | None = None,
        summary: str = "",
        entities: Sequence[str] | None = None,
        use_rewrite: bool = True,
        channel: int = 0,
        rewrite_mode: str = "auto",
    ) -> ChatResult:
        started = time.time()
        flags: dict[str, int] = {}

        sess: SessionState | None = None
        recent_docs: list[str] = []
        recent_ids: set[str] = set()
        if session_id:
            sess = self._sessions.get_or_create(session_id, channel)
            sid = sess.session_id
            window = tuple(self._sessions.window(sid, self._max_history_turns))
            summary = sess.summary
            entities = self._sessions.entities(sid)
            recent_docs = self._sessions.recent_doc_paths(sid, 2)
            recent_ids = self._sessions.recent_chunk_ids(sid, 3)
        else:
            sid = None
            window = tuple(_as_turn(item) for item in (history or [])[-self._max_history_turns:])
            entities = list(entities or [])

        if use_rewrite:
            rewrite = self._rewriter.resolve(
                question, window, summary, entities,
                self._rewrite_llm or llm,
                mode=rewrite_mode,
            )
        else:
            rewrite = RewriteResult(
                resolved_query=question,
                intent=Intent.NEW_TOPIC,
                entities=(),
                confidence=1.0,
                skipped=True,
            )

        failed = bool(rewrite.failed)
        new_topic = rewrite.intent is Intent.NEW_TOPIC and not rewrite.skipped
        isolate = failed or new_topic

        resolved = (
            (question if failed else rewrite.resolved_query) or question
        ).strip() or question

        if failed:
            # 改写失败按设计 §5.6 等价单轮：不继承历史、实体和会话 boost。
            merged_entities: list[str] = []
            penalty = 1.0
            doc_boost: dict[str, float] = {}
            history_for_answer: tuple[Turn, ...] = ()
            summary_for_answer = ""
            recent_ids = set()
        elif new_topic:
            # 新话题切断旧话题上下文，但保留摘要作为背景。
            merged_entities = list(dict.fromkeys(rewrite.entities))[:8]
            penalty = 1.0
            doc_boost = {}
            history_for_answer = ()
            summary_for_answer = summary
            recent_ids = set()
        else:
            merged_entities = list(dict.fromkeys(
                list(rewrite.entities) + list(entities)
            ))[:8]
            penalty = (
                1.0
                if rewrite.intent is Intent.FOLLOW_UP and CHAT_CONT_RE.search(question or "")
                else self._repeat_penalty
            )
            doc_boost = {path: 1.1 for path in recent_docs[:3]}
            history_for_answer = window
            summary_for_answer = summary

        if failed:
            flags["rewrite_failed"] = 1
        if isolate:
            flags["topic_isolated"] = 1

        options = AnswerOptions(
            rewrite=False,
            raw_question=question,
            entities=tuple(merged_entities),
            doc_boost=doc_boost,
            repeat_ids=frozenset(recent_ids),
            repeat_penalty=penalty,
            history=history_for_answer,
            summary=summary_for_answer,
            intent_hint=None if failed else rewrite.intent,
            multiturn=True,
        )
        search = self._pipeline.retrieve(resolved, options)

        if not search.hits and resolved != question:
            flags["fallback_raw"] = 1
            search = self._pipeline.retrieve(
                question,
                AnswerOptions(
                    rewrite=use_rewrite,
                    history=history_for_answer,
                    summary=summary_for_answer,
                ),
            )
        if not search.hits and history_for_answer:
            flags["fallback_concat"] = 1
            last = history_for_answer[-1].resolved_query or history_for_answer[-1].question
            search = self._pipeline.retrieve(
                f"{last} {question}",
                AnswerOptions(
                    rewrite=False,
                    history=history_for_answer,
                    summary=summary_for_answer,
                ),
            )

        builder = TraceBuilder()
        resolve_status = (
            StepStatus.ERROR if rewrite.failed
            else StepStatus.SKIP if rewrite.skipped
            else StepStatus.OK
        )
        builder.add(
            "resolve",
            resolve_status,
            summary=(
                f"{rewrite.intent.value} · 改写为「{resolved}」"
                if resolved != question
                else f"{rewrite.intent.value} · 无需改写"
            ),
            detail=(
                f"原始：{question}\n独立：{resolved}"
                if resolved != question else f"问题：{question}"
            ),
            ops=[
                f"读取历史 {len(window)} 轮" + (f"，摘要 {len(summary)} 字" if summary else ""),
                (
                    "门控判定：自足新问题，按新话题隔离"
                    if isolate and resolved == question
                    else f"门控判定：{'需要改写' if not rewrite.skipped else '无需改写'}"
                ),
                f"改写结果：「{resolved}」",
                f"意图 {rewrite.intent.value} · 置信度 {rewrite.confidence:.2f}"
                + (" · 改写失败已回退" if rewrite.failed else ""),
                f"生成侧历史 {len(history_for_answer)} 轮 · 话题隔离 {'开' if isolate else '关'}",
                f"改写 prompt rewrite@{default_registry().version('rewrite')}",
            ],
        )

        turn_id: int | None = None
        if rewrite.needs_clarification and len(search.hits) < 2:
            flags["clarified"] = 1
            text = rewrite.clarification_question or "你指的是哪一个？能再具体说明一下吗？"
            builder.extend(search.trace)
            builder.add(
                "clarify",
                StepStatus.OK,
                summary="指代存在多个候选，先向用户确认",
                detail=text,
                ops=["候选实体 ≥2 且检索证据不足 2 条", "不调用生成模型，直接返回澄清问句"],
            )
            answer = Answer(
                text=text,
                sources=(),
                evidence=(),
                citations=CitationCheck(ok=True),
                followups=(),
                trace=builder.trace,
                memory=MemoryStats(
                    turns_used=len(history_for_answer),
                    summary_used=bool(summary_for_answer),
                ),
            )
        else:
            if not search.hits:
                flags["no_evidence"] = 1
            generated = self._pipeline.generate(
                question,
                search,
                options,
                resolved_query=resolved,
                prefix_trace=search.trace,
            )
            answer = replace(generated, trace=builder.trace + generated.trace)

        citations = citations_from_answer(answer.text, answer.evidence)
        if not answer.citations.ok:
            flags["citation_invalid"] = 1 if answer.citations.invalid else flags.get("citation_invalid", 0)
            if answer.citations.missing:
                flags["citation_missing"] = 1

        latency_ms = int((time.time() - started) * 1000)
        if sid:
            turn = Turn(
                turn_id=0,
                session_id=sid,
                channel=channel,
                question=question,
                resolved_query=resolved,
                intent=rewrite.intent,
                answer=answer.text,
                citations=citations,
                evidence_ids=tuple(item.chunk.chunk_id for item in answer.evidence),
            )
            saved = self._sessions.append_turn(turn)
            turn_id = saved.turn_id
            if turn_id == 1 and not (sess.title if sess else ""):
                self._sessions.rename(sid, (question or "")[:80])

            upsert, delete = self._rewriter.update_entity_slots(
                entities or [], rewrite, turn_id
            )
            if delete:
                self._sessions.delete_entities(sid, delete)
            if upsert:
                self._sessions.upsert_entities(sid, upsert, turn_id)
            self._sessions.prune_entities(sid, 10)
            self._sessions.record_trace(sid, turn_id, {
                "channel": channel,
                "raw_query": question,
                "resolved_query": resolved,
                "intent": rewrite.intent.value,
                "confidence": rewrite.confidence,
                "needs_clarification": rewrite.needs_clarification,
                "rewrite_failed": rewrite.failed,
                "entities": merged_entities,
                "retrieved": [
                    {"chunk_id": hit.chunk.chunk_id, "doc_path": hit.chunk.doc_path,
                     "score": round(float(hit.score), 4)}
                    for hit in search.hits[:15]
                ],
                "prompt_tokens": answer.memory.prompt_tokens,
                "completion_tokens": estimate_tokens(answer.text),
                "latency_ms": latency_ms,
                "answer_hash": hashlib.md5((answer.text or "").encode()).hexdigest()[:12],
                "flags": flags,
            })
            print(
                f"[chat] sid={sid} turn={turn_id} intent={rewrite.intent.value} "
                f"rewrite={'fail' if rewrite.failed else 'ok'} "
                f"rewrite_model={getattr(self._rewrite_llm or llm, 'model', 'unknown')} "
                f"isolate={int(isolate)} resolved_len={len(resolved)} "
                f"prompt_tokens={answer.memory.prompt_tokens} ms={latency_ms}",
                flush=True,
            )
            self._summarizer.maybe_summarize(sid, llm)

        return ChatResult(
            question=question,
            answer=answer,
            session_id=sid,
            turn_id=turn_id,
            resolved_query=resolved,
            intent=rewrite.intent,
            confidence=rewrite.confidence,
            flags=flags,
            citations=citations,
            latency_ms=latency_ms,
        )

def _as_turn(item: object) -> Turn:
    if isinstance(item, Turn):
        return item
    if isinstance(item, Mapping):
        try:
            intent = Intent(str(item.get("intent") or "new_topic"))
        except ValueError:
            intent = Intent.NEW_TOPIC
        return Turn(
            turn_id=int(item.get("turn_id") or 0),
            session_id=str(item.get("session_id") or ""),
            question=str(item.get("question") or ""),
            resolved_query=str(item.get("resolved_query") or item.get("question") or ""),
            intent=intent,
            answer=str(item.get("answer") or ""),
            created_at=str(item.get("created_at") or ""),
        )
    return Turn(
        turn_id=0,
        question=str(getattr(item, "question", "")),
        resolved_query=str(getattr(item, "resolved_query", "")),
        intent=Intent.NEW_TOPIC,
        answer=str(getattr(item, "answer", "")),
    )
