"""原生 SQLite SessionRepository：会话、消息、实体、轨迹。"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence

from domain.models import (
    Citation,
    Intent,
    SessionState,
    SessionSummary,
    Turn,
)


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=3000;

CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id        TEXT PRIMARY KEY,
  channel           INTEGER NOT NULL DEFAULT 0,
  title             TEXT DEFAULT '',
  summary           TEXT DEFAULT '',
  summary_upto_turn INTEGER DEFAULT 0,
  summary_tokens    INTEGER DEFAULT 0,
  turn_count        INTEGER DEFAULT 0,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);

CREATE TABLE IF NOT EXISTS messages (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id     TEXT NOT NULL,
  turn_id        INTEGER NOT NULL,
  role           TEXT NOT NULL,
  content        TEXT NOT NULL,
  resolved_query TEXT,
  intent         TEXT,
  citations_json TEXT,
  evidence_json  TEXT,
  created_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_turn
  ON messages(session_id, turn_id, role);
CREATE INDEX IF NOT EXISTS idx_messages_session
  ON messages(session_id, turn_id);

CREATE TABLE IF NOT EXISTS session_entities (
  session_id  TEXT NOT NULL,
  entity_name TEXT NOT NULL,
  doc_path    TEXT DEFAULT '',
  hits        INTEGER DEFAULT 1,
  last_turn   INTEGER NOT NULL,
  PRIMARY KEY(session_id, entity_name)
);

CREATE TABLE IF NOT EXISTS turn_traces (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id          TEXT NOT NULL,
  turn_id             INTEGER NOT NULL,
  channel             INTEGER,
  raw_query           TEXT,
  resolved_query      TEXT,
  intent              TEXT,
  confidence          REAL,
  needs_clarification INTEGER DEFAULT 0,
  rewrite_failed      INTEGER DEFAULT 0,
  entities_json       TEXT,
  retrieved_json      TEXT,
  prompt_tokens       INTEGER,
  completion_tokens   INTEGER,
  latency_ms          INTEGER,
  answer_hash         TEXT,
  flags_json          TEXT,
  created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_session ON turn_traces(session_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_traces_ts ON turn_traces(created_at);
"""

