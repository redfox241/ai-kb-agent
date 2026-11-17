"""P2：页面静态契约（不执行 JS）。"""
from __future__ import annotations


def test_pages_have_no_port_placeholder(client):
    for path in ("/", "/chat", "/single", "/eval", "/analysis"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "@PORT@" not in response.text, path
        assert "8000" in response.text, path
        assert "<html" in response.text.lower() and "</html>" in response.text.lower()
        assert "<script" in response.text.lower(), path
