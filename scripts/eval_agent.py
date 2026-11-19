"""Agent 配对评测：同一批任务分别跑 RAG 基线与 Agent，对比证据召回/拒答/引用/成本。

用法：
  # 0) 校验评测集里的 gold_docs 是否在磁盘与索引中
  .venv/bin/python scripts/eval_agent.py --verify-gold

  # 1) 基线（当前即可用，走 /ask_smart）
  .venv/bin/python scripts/eval_agent.py --mode baseline \
      --base http://127.0.0.1:8002 --out eval/agent_baseline.json

  # 2) 实验组（Agent 上线后，走 /agent/run）
  .venv/bin/python scripts/eval_agent.py --mode agent \
      --base http://127.0.0.1:8002 --out eval/agent_v1.json

  # 3) 配对对比
  .venv/bin/python scripts/eval_agent.py --compare \
      eval/agent_baseline.json eval/agent_v1.json

设计原则：
- 不只看 LLM 分数，优先看 gold 证据是否被检索到；
- 拒答题单独统计，防止“用常识蒙对”被算作正确；
- 所有指标都能从响应里确定性计算，不依赖人工。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SET = ROOT / "auto_optimization" / "benchmark" / "agent_set.json"
DEFAULT_INDEX = Path(os.environ.get("RAG_INDEX_PATH") or ROOT / "var" / "index" / "chunks.json")
DEFAULT_VAULT = Path(os.environ.get("RAG_VAULT_PATH") or ROOT / "vault")

ABSTAIN_MARKERS = (
    "未找到", "没有找到", "没有足够", "不包含", "没有相关", "无法回答",
    "未收录", "不确定", "未提及", "不在知识库", "知识库未覆盖", "无相关",
)
# 出现这些词说明模型给了库外答案，不应算作严格拒答。
OVERRIDE_MARKERS = ("常识", "据我所知", "公开资料", "一般认为", "公开信息")


# ---------- 评测集 ----------

def load_set(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"tasks": data}
    data.setdefault("tasks", [])
    return data


def select_tasks(tasks: list[dict], args) -> list[dict]:
    picked = tasks
    if args.type:
        types = {t.strip() for t in args.type.split(",") if t.strip()}
        picked = [t for t in picked if t.get("type") in types]
    if args.ids:
        ids = {t.strip() for t in args.ids.split(",") if t.strip()}
        picked = [t for t in picked if t.get("id") in ids]
    if args.limit:
        picked = picked[: int(args.limit)]
    return picked


def validate_set(data: dict) -> list[str]:
    problems = []
    seen = set()
    for i, task in enumerate(data.get("tasks") or []):
        tid = task.get("id")
        if not tid:
            problems.append(f"task[{i}] 缺少 id")
        elif tid in seen:
            problems.append(f"重复 id: {tid}")
        seen.add(tid)
        if not task.get("question"):
            problems.append(f"{tid}: 缺少 question")
        if task.get("type") not in (data.get("taxonomy") or {}):
            problems.append(f"{tid}: 未知 type={task.get('type')!r}")
        if not isinstance(task.get("gold_docs", []), list):
            problems.append(f"{tid}: gold_docs 必须是数组")
        if task.get("expect_abstain") and task.get("gold_docs"):
            problems.append(f"{tid}: 拒答题不应有 gold_docs")
        if not task.get("expect_abstain") and task.get("type") != "abstention" and not task.get("gold_docs"):
            problems.append(f"{tid}: 非拒答题缺少 gold_docs")
    return problems


# ---------- gold 校验 ----------

def normalize_path(path: str) -> str:
    value = (path or "").strip().replace("\\", "/")
    if "#" in value:
        value = value.split("#", 1)[0]
    while value.startswith("./"):
        value = value[2:]
    return value


def index_doc_paths(index_path: Path) -> set[str]:
    chunks = json.loads(index_path.read_text(encoding="utf-8"))
    return {normalize_path(c.get("doc_path", "")) for c in chunks if c.get("doc_path")}


# 与 ingestion/vault.py 的排除规则保持一致，用于判断“未入索引”是否正常。
INDEX_EXCLUDE_DIRS = {
    ".obsidian", ".rag", ".claude", ".git", "assets", "copilot", "schema",
    "raw/archive", "__pycache__", ".trash", "templates", "_导入报告", "attachments",
}
INDEX_EXCLUDE_PREFIXES = ("session-", "article-", "handoff")


def _indexable(rel: str) -> bool:
    if rel.startswith(INDEX_EXCLUDE_PREFIXES):
        return False
    return not any(segment in INDEX_EXCLUDE_DIRS for segment in rel.split("/"))


def verify_gold(data: dict, vault: Path, index_path: Path) -> int:
    print(f"[gold] vault={vault}")
    print(f"[gold] index={index_path}")
    if not index_path.exists():
        print(f"[gold] 索引文件不存在：{index_path}", file=sys.stderr)
        return 2
    indexed = index_doc_paths(index_path)
    print(f"[gold] 索引文档数={len(indexed)}")

    # 索引新鲜度：索引里有、磁盘上没有 = 陈旧条目；磁盘上有、索引里没有 = 漏建。
    on_disk = {
        p.relative_to(vault).as_posix()
        for p in vault.rglob("*.md")
    } if vault.exists() else set()
    stale = sorted(indexed - on_disk)
    not_indexed = sorted(p for p in (on_disk - indexed) if _indexable(p))
    print(f"[gold] 索引陈旧={len(stale)} 应索引但未收录={len(not_indexed)}")
    for path in stale[:5]:
        print(f"  [索引陈旧] {path}")
    for path in not_indexed[:5]:
        print(f"  [未收录] {path}")

    missing_disk, missing_index, ok = [], [], 0
    for task in data.get("tasks") or []:
        for doc in task.get("gold_docs") or []:
            path = normalize_path(doc)
            exists = (vault / path).exists()
            in_index = path in indexed
            if exists and in_index:
                ok += 1
                continue
            if not exists:
                missing_disk.append((task["id"], path))
            if not in_index:
                missing_index.append((task["id"], path))

    print(f"[gold] 通过={ok} 磁盘缺失={len(missing_disk)} 索引缺失={len(missing_index)}")
    for tid, path in missing_disk:
        print(f"  [磁盘缺失] {tid}: {path}")
    for tid, path in missing_index:
        print(f"  [索引缺失] {tid}: {path}")

    if stale or not_indexed:
        print("[gold] 警告：索引与 vault 不同步。评测前建议重建索引（POST /reindex），"
              "否则 gold 召回会被陈旧快照污染。")
    return 0 if not (missing_disk or missing_index) else 1


# ---------- 调用 ----------

def parse_sse_lines(text: str) -> dict | None:
    """从 SSE 文本中取最后一个 done 事件的 data。"""
    done = None
    event = ""
    for line in (text or "").splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            payload = line.split(":", 1)[1].strip()
            if event == "done":
                try:
                    done = json.loads(payload)
                except json.JSONDecodeError:
                    pass
    return done


def call_endpoint(client: httpx.Client, base: str, endpoint: str, question: str,
                  timeout: int) -> tuple[dict, int]:
    t0 = time.perf_counter()
    if endpoint.endswith("/agent/run"):
        payload = {"question": question, "stream": False}
    else:
        payload = {"question": question, "stateless": True}
    r = client.post(f"{base}{endpoint}", json=payload, timeout=timeout)
    elapsed = int((time.perf_counter() - t0) * 1000)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        data = parse_sse_lines(r.text) or {}
    else:
        data = r.json()
    data["_elapsed_ms"] = elapsed
    return data, elapsed


def response_paths(data: dict) -> set[str]:
    paths: set[str] = set()
    for source in data.get("sources") or []:
        if isinstance(source, dict):
            path = source.get("path") or source.get("doc_path")
            if path:
                paths.add(normalize_path(path))
    for ev in data.get("evidence") or []:
        if isinstance(ev, dict):
            path = ev.get("doc_path") or (ev.get("chunk") or {}).get("doc_path")
            if path:
                paths.add(normalize_path(path))
    for cit in data.get("citations_detail") or []:
        if isinstance(cit, dict) and cit.get("doc_path"):
            paths.add(normalize_path(cit["doc_path"]))
    return paths


def is_abstain(text: str) -> bool:
    """只看结论区（前 240 字）判断是否拒答。

    早期实现对全文做标记词匹配，导致「答案里某一段说未找到」被误判为整题拒答；
    同时 OVERRIDE_MARKERS 又会让明确拒答的长答案被判成未拒答。两个方向都不稳。
    结论先行是现有 prompt 的固定结构，因此只检查开头，并只对拒答题计分。
    """
    value = (text or "").strip()
    if not value:
        return True
    head = value[:240]
    if not any(marker in head for marker in ABSTAIN_MARKERS):
        return False
    # 开头就给出库外答案（而不是先说“未找到”）时，不算严格拒答。
    if any(marker in head for marker in OVERRIDE_MARKERS):
        return False
    return True


def usage_of(data: dict) -> dict:
    usage = data.get("usage") or {}
    memory = data.get("memory") or {}
    # 注意：/ask_smart 的 steps 是 trace 事件列表，不是工具步数；
    # 只有 Agent 响应里的 usage.steps 才是预算口径。
    steps = usage.get("steps") if isinstance(usage, dict) else None
    tokens = usage.get("prompt_tokens")
    if tokens is None:
        tokens = memory.get("prompt_tokens")
    return {
        "steps": steps,
        "prompt_tokens": tokens,
    }


def evaluate_task(client: httpx.Client, base: str, endpoint: str, task: dict,
                  timeout: int) -> dict:
    question = task["question"]
    try:
        data, elapsed = call_endpoint(client, base, endpoint, question, timeout)
        error = ""
    except Exception as exc:  # noqa: BLE001 - 评测脚本需要记录任何失败
        data, elapsed, error = {}, 0, str(exc)

    answer = str(data.get("answer") or "")
    paths = response_paths(data)
    gold = [normalize_path(p) for p in task.get("gold_docs") or []]
    hit = [p for p in gold if p in paths]
    recall = (len(hit) / len(gold)) if gold else None

    expect_abstain = bool(task.get("expect_abstain"))
    abstained = is_abstain(answer) if not error else False
    # 拒答准确率只对拒答题计分：非拒答题里的“未找到”通常只是某个子问题缺证据。
    if expect_abstain:
        abstain_ok = abstained if not error else False
    else:
        abstain_ok = None

    expect_contains = task.get("expect_contains") or []
    contains_ok = all(term in answer for term in expect_contains) if expect_contains else None

    citations = data.get("citations") or {}
    if isinstance(citations, dict) and "ok" in citations:
        citation_ok = bool(citations.get("ok"))
    elif task.get("must_cite"):
        # 基线 /ask_smart 不返回 citations，只能验证“有没有来源”，
        # 不能等同于引用精度；Agent 模式才校验 citations.ok。
        citation_ok = bool(data.get("evidence") or data.get("sources"))
    else:
        citation_ok = None

    usage = usage_of(data)
    trace_steps = len(data["steps"]) if isinstance(data.get("steps"), list) else None
    return {
        "id": task.get("id"),
        "type": task.get("type"),
        "question": question,
        "answer_head": answer[:300],
        "error": error,
        "latency_ms": data.get("_elapsed_ms", elapsed),
        "steps": usage["steps"],
        "trace_steps": trace_steps,
        "prompt_tokens": usage["prompt_tokens"],
        "sources_count": len(data.get("sources") or []),
        "gold_docs": gold,
        "gold_hit": hit,
        "gold_recall": round(recall, 4) if recall is not None else None,
        "full_recall": (len(hit) == len(gold)) if gold else None,
        "abstained": abstained,
        "abstain_ok": abstain_ok,
        "contains_ok": contains_ok,
        "citation_ok": citation_ok,
        "gaps": data.get("gaps") or [],
        "coverage": data.get("coverage") or [],
    }


# ---------- 聚合 ----------

def _mean(values):
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 4) if values else None


def _rate(values):
    values = [v for v in values if v is not None]
    return round(sum(1 for v in values if v) / len(values), 4) if values else None


def _percentile(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    index = int(round((len(values) - 1) * p))
    return values[index]


def aggregate(rows: list[dict]) -> dict:
    valid = [r for r in rows if not r.get("error")]
    by_type: dict[str, list[dict]] = {}
    for row in valid:
        by_type.setdefault(row.get("type") or "", []).append(row)

    def group(items):
        return {
            "tasks": len(items),
            "evidence_recall": _mean([r.get("gold_recall") for r in items]),
            "full_recall_rate": _rate([r.get("full_recall") for r in items]),
            "abstain_acc": _rate([r.get("abstain_ok") for r in items]),
            "contains_acc": _rate([r.get("contains_ok") for r in items]),
            "citation_rate": _rate([r.get("citation_ok") for r in items]),
            "avg_steps": _mean([r.get("steps") for r in items]),
            "avg_trace_steps": _mean([r.get("trace_steps") for r in items]),
            "avg_sources": _mean([r.get("sources_count") for r in items]),
            "avg_tokens": _mean([r.get("prompt_tokens") for r in items]),
            "p50_latency_ms": _percentile([r.get("latency_ms") for r in items], 0.5),
            "p90_latency_ms": _percentile([r.get("latency_ms") for r in items], 0.9),
        }

    return {
        "tasks": len(rows),
        "errors": sum(1 for r in rows if r.get("error")),
        "overall": group(valid),
        "by_type": {name: group(items) for name, items in sorted(by_type.items())},
        "rows": rows,
    }


def print_report(report: dict, label: str) -> None:
    print(f"\n=== {label} ===")
    overall = report["overall"]
    if not overall["tasks"]:
        print("没有成功样本")
        return
    print(f"tasks={report['tasks']} errors={report['errors']}")
    print(f"evidence_recall={overall['evidence_recall']} "
          f"full_recall={overall['full_recall_rate']} "
          f"abstain_acc={overall['abstain_acc']} "
          f"contains_acc={overall['contains_acc']} "
          f"citation={overall['citation_rate']}")
    print(f"steps={overall['avg_steps']} tokens={overall['avg_tokens']} "
          f"p50={overall['p50_latency_ms']}ms p90={overall['p90_latency_ms']}ms")
    print("\n按类型：")
    print(f"{'type':<14}{'n':>4}{'recall':>9}{'full':>8}{'abstain':>9}{'cite':>8}")
    for name, item in report["by_type"].items():
        print(f"{name:<14}{item['tasks']:>4}"
              f"{str(item['evidence_recall']):>9}"
              f"{str(item['full_recall_rate']):>8}"
              f"{str(item['abstain_acc']):>9}"
              f"{str(item['citation_rate']):>8}")

    failed = [r for r in report["rows"] if r.get("error") or r.get("abstain_ok") is False]
    if failed:
        print("\n需要关注：")
        for row in failed[:10]:
            reason = row.get("error") or "abstain 不符合预期"
            print(f"  - {row.get('id')} {reason}")


def compare_reports(base: dict, agent: dict) -> None:
    print("\n=== 配对对比（Agent vs 基线）===")
    cols = ("tasks", "evidence_recall", "full_recall_rate", "abstain_acc",
            "contains_acc", "citation_rate", "avg_steps", "avg_tokens",
            "p50_latency_ms")
    print(f"{'metric':<20}{'baseline':>12}{'agent':>12}{'delta':>12}")
    for key in cols:
        b = base["overall"].get(key)
        a = agent["overall"].get(key)
        delta = ""
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            delta = f"{a - b:+.4f}" if isinstance(a, float) or isinstance(b, float) else f"{a - b:+d}"
        print(f"{key:<20}{str(b):>12}{str(a):>12}{delta:>12}")

    print("\n按类型 evidence_recall：")
    print(f"{'type':<14}{'baseline':>10}{'agent':>10}{'delta':>10}")
    for name in sorted(set(base["by_type"]) | set(agent["by_type"])):
        b = (base["by_type"].get(name) or {}).get("evidence_recall")
        a = (agent["by_type"].get(name) or {}).get("evidence_recall")
        delta = ""
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            delta = f"{a - b:+.4f}"
        print(f"{name:<14}{str(b):>10}{str(a):>10}{delta:>10}")

    base_rows = {r["id"]: r for r in base.get("rows") or []}
    deltas = []
    for row in agent.get("rows") or []:
        old = base_rows.get(row.get("id"))
        if not old:
            continue
        a, b = row.get("gold_recall"), old.get("gold_recall")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            deltas.append((a - b, row.get("id"), b, a))
    if deltas:
        deltas.sort(key=lambda item: item[0])
        print("\n召回变化最大（负→正）：")
        for delta, tid, b, a in deltas[:5] + deltas[-5:]:
            print(f"  {tid}: {b} -> {a} ({delta:+.2f})")


# ---------- 主流程 ----------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Agent 配对评测")
    parser.add_argument("--set", default=str(DEFAULT_SET))
    parser.add_argument("--base", default="http://127.0.0.1:8002")
    parser.add_argument("--mode", choices=("baseline", "agent"), default="baseline")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--type", default="")
    parser.add_argument("--ids", default="")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--verify-gold", action="store_true")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "AGENT"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.compare:
        base = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        agent = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        compare_reports(base, agent)
        return 0

    set_path = Path(args.set)
    if not set_path.exists():
        print(f"评测集不存在：{set_path}", file=sys.stderr)
        return 2
    data = load_set(set_path)

    problems = validate_set(data)
    if problems:
        print("[set] 校验失败：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 2
    print(f"[set] {set_path} tasks={len(data['tasks'])}")

    if args.verify_gold:
        vault = Path(data.get("vault_root") or DEFAULT_VAULT)
        return verify_gold(data, vault, DEFAULT_INDEX)

    tasks = select_tasks(data["tasks"], args)
    print(f"[run] selected={len(tasks)} mode={args.mode} base={args.base}")

    if args.dry_run:
        for task in tasks:
            print(f"  {task['id']:<8} {task['type']:<13} {task['question']}")
        return 0

    endpoint = args.endpoint or ("/agent/run" if args.mode == "agent" else "/ask_smart")
    rows = []
    with httpx.Client(trust_env=False, follow_redirects=True) as client:
        for index, task in enumerate(tasks, 1):
            row = evaluate_task(client, args.base, endpoint, task, args.timeout)
            rows.append(row)
            if row["error"]:
                flag = "ERR"
            elif task.get("expect_abstain") and row["abstained"]:
                flag = "ABSTAIN"
            else:
                flag = "OK"
            recall = "-" if row["gold_recall"] is None else f"{row['gold_recall']:.2f}"
            print(f"[{index:>2}/{len(tasks)}] {row['id']:<8} {flag:<7} "
                  f"recall={recall} {row['latency_ms']}ms"
                  + (f" err={row['error'][:60]}" if row["error"] else ""))

    report = aggregate(rows)
    report.update({
        "mode": args.mode,
        "endpoint": endpoint,
        "base": args.base,
        "set": str(set_path),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    print_report(report, f"{args.mode} report")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[out] {out}")

    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
