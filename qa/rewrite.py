"""查询改写：规则门控 + LLM JSON + 失败回退。"""
from __future__ import annotations

import difflib
import os
import re
from typing import Mapping, Sequence

from domain.json_utils import parse_json
from .context import aux_thinking
from .prompts import default_registry

from domain.models import Intent, RewriteResult


# ---------- 原生实现 ----------

INTENTS = {"new_topic", "follow_up", "correction", "comparison",
           "clarification", "meta", "chitchat"}
PRONOUN_RE = re.compile(
    r"(他|她|它|他们|她们|它们|这个|那个|这些|那些|前者|后者|该|其|"
    r"上面|刚才|上面说的|前面|这本书|那本书|这部|那部|此书|该书)"
)
ELLIPSIS_RE = re.compile(r"^(那|那么|还有|换成|如果是|要是)")
CONT_RE = re.compile(
    r"(继续|展开|详细|再说|然后|为什么|怎么做到|举个例子|具体说说|多说|"
    r"其他|其它|别的|举例说明|接着说)"
)
TOPIC_SHIFT_RE = re.compile(r"(换个话题|换一个话题|另一个话题|新问题|再问一个|问个别的)")

# “介绍/说说/讲讲 + 具体对象”本身就是完整问题，不应被当成上一轮的省略追问。
SELF_CONTAINED_RE = re.compile(
    r"^(?:请|帮我|麻烦)?\s*(?:介绍|简介|说说|讲讲|解释|科普)(?:一下|下)?\s*(\S{2,})$"
)

# 提取“实词”时剔除的指令词与疑问词。仅用于判断改写是否保留了用户真正问的对象。
_CONTENT_NOISE_TERMS = (
    "介绍一下", "介绍下", "请问", "帮我", "麻烦", "一下", "说说", "讲讲",
    "讲一下", "说一下", "解释一下", "解释", "什么是", "是谁啊", "是谁",
    "啥是", "科普", "关于", "有哪些", "有什么", "哪个", "哪些", "什么",
    "怎么", "为什么", "如何", "多少", "是否", "有没有", "能不能",
    "可不可以", "请", "的", "了", "吗", "呢", "啊", "吧", "呀", "嘛",
)
_CONTENT_NOISE_CHARS = set("的了呢吗吧啊呀嘛哦噢")

REWRITE_SYSTEM = default_registry().body("rewrite")


def _window_value(item: object, key: str, default: object = "") -> object:
    if isinstance(item, Mapping):
        value = item.get(key, default)
    else:
        value = getattr(item, key, default)
    return default if value is None else value


def _is_self_contained_new_topic(question: str) -> bool:
    """短句 + 明确对象（如“介绍下连杰”）是完整新问题，不是省略追问。"""
    q = (question or "").strip()
    if not q:
        return False
    if PRONOUN_RE.search(q) or ELLIPSIS_RE.search(q) or CONT_RE.search(q):
        return False
    return bool(SELF_CONTAINED_RE.match(q))


def _content_text(text: str) -> str:
    """去掉指令词、疑问词与语气词，留下用户真正询问的实词。"""
    value = text or ""
    for term in sorted(_CONTENT_NOISE_TERMS, key=len, reverse=True):
        value = value.replace(term, " ")
    value = re.sub(r"[\s，。！？、；：,.!?;:（）()《》“”\"'·\-—…]+", "", value)
    return "".join(ch for ch in value if ch not in _CONTENT_NOISE_CHARS)


def _content_bigrams(text: str) -> set[str]:
    content = _content_text(text)
    if len(content) < 2:
        return set()
    return {content[index:index + 2] for index in range(len(content) - 1)}


def _preserves_content(question: str, resolved: str) -> bool:
    """改写结果至少保留原始问题的一个实词二元组，否则视为话题被替换。"""
    grams = _content_bigrams(question)
    if not grams:
        return True
    return any(gram in (resolved or "") for gram in grams)


def _has_unresolved_content(
    question: str,
    resolved: str,
    entities: Sequence[str],
) -> bool:
    """当前输入里有实体，但改写结果和会话实体都没有承接它。"""
    grams = _content_bigrams(question)
    if not grams:
        return False
    if any(gram in (resolved or "") for gram in grams):
        return False
    entity_text = " ".join(str(item) for item in entities or ())
    if entity_text and any(gram in entity_text for gram in grams):
        return False
    return True


