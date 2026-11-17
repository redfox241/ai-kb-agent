"""Analytics 持久化适配器：评估记录 + 访问记录（SQLite）。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    total REAL,
    accuracy REAL, relevance REAL, completeness REAL, clarity REAL, source_support REAL,
    reason TEXT,
    strengths TEXT,
    weaknesses TEXT,
    suggestion TEXT,
    port INTEGER,
    latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_eval_ts ON eval_records(ts);
CREATE TABLE IF NOT EXISTS visit_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'query',
    path TEXT,
    question TEXT,
    endpoint TEXT,
    n_sources INTEGER,
    n_steps INTEGER,
    latency_ms INTEGER,
    status TEXT,
    port INTEGER
);
CREATE INDEX IF NOT EXISTS idx_visit_ts ON visit_records(ts);
CREATE INDEX IF NOT EXISTS idx_visit_kind ON visit_records(kind);
"""

_EVAL_KEYS = ["id", "ts", "question", "answer", "total", "accuracy", "relevance",
              "completeness", "clarity", "source_support", "reason", "strengths",
              "weaknesses", "suggestion", "port", "latency_ms"]
_VISIT_KEYS = ["id", "ts", "kind", "path", "question", "endpoint", "n_sources",
               "n_steps", "latency_ms", "status", "port"]


def default_db_path() -> Path:
    base = Path(__file__).resolve().parents[1]
    return Path(os.environ.get("RAG_EVAL_DB") or base / "var" / "db" / "analytics.db")


