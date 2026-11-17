"""Trace 事件构建（应用层）：只产出领域事件，不做展示渲染。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from domain.models import Chunk, StepStatus, TraceEvent


@dataclass
class TraceBuilder:
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    events: list[TraceEvent] = field(default_factory=list)

    def add(
        self,
        step: str,
        status: StepStatus = StepStatus.OK,
        *,
        summary: str = "",
        detail: str = "",
        items: Sequence[object] | None = None,
        ms: int = 0,
        ops: Sequence[object] | None = None,
        io: Mapping[str, object] | None = None,
        title: str | None = None,
    ) -> TraceEvent:
        attributes: dict[str, object] = {
            "summary": str(summary),
            "detail": str(detail),
            "items": list(items or []),
            "ops": [str(item) for item in (ops or [])],
            "io": dict(io or {}),
        }
        if title:
            attributes["title"] = title
        event = TraceEvent(
            trace_id=self.trace_id,
            step=step,
            status=status,
            started_at=time.time(),
            duration_ms=int(ms or 0),
            input_summary=str(summary),
            output_summary=str(summary),
            attributes=attributes,
        )
        self.events.append(event)
        return event

    def extend(self, events: Sequence[TraceEvent]) -> None:
        self.events.extend(events)

    @property
    def trace(self) -> tuple[TraceEvent, ...]:
        return tuple(self.events)


def io_item(label: object, value: object, meta: object = "") -> dict:
    return {
        "label": str(label),
        "value": str(value if value is not None else ""),
        "meta": str(meta or ""),
    }


def _chunk_title(chunk: object) -> str:
    if isinstance(chunk, Chunk):
        return chunk.title
    if isinstance(chunk, Mapping):
        return str(chunk.get("title") or "")
    return str(getattr(chunk, "title", "") or "")


def _chunk_type(chunk: object) -> str:
    if isinstance(chunk, Chunk):
        return chunk.kind.value
    if isinstance(chunk, Mapping):
        value = chunk.get("type") or chunk.get("kind") or ""
        return str(getattr(value, "value", value))
    kind = getattr(chunk, "kind", "")
    return str(getattr(kind, "value", kind))


def _chunk_path(chunk: object) -> str:
    if isinstance(chunk, Chunk):
        return chunk.doc_path
    if isinstance(chunk, Mapping):
        return str(chunk.get("doc_path") or "")
    return str(getattr(chunk, "doc_path", "") or "")


def _chunk_text(chunk: object) -> str:
    if isinstance(chunk, Chunk):
        return chunk.text
    if isinstance(chunk, Mapping):
        return str(chunk.get("text") or "")
    return str(getattr(chunk, "text", "") or "")


def context_io(question: str, selected: Sequence[object], context: str) -> dict:
    total_chars = sum(len(_chunk_text(chunk)) for chunk in selected)
    groups: dict[str, int] = {}
    for chunk in selected:
        path = _chunk_path(chunk)
        groups[path] = groups.get(path, 0) + 1

    input_rows = [
        io_item("用户问题", question),
        io_item("输入知识块", f"{len(selected)} 块 · 正文合计 {total_chars} 字符",
                "来自父子捆绑扩展"),
    ]
    for index, chunk in enumerate(selected, 1):
        text = _chunk_text(chunk)
        input_rows.append(io_item(
            f"#{index} {_chunk_title(chunk)}",
            text[:160] + ("…" if len(text) > 160 else ""),
            f"{_chunk_type(chunk)} · {_chunk_path(chunk)} · {len(text)} 字",
        ))

    pending_rows = [
        io_item("待添加来源行", f"{len(selected)} 条",
                "格式：[来源: 标题 | 类型 | 路径 | 时间]"),
        io_item("待拼接正文", f"{total_chars} 字符",
                f"{len(selected)} 块按父子扩展顺序拼接，块间空行分隔"),
        io_item("文件分组", f"{len(groups)} 个文件",
                "；".join(f"{path}（{count} 块）" for path, count in groups.items())),
    ]

    result_rows = []
    for index, chunk in enumerate(selected, 1):
        created = getattr(chunk, "created", "") if isinstance(chunk, Chunk) else ""
        time_tag = f" | {created}" if created else ""
        result_rows.append(io_item(
            f"#{index} 来源行",
            f"[来源: {_chunk_title(chunk)} | {_chunk_type(chunk)} | {_chunk_path(chunk)}{time_tag}]",
            f"已拼接正文 {len(_chunk_text(chunk))} 字",
        ))

    return {
        "input": input_rows,
        "pending": pending_rows,
        "result": result_rows,
        "output": [io_item("上下文全文", context,
                           f"{len(context)} 字符 · {len(selected)} 块 · 可直接作为 LLM 输入")],
    }


def generation_io(
    question: str,
    context: str,
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    *,
    answer: str = "",
    elapsed_ms: int | None = None,
    sources: Sequence[object] = (),
    error: Exception | None = None,
    context_meta: str = "",
) -> dict:
    input_rows = [
        io_item("用户问题", question),
        io_item("检索上下文", f"{len(context)} 字符", context_meta or "由上下文构建产出"),
        io_item("System Prompt", system_prompt, "参与本次生成的系统提示词全文"),
        io_item("模型参数", f"model={model} · max_tokens={max_tokens}",
                f"上下文 {len(context)} 字符 · 问题 {len(question)} 字符"),
    ]
    pending_rows = [
        io_item("User Prompt", user_prompt, f"{len(user_prompt)} 字符"),
        io_item("待调用模型", model,
                f"system prompt {len(system_prompt)} 字符 · max_tokens={max_tokens}"),
        io_item("待抽取来源", f"{len(sources)} 条", "生成后从选中知识块按文件去重"),
    ]

    if error is not None:
        result_rows = [
            io_item("调用状态", "失败", type(error).__name__),
            io_item("异常信息", str(error), "本次未产生答案"),
        ]
        output_rows = [io_item("最终输出", "生成失败，接口抛出异常", "无答案返回")]
    else:
        result_rows = [
            io_item("模型返回统计",
                    f"{len(answer or '')} 字" + (f" · {elapsed_ms}ms" if elapsed_ms is not None else ""),
                    "完整内容见下方「输出」"),
            io_item("来源抽取", f"{len(sources)} 条", "按文件去重"),
        ]
        output_rows = [
            io_item("最终答案", answer or "", f"{len(answer or '')} 字 · 已随响应返回"),
            io_item("参考来源", f"{len(sources)} 条", "已随响应 sources 字段返回"),
        ]

    return {
        "input": input_rows,
        "pending": pending_rows,
        "result": result_rows,
        "output": output_rows,
    }
