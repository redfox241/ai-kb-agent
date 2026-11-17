#!/usr/bin/env python3
"""kbtool —— 知识库工具 CLI（python -m cli.kbtool）。

用法：
  kbtool verify "<问题>" [--port 8001] [--times 2]
  kbtool test "<问题>" [--port 8001]
  kbtool score --question "..." --answer "..."
  kbtool history [--n 10]
  kbtool scores [--min 0] [--max 10] [--limit 50]
  kbtool benchmark [--port 8000] [--sample 5]
"""
import argparse
import json
import sys

from optimization.agent import mcp_tools  # noqa: E402


def main():
    parser = argparse.ArgumentParser(prog="kbtool", description="知识库工具 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify", help="单题快速验证")
    p.add_argument("question")
    p.add_argument("--port", type=int, default=8001)
    p.add_argument("--times", type=int, default=2)

    p = sub.add_parser("test", help="向 RAG 发问")
    p.add_argument("question")
    p.add_argument("--port", type=int, default=8001)

    p = sub.add_parser("score", help="对问答对打分")
    p.add_argument("--question", required=True)
    p.add_argument("--answer", required=True)

    p = sub.add_parser("history", help="历史优化记录")
    p.add_argument("--n", type=int, default=10)

    p = sub.add_parser("scores", help="历史评分")
    p.add_argument("--min", type=float)
    p.add_argument("--max", type=float)
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("benchmark", help="全量/抽样 benchmark")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--sample", type=int)

    args = parser.parse_args()

    try:
        if args.cmd == "verify":
            out = mcp_tools.verify_single_question(args.question, port=args.port, times=args.times)
        elif args.cmd == "test":
            out = mcp_tools.test_rag_service(args.question, port=args.port)
        elif args.cmd == "score":
            out = mcp_tools.score_answer(args.question, args.answer)
        elif args.cmd == "history":
            out = mcp_tools.get_optimization_history(args.n)
        elif args.cmd == "scores":
            out = mcp_tools.query_qa_archive(min_score=args.min, max_score=args.max, limit=args.limit)
        elif args.cmd == "benchmark":
            out = (mcp_tools.run_benchmark_sample(args.sample, port=args.port)
                   if args.sample else mcp_tools.run_benchmark(port=args.port))
        else:
            parser.error(f"未知命令: {args.cmd}")
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
