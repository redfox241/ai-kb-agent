"""P2：run_server 启动分支。"""
from __future__ import annotations

import bootstrap as bootstrap


class FakeRepo:
    def __init__(self, purged=0, fail=False):
        self.purged = purged
        self.fail = fail
        self.purge_called = 0

    def purge(self):
        self.purge_called += 1
        if self.fail:
            raise RuntimeError("purge down")
        return self.purged


class FakeService:
    def __init__(self, loaded=True):
        self.loaded = loaded
        self.loaded_called = 0
        self.rebuild_called = 0
        self.current_called = 0

    def load(self):
        self.loaded_called += 1
        return self.loaded

    def rebuild(self, background=True):
        self.rebuild_called += 1

    def current(self):
        self.current_called += 1
        return type("Snapshot", (), {"built_at": "test", "chunks": (1, 2)})()


def _patch(monkeypatch, repo, service):
    calls = {}
    monkeypatch.setattr(bootstrap, "_session_repo", lambda: repo)
    monkeypatch.setattr(bootstrap, "_index_service", lambda: service)
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, **kwargs: calls.update({"app": app, **kwargs}),
    )
    return calls


def test_run_server_loaded(monkeypatch):
    repo, service = FakeRepo(purged=2), FakeService(loaded=True)
    calls = _patch(monkeypatch, repo, service)

    bootstrap.run_server(7999)

    assert repo.purge_called == 1
    assert service.loaded_called == 1
    assert service.rebuild_called == 0
    assert service.current_called == 1
    assert calls["port"] == 7999
    assert calls["host"] == "127.0.0.1"


def test_run_server_rebuilds_when_missing(monkeypatch):
    repo, service = FakeRepo(), FakeService(loaded=False)
    _patch(monkeypatch, repo, service)

    bootstrap.run_server(7998)

    assert service.rebuild_called == 1
    assert service.current_called == 0


def test_run_server_survives_purge_failure(monkeypatch):
    repo, service = FakeRepo(fail=True), FakeService(loaded=True)
    calls = _patch(monkeypatch, repo, service)

    bootstrap.run_server(7997)

    assert repo.purge_called == 1
    assert calls["port"] == 7997
