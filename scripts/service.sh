#!/bin/bash
# AI-KB 服务管理统一入口：start / stop / restart / status / health
# 用法: scripts/service.sh [start|stop|restart|status|health] [--skip-daemon] [--no-wait] [--wait N]
# 不带子命令时默认 start；实现见 cli/service.py。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
cd "$ROOT"

cmd="start"
case "${1:-}" in
  start|stop|restart|status|health)
    cmd="$1"
    shift
    ;;
  ""|-*)
    ;;  # 无参数或直接跟选项：默认 start
  *)
    echo "用法: $0 [start|stop|restart|status|health] [--skip-daemon] [--no-wait] [--wait N]" >&2
    exit 2
    ;;
esac
exec "$PY" -m cli.service "$cmd" "$@"
