"""评估用例：后台评估查询质量、落库，并在低分时入队自优化。

依赖通过组合根注入：LLM 工厂、JobRunner、评分器回调。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from analytics.repository import record_eval


_llm_factory: Callable | None = None
_jobs = None
_enqueue: Callable | None = None
_config_loader: Callable | None = None

_REPO_ROOT = Path(__file__).resolve().parents[1]


def configure(*, llm_factory=None, jobs=None, enqueue=None, config_loader=None) -> None:
    global _llm_factory, _jobs, _enqueue, _config_loader
    _llm_factory = llm_factory
    _jobs = jobs
    _enqueue = enqueue
    _config_loader = config_loader


def _load_trigger_config():
    if _config_loader is not None:
        try:
            return _config_loader() or {}
        except Exception:
            return {}
    try:
        import yaml
        path = _REPO_ROOT / "auto_optimization" / "config.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _maybe_enqueue_low(question, answer, ev):
    if not ev or ev.get("total") is None:
        return
    cfg = _load_trigger_config()
    threshold = float(cfg.get("trigger", {}).get("immediate_threshold", 6))
    if ev["total"] > threshold:
        return
    try:
        if _enqueue is not None:
            _enqueue(question, answer, ev, cfg)
            return
        from optimization.agent import trigger
        incident = {
            "question": question,
            "score": ev["total"],
            "reason": ev.get("reason", ""),
            "answer": answer or "",
            "user_id": "auto_eval",
        }
        action, _ = trigger.enqueue(incident, cfg)
        print(f"[eval_trigger] total={ev['total']} action={action} | {question[:40]}", flush=True)
    except Exception as exc:
        print(f"[eval_trigger] 入队失败: {exc}", flush=True)


def _score(question, answer):
    from optimization.utils.scorer import score_answer_multi
    llm = _llm_factory() if _llm_factory else None
    if llm is None:
        return score_answer_multi(question, answer)
    return score_answer_multi(question, answer, llm=llm)


def _work(question, answer, port, latency_ms):
    try:
        ev = _score(question, answer)
        record_eval(question, answer, ev, port=port, latency_ms=latency_ms)
        _maybe_enqueue_low(question, answer, ev)
    except Exception:
        pass  # 评估失败不影响主流程


def log_eval_async(question, answer, port=None, latency_ms=None):
    """异步评估并记录；优先使用注入的 JobRunner。"""
    if _jobs is not None:
        _jobs.submit("evaluation", lambda: _work(question, answer, port, latency_ms))
        return
    threading.Thread(target=_work, args=(question, answer, port, latency_ms),
                     daemon=True).start()


def evaluate_and_record(question, answer, port=None, latency_ms=None):
    """同步评估并记录；返回评估结果（/ask_eval 用）。"""
    ev = _score(question, answer)
    record_eval(question, answer, ev, port=port, latency_ms=latency_ms)
    return ev
