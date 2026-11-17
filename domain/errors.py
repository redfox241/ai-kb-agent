"""领域错误：携带稳定的错误码、HTTP 映射与可重试语义。

API 层负责把 DomainError 翻译成 HTTP 响应；核心层不 import FastAPI。
"""
from __future__ import annotations

from typing import Mapping


class DomainError(Exception):
    code: str = "domain_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, detail: Mapping[str, object] | None = None):
        super().__init__(message)
        self.detail: dict[str, object] = dict(detail or {})


class IndexNotReady(DomainError):
    code = "index_not_ready"
    http_status = 503
    retryable = True


class ModelUnavailable(DomainError):
    code = "model_unavailable"
    http_status = 502
    retryable = True


class InvalidSession(DomainError):
    code = "invalid_session"
    http_status = 400


class SessionNotFound(DomainError):
    code = "session_not_found"
    http_status = 404


class DomainValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class BudgetExceeded(DomainError):
    code = "budget_exceeded"
    http_status = 429


class ToolPermissionDenied(DomainError):
    code = "tool_permission_denied"
    http_status = 403


class ConfigurationError(DomainError):
    code = "configuration_error"
    http_status = 500
