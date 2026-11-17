"""Trace 展示层：把 TraceEvent 渲染成前端 steps 结构。"""
from __future__ import annotations

from typing import Sequence

from domain.models import StepStatus, TraceEvent


STEP_TITLES = {
    "accept": "查询受理",
    "resolve": "上下文解析",
    "bm25": "BM25 关键词检索",
    "semantic": "语义向量检索",
    "rewrite": "查询改写",
    "merge": "三路融合排序",
    "expand": "父子捆绑扩展",
    "context": "上下文构建",
    "generate": "答案生成",
    "cite": "引用校验",
    "persist": "会话落库",
    "clarify": "请求澄清",
}

STATUS_ICONS = {
    StepStatus.OK: "✅",
    StepStatus.EMPTY: "⚠️",
    StepStatus.SKIP: "⏭️",
    StepStatus.ERROR: "❌",
    StepStatus.DEGRADED: "⚠️",
}


class TracePresenter:
    """把 TraceEvent 渲染成前端既有的 steps 结构。"""

    def render(self, events: Sequence[TraceEvent]) -> list[dict]:
        return [self.render_event(event) for event in events]

    def render_event(self, event: TraceEvent) -> dict:
        attributes = event.attributes or {}
        status = event.status.value if isinstance(event.status, StepStatus) else str(event.status)
        icon = STATUS_ICONS.get(event.status, "•") if isinstance(event.status, StepStatus) else "•"
        return {
            "key": event.step,
            "title": str(attributes.get("title") or STEP_TITLES.get(event.step, event.step)),
            "status": status,
            "icon": icon,
            "summary": str(attributes.get("summary", event.input_summary or "")),
            "detail": str(attributes.get("detail", "")),
            "items": list(attributes.get("items") or []),
            "ms": int(event.duration_ms or 0),
            "ops": list(attributes.get("ops") or []),
            "io": dict(attributes.get("io") or {}),
        }
