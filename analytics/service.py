"""AnalyticsService：评估与访问分析的用例门面。"""
from __future__ import annotations

from domain.ports import AnalyticsRepository


class AnalyticsService:
    def __init__(self, repository: AnalyticsRepository) -> None:
        self._repository = repository

    def migrate(self) -> None:
        self._repository.migrate()

    def record_eval(self, question, answer, evaluation, port=None, latency_ms=None, sources=None):
        return self._repository.record_eval(
            question, answer, evaluation,
            port=port, latency_ms=latency_ms, sources=sources,
        )

    def stats(self):
        return self._repository.stats()

    def history(self, limit=50, min_total=None, max_total=None, offset=0, keyword=None):
        return self._repository.history(
            limit=limit, min_total=min_total, max_total=max_total,
            offset=offset, keyword=keyword,
        )

    def low(self, min_total=6, limit=20):
        return self._repository.low(min_total=min_total, limit=limit)

    def record_visit(self, **kwargs):
        return self._repository.record_visit(**kwargs)

    def visits(self, limit=50, offset=0, kind=None, keyword=None):
        return self._repository.visits(
            limit=limit, offset=offset, kind=kind, keyword=keyword,
        )

    def visit_stats(self):
        return self._repository.visit_stats()
