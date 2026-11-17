"""领域模型：纯数据，不依赖框架、网络与存储。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Mapping, Sequence


Message = Mapping[str, str]


class ChunkKind(str, Enum):
    SUMMARY = "summary"
    DETAIL = "detail"


class Intent(str, Enum):
    NEW_TOPIC = "new_topic"
    FOLLOW_UP = "follow_up"
    CORRECTION = "correction"
    COMPARISON = "comparison"
    CLARIFICATION = "clarification"
    META = "meta"
    CHITCHAT = "chitchat"


class StepStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    SKIP = "skip"
    ERROR = "error"
    DEGRADED = "degraded"


class AnswerMode(str, Enum):
    WORKFLOW = "workflow"
    ASSISTED = "assisted"
    AGENT = "agent"


class IndexStatus(str, Enum):
    EMPTY = "empty"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_path: str
    title: str
    kind: ChunkKind
    text: str
    tags: tuple[str, ...] = ()
    created: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: float
    source: Literal["bm25", "semantic", "rewrite", "fused"] = "fused"


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    chunk: Chunk
    score: float
    rank: int


@dataclass(frozen=True)
class Source:
    title: str
    path: str
    kind: str


@dataclass(frozen=True)
class Citation:
    id: str
    title: str = ""
    doc_path: str = ""
    chunk_id: str = ""


@dataclass(frozen=True)
class CitationCheck:
    ok: bool
    invalid: tuple[str, ...] = ()
    missing: bool = False


@dataclass(frozen=True)
class MemoryStats:
    turns_used: int = 0
    summary_used: bool = False
    prompt_tokens: int | None = None
    truncated: bool = False


@dataclass(frozen=True)
class Turn:
    turn_id: int
    question: str
    resolved_query: str
    intent: Intent
    answer: str
    citations: tuple[Citation, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    created_at: str = ""
    session_id: str = ""
    channel: int = 0


@dataclass(frozen=True)
class SessionState:
    session_id: str
    channel: int
    title: str
    summary: str
    summary_upto_turn: int
    turn_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    channel: int
    title: str
    turn_count: int
    updated_at: str
    created_at: str


@dataclass(frozen=True)
class RewriteResult:
    resolved_query: str
    intent: Intent = Intent.NEW_TOPIC
    entities: tuple[str, ...] = ()
    filters: Mapping[str, str] = field(default_factory=dict)
    needs_clarification: bool = False
    clarification_question: str = ""
    confidence: float = 1.0
    failed: bool = False
    skipped: bool = True


@dataclass(frozen=True)
class AnswerOptions:
    rewrite: bool = True
    raw_question: str | None = None
    entities: tuple[str, ...] = ()
    doc_boost: Mapping[str, float] = field(default_factory=dict)
    repeat_ids: frozenset[str] = frozenset()
    repeat_penalty: float = 1.0
    history: tuple[Turn, ...] = ()
    summary: str = ""
    disco: bool = False
    mode: AnswerMode = AnswerMode.WORKFLOW
    top_k: int | None = None
    intent_hint: Intent | None = None
    multiturn: bool = False


@dataclass(frozen=True)
class TraceEvent:
    trace_id: str
    step: str
    status: StepStatus
    started_at: float
    duration_ms: int
    input_summary: str = ""
    output_summary: str = ""
    attributes: Mapping[str, object] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class Answer:
    text: str
    sources: tuple[Source, ...]
    evidence: tuple[Evidence, ...]
    citations: CitationCheck
    followups: tuple[str, ...] = ()
    trace: tuple[TraceEvent, ...] = ()
    memory: MemoryStats = field(default_factory=MemoryStats)


@dataclass(frozen=True)
class BuiltContext:
    system: str
    messages: tuple[Message, ...]
    prompt_tokens: int
    truncated: bool
    evidence_ids: tuple[str, ...]
    evidence_text: str


@dataclass(frozen=True)
class QueryEvent:
    question: str
    endpoint: str
    port: int | None = None
    n_sources: int = 0
    n_steps: int = 0
    latency_ms: int | None = None
    status: str = "ok"


@dataclass(frozen=True)
class EvalDimension:
    key: str
    name: str
    score: float
    weight: float
    comment: str = ""


@dataclass(frozen=True)
class EvalResult:
    question: str
    answer: str
    total: float
    dimensions: Mapping[str, EvalDimension] = field(default_factory=dict)
    reason: str = ""
    strengths: tuple[str, ...] = ()
    weaknesses: tuple[str, ...] = ()
    suggestion: str = ""
    port: int | None = None
    latency_ms: int | None = None


@dataclass(frozen=True)
class JobHandle:
    id: str
    name: str


@dataclass(frozen=True)
class ReindexProgress:
    status: str = "idle"
    total: int = 0
    done: int = 0
    current_file: str = ""
    level: str = ""
    failed: int = 0
    failed_chunks: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchResult:
    """检索用例的返回值：候选、降级状态、阶段计数与追踪。"""

    hits: tuple[ScoredChunk, ...]
    degraded: bool = False
    reason: str = ""
    bm25_hits: tuple[ScoredChunk, ...] = ()
    semantic_hits: tuple[ScoredChunk, ...] = ()
    rewrite_hits: tuple[ScoredChunk, ...] = ()
    bm25_count: int = 0
    semantic_count: int = 0
    rewrite_count: int = 0
    fused_count: int = 0
    trace: tuple[TraceEvent, ...] = ()
