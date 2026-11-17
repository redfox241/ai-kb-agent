"""P0：评估 / 访问 / 归档存储测试。"""
from __future__ import annotations

import json

from analytics import repository as eval_store
from infra.sqlite_analytics import SqliteAnalyticsRepository
from analytics import archive as qa_store
from analytics import visit_repository as visit_store


def _evaluation(total, **scores):
    dims = {
        key: {"score": value}
        for key, value in (scores or {"accuracy": 8, "relevance": 8, "completeness": 8,
                                      "clarity": 8, "sources": 8}).items()
    }
    return {"total": total, "dimensions": dims, "reason": "r",
            "strengths": ["s"], "weaknesses": ["w"], "suggestion": "x"}


def test_eval_store_roundtrip(tmp_path, monkeypatch):
    eval_store.configure(SqliteAnalyticsRepository(tmp_path / "eval.db"))
    eval_store.init_db()
    eval_store.record_eval("问题甲", "答案", _evaluation(8), port=8000, latency_ms=10)
    eval_store.record_eval("问题乙", "答案", _evaluation(5), port=8001, latency_ms=20)

    stats = eval_store.query_stats()
    assert stats["total"] == 2
    assert stats["grades"] == {"good": 1, "mid": 0, "bad": 1}

    high = eval_store.query_history(min_total=6)
    assert [row["question"] for row in high] == ["问题甲"]
    assert eval_store.query_history(keyword="乙")[0]["question"] == "问题乙"
    assert eval_store.query_history(offset=1)[0]["question"] == "问题甲"
    assert eval_store.query_low(6)[0]["question"] == "问题乙"


def test_visit_store_roundtrip(tmp_path, monkeypatch):
    visit_store.init_db()
    visit_store.record_visit(kind="query", question="问题", endpoint="ask",
                             n_sources=2, n_steps=3, latency_ms=10, port=8000)
    visit_store.record_visit(kind="page", path="/chat", endpoint="chat", port=8000)

    assert [row["kind"] for row in visit_store.query_visits(kind="page")] == ["page"]
    assert visit_store.query_visits(keyword="chat")[0]["path"] == "/chat"
    stats = visit_store.query_visit_stats()
    assert stats["total"] == 2
    assert stats["by_kind"]["query"] == 1
    assert stats["by_kind"]["page"] == 1


def test_qa_store_roundtrip(tmp_path, monkeypatch):
    archive = tmp_path / "qa"
    monkeypatch.setattr(qa_store, "QA_ARCHIVE_DIR", str(archive))
    monkeypatch.setattr(qa_store, "STATS_FILE", archive / "stats" / "qa_stats.jsonl")
    monkeypatch.setattr(qa_store, "SCORES_FILE", archive / "stats" / "scores.json")
    monkeypatch.setattr(qa_store, "LABELS_FILE", archive / "stats" / "labels.json")
    (archive / "stats").mkdir(parents=True)

    path = qa_store.save_qa_record("问题", "答案", sources=[{"title": "A", "path": "a.md"}],
                                   port=8000)
    assert path and (archive / "direct") in __import__("pathlib").Path(path).parents
    assert json.loads(qa_store.STATS_FILE.read_text(encoding="utf-8").splitlines()[0])["question"] == "问题"

    qa_store.record_score("问题", "答案", 5, reason="低分", port=8000)
    qa_store.record_score("另一个", "答案", 8, reason="高分", port=8001)
    assert qa_store.query_scores(min_score=6)[0]["question"] == "另一个"
    assert qa_store.query_scores(max_score=6)[0]["question"] == "问题"
    assert qa_store.query_scores(port=8001)[0]["question"] == "另一个"
    labels = json.loads(qa_store.LABELS_FILE.read_text(encoding="utf-8"))
    assert any(item["label"] == "low" for item in labels.values())
