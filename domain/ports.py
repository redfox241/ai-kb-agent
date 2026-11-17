"""领域端口：外部能力的抽象，由 infra 实现。

核心层只依赖这些 Protocol，不依赖 FastAPI、httpx、sqlite3 或具体 SDK。
"""
from __future__ import annotations

from typing import Callable, Mapping, Protocol, Sequence, TypeVar

from .models import (
    Answer,
    AnswerOptions,
    Chunk,
    EvalResult,
    JobHandle,
    Message,
    QueryEvent,
    ScoredChunk,
    SessionState,
    SessionSummary,
    Turn,
)


T = TypeVar("T")


class LLM(Protocol):
    model: str

    def chat(
        self,
        messages: Sequence[Message],
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str: ...


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]: ...


class SearchIndex(Protocol):
    def keyword(self, query: str, k: int) -> list[ScoredChunk]: ...

    def semantic(
        self,
        vector: Sequence[float],
        k: int,
        threshold: float,
    ) -> list[ScoredChunk]: ...

    def expand_parents(
        self,
        hits: Sequence[ScoredChunk],
        limit: int,
    ) -> list[Chunk]: ...


class SessionRepository(Protocol):
    def migrate(self) -> None: ...

    def get(self, session_id: str) -> SessionState | None: ...

    def get_or_create(self, session_id: str, channel: int) -> SessionState: ...

    def list_sessions(
        self,
        limit: int = 20,
        offset: int = 0,
        channel: int | None = None,
    ) -> list[SessionSummary]: ...

    def rename(self, session_id: str, title: str) -> bool: ...

    def reset(self, session_id: str) -> None: ...

    def delete(self, session_id: str) -> None: ...

    def append_turn(self, turn: Turn) -> Turn: ...

    def window(self, session_id: str, turns: int) -> list[Turn]: ...

    def update_summary(self, session_id: str, summary: str, upto_turn: int,
                       summary_tokens: int | None = None) -> None: ...

    def recent_doc_paths(self, session_id: str, turns: int) -> list[str]: ...

    def recent_chunk_ids(self, session_id: str, turns: int) -> set[str]: ...

    def entities(self, session_id: str, limit: int = 8) -> list[str]: ...

    def upsert_entities(
        self,
        session_id: str,
        entities: Sequence[str],
        turn_id: int,
        doc_paths: Mapping[str, str] | None = None,
    ) -> None: ...

    def delete_entities(self, session_id: str, entities: Sequence[str]) -> None: ...

    def prune_entities(self, session_id: str, keep_turns: int = 10) -> None: ...

    def record_trace(
        self,
        session_id: str,
        turn_id: int,
        payload: Mapping[str, object],
    ) -> None: ...

    def traces(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[Mapping[str, object]]: ...

    def export_markdown(self, session_id: str) -> str: ...

    def purge(
        self,
        days_messages: int = 180,
        days_traces: int = 30,
        days_sessions: int = 90,
    ) -> int: ...

    def stats(
        self,
        channel: int | None = None,
        days: int = 30,
    ) -> Mapping[str, object]: ...


class AnalyticsRepository(Protocol):
    def migrate(self) -> None: ...

    def record_query(self, event: QueryEvent) -> None: ...

    def record_eval(self, result: EvalResult) -> None: ...

    def eval_stats(self) -> Mapping[str, object]: ...

    def eval_history(
        self,
        limit: int = 50,
        offset: int = 0,
        min_total: float | None = None,
        max_total: float | None = None,
        keyword: str | None = None,
    ) -> list[Mapping[str, object]]: ...

    def visits(
        self,
        limit: int = 50,
        offset: int = 0,
        kind: str | None = None,
        keyword: str | None = None,
    ) -> list[Mapping[str, object]]: ...

    def visit_stats(self) -> Mapping[str, object]: ...


class Overrides(Protocol):
    def param(self, name: str, default: T) -> T: ...

    def prompt_rule(self) -> str: ...


class IndexStore(Protocol):
    """索引文件读写端口：由 infra 实现，ingestion 只依赖此协议。"""

    def read(self, path: object) -> object: ...

    def write(
        self,
        path: object,
        *,
        chunks: Sequence[object],
        embeddings: object,
        summary_by_doc: Mapping[str, object],
        meta: Mapping[str, object],
    ) -> None: ...


class JobRunner(Protocol):
    def start(self) -> None: ...

    def submit(self, name: str, fn: Callable[[], None]) -> JobHandle: ...

    def shutdown(self, wait: bool = True) -> None: ...


class Clock(Protocol):
    def now_iso(self) -> str: ...

    def monotonic(self) -> float: ...


class Assistant(Protocol):
    """HTTP 层唯一需要知道的用例接口。"""

    def answer(self, question: str, options: AnswerOptions) -> Answer: ...
