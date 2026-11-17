"""API 异常处理：领域错误 → HTTP。"""
from __future__ import annotations

from fastapi import FastAPI

from domain.errors import DomainError


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    def domain_error_handler(request, exc: DomainError):
        from fastapi.responses import JSONResponse
        payload = {"error": exc.code, **dict(exc.detail)}
        return JSONResponse(status_code=exc.http_status, content={"detail": payload})
