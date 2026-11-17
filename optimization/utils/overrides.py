"""overrides 读写与受控变更：统一委托给 config.OverridesStore。

server_auto(8001) 读 overrides.json；server(8000) 读 overrides.prod.json。
Agent 只允许修改试验 overrides，回归通过后由 trigger 同步到生产。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from config.overrides import ALLOWED_PARAMS, OverridesStore
from domain.errors import ConfigurationError
from infra.paths import AUTO_OPT_DIR

OVERRIDES_FILE = os.path.join(AUTO_OPT_DIR, "overrides.json")
PROD_OVERRIDES_FILE = os.path.join(AUTO_OPT_DIR, "overrides.prod.json")
BACKUP_DIR = os.path.join(AUTO_OPT_DIR, "logs", "overrides_bak")


def _store(path=None) -> OverridesStore:
    return OverridesStore(
        path or OVERRIDES_FILE,
        backup_dir=BACKUP_DIR,
        audit_path=os.path.join(BACKUP_DIR, "overrides_audit.jsonl"),
    )


def load_overrides(path=None):
    return _store(path).load()


def save_overrides(data, path=None):
    target = Path(path or OVERRIDES_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def snapshot_overrides():
    return {"data": _store().snapshot()}


def rollback_overrides(snapshot):
    _store().rollback(dict(snapshot.get("data", {})))


def apply_change(plan, snapshot=None):
    """应用受控变更到 overrides.json。仅允许 prompt_rule 与白名单参数。"""
    try:
        return _store().apply_change(plan)
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc


def sync_to_production():
    """回归验证通过后：试验 overrides → 生产 overrides。"""
    data = load_overrides()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    try:
        shutil.copy(PROD_OVERRIDES_FILE,
                    os.path.join(BACKUP_DIR, f"overrides.prod.{stamp}.json"))
    except FileNotFoundError:
        pass
    save_overrides(data, path=PROD_OVERRIDES_FILE)
    return data
