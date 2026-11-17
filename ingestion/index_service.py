"""索引服务：不可变快照 + 后台重建 + 原子持久化。"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np

from domain.errors import IndexNotReady
from domain.models import Chunk, IndexStatus, ReindexProgress
from domain.ports import Embedder, IndexStore

from .vault import scan_vault


BM25Factory = Callable[[Sequence[str]], object]


@dataclass(frozen=True)
class IndexSnapshot:
    version: str
    chunks: tuple[Chunk, ...]
    embeddings: np.ndarray
    bm25: object | None
    summary_by_doc: Mapping[str, Chunk]
    built_at: str
    meta: Mapping[str, object]


class IndexService:
    def __init__(
        self,
        vault_path: str | Path,
        index_path: str | Path,
        embedder: Embedder,
        *,
        store: IndexStore,
        bm25_factory: BM25Factory | None = None,
        batch_size: int = 16,
        chunk_size: int = 800,
        chunk_overlap: int = 100,
    ) -> None:
        self.vault_path = Path(vault_path)
        self.index_path = Path(index_path)
        self._embedder = embedder
        self._store = store
        self._bm25_factory = bm25_factory
        self._batch_size = max(1, int(batch_size))
        self._chunk_size = int(chunk_size)
        self._chunk_overlap = int(chunk_overlap)

        self._lock = threading.RLock()
        self._snapshot: IndexSnapshot | None = None
        self._status = IndexStatus.EMPTY
        self._error: str | None = None
        self._progress = ReindexProgress()
        self._thread: threading.Thread | None = None

    # ---------- 读取 ----------

    def current(self) -> IndexSnapshot:
        with self._lock:
            if self._snapshot is None:
                raise IndexNotReady(
                    "索引未就绪",
                    detail={"status": self._status.value},
                )
            return self._snapshot

    @property
    def status(self) -> IndexStatus:
        with self._lock:
            return self._status

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def progress(self) -> ReindexProgress:
        with self._lock:
            return self._progress

    # ---------- 加载 / 重建 ----------

    def load(self) -> bool:
        try:
            data = self._store.read(self.index_path)
            bm25 = self._build_bm25(data.chunks)
            version = str(data.meta.get("version") or self._version(data.chunks))
            snapshot = IndexSnapshot(
                version=version,
                chunks=data.chunks,
                embeddings=data.embeddings,
                bm25=bm25,
                summary_by_doc=data.summary_by_doc,
                built_at=str(data.meta.get("built_at") or ""),
                meta=data.meta,
            )
        except Exception as exc:
            with self._lock:
                if self._snapshot is None:
                    self._status = IndexStatus.EMPTY
                self._error = str(exc)
            return False

        with self._lock:
            self._snapshot = snapshot
            self._status = IndexStatus.READY
            self._error = None
            self._progress = ReindexProgress(
                status="done", total=len(data.chunks), done=len(data.chunks),
                current_file="", level="chunk",
            )
        return True

    def load_or_rebuild(self, background: bool = True) -> bool:
        if self.load():
            return True
        self.rebuild(background=background)
        return False

    def rebuild(self, background: bool = True) -> None:
        with self._lock:
            self._status = IndexStatus.BUILDING
            self._error = None
            self._progress = ReindexProgress(
                status="running", total=0, done=0,
                current_file="", level="chunk",
            )

        if background:
            self._thread = threading.Thread(target=self._rebuild, daemon=True)
            self._thread.start()
        else:
            self._rebuild()

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ---------- 内部 ----------

    def _rebuild(self) -> None:
        try:
            chunks, summary_by_doc = scan_vault(
                self.vault_path,
                chunk_size=self._chunk_size,
                overlap=self._chunk_overlap,
            )
            with self._lock:
                self._progress = ReindexProgress(
                    status="running", total=len(chunks), done=0,
                    current_file="", level="chunk",
                )

            embeddings, failures = self._embed_all(chunks)
            bm25 = self._build_bm25(chunks)
            built_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            version = self._version(chunks)
            meta = {
                "schema_version": 2,
                "built_at": built_at,
                "n_chunks": len(chunks),
                "vault": str(self.vault_path),
                "embedding_model": getattr(self._embedder, "model", "unknown"),
                "chunk_size": self._chunk_size,
                "chunk_overlap": self._chunk_overlap,
                "version": version,
            }
            snapshot = IndexSnapshot(
                version=version,
                chunks=tuple(chunks),
                embeddings=embeddings,
                bm25=bm25,
                summary_by_doc=summary_by_doc,
                built_at=built_at,
                meta=meta,
            )

            # 先持久化，再换内存引用；持久化失败不会破坏旧快照。
            self._store.write(
                self.index_path,
                chunks=snapshot.chunks,
                embeddings=snapshot.embeddings,
                summary_by_doc=snapshot.summary_by_doc,
                meta=meta,
            )

            with self._lock:
                self._snapshot = snapshot
                self._status = IndexStatus.READY
                self._error = (
                    f"{len(failures)} 个 chunk embedding 失败，已用零向量占位: "
                    + "; ".join(failures[:3])
                    if failures else None
                )
                self._progress = ReindexProgress(
                    status="done", total=len(chunks), done=len(chunks),
                    current_file="", level="chunk",
                    failed=len(failures),
                    failed_chunks=tuple(failures[:20]),
                )
        except Exception as exc:
            with self._lock:
                self._status = IndexStatus.FAILED
                self._error = str(exc)
                self._progress = ReindexProgress(
                    status="failed",
                    total=self._progress.total,
                    done=self._progress.done,
                    current_file="",
                    level="chunk",
                )

    def _build_bm25(self, chunks: Sequence[Chunk]) -> object | None:
        if self._bm25_factory is None:
            return None
        return self._bm25_factory([chunk.text for chunk in chunks])

    def _embed_all(self, chunks: Sequence[Chunk]) -> tuple[np.ndarray, list[str]]:
        """批量 embedding；单条失败时回退逐条并跳过，不终止整个重建。"""
        if not chunks:
            return np.zeros((0, 0), dtype=np.float32), []

        vectors: list[list[float] | None] = []
        failures: list[str] = []
        for start in range(0, len(chunks), self._batch_size):
            batch = chunks[start:start + self._batch_size]
            try:
                vectors.extend(self._embedder.embed_many([chunk.text for chunk in batch]))
            except Exception:
                for chunk in batch:
                    try:
                        vectors.append(self._embedder.embed(chunk.text))
                    except Exception as exc:
                        vectors.append(None)
                        failures.append(
                            f"{chunk.doc_path}: {type(exc).__name__}: {exc}"[:300]
                        )
            with self._lock:
                done = min(start + len(batch), len(chunks))
                self._progress = ReindexProgress(
                    status="running", total=len(chunks), done=done,
                    current_file=batch[-1].doc_path if batch else "",
                    level="chunk",
                    failed=len(failures),
                    failed_chunks=tuple(failures[:20]),
                )

        dim = next((len(vec) for vec in vectors if vec), 0)
        if dim == 0:
            raise ValueError(
                "所有 chunk embedding 均失败: " + "; ".join(failures[:3])
            )
        matrix = np.asarray(
            [vec if vec is not None else [0.0] * dim for vec in vectors],
            dtype=np.float32,
        )
        if matrix.ndim != 2 or matrix.shape[0] != len(chunks):
            raise ValueError(
                f"embedding 数量不匹配: chunks={len(chunks)} matrix={matrix.shape}"
            )
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return matrix / norms, failures

    def _version(self, chunks: Sequence[Chunk]) -> str:
        digest = hashlib.sha256()
        for chunk in chunks:
            for value in (
                chunk.chunk_id,
                chunk.doc_path,
                chunk.title,
                chunk.kind.value,
                chunk.text,
                chunk.created,
                "\u0001".join(chunk.tags),
            ):
                digest.update(value.encode("utf-8"))
                digest.update(b"\x00")
        return digest.hexdigest()[:12]
