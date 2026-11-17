"""会话管理 API：原生 SQLite Repository。"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from api.schemas import SessionPatch


router = APIRouter(prefix="/api", tags=["sessions"])


def _repo(request: Request):
    factory = request.app.state.session_repo
    return factory()


def _port(request: Request) -> int:
    return int(getattr(request.app.state, "port", 0))


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "session_not_found"})


@router.get("/sessions/stats")
def session_stats(request: Request):
    return _repo(request).stats(_port(request))


@router.get("/sessions")
def list_sessions(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    all_channels: bool = False,
):
    repo = _repo(request)
    port = _port(request)
    return {
        "sessions": [
            asdict(item)
            for item in repo.list_sessions(
                limit=limit,
                offset=offset,
                channel=None if all_channels else port,
            )
        ],
        "stats": repo.stats(port),
    }


@router.get("/sessions/{session_id}")
def session_detail(request: Request, session_id: str, limit: int = 40):
    repo = _repo(request)
    session = repo.get(session_id)
    if session is None:
        raise _not_found()
    return {
        "session": asdict(session),
        "messages": repo.messages(session_id, limit=limit),
        "entities": repo.entities(session_id),
        "traces": repo.traces(session_id, limit=20),
    }


@router.delete("/sessions/{session_id}")
def delete_session(request: Request, session_id: str):
    repo = _repo(request)
    if repo.get(session_id) is None:
        raise _not_found()
    repo.delete(session_id)
    return {"deleted": session_id}


@router.post("/sessions/{session_id}/reset")
def reset_session(request: Request, session_id: str):
    repo = _repo(request)
    if repo.get(session_id) is None:
        raise _not_found()
    repo.reset(session_id)
    return {"reset": session_id}


@router.patch("/sessions/{session_id}")
def rename_session(request: Request, session_id: str, req: SessionPatch):
    repo = _repo(request)
    if repo.get(session_id) is None:
        raise _not_found()
    repo.rename(session_id, req.title)
    return {"session": asdict(repo.get(session_id))}


@router.get("/sessions/{session_id}/export")
def export_session(request: Request, session_id: str):
    repo = _repo(request)
    if repo.get(session_id) is None:
        raise _not_found()
    return PlainTextResponse(
        repo.export_markdown(session_id),
        media_type="text/markdown; charset=utf-8",
    )
