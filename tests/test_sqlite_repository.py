"""PR-10：原生 SqliteSessionRepository 契约测试。"""
from __future__ import annotations

from infra.sqlite_sessions import SqliteSessionRepository
from domain import Citation, Intent, Turn


def _repo(tmp_path):
    repo = SqliteSessionRepository(tmp_path / "sessions.db")
    repo.migrate()
    repo.migrate()  # 幂等
    return repo


def test_session_and_turn_roundtrip(tmp_path):
    repo = _repo(tmp_path)
    state = repo.get_or_create("ses_repo_1", channel=8000)
    assert state.session_id == "ses_repo_1"
    assert state.channel == 8000

    saved = repo.append_turn(Turn(
        turn_id=0,
        session_id="ses_repo_1",
        channel=8000,
        question="孙悟空是谁？",
        resolved_query="孙悟空是谁？",
        intent=Intent.NEW_TOPIC,
        answer="须菩提祖师。[E1]",
        citations=(Citation(id="E1", title="孙悟空",
                            doc_path="wiki/sources/孙悟空.md",
                            chunk_id="wiki/sources/孙悟空.md#summary"),),
        evidence_ids=("wiki/sources/孙悟空.md#summary",),
    ))
    assert saved.turn_id == 1

    window = repo.window("ses_repo_1", 5)
    assert len(window) == 1
    assert window[0].question == "孙悟空是谁？"
    assert window[0].resolved_query == "孙悟空是谁？"
    assert window[0].intent is Intent.NEW_TOPIC

    assert repo.recent_doc_paths("ses_repo_1", 2) == ["wiki/sources/孙悟空.md"]
    assert repo.recent_chunk_ids("ses_repo_1", 2) == {"wiki/sources/孙悟空.md#summary"}
    assert repo.get("ses_repo_1").turn_count == 1


def test_list_rename_reset_delete(tmp_path):
    repo = _repo(tmp_path)
    repo.get_or_create("ses_repo_2", channel=8000)
    assert [item.session_id for item in repo.list_sessions(channel=8000)] == ["ses_repo_2"]

    assert repo.rename("ses_repo_2", "新标题") is True
    assert repo.get("ses_repo_2").title == "新标题"

    repo.reset("ses_repo_2")
    assert repo.window("ses_repo_2", 5) == []
    assert repo.get("ses_repo_2").turn_count == 0

    repo.delete("ses_repo_2")
    assert repo.get("ses_repo_2") is None
    assert repo.list_sessions(channel=8000) == []


def test_entities_and_traces(tmp_path):
    repo = _repo(tmp_path)
    repo.get_or_create("ses_repo_3", channel=8000)
    repo.append_turn(Turn(
        turn_id=0, session_id="ses_repo_3", channel=8000,
        question="q", resolved_query="q", intent=Intent.FOLLOW_UP, answer="a",
    ))
    repo.upsert_entities("ses_repo_3", ["孙悟空", "猪八戒"], 1)
    assert repo.entities("ses_repo_3") == ["孙悟空", "猪八戒"]
    repo.delete_entities("ses_repo_3", ["猪八戒"])
    assert repo.entities("ses_repo_3") == ["孙悟空"]

    repo.record_trace("ses_repo_3", 1, {
        "channel": 8000,
        "raw_query": "q",
        "resolved_query": "q",
        "intent": "follow_up",
        "confidence": 0.9,
        "needs_clarification": False,
        "rewrite_failed": False,
        "entities": ["孙悟空"],
        "retrieved": [{"chunk_id": "c1", "doc_path": "wiki/a.md", "score": 1.0}],
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "latency_ms": 12,
        "answer_hash": "abc",
        "flags": {"x": 1},
    })
    traces = repo.traces("ses_repo_3")
    assert len(traces) == 1
    assert traces[0]["entities"] == ["孙悟空"]
    assert traces[0]["flags"] == {"x": 1}


def test_export_and_stats(tmp_path):
    repo = _repo(tmp_path)
    repo.get_or_create("ses_repo_4", channel=8000)
    repo.append_turn(Turn(
        turn_id=0, session_id="ses_repo_4", channel=8000,
        question="问题", resolved_query="问题", intent=Intent.NEW_TOPIC,
        answer="答案",
    ))
    markdown = repo.export_markdown("ses_repo_4")
    assert "问题" in markdown and "答案" in markdown

    stats = repo.stats(channel=8000)
    assert stats["sessions"] == 1
    assert stats["total_turns"] == 1


def test_generated_session_id_is_valid(tmp_path):
    repo = _repo(tmp_path)
    state = repo.create(channel=8000)
    assert state.session_id.startswith("ses_")
    assert repo.valid_session_id(state.session_id)
