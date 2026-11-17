"""多轮评测：按会话执行多轮用例，计算改写/引用/拒答/延迟指标，支持基线对比门禁。

用法：
  python scripts/eval_multiturn.py --base http://127.0.0.1:8002 \
      --set auto_optimization/benchmark/multiturn_set.json --out eval_mt.json

  python scripts/eval_multiturn.py --compare baseline.json eval_mt.json --max-drop 5

指标定义见 docs/multi-turn-design.md §11.2。
"""
import argparse
import json
import os
import statistics
import sys
import time
import uuid

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SET = os.path.join(ROOT, "auto_optimization", "benchmark", "multiturn_set.json")

REFUSAL_MARKERS = ("未找到", "没有足够", "不包含", "没有相关", "无法回答", "未收录", "不确定")


def load_set(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def contains_all(text, needles):
    text = text or ""
    return all(n in text for n in (needles or []))


def ask(client, base, question, session_id, timeout=180):
    t0 = time.time()
    r = client.post(f"{base}/ask_smart",
                    json={"question": question, "session_id": session_id},
                    timeout=timeout)
    elapsed = int((time.time() - t0) * 1000)
    r.raise_for_status()
    data = r.json()
    data["_elapsed_ms"] = elapsed
    return data


def eval_conversation(client, base, conv):
    sid = f"eval_{conv.get('id', 'x')}_{uuid.uuid4().hex[:6]}"
    turns = []
    for idx, turn in enumerate(conv.get("turns") or []):
        q = turn["q"]
        try:
            d = ask(client, base, q, sid)
            err = ""
        except Exception as e:
            d, err = {}, str(e)
        resolved = d.get("resolved_query") or q
        answer = d.get("answer") or ""
        checks = {}
        if not err:
            if turn.get("expect_resolved_contains"):
                checks["rewrite"] = contains_all(resolved, turn["expect_resolved_contains"])
            elif idx == 0:
                checks["rewrite"] = resolved.strip() == q.strip()
            if turn.get("expect_rewrite") is not None:
                changed = resolved.strip() != q.strip()
                checks["rewrite"] = (changed == bool(turn["expect_rewrite"]))
            if turn.get("expect_answer_contains"):
                checks["answer"] = contains_all(answer, turn["expect_answer_contains"])
            if turn.get("expect_docs"):
                paths = " ".join(s.get("path", "") for s in d.get("sources") or [])
                checks["recall"] = contains_all(paths, turn["expect_docs"])
            if turn.get("expect_refusal"):
                checks["refusal"] = any(m in answer for m in REFUSAL_MARKERS)
            cit = d.get("citations") or {}
            checks["citation"] = bool(cit.get("ok")) or bool(turn.get("expect_refusal"))
        turns.append({
            "idx": idx,
            "question": q,
            "resolved_query": resolved,
            "intent": d.get("intent"),
            "confidence": d.get("confidence"),
            "answer_head": answer[:200],
            "latency_ms": d.get("latency_ms") or d.get("_elapsed_ms"),
            "prompt_tokens": (d.get("memory") or {}).get("prompt_tokens"),
            "citations": d.get("citations"),
            "flags": d.get("flags") or {},
            "checks": checks,
            "error": err,
        })
    return {"id": conv.get("id"), "category": conv.get("category"),
            "session_id": sid, "turns": turns}


def _rate(items, key):
    vals = [t["checks"][key] for t in items if key in (t.get("checks") or {})]
    if not vals:
        return None
    return round(sum(1 for v in vals if v) / len(vals), 4)


def _percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    k = int(round((len(values) - 1) * p))
    return values[k]


def aggregate(report):
    turns = [t for c in report["conversations"] for t in c["turns"]]
    lat = [t["latency_ms"] for t in turns if t.get("latency_ms")]
    toks = [t["prompt_tokens"] for t in turns if t.get("prompt_tokens")]
    metrics = {
        "turns": len(turns),
        "errors": sum(1 for t in turns if t.get("error")),
        "rewrite_acc": _rate(turns, "rewrite"),
        "citation_acc": _rate(turns, "citation"),
        "refusal_acc": _rate(turns, "refusal"),
        "recall5": _rate(turns, "recall"),
        "answer_acc": _rate(turns, "answer"),
        "avg_latency_ms": round(statistics.mean(lat)) if lat else None,
        "p95_latency_ms": _percentile(lat, 0.95),
        "avg_prompt_tokens": round(statistics.mean(toks)) if toks else None,
        "rewrite_failed": sum(1 for t in turns if (t.get("flags") or {}).get("rewrite_failed")
                              or t.get("confidence") == 0.4),
        "no_evidence": sum(1 for t in turns if (t.get("flags") or {}).get("no_evidence")),
    }
    by_cat = {}
    for c in report["conversations"]:
        by_cat.setdefault(c["category"], []).extend(c["turns"])
    metrics["by_category"] = {
        k: {"turns": len(v), "rewrite_acc": _rate(v, "rewrite"),
            "citation_acc": _rate(v, "citation")}
        for k, v in by_cat.items()}
    return metrics


def run(base, set_path, out_path, limit=None, category=None):
    data = load_set(set_path)
    convs = data.get("conversations") or []
    if category:
        convs = [c for c in convs if c.get("category") == category]
    if limit:
        convs = convs[:limit]
    report = {"version": data.get("version"), "base": base,
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "conversations": []}
    with httpx.Client(trust_env=False) as client:
        for i, conv in enumerate(convs, 1):
            print(f"[{i}/{len(convs)}] {conv.get('id')} {conv.get('category')}", flush=True)
            report["conversations"].append(eval_conversation(client, base, conv))
    report["metrics"] = aggregate(report)
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"报告已写入 {out_path}")
    return report


