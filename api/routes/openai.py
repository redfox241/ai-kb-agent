"""OpenAI 兼容口：/v1/chat/completions。"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request, Response

from api.runtime import AskRuntime
from api.schemas import ChatRequest


router = APIRouter(tags=["openai"])


def _runtime(request: Request) -> AskRuntime:
    return request.app.state.ask_runtime


def _history_pairs(messages: list) -> list[dict]:
    pairs: list[dict] = []
    pending = None
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role, content = message.get("role"), message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            pending = content.strip()
        elif role == "assistant" and pending:
            pairs.append({"question": pending, "answer": content.strip()})
            pending = None
    return pairs


@router.post("/v1/chat/completions")
def chat_completions(req: ChatRequest, request: Request, response: Response):
    runtime = _runtime(request)
    msgs = req.messages if isinstance(req.messages, list) else []
    cleaned = [
        message for message in msgs
        if isinstance(message, dict)
        and message.get("role") in ("user", "assistant")
        and isinstance(message.get("content"), str)
        and message["content"].strip()
    ]
    if not cleaned or cleaned[-1].get("role") != "user":
        raise HTTPException(status_code=400, detail={
            "error": "invalid_messages",
            "detail": "messages 最后一条必须是 user",
        })

    question = cleaned[-1]["content"]
    history = _history_pairs(cleaned[:-1])[-runtime.max_history_turns:]

    sid = None
    if runtime.multiturn_enabled and not req.stateless:
        if req.session_id:
            sid = runtime.resolve_session(request, req.session_id)
        elif not history:
            sid = runtime.resolve_session(request, None)
        if sid:
            runtime.bind_cookie(response, sid)

    started = time.time()
    steps: list = []
    try:
        if runtime.multiturn_enabled and (sid or history):
            result = runtime.chat_v2(
                question, runtime.llm_factory(),
                session_id=sid,
                history=None if sid else history,
                use_rewrite=True,
                channel=runtime.port,
            )
            answer, steps = result["answer"], result["steps"]
            session_id = result["session_id"]
            resolved = result["resolved_query"]
        else:
            answer, _, steps = runtime.answer_question(
                question, runtime.llm_factory(), use_rewrite=True
            )
            session_id, resolved = None, question
    except Exception:
        runtime.record_visit(
            question, "chat_completions", [], steps,
            int((time.time() - started) * 1000), status="error",
        )
        raise

    latency = int((time.time() - started) * 1000)
    runtime.record_visit(question, "chat_completions", [], steps, latency)
    return {
        "id": "chatcmpl-kb",
        "object": "chat.completion",
        "model": req.model or "ai-kb",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
        "steps": steps,
        "session_id": session_id,
        "resolved_query": resolved,
        "ai_kb": {
            "session_id": session_id,
            "resolved_query": resolved,
            "latency_ms": latency,
        },
    }
