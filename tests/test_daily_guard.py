"""P0：daily_benchmark_guard 分支测试。"""
from __future__ import annotations

import json
import sys

from optimization.agent import daily_benchmark_guard as guard


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "LOGS", str(tmp_path / "logs"))
    monkeypatch.setattr(guard, "AGENT_LOGS", str(tmp_path / "logs" / "agent"))
    monkeypatch.setattr(guard, "GUARD_LOG", str(tmp_path / "logs" / "agent" / "guard.log"))
    monkeypatch.setattr(guard, "BENCH", str(tmp_path / "benchmark"))
    (tmp_path / "benchmark").mkdir()
    (tmp_path / "logs" / "agent").mkdir(parents=True)


def _items():
    return [{"question": "q1", "tier": "basic", "baseline": 8}]


def test_load_and_save_set(tmp_path, monkeypatch):
    path = tmp_path / "set.json"
    monkeypatch.setattr(guard, "SET_FILE", str(path))
    path.write_text(json.dumps({"questions": _items()}), encoding="utf-8")
    assert guard.load_set()[0]["question"] == "q1"

    guard.save_set(_items())
    assert json.loads(path.read_text(encoding="utf-8"))["questions"][0]["baseline"] == 8


def test_rollback_to_first_stable(tmp_path, monkeypatch):
    root = tmp_path
    backups = root / "backups"
    backups.mkdir()
    (backups / "overrides.prod.before_sync.1.json").write_text('{"x": 1}', encoding="utf-8")
    prod = root / "overrides.prod.json"
    prod.write_text('{"x": 0}', encoding="utf-8")
    monkeypatch.setattr(guard, "ROOT", str(root))
    monkeypatch.setattr(guard.ov, "PROD_OVERRIDES_FILE", str(prod))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: None)

    assert guard.rollback_to_first_stable() is True
    assert json.loads(prod.read_text(encoding="utf-8")) == {"x": 1}


def test_main_check_mode_does_not_rollback_or_save(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    calls = {"rollback": 0, "save": 0}
    monkeypatch.setattr(sys, "argv", ["guard", "--check"])
    monkeypatch.setattr(guard, "load_set", lambda: _items())
    monkeypatch.setattr(guard.mcp_tools, "test_rag_service", lambda *a, **k: {"answer": "a"})
    monkeypatch.setattr(guard, "score_answer", lambda *a, **k: {"score": 3})
    monkeypatch.setattr(guard, "rollback_to_first_stable",
                        lambda: calls.__setitem__("rollback", calls["rollback"] + 1))
    monkeypatch.setattr(guard, "save_set", lambda items: calls.__setitem__("save", calls["save"] + 1))

    guard.main()
    assert calls == {"rollback": 0, "save": 0}


def test_main_regression_rolls_back(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    calls = {"rollback": 0}
    monkeypatch.setattr(sys, "argv", ["guard"])
    monkeypatch.setattr(guard, "load_set", lambda: _items())
    monkeypatch.setattr(guard.mcp_tools, "test_rag_service", lambda *a, **k: {"answer": "a"})
    monkeypatch.setattr(guard, "score_answer", lambda *a, **k: {"score": 3})
    monkeypatch.setattr(guard, "rollback_to_first_stable",
                        lambda: calls.__setitem__("rollback", calls["rollback"] + 1) or True)

    guard.main()
    assert calls["rollback"] == 1
    pending = json.loads((tmp_path / "logs" / "pending_revalidation.json").read_text(encoding="utf-8"))
    assert pending["questions"] == ["q1"]


def test_main_stable_refreshes_baseline(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    items = _items()
    saved = {}
    monkeypatch.setattr(sys, "argv", ["guard"])
    monkeypatch.setattr(guard, "load_set", lambda: items)
    monkeypatch.setattr(guard.mcp_tools, "test_rag_service", lambda *a, **k: {"answer": "a"})
    monkeypatch.setattr(guard, "score_answer", lambda *a, **k: {"score": 8})
    monkeypatch.setattr(guard, "rollback_to_first_stable", lambda: False)
    monkeypatch.setattr(guard, "save_set", lambda value: saved.update({"items": value}))

    guard.main()
    assert saved["items"][0]["baseline"] == 8
    cache = json.loads((tmp_path / "benchmark" / "baseline_cache.json").read_text(encoding="utf-8"))
    assert cache["8000:q1"]["score"] == 8