def _min_confidence() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("REWRITE_MIN_CONFIDENCE", "0.5"))))
    except (TypeError, ValueError):
        return 0.5


def needs_rewrite(question: str, has_history: bool, intent_hint=None, mode: str = "auto") -> bool:
    if not has_history:
        return False
    if mode == "off":
        return False
    if mode == "always":
        return True
    if intent_hint in ("correction", "clarification", "comparison"):
        return True
    q = (question or "").strip()
    if _is_self_contained_new_topic(q):
        return False
    if len(q) <= 12:
        return True
    if PRONOUN_RE.search(q) or ELLIPSIS_RE.search(q) or CONT_RE.search(q):
        return True
    return False


def _format_window(window: Sequence[object], max_turns: int = 1, answer_chars: int = 80) -> str:
    lines = []
    for item in list(window or [])[-max_turns:]:
        question = str(
            _window_value(item, "resolved_query", "")
            or _window_value(item, "question", "")
            or ""
        ).strip()
        raw = str(_window_value(item, "question", "") or "").strip()
        if raw and question and raw != question:
            question = f"{question}（原话：{raw}）"
        answer = str(_window_value(item, "answer", "") or "").strip().replace("\n", " ")
        if len(answer) > answer_chars:
            answer = answer[:answer_chars] + "…"
        lines.append(f"user: {question}")
        lines.append(f"assistant: {answer}")
    return "\n".join(lines) or "（无）"


def _last_topic(window: Sequence[object]) -> str:
    for item in reversed(list(window or [])):
        topic = str(
            _window_value(item, "resolved_query", "")
            or _window_value(item, "question", "")
            or ""
        ).strip()
        if topic:
            return topic[:80]
    return ""


def _prompt(question: str, window: Sequence[object], summary: str, entities: Sequence[str]) -> str:
    return f"""## 会话摘要
{summary or "（无）"}

## 当前话题（最近一轮独立问题）
{_last_topic(window) or "（无）"}

## 最近对话
{_format_window(window)}

## 会话实体
{"、".join(entities) if entities else "（无）"}

## 当前用户输入
{question}

## 判定要求
- 当前输入是省略、指代或“继续/展开”类追问时，必须延续“当前话题”，不要跳到更早的话题。
- 只有当前输入明确出现新的独立主题（如“解放战争……”“换个话题”）时，才判 intent=new_topic。
- resolved_query 必须是可独立检索的问题，不得包含上一轮完整问题，也不得把多个历史问题拼在一起。
- 只输出 JSON。

请按要求只输出 JSON。"""


def _rule_resolve(question: str, window: Sequence[object], entities: Sequence[str]) -> str:
    """规则兜底：只做高置信的代词替换，绝不拼接历史问题。

    设计约束见 docs/multi-turn-design.md §5.6：改写失败应等价单轮，
    用 raw_question 检索；拼接会把上一轮的污染累积到后续所有轮次。
    """
    q = (question or "").strip()
    if len(entities) == 1 and PRONOUN_RE.search(q):
        return PRONOUN_RE.sub(entities[0], q, count=1)
    return q


def _rewrite_max_tokens() -> int:
    try:
        return max(200, int(os.environ.get("REWRITE_MAX_TOKENS", "2000")))
    except (TypeError, ValueError):
        return 2000


def _guard_resolved(question: str, resolved: str) -> str:
    """拒绝累积/超长/把用户新实体整个丢掉的改写结果。"""
    resolved = (resolved or "").strip()
    if not resolved:
        return question
    if len(resolved) > 120:
        return question
    if resolved.count("？") + resolved.count("?") > 2:
        return question
    if not _preserves_content(question, resolved):
        return question
    return resolved


def _lcs_len(left: str, right: str) -> int:
    if not left or not right:
        return 0
    match = difflib.SequenceMatcher(None, left, right).find_longest_match(
        0, len(left), 0, len(right)
    )
    return match.size


