"""Ollama 本地小模型客户端：用于查询改写等辅助任务。

使用 Ollama 原生 `/api/chat`，并在 `chat_json()` 时传 `format="json"`，
避免小模型输出 Markdown 围栏或解释文本。
"""
from __future__ import annotations

import os

import httpx

from domain.json_utils import parse_json
from infra.llm import LLMError
from infra.paths import load_env

load_env()


class OllamaChatClient:
    def __init__(self, url=None, model=None, timeout=None):
        self.url = (
            url
            or os.environ.get("OLLAMA_CHAT_URL")
            or "http://localhost:11434/api/chat"
        ).strip()
        self.model = (
            model
            or os.environ.get("OLLAMA_CHAT_MODEL")
            or "qwen3:4B-Instruct"
        ).strip()
        self.timeout = float(timeout or os.environ.get("OLLAMA_CHAT_TIMEOUT", "120"))

    def _post(self, messages, system=None, max_tokens=None, temperature=None,
              fmt=None):
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        body = {
            "model": self.model,
            "messages": msgs,
            "stream": False,
            "options": {
                "temperature": 0.1 if temperature is None else temperature,
                "num_predict": int(max_tokens or 600),
            },
        }
        if fmt:
            body["format"] = fmt
        try:
            r = httpx.post(self.url, json=body, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise LLMError(f"Ollama 调用失败: {e}") from e
        return str((data.get("message") or {}).get("content") or "").strip()

    def chat(self, messages, system=None, max_tokens=None, temperature=None,
             thinking=None):
        # Ollama 的思考开关通过 `think` 控制；当前 Qwen3-Instruct 默认关闭。
        return self._post(
            messages,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def chat_json(self, prompt, system=None, max_tokens=None, temperature=None,
                  thinking=None):
        text = self._post(
            [{"role": "user", "content": prompt}],
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            fmt="json",
        )
        return parse_json(text)
