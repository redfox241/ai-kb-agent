#!/bin/bash
# 浏览器级冒烟：启动测试端口，用 Chrome headless 执行页面 JS，检查健康状态与页面结构。
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

PYTHON="${PYTHON:-.venv/bin/python}"
PORT="${E2E_PORT:-7934}"
DB="/tmp/aikb_e2e_$$.db"
EVAL_DB="/tmp/aikb_e2e_eval_$$.db"
LOG="/tmp/aikb_e2e_$$.log"
DOM="/tmp/aikb_e2e_$$.html"

CHROME="${CHROME_BIN:-}"
if [ -z "$CHROME" ]; then
  for candidate in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "$(command -v google-chrome 2>/dev/null || true)" \
    "$(command -v chromium 2>/dev/null || true)" \
    "$(command -v chromium-browser 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then CHROME="$candidate"; break; fi
  done
fi
if [ -z "$CHROME" ]; then
  echo "[e2e] Chrome 不可用，跳过浏览器 E2E"
  exit 0
fi

RAG_PORT="$PORT" \
RAG_SESSION_DB="$DB" \
RAG_EVAL_DB="$EVAL_DB" \
RAG_OVERRIDES="$PWD/auto_optimization/overrides.json" \
  "$PYTHON" server.py >"$LOG" 2>&1 &
PID=$!
cleanup() {
  kill "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
  rm -f "$DB" "$DB-wal" "$DB-shm" "$EVAL_DB" "$EVAL_DB-wal" "$EVAL_DB-shm" "$DOM"
}
trap cleanup EXIT

for _ in $(seq 1 40); do
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null || {
  echo "[e2e] health failed; log: $LOG" >&2
  tail -30 "$LOG" >&2 || true
  exit 1
}

"$CHROME" --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --virtual-time-budget=8000 --dump-dom "http://127.0.0.1:$PORT/chat" >"$DOM" 2>/dev/null

grep -q "健康" "$DOM" || {
  echo "[e2e] 页面健康状态未渲染" >&2
  grep -n "health" "$DOM" | head -5 >&2 || true
  exit 1
}
grep -q "多轮对话" "$DOM" || { echo "[e2e] 页面标题缺失" >&2; exit 1; }

echo "[e2e] browser smoke passed"
