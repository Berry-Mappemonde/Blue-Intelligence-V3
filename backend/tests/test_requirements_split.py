"""Production installs requirements.txt only; test libraries live in requirements-dev.txt."""
from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
TEST_ONLY = {"pytest", "pytest-split", "pytest-xdist", "iniconfig", "pluggy", "execnet",
             "mongomock", "mongomock-motor", "sentinels"}


def _pins(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            name, _, version = line.partition("==")
            out[name.strip().lower()] = version.strip()
    return out


def test_production_requirements_have_no_test_library():
    prod = _pins(BACKEND / "requirements.txt")
    assert not TEST_ONLY & set(prod), sorted(TEST_ONLY & set(prod))
    assert not {n for n in prod if n.startswith("pytest")}


def test_dev_requirements_include_production_and_pin_the_test_libraries():
    text = (BACKEND / "requirements-dev.txt").read_text(encoding="utf-8")
    assert re.search(r"^-r requirements\.txt$", text, re.M)
    dev = _pins(BACKEND / "requirements-dev.txt")
    assert TEST_ONLY <= set(dev)
    assert all(dev[n] for n in TEST_ONLY), "every test library is pinned"
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
