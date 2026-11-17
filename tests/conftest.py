"""共享测试夹具：假 LLM、假 Embedding、假索引、临时会话库。

设计约束：
- 不触网：所有 LLM / Embedding 调用由 fake 替代。
- 不污染真实数据：会话库指向 pytest 的 tmp_path。
- 不改业务代码：通过 monkeypatch 替换 server 的全局钩子，测试结束后恢复。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须在 import server 之前设置，避免会话库指向项目根。
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="aikb-tests-"))
os.environ.setdefault("RAG_SESSION_DB", str(_TEST_DATA_DIR / "sessions.db"))
os.environ.setdefault("RAG_EVAL_DB", str(_TEST_DATA_DIR / "analytics.db"))
# 测试默认不走本地 Ollama；需要真实 Qwen 的测试直接实例化 OllamaChatClient。
os.environ.setdefault("LLM_REWRITE_PROVIDER", "main")

import bootstrap as server  # noqa: E402
from infra.sqlite_sessions import SqliteSessionRepository  # noqa: E402
from domain import IndexStatus, ReindexProgress  # noqa: E402
from infra.index_files import chunk_from_dict  # noqa: E402
from ingestion.index_service import IndexSnapshot  # noqa: E402
from retrieval.bm25 import BM25Index as NewBM25Index  # noqa: E402


class FakeLLM:
    """确定性的假模型：改写返回 JSON，生成返回带引用的固定答案。"""

    model = "fake"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat(self, messages, system=None, max_tokens=None, temperature=None,
             thinking=None):
        self.calls.append({"messages": messages, "system": system or ""})
        system = system or ""
        user = messages[-1]["content"] if messages else ""

        if "查询改写" in system:
            resolved = _resolve_rewrite(user)
            return json.dumps({
                "resolved_query": resolved,
                "intent": "follow_up" if resolved != user else "new_topic",
                "entities": _entities_for(resolved),
                "filters": {},
                "needs_clarification": False,
                "clarification_question": "",
                "confidence": 0.92,
            }, ensure_ascii=False)

        if "压缩" in system or "摘要" in system:
            return "会话摘要：用户询问孙悟空及其师承。"

        return "孙悟空的师父是须菩提祖师。[E1]"


def _resolve_rewrite(user: str) -> str:
    """只解析“当前用户输入”，避免被历史里的“他师父”抢先命中。"""
    current = user
    if "## 当前用户输入" in user:
        current = user.split("## 当前用户输入", 1)[-1]
        current = current.split("请按要求", 1)[0]
    current = current.strip()

    if "那猪八戒" in current:
        return "猪八戒的师父是谁？"
    if "他师父" in current:
        return "孙悟空的师父是谁？"
    if "问题：" in current:
        return current.split("问题：", 1)[-1].strip()
    return current


def _entities_for(text: str) -> list[str]:
    if "猪八戒" in text:
        return ["猪八戒"]
    if "孙悟空" in text:
        return ["孙悟空"]
    return []


FAKE_CHUNKS = [
    {
        "chunk_id": "wiki/sources/孙悟空.md#summary",
        "doc_path": "wiki/sources/孙悟空.md",
        "title": "孙悟空",
        "type": "summary",
        "text": "孙悟空，师父须菩提祖师。",
        "tags": [],
        "created": "2026-01-01",
    },
    {
        "chunk_id": "wiki/sources/孙悟空.md#detail0",
        "doc_path": "wiki/sources/孙悟空.md",
        "title": "孙悟空",
        "type": "detail",
        "text": "须菩提祖师传授孙悟空七十二变与筋斗云。",
        "tags": [],
        "created": "2026-01-01",
    },
    {
        "chunk_id": "wiki/sources/猪八戒.md#summary",
        "doc_path": "wiki/sources/猪八戒.md",
        "title": "猪八戒",
        "type": "summary",
        "text": "猪八戒，师父唐僧。",
        "tags": [],
        "created": "2026-01-01",
    },
    {
        "chunk_id": "wiki/sources/猪八戒.md#detail0",
        "doc_path": "wiki/sources/猪八戒.md",
        "title": "猪八戒",
        "type": "detail",
        "text": "猪八戒随唐僧取经，法号悟能。",
        "tags": [],
        "created": "2026-01-01",
    },
]


def build_fake_index_state(chunks=None) -> dict:
    """构建测试用假索引状态。

    两个文档使用正交向量，避免“所有文档都命中”的假象。
    """
    chunks = chunks or FAKE_CHUNKS
    matrix = np.zeros((len(chunks), 8), dtype=np.float32)
    for index, chunk in enumerate(chunks):
        if "孙悟空" in chunk["doc_path"]:
            matrix[index, 0] = 1.0
        else:
            matrix[index, 1] = 1.0
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    return {
        "chunks": chunks,
        "emb_matrix": matrix,
        "bm25": NewBM25Index.build([c["text"] for c in chunks]),
        "summary_by_doc": {c["doc_path"]: c for c in chunks if c["type"] == "summary"},
        "built_at": "test",
        "status": "ready",
        "error": None,
        "reindex": {"status": "idle", "total": len(chunks), "done": len(chunks),
                    "current_file": "", "level": "chunk"},
    }


class FakeIndexService:
    """把旧 INDEX_STATE 包装成新 IndexService 的最小接口。"""

    def __init__(self, state: dict) -> None:
        chunks = tuple(chunk_from_dict(chunk) for chunk in state["chunks"])
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        self._snapshot = IndexSnapshot(
            version="test",
            chunks=chunks,
            embeddings=state["emb_matrix"],
            bm25=NewBM25Index.build([chunk.text for chunk in chunks]),
            summary_by_doc={
                doc_path: by_id[chunk["chunk_id"]]
                for doc_path, chunk in state["summary_by_doc"].items()
            },
            built_at="test",
            meta={},
        )
        self._status = IndexStatus.READY
        self._error = None
        self._progress = ReindexProgress(
            status="done", total=len(chunks), done=len(chunks),
            current_file="", level="chunk",
        )

    @property
    def status(self):
        return self._status

    @property
    def error(self):
        return self._error

    @property
    def progress(self):
        return self._progress

    def current(self):
        return self._snapshot

    def load(self):
        return True

    def rebuild(self, background=True):
        return None


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fake_embedding(monkeypatch):
    """查询向量固定为第一维，稳定命中“孙悟空”文档。"""
    vector = [1.0] + [0.0] * 7
    monkeypatch.setattr(server, "get_embedding", lambda text, **kw: list(vector))
    monkeypatch.setattr(server, "get_embeddings",
                        lambda texts, **kw: [list(vector) for _ in texts])
    monkeypatch.setattr(server, "get_param", lambda name, default: default)
    return lambda text, **kw: list(vector)


@pytest.fixture
def fake_index(monkeypatch, fake_embedding):
    """替换新 IndexService；测试结束后恢复原值。"""
    original_service = getattr(server, "_INDEX_SERVICE", None)
    state = build_fake_index_state()
    server._INDEX_SERVICE = FakeIndexService(state)
    try:
        yield state
    finally:
        server._INDEX_SERVICE = original_service


@pytest.fixture
def temp_session_db(tmp_path, monkeypatch):
    """每个测试使用独立 SQLite 文件，避免会话串扰。"""
    db_path = tmp_path / "sessions.db"
    monkeypatch.setenv("RAG_SESSION_DB", str(db_path))
    SqliteSessionRepository(db_path).migrate()
    return db_path


@pytest.fixture
def client(fake_index, fake_llm, temp_session_db, monkeypatch):
    """FastAPI TestClient：替换 LLM、埋点、评估、Disco，保证不触网。"""
    from analytics import evaluation as eval_logger
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "LLMClient", lambda: fake_llm)
    monkeypatch.setattr(server, "REWRITE_LLM", fake_llm)
    monkeypatch.setattr(server, "record_query_visit", lambda *a, **k: None)
    monkeypatch.setattr(server, "disco_analyze",
                        lambda question, chunks: {"trees": "", "graph": "", "outline": ""})
    monkeypatch.setattr(eval_logger, "log_eval_async", lambda *a, **k: None)
    monkeypatch.setattr(
        eval_logger,
        "evaluate_and_record",
        lambda *a, **k: {"total": 8.0, "dimensions": {}, "reason": "fake"},
    )

    with TestClient(server.app) as test_client:
        yield test_client
