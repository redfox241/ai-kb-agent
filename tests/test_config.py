"""PR-03：Settings / Profile / Overrides 契约测试。"""
from __future__ import annotations

import json
import os
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from config import PROFILES, OverridesStore, Settings
from config.profiles import BASE_DIR
from domain import ConfigurationError


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_profiles_are_explicit_and_consistent():
    assert PROFILES["prod"].port == 8000
    assert PROFILES["experiment"].port == 8001
    assert PROFILES["test"].port == 8002

    assert PROFILES["prod"].channel == 8000
    assert PROFILES["experiment"].channel == 8001
    assert PROFILES["test"].channel == 8002

    # 索引共享：prod / experiment 使用同一 var；test 独立。
    assert PROFILES["prod"].data_dir == BASE_DIR / "var"
    assert PROFILES["experiment"].data_dir == PROFILES["prod"].data_dir
    assert PROFILES["test"].data_dir == BASE_DIR / "var" / "test"

    for profile in PROFILES.values():
        assert profile.cookie_name == f"kb_session_{profile.port}"
        assert profile.overrides_path.name.startswith("overrides")


def test_settings_defaults():
    settings = Settings.from_env(PROFILES["prod"], env={})

    assert settings.data_dir == BASE_DIR / "var"
    assert settings.vault_path == BASE_DIR / "vault"
    assert settings.index_path == BASE_DIR / "var" / "index"
    assert settings.prompt_dir == BASE_DIR / "qa" / "prompts"
    assert settings.db.sessions == BASE_DIR / "var" / "db" / "sessions.db"
    assert settings.db.analytics == BASE_DIR / "var" / "db" / "analytics.db"

    assert settings.llm.style == "anthropic"
    assert settings.llm.max_tokens == 8000
    assert settings.embed.model == "nomic-embed-text"
    assert settings.ingestion.chunk_size == 800
    assert settings.retrieval.top_k == 30
    assert settings.retrieval.similarity_threshold == 0.3
    assert settings.multiturn.enabled is True
    assert settings.multiturn.max_history_turns == 3
    assert settings.jobs.max_workers == 4


def test_settings_env_overrides_profile():
    env = {
        "AIKB_DATA_DIR": "/tmp/aikb-data",
        "RAG_VAULT_PATH": "/tmp/aikb-vault",
        "RAG_INDEX_PATH": "/tmp/aikb-index",
        "RAG_SESSION_DB": "/tmp/aikb-sessions.db",
        "RAG_EVAL_DB": "/tmp/aikb-analytics.db",
        "LLM_API_STYLE": "openai",
        "LLM_BASE_URL": "https://example.test/v1/",
        "LLM_MODEL": "test-model",
        "LLM_MAX_TOKENS": "4096",
        "OLLAMA_EMBED_MODEL": "custom-embed",
        "CHUNK_SIZE": "1000",
        "CHUNK_OVERLAP": "200",
        "TOP_K": "20",
        "SIMILARITY_THRESHOLD": "0.42",
        "MULTITURN_ENABLED": "0",
        "MAX_HISTORY_TURNS": "5",
        "REPEAT_PENALTY": "0.9",
        "JOB_MAX_WORKERS": "2",
    }
    settings = Settings.from_env(PROFILES["prod"], env=env)

    assert settings.data_dir == Path("/tmp/aikb-data")
    assert settings.vault_path == Path("/tmp/aikb-vault")
    assert settings.index_path == Path("/tmp/aikb-index")
    assert settings.db.sessions == Path("/tmp/aikb-sessions.db")
    assert settings.db.analytics == Path("/tmp/aikb-analytics.db")
    assert settings.llm.style == "openai"
    assert settings.llm.base_url == "https://example.test/v1"
    assert settings.llm.model == "test-model"
    assert settings.llm.max_tokens == 4096
    assert settings.embed.model == "custom-embed"
    assert settings.ingestion.chunk_size == 1000
    assert settings.ingestion.chunk_overlap == 200
    assert settings.retrieval.top_k == 20
    assert settings.retrieval.similarity_threshold == 0.42
    assert settings.multiturn.enabled is False
    assert settings.multiturn.max_history_turns == 5
    assert settings.multiturn.repeat_penalty == 0.9
    assert settings.jobs.max_workers == 2


def test_settings_relative_env_paths_are_rooted_at_project():
    settings = Settings.from_env(PROFILES["prod"], env={"RAG_INDEX_PATH": "custom-index"})
    assert settings.index_path == BASE_DIR / "custom-index"


