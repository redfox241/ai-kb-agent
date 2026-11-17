"""Disco-RAG 篇章分析：论证树 → 关系网/提纲（本地 Ollama）。

参考《金融知识库服务 技术说明文档》§3.10，按本地硬件约束务实调整：
- 模型用 qwen3:4B（Qwen2.5:7B 实测 4.8 t/s 太慢，三步需 12 分钟）
- 输入压缩：chunk 只取 标题 + 首句，避免长文本拖慢推理
- 两步合并：论证树(紧凑) 一次调用 + 关系网&提纲 一次调用，总 ~57s
最终答案生成仍由主 LLM（DeepSeek）完成。任一步失败降级为空串，不中断后续。
"""
import os
import re
from pathlib import Path

import httpx

from infra.paths import load_env

load_env()

OLLAMA_CHAT_URL = os.environ.get("OLLAMA_CHAT_URL", "http://localhost:11434/api/chat")
OLLAMA_CHAT_MODEL = os.environ.get("OLLAMA_CHAT_MODEL", "qwen3:4B-Instruct")
DISCO_TOP_N = int(os.environ.get("DISCO_TOP_N", "15"))
DISCO_MAX_RELATIONS = int(os.environ.get("DISCO_MAX_RELATIONS", "5"))
DISCO_TIMEOUT = int(os.environ.get("DISCO_TIMEOUT", "600"))


def _ollama_chat(prompt, max_tokens, temperature=0.2):
    r = httpx.post(
        OLLAMA_CHAT_URL,
        json={
            "model": OLLAMA_CHAT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        },
        timeout=DISCO_TIMEOUT,
        trust_env=False,
    )
    r.raise_for_status()
    return (r.json().get("message") or {}).get("content", "").strip()


_PROMPTS_FILE = Path(__file__).resolve().parents[1] / "qa" / "prompts" / "disco.md"


def _load_prompts() -> dict[str, str]:
    """读取版本化 disco prompt 资产（infra 只读文件，不依赖应用模块）。"""
    try:
        text = _PROMPTS_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    body = text.split("---", 2)[-1]
    sections: dict[str, str] = {}
    current = ""
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"
    return {key: value.strip() for key, value in sections.items()}


def _render(prompt: str, **variables: object) -> str:
    for key, value in variables.items():
        prompt = prompt.replace("{{" + key + "}}", str(value))
    return prompt


def _points(chunks):
    """把 chunks 压缩为 '编号. 标题: 首句' 要点列表，控制输入长度。"""
    lines = []
    for i, c in enumerate(chunks):
        title = c.get("title") or c.get("doc_path") or c.get("id") or "未命名"
        first = (c.get("text") or "").strip().splitlines()[0] if (c.get("text") or "").strip() else ""
        lines.append(f"{i + 1}. {title}: {first[:40]}")
    return "\n".join(lines)


def disco_argument_trees(chunks):
    """Step 1 论证树（紧凑）：对每个片段标注 结论/前提/辅助。"""
    prompt = _render(_load_prompts().get("trees", ""), points=_points(chunks))
    return _ollama_chat(prompt, max_tokens=250)


def disco_relation_graph(chunks):
    """Step 2 关系网：chunk 间关系（支持/矛盾/因果/补充）。"""
    if len(chunks) < 2:
        return ""
    prompt = _render(_load_prompts().get("graph", ""), points=_points(chunks))
    prompt = prompt.replace("最多 5 对", f"最多 {DISCO_MAX_RELATIONS} 对")
    return _ollama_chat(prompt, max_tokens=150)


def disco_outline(question, trees, graph):
    """Step 3 写作提纲：基于问题、论证树与关系网生成答案组织蓝图。"""
    prompt = _render(
        _load_prompts().get("outline", ""),
        question=question,
        trees=trees or "（无）",
        graph=graph or "（无）",
    )
    return _ollama_chat(prompt, max_tokens=200)


def disco_analyze(question, chunks):
    """串行执行三步分析；任一步失败降级为空串，不中断后续。"""
    result = {"trees": "", "graph": "", "outline": ""}
    try:
        result["trees"] = disco_argument_trees(chunks)
    except Exception as e:
        print(f"[disco] 论证树失败: {e}", flush=True)
    try:
        result["graph"] = disco_relation_graph(chunks)
    except Exception as e:
        print(f"[disco] 关系网失败: {e}", flush=True)
    try:
        result["outline"] = disco_outline(question, result["trees"], result["graph"])
    except Exception as e:
        print(f"[disco] 提纲失败: {e}", flush=True)
    return result
