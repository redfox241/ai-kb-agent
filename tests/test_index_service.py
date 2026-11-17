"""PR-05：vault 扫描、索引快照、持久化与重建失败恢复。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from domain import Chunk, ChunkKind, IndexNotReady, IndexStatus
from infra.index_files import FileIndexStore
from ingestion.index_service import IndexService
from ingestion.vault import scan_vault
from infra.index_files import read_snapshot, write_snapshot


class FakeEmbedder:
    model = "fake-embed"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        if self.fail:
            raise RuntimeError("embedding down")
        return [1.0, 0.0]

    def embed_many(self, texts):
        if self.fail:
            raise RuntimeError("embedding down")
        self.calls += len(texts)
        return [[1.0, 0.0] for _ in texts]


def _build_vault(root: Path) -> None:
    (root / "wiki").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / "raw" / "archive").mkdir(parents=True)

    (root / "wiki" / "a.md").write_text(
        "---\n"
        "title: 孙悟空\n"
        "tags: [西游, 人物]\n"
        "date: 2026-01-01\n"
        "---\n\n"
        "孙悟空，师父须菩提祖师。\n\n"
        "## 法术\n\n"
        "须菩提祖师传授七十二变与筋斗云。\n",
        encoding="utf-8",
    )
    (root / "wiki" / "b.md").write_text(
        "---\ntitle: 猪八戒\n---\n\n猪八戒，师父唐僧。",
        encoding="utf-8",
    )
    (root / "wiki" / "c.md").write_text(
        "## 第一节\n\n" + "长内容。" * 200 + "\n\n## 第二节\n\n" + "更多内容。" * 200,
        encoding="utf-8",
    )
    (root / ".obsidian" / "secret.md").write_text("不应进入索引", encoding="utf-8")
    (root / "session-temp.md").write_text("会话临时文档", encoding="utf-8")
    (root / "excluded.md").write_text("---\nexclude: true\n---\n排除", encoding="utf-8")
    (root / "raw" / "archive" / "old.md").write_text("归档内容", encoding="utf-8")


def _legacy_chunk(chunk: Chunk) -> dict:
    return {
        "chunk_id": chunk.chunk_id,
        "doc_path": chunk.doc_path,
        "title": chunk.title,
        "type": chunk.kind.value,
        "text": chunk.text,
        "tags": list(chunk.tags),
        "created": chunk.created,
    }


def test_scan_vault_contract(tmp_path):
    vault = tmp_path / "vault"
    _build_vault(vault)

    chunks, summary = scan_vault(vault)
    doc_paths = {chunk.doc_path for chunk in chunks}

    assert "wiki/a.md" in doc_paths
    assert "wiki/b.md" in doc_paths
    assert "wiki/c.md" in doc_paths
    assert ".obsidian/secret.md" not in doc_paths
    assert "session-temp.md" not in doc_paths
    assert "excluded.md" not in doc_paths
    assert all(doc in doc_paths for doc in summary)
    assert all(chunk.chunk_id.startswith(chunk.doc_path) for chunk in chunks)

def test_index_files_roundtrip(tmp_path):
    chunks = (
        Chunk("wiki/a.md#summary", "wiki/a.md", "A", ChunkKind.SUMMARY, "摘要"),
        Chunk("wiki/a.md#detail0", "wiki/a.md", "A", ChunkKind.DETAIL, "正文"),
    )
    embeddings = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    summary_by_doc = {"wiki/a.md": chunks[0]}
    meta = {"built_at": "test", "version": "v1"}

    index_path = tmp_path / "index"
    write_snapshot(index_path, chunks, embeddings, summary_by_doc, meta)
    data = read_snapshot(index_path)

    assert data.chunks == chunks
    assert data.embeddings.shape == embeddings.shape
    assert data.summary_by_doc == summary_by_doc
    assert data.meta["schema_version"] == 2
    assert data.meta["n_chunks"] == 2
    assert json.loads((index_path / "chunks.json").read_text(encoding="utf-8"))[0]["type"] == "summary"
    assert not list(index_path.glob("*.tmp.*"))


def test_index_service_rebuild_load_and_snapshot(tmp_path):
    vault = tmp_path / "vault"
    index_path = tmp_path / "index"
    _build_vault(vault)

    embedder = FakeEmbedder()
    service = IndexService(
        vault, index_path, embedder, store=FileIndexStore(),
        bm25_factory=lambda texts: {"n": len(texts)},
        batch_size=2,
    )

    with pytest.raises(IndexNotReady):
        service.current()

    service.rebuild(background=False)
    assert service.status is IndexStatus.READY
    snapshot = service.current()
    expected_chunks, expected_summary = scan_vault(vault)

    assert snapshot.chunks == tuple(expected_chunks)
    assert snapshot.summary_by_doc == expected_summary
    assert snapshot.embeddings.shape == (len(expected_chunks), 2)
    assert snapshot.bm25 == {"n": len(expected_chunks)}
    assert service.progress.status == "done"
    assert service.progress.done == len(expected_chunks)
    assert embedder.calls == len(expected_chunks)

    loaded = IndexService(
        vault, index_path, FakeEmbedder(), store=FileIndexStore(),
        bm25_factory=lambda texts: {"n": len(texts)},
    )
    assert loaded.load() is True
    assert loaded.current().chunks == snapshot.chunks
    assert loaded.current().built_at == snapshot.built_at


def test_index_service_rebuild_failure_keeps_old_snapshot(tmp_path):
    vault = tmp_path / "vault"
    index_path = tmp_path / "index"
    _build_vault(vault)

    service = IndexService(
        vault, index_path, FakeEmbedder(), store=FileIndexStore(),
        bm25_factory=lambda texts: {"n": len(texts)},
    )
    service.rebuild(background=False)
    old_version = service.current().version

    service._embedder = FakeEmbedder(fail=True)
    service.rebuild(background=False)

    assert service.status is IndexStatus.FAILED
    assert "embedding down" in (service.error or "")
    assert service.current().version == old_version


def test_index_service_missing_index_and_background_rebuild(tmp_path):
    vault = tmp_path / "vault"
    index_path = tmp_path / "index"
    _build_vault(vault)

    service = IndexService(
        vault, index_path, FakeEmbedder(), store=FileIndexStore(),
        bm25_factory=lambda texts: None,
    )
    assert service.load() is False
    assert service.status is IndexStatus.EMPTY

    service.rebuild(background=True)
    assert service.wait(5) is True
    assert service.status is IndexStatus.READY


class _PartialFailEmbedder:
    """批量必失败，逐条时含“坏”的 chunk 失败：验证单个坏块不终止重建。"""

    model = "fake-embed"

    def embed(self, text: str) -> list[float]:
        if "坏" in text:
            raise RuntimeError("context length exceeded")
        return [1.0, 0.0]

    def embed_many(self, texts):
        raise RuntimeError("batch down")


def test_rebuild_isolates_single_chunk_failure(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "wiki" / "good.md").write_text(
        "---\ntitle: 好\n---\n\n正常内容。", encoding="utf-8"
    )
    (vault / "wiki" / "bad.md").write_text(
        "---\ntitle: 坏\n---\n\n坏内容。", encoding="utf-8"
    )

    service = IndexService(
        vault, tmp_path / "index", _PartialFailEmbedder(),
        store=FileIndexStore(),
        bm25_factory=lambda texts: {"n": len(texts)},
        batch_size=2,
    )
    service.rebuild(background=False)

    assert service.status is IndexStatus.READY
    assert service.progress.failed >= 1
    assert "embedding 失败" in (service.error or "")
    snapshot = service.current()
    assert snapshot.embeddings.shape == (len(snapshot.chunks), 2)
    assert np.any(np.linalg.norm(snapshot.embeddings, axis=1) == 0)
