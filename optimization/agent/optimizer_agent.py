"""Agent 优化器。

实现方式（本地适配）：
- 默认 internal：进程内受限 ReAct 循环。LLM 只允许输出受控变更
  （prompt_rule / param_change / knowledge_gap / give_up），由 trigger 以
  overrides 文件方式应用到 server_auto(8001)。比自由 agent 改代码更安全，
  也贴合设计文档 §5.4「可修复方向按安全性排序」。
- 可选 claude_cli：按设计文档调用 `claude --print` 自主 ReAct 循环，
  需要用户在 settings 中放行 claude 命令（见 README）。

JSON 输出协议（对应设计 §5.4）：
===RESULT===
{"status": "success|give_up|knowledge_gap", "final_score": 7.5, "attempts": 2,
 "score_history": [...], "changes_summary": "...", "diff_scope": "...",
 "root_cause": "...", "suggestion": "..."}
===END===
"""
import json
import os
import sys

from infra.paths import BASE_DIR, AUTO_OPT_DIR
ROOT = BASE_DIR

from optimization.agent import mcp_tools  # noqa: E402
from optimization.utils.overrides import (load_overrides, rollback_overrides,  # noqa: E402
                                               snapshot_overrides, apply_change)
from infra.llm import LLMClient, parse_json  # noqa: E402

AGENT_SYSTEM_PROMPT = """你是知识库自迭代优化 Agent。目标是修复低分问答（score<6），采用受限的受控变更，输出结构化结果。

## 可用变更方向（只能选一个）
1. prompt_rule：向 RAG 注入一条 system prompt 补充规则（文本），用于改变回答的组织方式/强调重点/补充要求
2. param_change：调整一个检索参数（SIMILARITY_THRESHOLD / TOP_K / MAX_SOURCES / MAX_CONTEXT_CHUNKS）
3. knowledge_gap：若问题确实超出知识库覆盖（回答已明示"未找到"且知识库确无内容），标记此方向，不做任何修改
4. give_up：已尝试且无法修复

## 诊断要求
- 先分析给出的当前回答与评分 weaknesses，定位根因（检索漏召回 / 排序不对 / prompt 引导不足）
- 每次修改采用一个明确方向，禁止同一方向微调
- 只输出 JSON，不要任何其他文本

## JSON 输出格式
{"action": "prompt_rule|param_change|knowledge_gap|give_up",
 "strategy": "本次优化方向的策略描述",
 "prompt_rule": "注入的规则文本（action=prompt_rule 时填写）",
 "param": {"name": "SIMILARITY_THRESHOLD", "value": 0.25},
 "reasoning": "根因分析与期望效果"}"""

DIAGNOSE_PROMPT = """当前低分问题：{question}
当前 8001 回答（截断）：{answer}
当前评分：{score} / 10
评分理由：{reason}
主要弱点：{weaknesses}

请诊断根因并输出受控变更计划（严格 JSON，字段见系统提示）。"""

SECOND_ATTEMPT_PROMPT = """第一次优化（{strategy}）后评分仍为 {score} 分，弱点：{weaknesses}。
请分析为什么第一次方向没生效，换一个【完全不同】的方向再次尝试。"""


def build_diagnose_prompt(question, answer, score, reason, weaknesses, attempt, prev):
    prompt = DIAGNOSE_PROMPT.format(
        question=question, answer=(answer or "")[:1500], score=score,
        reason=reason, weaknesses="; ".join(weaknesses))
    if attempt == 2 and prev:
        prompt += "\n\n" + SECOND_ATTEMPT_PROMPT.format(
            strategy=prev.get("strategy", ""), score=score,
            weaknesses="; ".join(weaknesses))
    return prompt


def _make_result(status, final_score, score_history, changes_summary, root_cause, suggestion, diff_scope="narrow"):
    return {
        "status": status,
        "final_score": final_score,
        "attempts": len(score_history),
        "score_history": score_history,
        "changes_summary": changes_summary,
        "diff_scope": diff_scope,
        "root_cause": root_cause,
        "suggestion": suggestion,
    }


