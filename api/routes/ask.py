"""问答 API：/ask、/ask_smart、/ask_eval。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response

from api.runtime import AskRuntime
from api.schemas import AskRequest


router = APIRouter(tags=["ask"])


def _runtime(request: Request) -> AskRuntime:
    return request.app.state.ask_runtime


def _run(
    req: AskRequest,
    request: Request,
    response: Response,
    *,
    endpoint: str,
    rewrite: bool,
    evaluate: bool,
    include_port: bool,
) -> dict:
    runtime = _runtime(request)
    llm = runtime.llm_factory()
    started = time.time()

    sid = None
    if runtime.multiturn_enabled and not req.stateless:
        sid = runtime.resolve_session(request, req.session_id)
        runtime.bind_cookie(response, sid)

    if sid:
        try:
            result = runtime.run_multiturn(
                req.question, llm, sid,
                use_rewrite=rewrite,
                reset=req.reset,
            )
        except Exception:
            runtime.record_visit(
                req.question, endpoint, [], [],
                int((time.time() - started) * 1000), status="error",
            )
            raise
        runtime.record_visit(
            req.question, endpoint, result["sources"], result["steps"],
            result["latency_ms"],
        )
        if evaluate:
            result["evaluation"] = runtime.evaluate(
                req.question, result["answer"], result["latency_ms"]
            )
        else:
            runtime.log_eval(req.question, result["answer"], result["latency_ms"])
        return result

    steps: list = []
    try:
        answer, sources, steps = runtime.answer_question(
            req.question, llm, use_rewrite=rewrite
        )
    except Exception:
        runtime.record_visit(
            req.question, endpoint, [], steps,
            int((time.time() - started) * 1000), status="error",
        )
        raise

    latency = int((time.time() - started) * 1000)
    runtime.record_visit(req.question, endpoint, sources, steps, latency)
    if evaluate:
        evaluation = runtime.evaluate(req.question, answer, latency)
        return {"question": req.question, "answer": answer, "sources": sources,
                "steps": steps, "evaluation": evaluation, "port": runtime.port}

    runtime.log_eval(req.question, answer, latency)
    payload = {"question": req.question, "answer": answer, "sources": sources,
               "steps": steps}
    if include_port:
        payload["port"] = runtime.port
    return payload


@router.post("/ask")
def ask(req: AskRequest, request: Request, response: Response):
    return _run(req, request, response, endpoint="ask", rewrite=False,
                evaluate=False, include_port=False)


@router.post("/ask_smart")
def ask_smart(req: AskRequest, request: Request, response: Response):
    return _run(req, request, response, endpoint="ask_smart", rewrite=True,
                evaluate=False, include_port=True)


@router.post("/ask_eval")
def ask_eval(req: AskRequest, request: Request, response: Response):
    return _run(req, request, response, endpoint="ask_eval", rewrite=True,
                evaluate=True, include_port=True)
