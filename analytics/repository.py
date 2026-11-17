"""评估与访问分析服务：只依赖 AnalyticsRepository 端口，由组合根注入实现。"""
from __future__ import annotations

from typing import Mapping

from domain.ports import AnalyticsRepository


_repository: AnalyticsRepository | None = None


def configure(repository: AnalyticsRepository) -> None:
    global _repository
    _repository = repository


def configured() -> bool:
    return _repository is not None


def get_repository() -> AnalyticsRepository:
    if _repository is None:
        raise RuntimeError("AnalyticsRepository 未配置：请在组合根调用 configure()")
    return _repository


def init_db() -> None:
    get_repository().migrate()


def record_eval(question, answer, evaluation, port=None, latency_ms=None, sources=None):
    return get_repository().record_eval(
        question, answer, evaluation,
        port=port, latency_ms=latency_ms, sources=sources,
    )


def query_stats() -> Mapping[str, object]:
    return get_repository().stats()


def query_history(limit=50, min_total=None, max_total=None, offset=0, keyword=None):
    return get_repository().history(
        limit=limit, min_total=min_total, max_total=max_total,
        offset=offset, keyword=keyword,
    )


def query_low(min_total=6, limit=20):
    return get_repository().low(min_total=min_total, limit=limit)
