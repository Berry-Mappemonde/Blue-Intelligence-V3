"""A run never writes tracked files (pilot dry-run finding).

What a zone learns during a run (seed URLs that produced a catalogue, bootstrapped
State domains) goes to runs/<run_id>/learned_exceptions.jsonl, not to the tracked
backend/data/poe_exceptions.json. Offline: all I/O of generate_zone_poe is stubbed.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import poe_pipeline as poe  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
URL = "https://www.martinique.gouv.fr/douane"
ZONE = {"mrgid": 33178, "name": "Martinique", "iso2": "MQ", "sov_iso2": "FR"}
CATALOG_TEXT = (f"[SOURCE: {URL}]\nPorts d'entrée désignés : Fort-de-France ; Le Marin ; "
                "Saint-Pierre.\n")


def _git_status() -> str | None:
    if shutil.which("git") is None or not (REPO / ".git").exists():
        return None
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                         cwd=REPO, capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


@pytest.fixture
def guarded_exceptions_file():
    """Byte snapshot of the tracked exceptions file, restored after the test whatever
    happens, so a regression can never leave the checkout modified."""
    path = poe.EXCEPTIONS_FILE
    before = path.read_bytes() if path.exists() else None
    yield path, before
    after = path.read_bytes() if path.exists() else None
    if after != before:
        if before is None:
            path.unlink()
        else:
            path.write_bytes(before)


def _harness(monkeypatch, tmp_path):
    import app.db

    async def no_settings():
        return {}

    async def find_sources(zone, *args, **kwargs):
        return [{"url": URL, "official": True}], True, None

    async def collect(official, log, rec=None, **kwargs):
        return [CATALOG_TEXT], {}, [{"url": URL, "official": True}], {}

    async def extract(zone, context, used_sources, log, rec=None, run=None,
                      catalog_text=None, settings=None):
        return [{"name": "Fort-de-France", "source_urls": [URL], "extraction_engine": "llm"}]

    async def persist_run(db, zone, docs, status, used_sources, hashes, run, rec, log):
        return {"mrgid": zone["mrgid"], "status": status, "poe_count": len(docs)}

    async def persist_live(*a, **k):
        raise AssertionError("a run never writes the live collections")

    async def nothing(*a, **k):
        return 0

    class _Zones:
        async def find_one(self, query):
            return dict(ZONE)

    class _DB:
        eez_zones = _Zones()

    monkeypatch.setenv("BI_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(app.db, "get_settings", no_settings)
    monkeypatch.setattr(poe, "_find_sources", find_sources)
    monkeypatch.setattr(poe, "_collect_texts", collect)
    # The catalogue parser finds ports: the zone "learns" its source URL.
    monkeypatch.setattr(poe, "extract_structured_ports",
                        lambda raw: [{"name": "Fort-de-France"}] if "Fort-de-France" in raw else [])
    monkeypatch.setattr(poe, "_extract_and_geocode", extract)
    monkeypatch.setattr(poe, "_store_evidence", nothing)
    monkeypatch.setattr(poe, "_judge_zone_run", nothing)
    monkeypatch.setattr(poe, "_persist_zone_run", persist_run)
    monkeypatch.setattr(poe, "_persist_zone", persist_live)
    monkeypatch.setattr(poe, "ZoneRecorder", lambda *a, **k: None)
    monkeypatch.setattr(poe, "build_whitelist", lambda *a, **k: ["gouv.fr"])
    # Start from an exceptions dict with nothing learned for MQ yet.
    monkeypatch.setattr(poe, "load_exceptions", lambda: {"manual": {}, "auto": {}})
    return _DB()


def test_a_run_leaves_the_git_tree_clean(monkeypatch, tmp_path, guarded_exceptions_file):
    path, before = guarded_exceptions_file
    status_before = _git_status()
    db = _harness(monkeypatch, tmp_path)
    run = SimpleNamespace(run_id="run-clean", variant="tinyfish", recorder=None)

    result = asyncio.run(poe.generate_zone_poe(db, 33178, run=run))

    assert result["poe_count"] == 1
    assert (path.read_bytes() if path.exists() else None) == before
    if status_before is not None:
        assert _git_status() == status_before
    learned = tmp_path / "runs" / "run-clean" / poe.LEARNED_NAME
    rows = [json.loads(line) for line in learned.read_text(encoding="utf-8").splitlines()]
    assert rows == [{**rows[0], "kind": "seed_urls", "mrgid": 33178, "iso2": "MQ",
                     "values": [URL]}]
    # The context is reset after the zone: a later live generation writes as before.
    assert poe._learning_run.get() is None


def test_default_run_folder_is_gitignored():
    if _git_status() is None:
        pytest.skip("not a git checkout")
    probe = "backend/data/runs/some-run/" + poe.LEARNED_NAME
    out = subprocess.run(["git", "check-ignore", "-q", probe], cwd=REPO)
    assert out.returncode == 0


def test_bootstrap_and_seed_urls_go_to_the_run_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv("BI_RUNS_DIR", str(tmp_path / "runs"))
    saved = []
    monkeypatch.setattr(poe, "save_exceptions", lambda exc: saved.append(exc))
    token = poe._learning_run.set("run-learn")
    try:
        assert poe._persist_learned({"auto": {"MQ": ["x.mq"]}}, "auto", ZONE, ["x.mq"]) == "run"
        added = poe.remember_seed_urls(ZONE, [URL], {"manual": {}, "auto": {}})
    finally:
        poe._learning_run.reset(token)
    assert added == [URL]
    assert saved == []
    rows = [json.loads(line) for line in
            (tmp_path / "runs" / "run-learn" / poe.LEARNED_NAME).read_text().splitlines()]
    assert [(r["kind"], r["values"]) for r in rows] == [("auto", ["x.mq"]), ("seed_urls", [URL])]


def test_live_generation_still_saves_exceptions(monkeypatch):
    saved = []
    monkeypatch.setattr(poe, "save_exceptions", lambda exc: saved.append(exc))
    assert poe._learning_run.get() is None
    added = poe.remember_seed_urls(ZONE, [URL], {"manual": {}, "auto": {}})
    assert added == [URL] and len(saved) == 1
    assert saved[0]["seed_urls"]["MQ"] == [URL]
