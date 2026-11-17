"""问答层：上下文、引用、流水线与多轮编排。"""
from .assistant import AssistantService
from .citation import citations_from_answer, followup_suggestions, validate_citations
from .context import build, build_context, estimate_tokens, format_evidence, summarize_prompt
from .multiturn import ChatResult, ChatService
from .pipeline import RagPipeline
from .rewrite import NativeRewriter

__all__ = [
    "AssistantService",
    "citations_from_answer",
    "followup_suggestions",
    "validate_citations",
    "build",
    "build_context",
    "estimate_tokens",
    "format_evidence",
    "summarize_prompt",
    "ChatResult",
    "ChatService",
    "RagPipeline",
    "NativeRewriter",
]
