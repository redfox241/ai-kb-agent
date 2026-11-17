"""P0：optimizer_agent 受限 ReAct 循环测试。"""
from __future__ import annotations

from optimization.agent import optimizer_agent as oa


class PlanLLM:
    def __init__(self, plans):
        self.plans = list(plans)
        self.prompts = []

    def chat_json(self, prompt, system=None, max_tokens=None):
        self.prompts.append(prompt)
        return self.plans.pop(0) if self.plans else None


def _prepare(monkeypatch, scores, plans):
    applied, restarted, rolled = [], [], []
    values = iter(scores)

    def verify(*args, **kwargs):
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(oa.mcp_tools, "verify_single_question", verify)
    monkeypatch.setattr(oa, "apply_change", lambda plan, snapshot=None: applied.append(plan))
    monkeypatch.setattr(oa, "restart_8001", lambda log=None: restarted.append(True))
    monkeypatch.setattr(oa, "snapshot_overrides", lambda: {"data": {}})
    monkeypatch.setattr(oa, "rollback_overrides", lambda snapshot: rolled.append(snapshot))
    return PlanLLM(plans), applied, restarted, rolled


def test_build_diagnose_prompt_second_attempt():
    prompt = oa.build_diagnose_prompt(
        "q", "a", 5, "reason", ["w1"], 2, {"strategy": "first"}
    )
    assert "第一次优化" in prompt
    assert "first" in prompt


def test_run_agent_success(monkeypatch):
    plan = {"action": "prompt_rule", "prompt_rule": "规则", "strategy": "s", "reasoning": "r"}
    llm, applied, restarted, rolled = _prepare(
        monkeypatch,
        [{"score": 5, "answer": "old"}, {"score": 7, "answer": "new"}],
        [plan],
    )

    result, history = oa.run_agent({"question": "q"}, llm=llm)

    assert result["status"] == "success"
    assert result["final_score"] == 7
    assert result["attempts"] == 1
    assert applied == [plan]
    assert restarted
    assert rolled == []
    assert history[0]["score"] == 7


def test_run_agent_knowledge_gap(monkeypatch):
    plan = {"action": "knowledge_gap", "reasoning": "知识库没有"}
    llm, applied, _, _ = _prepare(
        monkeypatch, [{"score": 3, "answer": "old"}], [plan]
    )

    result, _ = oa.run_agent({"question": "q"}, llm=llm)

    assert result["status"] == "knowledge_gap"
    assert applied == []


def test_run_agent_give_up_when_plan_missing(monkeypatch):
    llm, applied, _, _ = _prepare(
        monkeypatch, [{"score": 4, "answer": "old"}], [None]
    )

    result, _ = oa.run_agent({"question": "q"}, llm=llm)

    assert result["status"] == "give_up"
    assert applied == []


def test_run_agent_rolls_back_on_verify_failure(monkeypatch):
    plan = {"action": "param_change", "param": {"name": "TOP_K", "value": 10},
            "strategy": "s"}
    llm, applied, restarted, rolled = _prepare(
        monkeypatch,
        [{"score": 5, "answer": "old"}, RuntimeError("verify down")],
        [plan],
    )

    result, _ = oa.run_agent({"question": "q"}, llm=llm)

    assert result["status"] == "give_up"
    assert applied == [plan]
    assert len(rolled) == 1
    assert len(restarted) == 2
