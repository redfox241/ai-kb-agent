"""页面注册：把 web 层页面挂到 FastAPI app。"""
from __future__ import annotations


def register_pages(app, port: int) -> None:
    from web.analysis import register_analysis
    from web.chat import register_chat_ui
    from web.eval import register_eval_ui
    from web.single import register_ui

    register_ui(app, port, "/single")
    register_eval_ui(app, port)
    register_analysis(app, port)
    register_chat_ui(app, port, "/")
    register_chat_ui(app, port, "/chat")
