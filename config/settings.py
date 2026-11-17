"""启动配置：环境变量 → 不可变 Settings。

只读取一次、只做解析与校验；不在这里读项目文件、不建目录、不初始化 DB。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from domain.errors import ConfigurationError

from .profiles import BASE_DIR, Profile


@dataclass(frozen=True)
class DbSettings:
    sessions: Path
    analytics: Path


@dataclass(frozen=True)
class LLMSettings:
    style: str = "anthropic"
    base_url: str = ""
    api_key: str = ""
    model: str = "deepseek-v4-flash"
    max_tokens: int = 8000
    timeout_s: float = 180.0


@dataclass(frozen=True)
class EmbedSettings:
    url: str = "http://localhost:11434/api/embeddings"
    model: str = "nomic-embed-text"
    timeout_s: float = 180.0
    concurrency: int = 8


@dataclass(frozen=True)
class IngestionSettings:
    batch_size: int = 16
    chunk_size: int = 800
    chunk_overlap: int = 100


@dataclass(frozen=True)
class RetrievalSettings:
    top_k: int = 30
    max_context_chunks: int = 30
    similarity_threshold: float = 0.3
    max_sources: int = 10
    bm25_k1: float = 1.5
    bm25_b: float = 0.75


@dataclass(frozen=True)
class MultiturnSettings:
    enabled: bool = True
    rewrite_mode: str = "auto"
    max_history_turns: int = 3
    max_prompt_tokens: int = 12000
    summary_every_turns: int = 6
    followup_suggest: bool = True
    repeat_penalty: float = 0.85


@dataclass(frozen=True)
class JobSettings:
    max_workers: int = 4


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    vault_path: Path
    index_path: Path
    prompt_dir: Path
    db: DbSettings
    llm: LLMSettings
    embed: EmbedSettings
    ingestion: IngestionSettings
    retrieval: RetrievalSettings
    multiturn: MultiturnSettings
    jobs: JobSettings

    @classmethod
    def from_env(
        cls,
        profile: Profile,
        env: Mapping[str, str] | None = None,
    ) -> "Settings":
        values = dict(os.environ if env is None else env)

        data_dir = _path(values, "AIKB_DATA_DIR", profile.data_dir)
        vault_path = _path(values, "RAG_VAULT_PATH", BASE_DIR / "vault")
        index_path = _path(values, "RAG_INDEX_PATH", data_dir / "index")
        prompt_dir = _path(values, "AIKB_PROMPT_DIR", BASE_DIR / "qa" / "prompts")

        db = DbSettings(
            sessions=_path(values, "RAG_SESSION_DB", data_dir / "db" / "sessions.db"),
            analytics=_path(values, "RAG_EVAL_DB", data_dir / "db" / "analytics.db"),
        )

        llm = LLMSettings(
            style=(values.get("LLM_API_STYLE") or "anthropic").strip().lower(),
            base_url=(values.get("LLM_BASE_URL") or "").strip().rstrip("/"),
            api_key=(values.get("LLM_API_KEY") or "").strip(),
            model=(values.get("LLM_MODEL") or "deepseek-v4-flash").strip(),
            max_tokens=_int(values, "LLM_MAX_TOKENS", 8000),
            timeout_s=_float(values, "LLM_TIMEOUT", 180.0),
        )

        embed = EmbedSettings(
            url=(values.get("OLLAMA_EMBED_URL")
                 or "http://localhost:11434/api/embeddings").strip(),
            model=(values.get("OLLAMA_EMBED_MODEL")
                   or "nomic-embed-text").strip(),
            timeout_s=_float(values, "OLLAMA_EMBED_TIMEOUT", 180.0),
            concurrency=_int(values, "EMBED_CONCURRENCY", 8),
        )

        ingestion = IngestionSettings(
            batch_size=_int(values, "EMBED_BATCH_SIZE", 16),
            chunk_size=_int(values, "CHUNK_SIZE", 800),
            chunk_overlap=_int(values, "CHUNK_OVERLAP", 100),
        )

        retrieval = RetrievalSettings(
            top_k=_int(values, "TOP_K", 30),
            max_context_chunks=_int(values, "MAX_CONTEXT_CHUNKS", 30),
            similarity_threshold=_float(values, "SIMILARITY_THRESHOLD", 0.3),
            max_sources=_int(values, "MAX_SOURCES", 10),
            bm25_k1=_float(values, "BM25_K1", 1.5),
            bm25_b=_float(values, "BM25_B", 0.75),
        )

        multiturn = MultiturnSettings(
            enabled=_bool(values, "MULTITURN_ENABLED", profile.flags.multiturn),
            rewrite_mode=(values.get("REWRITE_MODE") or "auto").strip().lower(),
            max_history_turns=_int(values, "MAX_HISTORY_TURNS", 3),
            max_prompt_tokens=_int(values, "MULTITURN_MAX_PROMPT_TOKENS", 12000),
            summary_every_turns=_int(values, "SUMMARY_EVERY_TURNS", 6),
            followup_suggest=_bool(values, "FOLLOWUP_SUGGEST", True),
            repeat_penalty=_float(values, "REPEAT_PENALTY", 0.85),
        )

        jobs = JobSettings(max_workers=_int(values, "JOB_MAX_WORKERS", 4))

        if llm.style not in {"anthropic", "openai"}:
            raise ConfigurationError(
                f"LLM_API_STYLE 必须是 anthropic 或 openai，实际 {llm.style!r}",
                detail={"key": "LLM_API_STYLE", "value": llm.style},
            )
        if multiturn.rewrite_mode not in {"auto", "off", "always"}:
            raise ConfigurationError(
                f"REWRITE_MODE 必须是 auto/off/always，实际 {multiturn.rewrite_mode!r}",
                detail={"key": "REWRITE_MODE", "value": multiturn.rewrite_mode},
            )

        _validate(llm, embed, ingestion, retrieval, multiturn, jobs)
        return cls(
            data_dir=data_dir,
            vault_path=vault_path,
            index_path=index_path,
            prompt_dir=prompt_dir,
            db=db,
            llm=llm,
            embed=embed,
            ingestion=ingestion,
            retrieval=retrieval,
            multiturn=multiturn,
            jobs=jobs,
        )


def _path(values: Mapping[str, str], key: str, default: Path) -> Path:
    raw = (values.get(key) or "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _int(values: Mapping[str, str], key: str, default: int) -> int:
    raw = (values.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"配置 {key} 必须是整数",
            detail={"key": key, "value": raw},
        ) from exc


def _float(values: Mapping[str, str], key: str, default: float) -> float:
    raw = (values.get(key) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"配置 {key} 必须是数字",
            detail={"key": key, "value": raw},
        ) from exc


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _bool(values: Mapping[str, str], key: str, default: bool) -> bool:
    raw = (values.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigurationError(
        f"配置 {key} 必须是布尔值（1/0/true/false）",
        detail={"key": key, "value": raw},
    )


def _validate(
    llm: LLMSettings,
    embed: EmbedSettings,
    ingestion: IngestionSettings,
    retrieval: RetrievalSettings,
    multiturn: MultiturnSettings,
    jobs: JobSettings,
) -> None:
    checks = [
        ("LLM_MAX_TOKENS", llm.max_tokens, llm.max_tokens > 0),
        ("EMBED_CONCURRENCY", embed.concurrency, embed.concurrency >= 1),
        ("EMBED_BATCH_SIZE", ingestion.batch_size, ingestion.batch_size >= 1),
        ("CHUNK_SIZE", ingestion.chunk_size, ingestion.chunk_size > 0),
        (
            "CHUNK_OVERLAP",
            ingestion.chunk_overlap,
            0 <= ingestion.chunk_overlap < ingestion.chunk_size,
        ),
        ("TOP_K", retrieval.top_k, retrieval.top_k > 0),
        (
            "MAX_CONTEXT_CHUNKS",
            retrieval.max_context_chunks,
            retrieval.max_context_chunks > 0,
        ),
        (
            "SIMILARITY_THRESHOLD",
            retrieval.similarity_threshold,
            0.0 <= retrieval.similarity_threshold <= 1.0,
        ),
        ("MAX_SOURCES", retrieval.max_sources, retrieval.max_sources > 0),
        ("BM25_K1", retrieval.bm25_k1, retrieval.bm25_k1 > 0),
        ("BM25_B", retrieval.bm25_b, 0.0 <= retrieval.bm25_b <= 1.0),
        ("MAX_HISTORY_TURNS", multiturn.max_history_turns, multiturn.max_history_turns >= 0),
        (
            "MULTITURN_MAX_PROMPT_TOKENS",
            multiturn.max_prompt_tokens,
            multiturn.max_prompt_tokens > 0,
        ),
        (
            "SUMMARY_EVERY_TURNS",
            multiturn.summary_every_turns,
            multiturn.summary_every_turns >= 1,
        ),
        (
            "REPEAT_PENALTY",
            multiturn.repeat_penalty,
            0.0 < multiturn.repeat_penalty <= 1.0,
        ),
        ("JOB_MAX_WORKERS", jobs.max_workers, jobs.max_workers >= 1),
    ]
    for key, value, ok in checks:
        if not ok:
            raise ConfigurationError(
                f"配置 {key} 取值非法: {value!r}",
                detail={"key": key, "value": value},
            )
