"""PR-02：领域模型、端口与错误的契约测试。"""
from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from domain import (
    Answer,
    AnswerMode,
    AnswerOptions,
    BudgetExceeded,
    Chunk,
    ChunkKind,
    CitationCheck,
    DomainError,
    IndexNotReady,
    InvalidSession,
    LLM,
    ModelUnavailable,
    SearchIndex,
    SessionNotFound,
    SessionRepository,
    Source,
    StepStatus,
    ToolPermissionDenied,
    TraceEvent,
)

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "domain"


def test_enum_values_are_stable():
    assert ChunkKind.SUMMARY.value == "summary"
    assert ChunkKind.DETAIL.value == "detail"
    assert AnswerMode.WORKFLOW.value == "workflow"
    assert AnswerMode.ASSISTED.value == "assisted"
    assert AnswerMode.AGENT.value == "agent"
    assert StepStatus.DEGRADED.value == "degraded"


def test_chunk_is_frozen_and_uses_immutable_collections():
    chunk = Chunk(
        chunk_id="wiki/a.md#summary",
        doc_path="wiki/a.md",
        title="A",
        kind=ChunkKind.SUMMARY,
        text="正文",
        tags=("tag",),
        metadata={"year": "2026"},
    )
    assert chunk.kind is ChunkKind.SUMMARY
    assert chunk.tags == ("tag",)
    assert chunk.metadata["year"] == "2026"

    with pytest.raises(FrozenInstanceError):
        chunk.text = "changed"  # type: ignore[misc]


def test_answer_options_defaults_are_isolated():
    first = AnswerOptions()
    second = AnswerOptions()

    assert first.rewrite is True
    assert first.mode is AnswerMode.WORKFLOW
    assert first.repeat_ids == frozenset()
    assert first.doc_boost == {}
    assert first.doc_boost is not second.doc_boost
    assert first.history == ()


def test_answer_and_citation_contract():
    answer = Answer(
        text="答案。[E1]",
        sources=(Source(title="A", path="wiki/a.md", kind="summary"),),
        evidence=(),
        citations=CitationCheck(ok=True),
    )
    assert answer.citations.ok is True
    assert answer.followups == ()
    assert answer.memory.turns_used == 0


def test_trace_event_contract():
    event = TraceEvent(
        trace_id="tr_1",
        step="bm25",
        status=StepStatus.OK,
        started_at=1.0,
        duration_ms=8,
        output_summary="命中 2 条",
        attributes={"top_k": 10},
    )
    assert event.status is StepStatus.OK
    assert event.attributes["top_k"] == 10


def test_domain_errors_carry_stable_mapping():
    cases = [
        (IndexNotReady("x", detail={"status": "building"}), "index_not_ready", 503, True),
        (ModelUnavailable("x"), "model_unavailable", 502, True),
        (InvalidSession("x"), "invalid_session", 400, False),
        (SessionNotFound("x"), "session_not_found", 404, False),
        (BudgetExceeded("x"), "budget_exceeded", 429, False),
        (ToolPermissionDenied("x"), "tool_permission_denied", 403, False),
    ]
    for error, code, status, retryable in cases:
        assert isinstance(error, DomainError)
        assert error.code == code
        assert error.http_status == status
        assert error.retryable is retryable

    assert IndexNotReady("x", detail={"status": "building"}).detail["status"] == "building"


def test_protocols_expose_the_expected_surface():
    assert "chat" in LLM.__dict__
    assert "keyword" in SearchIndex.__dict__
    assert "append_turn" in SessionRepository.__dict__
    assert "window" in SessionRepository.__dict__
    assert "recent_doc_paths" in SessionRepository.__dict__


def test_domain_layer_has_no_framework_or_infra_imports():
    forbidden_roots = {
        "fastapi", "starlette", "httpx", "sqlite3", "numpy", "yaml",
        "pydantic", "server", "common", "eval_store",
        "visit_store", "ui", "ui_chat", "ui_eval", "ui_analysis",
    }

    for path in DOMAIN_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]

            for module in modules:
                root = module.split(".", 1)[0]
                assert root not in forbidden_roots, f"{path.name} imports {module}"
