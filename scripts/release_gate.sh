#!/bin/bash
# 本地发布门禁：测试 + 架构 + 启动健康检查；可选 benchmark。
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

PYTHON="${PYTHON:-.venv/bin/python}"
PORT="${GATE_PORT:-7933}"
DB="/tmp/aikb_gate_$$.db"
EVAL_DB="/tmp/aikb_gate_eval_$$.db"
LOG="/tmp/aikb_gate_$$.log"

echo "[gate] tests"
"$PYTHON" -m pytest -q

echo "[gate] architecture"
"$PYTHON" -m pytest tests/test_architecture.py -q

echo "[gate] start $PORT"
RAG_PORT="$PORT" \
RAG_SESSION_DB="$DB" \
RAG_EVAL_DB="$EVAL_DB" \
RAG_OVERRIDES="$PWD/auto_optimization/overrides.json" \
  "$PYTHON" server.py >"$LOG" 2>&1 &
PID=$!
cleanup() {
  kill "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
  rm -f "$DB" "$DB-wal" "$DB-shm" "$EVAL_DB" "$EVAL_DB-wal" "$EVAL_DB-shm"
}
trap cleanup EXIT

for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "[gate] health ok"
    break
  fi
  sleep 1
done
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null || {
  echo "[gate] health failed; log: $LOG" >&2
  tail -30 "$LOG" >&2 || true
  exit 1
}

if [ "${RUN_BENCHMARK:-0}" = "1" ]; then
  echo "[gate] benchmark"
  "$PYTHON" scripts/eval_multiturn.py \
    --base "http://127.0.0.1:$PORT" --out eval_mt_gate.json
fi

echo "[gate] passed"
