"""LLM 客户端：支持 Anthropic 兼容(DeepSeek)与 OpenAI 兼容接口。

- Anthropic 兼容：POST {base}/v1/messages，响应 content 数组需提取 text 块（跳过 thinking 块）
- OpenAI 兼容：POST {base}/v1/chat/completions
- 风格由 LLM_API_STYLE 决定，默认 anthropic
"""
import os

import httpx

from infra.paths import load_env

load_env()


class LLMError(Exception):
    pass


_PLACEHOLDER_KEYS = {"", "__from_env__", "__LLM_API_KEY__", "your-key", "sk-xxx"}


def _resolve_api_key():
    for key in ("LLM_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        v = (os.environ.get(key) or "").strip()
        if v and v not in _PLACEHOLDER_KEYS:
            return v
    return ""


class LLMClient:
    def __init__(self, base_url=None, api_key=None, model=None, max_tokens=None,
                 style=None, timeout=180):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or _resolve_api_key()
        self.model = model or os.environ.get("LLM_MODEL", "deepseek-v4-flash")
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "8000") or max_tokens or 8000)
        self.style = (style or os.environ.get("LLM_API_STYLE", "anthropic")).lower()
        self.timeout = timeout

    # ---------- 底层调用 ----------
    def _call_anthropic(self, messages, system=None, max_tokens=None,
                        temperature=None, thinking=None):
        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if thinking is not None:
            body["thinking"] = thinking
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=self.timeout)
            # 部分 Anthropic 兼容网关不支持 thinking 字段；400 时回退再试一次。
            if r.status_code == 400 and thinking is not None:
                body.pop("thinking", None)
                r = httpx.post(url, headers=headers, json=body, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise LLMError(f"Anthropic 调用失败: {e}") from e
        parts = []
        for block in data.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts).strip()

    def _call_openai(self, messages, system=None, max_tokens=None,
                     temperature=None, thinking=None):
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        body = {"model": self.model, "messages": msgs, "max_tokens": max_tokens or self.max_tokens}
        if temperature is not None:
            body["temperature"] = temperature
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise LLMError(f"OpenAI 调用失败: {e}") from e
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

    # ---------- 对外接口 ----------
    def chat(self, messages, system=None, max_tokens=None, temperature=None,
             thinking=None):
        """messages: [{'role':'user'|'assistant','content':str}, ...]"""
        if self.style == "openai":
            # OpenAI 兼容路径暂不透传 thinking。
            return self._call_openai(
                messages, system=system, max_tokens=max_tokens,
                temperature=temperature,
            )
        return self._call_anthropic(
            messages, system=system, max_tokens=max_tokens,
            temperature=temperature, thinking=thinking,
        )

    def chat_json(self, prompt, system=None, max_tokens=None, temperature=None,
                  thinking=None):
        """请求 JSON 输出，自动剥离代码围栏，返回 dict；失败返回 None。"""
        text = self.chat(
            [{"role": "user", "content": prompt}],
            system=system,
            max_tokens=max_tokens or 2000,
            temperature=0.2 if temperature is None else temperature,
            thinking=thinking,
        )
        return parse_json(text)

from domain.json_utils import parse_json
