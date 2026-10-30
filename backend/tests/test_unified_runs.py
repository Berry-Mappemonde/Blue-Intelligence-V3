"""Unified run mechanism (P0-4): trace, manifest, resume, export, upload.

Offline: in-memory async fake of the Mongo collections, stubbed zone
pipeline, temp directories for runs and exports. No network, no keys.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "bi_test_unified_runs")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import events  # noqa: E402
from app.core.tasks import TaskState  # noqa: E402
from app.services import isolated_runs, object_storage, poe_runs  # noqa: E402
from app.services import project_runs  # noqa: E402
from app.services import run_artifacts as artifacts  # noqa: E402
from app.services import unified_runs  # noqa: E402


# ── in-memory async Mongo fake ───────────────────────────────────────

def _get(doc, key):
    cur = doc
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _match(doc, q):
    for k, v in (q or {}).items():
        val = _get(doc, k)
        if isinstance(v, dict) and any(op.startswith("$") for op in v):
            if "$in" in v and val not in v["$in"]:
                return False
            if "$exists" in v and (val is not None) != bool(v["$exists"]):
                return False
        elif val != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = [dict(d) for d in docs]

    def sort(self, key, direction=1):
        self._docs.sort(key=lambda d: (_get(d, key) is None, _get(d, key)),
                        reverse=direction == -1)
        return self

    def skip(self, n):
        self._docs = self._docs[int(n):]
        return self

    async def to_list(self, n=None):
        return list(self._docs if n is None else self._docs[:n])

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Coll:
    def __init__(self):
        self.docs: list[dict] = []
        self._n = 0

    async def create_index(self, *a, **k):
        return None

    async def insert_one(self, doc):
        doc = dict(doc)
        if "_id" not in doc:
            self._n += 1
            doc["_id"] = f"auto-{self._n}"
        self.docs.append(doc)

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)

    async def delete_many(self, q):
        self.docs = [d for d in self.docs if not _match(d, q)]

    async def find_one(self, q, proj=None):
        for d in self.docs:
            if _match(d, q):
                return dict(d)
        return None

    def find(self, q=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, q)])

    async def count_documents(self, q):
        return sum(1 for d in self.docs if _match(d, q))

    async def update_one(self, q, update, upsert=False):
        target = next((d for d in self.docs if _match(d, q)), None)
        if target is None:
            if not upsert:
                return
            target = {k: v for k, v in q.items() if not isinstance(v, dict)}
            target.update(update.get("$setOnInsert") or {})
            self.docs.append(target)
        target.update(update.get("$set") or {})
        for k, v in (update.get("$inc") or {}).items():
            head, _, tail = k.partition(".")
            if tail:
                target.setdefault(head, {})
                target[head][tail] = target[head].get(tail, 0) + v
            else:
                target[k] = target.get(k, 0) + v
        for k, v in (update.get("$push") or {}).items():
            target.setdefault(k, []).append(v)


class _DB:
    def __init__(self):
        self._c: dict[str, _Coll] = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, _Coll())

    def __getitem__(self, name):
        return getattr(self, name)


# ── fixtures ─────────────────────────────────────────────────────────

ZONES = [(1, "Alpha"), (2, "Bravo"), (3, "Charlie"), (4, "Delta")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    exports = tmp_path / "exports"
    monkeypatch.setenv("BI_RUNS_DIR", str(runs))
    monkeypatch.setenv("BI_EXPORT_DIR", str(exports))
    monkeypatch.setattr(events, "RUNS_DIR", tmp_path / "events")
    for name in ("BI_OBJECT_STORAGE_UPLOAD", "BI_OBJECT_STORAGE_ENDPOINT",
                 "BI_OBJECT_STORAGE_BUCKET"):
        monkeypatch.delenv(name, raising=False)

    import app.db as appdb
    from app.config import DEFAULT_SETTINGS

    async def _settings():
        return dict(DEFAULT_SETTINGS)

    monkeypatch.setattr(appdb, "get_settings", _settings)
    db = _DB()
    for mrgid, name in ZONES:
        db.eez_zones.docs.append({"_id": f"z{mrgid}", "mrgid": mrgid, "name": name})
    return {"db": db, "runs": runs, "exports": exports}


class _Crash(BaseException):
    """Process killed mid-run: not caught by the run's `except Exception`."""


