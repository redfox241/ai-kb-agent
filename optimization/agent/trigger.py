#!/usr/bin/env python3
"""trigger.py —— 自迭代系统调度与验收（对应设计文档 §5.3）。

命令：
  --incident <file>         处理一个低分事件（评分后触发）
  --process-queue           处理积压队列（单题/批量）
  --daily-patrol            每日巡检
  --list-failures           列出待人工处理失败
  --mark-resolved <hash>    标记已解决（抑制缩短至1天）
  --mark-wontfix <hash>     标记无需处理
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from infra.paths import BASE_DIR, AUTO_OPT_DIR
ROOT = BASE_DIR

from infra.llm import LLMClient  # noqa: E402

from optimization.utils import overrides as ov  # noqa: E402
from optimization.utils.scorer import score_answer  # noqa: E402

LOGS = os.path.join(AUTO_OPT_DIR, "logs")
AGENT_LOGS = os.path.join(LOGS, "agent")
BENCH = os.path.join(AUTO_OPT_DIR, "benchmark")
HISTORY_FILE = os.path.join(LOGS, "optimization_history.jsonl")
FAILURES_FILE = os.path.join(LOGS, "optimization_failures.jsonl")
DAILY_FILE = os.path.join(LOGS, "daily_optimizations.jsonl")
SUPPRESS_FILE = os.path.join(LOGS, "suppressed_questions.json")
QUEUE_FILE = os.path.join(LOGS, "pending_queue.json")
LOCK_FILE = os.path.join(LOGS, ".running_lock")
TRIGGERS_LOCK = os.path.join(LOGS, ".triggers_lock")
PENDING_REVALIDATION = os.path.join(LOGS, "pending_revalidation.json")
AGENT_BAK = os.path.join(AGENT_LOGS, ".agent_bak")

SERVER_AUTO = os.path.join(ROOT, "server_auto.py")
SERVER_FILE = os.path.join(ROOT, "server.py")
BACKUP_DIR = os.path.join(ROOT, "backups")


# ---------- 基础工具 ----------

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def md5(q):
    return hashlib.md5(q.strip().lower().encode("utf-8")).hexdigest()


def normalize_question(q):
    q = re.sub(r"^[^\w一-鿿]+", "", q)
    q = re.sub(r"\s+", " ", q).strip().lower()
    return q


def append_jsonl(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def log(msg, tag="trigger"):
    Path(AGENT_LOGS).mkdir(parents=True, exist_ok=True)
    line = f"[{now_iso()}] [{tag}] {msg}"
    with open(os.path.join(AGENT_LOGS, "run.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def load_config():
    import yaml
    cfg_path = os.path.join(AUTO_OPT_DIR, "config.yaml")
    try:
        return yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}
    except Exception:
        return {}


# ---------- 抑制名单 ----------

def _is_suppressed(question, config):
    h = md5(normalize_question(question))
    data = load_json(SUPPRESS_FILE, {})
    now = time.time()
    cleaned = {}
    hit = False
    for k, v in data.items():
        try:
            exp = time.mktime(time.strptime(v.get("expires_at", ""), "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            exp = 0
        if exp < now:
            continue  # 过期清理
        cleaned[k] = v
        if k == h:
            hit = True
    if cleaned != data:
        save_json(SUPPRESS_FILE, cleaned)
    return hit, h, data.get(h, {})


def suppress(question, days, reason, note=""):
    h = md5(normalize_question(question))
    data = load_json(SUPPRESS_FILE, {})
    exp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + days * 86400))
    data[h] = {"question": question, "reason": reason, "suppressed_at": now_iso(),
               "expires_at": exp, "note": note}
    save_json(SUPPRESS_FILE, data)
    return h


# ---------- 去重 & 意图聚合 ----------

def _extract_keywords(q, config):
    stop = set(config.get("stopwords", []))
    kws = []
    text = normalize_question(q)
    try:
        import jieba
        tokens = jieba.cut(text)
    except Exception:
        from retrieval.bm25 import tokenize
        tokens = tokenize(text)
    for t in tokens:
        t = t.strip().lower()
        if not t or len(t) < 2 or t in stop or t.isspace():
            continue
        if t not in kws:
            kws.append(t)
    return kws


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def find_similar_intent(question, queue, config):
    """同意图检测：返回已有条目下标与核心意图词；不同业务区分词强制不聚合。"""
    kws = _extract_keywords(question, config)
    domain = set(str(w).lower() for w in config.get("domain_words", []))
    q_domains = set(kws) & domain
    for i, item in enumerate(queue):
        other_kws = set(item.get("keywords", []))
        if q_domains and other_kws & domain:
            if q_domains != (other_kws & domain):
                continue  # 业务区分词不同 → 不聚合
        sim = _jaccard(kws, other_kws)
        th = float(config.get("dedup", {}).get("intent_jaccard_threshold", 0.6))
        contained = kws and other_kws and (set(kws) <= set(other_kws) or set(other_kws) <= set(kws))
        if sim >= th or contained:
            return i
    return None


# ---------- 队列 ----------

def load_queue():
    return load_json(QUEUE_FILE, [])


def save_queue(q):
    save_json(QUEUE_FILE, q)


def enqueue(incident, config):
    """去重 → 聚合 → 入队。返回 (action, entry)。"""
    h = md5(normalize_question(incident["question"]))
    # 6h 冷却
    if os.path.exists(HISTORY_FILE):
        for line in Path(HISTORY_FILE).read_text(encoding="utf-8").splitlines()[-100:]:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("question_hash") == h:
                try:
                    t = time.mktime(time.strptime(r.get("ts", ""), "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    continue
                if time.time() - t < 6 * 3600:
                    return "dedup", None
    queue = load_queue()
    # 同意图聚合
    idx = find_similar_intent(incident["question"], queue, config)
    if idx is not None:
        item = queue[idx]
        variants = item.get("variants", [item["question"]])
        if incident["question"] not in variants and len(variants) < config.get("dedup", {}).get("max_variants", 5):
            variants.append(incident["question"])
        item["variants"] = variants
        item["keywords"] = _extract_keywords(item["question"], config)
        item["priority"] = calculate_priority(item, config)
        if incident.get("score", 10) < item.get("score", 10):
            item["score"] = incident.get("score", item.get("score", 10))
        item["enqueued_at"] = now_iso()
        save_queue(queue)
        return "aggregated", item
    # 新条目
    entry = {
        "question": incident["question"],
        "variants": [incident["question"]],
        "score": incident.get("score", 0),
        "reason": incident.get("reason", ""),
        "answer": incident.get("answer", ""),
        "triggered_at": incident.get("triggered_at", now_iso()),
        "enqueued_at": now_iso(),
        "affected_users": incident.get("affected_users") or [incident.get("user_id", "local")],
        "keywords": _extract_keywords(incident["question"], config),
        "related_count": 0,
    }
    entry["priority"] = calculate_priority(entry, config)
    queue.append(entry)
    save_queue(queue)
    return "enqueued", entry


def calculate_priority(entry, config):
    """priority = score - (affected_users-1)*0.5 - related_count*0.3；越低越优先。"""
    cfg = config.get("priority", {})
    n_users = len(entry.get("affected_users", []) or [])
    score = entry.get("score", 10)
    return score - (n_users - 1) * cfg.get("affected_user_weight", 0.5) \
        - entry.get("related_count", 0) * cfg.get("related_weight", 0.3)


def should_batch(queue):
    """满足任一条件走批量：≥3题 / ≥2同主题 / 首条>10min且≥2条。"""
    if len(queue) >= 3:
        return True
    if len(queue) >= 2:
        domains = [set(e.get("keywords", [])) for e in queue]
        for i in range(len(domains)):
            for j in range(i + 1, len(domains)):
                if domains[i] & domains[j]:
                    return True
        try:
            first_ts = time.mktime(time.strptime(queue[0]["enqueued_at"], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            first_ts = time.time()
        if len(queue) >= 2 and time.time() - first_ts > 600:
            return True
    return False


# ---------- Agent 运行 ----------

def run_agent(entry, config, is_batch=False):
    from optimization.agent import optimizer_agent

    # 备份 server_auto 与 overrides
    Path(AGENT_LOGS).mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy(SERVER_AUTO, AGENT_BAK)
    except FileNotFoundError:
        pass
    snap = ov.snapshot_overrides()
    log(f"调用 Agent 优化: {entry['question']} (batch={is_batch})")
    incidents = [{"question": entry["question"], "answer": entry.get("answer", ""),
                  "score": entry.get("score", 0), "reason": entry.get("reason", "")}]
    result, score_history = optimizer_agent.run_agent(incidents[0], port=8001, log=log)
    return result, score_history, snap


# ---------- 回归验证 ----------

def _probe_questions(config, mode_a=True):
    lite = load_json(os.path.join(BENCH, "lite_set.json"), {})
    core = lite.get("core", [])
    probes = list(core)
    return probes


def _baseline_for(q):
    bset = load_json(os.path.join(BENCH, "benchmark_set.json"), {})
    for it in bset.get("questions", []):
        if it["question"] == q and it.get("baseline") is not None:
            return it["baseline"]
    cache = load_json(os.path.join(BENCH, "baseline_cache.json"), {})
    return cache.get(f"8000:{q}", {}).get("score")


def run_regression(trigger_q, config):
    """轻量回归：核心子集 + 随机探针，8001 新分 vs 基线。"""
    import random
    from optimization.agent import mcp_tools
    probes = _probe_questions(config)
    items = load_json(os.path.join(BENCH, "benchmark_set.json"), {}).get("questions", [])
    others = [it["question"] for it in items if it["question"] not in probes]
    n_probe = load_json(os.path.join(BENCH, "lite_set.json"), {}).get("random_probes", 2)
    probes += random.sample(others, min(n_probe, len(others)))

    results = []
    for q in probes:
        old = _baseline_for(q)
        try:
            res = mcp_tools.verify_single_question(q, port=8001, times=1)
            new = res["score"]
        except Exception as e:
            log(f"回归题评分失败 {q}: {e}")
            continue
        delta = round(new - old, 2) if old is not None else None
        results.append({"question": q, "old": old, "new": new, "delta": delta})
        log(f"回归 [{q}] old={old} new={new} delta={delta}")

    with_delta = [r for r in results if r["delta"] is not None]
    passed = True
    if with_delta:
        worst = min(r["delta"] for r in with_delta)
        avg = sum(r["delta"] for r in with_delta) / len(with_delta)
        passed = worst > -2 and avg >= -0.5
        log(f"回归判定: worst={worst} avg={round(avg,2)} passed={passed}")
    return {"passed": passed, "results": results}


# ---------- 同步 / 回滚 ----------

def sync_to_production(trigger_q):
    """备份 8000 → 同步 overrides → 重启 → 健康检查 → 验证触发题。失败回滚。"""
    import subprocess
    ts = time.strftime("%Y%m%dT%H%M%S")
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy(SERVER_FILE, os.path.join(BACKUP_DIR, f"server.py.before_sync.{ts}"))
    except FileNotFoundError:
        pass
    try:
        shutil.copy(os.path.join(AUTO_OPT_DIR, "overrides.prod.json"),
                    os.path.join(BACKUP_DIR, f"overrides.prod.before_sync.{ts}"))
    except FileNotFoundError:
        pass
    ov.sync_to_production()
    log("同步 overrides -> 生产，重启 8000")
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "restart_8000.sh")],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        log(f"8000 重启失败: {r.stderr[-300:]}", tag="sync")
        return False, ts
    # 验证触发问题在 8000 的回答
    try:
        res = test_8000(trigger_q)
        s = score_answer(trigger_q, res.get("answer", ""))
        log(f"同步后 8000 验证触发题得分: {s['score'] if s else 'N/A'}")
        if s and s["score"] >= 6:
            return True, ts
        # 得分回退到 <6 → 回滚
        log("同步后触发题验证 <6，回滚 8000", tag="sync")
        rollback_production(ts)
        return False, ts
    except Exception as e:
        log(f"同步后验证异常: {e}，回滚", tag="sync")
        rollback_production(ts)
        return False, ts


def test_8000(question):
    import httpx
    r = httpx.post("http://127.0.0.1:8000/ask_smart",
                   json={"question": question, "stateless": True}, timeout=120,
                   trust_env=False)
    r.raise_for_status()
    return r.json()


def rollback_production(ts=None):
    """从备份恢复 8000 生产 overrides 并重启。"""
    import subprocess
    Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    if ts:
        src = os.path.join(BACKUP_DIR, f"overrides.prod.before_sync.{ts}")
        if os.path.exists(src):
            shutil.copy(src, os.path.join(AUTO_OPT_DIR, "overrides.prod.json"))
    ov.save_overrides(load_json(os.path.join(AUTO_OPT_DIR, "overrides.prod.json"), {}),
                      path=os.path.join(AUTO_OPT_DIR, "overrides.prod.json"))
    subprocess.run(["bash", os.path.join(ROOT, "scripts", "restart_8000.sh")],
                   capture_output=True, text=True, timeout=180)


def rollback_agent(snap):
    """恢复试验 overrides 到 Agent 运行前。"""
    ov.rollback_overrides(snap)
    import subprocess
    subprocess.run(["bash", os.path.join(ROOT, "scripts", "restart_8001.sh")],
                   capture_output=True, text=True, timeout=180)


def invalidate_baseline(q):
    cache = load_json(os.path.join(BENCH, "baseline_cache.json"), {})
    cache.pop(f"8000:{q}", None)
    save_json(os.path.join(BENCH, "baseline_cache.json"), cache)


# ---------- 事件处理 ----------

def handle_agent_result(entry, result, score_history, snap, config):
    status = result.get("status")
    q = entry["question"]
    log(f"Agent 结果: status={status} final_score={result.get('final_score')}")

    if status == "give_up":
        rollback_agent(snap)
        suppress(q, config.get("suppression", {}).get("give_up_days", 30), "give_up")
        record_failure(entry, result, "optimization_failed")
        append_jsonl(HISTORY_FILE, _history_entry(entry, result, status, synced=False))
        return

    if status == "knowledge_gap":
        rollback_agent(snap)
        suppress(q, config.get("suppression", {}).get("knowledge_gap_days", 30), "knowledge_gap")
        record_failure(entry, result, "knowledge_gap")
        append_jsonl(HISTORY_FILE, _history_entry(entry, result, status, synced=False))
        return

    # success → 回归验证
    final = result.get("final_score", 0)
    reg = run_regression(q, config)
    if not reg["passed"]:
        rollback_agent(snap)
        suppress(q, config.get("suppression", {}).get("regression_failed_days", 7),
                 "regression_failed", note=json.dumps(reg["results"], ensure_ascii=False)[:500])
        record_failure(entry, result, "regression_failed")
        append_jsonl(HISTORY_FILE, _history_entry(entry, result, "regression_failed", synced=False))
        return

    # 通过 → 同步
    ok, ts = sync_to_production(q)
    days = config.get("suppression", {}).get("edge_days", 3) if final < 7 \
        else config.get("suppression", {}).get("success_days", 1)
    if ok:
        invalidate_baseline(q)
        append_jsonl(DAILY_FILE, {"ts": now_iso(), "question": q, "final_score": final,
                                  "synced": True, "changes": result.get("changes_summary", "")})
        suppress(q, days, "recently_optimized")
        append_jsonl(HISTORY_FILE, _history_entry(entry, result, "success", synced=True))
        log(f"优化成功并同步线上: {q} score={final}")
    else:
        rollback_agent(snap)
        suppress(q, config.get("suppression", {}).get("regression_failed_days", 7), "regression_failed")
        record_failure(entry, result, "regression_failed")
        append_jsonl(HISTORY_FILE, _history_entry(entry, result, "regression_failed", synced=False))


def _history_entry(entry, result, status, synced):
    return {
        "ts": now_iso(),
        "question": entry["question"],
        "question_hash": md5(normalize_question(entry["question"])),
        "status": status,
        "final_score": result.get("final_score"),
        "attempts": result.get("attempts"),
        "score_history": result.get("score_history", []),
        "changes_summary": result.get("changes_summary", ""),
        "root_cause": result.get("root_cause", ""),
        "suggestion": result.get("suggestion", ""),
        "synced": synced,
    }


def record_failure(entry, result, failure_type):
    h = md5(normalize_question(entry["question"]))
    supp = load_json(SUPPRESS_FILE, {}).get(h, {})
    rec = {
        "timestamp": now_iso(),
        "question": entry["question"],
        "question_hash": h,
        "original_score": entry.get("score", 0),
        "failure_type": failure_type,
        "status": "pending_review",
        "score_history": result.get("score_history", []),
        "agent_diagnosis": result.get("root_cause", ""),
        "suggested_action": result.get("suggestion", ""),
        "suppressed_until": supp.get("expires_at", ""),
    }
    append_jsonl(FAILURES_FILE, rec)
    log(f"失败记录: {failure_type} | {entry['question']}")


# ---------- 调度 ----------

def _acquire_lock():
    Path(LOGS).mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        return None


def _release_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass


def process_incident(incident, config):
    """处理一个低分事件。"""
    q = incident["question"]
    # Layer 1 抑制
    hit, h, rec = _is_suppressed(q, config)
    if hit:
        log(f"抑制命中，跳过: {q} reason={rec.get('reason')}")
        return "suppressed"
    # 去重/聚合/入队
    action, entry = enqueue(incident, config)
    log(f"入队动作: {action}")
    if action == "dedup":
        return "dedup"
    # 抢占锁
    fd = _acquire_lock()
    if fd is None:
        log("有 Agent 在运行，事件已入队，等待 --process-queue")
        return "queued"
    try:
        drain_queue(config)
    finally:
        _release_lock(fd)
    return "processed"


def drain_queue(config):
    """处理积压队列（单题或批量）。"""
    queue = load_queue()
    if not queue:
        log("队列为空")
        return
    queue.sort(key=lambda e: e.get("priority", 99))
    batch = should_batch(queue)
    if batch:
        log(f"批量模式：处理 {len(queue)} 题")
        for entry in queue[:]:
            process_one(entry, config)
        save_queue([])
    else:
        entry = queue.pop(0)
        save_queue(queue)
        process_one(entry, config)


def process_one(entry, config):
    """运行 Agent 并处理结果。"""
    result, score_history, snap = run_agent(entry, config)
    handle_agent_result(entry, result, score_history, snap, config)


# ---------- 巡检 / 失败管理 ----------

def _today_trigger_count():
    """今日 Agent 触发次数（按 optimization_history 今日记录统计）。"""
    today = time.strftime("%Y-%m-%d")
    cnt = 0
    if os.path.exists(HISTORY_FILE):
        for line in Path(HISTORY_FILE).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("ts", "").startswith(today):
                    cnt += 1
            except Exception:
                continue
    return cnt


def _exceed_daily_limit(config):
    limit = int(config.get("dedup", {}).get("max_daily_triggers", 8))
    return _today_trigger_count() >= limit


def daily_patrol(config):
    """每日巡检：扫描 SQLite 低分评估记录入队，并输出待处理清单。"""
    _ensure_analytics()
    log("==== 每日巡检 ====")
    from analytics.repository import query_low
    low = query_low(min_total=6, limit=30)  # 总分≤6 的低分评估
    log(f"低分评估记录(≤6): {len(low)} 条")
    for s in low[:10]:
        log(f"  低分: [{s['total']}] {s['question'][:60]}")
        incident = {
            "question": s["question"],
            "score": s.get("total", 0),
            "reason": s.get("reason", ""),
            "answer": s.get("answer", "") or "",
            "user_id": "daily_patrol",
        }
        action, _ = enqueue(incident, config)
        log(f"    入队动作: {action}")
    # 待人工处理失败
    pending = []
    if os.path.exists(FAILURES_FILE):
        for line in Path(FAILURES_FILE).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("status") == "pending_review":
                pending.append(r)
    log(f"待人工处理: {len(pending)} 条")
    for r in pending[:10]:
        log(f"  [待处理] {r['failure_type']} | {r['question'][:60]} | {r['question_hash'][:8]}")
    # 待重新验证
    pv = load_json(PENDING_REVALIDATION, {})
    if pv.get("questions"):
        log(f"待重新验证: {len(pv['questions'])} 条")
    log("==== 巡检结束 ====")
    return {"low": len(low), "pending_review": len(pending)}


def list_failures():
    rows = []
    if os.path.exists(FAILURES_FILE):
        for line in Path(FAILURES_FILE).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("status") == "pending_review":
                rows.append(r)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return rows


def mark_failure(qhash, status):
    if not os.path.exists(FAILURES_FILE):
        return
    lines = Path(FAILURES_FILE).read_text(encoding="utf-8").splitlines()
    out = []
    target = None
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            out.append(line)
            continue
        if r.get("question_hash") == qhash and r.get("status") == "pending_review":
            r["status"] = status
            r["resolved_at"] = now_iso()
            target = r
        out.append(json.dumps(r, ensure_ascii=False))
    Path(FAILURES_FILE).write_text("\n".join(out) + "\n", encoding="utf-8")
    if target:
        # 人工处理 → 抑制缩短至1天
        suppress(target["question"], 1, "resolved_by_human")
        log(f"标记 {qhash} -> {status}")
    else:
        log(f"未找到 {qhash}")


# ---------- main ----------

def daemon_loop(config):
    """常驻守护进程：周期消费队列 + 定时巡检。"""
    interval = int(config.get("trigger", {}).get("daemon_interval_sec", 60))
    patrol_time = str(config.get("trigger", {}).get("patrol_time", "09:00"))
    patrol_day = None
    log(f"[daemon] 启动，轮询间隔 {interval}s，巡检时刻 {patrol_time}")
    while True:
        today = time.strftime("%Y-%m-%d")
        now_hhmm = time.strftime("%H:%M")
        # 每天到点跑一次巡检（补漏即时触发覆盖不到的历史低分）
        if patrol_day != today and now_hhmm >= patrol_time:
            try:
                daily_patrol(config)
            except Exception as e:
                log(f"[daemon] 巡检异常: {e}")
            patrol_day = today
        if patrol_day is None:
            patrol_day = today
        # 消费队列（未超日限流时）
        if _exceed_daily_limit(config):
            log(f"[daemon] 今日已触发 {_today_trigger_count()} 次，达上限，跳过队列消费")
        else:
            fd = _acquire_lock()
            if fd is None:
                log("[daemon] 有 Agent 在运行，跳过本次")
            else:
                try:
                    drain_queue(config)
                except Exception as e:
                    log(f"[daemon] 队列处理异常: {e}")
                finally:
                    _release_lock(fd)
        time.sleep(interval)


def _ensure_analytics():
    from analytics import repository
    if repository.configured():
        return
    from infra.sqlite_analytics import SqliteAnalyticsRepository
    repo = SqliteAnalyticsRepository()
    repo.migrate()
    repository.configure(repo)


def main():
    _ensure_analytics()
    parser = argparse.ArgumentParser(prog="trigger")
    parser.add_argument("--incident", help="低分事件 JSON 文件")
    parser.add_argument("--process-queue", action="store_true")
    parser.add_argument("--daily-patrol", action="store_true")
    parser.add_argument("--daemon", action="store_true", help="常驻守护进程：周期消费队列+定时巡检")
    parser.add_argument("--list-failures", action="store_true")
    parser.add_argument("--mark-resolved", metavar="HASH")
    parser.add_argument("--mark-wontfix", metavar="HASH")
    args = parser.parse_args()
    config = load_config()

    if args.daemon:
        daemon_loop(config)
    elif args.incident:
        incident = load_json(args.incident, None)
        if not incident:
            log(f"无法读取 incident: {args.incident}")
            sys.exit(1)
        process_incident(incident, config)
    elif args.process_queue:
        fd = _acquire_lock()
        if fd is None:
            print("有 Agent 在运行，跳过")
        else:
            try:
                drain_queue(config)
            finally:
                _release_lock(fd)
    elif args.daily_patrol:
        daily_patrol(config)
    elif args.list_failures:
        list_failures()
    elif args.mark_resolved:
        mark_failure(args.mark_resolved, "resolved")
    elif args.mark_wontfix:
        mark_failure(args.mark_wontfix, "wont_fix")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
