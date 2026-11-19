#!/usr/bin/env bash
# ai-kb 8001（experiment profile）API 调用入口。
# 实际实现复用 scripts/api_8002.sh，仅覆盖端口与会话文件。
#
# 用法示例：
#   ./scripts/api_8001.sh health
#   ./scripts/api_8001.sh ask "什么是 RAG？"
#   ./scripts/api_8001.sh ask_smart "长征从什么时候开始？"
#   ./scripts/api_8001.sh ask_eval "平型关大捷发生在哪一年？"
#   ./scripts/api_8001.sh sessions
#   ./scripts/api_8001.sh start | stop | status
#   ./scripts/api_8001.sh help

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export RAG_PORT="${RAG_PORT:-8001}"
export RAG_COOKIE_JAR="${RAG_COOKIE_JAR:-/tmp/ai-kb-8001.cookies}"
export API_PROG="$0"

exec "$ROOT/scripts/api_8002.sh" "$@"
