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


def test_trace_alone_does_not_mark_zone_done(env, monkeypatch):
    """Mongo is the source of truth: a unit_done line without its poe_run_zones
    doc (restore, other DB, manual cleanup) is flagged and the zone is re-run."""
    db = env["db"]
    db.poe_runs.docs.append({"_id": "r-trace", "state": "running", "label": "x",
                             "params": {"limit": 0, "only_zones": None,
                                        "concurrency": 1, "variant": "tinyfish"}})
    db.poe_run_zones.docs.append({"run_id": "r-trace", "mrgid": 3, "status": "ia"})
    artifacts.append_trace("r-trace", "unit_done", unit=2)
    artifacts.append_trace("r-trace", "unit_done", unit=3)
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe", _zone_stub(calls, attempt=1))
    _run(unified_runs.start_run(db, TaskState(), run_id="r-trace", resume=True))
    assert sorted(calls) == [1, 2, 4]
    flagged = [r for r in artifacts.read_trace("r-trace")
               if r["event"] == "unit_missing_in_mongo"]
    assert [r["unit"] for r in flagged] == [2]
    assert {d["mrgid"] for d in db.poe_run_zones.docs} == {1, 2, 3, 4}


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


# ── retry of errored zones on resume ─────────────────────────────────

def _failing_stub(calls: list, *, attempt: int, fail: dict):
    """fail: mrgid -> "raise" | "raise_after_ports" | "status_erreur"."""
    async def fake_zone(db, mrgid, logger=None, force=False, run=None):
        calls.append(mrgid)
        mode = fail.get(mrgid)
        if mode == "raise":
            raise RuntimeError(f"zone {mrgid} timed out")
        ports = [] if mode == "status_erreur" else [
            {"name": f"Port {mrgid}", "lat": 10.0 + mrgid, "lon": -60.0}]
        await db.poe_run_ports.delete_many({"run_id": run.run_id, "mrgid": mrgid})
        await db.poe_run_ports.insert_many([
            {"_id": f"{run.run_id}:{mrgid}:{i}:a{attempt}", "run_id": run.run_id,
             "mrgid": mrgid, "attempt": attempt, **p} for i, p in enumerate(ports)])
        if mode == "raise_after_ports":
            raise RuntimeError(f"zone {mrgid} failed after persisting")
        status = "erreur" if mode == "status_erreur" else "ia"
        await db.poe_run_zones.update_one(
            {"run_id": run.run_id, "mrgid": mrgid},
            {"$set": {"run_id": run.run_id, "mrgid": mrgid, "status": status,
                      "poe_count": len(ports), "attempt": attempt}}, upsert=True)
        return {"status": status, "poe_count": len(ports)}

    return fake_zone


def _run_with_errors(env, monkeypatch, run_id="r-err"):
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe", _failing_stub(
        calls, attempt=1,
        fail={2: "raise", 3: "raise_after_ports", 4: "status_erreur"}))
    _run(unified_runs.start_run(env["db"], TaskState(), run_id=run_id, concurrency=1))
    assert calls == [1, 2, 3, 4]
    assert artifacts.errored_units(run_id) == {2, 3, 4}
    return run_id


def _snapshot(db, run_id):
    zones = {d["mrgid"]: dict(d) for d in db.poe_run_zones.docs if d["run_id"] == run_id}
    ports = {d["_id"]: dict(d) for d in db.poe_run_ports.docs if d["run_id"] == run_id}
    return zones, ports


def test_resume_without_retry_keeps_errored_zones_as_done(env, monkeypatch):
    db = env["db"]
    rid = _run_with_errors(env, monkeypatch)
    zones_before, ports_before = _snapshot(db, rid)
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _failing_stub(calls, attempt=2, fail={}))
    _run(unified_runs.start_run(db, TaskState(), run_id=rid, resume=True))
    assert calls == []
    assert _snapshot(db, rid) == (zones_before, ports_before)


