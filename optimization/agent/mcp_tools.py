"""MCP 工具业务逻辑（kbtool CLI 复用同一套实现）。

工具清单（对应设计文档 §5.5）：
- verify_single_question — 单题快速验证（times=2 取均值，含结构化反馈）
- score_answer          — 对问答对打分
- test_rag_service      — 向 8000/8001 发测试问题
- query_qa_archive      — 查历史评分记录
- run_benchmark         — 全量 Benchmark 对比（含基线缓存）
- run_benchmark_sample  — 抽样快速验证
- get_optimization_history — 查看历史优化记录
"""
import json
import os
import random
import sys
import time
from pathlib import Path

import httpx

from infra.paths import BASE_DIR, AUTO_OPT_DIR
ROOT = BASE_DIR

from optimization.utils.scorer import score_answer as _score  # noqa: E402
from analytics.archive import query_scores  # noqa: E402

AUTO_OPT = AUTO_OPT_DIR
LOGS_DIR = os.path.join(AUTO_OPT, "logs")
BENCHMARK_DIR = os.path.join(AUTO_OPT, "benchmark")
HISTORY_FILE = os.path.join(LOGS_DIR, "optimization_history.jsonl")


# ---------- 基础 ----------

def test_rag_service(question, port=8001, timeout=120):
    """向 RAG 服务发问题，返回答案与来源。"""
    r = httpx.post(f"http://127.0.0.1:{int(port)}/ask_smart",
                   json={"question": question, "stateless": True},
                   timeout=timeout, trust_env=False)
    r.raise_for_status()
    data = r.json()
    return {"question": question, "answer": data.get("answer", ""),
            "sources": data.get("sources", []), "port": int(port)}


def score_answer(question, answer):
    return _score(question, answer)


def verify_single_question(question, port=8001, times=2):
    """单题快速验证：调用 RAG → 评分 times 次取均值。passed = avg >= 6。"""
    res = test_rag_service(question, port=int(port))
    answer = res["answer"]
    scores_raw, reasons, strengths, weaknesses = [], [], [], []
    for _ in range(max(1, int(times))):
        s = score_answer(question, answer)
        if s:
            scores_raw.append(s["score"])
            reasons.append(s["reason"])
            strengths.extend(s["strengths"])
            weaknesses.extend(s["weaknesses"])
    avg = round(sum(scores_raw) / len(scores_raw), 2) if scores_raw else 0.0
    # 去重并截断
    def _uniq(lst, n=5):
        seen, out = set(), []
        for x in lst:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
            if len(out) >= n:
                break
        return out
    return {
        "question": question,
        "answer": answer,
        "score": avg,
        "scores_raw": scores_raw,
        "reason": "; ".join(_uniq(reasons, 2)),
        "strengths": _uniq(strengths),
        "weaknesses": _uniq(weaknesses),
        "passed": avg >= 6,
        "port": int(port),
    }


def query_qa_archive(min_score=None, max_score=None, limit=50, port=None):
    return query_scores(min_score=min_score, max_score=max_score, limit=limit, port=port)


def get_optimization_history(n=10):
    rows = []
    if os.path.exists(HISTORY_FILE):
        for line in Path(HISTORY_FILE).read_text(encoding="utf-8").splitlines()[-int(n):]:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


# ---------- Benchmark ----------

def _load_benchmark_set():
    p = os.path.join(BENCHMARK_DIR, "benchmark_set.json")
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    return data.get("questions", data) if isinstance(data, dict) else data


def _load_baseline_cache():
    p = os.path.join(BENCHMARK_DIR, "baseline_cache.json")
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_baseline_cache(cache):
    Path(os.path.join(BENCHMARK_DIR, "baseline_cache.json")).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def run_benchmark(port=8000, use_cache=True):
    """全量 benchmark：与 baseline_cache 对比，输出逐题 delta 与通过判断。"""
    items = _load_benchmark_set()
    cache = _load_baseline_cache()
    results = []
    for it in items:
        q = it["question"]
        key = f"{port}:{q}"
        if use_cache and key in cache and cache[key].get("answer"):
            answer, score = cache[key]["answer"], cache[key]["score"]
            live = False
        else:
            res = test_rag_service(q, port=int(port))
            answer = res["answer"]
            s = score_answer(q, answer)
            score = s["score"] if s else None
            live = True
            if score is not None:
                cache[key] = {"answer": answer[:2000], "score": score,
                              "cached_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        base = it.get("baseline")
        delta = round(score - base, 2) if (score is not None and base is not None) else None
        results.append({"question": q, "tier": it.get("tier", ""), "score": score,
                        "baseline": base, "delta": delta, "answer": answer[:500],
                        "live": live, "passed": score is not None and score >= 7})
    _save_baseline_cache(cache)
    scores = [r["score"] for r in results if r["score"] is not None]
    avg = round(sum(scores) / len(scores), 2) if scores else None
    regressed = [r for r in results if r["delta"] is not None and r["delta"] < -2]
    return {"total": len(results), "avg": avg, "regressed": regressed,
            "results": results, "port": int(port)}


def run_benchmark_sample(n=5, port=8000):
    items = _load_benchmark_set()
    tiers = {}
    for it in items:
        tiers.setdefault(it.get("tier", ""), []).append(it)
    picked = []
    for t, arr in tiers.items():
        picked.append(random.choice(arr))  # 每档至少 1 条
    rest = [it for it in items if it not in picked]
    picked += random.sample(rest, min(len(rest), n - len(picked)))
    results = []
    for it in picked:
        res = test_rag_service(it["question"], port=int(port))
        s = score_answer(it["question"], res["answer"])
        results.append({"question": it["question"], "tier": it.get("tier", ""),
                        "score": s["score"] if s else None})
    return {"n": len(results), "results": results, "port": int(port)}
