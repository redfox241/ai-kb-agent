"""运行期 overrides：白名单、缓存、原子写、备份、审计与回滚。

只允许两类变更：
- params：检索/会话白名单参数
- prompt_rule：追加到 system prompt 的受控规则

不直接改代码；失败时保留旧值。
"""
from __future__ import annotations

import copy
import json
import math
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Mapping, TypeVar

from domain.errors import ConfigurationError


T = TypeVar("T")

ALLOWED_PARAMS: dict[str, type] = {
    "SIMILARITY_THRESHOLD": float,
    "TOP_K": int,
    "MAX_SOURCES": int,
    "MAX_CONTEXT_CHUNKS": int,
    "MAX_HISTORY_TURNS": int,
    "REPEAT_PENALTY": float,
    "SUMMARY_EVERY_TURNS": int,
}

DEFAULT_MAX_PROMPT_RULE_CHARS = 4000
DEFAULT_BACKUP_KEEP = 20

# (min, max, min_inclusive)
_PARAM_BOUNDS: dict[str, tuple[float | None, float | None, bool]] = {
    "SIMILARITY_THRESHOLD": (0.0, 1.0, True),
    "TOP_K": (1.0, None, True),
    "MAX_SOURCES": (1.0, None, True),
    "MAX_CONTEXT_CHUNKS": (1.0, None, True),
    "MAX_HISTORY_TURNS": (0.0, None, True),
    "REPEAT_PENALTY": (0.0, 1.0, False),
    "SUMMARY_EVERY_TURNS": (1.0, None, True),
}


def _coerce_param(name: str, raw: object, expected: type) -> object:
    if expected is int and isinstance(raw, float) and not raw.is_integer():
        raise ValueError(f"{name}={raw!r} 必须是整数")
    try:
        value = expected(raw)  # type: ignore[call-arg]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}={raw!r} 类型非法") from exc

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name}={raw!r} 必须是有限数字")

    bounds = _PARAM_BOUNDS.get(name)
    if bounds is None:
        return value
    low, high, low_inclusive = bounds
    if low is not None:
        if low_inclusive and value < low:
            raise ValueError(f"{name}={value!r} 不能小于 {low}")
        if not low_inclusive and value <= low:
            raise ValueError(f"{name}={value!r} 必须大于 {low}")
    if high is not None and value > high:
        raise ValueError(f"{name}={value!r} 不能大于 {high}")
    return value