def test_resume_with_retry_redoes_errored_zones_once(env, monkeypatch):
    db = env["db"]
    rid = _run_with_errors(env, monkeypatch)
    zones_before, ports_before = _snapshot(db, rid)
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _failing_stub(calls, attempt=2, fail={}))
    summary = _run(unified_runs.start_run(db, TaskState(), run_id=rid, resume=True,
                                          retry_errored=True))
    # 2 (raised, no ports) and 4 (status erreur, no ports) are redone once;
    # 3 has ports stored for this run, so it is left alone; 1 is completed.
    assert sorted(calls) == [2, 4]
    zones_after, ports_after = _snapshot(db, rid)
    assert zones_after[1] == zones_before[1]
    assert zones_after[3] == zones_before[3]
    assert zones_after[2]["status"] == "ia" and zones_after[2]["attempt"] == 2
    for pid, doc in ports_before.items():
        assert ports_after[pid] == doc
    assert summary["zones_done"] == 4 and summary["errors"] == 0
    trace = artifacts.read_trace(rid)
    retried = [r["unit"] for r in trace if r["event"] == "unit_start" and r.get("retry")]
    assert sorted(retried) == [2, 4]
    assert [r["unit"] for r in trace if r["event"] == "unit_retry_skipped"] == [3]
    assert artifacts.errored_units(rid) == {3}

    # A second retry never touches the zones that are now completed.
    calls2: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _failing_stub(calls2, attempt=3, fail={}))
    _run(unified_runs.start_run(db, TaskState(), run_id=rid, resume=True,
                                retry_errored=True))
    assert calls2 == []


def test_retry_that_fails_again_is_attempted_once(env, monkeypatch):
    db = env["db"]
    rid = _run_with_errors(env, monkeypatch)
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _failing_stub(calls, attempt=2, fail={2: "raise"}))
    summary = _run(unified_runs.start_run(db, TaskState(), run_id=rid, resume=True,
                                          retry_errored=True))
    assert sorted(calls) == [2, 4]
    assert summary["errors"] == 1
    assert artifacts.errored_units(rid) == {2, 3}


def test_retry_falls_back_to_zone_status_without_trace(env, monkeypatch):
    """Runs started before the trace existed: the poe_run_zones status is used."""
    db = env["db"]
    db.poe_runs.docs.append({"_id": "r-old-err", "state": "done", "label": "x",
                             "params": {"limit": 0, "only_zones": [1, 2],
                                        "concurrency": 1, "variant": "tinyfish"}})
    db.poe_run_zones.docs += [
        {"run_id": "r-old-err", "mrgid": 1, "status": "ia"},
        {"run_id": "r-old-err", "mrgid": 2, "status": "erreur"}]
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _failing_stub(calls, attempt=1, fail={}))
    _run(unified_runs.start_run(db, TaskState(), run_id="r-old-err", resume=True,
                                retry_errored=True))
    assert calls == [2]


def test_retry_errored_requires_resume(env):
    with pytest.raises(ValueError):
        _run(unified_runs.start_run(env["db"], TaskState(), run_id="r-new",
                                    retry_errored=True))
    assert env["db"].poe_runs.docs == []


# ── CLI: backend/run.py --resume / --export ──────────────────────────

def _cli():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "run.py"
    spec = importlib.util.spec_from_file_location("bi_run_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cli(env, monkeypatch):
    mod = _cli()
    monkeypatch.setattr(mod, "_default_db", lambda: env["db"])
    monkeypatch.setenv("SAMPLE_MODE", "true")  # restored after the test
    return mod


def test_cli_resume_with_and_without_retry(env, monkeypatch, cli, capsys):
    rid = _run_with_errors(env, monkeypatch, run_id="r-cli")
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _failing_stub(calls, attempt=2, fail={}))
    assert cli.main(["--no-dotenv", "--resume", rid]) == 0
    assert calls == []
    out = json.loads(capsys.readouterr().out)
    assert out["run_id"] == rid and out["zones_done"] == 4

    assert cli.main(["--no-dotenv", "--resume", rid, "--retry-errored"]) == 0
    assert sorted(calls) == [2, 4]


def test_cli_resume_unknown_run_exits_2(env, cli, capsys):
    assert cli.main(["--no-dotenv", "--resume", "r-missing"]) == 2
    assert capsys.readouterr().out == ""


def test_cli_export_then_refuses_overwrite(env, monkeypatch, cli, capsys):
    rid = _finished_run(env, monkeypatch, run_id="r-cli-exp")
    assert cli.main(["--no-dotenv", "--export", rid]) == 0
    out = json.loads(capsys.readouterr().out)
    assert Path(out["dir"]) == env["exports"] / rid
    assert out["upload"] == {"uploaded": False, "reason": "disabled"}
    assert cli.main(["--no-dotenv", "--export", rid]) == 2