_TRACE_COLUMNS = (
    "channel", "raw_query", "resolved_query", "intent", "confidence",
    "needs_clarification", "rewrite_failed", "entities_json",
    "retrieved_json", "prompt_tokens", "completion_tokens", "latency_ms",
    "answer_hash", "flags_json",
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _dump(value: object) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _load(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except Exception:
        return default


def _intent(value: object) -> Intent:
    try:
        return Intent(str(value))
    except ValueError:
        return Intent.NEW_TOPIC


class SqliteSessionRepository:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._write_lock = threading.Lock()

    # ---------- 基础 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def migrate(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5)
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (1, _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def valid_session_id(self, session_id: str) -> bool:
        return bool(session_id) and bool(SESSION_ID_RE.match(session_id))

    def _state(self, row: sqlite3.Row | None) -> SessionState | None:
        if row is None:
            return None
        return SessionState(
            session_id=str(row["session_id"]),
            channel=int(row["channel"] or 0),
            title=str(row["title"] or ""),
            summary=str(row["summary"] or ""),
            summary_upto_turn=int(row["summary_upto_turn"] or 0),
            turn_count=int(row["turn_count"] or 0),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    # ---------- 会话 ----------

    def create(self, channel: int = 0, title: str = "", session_id: str | None = None) -> SessionState:
        sid = session_id if session_id and self.valid_session_id(session_id) else f"ses_{secrets.token_hex(6)}"
        ts = _now()
        conn = self._connect()
        try:
            with self._write_lock:
                conn.execute(
                    """INSERT INTO sessions(session_id, channel, title, created_at, updated_at)
                       VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO NOTHING""",
                    (sid, int(channel or 0), (title or "")[:80], ts, ts),
                )
                conn.commit()
        finally:
            conn.close()
        state = self.get(sid)
        assert state is not None
        return state

    def get(self, session_id: str) -> SessionState | None:
        if not self.valid_session_id(session_id):
            return None
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return self._state(row)
        finally:
            conn.close()

    def get_or_create(self, session_id: str, channel: int) -> SessionState:
        state = self.get(session_id)
        if state is not None:
            return state
        return self.create(channel=channel, session_id=session_id)

    def list_sessions(self, limit: int = 20, offset: int = 0,
                      channel: int | None = None) -> list[SessionSummary]:
        conn = self._connect()
        try:
            sql = """SELECT session_id, channel, title, turn_count, updated_at, created_at
                     FROM sessions"""
            args: list[object] = []
            if channel is not None:
                sql += " WHERE channel=?"
                args.append(int(channel))
            sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            args += [int(limit), int(offset)]
            return [
                SessionSummary(
                    session_id=str(row["session_id"]),
                    channel=int(row["channel"] or 0),
                    title=str(row["title"] or ""),
                    turn_count=int(row["turn_count"] or 0),
                    updated_at=str(row["updated_at"] or ""),
                    created_at=str(row["created_at"] or ""),
                )
                for row in conn.execute(sql, args).fetchall()
            ]
        finally:
            conn.close()

    def rename(self, session_id: str, title: str) -> bool:
        conn = self._connect()
        try:
            with self._write_lock:
                cur = conn.execute(
                    "UPDATE sessions SET title=?, updated_at=? WHERE session_id=?",
                    ((title or "")[:80], _now(), session_id),
                )
                conn.commit()
                return cur.rowcount > 0
        finally:
            conn.close()

    def reset(self, session_id: str) -> None:
        conn = self._connect()
        try:
            with self._write_lock:
                conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM session_entities WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM turn_traces WHERE session_id=?", (session_id,))
                conn.execute(
                    """UPDATE sessions SET summary='', summary_upto_turn=0, summary_tokens=0,
                       turn_count=0, updated_at=? WHERE session_id=?""",
                    (_now(), session_id),
                )
                conn.commit()
        finally:
            conn.close()

    def delete(self, session_id: str) -> None:
        conn = self._connect()
        try:
            with self._write_lock:
                for table in ("messages", "session_entities", "turn_traces", "sessions"):
                    conn.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
                conn.commit()
        finally:
            conn.close()

    # ---------- 轮次 ----------

    def append_turn(self, turn: Turn) -> Turn:
        if not turn.session_id:
            raise ValueError("append_turn 需要 turn.session_id")
        ts = _now()
        conn = self._connect()
        try:
            with self._write_lock:
                cur = conn.execute(
                    """UPDATE sessions SET turn_count=turn_count+1, updated_at=?, channel=?
                       WHERE session_id=?""",
                    (ts, int(turn.channel or 0), turn.session_id),
                )
                if cur.rowcount == 0:
                    conn.execute(
                        """INSERT INTO sessions(session_id, channel, title, turn_count,
                           created_at, updated_at) VALUES (?,?,?,1,?,?)""",
                        (turn.session_id, int(turn.channel or 0), (turn.question or "")[:80], ts, ts),
                    )
                turn_id = int(conn.execute(
                    "SELECT turn_count FROM sessions WHERE session_id=?", (turn.session_id,)
                ).fetchone()[0])
                conn.execute(
                    """INSERT INTO messages(session_id, turn_id, role, content,
                       resolved_query, intent, created_at) VALUES (?,?,?,?,?,?,?)""",
                    (turn.session_id, turn_id, "user", turn.question,
                     turn.resolved_query, turn.intent.value, ts),
                )
                conn.execute(
                    """INSERT INTO messages(session_id, turn_id, role, content,
                       citations_json, evidence_json, created_at) VALUES (?,?,?,?,?,?,?)""",
                    (turn.session_id, turn_id, "assistant", turn.answer,
                     _dump([
                         {"id": c.id, "title": c.title, "doc_path": c.doc_path,
                          "chunk_id": c.chunk_id}
                         for c in turn.citations
                     ]),
                     _dump(list(turn.evidence_ids)), ts),
                )
                conn.commit()
        finally:
            conn.close()
        return Turn(
            turn_id=turn_id,
            session_id=turn.session_id,
            channel=turn.channel,
            question=turn.question,
            resolved_query=turn.resolved_query,
            intent=turn.intent,
            answer=turn.answer,
            citations=turn.citations,
            evidence_ids=turn.evidence_ids,
            created_at=ts,
        )

    def window(self, session_id: str, turns: int) -> list[Turn]:
        if not self.valid_session_id(session_id):
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM (
                     SELECT turn_id, role, content, resolved_query, intent, created_at, id
                     FROM messages WHERE session_id=?
                     ORDER BY turn_id DESC, id DESC LIMIT ?
                   ) ORDER BY turn_id ASC, id ASC""",
                (session_id, max(int(turns) * 4, 12)),
            ).fetchall()
        finally:
            conn.close()

        by_turn: dict[int, dict] = {}
        for row in rows:
            turn_id = int(row["turn_id"])
            slot = by_turn.setdefault(turn_id, {"turn_id": turn_id})
            if row["role"] == "user":
                slot["question"] = str(row["content"] or "")
                slot["resolved_query"] = str(row["resolved_query"] or row["content"] or "")
                slot["intent"] = row["intent"]
            elif row["role"] == "assistant":
                slot["answer"] = str(row["content"] or "")
                slot["created_at"] = str(row["created_at"] or "")

        result = []
        for _, slot in sorted(by_turn.items()):
            if slot.get("question") and slot.get("answer"):
                result.append(Turn(
                    turn_id=int(slot["turn_id"]),
                    session_id=session_id,
                    question=str(slot.get("question") or ""),
                    resolved_query=str(slot.get("resolved_query") or ""),
                    intent=_intent(slot.get("intent")),
                    answer=str(slot.get("answer") or ""),
                    created_at=str(slot.get("created_at") or ""),
                ))
        return result[-turns:]

    def messages(self, session_id: str, limit: int = 40) -> list[dict]:
        """兼容旧 /api/sessions/{id} 的消息列表结构。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM (
                     SELECT turn_id, role, content, resolved_query, intent,
                            citations_json, created_at, id
                     FROM messages WHERE session_id=?
                     ORDER BY turn_id DESC, id DESC LIMIT ?
                   ) ORDER BY turn_id ASC, id ASC""",
                (session_id, int(limit)),
            ).fetchall()
        finally:
            conn.close()
        result = []
        for row in rows:
            item = {key: row[key] for key in row.keys() if key != "id"}
            item["citations"] = _load(item.pop("citations_json", None), [])
            result.append(item)
        return result

    def recent_doc_paths(self, session_id: str, turns: int) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT citations_json FROM messages
                   WHERE session_id=? AND role='assistant' AND citations_json IS NOT NULL
                   ORDER BY id DESC LIMIT ?""",
                (session_id, int(turns)),
            ).fetchall()
        finally:
            conn.close()
        paths: list[str] = []
        for row in rows:
            for citation in _load(row["citations_json"], []):
                path = (citation or {}).get("doc_path") or ""
                if path and path not in paths:
                    paths.append(path)
        return paths

    def recent_chunk_ids(self, session_id: str, turns: int) -> set[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT evidence_json FROM messages
                   WHERE session_id=? AND role='assistant' AND evidence_json IS NOT NULL
                   ORDER BY id DESC LIMIT ?""",
                (session_id, int(turns)),
            ).fetchall()
        finally:
            conn.close()
        ids: set[str] = set()
        for row in rows:
            ids.update(item for item in _load(row["evidence_json"], []) if isinstance(item, str))
        return ids

    # ---------- 摘要 / 实体 ----------

    def update_summary(self, session_id: str, summary: str, upto_turn: int,
                       summary_tokens: int | None = None) -> None:
        conn = self._connect()
        try:
            with self._write_lock:
                conn.execute(
                    """UPDATE sessions
                       SET summary=?, summary_upto_turn=?, summary_tokens=?, updated_at=?
                       WHERE session_id=?""",
                    (
                        summary or "",
                        int(upto_turn or 0),
                        int(summary_tokens) if summary_tokens is not None else 0,
                        _now(),
                        session_id,
                    ),
                )
                conn.commit()
        finally:
            conn.close()

    def entities(self, session_id: str, limit: int = 8) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT entity_name FROM session_entities WHERE session_id=?
                   ORDER BY last_turn DESC, hits DESC LIMIT ?""",
                (session_id, int(limit)),
            ).fetchall()
            return [str(row[0]) for row in rows]
        finally:
            conn.close()

    def upsert_entities(self, session_id: str, entities: Sequence[str], turn_id: int,
                        doc_paths: Mapping[str, str] | None = None) -> None:
        doc_paths = doc_paths or {}
        conn = self._connect()
        try:
            with self._write_lock:
                for name in entities or []:
                    name = str(name).strip()
                    if not name:
                        continue
                    conn.execute(
                        """INSERT INTO session_entities(session_id, entity_name, doc_path, hits, last_turn)
                           VALUES (?,?,?,1,?)
                           ON CONFLICT(session_id, entity_name) DO UPDATE SET
                             hits=hits+1, last_turn=excluded.last_turn,
                             doc_path=CASE WHEN excluded.doc_path!='' THEN excluded.doc_path
                                           ELSE session_entities.doc_path END""",
                        (session_id, name[:80], (doc_paths.get(name) or "")[:300], int(turn_id)),
                    )
                conn.commit()
        finally:
            conn.close()

    def delete_entities(self, session_id: str, entities: Sequence[str]) -> None:
        if not entities:
            return
        conn = self._connect()
        try:
            with self._write_lock:
                for name in entities:
                    conn.execute(
                        "DELETE FROM session_entities WHERE session_id=? AND entity_name=?",
                        (session_id, str(name)[:80]),
                    )
                conn.commit()
        finally:
            conn.close()

    def prune_entities(self, session_id: str, keep_turns: int = 10) -> None:
        conn = self._connect()
        try:
            with self._write_lock:
                conn.execute(
                    """DELETE FROM session_entities WHERE session_id=? AND last_turn <
                       (SELECT MAX(turn_count) FROM sessions WHERE session_id=?) - ?""",
                    (session_id, session_id, int(keep_turns)),
                )
                conn.commit()
        finally:
            conn.close()

    # ---------- 轨迹 / 导出 ----------

    def record_trace(self, session_id: str, turn_id: int, payload: Mapping[str, object]) -> None:
        data = {key: payload.get(key) for key in _TRACE_COLUMNS}
        data["needs_clarification"] = 1 if data.get("needs_clarification") else 0
        data["rewrite_failed"] = 1 if data.get("rewrite_failed") else 0
        data["entities_json"] = _dump(payload.get("entities") or [])
        data["retrieved_json"] = _dump(payload.get("retrieved") or [])
        data["flags_json"] = _dump(payload.get("flags") or {})
        conn = self._connect()
        try:
            with self._write_lock:
                conn.execute(
                    """INSERT INTO turn_traces(session_id, turn_id, channel, raw_query,
                       resolved_query, intent, confidence, needs_clarification, rewrite_failed,
                       entities_json, retrieved_json, prompt_tokens, completion_tokens,
                       latency_ms, answer_hash, flags_json, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (session_id, turn_id, data["channel"], data["raw_query"],
                     data["resolved_query"], data["intent"], data["confidence"],
                     data["needs_clarification"], data["rewrite_failed"],
                     data["entities_json"], data["retrieved_json"], data["prompt_tokens"],
                     data["completion_tokens"], data["latency_ms"], data["answer_hash"],
                     data["flags_json"], _now()),
                )
                conn.commit()
        finally:
            conn.close()

    def traces(self, session_id: str, limit: int = 50) -> list[Mapping[str, object]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM turn_traces WHERE session_id=? ORDER BY turn_id DESC LIMIT ?",
                (session_id, int(limit)),
            ).fetchall()
        finally:
            conn.close()
        result = []
        for row in rows:
            data = dict(row)
            data["entities"] = _load(data.pop("entities_json", None), [])
            data["retrieved"] = _load(data.pop("retrieved_json", None), [])
            data["flags"] = _load(data.pop("flags_json", None), {})
            result.append(data)
        return result

    def export_markdown(self, session_id: str) -> str:
        session = self.get(session_id) or None
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT role, content FROM messages WHERE session_id=?
                   ORDER BY turn_id ASC, id ASC LIMIT 400""",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        lines = [
            f"# {session.title if session else session_id}", "",
            f"- 会话：`{session_id}`",
            f"- 轮次：{session.turn_count if session else 0}",
            f"- 更新时间：{session.updated_at if session else ''}", "",
        ]
        for row in rows:
            lines += ["## 用户" if row["role"] == "user" else "## 助手", "", str(row["content"]), ""]
        return "\n".join(lines)

    def purge(self, days_messages: int = 180, days_traces: int = 30,
              days_sessions: int = 90) -> int:
        conn = self._connect()
        try:
            with self._write_lock:
                conn.execute("DELETE FROM messages WHERE created_at < date('now', ?)",
                             (f"-{int(days_messages)} day",))
                conn.execute(
                    """DELETE FROM turn_traces WHERE created_at < date('now', ?) OR id NOT IN (
                         SELECT id FROM turn_traces ORDER BY id DESC LIMIT 2000)""",
                    (f"-{int(days_traces)} day",),
                )
                stale = [row[0] for row in conn.execute(
                    "SELECT session_id FROM sessions WHERE updated_at < date('now', ?)",
                    (f"-{int(days_sessions)} day",),
                ).fetchall()]
                for sid in stale:
                    for table in ("messages", "session_entities", "turn_traces", "sessions"):
                        conn.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
                conn.commit()
                return len(stale)
        finally:
            conn.close()

    def stats(self, channel: int | None = None, days: int = 30) -> Mapping[str, object]:
        conn = self._connect()
        try:
            where, args = "", []
            if channel is not None:
                where = " WHERE channel=?"
                args.append(int(channel))
            row = conn.execute(
                f"""SELECT COUNT(*), COALESCE(AVG(turn_count),0), COALESCE(SUM(turn_count),0)
                    FROM sessions{where}""", args,
            ).fetchone()
            n_sessions, avg_turns, total_turns = row[0], round(row[1], 2), row[2] or 0
            query_where = " WHERE channel=?" if channel is not None else ""
            n_multi = conn.execute(
                f"""SELECT COUNT(*) FROM turn_traces{query_where}
                    {'AND' if channel is not None else 'WHERE'} turn_id > 1""",
                args,
            ).fetchone()[0]
            n_failed = conn.execute(
                f"""SELECT COUNT(*) FROM turn_traces{query_where}
                    {'AND' if channel is not None else 'WHERE'} rewrite_failed=1""",
                args,
            ).fetchone()[0]
            return {
                "sessions": n_sessions,
                "avg_turns": avg_turns,
                "total_turns": total_turns,
                "followup_turns": n_multi,
                "rewrite_failed": n_failed,
                "followup_rate": round(n_multi / total_turns, 3) if total_turns else 0.0,
            }
        finally:
            conn.close()