class OverridesStore:
    def __init__(
        self,
        path: str | Path,
        *,
        backup_dir: str | Path | None = None,
        audit_path: str | Path | None = None,
        allowed_params: Mapping[str, type] | None = None,
        max_prompt_rule_chars: int = DEFAULT_MAX_PROMPT_RULE_CHARS,
        backup_keep: int = DEFAULT_BACKUP_KEEP,
    ) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.path.parent / "logs" / "overrides_bak"
        self.audit_path = Path(audit_path) if audit_path else self.path.parent / "logs" / "overrides_audit.jsonl"
        self.allowed_params = dict(allowed_params or ALLOWED_PARAMS)
        self.max_prompt_rule_chars = int(max_prompt_rule_chars)
        self.backup_keep = int(backup_keep)

        self._lock = threading.RLock()
        self._cache: dict = {}
        self._cache_key: tuple[int, int] | None = None
        self._has_cache = False
        self.last_error: str | None = None

    # ---------- 读取 ----------

    def load(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._load_locked())

    def snapshot(self) -> dict:
        return self.load()

    def param(self, name: str, default: T) -> T:
        with self._lock:
            data = self._load_locked()
            params = data.get("params")
            if not isinstance(params, dict) or name not in params:
                return default
            expected = self.allowed_params.get(name)
            if expected is None:
                return default
            raw = params[name]
            try:
                value = _coerce_param(name, raw, expected)
            except ValueError as exc:
                self.last_error = f"参数 {name} 非法: {raw!r} ({exc})"
                return default
            return value  # type: ignore[return-value]

    def prompt_rule(self) -> str:
        with self._lock:
            data = self._load_locked()
            value = data.get("prompt_rule")
            if not isinstance(value, str):
                return ""
            value = value.strip()
            if len(value) > self.max_prompt_rule_chars:
                self.last_error = (
                    f"prompt_rule 超过 {self.max_prompt_rule_chars} 字，已截断"
                )
                return value[: self.max_prompt_rule_chars]
            return value

    # ---------- 写入 ----------

    def apply_param(self, name: str, value: object) -> dict:
        expected = self.allowed_params.get(name)
        if expected is None:
            raise ConfigurationError(
                f"不允许的参数: {name}",
                detail={"name": name, "allowed": sorted(self.allowed_params)},
            )
        try:
            coerced = _coerce_param(name, value, expected)
        except ValueError as exc:
            raise ConfigurationError(
                f"参数值非法: {name}={value!r}",
                detail={"name": name, "value": value},
            ) from exc

        with self._lock:
            data = copy.deepcopy(self._load_locked())
            params = data.setdefault("params", {})
            if not isinstance(params, dict):
                params = {}
                data["params"] = params
            params[name] = coerced
            self._write_locked(data, event="param", changes={name: coerced})
            return copy.deepcopy(data)

    def apply_prompt_rule(self, rule: str) -> dict:
        rule = (rule or "").strip()
        if not rule:
            raise ConfigurationError("prompt_rule 为空")
        if len(rule) > self.max_prompt_rule_chars:
            raise ConfigurationError(
                f"prompt_rule 超过 {self.max_prompt_rule_chars} 字",
                detail={"length": len(rule)},
            )
        with self._lock:
            data = copy.deepcopy(self._load_locked())
            data["prompt_rule"] = rule
            self._write_locked(data, event="prompt_rule", changes={"prompt_rule": rule})
            return copy.deepcopy(data)

    def apply_change(self, plan: Mapping[str, object]) -> dict:
        action = plan.get("action")
        if action == "prompt_rule":
            return self.apply_prompt_rule(str(plan.get("prompt_rule") or ""))
        if action == "param_change":
            param = plan.get("param")
            if not isinstance(param, Mapping):
                raise ConfigurationError("param_change 缺少 param")
            return self.apply_param(str(param.get("name") or ""), param.get("value"))
        raise ConfigurationError(f"不支持的 action: {action}")

    def rollback(self, snapshot: Mapping[str, object]) -> dict:
        if not isinstance(snapshot, Mapping):
            raise ConfigurationError("rollback snapshot 必须是对象")
        with self._lock:
            data = copy.deepcopy(dict(snapshot))
            self._write_locked(data, event="rollback", changes={"snapshot": True})
            return copy.deepcopy(data)

    def reload(self) -> None:
        with self._lock:
            self._cache = {}
            self._cache_key = None
            self._has_cache = False

    # ---------- 内部 ----------

    def _file_key(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _load_locked(self) -> dict:
        key = self._file_key()
        if self._has_cache and key == self._cache_key:
            return self._cache

        if key is None:
            self._cache = {}
            self._cache_key = None
            self._has_cache = True
            return self._cache

        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, json.JSONDecodeError) as exc:
            self.last_error = f"读取 overrides 失败: {exc}"
            data = {}

        if not isinstance(data, dict):
            self.last_error = "overrides 顶层必须是对象"
            data = {}

        self._cache = data
        self._cache_key = key
        self._has_cache = True
        return self._cache

    def _write_locked(self, data: dict, *, event: str, changes: Mapping[str, object]) -> None:
        self._backup_locked()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f"{self.path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

        self._cache = copy.deepcopy(data)
        self._cache_key = self._file_key()
        self._has_cache = True
        self._audit_locked(event, changes)

    def _backup_locked(self) -> None:
        if not self.path.exists():
            return
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        target = self.backup_dir / f"{self.path.stem}.{stamp}{self.path.suffix}"
        try:
            shutil.copy2(self.path, target)
        except OSError as exc:
            self.last_error = f"备份 overrides 失败: {exc}"
            return
        backups = sorted(self.backup_dir.glob(f"{self.path.stem}.*{self.path.suffix}"))
        for old in backups[: max(0, len(backups) - self.backup_keep)]:
            try:
                old.unlink()
            except OSError:
                pass

    def _audit_locked(self, event: str, changes: Mapping[str, object]) -> None:
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            "path": str(self.path),
            "changes": changes,
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.last_error = f"写审计日志失败: {exc}"
