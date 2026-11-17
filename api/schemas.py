"""API 请求 / 响应 DTO。"""
from __future__ import annotations

from pydantic import BaseModel


class AskRequest(BaseModel):
    question: str
    top_k: int | None = None
    session_id: str | None = None
    reset: bool = False
    stateless: bool = False


class ChatRequest(BaseModel):
    messages: list
    model: str | None = None
    session_id: str | None = None
    stream: bool = False
    stateless: bool = False


class TrackRequest(BaseModel):
    path: str = "/"
    title: str | None = None


class SessionPatch(BaseModel):
    title: str = ""
