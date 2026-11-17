"""API 运行时依赖：由 server 组合根注入，路由只调用回调。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class AskRuntime:
    port: int
    multiturn_enabled: bool
    run_multiturn: Callable
    chat_v2: Callable
    answer_question: Callable
    max_history_turns: int
    resolve_session: Callable
    bind_cookie: Callable
    record_visit: Callable
    log_eval: Callable
    evaluate: Callable
    llm_factory: Callable
