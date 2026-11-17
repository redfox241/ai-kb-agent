"""组合根配置接线：Profile / Settings / Overrides 唯一来源。"""
from __future__ import annotations

import bootstrap as bootstrap
from config.overrides import OverridesStore
from config.profiles import PROFILES
from config.settings import Settings


def test_bootstrap_uses_profile_and_settings():
    assert bootstrap.PROFILE in PROFILES.values()
    assert isinstance(bootstrap.SETTINGS, Settings)
    assert bootstrap.PORT == bootstrap.PROFILE.port or bootstrap.PORT


def test_bootstrap_uses_overrides_store():
    assert isinstance(bootstrap.OVERRIDES, OverridesStore)
    assert bootstrap.get_param("NOT_A_REAL_PARAM", 7) == 7


def test_profile_by_port():
    assert bootstrap._profile_for(8000).name == "prod"
    assert bootstrap._profile_for(8001).name == "experiment"
    assert bootstrap._profile_for(8002).name == "test"
