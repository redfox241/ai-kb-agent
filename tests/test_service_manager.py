"""AI-KB 服务管理脚本的单元测试。"""
from __future__ import annotations

import os
import socket
from types import SimpleNamespace

import pytest

from cli import service as manager


def test_pid_helpers(tmp_path):
    path = tmp_path / "x.pid"
    manager._write_pid(path, 123)
    assert manager._read_pid(path) == 123
    assert manager._pid_alive(os.getpid()) is True
    assert manager._pid_alive(0) is False


def test_port_open():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert manager._port_open(port) is True


def test_start_service_spawns_and_writes_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(manager, "PID_DIR", tmp_path / "pids")
    monkeypatch.setattr(manager, "_port_open", lambda port: False)
    monkeypatch.setattr(manager, "_wait_health", lambda *a, **k: True)
    captured = {}

    class Proc:
        pid = 4321

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return Proc()

    monkeypatch.setattr(manager.subprocess, "Popen", fake_popen)
    svc = manager.Service("prod", 8000, "server.py", "auto_optimization/overrides.prod.json")
    manager.start_service(svc, timeout=1)

    assert captured["cmd"][-1] == "server.py"
    assert captured["env"]["RAG_PORT"] == "8000"
    assert manager._read_pid(svc.pid_file) == 4321


def test_stop_service_terminates_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(manager, "PID_DIR", tmp_path / "pids")
    svc = manager.Service("prod", 8000, "server.py", "auto_optimization/overrides.prod.json")
    manager._write_pid(svc.pid_file, 555)
    killed = []
    values = [True, False]
    monkeypatch.setattr(manager, "_pid_alive", lambda pid: values.pop(0) if values else False)
    monkeypatch.setattr(manager.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    manager.stop_service(svc)

    assert killed and killed[0][0] == 555
    assert not svc.pid_file.exists()


def test_command_start_stop_dispatch(monkeypatch):
    started, stopped, daemon = [], [], []
    monkeypatch.setattr(manager, "start_service", lambda svc, **k: started.append(svc.port))
    monkeypatch.setattr(manager, "start_daemon", lambda **k: daemon.append(True))
    monkeypatch.setattr(manager, "stop_service", lambda svc: stopped.append(svc.port))
    monkeypatch.setattr(manager, "stop_daemon", lambda: daemon.append(False))

    args = SimpleNamespace(no_wait=False, wait=1, skip_daemon=True)
    assert manager.command_start(args) == 0
    assert started == [8000, 8001, 8002]
    assert daemon == []

    assert manager.command_stop(args) == 0
    assert stopped == [8000, 8001, 8002]
    assert daemon == [False]