def test_cli_functions_called_directly_with_stub_db(env, monkeypatch):
    mod = _cli()  # _default_db is never reached when a db is passed
    monkeypatch.setattr(mod, "_default_db", lambda: pytest.fail("default db used"))
    rid = _finished_run(env, monkeypatch, run_id="r-cli-fn")
    summary = _run(mod.resume_run(rid, db=env["db"]))
    assert summary["run_id"] == rid
    out = _run(mod.export_run(rid, db=env["db"]))
    assert Path(out["dir"]).is_dir()


def test_cli_argument_rules(cli):
    with pytest.raises(SystemExit):
        cli.main(["--no-dotenv", "--zone", "Bermuda", "--retry-errored"])
    with pytest.raises(SystemExit):
        cli.main(["--no-dotenv", "--resume", "a", "--export", "a"])


# ── crash robustness: atomic manifest, fsynced trace, torn last line ─

def _torn_trace(rid: str, tail: bytes) -> Path:
    artifacts.append_trace(rid, "run_start", layer="poe")
    artifacts.append_trace(rid, "unit_done", unit=1, status="ia")
    path = artifacts.trace_path(rid)
    with path.open("ab") as f:
        f.write(tail)  # crash in the middle of the next line
    return path


def test_truncated_final_trace_line_is_ignored_with_warning(env, caplog):
    _torn_trace("r-torn", b'{"ts": "2026-10-30", "run_id": "r-torn", "event": "unit_do')
    with caplog.at_level("WARNING", logger=artifacts.__name__):
        recs = artifacts.read_trace("r-torn")
    assert [r["event"] for r in recs] == ["run_start", "unit_done"]
    assert artifacts.completed_units("r-torn") == {1}
    assert any("truncated final line" in r.getMessage() for r in caplog.records)


def test_torn_utf8_sequence_does_not_crash(env, caplog):
    tail = '{"event": "unit_done", "zone": "Curaçao'.encode("utf-8")
    _torn_trace("r-utf8", tail[:-len("ao") - 1])  # cut inside the 2-byte "ç"
    with caplog.at_level("WARNING", logger=artifacts.__name__):
        assert len(artifacts.read_trace("r-utf8")) == 2
    assert any("truncated final line" in r.getMessage() for r in caplog.records)


def test_append_after_torn_line_starts_a_new_line(env):
    _torn_trace("r-next", b'{"event": "unit_st')
    artifacts.append_trace("r-next", "run_resume", layer="poe")
    recs = artifacts.read_trace("r-next")
    assert [r["event"] for r in recs] == ["run_start", "unit_done", "run_resume"]


def test_resume_with_truncated_trace(env, monkeypatch):
    db = env["db"]
    db.poe_runs.docs.append({"_id": "r-crash2", "state": "running", "label": "x",
                             "params": {"limit": 0, "only_zones": None,
                                        "concurrency": 1, "variant": "tinyfish"}})
    # Zone 1 was persisted, then traced; the crash cut zone 2's unit_done line.
    db.poe_run_zones.docs.append({"run_id": "r-crash2", "mrgid": 1, "status": "ia"})
    _torn_trace("r-crash2", b'{"ts": "x", "event": "unit_done", "unit": 2')
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe", _zone_stub(calls, attempt=1))
    summary = _run(unified_runs.start_run(db, TaskState(), run_id="r-crash2", resume=True))
    # zone 1 is done; the torn unit_done for zone 2 does not count.
    assert sorted(calls) == [2, 3, 4]
    assert summary["zones_done"] == 4
    events_ = [r["event"] for r in artifacts.read_trace("r-crash2")]
    assert events_[-1] == "run_done"