def _post_check_intent(
    question: str,
    resolved: str,
    intent: Intent,
    entities: Sequence[str],
    window: Sequence[object],
) -> Intent:
    """小模型意图兜底：用实体/话题锚点修正 new_topic 判定。

    小模型容易把“解放战争……”判成 follow_up。这里用会话实体做确定性校验：
    当前问题没有承接任何 active entity 时，判为新话题。
    """
    if intent is Intent.NEW_TOPIC:
        return intent
    q = (question or "").strip()
    if TOPIC_SHIFT_RE.search(q):
        return Intent.NEW_TOPIC
    # 代词/省略/继续类优先相信模型，避免把正常追问误切成新话题。
    if PRONOUN_RE.search(q) or ELLIPSIS_RE.search(q) or CONT_RE.search(q):
        return intent
    # 模型把新输入里的实体整个丢掉时，即使它包含当前实体，也应判为新话题。
    if intent in (Intent.FOLLOW_UP, Intent.CLARIFICATION):
        if _has_unresolved_content(q, resolved, entities):
            return Intent.NEW_TOPIC
    if entities and not any(entity and entity in resolved for entity in entities):
        return Intent.NEW_TOPIC
    # 实体槽为空时，用上一轮独立问题做保守兜底；短句仍视为追问。
    if not entities and len(q) > 6:
        topic = _last_topic(window)
        if topic and _lcs_len(topic, resolved) < 4:
            return Intent.NEW_TOPIC
    return intent


def _sanitize(data: Mapping, question: str) -> RewriteResult | None:
    resolved = str(data.get("resolved_query") or "").strip()
    if not resolved:
        return None
    intent = data.get("intent")
    if intent not in INTENTS:
        intent = "follow_up"
    raw_entities = data.get("entities")
    entities = (
        [str(item).strip() for item in raw_entities if str(item).strip()]
        if isinstance(raw_entities, list) else []
    )
    filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))
    clarify = bool(data.get("needs_clarification"))
    clarify_question = str(data.get("clarification_question") or "").strip()
    if clarify and not clarify_question:
        clarify = False
    return RewriteResult(
        resolved_query=resolved,
        intent=Intent(str(intent)),
        entities=tuple(entities),
        filters=dict(filters),
        needs_clarification=clarify,
        clarification_question=clarify_question,
        confidence=confidence,
        failed=False,
        skipped=False,
    )


def resolve_native(
    question: str,
    window: Sequence[object],
    summary: str,
    entities: Sequence[str],
    llm,
    mode: str = "auto",
    intent_hint: str | None = None,
) -> RewriteResult:
    base = RewriteResult(
        resolved_query=(question or "").strip(),
        intent=Intent.NEW_TOPIC,
        entities=(),
        confidence=1.0,
        failed=False,
        skipped=True,
    )
    window = window or []
    entities = entities or []
    q = (question or "").strip()

    # 自足型新问题直接按新话题处理：清空旧实体并隔离历史，避免被上一轮话题吞掉。
    if (
        mode != "off"
        and window
        and intent_hint not in ("correction", "clarification", "comparison")
        and _is_self_contained_new_topic(q)
    ):
        return RewriteResult(
            resolved_query=q,
            intent=Intent.NEW_TOPIC,
            entities=(),
            filters={},
            confidence=1.0,
            failed=False,
            skipped=False,
        )

    if not needs_rewrite(question, bool(window), intent_hint=intent_hint, mode=mode):
        return base

    try:
        for attempt in range(2):
            prompt = _prompt(question, window, summary, entities)
            if attempt:
                prompt += "\n\n注意：上一次输出无法解析。请只输出一行合法 JSON，不要代码围栏、不要解释。"
            chat_json = getattr(llm, "chat_json", None)
            if callable(chat_json):
                data = chat_json(
                    prompt, system=REWRITE_SYSTEM,
                    max_tokens=_rewrite_max_tokens(),
                    temperature=0.1,
                    thinking=aux_thinking(),
                )
            else:
                text = llm.chat(
                    [{"role": "user", "content": prompt}],
                    system=REWRITE_SYSTEM,
                    max_tokens=_rewrite_max_tokens(),
                    temperature=0.1,
                    thinking=aux_thinking(),
                )
                data = parse_json(text)
            parsed = _sanitize(data, question) if isinstance(data, Mapping) else None
            if parsed:
                guarded = _guard_resolved(question, parsed.resolved_query)
                if guarded != parsed.resolved_query:
                    print(
                        f"[rewrite] rejected dropped-content "
                        f"q={question!r} resolved={parsed.resolved_query!r}",
                        flush=True,
                    )
                    parsed = ReplaceResolved(parsed, guarded)
                    parsed = ReplaceIntent(parsed, Intent.NEW_TOPIC)
                    parsed = ReplaceEntities(parsed, ())
                    parsed = ReplaceClarification(parsed, False)
                elif (
                    parsed.confidence < _min_confidence()
                    and parsed.resolved_query.strip() != q
                ):
                    # 小模型自己都没把握时，宁可按新话题原文检索，也不要继承上一轮。
                    print(
                        f"[rewrite] rejected low-confidence "
                        f"confidence={parsed.confidence:.2f} q={question!r} "
                        f"resolved={parsed.resolved_query!r}",
                        flush=True,
                    )
                    parsed = ReplaceResolved(parsed, q)
                    parsed = ReplaceIntent(parsed, Intent.NEW_TOPIC)
                    parsed = ReplaceEntities(parsed, ())
                    parsed = ReplaceClarification(parsed, False)
                checked = _post_check_intent(
                    question, parsed.resolved_query, parsed.intent, entities, window
                )
                if checked is not parsed.intent:
                    print(
                        f"[rewrite] intent override {parsed.intent.value}->{checked.value} "
                        f"q={question!r}",
                        flush=True,
                    )
                    parsed = ReplaceIntent(parsed, checked)
                    if checked is Intent.NEW_TOPIC:
                        parsed = ReplaceClarification(parsed, False)
                return parsed
    except Exception as exc:
        print(
            f"[rewrite] failed: {type(exc).__name__}: {exc}",
            flush=True,
        )

    # 失败等价单轮：使用原文，不继承历史；仅保留高置信代词替换。
    return RewriteResult(
        resolved_query=_rule_resolve(question, window, entities),
        intent=Intent.FOLLOW_UP,
        entities=(),
        confidence=0.4,
        failed=True,
        skipped=False,
    )