@pytest.mark.parametrize("key,value", [
    ("TOP_K", "abc"),
    ("TOP_K", "0"),
    ("SIMILARITY_THRESHOLD", "1.5"),
    ("CHUNK_OVERLAP", "800"),
    ("MULTITURN_ENABLED", "maybe"),
    ("REPEAT_PENALTY", "0"),
    ("LLM_API_STYLE", "grpc"),
    ("REWRITE_MODE", "sometimes"),
])
def test_settings_reject_invalid_values(key, value):
    with pytest.raises(ConfigurationError) as exc_info:
        Settings.from_env(PROFILES["prod"], env={key: value})
    assert exc_info.value.detail["key"] == key


def test_settings_are_frozen():
    settings = Settings.from_env(PROFILES["prod"], env={})
    with pytest.raises(FrozenInstanceError):
        settings.data_dir = Path("/tmp")  # type: ignore[misc]


def test_overrides_defaults_when_file_missing(tmp_path):
    store = OverridesStore(tmp_path / "overrides.json")
    assert store.load() == {}
    assert store.param("TOP_K", 30) == 30
    assert store.prompt_rule() == ""


def test_overrides_reads_whitelisted_params_and_prompt_rule(tmp_path):
    path = tmp_path / "overrides.json"
    _write(path, {
        "params": {"TOP_K": "12", "SIMILARITY_THRESHOLD": 0.25, "NOT_ALLOWED": 99},
        "prompt_rule": "  优先引用最近的来源。  ",
    })
    store = OverridesStore(path)

    assert store.param("TOP_K", 30) == 12
    assert store.param("SIMILARITY_THRESHOLD", 0.3) == 0.25
    assert store.param("NOT_ALLOWED", 99) == 99
    assert store.prompt_rule() == "优先引用最近的来源。"


def test_overrides_invalid_and_out_of_range_values_fall_back(tmp_path):
    path = tmp_path / "overrides.json"
    _write(path, {"params": {"TOP_K": "abc", "SIMILARITY_THRESHOLD": 1.8}})
    store = OverridesStore(path)

    assert store.param("TOP_K", 30) == 30
    assert store.param("SIMILARITY_THRESHOLD", 0.3) == 0.3
    assert store.last_error


def test_overrides_cache_invalidates_on_file_change(tmp_path):
    path = tmp_path / "overrides.json"
    _write(path, {"params": {"TOP_K": 10}})
    store = OverridesStore(path)
    assert store.param("TOP_K", 30) == 10

    _write(path, {"params": {"TOP_K": 20}})
    future = time.time() + 2
    os.utime(path, (future, future))

    assert store.param("TOP_K", 30) == 20


def test_overrides_apply_param_writes_backup_and_audit(tmp_path):
    path = tmp_path / "overrides.json"
    _write(path, {"params": {"TOP_K": 10}})
    backup_dir = tmp_path / "backups"
    audit_path = tmp_path / "audit.jsonl"
    store = OverridesStore(path, backup_dir=backup_dir, audit_path=audit_path)

    data = store.apply_param("TOP_K", 15)

    assert data["params"]["TOP_K"] == 15
    assert json.loads(path.read_text(encoding="utf-8"))["params"]["TOP_K"] == 15
    assert list(backup_dir.glob("overrides.*.json"))
    assert "param" in audit_path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp.*"))


def test_overrides_apply_rejects_unknown_and_invalid(tmp_path):
    store = OverridesStore(tmp_path / "overrides.json")

    with pytest.raises(ConfigurationError):
        store.apply_param("NOT_ALLOWED", 1)
    with pytest.raises(ConfigurationError):
        store.apply_param("SIMILARITY_THRESHOLD", 2.0)
    with pytest.raises(ConfigurationError):
        store.apply_param("TOP_K", 1.5)


def test_overrides_prompt_rule_apply_and_rollback(tmp_path):
    path = tmp_path / "overrides.json"
    _write(path, {"params": {"TOP_K": 10}, "prompt_rule": "旧规则"})
    store = OverridesStore(path, max_prompt_rule_chars=100)

    snapshot = store.snapshot()
    store.apply_prompt_rule("新规则：先给结论。")
    assert store.prompt_rule() == "新规则：先给结论。"

    store.rollback(snapshot)
    assert store.prompt_rule() == "旧规则"
    assert store.param("TOP_K", 30) == 10

    with pytest.raises(ConfigurationError):
        store.apply_prompt_rule("")
    with pytest.raises(ConfigurationError):
        store.apply_prompt_rule("x" * 101)


def test_overrides_apply_change_protocol(tmp_path):
    store = OverridesStore(tmp_path / "overrides.json")

    store.apply_change({
        "action": "param_change",
        "param": {"name": "TOP_K", "value": 18},
    })
    assert store.param("TOP_K", 30) == 18

    store.apply_change({"action": "prompt_rule", "prompt_rule": "规则"})
    assert store.prompt_rule() == "规则"

    with pytest.raises(ConfigurationError):
        store.apply_change({"action": "unknown"})
