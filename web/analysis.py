"""分析展示页面 /analysis：聚合访问记录与评估记录，可视化各维度质量。"""
from pathlib import Path

from fastapi.responses import HTMLResponse

ANALYSIS_PAGE = (Path(__file__).parent / "templates" / "analysis.html").read_text(encoding="utf-8")


def register_analysis(app, port):
    @app.get("/analysis", response_class=HTMLResponse, include_in_schema=False)
    def analysis_page():
        return HTMLResponse(ANALYSIS_PAGE.replace("@PORT@", str(port)))
