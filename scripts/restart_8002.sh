#!/bin/bash
# 重启 8002 test服务并等待健康（PID 文件，不依赖 lsof/pkill）
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
cd "$ROOT"
PID_FILE="$ROOT/logs/pids/server_8002.pid"
LOG_FILE="$ROOT/logs/server_8002.log"
mkdir -p "$ROOT/logs/pids"
if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE")"
  kill "$PID" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.2
  done
  rm -f "$PID_FILE"
fi
RAG_PORT=8002 RAG_PROFILE=test \
RAG_OVERRIDES="$ROOT/auto_optimization/overrides.json" \
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  nohup "$PY" server_v3.py >>"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:8002/health" >/dev/null 2>&1; then
    echo "8002 healthy"
    exit 0
  fi
  sleep 1
done
echo "8002 未就绪（日志：$LOG_FILE）" >&2
exit 1
