"""unified_runs — The single way to launch, resume and export a run (P0-4).

V2 Mongo runs stay the source of truth (`poe_runs`, `poe_run_zones`,
`poe_run_ports`, `poe_run_events`). On top of them every run keeps
data/runs/<run_id>/trace.jsonl (append-only) and manifest.json
(run_artifacts). Resume skips every zone already completed, so nothing
already stored is rewritten.

Only the PoE layer is driven per zone today. Projects, marinas,
harbour offices and MPAs still start from their own endpoints
(LEGACY_ENDPOINTS); folding them in is part of P0-5.

Export: GeoJSON + zones + trace + manifest (SHA-256 of each file) into
data/exports/<run_id>/, never overwritten. GeoParquet is not written:
neither pyarrow nor geopandas is a backend dependency. Upload to Nebius
Object Storage only when BI_OBJECT_STORAGE_UPLOAD is set (object_storage).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from app.config import DATA_DIR
from app.services import object_storage
from app.services import poe_runs
from app.services import run_artifacts as artifacts
from app.services.run_fingerprint import sample_mode_enabled

POE_LAYERS = ("poe", "formalities")
LEGACY_ENDPOINTS = {
    "projects": "POST /api/projects/runs",
    "marinas": "POST /api/marinas/build",
    "capitaineries": "POST /api/capitaineries/build",
    "amp": "POST /api/amp/discover-visit-urls",
}
EXPORT_FILES = ("ports.geojson", "zones.json", artifacts.TRACE_NAME)
GEOPARQUET_NOTE = "not written: pyarrow / geopandas are not backend dependencies"
# Run ids with an export in progress in this process. Checked and set with
# no await in between, so a second concurrent export of the same run is
# refused (409) instead of deleting the first one's .partial folder.
_EXPORTING: set[str] = set()


class RunExists(ValueError):
    pass


class UnsupportedLayer(ValueError):
    pass


class ExportIncomplete(ValueError):
    """No export folder, or one whose files do not match its manifest."""


class ModeMismatch(ValueError):
    """The run cannot be resumed in the mode (live / sample) it was started in."""


def recorded_sample_mode(run: dict | None) -> bool | None:
    """params.code.sample_mode of a run; None for runs started before it was recorded."""
    code = ((run or {}).get("params") or {}).get("code")
    value = code.get("sample_mode") if isinstance(code, dict) else None
    return value if isinstance(value, bool) else None


def check_resume_mode(run: dict, *, unknown_is_live: bool = False) -> None:
    """Refuse a resume that would run in another mode than the one recorded.

    A resumed zone is written into the same run as the zones already done:
    sample fixtures must never be mixed into a live run, or the reverse.
    unknown_is_live: treat runs without a recorded mode as live (they were
    started through the API before the field existed).
    """
    recorded = recorded_sample_mode(run)
    if recorded is None:
        if not unknown_is_live:
            return
        recorded = False
    current = sample_mode_enabled()
    if recorded != current:
        want, got = ("sample", "live") if recorded else ("live", "sample")
        raise ModeMismatch(
            f"run {run.get('_id')} was started in {want} mode; resuming it in "
            f"{got} mode would mix {got} results into it — refused")


def normalize_layer(layer: str) -> str:
    key = (layer or "poe").strip().lower()
    if key in POE_LAYERS:
        return "poe"
    if key in LEGACY_ENDPOINTS:
        raise UnsupportedLayer(
            f"layer {key} is not driven by the unified runner yet — "
            f"use {LEGACY_ENDPOINTS[key]} (it writes trace + manifest too)")
    raise UnsupportedLayer(f"unknown layer: {key}")


def export_root() -> Path:
    raw = (os.environ.get("BI_EXPORT_DIR") or "").strip()
    return Path(raw) if raw else DATA_DIR / "exports"


async def resolve_launch(db, *, layer: str = "poe", run_id: str | None = None,
                         resume: bool = False, label: str = "", limit: int = 0,
                         zones: list[int] | None = None, concurrency: int = 2,
                         variant: str = "tinyfish",
                         retry_errored: bool = False) -> dict:
    """Check the request and return the execute_run arguments.

    A new run never reuses an existing run_id. A resume reuses the stored
    parameters so the zone list cannot drift between attempts.
    retry_errored only applies to a resume.
    """
    normalize_layer(layer)
    if retry_errored and not resume:
        raise ValueError("retry_errored requires resume=True")
    if resume:
        if not run_id:
            raise ValueError("run_id required to resume")
        artifacts.safe_run_id(run_id)
        existing = await db.poe_runs.find_one({"_id": run_id})
        if not existing:
            raise ValueError(f"run {run_id} unknown — cannot resume")
        check_resume_mode(existing)
        params = existing.get("params") or {}
        return {
            "run_id": run_id, "resume": True,
            "label": label or existing.get("label") or "",
            "limit": int(params.get("limit") or 0),
            "only_zones": params.get("only_zones"),
            "concurrency": int(params.get("concurrency") or concurrency),
            "variant": params.get("variant") or variant,
            "retry_errored": bool(retry_errored),
        }
    rid = run_id or poe_runs.new_run_id()
    artifacts.safe_run_id(rid)
    if await db.poe_runs.find_one({"_id": rid}):
        raise RunExists(f"run {rid} already exists — pass resume=True")
    return {"run_id": rid, "resume": False, "label": label, "limit": limit,
            "only_zones": zones, "concurrency": concurrency, "variant": variant,
            "retry_errored": False}


async def start_run(db, state, *, layer: str = "poe", run_id: str | None = None,
                    resume: bool = False, label: str = "", limit: int = 0,
                    zones: list[int] | None = None, concurrency: int = 2,
                    variant: str = "tinyfish", rules_overrides: dict | None = None,
                    profile: str | None = None, resolved: dict | None = None,
                    retry_errored: bool = False) -> dict:
    """Launch (or resume) a run. Writes the Mongo run doc, trace and manifest.

    retry_errored=True (resume only): zones whose last trace status is an
    error are redone once. Completed zones are never re-run and zones that
    already have stored ports are left alone.
    """
    args = resolved or await resolve_launch(
        db, layer=layer, run_id=run_id, resume=resume, label=label, limit=limit,
        zones=zones, concurrency=concurrency, variant=variant,
        retry_errored=retry_errored)
    return await poe_runs.execute_run(
        db, state, args["run_id"], label=args["label"], limit=args["limit"],
        only_zones=args["only_zones"], concurrency=args["concurrency"],
        resume=args["resume"], variant=args["variant"],
        rules_overrides=None if args["resume"] else rules_overrides,
        profile=None if args["resume"] else profile,
        retry_errored=bool(args.get("retry_errored")))


def _feature_collection(run_id: str, ports: list[dict]) -> dict:
    """Map GeoJSON of the run. Score and C-level are copied as stored."""
    from app.services.poe_pipeline import ports_to_geojson

    fc = ports_to_geojson(ports)
    by_id = {str(p.get("_id")): p for p in ports}
    for feat in fc["features"]:
        props = feat["properties"]
        src = by_id.get(str(props.get("id"))) or {}
        props["run_id"] = run_id
        props[artifacts.SCORE_FIELD] = src.get(artifacts.SCORE_FIELD)
        props[artifacts.LEVEL_FIELD] = src.get(artifacts.LEVEL_FIELD)
    fc["run_id"] = run_id
    return fc


def _dump(path: Path, doc) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str) + "\n",
                    encoding="utf-8")


async def export_run(db, run_id: str, *, out_root: Path | None = None,
                     storage_config: object_storage.StorageConfig | None = None,
                     storage_client=None) -> dict:
    """Export a finished run. An existing export folder is never overwritten."""
    rid = artifacts.safe_run_id(run_id)
    run = await db.poe_runs.find_one({"_id": rid})
    if not run:
        raise ValueError(f"run {rid} unknown")
    if run.get("state") not in ("done", "cancelled"):
        raise ValueError(f"run {rid} is {run.get('state')!r}, not finished")
    if rid in _EXPORTING:
        raise FileExistsError(f"export of run {rid} already in progress")
    _EXPORTING.add(rid)
    try:
        return await _export_locked(db, run, rid, out_root, storage_config, storage_client)
    finally:
        _EXPORTING.discard(rid)


async def _export_locked(db, run: dict, rid: str, out_root, storage_config,
                         storage_client) -> dict:
    root = Path(out_root) if out_root else export_root()
    out = root / rid
    if out.exists():
        raise FileExistsError(f"export {out} already exists")

    ports = await db.poe_run_ports.find({"run_id": rid}).to_list(None)
    zones = await db.poe_run_zones.find({"run_id": rid}).to_list(None)
    # File writes, hashing and the upload are blocking: they run in a worker
    # thread so live runs on the event loop are not stalled.
    manifest, manifest_sha, counts, upload = await asyncio.to_thread(
        _write_export, run, rid, root, ports, zones, storage_config, storage_client)
    rel = artifacts.data_relative(out, root)
    entry = {"dir": rel, "manifest_sha256": manifest_sha,
             "written_at": manifest["written_at"],
             "uploaded": bool(upload.get("uploaded"))}
    if upload.get("error"):
        entry["upload_error"] = upload["error"]
    await db.poe_runs.update_one({"_id": rid}, {"$push": {"exports": entry}})
    return {"run_id": rid, "dir": rel, "manifest_sha256": manifest_sha,
            "files": manifest["files"], "counts": counts, "upload": upload}


def _write_export(run: dict, rid: str, root: Path, ports: list, zones: list,
                  storage_config, storage_client):
    """Write the export folder (via a .partial dir), then upload it. Blocking."""
    out = root / rid
    partial = root / f".{rid}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    fc = _feature_collection(rid, ports)
    _dump(partial / "ports.geojson", fc)
    _dump(partial / "zones.json", zones)
    trace = artifacts.trace_path(rid)
    if trace.is_file():
        shutil.copyfile(trace, partial / artifacts.TRACE_NAME)

    by_status: dict[str, int] = {}
    urls = []
    for z in zones:
        s = z.get("status") or "erreur"
        by_status[s] = by_status.get(s, 0) + 1
        for src in z.get("sources") or []:
            urls.append(src.get("url") if isinstance(src, dict) else src)
    counts = {
        "zones": len(zones),
        "by_status": by_status,
        "ports_total": len(ports),
        "ports_located": len(fc["features"]),
        "ports_unlocated": len(ports) - len(fc["features"]),
        "with_score": sum(1 for p in ports if p.get(artifacts.SCORE_FIELD) is not None),
        "with_level": sum(1 for p in ports if p.get(artifacts.LEVEL_FIELD) is not None),
    }
    manifest = artifacts.build_manifest(
        run_id=rid, layer="poe", state=run.get("state"),
        params=run.get("params"), counts=counts,
        files=artifacts.file_entries(partial, list(EXPORT_FILES)),
        sources=artifacts.sources_summary(urls),
        cost=(run.get("summary") or {}).get("tokenfactory") or {},
        extra={"export": {"formats": ["geojson"], "geoparquet": GEOPARQUET_NOTE,
                          "run_manifest_sha256": run.get("manifest_sha256")}})
    artifacts.write_json_atomic(partial / artifacts.MANIFEST_NAME, manifest)
    if out.exists():  # another process exported meanwhile: keep its folder
        shutil.rmtree(partial, ignore_errors=True)
        raise FileExistsError(f"export {out} already exists")
    os.replace(partial, out)
    manifest_sha = artifacts.sha256_file(out / artifacts.MANIFEST_NAME)
    upload = _safe_upload(out, rid, storage_config, storage_client)
    return manifest, manifest_sha, counts, upload


def _safe_upload(out: Path, rid: str, storage_config, storage_client) -> dict:
    """An upload failure is reported, never raised: the export stays usable
    and upload_existing_export can retry it."""
    try:
        return object_storage.upload_export(
            out, rid, config=storage_config, client=storage_client)
    except Exception as exc:
        return {"uploaded": False, "reason": "error",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def verify_export(out: Path) -> str:
    """Check a finished export against its manifest; return the manifest SHA-256.

    Every listed file must exist with the recorded size and hash, and the
    folder must hold nothing else, since every file in it is uploaded.
    """
    manifest_file = out / artifacts.MANIFEST_NAME
    manifest = artifacts.read_manifest(manifest_file) if out.is_dir() else None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ExportIncomplete(f"export {out.name} has no readable manifest")
    listed = set()
    for f in manifest["files"]:
        path = out / str(f.get("path"))
        listed.add(path.name)
        if (not path.is_file() or path.stat().st_size != f.get("bytes")
                or artifacts.sha256_file(path) != f.get("sha256")):
            raise ExportIncomplete(f"export {out.name}: {path.name} does not match the manifest")
    extra = {p.name for p in out.iterdir()} - listed - {artifacts.MANIFEST_NAME}
    if extra:
        raise ExportIncomplete(f"export {out.name}: unexpected files {sorted(extra)}")
    return artifacts.sha256_file(manifest_file)


async def upload_existing_export(db, run_id: str, *, out_root: Path | None = None,
                                 storage_config: object_storage.StorageConfig | None = None,
                                 storage_client=None) -> dict:
    """Upload an existing export again (e.g. after a failed upload), without
    re-exporting. The folder is verified against its manifest first, and
    against the manifest hash recorded on the run when there is one."""
    rid = artifacts.safe_run_id(run_id)
    root = Path(out_root) if out_root else export_root()
    out = root / rid
    manifest_sha = await asyncio.to_thread(verify_export, out)
    run = await db.poe_runs.find_one({"_id": rid}) or {}
    recorded = {e.get("manifest_sha256") for e in run.get("exports") or []
                if isinstance(e, dict)} - {None}
    if recorded and manifest_sha not in recorded:
        raise ExportIncomplete(f"export {rid}: manifest differs from the one recorded on the run")
    upload = await asyncio.to_thread(_safe_upload, out, rid, storage_config, storage_client)
    if run:
        entry = {"manifest_sha256": manifest_sha, "at": artifacts.now_iso(),
                 "uploaded": bool(upload.get("uploaded"))}
        if upload.get("error"):
            entry["error"] = upload["error"]
        await db.poe_runs.update_one({"_id": rid}, {"$push": {"uploads": entry}})
    return {"run_id": rid, "dir": artifacts.data_relative(out, root),
            "manifest_sha256": manifest_sha, **upload}
