"""Vault 扫描：Markdown → 摘要 chunk + 正文 chunk。

与 server.py 的 process_documents 保持行为一致：
- 相同的排除目录/前缀
- 相同的摘要块与正文分块
- 相同的 chunk_id 规则

EXCLUDE_FILES 在旧实现中未实际启用；这里同样不启用，避免迁移期行为漂移。
"""
from __future__ import annotations

from pathlib import Path

from domain.models import Chunk, ChunkKind

from .chunking import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, build_page_summary, chunk_text
from .frontmatter import parse_frontmatter


EXCLUDE_DIRS = {
    ".obsidian", ".rag", ".claude", ".git", "assets", "copilot", "schema",
    "raw/archive", "__pycache__", ".trash", "templates", "_导入报告", "attachments",
}
EXCLUDE_FILE_PREFIXES = ("session-", "article-", "handoff")
EXCLUDE_FILES = {"opencode.json"}  # 迁移期暂不启用，见模块 docstring


def scan_vault(
    root: str | Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> tuple[list[Chunk], dict[str, Chunk]]:
    """遍历 vault，返回 (chunks, summary_by_doc)。"""
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"Vault 不存在: {root}")

    chunks: list[Chunk] = []
    summary_by_doc: dict[str, Chunk] = {}

    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        parts = rel.split("/")
        if any(segment in EXCLUDE_DIRS for segment in parts):
            continue
        if rel.startswith(EXCLUDE_FILE_PREFIXES):
            continue

        text = path.read_text(encoding="utf-8", errors="ignore")
        meta, body = parse_frontmatter(text)
        if meta.get("exclude"):
            continue

        title = meta.get("title") or path.stem.replace("-", " ")
        created = str(meta.get("date", "") or meta.get("created", "") or "")
        tags = _normalise_tags(meta.get("tags"))

        summary = Chunk(
            chunk_id=f"{rel}#summary",
            doc_path=rel,
            title=title,
            kind=ChunkKind.SUMMARY,
            text=build_page_summary(meta, body, title, rel),
            tags=tags,
            created=created,
        )
        chunks.append(summary)
        summary_by_doc[rel] = summary

        for index, piece in enumerate(chunk_text(body, chunk_size, overlap)):
            chunks.append(Chunk(
                chunk_id=f"{rel}#detail{index}",
                doc_path=rel,
                title=title,
                kind=ChunkKind.DETAIL,
                text=piece,
                tags=tags,
                created=created,
            ))

    return chunks, summary_by_doc


def _normalise_tags(raw: object) -> tuple[str, ...]:
    if isinstance(raw, list):
        return tuple(str(tag) for tag in raw)
    if raw:
        return (str(raw),)
    return ()