def test_trace_lines_are_flushed_and_fsynced(env, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(artifacts.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    artifacts.append_trace("r-sync", "run_start")
    artifacts.append_trace("r-sync", "unit_start", unit=1)
    assert len(synced) == 2


def test_manifest_write_is_atomic(env, monkeypatch):
    path = artifacts.manifest_path("r-atomic")
    first = artifacts.write_json_atomic(path, {"run_id": "r-atomic", "v": 1})
    assert artifacts.sha256_file(path) == first

    def crash(src, dst):
        raise OSError("killed during rename")

    monkeypatch.setattr(artifacts.os, "replace", crash)
    with pytest.raises(OSError):
        artifacts.write_json_atomic(path, {"run_id": "r-atomic", "v": 2})
    # The previous manifest is intact and no temp file is left behind.
    assert json.loads(path.read_text())["v"] == 1
    assert sorted(p.name for p in path.parent.iterdir()) == ["manifest.json"]
    monkeypatch.undo()

    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(artifacts.os, "fsync", lambda fd: (order.append("fsync"),
                                                           real_fsync(fd)))
    monkeypatch.setattr(artifacts.os, "replace", lambda a, b: (order.append("replace"),
                                                               real_replace(a, b)))
    artifacts.write_json_atomic(path, {"run_id": "r-atomic", "v": 3})
    assert order[:2] == ["fsync", "replace"]
    assert json.loads(path.read_text())["v"] == 3
    assert oct(path.stat().st_mode & 0o777) == oct(0o644)


# ── HTTP launch routes: one execution per run_id ─────────────────────

@pytest.fixture
def api(env, monkeypatch):
    """The runs router on a bare FastAPI app, wired to the fake db.

    resolve_launch keeps its real body (one awaited Mongo lookup);
    start_run is stubbed so only launches are counted.
    """
    from fastapi import FastAPI
    from app.routers import runs as runs_router

    monkeypatch.setattr(runs_router, "_db", env["db"])
    monkeypatch.setattr(runs_router, "RUN_STATES", {})
    started: list = []
    release = asyncio.Event()

    async def fake_start(db, state, *, resolved=None, **kw):
        started.append(resolved["run_id"])
        await release.wait()
        return {"run_id": resolved["run_id"]}

    monkeypatch.setattr(runs_router.unified_runs, "start_run", fake_start)
    plain_find_one = _Coll.find_one

    async def yielding_find_one(self, q, proj=None):
        await asyncio.sleep(0)  # a real Mongo lookup suspends the request
        return await plain_find_one(self, q, proj)

    monkeypatch.setattr(_Coll, "find_one", yielding_find_one)
    app = FastAPI()
    app.include_router(runs_router.router)
    return {"app": app, "router": runs_router, "started": started, "release": release}


def _post_concurrently(app, requests):
    import httpx

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            res = await asyncio.gather(*(c.post(url, json=body) for url, body in requests))
            await asyncio.sleep(0.05)  # let the launched runner tasks start
            return res
    return _run(go())


def test_concurrent_resume_starts_the_run_once(env, api):
    env["db"].poe_runs.docs.append({"_id": "r1", "state": "running", "label": "x",
                                    "params": {"limit": 0, "only_zones": None,
                                               "concurrency": 1, "variant": "tinyfish"}})
    res = _post_concurrently(api["app"], [
        ("/api/poe/runs", {"resume_run_id": "r1"}),
        ("/api/runs", {"run_id": "r1", "resume": True})])
    assert sorted(r.status_code for r in res) == [202, 409]
    assert api["started"] == ["r1"]


def test_concurrent_launches_respect_parallel_cap(env, api):
    cap = api["router"].MAX_PARALLEL_RUNS
    res = _post_concurrently(api["app"], [
        ("/api/runs", {"run_id": f"r-cap-{i}"}) for i in range(cap + 2)])
    assert sorted(r.status_code for r in res) == [202] * cap + [409] * 2
    assert len(api["started"]) == cap


def test_failed_lookup_releases_the_reserved_run(env, api):
    res = _post_concurrently(api["app"], [("/api/runs", {"run_id": "r-x", "resume": True})])
    assert res[0].status_code == 400  # unknown run
    assert "r-x" not in api["router"].RUN_STATES
    env["db"].poe_runs.docs.append({"_id": "r-x", "state": "done", "params": {}})
    res = _post_concurrently(api["app"], [("/api/runs", {"run_id": "r-x", "resume": True})])
    assert res[0].status_code == 202 and api["started"] == ["r-x"]


# ── resume never changes the run's mode (live vs sample) ─────────────

_LIVE_VARS = ("ZETARIS_API_KEY", "ZETARIS_ORG_ID", "METERLESS_RUNTIME_TOKEN",
              "MODEL_API_KEY")


def _live_env(monkeypatch, *, creds: bool):
    """No SAMPLE_MODE; dummy (never used) credentials or none. Restored after."""
    monkeypatch.delenv("SAMPLE_MODE", raising=False)
    for name in _LIVE_VARS:
        if creds:
            monkeypatch.setenv(name, "dummy-not-a-key")
        else:
            monkeypatch.delenv(name, raising=False)


def _mode_cli(env, monkeypatch):
    mod = _cli()
    monkeypatch.setattr(mod, "_default_db", lambda: env["db"])
    return mod


def test_cli_resume_refuses_live_run_without_credentials(env, monkeypatch, capsys):
    _live_env(monkeypatch, creds=True)
    rid = _run_with_errors(env, monkeypatch, run_id="r-live-mode")
    assert env["db"].poe_runs.docs[0]["params"]["code"]["sample_mode"] is False
    zones_before, ports_before = _snapshot(env["db"], rid)
    _live_env(monkeypatch, creds=False)
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _failing_stub(calls, attempt=2, fail={}))
    cli = _mode_cli(env, monkeypatch)
    assert cli.main(["--no-dotenv", "--resume", rid, "--retry-errored"]) == 3
    assert calls == [] and capsys.readouterr().out == ""
    assert os.environ.get("SAMPLE_MODE") is None  # not switched on behind our back
    assert _snapshot(env["db"], rid) == (zones_before, ports_before)


def test_cli_resume_refuses_mode_switch_either_way(env, monkeypatch, capsys):
    _live_env(monkeypatch, creds=True)
    live = _run_with_errors(env, monkeypatch, run_id="r-live-2")
    cli = _mode_cli(env, monkeypatch)
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _failing_stub(calls, attempt=2, fail={}))
    assert cli.main(["--no-dotenv", "--sample-mode", "--resume", live]) == 3

    _live_env(monkeypatch, creds=False)
    monkeypatch.setenv("SAMPLE_MODE", "true")
    sample = _run_with_errors(env, monkeypatch, run_id="r-sample-2")
    _live_env(monkeypatch, creds=True)
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _failing_stub(calls, attempt=2, fail={}))
    assert cli.main(["--no-dotenv", "--resume", sample, "--retry-errored"]) == 3
    assert calls == []


