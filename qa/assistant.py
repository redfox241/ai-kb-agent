"""AssistantService：workflow / assisted / agent 三种模式的统一门面。

主链路保持确定性 workflow；agent 模式只预留入口，不在此实现自主循环。
"""
from __future__ import annotations

from dataclasses import replace

from domain.errors import ModelUnavailable
from domain.models import Answer, AnswerMode, AnswerOptions


class AssistantService:
    def __init__(self, pipeline) -> None:
        self._pipeline = pipeline

    def answer(
        self,
        question: str,
        options: AnswerOptions | None = None,
        *,
        mode: AnswerMode = AnswerMode.WORKFLOW,
    ) -> Answer:
        if mode is AnswerMode.AGENT:
            raise ModelUnavailable(
                "Agent 模式未启用",
                detail={"mode": mode.value, "hint": "主链路保持 workflow"},
            )
        options = options or AnswerOptions()
        if mode is AnswerMode.ASSISTED:
            options = replace(options, rewrite=True)
        return self._pipeline.answer(question, options)
