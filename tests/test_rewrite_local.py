"""本地 Qwen 改写：prompt 话题锚点、意图兜底与 Ollama 客户端契约。"""
from __future__ import annotations

from domain import Intent, RewriteResult, Turn
from infra.ollama_chat import OllamaChatClient
from qa.multiturn import ChatService
from qa.rewrite import NativeRewriter, _format_window, _prompt


class JsonLLM:
    def __init__(self, data):
        self.data = data
        self.prompts: list[str] = []

    def chat_json(self, prompt, system=None, max_tokens=None,
                  temperature=None, thinking=None):
        self.prompts.append(prompt)
        return dict(self.data)


WINDOW_4 = [
    Turn(
        turn_id=1,
        question="长征",
        resolved_query="长征",
        intent=Intent.NEW_TOPIC,
        answer="长征是 1934—1935 年的战略转移。",
    ),
    Turn(
        turn_id=2,
        question="著名的事件",
        resolved_query="长征中的著名事件",
        intent=Intent.FOLLOW_UP,
        answer="遵义会议。",
    ),
    Turn(
        turn_id=3,
        question="抗日战争开始时间和结束时间",
        resolved_query="抗日战争开始时间和结束时间",
        intent=Intent.NEW_TOPIC,
        answer="1937 年全面爆发，1945 年结束。",
    ),
]

WINDOW_7 = [
    Turn(
        turn_id=5,
        question="著名战役",
        resolved_query="抗日战争的著名战役",
        intent=Intent.FOLLOW_UP,
        answer="百团大战。",
    ),
    Turn(
        turn_id=6,
        question="常用的武器型号",
        resolved_query="抗日战争常用的武器型号",
        intent=Intent.FOLLOW_UP,
        answer="知识库未收录抗日武器型号。",
    ),
]


def test_prompt_contains_current_topic_and_resolved_history():
    prompt = _prompt("主要领导人", WINDOW_4, "", ["抗日战争"])
    assert "## 当前话题（最近一轮独立问题）" in prompt
    assert "抗日战争开始时间和结束时间" in prompt
    assert "user: 抗日战争开始时间和结束时间" in prompt
    assert "不要跳到更早的话题" in prompt
    # 历史行优先使用 resolved_query，并保留原话
    assert "user: 长征中的著名事件（原话：著名的事件）" in _format_window([WINDOW_4[1]])


def test_small_model_follow_up_keeps_latest_topic():
    llm = JsonLLM({
        "resolved_query": "抗日战争的主要领导人",
        "intent": "follow_up",
        "entities": ["抗日战争"],
        "filters": {},
        "needs_clarification": False,
        "clarification_question": "",
        "confidence": 0.9,
    })
    result = NativeRewriter().resolve(
        "主要领导人", WINDOW_4, "", ["抗日战争"], llm, mode="auto"
    )
    assert result.resolved_query == "抗日战争的主要领导人"
    assert result.intent is Intent.FOLLOW_UP
    assert result.failed is False


def test_small_model_follow_up_is_corrected_to_new_topic():
    llm = JsonLLM({
        "resolved_query": "解放战争中著名的战役",
        "intent": "follow_up",  # 小模型容易误判为追问
        "entities": ["解放战争"],
        "filters": {},
        "needs_clarification": False,
        "clarification_question": "",
        "confidence": 0.9,
    })
    result = NativeRewriter().resolve(
        "解放战争著名的战役", WINDOW_7, "", ["抗日战争"], llm, mode="auto"
    )
    assert result.resolved_query == "解放战争中著名的战役"
    assert result.intent is Intent.NEW_TOPIC


def test_new_topic_is_detected_without_entity_slots():
    llm = JsonLLM({
        "resolved_query": "解放战争中著名的战役",
        "intent": "follow_up",
        "entities": ["解放战争"],
        "filters": {},
        "needs_clarification": False,
        "clarification_question": "",
        "confidence": 0.9,
    })
    result = NativeRewriter().resolve(
        "解放战争著名的战役", WINDOW_7, "", [], llm, mode="auto"
    )
    assert result.intent is Intent.NEW_TOPIC


def test_ollama_chat_json_uses_format_json(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": '{"resolved_query":"x","intent":"new_topic"}'}}

    def fake_post(url, json=None, timeout=None):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("infra.ollama_chat.httpx.post", fake_post)
    client = OllamaChatClient(
        url="http://localhost:11434/api/chat",
        model="qwen3:4B-Instruct",
        timeout=30,
    )

    data = client.chat_json(
        "prompt", system="system", max_tokens=600, temperature=0.1
    )

    assert data == {"resolved_query": "x", "intent": "new_topic"}
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["json"]["model"] == "qwen3:4B-Instruct"
    assert captured["json"]["format"] == "json"
    assert captured["json"]["options"]["temperature"] == 0.1
    assert captured["json"]["options"]["num_predict"] == 600
    assert captured["json"]["messages"][0] == {"role": "system", "content": "system"}


class _SpyRewriter:
    def __init__(self):
        self.llm = None

    def resolve(self, question, window, summary, entities, llm,
                mode="auto", intent_hint=None):
        self.llm = llm
        return RewriteResult(
            resolved_query=question,
            intent=Intent.NEW_TOPIC,
            skipped=True,
        )

    def update_entity_slots(self, prev_entities, result, turn_id):
        return [], []


class _StubPipeline:
    def retrieve(self, question, options=None):
        from domain import SearchResult
        return SearchResult(hits=())

    def generate(self, question, search, options=None, *,
                 resolved_query=None, prefix_trace=()):
        from domain import Answer, CitationCheck
        return Answer(
            text="ok",
            sources=(),
            evidence=(),
            citations=CitationCheck(ok=True),
        )


def test_chat_service_routes_rewrite_to_aux_llm():
    rewriter = _SpyRewriter()
    main_llm = object()
    aux_llm = OllamaChatClient(model="qwen3:4B-Instruct")
    service = ChatService(
        _StubPipeline(), None, rewriter, rewrite_llm=aux_llm
    )

    service.chat("问题", main_llm)

    assert rewriter.llm is aux_llm