class SqliteAnalyticsRepository:
    """实现 domain.ports.AnalyticsRepository。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self._lock = threading.Lock()

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path)

    def migrate(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.execute("DELETE FROM visit_records WHERE ts < date('now','-90 day')")
            conn.commit()
        finally:
            conn.close()

    # ---------- 评估 ----------

    def record_eval(self, question, answer, evaluation, port=None, latency_ms=None, sources=None):
        if not evaluation:
            return
        dims = evaluation.get("dimensions") or {}

        def score(key):
            return (dims.get(key) or {}).get("score")

        conn = self._connect()
        try:
            with self._lock:
                conn.execute(
                    """INSERT INTO eval_records
                       (ts, question, answer, total, accuracy, relevance, completeness,
                        clarity, source_support, reason, strengths, weaknesses, suggestion,
                        port, latency_ms)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (time.strftime("%Y-%m-%dT%H:%M:%S"),
                     question, (answer or "")[:4000], evaluation.get("total"),
                     score("accuracy"), score("relevance"), score("completeness"),
                     score("clarity"), score("sources"),
                     evaluation.get("reason", ""),
                     json.dumps(evaluation.get("strengths") or [], ensure_ascii=False),
                     json.dumps(evaluation.get("weaknesses") or [], ensure_ascii=False),
                     evaluation.get("suggestion", ""),
                     port, latency_ms))
                conn.commit()
        finally:
            conn.close()

    def stats(self):
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM eval_records").fetchone()[0]
            row = conn.execute(
                """SELECT AVG(total), AVG(accuracy), AVG(relevance), AVG(completeness),
                          AVG(clarity), AVG(source_support) FROM eval_records""").fetchone()
            avg = {k: round(v, 2) if v is not None else None
                   for k, v in zip(["total", "accuracy", "relevance", "completeness",
                                    "clarity", "source_support"], row)}
            grades = {"good": 0, "mid": 0, "bad": 0}
            for (value,) in conn.execute("SELECT total FROM eval_records"):
                if value is None:
                    continue
                if value >= 8:
                    grades["good"] += 1
                elif value >= 6:
                    grades["mid"] += 1
                else:
                    grades["bad"] += 1
            trend = conn.execute(
                """SELECT substr(ts,1,10) AS day, COUNT(*), AVG(total)
                   FROM eval_records GROUP BY day ORDER BY day""").fetchall()
            trend = [{"day": day, "count": count,
                      "avg_total": round(avg_value, 2) if avg_value else None}
                     for day, count, avg_value in trend]
            return {"total": total, "avg": avg, "grades": grades, "trend": trend}
        finally:
            conn.close()

    def history(self, limit=50, min_total=None, max_total=None, offset=0, keyword=None):
        conn = self._connect()
        try:
            sql = "SELECT * FROM eval_records WHERE 1=1"
            args: list[object] = []
            if min_total is not None:
                sql += " AND total >= ?"
                args.append(min_total)
            if max_total is not None:
                sql += " AND total <= ?"
                args.append(max_total)
            if keyword:
                sql += " AND (question LIKE ? OR answer LIKE ?)"
                args += [f"%{keyword}%", f"%{keyword}%"]
            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            args += [limit, offset]
            rows = conn.execute(sql, args).fetchall()
            return [self._eval_row(row) for row in rows]
        finally:
            conn.close()

    def low(self, min_total=6, limit=20):
        return self.history(limit=limit, max_total=min_total)

    @staticmethod
    def _eval_row(row):
        data = dict(zip(_EVAL_KEYS, row))
        for key in ("strengths", "weaknesses"):
            try:
                data[key] = json.loads(data.get(key) or "[]")
            except Exception:
                data[key] = []
        return data

    # ---------- 访问 ----------

    def record_visit(self, kind="query", question=None, path=None, endpoint=None,
                     n_sources=None, n_steps=None, latency_ms=None,
                     status="ok", port=None):
        conn = self._connect()
        try:
            with self._lock:
                conn.execute(
                    """INSERT INTO visit_records
                       (ts, kind, path, question, endpoint, n_sources, n_steps,
                        latency_ms, status, port)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (time.strftime("%Y-%m-%dT%H:%M:%S"), kind, path,
                     (question or "")[:500], endpoint, n_sources, n_steps,
                     latency_ms, status, port))
                conn.commit()
        finally:
            conn.close()

    def visits(self, limit=50, offset=0, kind=None, keyword=None):
        conn = self._connect()
        try:
            sql = "SELECT * FROM visit_records WHERE 1=1"
            args: list[object] = []
            if kind in ("query", "page"):
                sql += " AND kind = ?"
                args.append(kind)
            if keyword:
                sql += " AND (question LIKE ? OR path LIKE ? OR endpoint LIKE ?)"
                like = f"%{keyword}%"
                args += [like, like, like]
            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            args += [limit, offset]
            return [dict(zip(_VISIT_KEYS, row))
                    for row in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def visit_stats(self):
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM visit_records").fetchone()[0]
            by_kind = {"query": 0, "page": 0}
            for kind, count in conn.execute(
                    "SELECT kind, COUNT(*) FROM visit_records GROUP BY kind"):
                by_kind[kind or "query"] = count
            today = conn.execute(
                "SELECT COUNT(*) FROM visit_records WHERE substr(ts,1,10)=date('now')"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT AVG(latency_ms), MAX(latency_ms) FROM visit_records "
                "WHERE kind='query' AND latency_ms IS NOT NULL").fetchone()
            errors = conn.execute(
                "SELECT COUNT(*) FROM visit_records WHERE status != 'ok'").fetchone()[0]
            trend = conn.execute(
                """SELECT substr(ts,1,10) AS day, COUNT(*),
                          SUM(CASE WHEN kind='query' THEN 1 ELSE 0 END)
                   FROM visit_records GROUP BY day ORDER BY day DESC LIMIT 30""").fetchall()
            trend = [{"day": day, "count": count, "query": queries or 0}
                     for day, count, queries in trend][::-1]
            return {"total": total, "by_kind": by_kind, "today": today,
                    "avg_latency_ms": round(row[0]) if row and row[0] is not None else None,
                    "max_latency_ms": row[1] if row else None,
                    "errors": errors, "trend": trend}
        finally:
            conn.close()
