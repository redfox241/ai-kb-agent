#!/usr/bin/env python3
"""回填 benchmark 基线：对 8000 跑完整验证集，写入 baseline 与 baseline_cache。"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from optimization.agent import mcp_tools  # noqa: E402

SET_FILE = os.path.join(ROOT, "auto_optimization", "benchmark", "benchmark_set.json")


def main():
    data = json.loads(Path(SET_FILE).read_text(encoding="utf-8"))
    items = data.get("questions", data)
    cache = {}
    for i, it in enumerate(items):
        q = it["question"]
        res = mcp_tools.test_rag_service(q, port=8000)
        s = mcp_tools.score_answer(q, res["answer"])
        score = s["score"] if s else None
        it["baseline"] = score
        cache[f"8000:{q}"] = {"answer": res["answer"][:2000], "score": score,
                              "cached_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        print(f"[{i+1}/{len(items)}] {q[:40]} -> {score}", flush=True)
    Path(SET_FILE).write_text(json.dumps({"version": "v1", "questions": items},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
    Path(os.path.join(ROOT, "auto_optimization", "benchmark", "baseline_cache.json")).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print("baseline 已回填")


if __name__ == "__main__":
    main()
