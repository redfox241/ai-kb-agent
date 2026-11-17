"""Web 静态资源目录入口（页面路由见 api/routes/pages.py）。"""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
