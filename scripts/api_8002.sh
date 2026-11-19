#!/usr/bin/env bash
# ai-kb API 调用脚本
#
# 默认访问 8002（test profile）；设置 RAG_PORT=8001 可切到 experiment。
# 8001 专用入口见 scripts/api_8001.sh。
#
# 依赖：curl、jq
# 用法：
#   ./scripts/api_8002.sh health
#   ./scripts/api_8002.sh ask "什么是 RAG？"
#   ./scripts/api_8002.sh ask_smart "长征从什么时候开始？"
#   ./scripts/api_8002.sh ask_eval "平型关大捷发生在哪一年？"
#   ./scripts/api_8002.sh chat "水浒传中谁杀过老虎？"
#   ./scripts/api_8002.sh disco "什么是 ReAct 范式？"
#
#   ./scripts/api_8002.sh sessions
#   ./scripts/api_8002.sh session <session_id>
#   ./scripts/api_8002.sh session-rename <session_id> "新标题"
#   ./scripts/api_8002.sh session-reset <session_id>
#   ./scripts/api_8002.sh session-delete <session_id>
#   ./scripts/api_8002.sh session-export <session_id>
#
#   ./scripts/api_8002.sh eval-stats
#   ./scripts/api_8002.sh eval-history 20
#   ./scripts/api_8002.sh visits 20
#   ./scripts/api_8002.sh visit-stats
#
#   ./scripts/api_8002.sh index
#   ./scripts/api_8002.sh reindex
#   ./scripts/api_8002.sh reindex-status
#
#   ./scripts/api_8002.sh smoke
#   ./scripts/api_8002.sh start | stop | status
#
# 可覆盖环境变量：
#   RAG_PORT=8001    # 目标端口，默认 8002
#   RAG_BASE=http://127.0.0.1:8001
#   RAG_TIMEOUT=120
#   RAG_COOKIE_JAR=/tmp/ai-kb-8001.cookies
#   SESSION_ID=xxx   # ask_smart/chat 指定已有会话
#   RAW=1            # 输出完整响应（默认省略体积很大的 steps 调试轨迹）
#
# 示例：
#   RAG_PORT=8001 ./scripts/api_8002.sh health
#   RAW=1 ./scripts/api_8002.sh ask "什么是 RAG？" | jq .
#   ./scripts/api_8002.sh ask_smart "长征从什么时候开始？"
#   SESSION_ID=xxx ./scripts/api_8002.sh ask_smart "它走了多少公里？"

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${RAG_PORT:-8002}"
BASE="${RAG_BASE:-http://127.0.0.1:$PORT}"
TIMEOUT="${RAG_TIMEOUT:-120}"
COOKIE_JAR="${RAG_COOKIE_JAR:-/tmp/ai-kb-$PORT.cookies}"
PID_FILE="$ROOT/logs/pids/server_$PORT.pid"
START_SCRIPT="$ROOT/scripts/restart_$PORT.sh"
RAW="${RAW:-0}"
PROG="${API_PROG:-$0}"

case "$PORT" in
  8000) PROFILE="prod" ;;
  8001) PROFILE="experiment" ;;
  8002) PROFILE="test" ;;
  *)    PROFILE="unknown" ;;
esac

if ! command -v curl >/dev/null 2>&1; then
  echo "缺少 curl" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "缺少 jq，请先安装：brew install jq" >&2
  exit 1
fi

pretty() {
  jq . 2>/dev/null || cat
}

# /ask* 响应里的 steps 是前端调试轨迹，脚本默认不打印。
answer_view() {
  if [ "$RAW" = "1" ]; then
    pretty
  else
    jq 'del(.steps)' 2>/dev/null || cat
  fi
}

_curl() {
  local method="$1" path="$2" data="${3-}"
  local args=(
    --silent --show-error --max-time "$TIMEOUT"
    --fail-with-body
    -X "$method"
    -b "$COOKIE_JAR" -c "$COOKIE_JAR"
    "$BASE$path"
  )
  if [ -n "$data" ]; then
    args=(
      --silent --show-error --max-time "$TIMEOUT"
      --fail-with-body
      -X "$method"
      -b "$COOKIE_JAR" -c "$COOKIE_JAR"
      -H "Content-Type: application/json"
      --data-binary "$data"
      "$BASE$path"
    )
  fi
  curl "${args[@]}"
}

post_json() {
  local path="$1" data="$2"
  _curl POST "$path" "$data" | pretty
}

post_answer() {
  local path="$1" data="$2"
  _curl POST "$path" "$data" | answer_view
}

get_json() {
  _curl GET "$1" | pretty
}

question_body() {
  local question="$*"
  if [ -n "${SESSION_ID:-}" ]; then
    jq -nc --arg q "$question" --arg sid "$SESSION_ID" \
      '{question:$q, session_id:$sid, stateless:false}'
  else
    jq -nc --arg q "$question" '{question:$q, stateless:false}'
  fi
}

need_question() {
  if [ "$#" -eq 0 ] || [ -z "${1:-}" ]; then
    echo "缺少问题，例如：$PROG ${CMD:-ask} \"什么是 RAG？\"" >&2
    exit 2
  fi
}

start_port() {
  if [ ! -f "$START_SCRIPT" ]; then
    echo "找不到启动脚本：$START_SCRIPT" >&2
    exit 1
  fi
  bash "$START_SCRIPT"
}

