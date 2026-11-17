"""API app 工厂：路由、页面、中间件、异常处理集中装配。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.errors import install_error_handlers
from api.middleware import install_middleware
from api.routes.admin import router as admin_router
from api.routes.analytics import router as analytics_router
from api.routes.ask import router as ask_router
from api.routes.disco import router as disco_router
from api.routes.openai import router as openai_router
from api.routes.pages import register_pages
from api.routes.sessions import router as sessions_router
from web.pages import STATIC_DIR


def create_app(
    profile,
    *,
    port: int | None = None,
    start: Callable[[], None] | None = None,
    stop: Callable[[], None] | None = None,
    title: str | None = None,
) -> FastAPI:
    """按 profile 构建 app；生命周期由组合根注入的回调驱动。"""
    resolved_port = int(port or getattr(profile, "port", 8000))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start is not None:
            start()
        try:
            yield
        finally:
            if stop is not None:
                stop()

    app = FastAPI(
        title=title or f"AI-KB RAG ({getattr(profile, 'name', 'prod')})",
        lifespan=lifespan,
    )
    app.state.port = resolved_port
    app.state.profile = profile

    app.include_router(sessions_router)
    app.include_router(ask_router)
    app.include_router(openai_router)
    app.include_router(admin_router)
    app.include_router(analytics_router)
    app.include_router(disco_router)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    register_pages(app, resolved_port)
    install_middleware(app, resolved_port)
    install_error_handlers(app)
    return app
