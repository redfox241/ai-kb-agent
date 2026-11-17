"""架构门禁：四层 import 规则 + shim / 资产约束。

代码已从 ai_kb/ 包扁平化到仓库根目录，分层目录直接位于根：
- domain：仅标准库；
- application（qa / retrieval / ingestion / conversations / analytics）：只依赖 domain；
- infra：只实现端口，不依赖 api / application；
- api：不直接依赖 infra 具体实现（组合根除外）；
- bootstrap：唯一允许同时 import 四层；
- server*.py：薄 shim。
"""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

LAYER_DIRS = (
    "domain",
    "config",
    "ingestion",
    "retrieval",
    "qa",
    "conversations",
    "analytics",
    "optimization",
    "api",
    "web",
    "infra",
    "cli",
)
APPLICATION_DIRS = ("qa", "retrieval", "ingestion", "conversations", "analytics")
DOMAIN_FORBIDDEN = {
    "fastapi", "starlette", "httpx", "sqlite3", "numpy", "yaml",
    "pydantic", "requests", "uvicorn",
}
APPLICATION_FORBIDDEN = {
    "fastapi", "starlette", "httpx", "sqlite3", "pydantic", "requests", "uvicorn",
    "api", "bootstrap", "web", "infra", "config", "server", "ui",
}
INFRA_FORBIDDEN = {
    "api", "bootstrap", "web", "server", "ui",
    *APPLICATION_DIRS,
}
API_FORBIDDEN = {"bootstrap", "server", "ui", "infra"}
WEB_FORBIDDEN = {
    "api", "bootstrap", "server", "ui", "infra",
    *APPLICATION_DIRS,
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            modules.add("." * node.level + module if node.level else module)
    return modules


def _roots(modules: set[str]) -> set[str]:
    """绝对 import 的顶层段；相对 import（. 开头）不计入。"""
    roots: set[str] = set()
    for module in modules:
        if module and not module.startswith("."):
            roots.add(module.split(".", 1)[0])
    return roots


def _walk(directory: str):
    return (ROOT / directory).rglob("*.py")


def test_layers_are_top_level_directories():
    missing = [name for name in LAYER_DIRS if not (ROOT / name).is_dir()]
    assert not missing, missing
    assert not (ROOT / "ai_kb").exists(), "ai_kb/ 包已扁平化到根目录，不应再存在"


def test_domain_has_no_framework_or_infra_imports():
    for path in _walk("domain"):
        roots = _roots(_imports(path))
        assert not (roots & DOMAIN_FORBIDDEN), (path.name, roots & DOMAIN_FORBIDDEN)
        assert roots <= {"__future__", "typing", "dataclasses", "enum", "json", "re"}, path.name


def test_application_layers_depend_on_domain_only():
    for directory in APPLICATION_DIRS:
        for path in _walk(directory):
            roots = _roots(_imports(path))
            assert not (roots & APPLICATION_FORBIDDEN), (
                path, roots & APPLICATION_FORBIDDEN,
            )


def test_optimization_is_an_isolated_agent_sidecar():
    """优化器是旁路工具边界：可用 infra/analytics，但不得依赖 api/server/web。"""
    forbidden = {"api", "bootstrap", "server", "ui", "web"}
    for path in _walk("optimization"):
        roots = _roots(_imports(path))
        assert not (roots & forbidden), (path, roots & forbidden)


def test_infra_does_not_import_api_or_application():
    for path in _walk("infra"):
        roots = _roots(_imports(path))
        assert not (roots & INFRA_FORBIDDEN), (path, roots & INFRA_FORBIDDEN)


def test_api_does_not_import_infra_concretes():
    for path in _walk("api"):
        roots = _roots(_imports(path))
        assert not (roots & API_FORBIDDEN), (path, roots & API_FORBIDDEN)


def test_web_has_no_backend_dependencies():
    for path in _walk("web"):
        roots = _roots(_imports(path))
        assert not (roots & WEB_FORBIDDEN), (path, roots & WEB_FORBIDDEN)


def test_server_is_a_thin_shim():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert len(source.splitlines()) <= 60
    assert "@app." not in source
    modules = _imports(ROOT / "server.py")
    assert modules <= {"bootstrap"}, modules


def test_root_python_modules_are_entrypoints_only():
    allowed = {"server.py", "server_auto.py", "server_v3.py", "bootstrap.py", "version.py"}
    roots = {path.name for path in ROOT.glob("*.py")}
    assert roots <= allowed, roots - allowed


def test_prompt_assets_are_versioned_markdown():
    prompts = ROOT / "qa" / "prompts"
    expected = {"answer.md", "rewrite.md", "disco.md", "summarize.md"}
    assert expected <= {path.name for path in prompts.glob("*.md")}
    for name in expected:
        text = (prompts / name).read_text(encoding="utf-8")
        assert text.startswith("---"), name
        assert "version:" in text.split("---", 2)[1], name


def test_layers_have_no_sys_path_hacks():
    for directory in LAYER_DIRS:
        for path in _walk(directory):
            source = path.read_text(encoding="utf-8")
            assert "sys.path.insert" not in source, path


def test_web_assets_are_outside_python():
    web = ROOT / "web"
    assert list((web / "templates").glob("*.html"))
    assert list((web / "static").glob("*.css"))
    assert list((web / "static").glob("*.js"))
    for path in web.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "<!DOCTYPE html" not in source, path


def test_bootstrap_is_the_only_composition_root():
    allowed = {ROOT / "bootstrap.py"}
    for directory in LAYER_DIRS:
        for path in _walk(directory):
            if path in allowed:
                continue
            source = path.read_text(encoding="utf-8")
            if "optimization" in path.parts:
                continue  # 优化器旁路允许直接构造 infra
            assert "from infra" not in source or path.parts[-2] in {
                "infra", "cli",
            }, path