def test_cli_resume_in_recorded_mode_works(env, monkeypatch, capsys):
    _live_env(monkeypatch, creds=True)
    rid = _run_with_errors(env, monkeypatch, run_id="r-live-ok")
    calls: list = []
    monkeypatch.setattr(poe_runs.poe, "generate_zone_poe",
                        _failing_stub(calls, attempt=2, fail={}))
    cli = _mode_cli(env, monkeypatch)
    assert cli.main(["--no-dotenv", "--resume", rid, "--retry-errored"]) == 0
    assert sorted(calls) == [2, 4]


def test_api_resume_refuses_recorded_mode_mismatch(env, monkeypatch):
    monkeypatch.setenv("SAMPLE_MODE", "true")
    rid = _run_with_errors(env, monkeypatch, run_id="r-api-mode")
    monkeypatch.delenv("SAMPLE_MODE")
    with pytest.raises(unified_runs.ModeMismatch):
        _run(unified_runs.resolve_launch(env["db"], run_id=rid, resume=True))


# ── upload failure after export: retry the upload, never re-export ───

class _FailingClient(_StubClient):
    def put_file(self, path, bucket, key):
        super().put_file(path, bucket, key)
        raise OSError("connection reset")


def _enable_upload(monkeypatch):
    monkeypatch.setenv("BI_OBJECT_STORAGE_UPLOAD", "1")
    monkeypatch.setenv("BI_OBJECT_STORAGE_ENDPOINT", "https://storage.example.invalid")
    monkeypatch.setenv("BI_OBJECT_STORAGE_BUCKET", "bi-runs")


def _export_with_failed_upload(env, monkeypatch, rid="r-up"):
    _enable_upload(monkeypatch)
    _finished_run(env, monkeypatch, run_id=rid)
    out = _run(unified_runs.export_run(env["db"], rid, storage_client=_FailingClient()))
    return rid, out


def _hashes(base: Path) -> dict:
    return {p.name: artifacts.sha256_file(p) for p in sorted(base.iterdir())}


def test_upload_failure_keeps_the_export_and_is_recorded(env, monkeypatch):
    rid, out = _export_with_failed_upload(env, monkeypatch)
    assert out["upload"]["uploaded"] is False
    assert "connection reset" in out["upload"]["error"]
    entry = env["db"].poe_runs.docs[0]["exports"][0]
    assert entry["uploaded"] is False and "connection reset" in entry["upload_error"]
    assert (env["exports"] / rid / "manifest.json").is_file()


