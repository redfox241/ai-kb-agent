"""P1：eval_logger 评估落库与低分入队。"""
from __future__ import annotations

from analytics import evaluation as eval_logger


def test_evaluate_and_record(monkeypatch):
    captured = {}
    monkeypatch.setattr(eval_logger, "record_eval",
                        lambda *a, **k: captured.update({"args": a, "kwargs": k}))
    monkeypatch.setattr(
        "optimization.utils.scorer.score_answer_multi",
        lambda question, answer, llm=None: {"total": 8.0},
    )

    result = eval_logger.evaluate_and_record("q", "a", port=8000, latency_ms=12)
    assert result == {"total": 8.0}
    assert captured["args"] == ("q", "a", {"total": 8.0})
    assert captured["kwargs"] == {"port": 8000, "latency_ms": 12}


def test_log_eval_async_uses_thread(monkeypatch):
    captured = {}

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            captured["args"] = self.args
            self.target(*self.args)

    monkeypatch.setattr(eval_logger, "_jobs", None)
    monkeypatch.setattr(eval_logger.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(eval_logger, "_work", lambda *args: captured.update({"work": args}))

    eval_logger.log_eval_async("q", "a", port=8000, latency_ms=5)
    assert captured["work"] == ("q", "a", 8000, 5)


def test_maybe_enqueue_low(monkeypatch):
    captured = {}
    monkeypatch.setattr(eval_logger, "_load_trigger_config",
                        lambda: {"trigger": {"immediate_threshold": 6}})
    monkeypatch.setattr(
        "optimization.agent.trigger.enqueue",
        lambda incident, cfg: captured.update({"incident": incident}) or ("enqueued", {}),
    )

    eval_logger._maybe_enqueue_low("q", "a", {"total": 5, "reason": "r"})
    assert captured["incident"]["question"] == "q"
    assert captured["incident"]["score"] == 5

    captured.clear()
    eval_logger._maybe_enqueue_low("q", "a", {"total": 8})
    assert captured == {}
