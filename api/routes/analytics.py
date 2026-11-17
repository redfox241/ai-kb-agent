"""评估、访问与页面埋点 API。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from api.schemas import TrackRequest


router = APIRouter(prefix="/api", tags=["analytics"])


def _service(request: Request):
    return request.app.state.analytics


@router.get("/eval/stats")
def eval_stats(request: Request):
    return _service(request).stats()


@router.get("/eval/history")
def eval_history(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    min_total: float | None = None,
    max_total: float | None = None,
    q: str | None = None,
):
    return {
        "rows": _service(request).history(
            limit=limit, offset=offset, min_total=min_total,
            max_total=max_total, keyword=q,
        ),
        "limit": limit,
        "offset": offset,
    }


@router.post("/track")
def track(req: TrackRequest, request: Request):
    try:
        _service(request).record_visit(
            kind="page",
            path=req.path,
            endpoint=req.title or "page",
            port=request.app.state.port,
        )
    except Exception as exc:
        print(f"[visit] 页面访问记录失败: {exc}", flush=True)
    return {"ok": True}


@router.get("/visits")
def visits(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    kind: str | None = None,
    q: str | None = None,
):
    return {
        "rows": _service(request).visits(
            limit=limit, offset=offset, kind=kind, keyword=q,
        ),
        "limit": limit,
        "offset": offset,
    }


@router.get("/visit/stats")
def visit_stats(request: Request):
    return _service(request).visit_stats()
