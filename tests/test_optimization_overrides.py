"""P1：optimization overrides 同步/回滚/白名单。"""
from __future__ import annotations

import json

import pytest

from optimization.utils import overrides as ov


def _paths(tmp_path, monkeypatch):
    exp = tmp_path / "overrides.json"
    prod = tmp_path / "overrides.prod.json"
    bak = tmp_path / "backups"
    monkeypatch.setattr(ov, "OVERRIDES_FILE", str(exp))
    monkeypatch.setattr(ov, "PROD_OVERRIDES_FILE", str(prod))
    monkeypatch.setattr(ov, "BACKUP_DIR", str(bak))
    return exp, prod, bak


def test_sync_to_production(tmp_path, monkeypatch):
    exp, prod, bak = _paths(tmp_path, monkeypatch)
    exp.write_text(json.dumps({"params": {"TOP_K": 20}, "prompt_rule": "规则"}),
                   encoding="utf-8")
    prod.write_text(json.dumps({"params": {"TOP_K": 10}, "prompt_rule": "旧规则"}),
                    encoding="utf-8")

    data = ov.sync_to_production()

    assert data["params"]["TOP_K"] == 20
    assert json.loads(prod.read_text(encoding="utf-8"))["prompt_rule"] == "规则"
    assert list(bak.glob("overrides.prod.*.json"))


def test_apply_change_and_rollback(tmp_path, monkeypatch):
    exp, _, bak = _paths(tmp_path, monkeypatch)
    exp.write_text(json.dumps({"params": {"TOP_K": 10}}), encoding="utf-8")
    snapshot = ov.snapshot_overrides()

    ov.apply_change({"action": "param_change",
                     "param": {"name": "TOP_K", "value": 15}})
    assert ov.load_overrides()["params"]["TOP_K"] == 15

    ov.rollback_overrides(snapshot)
    assert ov.load_overrides()["params"]["TOP_K"] == 10


def test_apply_change_rejects_unknown(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        ov.apply_change({"action": "param_change",
                         "param": {"name": "NOT_ALLOWED", "value": 1}})
    with pytest.raises(ValueError):
        ov.apply_change({"action": "prompt_rule", "prompt_rule": ""})