def compare(old_path, new_path, max_drop=5.0, max_latency_up=50.0):
    old = load_set(old_path)["metrics"]
    new = load_set(new_path)["metrics"]
    keys = ["rewrite_acc", "citation_acc", "refusal_acc", "recall5", "answer_acc"]
    failed = []
    rows = []
    for k in keys:
        o, n = old.get(k), new.get(k)
        if o is None or n is None:
            rows.append((k, o, n, None, "skip"))
            continue
        delta = round((n - o) * 100, 2)
        ok = delta >= -max_drop
        rows.append((k, o, n, delta, "ok" if ok else "FAIL"))
        if not ok:
            failed.append(k)
    ol, nl = old.get("p95_latency_ms"), new.get("p95_latency_ms")
    lat_delta = None
    if ol and nl:
        lat_delta = round((nl - ol) / ol * 100, 1)
        if lat_delta > max_latency_up:
            failed.append("p95_latency")
    print(f"{'metric':<16}{'old':>10}{'new':>10}{'delta':>10}  status")
    for k, o, n, d, st in rows:
        print(f"{k:<16}{str(o):>10}{str(n):>10}{str(d):>10}  {st}")
    if lat_delta is not None:
        print(f"{'p95_latency':<16}{ol:>10}{nl:>10}{lat_delta:>9}%  "
              f"{'ok' if 'p95_latency' not in failed else 'FAIL'}")
    if failed:
        print("门禁未通过：" + ", ".join(failed))
        return 1
    print("门禁通过")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8002")
    ap.add_argument("--set", default=DEFAULT_SET)
    ap.add_argument("--out", default="eval_mt.json")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--category")
    ap.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"))
    ap.add_argument("--max-drop", type=float, default=5.0)
    ap.add_argument("--max-latency-up", type=float, default=50.0)
    args = ap.parse_args()
    if args.compare:
        sys.exit(compare(args.compare[0], args.compare[1],
                         max_drop=args.max_drop, max_latency_up=args.max_latency_up))
    run(args.base, args.set, args.out, limit=args.limit, category=args.category)


if __name__ == "__main__":
    main()
