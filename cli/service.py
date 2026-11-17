"""AI-KB 服务管理：start / stop / restart / status / health。

跨平台实现：PID 文件 + socket 探活 + HTTP health，不依赖 lsof。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from config.profiles import PROFILES


BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs"
PID_DIR = LOG_DIR / "pids"


@dataclass(frozen=True)
class Service:
    name: str
    port: int
    entry: str
    overrides: str

    @property
    def log_file(self) -> Path:
        return LOG_DIR / f"server_{self.port}.log"

    @property
    def pid_file(self) -> Path:
        return PID_DIR / f"server_{self.port}.pid"


_ENTRYPOINTS = {
    "prod": "server.py",
    "experiment": "server_auto.py",
    "test": "server_v3.py",
}

# 端口 / overrides 唯一来源：config.profiles.PROFILES
SERVICES = tuple(
    Service(
        profile.name,
        profile.port,
        _ENTRYPOINTS[profile.name],
        str(profile.overrides_path.relative_to(BASE_DIR)),
    )
    for profile in PROFILES.values()
)

DAEMON_NAME = "optimizer"
DAEMON_PID = PID_DIR / "daemon.pid"
DAEMON_LOG = LOG_DIR / "daemon.log"


def _mkdirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")


def _port_open(port: int, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _health(port: int, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def _wait_health(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _health(port, timeout=1.0):
            return True
        time.sleep(1)
    return False


def _spawn(cmd: list[str], env: dict, log_file: Path, pid_file: Path) -> int:
    _mkdirs()
    log = log_file.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    _write_pid(pid_file, process.pid)
    return process.pid


def _terminate(pid: int, timeout: float = 5.0) -> None:
    if not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def start_service(service: Service, *, wait: bool = True, timeout: float = 20.0) -> None:
    if _port_open(service.port):
        print(f"  {service.port} 已在运行")
        return
    env = os.environ.copy()
    env.update({
        "RAG_PORT": str(service.port),
        "RAG_PROFILE": service.name,
        "RAG_OVERRIDES": str(BASE_DIR / service.overrides),
        "PYTHONPATH": str(BASE_DIR) + os.pathsep + env.get("PYTHONPATH", ""),
    })
    pid = _spawn(
        [sys.executable, service.entry],
        env,
        service.log_file,
        service.pid_file,
    )
    if wait and not _wait_health(service.port, timeout):
        print(f"  {service.port} 启动中（pid {pid}，健康检查未在 {timeout:.0f}s 内完成）")
    else:
        print(f"  {service.port} ok（pid {pid}）")


def stop_service(service: Service) -> None:
    pid = _read_pid(service.pid_file)
    if pid is None:
        if _port_open(service.port):
            print(f"  {service.port} 端口占用，但无 PID 文件，跳过")
        else:
            print(f"  {service.port} 未运行")
        return
    print(f"  停止 {service.port}（pid {pid}）...")
    _terminate(pid)
    try:
        service.pid_file.unlink()
    except FileNotFoundError:
        pass


def start_daemon(*, wait: bool = True) -> None:
    pid = _read_pid(DAEMON_PID)
    if pid and _pid_alive(pid):
        print(f"  守护进程已在运行（pid {pid}）")
        return
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BASE_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    pid = _spawn(
        [sys.executable, "-m", "optimization.agent.trigger", "--daemon"],
        env,
        DAEMON_LOG,
        DAEMON_PID,
    )
    if wait:
        time.sleep(0.5)
        if not _pid_alive(pid):
            print("  守护进程启动失败，请查看 logs/daemon.log")
            return
    print(f"  守护进程 ok（pid {pid}）")


def stop_daemon() -> None:
    pid = _read_pid(DAEMON_PID)
    if pid is None or not _pid_alive(pid):
        print("  守护进程未运行")
        try:
            DAEMON_PID.unlink()
        except FileNotFoundError:
            pass
        return
    print(f"  停止守护进程（pid {pid}）...")
    _terminate(pid)
    try:
        DAEMON_PID.unlink()
    except FileNotFoundError:
        pass


def command_start(args) -> int:
    _mkdirs()
    print("启动 AI-KB 服务：")
    for service in SERVICES:
        start_service(service, wait=not args.no_wait, timeout=args.wait)
    if not args.skip_daemon:
        start_daemon(wait=True)
    print("完成")
    return 0


def command_stop(args) -> int:
    print("停止 AI-KB 服务：")
    stop_daemon()
    for service in SERVICES:
        stop_service(service)
    print("完成")
    return 0


def command_status(args) -> int:
    print("AI-KB 服务状态：")
    for service in SERVICES:
        pid = _read_pid(service.pid_file)
        running = bool(pid and _pid_alive(pid))
        port = "open" if _port_open(service.port) else "closed"
        print(f"  {service.port:<5} {service.name:<10} "
              f"{'running' if running else 'stopped':<8} port={port} pid={pid or '-'}")
    daemon = _read_pid(DAEMON_PID)
    print(f"  daemon     {'running' if daemon and _pid_alive(daemon) else 'stopped'}"
          f" pid={daemon or '-'}")
    return 0


def command_health(args) -> int:
    print("AI-KB 健康检查：")
    ok = True
    for service in SERVICES:
        data = _health(service.port)
        if data:
            print(f"  {service.port} ok {data}")
        else:
            ok = False
            print(f"  {service.port} 不可达")
    return 0 if ok else 1


def command_restart(args) -> int:
    command_stop(args)
    return command_start(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-kb", description="AI-KB 服务管理")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--skip-daemon", action="store_true", help="不启动自演进守护进程")
        p.add_argument("--no-wait", action="store_true", help="不等待健康检查")
        p.add_argument("--wait", type=float, default=20.0, help="健康检查超时秒数")

    for name, fn in (
        ("start", command_start),
        ("stop", command_stop),
        ("restart", command_restart),
        ("status", command_status),
        ("health", command_health),
    ):
        p = sub.add_parser(name)
        common(p)
        p.set_defaults(func=fn)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
