"""部署 profile：端口、数据目录、overrides 与功能开关。

profile 只表达“部署差异”，不承载业务规则。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ProfileFlags:
    multiturn: bool = True
    disco: bool = True
    agent: bool = False


@dataclass(frozen=True)
class Profile:
    name: str
    port: int
    channel: int
    data_dir: Path
    overrides_path: Path
    cookie_name: str
    flags: ProfileFlags


PROFILES: dict[str, Profile] = {
    "prod": Profile(
        name="prod",
        port=8000,
        channel=8000,
        data_dir=BASE_DIR / "var",
        overrides_path=BASE_DIR / "auto_optimization" / "overrides.prod.json",
        cookie_name="kb_session_8000",
        flags=ProfileFlags(multiturn=True, disco=True, agent=False),
    ),
    "experiment": Profile(
        name="experiment",
        port=8001,
        channel=8001,
        data_dir=BASE_DIR / "var",
        overrides_path=BASE_DIR / "auto_optimization" / "overrides.json",
        cookie_name="kb_session_8001",
        flags=ProfileFlags(multiturn=True, disco=True, agent=False),
    ),
    "test": Profile(
        name="test",
        port=8002,
        channel=8002,
        data_dir=BASE_DIR / "var" / "test",
        overrides_path=BASE_DIR / "auto_optimization" / "overrides.json",
        cookie_name="kb_session_8002",
        flags=ProfileFlags(multiturn=True, disco=True, agent=False),
    ),
}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise KeyError(f"未知 profile: {name}；可选 {sorted(PROFILES)}") from exc
