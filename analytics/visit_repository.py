"""访问记录服务：复用 AnalyticsRepository 端口（与评估共享同一实现）。"""
from __future__ import annotations

from .repository import get_repository


def init_db() -> None:
    get_repository().migrate()


def record_visit(kind="query", question=None, path=None, endpoint=None,
                 n_sources=None, n_steps=None, latency_ms=None,
                 status="ok", port=None):
    return get_repository().record_visit(
        kind=kind, question=question, path=path, endpoint=endpoint,
        n_sources=n_sources, n_steps=n_steps, latency_ms=latency_ms,
        status=status, port=port,
    )


def query_visits(limit=50, offset=0, kind=None, keyword=None):
    return get_repository().visits(
        limit=limit, offset=offset, kind=kind, keyword=keyword,
    )


def query_visit_stats():
    return get_repository().visit_stats()