def _zone_stub(calls: list, *, attempt: int, crash_on: int | None = None,
               ports_for=None):
    crashed = {"v": False}

    async def fake_zone(db, mrgid, logger=None, force=False, run=None):
        if crashed["v"]:
            raise _Crash()
        calls.append(mrgid)
        ports = ports_for(mrgid) if ports_for else [
            {"name": f"Port {mrgid}", "lat": 10.0 + mrgid, "lon": -60.0,
             "confidence": 50 + mrgid}]
        await db.poe_run_ports.delete_many({"run_id": run.run_id, "mrgid": mrgid})
        await db.poe_run_ports.insert_many([
            {"_id": f"{run.run_id}:{mrgid}:{i}:a{attempt}", "run_id": run.run_id,
             "mrgid": mrgid, "attempt": attempt, **p}
            for i, p in enumerate(ports)])
        if mrgid == crash_on:
            crashed["v"] = True  # ports written, zone doc not yet: torn zone
            raise _Crash()
        await db.poe_run_zones.update_one(
            {"run_id": run.run_id, "mrgid": mrgid},
            {"$set": {"run_id": run.run_id, "mrgid": mrgid, "status": "ia",
                      "poe_count": len(ports), "attempt": attempt,
                      "sources": [{"url": f"https://www.gov{mrgid}.example/poe"}]}},
            upsert=True)
        return {"status": "ia", "poe_count": len(ports)}

    return fake_zone


def _run(coro):
    return asyncio.run(coro)


# ── resume after a crash ─────────────────────────────────────────────

def test_resume_after_crash_keeps_completed_zones(env, monkeypatch):
    db = env["db"]
    calls1: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _zone_stub(calls1, attempt=1, crash_on=3))
    with pytest.raises(_Crash):
        _run(unified_runs.start_run(db, TaskState(), run_id="r-crash",
                                    concurrency=1, label="pilot"))
    assert calls1 == [1, 2, 3]
    assert db.poe_runs.docs[0]["state"] == "running"
    done_before = {d["mrgid"]: dict(d) for d in db.poe_run_zones.docs}
    ports_before = {d["_id"]: dict(d) for d in db.poe_run_ports.docs
                    if d["mrgid"] in (1, 2)}
    assert set(done_before) == {1, 2}
    assert artifacts.completed_units("r-crash") == {1, 2}

    calls2: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _zone_stub(calls2, attempt=2))
    summary = _run(unified_runs.start_run(db, TaskState(), run_id="r-crash",
                                          resume=True))
    assert sorted(calls2) == [3, 4]
    for mrgid, before in done_before.items():
        after = next(d for d in db.poe_run_zones.docs if d["mrgid"] == mrgid)
        assert after == before
    for pid, before in ports_before.items():
        assert next(d for d in db.poe_run_ports.docs if d["_id"] == pid) == before
    # The torn zone 3 is regenerated once, not duplicated.
    assert [d["attempt"] for d in db.poe_run_ports.docs if d["mrgid"] == 3] == [2]
    assert summary["zones_total"] == 4 and summary["zones_done"] == 4
    assert db.poe_runs.docs[0]["state"] == "done"

    trace = artifacts.read_trace("r-crash")
    kinds = [r["event"] for r in trace]
    assert kinds[0] == "run_start" and "run_resume" in kinds and kinds[-1] == "run_done"
    assert sorted(r["unit"] for r in trace if r["event"] == "unit_done") == [1, 2, 3, 4]


def test_trace_alone_marks_zone_done(env, monkeypatch):
    """A unit_done line is enough to skip a zone, even without its Mongo doc."""
    db = env["db"]
    db.poe_runs.docs.append({"_id": "r-trace", "state": "running", "label": "x",
                             "params": {"limit": 0, "only_zones": None,
                                        "concurrency": 1, "variant": "tinyfish"}})
    artifacts.append_trace("r-trace", "unit_done", unit=2)
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe", _zone_stub(calls, attempt=1))
    _run(unified_runs.start_run(db, TaskState(), run_id="r-trace", resume=True))
    assert sorted(calls) == [1, 3, 4]


