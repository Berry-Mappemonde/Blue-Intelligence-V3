"""
app.routers.exports — Dated, immutable GeoJSON export snapshots.

Inspired by Open Waters: Seamap: each published build is a dated archive
that never changes again (`<YYYY-MM-DD>.pmtiles` on their side). Here a snapshot
writes the seven datasets under ``backend/exports/<YYYY-MM-DD>/`` with a
``MANIFEST.json`` (version, fingerprint, per-file counts). An already-written
folder is never rewritten: posting the same day again returns 409.

Endpoints:
  * ``POST /api/export/snapshot``                    — write today's snapshot
  * ``GET  /api/export/snapshots``                   — list snapshots (manifests)
  * ``GET  /api/export/snapshots/{date}/{filename}`` — download an archived file
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import ROUTE_FILE
from app.core.export_meta import versioned_fc
from app.db import db

router = APIRouter(prefix="/api")

_REPO_BACKEND = Path(__file__).resolve().parents[2]
EXPORTS_DIR = Path(os.environ.get("EXPORTS_DIR") or _REPO_BACKEND / "exports")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILE_RE = re.compile(r"^[a-z0-9_.-]+\.(geojson|json)$")


async def _fc_projects() -> tuple[dict, str]:
    from app.routers.projects import project_to_feature
    docs = await db.projects.find({}).to_list(20000)
    fc = {"type": "FeatureCollection",
          "features": [project_to_feature(p) for p in docs]}
    return fc, "Extraction automatique de sources publiques (fondations) — vérifier la source de chaque fiche"


async def _fc_marinas() -> tuple[dict, str]:
    from app.routers.marinas import _all_marinas
    from app.services.marina_world import marinas_to_slim_geojson
    docs = await _all_marinas({})
    return marinas_to_slim_geojson(docs), "© OpenStreetMap contributors (ODbL)"


async def _fc_anchorages() -> tuple[dict, str]:
    from app.services.anchorage_build import anchorages_to_geojson
    docs = await db.anchorages.find({}).sort(
        [("priority", 1), ("name", 1)]).to_list(20000)
    return anchorages_to_geojson(docs), "© OpenStreetMap contributors (ODbL)"


async def _fc_capitaineries() -> tuple[dict, str]:
    from app.routers.capitaineries import _all_docs
    from app.services.capitainerie_world import to_slim_geojson
    docs = await _all_docs({})
    return to_slim_geojson(docs), (
        "© OpenStreetMap contributors (ODbL) · SHOM INFORMATIONS_PORTUAIRES "
        "(Licence Ouverte Etalab) · NOAA ENC Direct to GIS")


async def _fc_amp() -> tuple[dict, str]:
    from app.services import amp as amp_svc
    docs = await db.amp_sites.find({}, amp_svc.SLIM_PROJECTION).to_list(20000)
    fc = amp_svc.to_feature_collection(docs, geometry=False, extra={
        "name": "amp_sites",
        "note": "centroids + manager_url / visit_url — not official boundaries",
    })
    return fc, "ProtectedSeas Navigator — centroïdes et métadonnées, pas les limites officielles"


async def _fc_poe() -> tuple[dict, str]:
    from app.services.poe_pipeline import ports_to_geojson
    docs = await db.poe_ports.find({}).to_list(10000)
    return ports_to_geojson(docs), (
        "Extraction Blue Intelligence de sources gouvernementales — "
        "chaque port cite sa source ; statuts à vérifier avant escale")


async def _fc_route() -> tuple[dict, str]:
    if not ROUTE_FILE.exists():
        raise HTTPException(404, "route.geojson not found")
    fc = json.loads(ROUTE_FILE.read_text(encoding="utf-8"))
    return fc, "Naviguide — Berry-Mappemonde (route officielle)"


# dataset name → (archived filename, builder)
DATASETS: dict[str, tuple[str, object]] = {
    "projects": ("projects.geojson", _fc_projects),
    "marinas": ("marinas.geojson", _fc_marinas),
    "anchorages": ("anchorages.geojson", _fc_anchorages),
    "capitaineries": ("capitaineries.geojson", _fc_capitaineries),
    "amp": ("amp.geojson", _fc_amp),
    "poe": ("ports_of_entry.geojson", _fc_poe),
    "route": ("route.geojson", _fc_route),
}


def write_snapshot_files(base_dir: Path, date: str,
                         datasets: dict[str, dict]) -> dict:
    """Write versioned files + MANIFEST.json. Refuse an existing folder.

    Pure vs Mongo (testable): ``datasets`` maps the dataset name
    to its ALREADY versioned FeatureCollection (``metadata`` block present).
    """
    target = base_dir / date
    if target.exists():
        raise FileExistsError(date)
    target.mkdir(parents=True)
    manifest: dict = {
        "date": date,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "immutable": True,
        "files": {},
    }
    for name, fc in datasets.items():
        filename = DATASETS[name][0]
        payload = json.dumps(fc, ensure_ascii=False)
        path = target / filename
        path.write_text(payload, encoding="utf-8")
        meta = fc.get("metadata") or {}
        manifest["files"][filename] = {
            "dataset": name,
            "version": meta.get("version"),
            "content_sha256": meta.get("content_sha256"),
            "count": meta.get("count"),
            "bytes": path.stat().st_size,
        }
    (target / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


@router.post("/export/snapshot")
async def create_snapshot():
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if (EXPORTS_DIR / date).exists():
        raise HTTPException(
            409, f"snapshot {date} already exists — snapshots are immutable")
    datasets: dict[str, dict] = {}
    for name, (_filename, builder) in DATASETS.items():
        fc, license_note = await builder()
        datasets[name] = versioned_fc(fc, name, license_note=license_note)
    try:
        manifest = write_snapshot_files(EXPORTS_DIR, date, datasets)
    except FileExistsError:
        raise HTTPException(
            409, f"snapshot {date} already exists — snapshots are immutable")
    return manifest


@router.get("/export/snapshots")
async def list_snapshots():
    if not EXPORTS_DIR.exists():
        return {"snapshots": []}
    out = []
    for child in sorted(EXPORTS_DIR.iterdir(), reverse=True):
        if not child.is_dir() or not _DATE_RE.match(child.name):
            continue
        manifest_path = child / "MANIFEST.json"
        if manifest_path.exists():
            try:
                out.append(json.loads(manifest_path.read_text(encoding="utf-8")))
            except Exception:
                out.append({"date": child.name, "error": "invalid manifest"})
    return {"snapshots": out}


@router.get("/export/snapshots/{date}/{filename}")
async def download_snapshot_file(date: str, filename: str):
    if not _DATE_RE.match(date) or not _FILE_RE.match(filename):
        raise HTTPException(400, "invalid snapshot path")
    path = (EXPORTS_DIR / date / filename).resolve()
    if not str(path).startswith(str(EXPORTS_DIR.resolve())) or not path.is_file():
        raise HTTPException(404, "snapshot file not found")
    return FileResponse(path, media_type="application/geo+json",
                        filename=filename)
