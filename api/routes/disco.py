"""Disco 路由：检索 + 话语分析 + 生成。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, Request

from api.presenters import sources_to_legacy
from api.schemas import AskRequest


router = APIRouter(tags=["disco"])


@dataclass(frozen=True)
class DiscoRuntime:
    port: int
    service_factory: Callable
    render_trace: Callable
    record_visit: Callable
    log_eval: Callable


def _runtime(request: Request) -> DiscoRuntime:
    return request.app.state.disco_runtime


@router.post("/ask_smart_disco")
def ask_smart_disco(req: AskRequest, request: Request):
    runtime = _runtime(request)
    started = time.time()
    try:
        result = runtime.service_factory().answer(req.question)
    except Exception:
        runtime.record_visit(
            req.question, "ask_smart_disco", [], [],
            int((time.time() - started) * 1000), status="error",
        )
        raise

    answer = result.answer
    steps = runtime.render_trace(answer.trace)
    sources = sources_to_legacy(answer.sources)
    latency = int((time.time() - started) * 1000)
    runtime.record_visit(req.question, "ask_smart_disco", sources, steps, latency)
    runtime.log_eval(req.question, answer.text, latency)
    return {
        "question": req.question,
        "answer": answer.text,
        "sources": sources,
        "disco": dict(result.disco),
        "steps": steps,
        "port": runtime.port,
    }
