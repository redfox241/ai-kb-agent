"""trigger.py 确定性逻辑单元测试（不触发实时 Agent 循环）。"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from optimization.agent import trigger  # noqa: E402


def _fresh_state():
    """将日志/抑制/队列指向临时目录，避免污染真实数据。"""
    tmp = tempfile.mkdtemp()
    trigger.LOGS = tmp
    trigger.AGENT_LOGS = os.path.join(tmp, "agent")
    trigger.SUPPRESS_FILE = os.path.join(tmp, "suppressed_questions.json")
    trigger.QUEUE_FILE = os.path.join(tmp, "pending_queue.json")
    trigger.HISTORY_FILE = os.path.join(tmp, "optimization_history.jsonl")
    os.makedirs(trigger.AGENT_LOGS, exist_ok=True)


CONFIG = {
    "dedup": {"cooldown_hours": 6, "max_variants": 5, "intent_jaccard_threshold": 0.6},
    "priority": {"affected_user_weight": 0.5, "related_weight": 0.3},
    "domain_words": ["RAG", "MCP", "战役", "长征", "代理"],
    "stopwords": ["的", "是", "什么", "怎么", "为什么", "多少", "呢", "吗", "了", "在", "有"],
}


def test_normalize():
    assert trigger.normalize_question("  ？？请问，长征 从 什么时候开始？") == "请问，长征 从 什么时候开始？".lower()


def test_suppression():
    _fresh_state()
    h = trigger.suppress("测试问题", 30, "give_up")
    hit, h2, rec = trigger._is_suppressed("测试问题", CONFIG)
    assert hit and h == h2
    hit3, _, _ = trigger._is_suppressed("不同问题", CONFIG)
    assert not hit3


def test_dedup_6h():
    _fresh_state()
    incident = {"question": "长征什么时候开始？", "score": 3, "answer": "x", "user_id": "u1"}
    action, entry = trigger.enqueue(incident, CONFIG)
    assert action in ("enqueued", "aggregated")
    # 写入历史，模拟刚优化过
    trigger.append_jsonl(trigger.HISTORY_FILE, {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "question": incident["question"],
        "question_hash": trigger.md5(trigger.normalize_question(incident["question"])),
        "status": "success"})
    action2, _ = trigger.enqueue(incident, CONFIG)
    assert action2 == "dedup"


def test_intent_aggregation():
    _fresh_state()
    trigger.enqueue({"question": "RAG 是什么？", "score": 3, "user_id": "u1"}, CONFIG)
    action, entry = trigger.enqueue({"question": "什么是 RAG 技术？", "score": 2, "user_id": "u2"}, CONFIG)
    assert action == "aggregated", action
    queue = trigger.load_queue()
    assert len(queue) == 1
    assert len(queue[0]["variants"]) == 2
    assert queue[0]["score"] == 2  # 保留最低分


def test_domain_separation():
    _fresh_state()
    # 关键词 Jaccard=3/5=0.6，本会聚合；但领域词 RAG vs MCP 不同 → 强制不聚合
    trigger.enqueue({"question": "RAG 检索技术原理是什么？", "score": 3, "user_id": "u1"}, CONFIG)
    action, _ = trigger.enqueue({"question": "MCP 检索技术原理详解", "score": 4, "user_id": "u2"}, CONFIG)
    assert action == "enqueued"
    assert len(trigger.load_queue()) == 2
    # 同领域词 → 聚合
    action2, _ = trigger.enqueue({"question": "RAG 检索技术原理介绍", "score": 2, "user_id": "u3"}, CONFIG)
    assert action2 == "aggregated"
    assert len(trigger.load_queue()) == 2


def test_priority_and_batch():
    _fresh_state()
    e1 = {"question": "q1", "score": 8, "affected_users": ["a"], "related_count": 0,
          "keywords": ["k1"]}
    e2 = {"question": "q2", "score": 3, "affected_users": ["a", "b", "c"], "related_count": 2,
          "keywords": ["k2"]}
    assert trigger.calculate_priority(e2, CONFIG) < trigger.calculate_priority(e1, CONFIG)
    # 3 条队列 → 批量
    q = [e1, e2, {"question": "q3", "score": 5, "affected_users": ["a"], "related_count": 0,
                  "keywords": ["k3"], "enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%S")}]
    assert trigger.should_batch(q)


def test_parse_result():
    from optimization.agent import optimizer_agent
    good = optimizer_agent.parse_agent_result(
        '前置说明\n===RESULT===\n{"status": "success", "final_score": 7.5, "attempts": 2}\n===END===\n后置')
    assert good["status"] == "success" and good["final_score"] == 7.5
    assert optimizer_agent.parse_agent_result("随便一句话") is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n全部 {len(fns)} 项测试通过")
