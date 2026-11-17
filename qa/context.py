"""上下文构建：token 预算、消息布局、证据编号。

同时兼容旧 dict 与新 dataclass 输入；多轮规则与摘要提示词来自 qa/prompts。
"""
from __future__ import annotations

import os
from typing import Mapping, Sequence

from domain.models import BuiltContext, Chunk, Message

from .prompts import default_registry


DEFAULT_BUDGET = 12000
DEFAULT_MAX_HISTORY_TURNS = 3
ANSWER_CLIP = 600

MULTITURN_RULES = """## 多轮对话规则
1. 历史对话只用于理解指代和延续话题，**不得作为事实依据**。
2. 只允许引用本轮证据编号 [E1]..[En]；不得编造引用，也不得引用历史中的编号。
3. 证据不足时明确说“当前知识库中没有足够信息”，并指出缺少什么。
4. 与本会话上一轮结论冲突时，显式说明冲突与更新依据。
5. 不要重复上一轮已完整回答的内容，除非用户要求展开。
6. 指代已由系统改写，回答时直接使用具体实体，不复述“他/这个/刚才那个”。
7. 回答语言与用户一致，默认简体中文。
8. **引用格式以本规则为准**：关键结论后标注 [E1]、[E2]；不要再输出 [来源: ...] 行，来源由系统统一展示。"""

SUMMARY_SYSTEM = default_registry().body("summarize")


def aux_thinking() -> dict[str, str] | None:
    """辅助任务（改写/摘要）默认关闭 thinking，可用 LLM_AUX_THINKING=on 恢复。"""
    flag = (os.environ.get("LLM_AUX_THINKING") or "off").strip().lower()
    return None if flag in {"on", "1", "true", "yes"} else {"type": "disabled"}


def _get(item: object, key: str, default: object = "") -> object:
    if isinstance(item, Mapping):
        value = item.get(key, default)
    else:
        value = getattr(item, key, default)
    return default if value is None else value


def _chunk_type(chunk: object) -> str:
    if isinstance(chunk, Mapping):
        value = chunk.get("type") or chunk.get("kind") or ""
    else:
        kind = getattr(chunk, "kind", "")
        value = getattr(kind, "value", kind)
    return str(value)


def _chunk_text(chunk: object) -> str:
    return str(_get(chunk, "text", "") or "")


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文 1.5 字/token，英文 4 字符/token。"""
    if not text:
        return 0
    text = str(text)
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4) + 1


def format_evidence(evidence: Sequence[object]) -> str:
    """证据块：[E1] 《标题》｜类型｜路径｜时间 + 正文。"""
    lines: list[str] = []
    for index, chunk in enumerate(evidence, 1):
        created = str(_get(chunk, "created", "") or "")
        time_tag = f"｜{created}" if created else ""
        lines.append(
            f"[E{index}] 《{_get(chunk, 'title', '')}》｜{_chunk_type(chunk)}｜"
            f"{_get(chunk, 'doc_path', '')}{time_tag}"
        )
        lines.append(_chunk_text(chunk).strip())
        lines.append("")
    return "\n".join(lines).strip()


def build_context(chunks: Sequence[object]) -> str:
    """单轮上下文：来源行 + 正文。"""
    lines: list[str] = []
    for chunk in chunks:
        created = str(_get(chunk, "created", "") or "")
        time_tag = f" | {created}" if created else ""
        lines.append(
            f"[来源: {_get(chunk, 'title', '')} | {_chunk_type(chunk)} | "
            f"{_get(chunk, 'doc_path', '')}{time_tag}]"
        )
        lines.append(_chunk_text(chunk))
        lines.append("")
    return "\n".join(lines)


def _current_user_content(evidence_text: str, query: str, raw_question: str | None = None) -> str:
    parts = ["## 检索到的知识库内容", evidence_text or "（无）", ""]
    if raw_question and raw_question.strip() and raw_question.strip() != (query or "").strip():
        parts += ["## 用户原始输入", raw_question.strip(), ""]
    parts += ["## 当前问题", (query or "").strip()]
    return "\n".join(parts)


def _clip_answer(text: str, limit: int = ANSWER_CLIP) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……"


def _history_pairs(window: Sequence[object], turns: int) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in list(window or [])[-turns:]:
        question = str(_get(item, "question", "") or "").strip()
        answer = str(_get(item, "answer", "") or "").strip()
        if question and answer:
            pairs.append((question, _clip_answer(answer)))
    return pairs


def build(
    system_prompt: str,
    summary: str,
    window: Sequence[object],
    evidence: Sequence[Chunk],
    query: str,
    raw_question: str | None = None,
    budget: int | None = None,
    max_turns: int | None = None,
) -> BuiltContext:
    """构建多轮 LLM 消息，保证首尾 user、角色交替、历史不带证据。"""
    budget = int(budget or DEFAULT_BUDGET)
    max_turns = int(max_turns or DEFAULT_MAX_HISTORY_TURNS)

    system = (system_prompt or "").strip()
    if summary:
        system += f"\n\n## 对话摘要\n{summary.strip()}"
    system += "\n\n" + MULTITURN_RULES

    evidence_budget = min(6000, int(budget * 0.5))
    kept: list[Chunk] = []
    used = 0
    for chunk in evidence or []:
        cost = estimate_tokens(format_evidence([chunk]))
        if kept and used + cost > evidence_budget:
            break
        kept.append(chunk)
        used += cost

    evidence_text = format_evidence(kept)
    evidence_ids = [str(_get(chunk, "chunk_id")) for chunk in kept]

    history = _history_pairs(window, max_turns)
    sys_cost = estimate_tokens(system)
    cur_cost = estimate_tokens(_current_user_content(evidence_text, query, raw_question))
    remaining = budget - sys_cost - cur_cost

    selected: list[tuple[str, str]] = []
    history_cost = 0
    for question, answer in reversed(history):
        cost = estimate_tokens(question) + estimate_tokens(answer)
        if selected and history_cost + cost > remaining:
            break
        selected.insert(0, (question, answer))
        history_cost += cost

    if history and selected and history[0] not in selected:
        question, answer = history[0]
        cost = estimate_tokens(question) + estimate_tokens(answer)
        if history_cost + cost <= remaining:
            selected.insert(0, (question, answer))

    truncated = len(selected) < len(history) or len(kept) < len(evidence or [])

    messages: list[Message] = []
    for question, answer in selected:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})
    messages.append({
        "role": "user",
        "content": _current_user_content(evidence_text, query, raw_question),
    })
    if messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": "（会话开始）"})

    prompt_tokens = estimate_tokens(system) + sum(
        estimate_tokens(message["content"]) for message in messages
    )
    return BuiltContext(
        system=system,
        messages=tuple(messages),
        prompt_tokens=prompt_tokens,
        truncated=truncated,
        evidence_ids=tuple(evidence_ids),
        evidence_text=evidence_text,
    )


def summarize_prompt(prev_summary: str, turns: Sequence[object]) -> str:
    lines: list[str] = []
    if prev_summary:
        lines += ["## 已有摘要", prev_summary.strip(), ""]
    lines.append("## 待压缩对话")
    for item in turns:
        question = str(_get(item, "question", "") or "").strip()
        answer = str(_get(item, "answer", "") or "").strip().replace("\n", " ")
        lines += [f"user: {question}", f"assistant: {_clip_answer(answer, 300)}"]
    return "\n".join(lines)
