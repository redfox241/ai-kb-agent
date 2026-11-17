"""滚动摘要与实体上下文维护（会话应用层用例）。"""
from __future__ import annotations

import os

from domain.ports import JobRunner, SessionRepository


def _summary_max_tokens() -> int:
    try:
        return max(400, int(os.environ.get("SUMMARY_MAX_TOKENS", "1200")))
    except (TypeError, ValueError):
        return 1200


class Summarizer:
    def __init__(
        self,
        sessions: SessionRepository,
        *,
        jobs: JobRunner | None = None,
        every: int = 6,
    ) -> None:
        self._sessions = sessions
        self._jobs = jobs
        self._every = max(1, int(every))

    def maybe_summarize(self, session_id: str, llm) -> None:
        if self._jobs is None:
            return
        session = self._sessions.get(session_id)
        if session is None:
            return
        if session.turn_count - session.summary_upto_turn < self._every:
            return

        def work() -> None:
            try:
                # 延迟导入，避免 conversations.summarizer <-> qa/__init__ 循环导入。
                from qa.context import (
                    SUMMARY_SYSTEM,
                    aux_thinking,
                    estimate_tokens,
                    summarize_prompt,
                )

                current = self._sessions.get(session_id)
                if current is None:
                    return
                upto = current.summary_upto_turn
                turns = [
                    turn for turn in self._sessions.window(session_id, self._every * 2)
                    if turn.turn_id > upto
                ]
                if len(turns) < self._every:
                    return
                prompt = summarize_prompt(current.summary, turns)

                summary = llm.chat(
                    [{"role": "user", "content": prompt}],
                    system=SUMMARY_SYSTEM,
                    max_tokens=_summary_max_tokens(),
                    temperature=0.2,
                    thinking=aux_thinking(),
                )
                if not (summary or "").strip():
                    # 部分模型会把预算全部用于 thinking，空输出时放宽预算再试一次。
                    summary = llm.chat(
                        [{"role": "user", "content": prompt}],
                        system=SUMMARY_SYSTEM,
                        max_tokens=max(_summary_max_tokens() * 2, 2400),
                        temperature=0.2,
                        thinking=aux_thinking(),
                    )
                summary = (summary or "").strip()[:800]
                if summary:
                    self._sessions.update_summary(
                        session_id,
                        summary,
                        turns[-1].turn_id,
                        summary_tokens=estimate_tokens(summary),
                    )
                else:
                    print(
                        f"[summary] empty session={session_id} upto={turns[-1].turn_id}",
                        flush=True,
                    )
            except Exception as exc:
                print(
                    f"[summary] failed session={session_id}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

        self._jobs.submit("summary", work)
