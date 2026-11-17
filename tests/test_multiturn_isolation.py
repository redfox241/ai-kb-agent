"""多轮话题隔离与改写失败回退的回归测试。"""
from __future__ import annotations

import os
import sqlite3

from domain import (
    Answer,
    CitationCheck,
    Intent,
    JobHandle,
    RewriteResult,
    SearchResult,
    Turn,
)
from infra.llm import LLMClient
from infra.sqlite_sessions import SqliteSessionRepository
from qa.multiturn import ChatService


class StubRewriter:
    def __init__(self, result: RewriteResult):
        self.result = result

    def resolve(self, question, window, summary, entities, llm,
                mode="auto", intent_hint=None):
        return self.result

    def update_entity_slots(self, prev_entities, result, turn_id):
        return list(result.entities), []


class RecordingPipeline:
    def __init__(self, hits=()):
        self.hits = tuple(hits)
        self.calls: list[tuple[str, object]] = []

    def retrieve(self, question, options=None):
        self.calls.append((question, options))
        return SearchResult(hits=self.hits)

    def generate(self, question, search, options=None, *,
                 resolved_query=None, prefix_trace=()):
        return Answer(
            text="答案[E1]",
            sources=(),
            evidence=(),
            citations=CitationCheck(ok=True),
        )


class SyncJobs:
    def __init__(self):
        self.names = []

    def start(self):
        return None

    def submit(self, name, fn):
        self.names.append(name)
        fn()
        return JobHandle(id=str(len(self.names)), name=name)

    def shutdown(self, wait=True):
        return None


class FlakySummaryLLM:
    """第一次摘要返回空，第二次返回有效摘要，用于验证重试。"""

    model = "flaky"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, *, system=None, max_tokens=None,
             temperature=None, thinking=None):
        self.calls += 1
        if self.calls == 1:
            return ""
        return "会话摘要：用户询问长征的起止时间与主要事件。"


def _repo() -> SqliteSessionRepository:
    repo = SqliteSessionRepository(os.environ["RAG_SESSION_DB"])
    repo.migrate()
    return repo


def _seed_history(repo: SqliteSessionRepository, session_id: str) -> None:
    repo.create(channel=8000, session_id=session_id)
    repo.append_turn(Turn(
        turn_id=0,
        session_id=session_id,
        channel=8000,
        question="抗日战争开始时间和结束时间",
        resolved_query="抗日战争开始时间和结束时间",
        intent=Intent.NEW_TOPIC,
        answer="1937 年全面爆发，1945 年结束。[E1]",
    ))
    repo.upsert_entities(session_id, ["抗日战争"], 1)


def test_failed_rewrite_uses_raw_question_and_isolates(temp_session_db):
    repo = _repo()
    _seed_history(repo, "ses_iso_failed")
    pipeline = RecordingPipeline()
    rewriter = StubRewriter(RewriteResult(
        resolved_query="抗日战争开始时间和结束时间 主要领导人",
        intent=Intent.FOLLOW_UP,
        entities=("抗日战争",),
        confidence=0.4,
        failed=True,
        skipped=False,
    ))
    service = ChatService(pipeline, repo, rewriter)

    result = service.chat("主要领导人", None, session_id="ses_iso_failed", channel=8000)

    assert result.resolved_query == "主要领导人"
    assert result.flags.get("rewrite_failed") == 1
    assert result.flags.get("topic_isolated") == 1

    question, options = pipeline.calls[0]
    assert question == "主要领导人"
    assert options.history == ()
    assert options.entities == ()
    assert options.doc_boost == {}
    assert options.repeat_ids == frozenset()


def test_new_topic_isolates_history_and_entities(temp_session_db):
    repo = _repo()
    _seed_history(repo, "ses_iso_new_topic")
    pipeline = RecordingPipeline()
    rewriter = StubRewriter(RewriteResult(
        resolved_query="解放战争著名的战役",
        intent=Intent.NEW_TOPIC,
        entities=("解放战争",),
        confidence=0.95,
        failed=False,
        skipped=False,
    ))
    service = ChatService(pipeline, repo, rewriter)

    result = service.chat(
        "解放战争著名的战役", None,
        session_id="ses_iso_new_topic", channel=8000,
    )

    assert result.resolved_query == "解放战争著名的战役"
    assert result.flags.get("topic_isolated") == 1

    question, options = pipeline.calls[0]
    assert question == "解放战争著名的战役"
    assert options.history == ()
    assert options.entities == ("解放战争",)
    assert options.doc_boost == {}
    assert options.repeat_ids == frozenset()


def test_summary_retries_on_empty_output_and_records_tokens(temp_session_db):
    repo = _repo()
    pipeline = RecordingPipeline()
    jobs = SyncJobs()
    service = ChatService(
        pipeline, repo,
        StubRewriter(RewriteResult(
            resolved_query="长征",
            intent=Intent.NEW_TOPIC,
            skipped=True,
        )),
        jobs=jobs,
        summary_every=1,
    )
    llm = FlakySummaryLLM()

    service.chat("长征", llm, session_id="ses_summary_retry", channel=8000)

    session = repo.get("ses_summary_retry")
    assert llm.calls == 2
    assert session is not None
    assert session.summary
    assert session.summary_upto_turn == 1
    with sqlite3.connect(repo.db_path) as conn:
        tokens = conn.execute(
            "SELECT summary_tokens FROM sessions WHERE session_id=?",
            ("ses_summary_retry",),
        ).fetchone()[0]
    assert tokens > 0


def test_chat_json_accepts_temperature_and_thinking(monkeypatch):
    client = LLMClient(
        base_url="http://127.0.0.1:1",
        api_key="test-key",
        model="test-model",
        style="anthropic",
    )
    seen: dict[str, object] = {}

    def fake_chat(messages, system=None, max_tokens=None,
                  temperature=None, thinking=None):
        seen.update({
            "system": system,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "thinking": thinking,
        })
        return '{"resolved_query": "独立问题", "intent": "follow_up"}'

    monkeypatch.setattr(client, "chat", fake_chat)

    data = client.chat_json(
        "prompt",
        system="system",
        max_tokens=2000,
        temperature=0.1,
        thinking={"type": "disabled"},
    )

    assert data == {"resolved_query": "独立问题", "intent": "follow_up"}
    assert seen["temperature"] == 0.1
    assert seen["thinking"] == {"type": "disabled"}
