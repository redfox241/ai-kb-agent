"""独立多维评估页面 /eval：输入问题 → 调 /ask_eval → 展示逐维度评分。"""
from pathlib import Path

from fastapi.responses import HTMLResponse

EVAL_PAGE = (Path(__file__).parent / "templates" / "eval.html").read_text(encoding="utf-8")


def register_eval_ui(app, port):
    @app.get("/eval", response_class=HTMLResponse, include_in_schema=False)
    def eval_page():
        return HTMLResponse(EVAL_PAGE.replace("@PORT@", str(port)))
