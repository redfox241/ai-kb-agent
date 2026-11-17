"""P0：mcp_tools 工具层测试（mock HTTP / LLM）。"""
from __future__ import annotations

import json

from optimization.agent import mcp_tools


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def test_test_rag_service(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse({"answer": "答案", "sources": [{"title": "A"}]})

    monkeypatch.setattr(mcp_tools.httpx, "post", fake_post)
    result = mcp_tools.test_rag_service("问题", port=8001)

    assert result["answer"] == "答案"
    assert result["port"] == 8001
    assert captured["url"] == "http://127.0.0.1:8001/ask_smart"
    assert captured["json"]["stateless"] is True


def test_verify_single_question(monkeypatch):
    monkeypatch.setattr(mcp_tools, "test_rag_service", lambda *a, **k: {"answer": "答案"})
    scores = iter([
        {"score": 5, "reason": "r1", "strengths": ["s"], "weaknesses": ["w1"]},
        {"score": 7, "reason": "r2", "strengths": ["s"], "weaknesses": ["w2"]},
    ])
    monkeypatch.setattr(mcp_tools, "score_answer", lambda *a, **k: next(scores))

    result = mcp_tools.verify_single_question("问题", times=2)
    assert result["score"] == 6.0
    assert result["scores_raw"] == [5, 7]
    assert result["passed"] is True
    assert result["reason"] == "r1; r2"
    assert result["weaknesses"] == ["w1", "w2"]


def test_run_benchmark_with_cache(monkeypatch):
    items = [
        {"question": "q1", "tier": "basic", "baseline": 7},
        {"question": "q2", "tier": "basic", "baseline": 8},
    ]
    cache = {"8000:q1": {"answer": "cached", "score": 8}}
    saved = {}

    monkeypatch.setattr(mcp_tools, "_load_benchmark_set", lambda: items)
    monkeypatch.setattr(mcp_tools, "_load_baseline_cache", lambda: dict(cache))
    monkeypatch.setattr(mcp_tools, "_save_baseline_cache", lambda c: saved.update(c))
    monkeypatch.setattr(mcp_tools, "test_rag_service", lambda *a, **k: {"answer": "live"})
    monkeypatch.setattr(mcp_tools, "score_answer", lambda *a, **k: {"score": 5})

    result = mcp_tools.run_benchmark(port=8000)
    assert result["total"] == 2
    assert result["avg"] == 6.5
    assert result["regressed"][0]["question"] == "q2"
    assert saved["8000:q2"]["score"] == 5


def test_get_optimization_history(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    path.write_text("\n".join(json.dumps({"i": i}) for i in range(3)), encoding="utf-8")
    monkeypatch.setattr(mcp_tools, "HISTORY_FILE", str(path))

    assert [row["i"] for row in mcp_tools.get_optimization_history(2)] == [1, 2]


def test_query_qa_archive(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        mcp_tools, "query_scores",
        lambda **kwargs: captured.update(kwargs) or [{"score": 8}],
    )
    result = mcp_tools.query_qa_archive(min_score=6, max_score=9, limit=10)
    assert result == [{"score": 8}]
    assert captured == {"min_score": 6, "max_score": 9, "limit": 10, "port": None}