def test_new_run_never_reuses_an_existing_id(env):
    db = env["db"]
    db.poe_runs.docs.append({"_id": "r-old", "state": "done"})
    with pytest.raises(unified_runs.RunExists):
        _run(unified_runs.start_run(db, TaskState(), run_id="r-old"))
    with pytest.raises(ValueError):
        _run(unified_runs.start_run(db, TaskState(), run_id="r-none", resume=True))
    with pytest.raises(unified_runs.UnsupportedLayer):
        _run(unified_runs.start_run(db, TaskState(), layer="marinas"))
    assert db.poe_runs.docs == [{"_id": "r-old", "state": "done"}]


# ── manifest and export ──────────────────────────────────────────────

def _finished_run(env, monkeypatch, run_id="r-exp", ports_for=None):
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _zone_stub(calls, attempt=1, ports_for=ports_for))
    _run(unified_runs.start_run(env["db"], TaskState(), run_id=run_id, zones=[1, 2]))
    return run_id


def test_run_manifest_hashes_match_files(env, monkeypatch):
    rid = _finished_run(env, monkeypatch)
    manifest = artifacts.read_manifest(artifacts.manifest_path(rid))
    assert manifest["run_id"] == rid and manifest["state"] == "done"
    assert manifest["git"]["sha"]
    assert manifest["counts"]["zones_done"] == 2
    assert manifest["counts"]["ports_total"] == 2
    assert manifest["sources"]["domains"] == ["gov1.example", "gov2.example"]
    assert manifest["tool_versions"]["python"]
    assert "provider" in manifest["models"]
    files = {f["path"]: f for f in manifest["files"]}
    trace = artifacts.trace_path(rid)
    assert files["trace.jsonl"]["sha256"] == artifacts.sha256_file(trace)
    run_doc = env["db"].poe_runs.docs[0]
    assert run_doc["manifest_sha256"] == artifacts.sha256_file(artifacts.manifest_path(rid))


def test_export_manifest_hashes_match_and_never_overwrite(env, monkeypatch):
    rid = _finished_run(env, monkeypatch)
    out = _run(unified_runs.export_run(env["db"], rid))
    base = Path(out["dir"])
    assert base == env["exports"] / rid
    manifest = json.loads((base / "manifest.json").read_text())
    paths = sorted(f["path"] for f in manifest["files"])
    assert paths == ["ports.geojson", "trace.jsonl", "zones.json"]
    for f in manifest["files"]:
        assert artifacts.sha256_file(base / f["path"]) == f["sha256"]
        assert (base / f["path"]).stat().st_size == f["bytes"]
    assert out["manifest_sha256"] == artifacts.sha256_file(base / "manifest.json")
    assert manifest["export"]["geoparquet"].startswith("not written")
    fc = json.loads((base / "ports.geojson").read_text())
    assert len(fc["features"]) == 2
    text = (base / "manifest.json").read_text().lower()
    for word in ("api_key", "secret", "password"):
        assert word not in text
    with pytest.raises(FileExistsError):
        _run(unified_runs.export_run(env["db"], rid))
    assert artifacts.sha256_file(base / "manifest.json") == out["manifest_sha256"]


def test_export_refuses_unfinished_run(env):
    env["db"].poe_runs.docs.append({"_id": "r-live", "state": "running"})
    with pytest.raises(ValueError):
        _run(unified_runs.export_run(env["db"], "r-live"))


# ── score vs C-level ─────────────────────────────────────────────────

def test_score_and_level_stay_independent(env, monkeypatch):
    def ports_for(mrgid):
        if mrgid == 1:
            return [{"name": "High score no level", "lat": 1.0, "lon": 1.0,
                     "confidence": 95},
                    {"name": "Low score C3", "lat": 1.1, "lon": 1.1,
                     "confidence": 10, "confidence_level": "C3"}]
        return [{"name": "Level only", "lat": 2.0, "lon": 2.0,
                 "confidence_level": "C1"}]

    rid = _finished_run(env, monkeypatch, run_id="r-lvl", ports_for=ports_for)
    out = _run(unified_runs.export_run(env["db"], rid))
    fc = json.loads((Path(out["dir"]) / "ports.geojson").read_text())
    got = {f["properties"]["name"]: (f["properties"]["confidence"],
                                     f["properties"]["confidence_level"])
           for f in fc["features"]}
    assert got == {"High score no level": (95, None),
                   "Low score C3": (10, "C3"),
                   "Level only": (None, "C1")}
    assert out["counts"]["with_score"] == 2 and out["counts"]["with_level"] == 2
    manifest = json.loads((Path(out["dir"]) / "manifest.json").read_text())
    assert manifest["fields"]["derived_from_each_other"] is False
    assert manifest["fields"]["certification_level"]["nullable"] is True
    # Stored ports are untouched by the export.
    stored = {d["name"]: d for d in env["db"].poe_run_ports.docs}
    assert "confidence_level" not in stored["High score no level"]
    assert "confidence" not in stored["Level only"]