def run_agent(incident, port=8001, llm=None, log=None):
    """进程内受限 ReAct 循环：诊断 → 受控变更 → 验证 → (换方向) → 输出 RESULT。"""
    llm = llm or LLMClient()
    log = log or (lambda *a, **k: None)
    question = incident.get("question", "")

    # 预取当前 8001 行为
    log(f"[agent] 预取 8001 当前回答: {question}")
    try:
        current = mcp_tools.verify_single_question(question, port=port, times=1)
    except Exception as e:
        current = {"answer": incident.get("answer", ""), "score": incident.get("score", 0),
                   "reason": str(e), "weaknesses": []}
    score_history = []
    prev_plan = None
    snapshot = snapshot_overrides()
    last_score = current.get("score", 0) or 0

    for attempt in (1, 2):
        prompt = build_diagnose_prompt(
            question, current.get("answer", ""), last_score,
            current.get("reason", ""), current.get("weaknesses", []), attempt, prev_plan)
        log(f"[agent] attempt {attempt} 请求变更计划")
        plan = llm.chat_json(prompt, system=AGENT_SYSTEM_PROMPT, max_tokens=1200)
        if not plan:
            log("[agent] 变更计划解析失败")
            break

        action = plan.get("action", "give_up")
        if action == "knowledge_gap":
            return _make_result("knowledge_gap", last_score, score_history, "",
                                plan.get("reasoning", ""),
                                "需人工补充知识库内容"), score_history
        if action == "give_up":
            break

        log(f"[agent] 应用变更: {action} -> {plan.get('strategy', '')}")
        try:
            apply_change(plan, snapshot=snapshot)
        except Exception as e:
            log(f"[agent] 应用变更失败: {e}")
            break

        # 重启 8001 + 验证
        restart_8001(log)
        try:
            v = mcp_tools.verify_single_question(question, port=port, times=2)
        except Exception as e:
            log(f"[agent] 验证失败: {e}")
            rollback_overrides(snapshot)
            restart_8001(log)
            break

        last_score = v.get("score", 0) or 0
        score_history.append({
            "attempt": attempt,
            "score": last_score,
            "reason": v.get("reason", ""),
            "weaknesses": v.get("weaknesses", []),
            "strategy": plan.get("strategy", ""),
            "action": action,
        })
        log(f"[agent] attempt {attempt} 得分 {last_score}")
        if last_score >= 6:
            return _make_result(
                "success", last_score, score_history,
                plan.get("strategy", ""), plan.get("reasoning", ""),
                "建议次日回归验证确认效果持续性"), score_history
        prev_plan = plan

    return _make_result("give_up", last_score, score_history, "",
                        "两次优化均未达到 6 分", "需人工介入分析"), score_history


def restart_8001(log=None):
    """重启 8001 测试服务并等待健康。"""
    log = log or (lambda *a, **k: None)
    script = os.path.join(ROOT, "scripts", "restart_8001.sh")
    import subprocess
    try:
        r = subprocess.run(["bash", script], capture_output=True, text=True, timeout=180)
        log(f"[restart] rc={r.returncode} {r.stdout[-200:]}")
        return r.returncode == 0
    except Exception as e:
        log(f"[restart] 失败: {e}")
        return False


# ---------- claude_cli 模式（可选，需放行权限） ----------

def build_claude_cmd(prompt):
    return ["claude", "-p", prompt,
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Bash,Read,Edit,Write",
            "--disallowedTools", "WebFetch,WebSearch",
            "--output-format", "text"]


def run_agent_claude_cli(incident):
    """设计文档 §5.2 的 claude --print 方式。需在 settings 放行。"""
    import subprocess
    question = incident.get("question", "")
    prompt = (
        f"你是知识库自迭代优化 Agent。修复以下低分问答（score<6）。\n"
        f"问题：{question}\n当前回答：{incident.get('answer','')[:800]}\n"
        f"评分：{incident.get('score')}。\n"
        f"安全约束：只修改 server_auto.py（本目录 8001 用 RAG_PORT=8001 启动），"
        f"绝不动 server.py；每次最多 2 处 prompt + 2 处参数修改；禁止 rm -rf、curl :8000。\n"
        f"工作流：诊断根因 → 修改 server_auto.py → 运行 `RAG_PORT=8001 bash scripts/restart_8001.sh` "
        f"→ `./scripts/kbtool verify \"{question}\" --port 8001` → score>=6 则输出 "
        f"===RESULT=== JSON ===END===（status=success），否则换完全不同方向二次修改，仍不足输出 status=give_up。")
    cmd = build_claude_cmd(prompt)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=2400, cwd=ROOT)
    return r.stdout, r.returncode


def parse_agent_result(text):
    """解析 ===RESULT=== JSON ===END=== 输出。"""
    if not text:
        return None
    start = text.find("===RESULT===")
    end = text.find("===END===", start)
    if start >= 0 and end > start:
        text = text[start + len("===RESULT==="):end].strip()
    data = parse_json(text)
    if not isinstance(data, dict):
        return None
    if data.get("status") not in ("success", "give_up", "knowledge_gap"):
        return None
    return data