stop_port() {
  if [ ! -f "$PID_FILE" ]; then
    echo "$PORT 未运行（无 PID 文件）"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "已停止 ${PORT}（pid $pid）"
}

status_port() {
  if curl -sf --max-time 3 "$BASE/health" >/dev/null 2>&1; then
    echo "${PORT}（${PROFILE}）healthy: $BASE"
    curl -sS --max-time 3 "$BASE/health" | pretty
  else
    echo "${PORT}（${PROFILE}）不可达: $BASE" >&2
    exit 1
  fi
}

usage() {
  cat <<EOF
ai-kb API 调用脚本
  端口: $PORT ($PROFILE)
  地址: $BASE
  会话: $COOKIE_JAR

用法: $PROG <命令> [参数]

问答:
  health                          健康检查
  ask "<问题>"                    单轮问答
  ask_smart "<问题>"              多轮问答（自动保存会话）
  ask_eval "<问题>"               问答 + 评估
  disco "<问题>"                  检索 + 话语分析
  chat "<问题>"                   OpenAI 兼容接口

会话:
  sessions [limit]                会话列表
  session <id>                    会话详情
  session-rename <id> "<标题>"    重命名
  session-reset <id>              重置
  session-delete <id>             删除
  session-export <id>             导出 Markdown

统计 / 运维:
  eval-stats                     评估统计
  eval-history [limit]           评估历史
  visits [limit]                 访问记录
  visit-stats                    访问统计
  index                          索引状态
  reindex                        重建索引
  reindex-status                 重建进度
  openapi                        OpenAPI JSON

服务 / 其他:
  smoke                          冒烟测试（会调用 LLM）
  start | stop | status          启停 $PORT
  help                           本帮助

环境变量:
  RAG_PORT=$PORT
  RAG_BASE=$BASE
  RAG_TIMEOUT=$TIMEOUT
  SESSION_ID=<id>                 指定已有会话
  RAW=1                           输出完整响应（含 steps）
EOF
}

CMD="${1:-help}"
[ "$#" -gt 0 ] && shift || true

case "$CMD" in
  health)
    status_port
    ;;

  ask)
    need_question "$@"
    post_answer /ask "$(jq -nc --arg q "$*" '{question:$q, stateless:true}')"
    ;;

  ask_smart)
    need_question "$@"
    post_answer /ask_smart "$(question_body "$*")"
    ;;

  ask_eval)
    need_question "$@"
    post_answer /ask_eval "$(jq -nc --arg q "$*" '{question:$q, stateless:true}')"
    ;;

  disco)
    need_question "$@"
    post_answer /ask_smart_disco "$(jq -nc --arg q "$*" '{question:$q, stateless:true}')"
    ;;

  chat)
    need_question "$@"
    body="$(jq -nc --arg q "$*" \
      '{messages:[{role:"user",content:$q}], stream:false, stateless:false}')"
    post_answer /v1/chat/completions "$body"
    ;;

  sessions)
    limit="${1:-20}"
    get_json "/api/sessions?limit=$limit"
    ;;

  session)
    if [ -z "${1:-}" ]; then echo "用法：$PROG session <session_id>" >&2; exit 2; fi
    get_json "/api/sessions/$1"
    ;;

  session-rename)
    if [ "$#" -lt 2 ]; then echo "用法：$PROG session-rename <session_id> <标题>" >&2; exit 2; fi
    sid="$1"; shift
    body="$(jq -nc --arg t "$*" '{title:$t}')"
    _curl PATCH "/api/sessions/$sid" "$body" | pretty
    ;;

  session-reset)
    if [ -z "${1:-}" ]; then echo "用法：$PROG session-reset <session_id>" >&2; exit 2; fi
    _curl POST "/api/sessions/$1/reset" | pretty
    ;;

  session-delete)
    if [ -z "${1:-}" ]; then echo "用法：$PROG session-delete <session_id>" >&2; exit 2; fi
    _curl DELETE "/api/sessions/$1" | pretty
    ;;

  session-export)
    if [ -z "${1:-}" ]; then echo "用法：$PROG session-export <session_id>" >&2; exit 2; fi
    _curl GET "/api/sessions/$1/export"
    ;;

  eval-stats)
    get_json /api/eval/stats
    ;;

  eval-history)
    limit="${1:-20}"
    get_json "/api/eval/history?limit=$limit"
    ;;

  visits)
    limit="${1:-20}"
    get_json "/api/visits?limit=$limit"
    ;;

  visit-stats)
    get_json /api/visit/stats
    ;;

  index)
    get_json /index_report
    ;;

  reindex)
    _curl POST /reindex | pretty
    ;;

  reindex-status)
    get_json /reindex_status
    ;;

  openapi)
    _curl GET /openapi.json | pretty
    ;;

  smoke)
    echo "== health =="
    status_port
    echo "== /ask =="
    post_answer /ask "$(jq -nc --arg q "什么是 RAG？" '{question:$q, stateless:true}')"
    echo "== /ask_smart =="
    post_answer /ask_smart "$(jq -nc --arg q "它和普通检索有什么区别？" '{question:$q, stateless:false}')"
    echo "== sessions =="
    get_json "/api/sessions?limit=5"
    ;;

  start)
    start_port
    ;;

  stop)
    stop_port
    ;;

  status)
    status_port
    ;;

  help|-h|--help)
    usage
    ;;

  *)
    echo "未知命令：$CMD" >&2
    echo "运行 $PROG help 查看用法" >&2
    exit 2
    ;;
esac
