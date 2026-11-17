"""运行时路径与环境加载（基础设施层）。

设计约定：
- BASE_DIR 是仓库根，不是包根；
- 运行数据默认位于 `var/`（可用环境变量覆盖）；
- 自优化数据仍保留在 `auto_optimization/`。
"""
import json
import os
from pathlib import Path

BASE_DIR = str(Path(__file__).resolve().parents[1])


def load_env(path=None):
    """极简 dotenv 加载，已存在的环境变量优先。"""
    p = path or os.path.join(BASE_DIR, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    # 密钥不落盘 .env：从 Claude settings 注入 ANTHROPIC_AUTH_TOKEN 等
    claude_env = Path.home() / ".claude" / "settings.json"
    if claude_env.exists():
        try:
            for k, v in (json.loads(claude_env.read_text()).get("env") or {}).items():
                if isinstance(v, str) and v:
                    os.environ.setdefault(k, v)
        except Exception:
            pass
    # 本机服务（Ollama 等）不走系统代理，避免 localhost 被代理返回 502
    local_no_proxy = "localhost,127.0.0.1,::1"
    for key in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(key, "")
        parts = [x.strip() for x in existing.split(",") if x.strip()]
        for item in local_no_proxy.split(","):
            if item not in parts:
                parts.append(item)
        os.environ[key] = ",".join(parts)


def get_path(key, default):
    return os.environ.get(key) or os.path.join(BASE_DIR, default)


VAULT_PATH = get_path("RAG_VAULT_PATH", "vault")
INDEX_PATH = get_path("RAG_INDEX_PATH", "var/index")
LOG_DIR = os.path.join(BASE_DIR, "logs")
QA_ARCHIVE_DIR = os.path.join(BASE_DIR, "qa_archive")
AUTO_OPT_DIR = os.path.join(BASE_DIR, "auto_optimization")
DATA_DIR = get_path("AIKB_DATA_DIR", "var")


def ensure_dirs():
    for d in (LOG_DIR, QA_ARCHIVE_DIR, os.path.join(AUTO_OPT_DIR, "logs"),
              os.path.join(AUTO_OPT_DIR, "logs", "agent"),
              os.path.join(AUTO_OPT_DIR, "benchmark"),
              os.path.join(DATA_DIR, "db"), INDEX_PATH):
        os.makedirs(d, exist_ok=True)


load_env()
ensure_dirs()