def test_upload_existing_export_without_reexport(env, monkeypatch):
    rid, _ = _export_with_failed_upload(env, monkeypatch)
    base = env["exports"] / rid
    before = _hashes(base)
    stub = _StubClient()
    up = _run(unified_runs.upload_existing_export(env["db"], rid, storage_client=stub))
    assert up["uploaded"] is True
    assert [c[0] for c in stub.calls] == ["ports.geojson", "trace.jsonl", "zones.json",
                                          "manifest.json"]
    assert _hashes(base) == before  # nothing rewritten
    run = env["db"].poe_runs.docs[0]
    assert len(run["exports"]) == 1
    assert run["uploads"][-1]["uploaded"] is True
    assert run["uploads"][-1]["manifest_sha256"] == before["manifest.json"]


def test_upload_existing_export_refuses_tampered_or_incomplete(env, monkeypatch):
    rid, _ = _export_with_failed_upload(env, monkeypatch)
    base = env["exports"] / rid
    (base / "zones.json").write_text("[]\n")
    stub = _StubClient()
    with pytest.raises(unified_runs.ExportIncomplete):
        _run(unified_runs.upload_existing_export(env["db"], rid, storage_client=stub))
    (base / "manifest.json").unlink()
    with pytest.raises(unified_runs.ExportIncomplete):
        _run(unified_runs.upload_existing_export(env["db"], rid, storage_client=stub))
    with pytest.raises(unified_runs.ExportIncomplete):
        _run(unified_runs.upload_existing_export(env["db"], "r-none", storage_client=stub))
    assert stub.calls == []


def _bare_runs_app(env, monkeypatch):
    from fastapi import FastAPI
    from app.routers import runs as runs_router

    monkeypatch.setattr(runs_router, "_db", env["db"])
    app = FastAPI()
    app.include_router(runs_router.router)
    return app


def test_upload_route_after_failed_upload(env, monkeypatch):
    rid, _ = _export_with_failed_upload(env, monkeypatch, rid="r-up-route")
    stub = _StubClient()
    monkeypatch.setattr(object_storage, "S3Client", lambda cfg: stub)
    app = _bare_runs_app(env, monkeypatch)
    res = _post_concurrently(app, [(f"/api/runs/{rid}/export", {})])
    assert res[0].status_code == 409  # the export itself is never redone
    res = _post_concurrently(app, [(f"/api/runs/{rid}/upload", {})])
    assert res[0].status_code == 200 and res[0].json()["uploaded"] is True
    assert stub.calls[-1][0] == "manifest.json"


def test_cli_upload_existing_export(env, monkeypatch, cli, capsys):
    rid, _ = _export_with_failed_upload(env, monkeypatch, rid="r-up-cli")
    stub = _StubClient()
    monkeypatch.setattr(object_storage, "S3Client", lambda cfg: stub)
    assert cli.main(["--no-dotenv", "--upload", rid]) == 0
    assert json.loads(capsys.readouterr().out)["uploaded"] is True
    assert stub.calls[-1][0] == "manifest.json"
    (env["exports"] / rid / "ports.geojson").write_text("{}\n")
    assert cli.main(["--no-dotenv", "--upload", rid]) == 2


# ── export and upload never block the event loop ─────────────────────

class _ThreadClient(_StubClient):
    def __init__(self):
        super().__init__()
        self.threads = set()

    def put_file(self, path, bucket, key):
        import threading
        self.threads.add(threading.get_ident())
        super().put_file(path, bucket, key)


def test_export_and_upload_io_runs_off_the_event_loop(env, monkeypatch):
    import threading
    _enable_upload(monkeypatch)
    rid = _finished_run(env, monkeypatch, run_id="r-thread")
    seen = {"dump": set(), "sha": set()}
    real_dump, real_sha = unified_runs._dump, artifacts.sha256_file

    def dump(path, doc):
        seen["dump"].add(threading.get_ident())
        return real_dump(path, doc)

    def sha(path):
        seen["sha"].add(threading.get_ident())
        return real_sha(path)

    monkeypatch.setattr(unified_runs, "_dump", dump)
    monkeypatch.setattr(artifacts, "sha256_file", sha)

    async def go(fn, **kw):
        loop_thread = threading.get_ident()
        client = _ThreadClient()
        await fn(env["db"], rid, storage_client=client, **kw)
        return loop_thread, client

    loop_thread, client = _run(go(unified_runs.export_run))
    assert client.calls and loop_thread not in client.threads
    assert seen["dump"] and loop_thread not in seen["dump"]
    assert seen["sha"] and loop_thread not in seen["sha"]
    seen["sha"].clear()
    loop_thread, client = _run(go(unified_runs.upload_existing_export))
    assert client.calls and loop_thread not in client.threads
    assert seen["sha"] and loop_thread not in seen["sha"]
