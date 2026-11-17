"""Prompt 资产：版本化 Markdown + 严格渲染。

约束（见详细设计 §06）：
- 缺失变量报错、未知变量报错；
- 版本号可随 trace 返回；
- 优化器只能追加 prompt_rule，不能改写基础 prompt。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
META_RE = re.compile(r"^(?P<key>[A-Za-z_]+)\s*:\s*(?P<value>.*)$")


class PromptError(ValueError):
    """Prompt 资产缺失或渲染变量不匹配。"""


@dataclass(frozen=True)
class PromptAsset:
    id: str
    version: int
    body: str
    variables: frozenset[str]
    owner: str = "qa"
    path: Path | None = None


def _parse_meta(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        raise PromptError("prompt 资产缺少 frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise PromptError("prompt frontmatter 未闭合")
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        match = META_RE.match(line.strip())
        if match:
            meta[match.group("key")] = match.group("value").strip().strip('"')
    return meta, parts[2].lstrip("\n")


class PromptRegistry:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else Path(__file__).parent / "prompts"
        self._assets: dict[str, PromptAsset] = {}
        self._load()

    def _load(self) -> None:
        for path in sorted(self.root.glob("*.md")):
            meta, body = _parse_meta(path.read_text(encoding="utf-8"))
            prompt_id = meta.get("id") or path.stem
            raw_vars = meta.get("variables", "")
            variables = frozenset(
                item.strip() for item in raw_vars.strip("[]").split(",") if item.strip()
            )
            self._assets[prompt_id] = PromptAsset(
                id=prompt_id,
                version=int(meta.get("version") or 0),
                body=body.strip(),
                variables=variables,
                owner=meta.get("owner", "qa"),
                path=path,
            )

    def get(self, prompt_id: str) -> PromptAsset:
        try:
            return self._assets[prompt_id]
        except KeyError as exc:
            raise PromptError(f"未知 prompt: {prompt_id}") from exc

    def body(self, prompt_id: str) -> str:
        return self.get(prompt_id).body

    def version(self, prompt_id: str) -> int:
        return self.get(prompt_id).version

    def render(self, prompt_id: str, **variables: object) -> str:
        asset = self.get(prompt_id)
        used = set(_VAR_RE.findall(asset.body))
        if used != set(variables):
            missing = used - set(variables)
            unknown = set(variables) - used
            raise PromptError(
                f"prompt {prompt_id} 变量不匹配: missing={sorted(missing)} "
                f"unknown={sorted(unknown)}"
            )
        return _VAR_RE.sub(lambda m: str(variables[m.group(1)]), asset.body)


_default: PromptRegistry | None = None


def default_registry() -> PromptRegistry:
    global _default
    if _default is None:
        _default = PromptRegistry()
    return _default
