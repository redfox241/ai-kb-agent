"""P0 API 契约冒烟：入口、字段、错误、cookie 与页面。

不触网：client fixture 已替换 LLM / 评估 / 埋点 / Disco。
"""
from __future__ import annotations

import bootstrap as server


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["index"] == "ready"


def test_ask_stateless_contract(client):
    response = client.post("/ask", json={"question": "孙悟空是谁？", "stateless": True})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["answer"] and "须菩提" in data["answer"]
    assert data["sources"]
    assert data["steps"]
    assert data.get("session_id") is None


def test_ask_smart_multiturn_contract(client):
    first = client.post("/ask_smart", json={
        "question": "孙悟空是谁？", "session_id": "ses_api_contract_1",
    })
    assert first.status_code == 200, first.text
    first_data = first.json()
    assert first_data["session_id"] == "ses_api_contract_1"
    assert first_data["turn_id"] == 1
    assert first_data["intent"] == "new_topic"
    assert first_data["citations"]["ok"] is True

    second = client.post("/ask_smart", json={
        "question": "他师父呢？", "session_id": "ses_api_contract_1",
    })
    assert second.status_code == 200, second.text
    second_data = second.json()
    assert second_data["turn_id"] == 2
    assert second_data["resolved_query"] == "孙悟空的师父是谁？"
    assert second_data["memory"]["turns_used"] == 1
    assert any(step["key"] == "resolve" for step in second_data["steps"])


def test_ask_eval_contract(client):
    response = client.post("/ask_eval", json={
        "question": "孙悟空是谁？", "session_id": "ses_api_contract_eval",
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["evaluation"]["total"] == 8.0
    assert data["answer"]


def test_ask_disco_contract(client):
    response = client.post("/ask_smart_disco", json={
        "question": "孙悟空是谁？", "stateless": True,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert "disco" in data
    assert data["answer"]


def test_openai_compatible_contract(client):
    response = client.post("/v1/chat/completions", json={"messages": [
        {"role": "user", "content": "孙悟空是谁？"},
        {"role": "assistant", "content": "孙悟空是《西游记》人物。"},
        {"role": "user", "content": "他师父呢？"},
    ]})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["choices"][0]["message"]["content"]
    assert data["resolved_query"] == "孙悟空的师父是谁？"
    assert data["session_id"] is None
    assert data["ai_kb"]["resolved_query"] == "孙悟空的师父是谁？"


def test_openai_rejects_non_user_last_message(client):
    response = client.post("/v1/chat/completions", json={"messages": [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]})
    assert response.status_code == 400


def test_sessions_crud(client):
    created = client.post("/ask_smart", json={
        "question": "孙悟空是谁？", "session_id": "ses_api_contract_crud",
    })
    assert created.status_code == 200, created.text
    follow_up = client.post("/ask_smart", json={
        "question": "他师父呢？", "session_id": "ses_api_contract_crud",
    })
    assert follow_up.status_code == 200, follow_up.text

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert "ses_api_contract_crud" in [s["session_id"] for s in listed.json()["sessions"]]

    detail = client.get("/api/sessions/ses_api_contract_crud")
    assert detail.status_code == 200
    detail_data = detail.json()
    assert len(detail_data["messages"]) == 4
    assert detail_data["entities"]

    exported = client.get("/api/sessions/ses_api_contract_crud/export")
    assert exported.status_code == 200
    assert "须菩提" in exported.text

    renamed = client.patch("/api/sessions/ses_api_contract_crud",
                           json={"title": "悟空师承"})
    assert renamed.status_code == 200
    assert renamed.json()["session"]["title"] == "悟空师承"

    reset = client.post("/api/sessions/ses_api_contract_crud/reset")
    assert reset.status_code == 200
    assert client.get("/api/sessions/ses_api_contract_crud").json()["messages"] == []

    deleted = client.delete("/api/sessions/ses_api_contract_crud")
    assert deleted.status_code == 200
    assert client.get("/api/sessions/ses_api_contract_crud").status_code == 404


def test_cookie_session_fallback(client):
    first = client.post("/ask_smart", json={"question": "孙悟空是谁？"})
    assert first.status_code == 200, first.text
    first_data = first.json()
    sid = first_data.get("session_id")
    assert sid
    assert any(key.lower() == "set-cookie" for key in first.headers.keys())

    second = client.post("/ask_smart", json={"question": "他师父呢？"})
    assert second.status_code == 200, second.text
    second_data = second.json()
    assert second_data["session_id"] == sid
    assert second_data["turn_id"] == 2


def test_pages_render(client):
    expectations = {
        "/": ("多轮对话", "sessionSel", "/static/chat.js"),
        "/chat": ("sessionSel", "/static/chat.js"),
        "/single": ("单轮模式", "stateless", "/static/single.js"),
        "/eval": ("多维评估", "/static/eval.js"),
        "/analysis": ("评估分析", "/static/analysis.js"),
    }
    for path, needles in expectations.items():
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.text
        for asset in [item for item in needles if item.startswith("/static/")]:
            asset_response = client.get(asset)
            assert asset_response.status_code == 200, asset
            body += asset_response.text
        for needle in needles:
            if needle.startswith("/static/"):
                continue
            assert needle in body, (path, needle)


def test_analytics_endpoints(client):
    assert client.get("/api/eval/stats").status_code == 200
    assert client.get("/api/eval/history?limit=5").status_code == 200
    assert client.post("/api/track", json={"path": "/chat", "title": "test"}).status_code == 200
    assert client.get("/api/visits?limit=5").status_code == 200
    assert client.get("/api/visit/stats").status_code == 200


def test_index_not_ready_maps_to_503(client, monkeypatch):
    from domain import IndexNotReady, IndexStatus, ReindexProgress

    class EmptyIndexService:
        status = IndexStatus.READY
        error = None
        progress = ReindexProgress()

        def current(self):
            raise IndexNotReady("索引未就绪", detail={"status": "empty"})

    monkeypatch.setattr(server, "_INDEX_SERVICE", EmptyIndexService())

    response = client.post("/ask", json={"question": "孙悟空是谁？", "stateless": True})
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "index_not_ready"
