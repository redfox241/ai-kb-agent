"""embedding 客户端：上下文保护与错误信息。"""
from __future__ import annotations

import pytest

from infra import embedding


class _Response:
    def __init__(self, status_code: int, payload: str) -> None:
        self.status_code = status_code
        self.text = payload

    def json(self):
        return {"embedding": [0.1, 0.2]}


def test_get_embedding_truncates_to_max_chars(monkeypatch):
    captured: dict = {}

    def fake_post(url, json, timeout, trust_env):
        captured.update(json)
        return _Response(200, "")

    monkeypatch.setenv("OLLAMA_EMBED_MAX_CHARS", "100")
    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    assert embedding.get_embedding("字" * 500) == [0.1, 0.2]
    assert len(captured["prompt"]) == 100


def test_get_embedding_reports_ollama_error_body(monkeypatch):
    def fake_post(url, json, timeout, trust_env):
        return _Response(500, '{"error":"the input length exceeds the context length"}')

    monkeypatch.setattr(embedding.httpx, "post", fake_post)

    with pytest.raises(RuntimeError) as exc:
        embedding.get_embedding("测试")
    assert "HTTP 500" in str(exc.value)
    assert "context length" in str(exc.value)
