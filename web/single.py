"""Web UI：挂载到 RAG 服务的根路径，提供浏览器问答界面。"""
from pathlib import Path

from fastapi.responses import HTMLResponse

PAGE = (Path(__file__).parent / "templates" / "single.html").read_text(encoding="utf-8")


def register_ui(app, port, path="/single"):
    @app.get(path, response_class=HTMLResponse, include_in_schema=False)
    def index():
        return HTMLResponse(PAGE.replace("@PORT@", str(port)))
