"""P2：SQLite 与 Overrides 并发写测试。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from config.overrides import OverridesStore
from infra.sqlite_sessions import SqliteSessionRepository
from domain import Intent, Turn


def test_sqlite_concurrent_append_turn(tmp_path):
    repo = SqliteSessionRepository(tmp_path / "sessions.db")
    repo.migrate()
    repo.create(session_id="ses_concurrent", channel=8000)

    def write(index):
        repo.append_turn(Turn(
            turn_id=0,
            session_id="ses_concurrent",
            channel=8000,
            question=f"q{index}",
            resolved_query=f"q{index}",
            intent=Intent.NEW_TOPIC,
            answer=f"a{index}",
        ))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    session = repo.get("ses_concurrent")
    window = repo.window("ses_concurrent", 100)
    assert session.turn_count == 40
    assert len(window) == 40
    assert sorted(turn.turn_id for turn in window) == list(range(1, 41))


def test_overrides_concurrent_writes(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"params": {"TOP_K": 10}}), encoding="utf-8")
    store = OverridesStore(
        path,
        backup_dir=tmp_path / "backups",
        audit_path=tmp_path / "audit.jsonl",
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda value: store.apply_param("TOP_K", value), range(11, 19)))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["params"]["TOP_K"] in set(range(11, 19))
    assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 8
    assert not list(tmp_path.glob("overrides.json.tmp.*"))
