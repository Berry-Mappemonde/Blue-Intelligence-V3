"""app.routers.climatology — 7th mode: sourced monthly atlas, kind climatology.

GET /api/climatology/meta
GET /api/climatology/point?lat=&lon=&month=
GET /api/climatology/crossings?lat1=&lon1=&lat2=&lon2=&month=
GET /api/climatology/{wind|wave|current|cyclones}.geojson?month=
GET /api/climatology/{wind|wave|current}/tiles/{z}/{x}/{y}.json?month=
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from app.core.export_meta import DISCLAIMER_EN, DISCLAIMER_FR, versioned_fc
from app.services.climatology_common import (
    CMEMS_CREDIT,
    CURRENT_PERIOD,
    CYCLONE_PERIOD,
    DOI,
    IBTRACS_CREDIT,
    KIND,
    LICENSE_CMEMS,
    LICENSE_IBTRACS,
    PERIOD_SUMMARY,
    PROVENANCE,
    SOURCE_IDS,
    WAVE_PERIOD,
    WIND_PERIOD,
    empty_blocks,
    is_land,
    parse_month,
    product_meta,
    snapshot_status,
    wrap_lon,
)
from app.services.climatology_current import current_at, current_geojson, current_tile
from app.services.climatology_cyclones import crossings as cyclone_crossings
from app.services.climatology_cyclones import nearby_count, tracks_geojson, tracks_in_month
from app.services.climatology_wave import wave_at, wave_geojson, wave_tile
from app.services.climatology_wind import atlas_at, wind_geojson, wind_tile

router = APIRouter(prefix="/api")

# The atlas (1980-2020) is immutable: browsers, nginx and Cloudflare may keep
# a point for a month. Handlers below are plain ``def`` on purpose: the
# cyclone maths is CPU-bound Python and used to run inside the event loop
# (incident 2026-09-21 — one slow request froze every other route).
CACHE_CONTROL_POINT = "public, max-age=2592000"
CACHE_CONTROL_LAYER = "public, max-age=86400"


def _month(month: int) -> int:
    try:
        return parse_month(month)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _coord(lat: float, lon: float) -> tuple[float, float]:
    """Accept unwrapped longitudes (186, −190) and fold them into ±180 for the grids."""
    if not (-90.0 <= lat <= 90.0):
        raise HTTPException(400, "lat/lon out of range")
    try:
        lon_w = wrap_lon(lon)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "lat/lon out of range") from exc
    return float(lat), float(lon_w)


@router.get("/climatology/meta")
async def climatology_meta(month: int = Query(1, ge=1, le=12)):
    month = _month(month)
    snaps = snapshot_status(month)
    return {
        "kind": KIND,
        "month": month,
        "period": PERIOD_SUMMARY,
        "periods": {
            "wind": WIND_PERIOD,
            "wave": WAVE_PERIOD,
            "current": CURRENT_PERIOD,
            "cyclone": CYCLONE_PERIOD,
        },
        "provenance": PROVENANCE,
        "source_ids": list(SOURCE_IDS.values()),
        "doi": DOI,
        "snapshot": snaps,
        "attribution": {
            "cmems": CMEMS_CREDIT,
            "ibtracs": IBTRACS_CREDIT,
        },
        "disclaimer": DISCLAIMER_EN,
        "disclaimer_fr": DISCLAIMER_FR,
        "review": False,
        "products": {k: product_meta(k, month) for k in ("wind", "wave", "current", "cyclone")},
    }


@router.get("/climatology/point")
def climatology_point(
    response: Response,
    lat: float = Query(...),
    lon: float = Query(...),
    month: int = Query(..., ge=1, le=12),
    dest_lat: float | None = Query(None),
    dest_lon: float | None = Query(None),
    day: int | None = Query(None, ge=1, le=31),
):
    response.headers["Cache-Control"] = CACHE_CONTROL_POINT
    lat, lon = _coord(lat, lon)
    month = _month(month)
    land = is_land(lat, lon)
    blocks = empty_blocks()
    if not land:
        blocks["wind_atlas"] = atlas_at(lat, lon, month)
        blocks["wave"] = wave_at(lat, lon, month)
        blocks["current"] = current_at(lat, lon, month)
    blocks["cyclone"] = {
        "tracks_in_month": tracks_in_month(month),
        "nearby": nearby_count(lat, lon, month) if not land else 0,
        "crossings_if_leg": None,
    }
    if dest_lat is not None and dest_lon is not None:
        dest_lat, dest_lon = _coord(dest_lat, dest_lon)
        blocks["cyclone"]["crossings_if_leg"] = cyclone_crossings(
            lat, lon, dest_lat, dest_lon, month, day=day,
        )
    return {
        "kind": KIND,
        "month": month,
        "period": PERIOD_SUMMARY,
        "provenance": PROVENANCE,
        "periods": {
            "wind": WIND_PERIOD,
            "wave": WAVE_PERIOD,
            "current": CURRENT_PERIOD,
            "cyclone": CYCLONE_PERIOD,
        },
        "coordinates": {
            "latitude": lat,
            "longitude": lon,
            "cell_selection": "land" if land else "sea",
        },
        "snapshot": snapshot_status(month),
        "disclaimer": DISCLAIMER_EN,
        "disclaimer_fr": DISCLAIMER_FR,
        **blocks,
    }


@router.get("/climatology/crossings")
def climatology_crossings(
    response: Response,
    lat1: float = Query(...),
    lon1: float = Query(...),
    lat2: float = Query(...),
    lon2: float = Query(...),
    month: int = Query(..., ge=1, le=12),
    day: int | None = Query(None, ge=1, le=31),
    dayrange: int | None = Query(None, ge=7, le=45),
):
    response.headers["Cache-Control"] = CACHE_CONTROL_POINT
    lat1, lon1 = _coord(lat1, lon1)
    lat2, lon2 = _coord(lat2, lon2)
    return cyclone_crossings(lat1, lon1, lat2, lon2, _month(month), day=day, dayrange=dayrange)


def _export(fc: dict, dataset: str, license_note: str):
    meta = fc.pop("_climatology", {}) or {}
    return versioned_fc(
        fc,
        dataset,
        license_note=license_note,
        period=meta.get("period"),
        month=meta.get("month"),
        source_ids=meta.get("source_ids"),
        doi=meta.get("doi"),
        extra_metadata=meta,
    )


@router.get("/climatology/wind.geojson")
def climatology_wind_geojson(
    response: Response,
    month: int = Query(..., ge=1, le=12),
    spacing_deg: float = Query(1.0, ge=0.5, le=4.0),
):
    response.headers["Cache-Control"] = CACHE_CONTROL_LAYER
    return _export(wind_geojson(_month(month), spacing_deg=spacing_deg),
                   "climatology-wind", LICENSE_CMEMS)


@router.get("/climatology/wave.geojson")
def climatology_wave_geojson(
    response: Response,
    month: int = Query(..., ge=1, le=12),
    stat: str = Query("p90"),
    spacing_deg: float = Query(1.0, ge=0.5, le=4.0),
):
    response.headers["Cache-Control"] = CACHE_CONTROL_LAYER
    return _export(wave_geojson(_month(month), stat=stat, spacing_deg=spacing_deg),
                   "climatology-wave", LICENSE_CMEMS)


@router.get("/climatology/current.geojson")
def climatology_current_geojson(
    response: Response,
    month: int = Query(..., ge=1, le=12),
    spacing_deg: float = Query(1.0, ge=0.5, le=4.0),
):
    response.headers["Cache-Control"] = CACHE_CONTROL_LAYER
    return _export(current_geojson(_month(month), spacing_deg=spacing_deg),
                   "climatology-current", LICENSE_CMEMS)


@router.get("/climatology/cyclones.geojson")
def climatology_cyclones_geojson(response: Response, month: int = Query(..., ge=1, le=12)):
    response.headers["Cache-Control"] = CACHE_CONTROL_LAYER
    return _export(tracks_geojson(_month(month)),
                   "climatology-cyclones", LICENSE_IBTRACS)


@router.get("/climatology/{kind}/tiles/{z}/{x}/{y}.json")
def climatology_tile(
    response: Response,
    kind: str,
    z: int,
    x: int,
    y: int,
    month: int = Query(..., ge=1, le=12),
    stat: str = Query("p90"),
):
    """XYZ Web Mercator tile. Empty FeatureCollection (200) if out of range, land, or no snapshot."""
    response.headers["Cache-Control"] = CACHE_CONTROL_LAYER
    if kind not in ("wind", "wave", "current"):
        raise HTTPException(404, "unknown climatology kind")
    month = _month(month)
    empty = {"type": "FeatureCollection", "features": []}
    if z < 0 or z > 12 or x < 0 or y < 0:
        return empty
    n = 1 << int(z)
    if x >= n or y >= n:
        return empty
    if kind == "wind":
        return wind_tile(month, z, x, y)
    if kind == "wave":
        return wave_tile(month, z, x, y, stat=stat)
    return current_tile(month, z, x, y)
