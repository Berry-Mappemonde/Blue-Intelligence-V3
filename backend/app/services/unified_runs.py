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

import json
import os
import shutil
from pathlib import Path

from app.config import DATA_DIR
from app.services import object_storage
from app.services import poe_runs
from app.services import run_artifacts as artifacts

POE_LAYERS = ("poe", "formalities")
LEGACY_ENDPOINTS = {
    "projects": "POST /api/projects/runs",
    "marinas": "POST /api/marinas/build",
    "capitaineries": "POST /api/capitaineries/build",
    "amp": "POST /api/amp/discover-visit-urls",
}
EXPORT_FILES = ("ports.geojson", "zones.json", artifacts.TRACE_NAME)
GEOPARQUET_NOTE = "not written: pyarrow / geopandas are not backend dependencies"


class RunExists(ValueError):
    pass


class UnsupportedLayer(ValueError):
    pass


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
                         variant: str = "tinyfish") -> dict:
    """Check the request and return the execute_run arguments.

    A new run never reuses an existing run_id. A resume reuses the stored
    parameters so the zone list cannot drift between attempts.
    """
    normalize_layer(layer)
    if resume:
        if not run_id:
            raise ValueError("run_id required to resume")
        artifacts.safe_run_id(run_id)
        existing = await db.poe_runs.find_one({"_id": run_id})
        if not existing:
            raise ValueError(f"run {run_id} unknown — cannot resume")
        params = existing.get("params") or {}
        return {
            "run_id": run_id, "resume": True,
            "label": label or existing.get("label") or "",
            "limit": int(params.get("limit") or 0),
            "only_zones": params.get("only_zones"),
            "concurrency": int(params.get("concurrency") or concurrency),
            "variant": params.get("variant") or variant,
        }
    rid = run_id or poe_runs.new_run_id()
    artifacts.safe_run_id(rid)
    if await db.poe_runs.find_one({"_id": rid}):
        raise RunExists(f"run {rid} already exists — pass resume=True")
    return {"run_id": rid, "resume": False, "label": label, "limit": limit,
            "only_zones": zones, "concurrency": concurrency, "variant": variant}


async def start_run(db, state, *, layer: str = "poe", run_id: str | None = None,
                    resume: bool = False, label: str = "", limit: int = 0,
                    zones: list[int] | None = None, concurrency: int = 2,
                    variant: str = "tinyfish", rules_overrides: dict | None = None,
                    profile: str | None = None, resolved: dict | None = None) -> dict:
    """Launch (or resume) a run. Writes the Mongo run doc, trace and manifest."""
    args = resolved or await resolve_launch(
        db, layer=layer, run_id=run_id, resume=resume, label=label, limit=limit,
        zones=zones, concurrency=concurrency, variant=variant)
    return await poe_runs.execute_run(
        db, state, args["run_id"], label=args["label"], limit=args["limit"],
        only_zones=args["only_zones"], concurrency=args["concurrency"],
        resume=args["resume"], variant=args["variant"],
        rules_overrides=None if args["resume"] else rules_overrides,
        profile=None if args["resume"] else profile)


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
    root = Path(out_root) if out_root else export_root()
    out = root / rid
    if out.exists():
        raise FileExistsError(f"export {out} already exists")
    partial = root / f".{rid}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)

    ports = await db.poe_run_ports.find({"run_id": rid}).to_list(None)
    zones = await db.poe_run_zones.find({"run_id": rid}).to_list(None)
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
    os.replace(partial, out)
    manifest_sha = artifacts.sha256_file(out / artifacts.MANIFEST_NAME)

    upload = object_storage.upload_export(
        out, rid, config=storage_config, client=storage_client)
    await db.poe_runs.update_one({"_id": rid}, {"$push": {"exports": {
        "dir": str(out), "manifest_sha256": manifest_sha,
        "written_at": manifest["written_at"],
        "uploaded": bool(upload.get("uploaded")),
    }}})
    return {"run_id": rid, "dir": str(out), "manifest_sha256": manifest_sha,
            "files": manifest["files"], "counts": counts, "upload": upload}
