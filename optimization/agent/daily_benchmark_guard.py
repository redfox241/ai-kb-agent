#!/usr/bin/env python3
"""每晚全量守护（"做梦"机制，对应设计 §5.12）。

- 跑完整 benchmark（8000 实时评分，不用缓存）
- 对比基线（benchmark_set.json 的 baseline）
- 平均分 >= 基线 且无单题回退>3 → 刷新基线，确认当日优化
- 轻微下降(0.3-1) → 告警不回滚
- 明显下降(>1) 或单题回退>3 → 回滚当日首个稳定版本 + 待重新验证
"""
import json
import os
import sys
import time
from pathlib import Path

from infra.paths import BASE_DIR, AUTO_OPT_DIR
ROOT = BASE_DIR

from optimization.agent import mcp_tools  # noqa: E402
from optimization.utils.scorer import score_answer  # noqa: E402
from optimization.utils import overrides as ov  # noqa: E402

LOGS = os.path.join(AUTO_OPT_DIR, "logs")
AGENT_LOGS = os.path.join(LOGS, "agent")
BENCH = os.path.join(AUTO_OPT_DIR, "benchmark")
SET_FILE = os.path.join(BENCH, "benchmark_set.json")
GUARD_LOG = os.path.join(AGENT_LOGS, "guard.log")


def log(msg):
    Path(AGENT_LOGS).mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] [guard] {msg}"
    with open(GUARD_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def load_set():
    data = json.loads(Path(SET_FILE).read_text(encoding="utf-8"))
    return data.get("questions", data)


def save_set(items):
    Path(SET_FILE).write_text(json.dumps({"version": "v1", "questions": items},
                                         ensure_ascii=False, indent=2), encoding="utf-8")


def rollback_to_first_stable():
    """回滚到当日第一个 sync 前的生产 overrides 备份。"""
    import shutil
    bak_dir = os.path.join(ROOT, "backups")
    if not os.path.isdir(bak_dir):
        return False
    files = sorted(f for f in os.listdir(bak_dir) if f.startswith("overrides.prod.before_sync."))
    if not files:
        log("无历史备份，跳过回滚")
        return False
    first = os.path.join(bak_dir, files[0])
    shutil.copy(first, ov.PROD_OVERRIDES_FILE)
    log(f"回滚到备份: {files[0]}")
    import subprocess
    subprocess.run(["bash", os.path.join(ROOT, "scripts", "restart_8000.sh")],
                   capture_output=True, text=True, timeout=180)
    return True


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="daily_benchmark_guard",
                                 description="每晚全量 benchmark 守护：检测回退并回滚")
    ap.add_argument("--check", action="store_true",
                    help="只读模式：跑全量 benchmark 输出报告，不刷新基线、不回滚")
    args = ap.parse_args()

    items = load_set()
    results = []
    baseline_avg, current_avg = None, None
    for it in items:
        q = it["question"]
        try:
            res = mcp_tools.test_rag_service(q, port=8000)
            s = score_answer(q, res["answer"])
            score = s["score"] if s else None
        except Exception as e:
            log(f"评分失败 {q}: {e}")
            score = None
        old = it.get("baseline")
        delta = round(score - old, 2) if (score is not None and old is not None) else None
        results.append({"question": q, "tier": it.get("tier"), "old": old, "new": score, "delta": delta})
        log(f"  [{q}] old={old} new={score} delta={delta}")

    scores = [r["new"] for r in results if r["new"] is not None]
    bases = [r["old"] for r in results if r["old"] is not None]
    current_avg = round(sum(scores) / len(scores), 2) if scores else None
    baseline_avg = round(sum(bases) / len(bases), 2) if bases else None

    regressed = [r for r in results if r["delta"] is not None and r["delta"] < -3]
    drop = (baseline_avg - current_avg) if (baseline_avg is not None and current_avg is not None) else 0

    log(f"基线平均 {baseline_avg} -> 当前平均 {current_avg} (下降 {round(drop,2)}) 单题回退>3: {len(regressed)} 条")

    if args.check:
        log("== --check 只读模式：仅报告，不刷新基线/不回滚 ==")
        return

    if regressed or drop > 1:
        log("检测到明显回退，回滚到当日首个稳定版本")
        if rollback_to_first_stable():
            # 标记当日优化为待重新验证
            pending = {"rolled_back_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "questions": [r["question"] for r in regressed]}
            Path(os.path.join(LOGS, "pending_revalidation.json")).write_text(
                json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
            log("已写入 pending_revalidation，次日巡检优先验证")
    elif 0.3 <= drop <= 1:
        log("轻微下降，告警不回滚（供人工判断）")
    else:
        # 确认，刷新基线
        for it, r in zip(items, results):
            if r["new"] is not None:
                it["baseline"] = r["new"]
        save_set(items)
        # 刷新 baseline_cache
        cache = {}
        for r in results:
            if r["new"] is not None:
                cache[f"8000:{r['question']}"] = {"answer": "", "score": r["new"],
                                                  "cached_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        Path(os.path.join(BENCH, "baseline_cache.json")).write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        log("全量守护通过，已刷新基线")
    log("==== guard 结束 ====")


if __name__ == "__main__":
    main()
