"""PR-12：新链路 API 接线。"""
from __future__ import annotations

import bootstrap as server


def _keys(payload):
    return [step["key"] for step in payload["steps"]]


def test_multiturn_uses_v2_by_default(client):
    response = client.post("/ask_smart", json={
        "question": "孙悟空是谁？",
        "session_id": "ses_wiring_v2",
    })
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["port"] == server.PORT
    assert data["session_id"] == "ses_wiring_v2"
    assert data["turn_id"] == 1
    assert "cite" in _keys(data)
    assert {"accept", "bm25", "semantic", "merge", "expand", "context", "generate"} <= set(_keys(data))


def test_openai_endpoint_uses_v2(client):
    response = client.post("/v1/chat/completions", json={"messages": [
        {"role": "user", "content": "孙悟空是谁？"},
        {"role": "assistant", "content": "孙悟空是《西游记》人物。"},
        {"role": "user", "content": "他师父呢？"},
    ]})
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["session_id"] is None
    assert data["resolved_query"] == "孙悟空的师父是谁？"
    assert "cite" in _keys(data)
