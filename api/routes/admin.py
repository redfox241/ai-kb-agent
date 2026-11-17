"""运维 API：health / index_report / reindex。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from fastapi import APIRouter, Request


router = APIRouter(tags=["admin"])


@dataclass(frozen=True)
class AdminRuntime:
    port: int
    state: Mapping
    vault_path: str
    index_path: str
    rebuild: Callable


def _runtime(request: Request) -> AdminRuntime:
    return request.app.state.admin_runtime


@router.get("/health")
def health(request: Request):
    runtime = _runtime(request)
    state = runtime.state
    return {
        "status": "ok",
        "port": runtime.port,
        "index": state.status,
        "n_chunks": len(state.chunks),
        "built_at": state.built_at,
    }


@router.get("/index_report")
def index_report(request: Request):
    runtime = _runtime(request)
    state = runtime.state
    return {
        "status": state.status,
        "n_chunks": len(state.chunks),
        "built_at": state.built_at,
        "error": state.error,
        "vault": runtime.vault_path,
        "index_dir": runtime.index_path,
    }


@router.get("/reindex_status")
def reindex_status(request: Request):
    runtime = _runtime(request)
    state = runtime.state
    return {
        "status": state.status,
        "reindex": state.reindex,
        "error": state.error,
        "kp": {"status": "idle", "done": 0, "total": 0},
    }


@router.post("/reindex")
def reindex(request: Request):
    runtime = _runtime(request)
    if runtime.state.status == "building":
        return {"status": "already_building"}
    runtime.rebuild()
    return {"status": "started"}


@router.post("/reindex_partial")
def reindex_partial(request: Request):
    # 本地简化：部分重建直接走全量。
    return reindex(request)
