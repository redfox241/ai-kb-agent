"""Ollama Embedding 客户端"""
import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from infra.paths import load_env

load_env()

DEFAULT_MAX_CHARS = 1500


def _max_chars() -> int:
    """单条文本上限：防止超出 embedding 模型上下文（中文约 2 token/字）。"""
    try:
        return max(1, int(os.environ.get("OLLAMA_EMBED_MAX_CHARS", DEFAULT_MAX_CHARS)))
    except ValueError:
        return DEFAULT_MAX_CHARS


def _post(text, model, url):
    payload = {
        "model": model or os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        "prompt": text[: _max_chars()],
    }
    r = httpx.post(
        url or os.environ.get("OLLAMA_EMBED_URL", "http://localhost:11434/api/embeddings"),
        json=payload,
        timeout=180,
        trust_env=False,
    )
    if r.status_code >= 400:
        detail = (r.text or "").strip().replace("\n", " ")[:300]
        raise RuntimeError(
            f"Ollama embedding 失败（HTTP {r.status_code}，model={payload['model']}）: {detail}"
        )
    return r.json()["embedding"]


def get_embedding(text, model=None, url=None):
    return _post(text, model, url)


def get_embeddings(texts, concurrency=8):
    """批量 embedding，线程池并发。返回与 texts 同序的向量列表。"""
    if not texts:
        return []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(lambda t: _post(t, None, None), texts))
