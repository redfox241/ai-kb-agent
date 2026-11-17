"""聊天式前端（/chat）：多轮消息流 + 每轮轨迹 + 会话管理。

与 ui.py（单轮页面）并存，便于灰度；渲染器与 ui.py 保持同一实现思路。
"""
from pathlib import Path

from fastapi.responses import HTMLResponse

PAGE = (Path(__file__).parent / "templates" / "chat.html").read_text(encoding="utf-8")


def register_chat_ui(app, port, path="/chat"):
    @app.get(path, response_class=HTMLResponse, include_in_schema=False)
    def chat_page():
        return HTMLResponse(PAGE.replace("@PORT@", str(port)))
