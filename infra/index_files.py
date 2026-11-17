"""索引文件读写：chunks.json / embeddings.npy / summary_index.json / meta.json。

持久化格式保持旧版兼容：
- chunk 的类型字段写作 `type`（summary/detail），读取时兼容 `kind`
- meta.json 最后写入，作为一次提交的标记
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from domain.models import Chunk, ChunkKind


@dataclass(frozen=True)
class IndexFileData:
    chunks: tuple[Chunk, ...]
    embeddings: np.ndarray
    summary_by_doc: Mapping[str, Chunk]
    meta: Mapping[str, object]


def chunk_to_dict(chunk: Chunk) -> dict:
    data = {
        "chunk_id": chunk.chunk_id,
        "doc_path": chunk.doc_path,
        "title": chunk.title,
        "type": chunk.kind.value,
        "text": chunk.text,
        "tags": list(chunk.tags),
        "created": chunk.created,
    }
    if chunk.metadata:
        data["metadata"] = dict(chunk.metadata)
    return data


def chunk_from_dict(data: Mapping[str, object]) -> Chunk:
    raw_kind = str(data.get("type") or data.get("kind") or ChunkKind.DETAIL.value)
    try:
        kind = ChunkKind(raw_kind)
    except ValueError:
        kind = ChunkKind.DETAIL

    raw_tags = data.get("tags")
    if isinstance(raw_tags, list):
        tags = tuple(str(tag) for tag in raw_tags)
    elif raw_tags:
        tags = (str(raw_tags),)
    else:
        tags = ()

    raw_meta = data.get("metadata")
    metadata = dict(raw_meta) if isinstance(raw_meta, dict) else {}

    return Chunk(
        chunk_id=str(data.get("chunk_id") or ""),
        doc_path=str(data.get("doc_path") or ""),
        title=str(data.get("title") or ""),
        kind=kind,
        text=str(data.get("text") or ""),
        tags=tags,
        created=str(data.get("created") or ""),
        metadata=metadata,
    )


def write_snapshot(
    index_path: str | Path,
    chunks: Sequence[Chunk],
    embeddings: np.ndarray,
    summary_by_doc: Mapping[str, Chunk],
    meta: Mapping[str, object],
) -> None:
    index_path = Path(index_path)
    index_path.mkdir(parents=True, exist_ok=True)

    meta = dict(meta)
    meta.setdefault("schema_version", 2)
    meta["n_chunks"] = len(chunks)

    _atomic_write_text(
        index_path / "chunks.json",
        json.dumps([chunk_to_dict(chunk) for chunk in chunks], ensure_ascii=False),
    )
    _atomic_save_npy(index_path / "embeddings.npy", embeddings)
    _atomic_write_text(
        index_path / "summary_index.json",
        json.dumps(
            {doc_path: chunk.chunk_id for doc_path, chunk in summary_by_doc.items()},
            ensure_ascii=False,
        ),
    )
    # meta 最后写：它存在且 chunks 行数匹配，才视为一次完整提交。
    _atomic_write_text(
        index_path / "meta.json",
        json.dumps(meta, ensure_ascii=False),
    )


def read_snapshot(index_path: str | Path) -> IndexFileData:
    index_path = Path(index_path)

    meta = json.loads((index_path / "meta.json").read_text(encoding="utf-8"))
    raw_chunks = json.loads((index_path / "chunks.json").read_text(encoding="utf-8"))
    embeddings = np.load(index_path / "embeddings.npy")

    if not isinstance(raw_chunks, list):
        raise ValueError("chunks.json 必须是数组")
    chunks = tuple(chunk_from_dict(item) for item in raw_chunks)

    if embeddings.ndim == 0 or embeddings.shape[0] != len(chunks):
        raise ValueError(
            f"chunks 与 embeddings 行数不一致: {len(chunks)} vs {getattr(embeddings, 'shape', None)}"
        )

    try:
        summary_ids = json.loads(
            (index_path / "summary_index.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        summary_ids = {}

    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    summary_by_doc: dict[str, Chunk] = {}
    if isinstance(summary_ids, dict):
        for doc_path, chunk_id in summary_ids.items():
            chunk = by_id.get(str(chunk_id))
            if chunk is not None:
                summary_by_doc[str(doc_path)] = chunk
    if not summary_by_doc:
        summary_by_doc = {
            chunk.doc_path: chunk
            for chunk in chunks
            if chunk.kind is ChunkKind.SUMMARY
        }

    return IndexFileData(
        chunks=chunks,
        embeddings=embeddings,
        summary_by_doc=summary_by_doc,
        meta=meta if isinstance(meta, dict) else {},
    )


def _atomic_write_text(path: Path, text: str) -> None:
    temp = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _atomic_save_npy(path: Path, matrix: np.ndarray) -> None:
    temp = path.with_name(
        f"{path.stem}.tmp.{os.getpid()}.{threading.get_ident()}.npy"
    )
    try:
        np.save(temp, matrix)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


class FileIndexStore:
    """IndexStore 端口的文件实现。"""

    def read(self, path):
        return read_snapshot(path)

    def write(self, path, *, chunks, embeddings, summary_by_doc, meta):
        write_snapshot(
            path,
            chunks=chunks,
            embeddings=embeddings,
            summary_by_doc=summary_by_doc,
            meta=meta,
        )
