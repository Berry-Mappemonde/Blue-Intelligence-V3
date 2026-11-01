"""Production installs requirements.txt only; test libraries and linters live in
requirements-dev.txt."""
from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
TEST_ONLY = {"pytest", "pytest-split", "pytest-xdist", "iniconfig", "pluggy", "execnet",
             "mongomock", "mongomock-motor", "sentinels"}
LINTERS = {"black", "flake8", "isort", "mypy", "mypy_extensions", "pycodestyle", "pyflakes",
           "mccabe"}
DEV_ONLY = TEST_ONLY | LINTERS


def _norm(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _pins(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            name, _, version = line.partition("==")
            out[_norm(name)] = version.strip()
    return out


def test_production_requirements_have_no_test_library_or_linter():
    prod = _pins(BACKEND / "requirements.txt")
    dev_only = {_norm(n) for n in DEV_ONLY}
    assert not dev_only & set(prod), sorted(dev_only & set(prod))
    assert not {n for n in prod if n.startswith(("pytest", "flake8", "mypy"))}


def test_dev_requirements_include_production_and_pin_tests_and_linters():
    text = (BACKEND / "requirements-dev.txt").read_text(encoding="utf-8")
    assert re.search(r"^-r requirements\.txt$", text, re.M)
    dev = _pins(BACKEND / "requirements-dev.txt")
    dev_only = {_norm(n) for n in DEV_ONLY}
    assert dev_only <= set(dev), sorted(dev_only - set(dev))
    assert all(dev[n] for n in dev_only), "every dev-only package is pinned"
    assert not set(dev) & set(_pins(BACKEND / "requirements.txt")), "no duplicate pins"


def test_ci_installs_dev_requirements_and_deploy_installs_production_only():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pip install -r requirements-dev.txt" in ci
    assert "pip install -r requirements.txt" not in ci
    deploy = REPO / "infra" / "vps" / "deploy-app.sh"
    if deploy.exists():
        text = deploy.read_text(encoding="utf-8")
        assert "requirements-dev" not in text
        assert "-r requirements.txt" in text