def ReplaceResolved(result: RewriteResult, resolved: str) -> RewriteResult:
    return RewriteResult(
        resolved_query=resolved,
        intent=result.intent,
        entities=result.entities,
        filters=result.filters,
        needs_clarification=result.needs_clarification,
        clarification_question=result.clarification_question,
        confidence=result.confidence,
        failed=result.failed,
        skipped=result.skipped,
    )


def ReplaceEntities(result: RewriteResult, entities: Sequence[str]) -> RewriteResult:
    return RewriteResult(
        resolved_query=result.resolved_query,
        intent=result.intent,
        entities=tuple(entities),
        filters=result.filters,
        needs_clarification=result.needs_clarification,
        clarification_question=result.clarification_question,
        confidence=result.confidence,
        failed=result.failed,
        skipped=result.skipped,
    )


def ReplaceClarification(
    result: RewriteResult,
    needed: bool,
    question: str = "",
) -> RewriteResult:
    return RewriteResult(
        resolved_query=result.resolved_query,
        intent=result.intent,
        entities=result.entities,
        filters=result.filters,
        needs_clarification=needed,
        clarification_question=question if needed else "",
        confidence=result.confidence,
        failed=result.failed,
        skipped=result.skipped,
    )


def ReplaceIntent(result: RewriteResult, intent: Intent) -> RewriteResult:
    return RewriteResult(
        resolved_query=result.resolved_query,
        intent=intent,
        entities=result.entities,
        filters=result.filters,
        needs_clarification=result.needs_clarification,
        clarification_question=result.clarification_question,
        confidence=result.confidence,
        failed=result.failed,
        skipped=result.skipped,
    )


class NativeRewriter:
    def needs_rewrite(self, question: str, has_history: bool, intent_hint=None, mode="auto") -> bool:
        hint = intent_hint.value if isinstance(intent_hint, Intent) else intent_hint
        return needs_rewrite(question, has_history, intent_hint=hint, mode=mode)

    def resolve(
        self,
        question: str,
        window: Sequence[object],
        summary: str,
        entities: Sequence[str],
        llm,
        mode: str = "auto",
        intent_hint: Intent | None = None,
    ) -> RewriteResult:
        hint = intent_hint.value if isinstance(intent_hint, Intent) else intent_hint
        return resolve_native(question, window, summary, entities, llm,
                              mode=mode, intent_hint=hint)

    def update_entity_slots(
        self,
        prev_entities: Sequence[str],
        result: RewriteResult,
        turn_id: int,
    ) -> tuple[list[str], list[str]]:
        upsert = list(dict.fromkeys(result.entities))
        delete = []
        if result.intent in (Intent.CORRECTION, Intent.NEW_TOPIC):
            delete = [entity for entity in prev_entities if entity not in upsert]
        return upsert, delete
