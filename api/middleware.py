"""接入层中间件：Request-Id + 页面访问日志。

只依赖 application（analytics），不直接触碰 infra。
"""
from __future__ import annotations

import time
import uuid

from analytics.visit_repository import record_visit


def _should_log(path: str) -> bool:
    if not path or path.startswith("/api/") or path.startswith("/static/"):
        return False
    return path not in {"/health", "/openapi.json", "/docs", "/redoc", "/favicon.ico"}


def install_middleware(app, port: int) -> None:
    @app.middleware("http")
    async def access_log(request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        response = await call_next(request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        response.headers["x-request-id"] = request_id

        if _should_log(request.url.path):
            try:
                record_visit(
                    kind="page",
                    path=request.url.path,
                    endpoint=f"{request.method} {request.url.path}",
                    latency_ms=latency_ms,
                    status="ok" if response.status_code < 400 else "error",
                    port=port,
                )
            except Exception:
                pass
        return response