# ── upload ───────────────────────────────────────────────────────────

class _StubClient:
    def __init__(self):
        self.calls = []

    def put_file(self, path, bucket, key):
        self.calls.append((Path(path).name, bucket, key))


def test_upload_disabled_by_default(env, monkeypatch):
    assert object_storage.StorageConfig.from_env().enabled is False
    rid = _finished_run(env, monkeypatch)
    stub = _StubClient()
    out = _run(unified_runs.export_run(env["db"], rid, storage_client=stub))
    assert out["upload"] == {"uploaded": False, "reason": "disabled"}
    assert stub.calls == []
    assert env["db"].poe_runs.docs[0]["exports"][0]["uploaded"] is False


def test_upload_enabled_uses_wrapper_manifest_last(env, monkeypatch):
    monkeypatch.setenv("BI_OBJECT_STORAGE_UPLOAD", "1")
    monkeypatch.setenv("BI_OBJECT_STORAGE_ENDPOINT", "https://storage.example.invalid")
    monkeypatch.setenv("BI_OBJECT_STORAGE_BUCKET", "bi-runs")
    rid = _finished_run(env, monkeypatch)
    stub = _StubClient()
    out = _run(unified_runs.export_run(env["db"], rid, storage_client=stub))
    assert out["upload"]["uploaded"] is True
    assert [c[0] for c in stub.calls][-1] == "manifest.json"
    assert all(c[1] == "bi-runs" and c[2].startswith(f"runs/{rid}/") for c in stub.calls)


def test_upload_enabled_without_bucket_does_nothing(tmp_path):
    cfg = object_storage.StorageConfig(enabled=True)
    stub = _StubClient()
    assert object_storage.upload_export(tmp_path, "r", config=cfg, client=stub)[
        "uploaded"] is False
    assert stub.calls == []


# ── legacy launch paths get the same artifacts ───────────────────────

def test_project_runs_open_finalize_write_trace_and_manifest(env):
    db = env["db"]

    async def go():
        opened = await project_runs.open_run(db, mode="test", label="p",
                                             run_id="r-proj", to_file=False)
        await project_runs.finalize_run(db, opened["run_id"])

    _run(go())
    kinds = [r["event"] for r in artifacts.read_trace("r-proj")]
    assert kinds == ["run_start", "run_done"]
    manifest = artifacts.read_manifest(artifacts.manifest_path("r-proj"))
    assert manifest["layer"] == "projects" and manifest["state"] == "done"
    assert db.project_runs.docs[0]["wrote_projects"] is False


def test_isolated_runs_open_finalize_write_trace_and_manifest(env):
    db = env["db"]

    async def go():
        opened = await isolated_runs.open_run(db, "marinas", kind="test",
                                              run_id="r-mar", to_file=False)
        isolated_runs.reset_run(opened["token"])
        await isolated_runs.finalize_run(db, "marinas", "r-mar")

    _run(go())
    kinds = [r["event"] for r in artifacts.read_trace("r-mar")]
    assert kinds == ["run_start", "run_done"]
    manifest = artifacts.read_manifest(artifacts.manifest_path("r-mar"))
    assert manifest["layer"] == "marinas" and manifest["counts"]["items"] == 0
    assert db.marinas.docs == []


def test_artifact_failure_does_not_break_a_run(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(artifacts, "append_trace", boom)
    rid = _finished_run(env, monkeypatch, run_id="r-disk")
    assert env["db"].poe_runs.docs[0]["state"] == "done"
    assert rid == "r-disk"


def test_run_id_is_path_safe():
    for bad in ("../x", "a/b", "", ".."):
        with pytest.raises(ValueError):
            artifacts.run_dir(bad)
