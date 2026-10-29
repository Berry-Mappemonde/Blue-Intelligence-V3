"""IBTrACS v04r01 tracks (since 1980) — numeric counter, not an LLM "season".

Cost note (incident 2026-09-21): ``crossings`` used to test every point of
every storm of the month against the leg (~1 s of Python per call, 929
storms in September). A per-storm bounding box, padded by the search
radius, now discards the storms that are certainly too far before any
haversine runs; identical requests are served from an LRU. Results are
unchanged — the prefilter is conservative (see tests).
"""
from __future__ import annotations

import calendar
import copy
import json
import math
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from app.services.climatology_common import (
    CYCLONE_PERIOD,
    KIND,
    LICENSE_IBTRACS,
    SOURCE_IDS,
    climatology_dir,
    haversine_nm,
    parse_month,
    point_to_segment_nm,
    rule,
    wrap_lon,
)

NCEI_STORM_URL = "https://www.ncei.noaa.gov/products/international-best-track-archive"


def cyclones_path() -> Path:
    return climatology_dir() / "cyclones" / "ibtracs_since1980.json"


def has_snapshot() -> bool:
    return cyclones_path().is_file()


@lru_cache(maxsize=1)
def _load_index() -> dict:
    path = cyclones_path()
    if not path.is_file():
        return {"kind": KIND, "storms": [], "period": CYCLONE_PERIOD, "count": 0}
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.setdefault("storms", [])
    return raw


def reload_index() -> dict:
    _load_index.cache_clear()
    _crossings_cached.cache_clear()
    _nearby_cached.cache_clear()
    return _load_index()


# ── Bounding-box prefilter ───────────────────────────────────────────────────
# Padding is generous on purpose: the box only says « certainly too far ».
_PAD_DEG = 0.3
_MAX_COS_LAT = 85.0


def _storm_bbox(storm: dict) -> tuple[float, float, float, float] | None:
    """(min_lat, max_lat, min_lon, max_lon) on the unwrapped track, memoised on the storm."""
    bb = storm.get("_bbox")
    if bb is None:
        coords = _unwrap_coords(storm.get("coords") or [])
        if coords:
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            bb = (min(lats), max(lats), min(lons), max(lons))
        else:
            bb = ()
        storm["_bbox"] = bb
    return bb or None


def _pads(radius_nm: float, max_abs_lat: float) -> tuple[float, float]:
    lat_pad = radius_nm / 60.0 + _PAD_DEG
    cos_lat = math.cos(math.radians(min(abs(max_abs_lat) + lat_pad, _MAX_COS_LAT)))
    lon_pad = radius_nm / 60.0 / max(cos_lat, 0.05) + _PAD_DEG
    return lat_pad, lon_pad


def bbox_near_segment(
    bb: tuple[float, float, float, float],
    lat1: float, lon1: float, lat2: float, lon2: float, radius_nm: float,
) -> bool:
    """False only when every point of the box is farther than ``radius_nm``
    from the leg (which ``point_to_segment_nm`` samples linearly, the short
    way round in longitude). The storm box may live past ±180 (unwrapped):
    the leg is tested shifted by −360 / 0 / +360."""
    b_lat_min, b_lat_max, b_lon_min, b_lon_max = bb
    s_lat_min, s_lat_max = min(lat1, lat2), max(lat1, lat2)
    lat_pad, lon_pad = _pads(radius_nm, max(abs(s_lat_min), abs(s_lat_max), abs(b_lat_min), abs(b_lat_max)))
    if s_lat_min - lat_pad > b_lat_max or s_lat_max + lat_pad < b_lat_min:
        return False
    dlon = wrap_lon(lon2 - lon1)
    lo, hi = min(lon1, lon1 + dlon), max(lon1, lon1 + dlon)
    for shift in (-360.0, 0.0, 360.0):
        if lo + shift - lon_pad <= b_lon_max and hi + shift + lon_pad >= b_lon_min:
            return True
    return False


def bbox_near_point(bb: tuple[float, float, float, float], lat: float, lon: float, radius_nm: float) -> bool:
    return bbox_near_segment(bb, lat, lon, lat, lon, radius_nm)


def _min_kn() -> float:
    return float(rule("climatology.cyclone_min_kn", 34))


def storms_for_month(month: int, *, min_kn: float | None = None) -> list[dict]:
    month = parse_month(month)
    floor = _min_kn() if min_kn is None else float(min_kn)
    out = []
    for s in _load_index().get("storms") or []:
        months = s.get("months") or []
        if month not in months:
            continue
        if float(s.get("max_wind_kn") or 0) < floor:
            continue
        out.append(s)
    return out


def _unwrap_lon(prev: float, lon: float) -> float:
    x = float(lon)
    while x - prev > 180.0:
        x -= 360.0
    while x - prev < -180.0:
        x += 360.0
    return x


def _unwrap_coords(coords: list[list[float]]) -> list[list[float]]:
    """170 → −170 becomes 170 → 190. One storm stays one continuous line."""
    clean = [c for c in coords if len(c) >= 2]
    if not clean:
        return []
    out = [[float(clean[0][0]), float(clean[0][1])]]
    for pt in clean[1:]:
        out.append([_unwrap_lon(out[-1][0], float(pt[0])), float(pt[1])])
    return out


def _color(max_wind: float) -> str:
    if max_wind >= 96:
        return "#ef4444"
    if max_wind >= 64:
        return "#f97316"
    return "#eab308"


def tracks_geojson(month: int) -> dict:
    month = parse_month(month)
    features = []
    for s in storms_for_month(month):
        coords = _unwrap_coords(s.get("coords") or [])
        if len(coords) < 2:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "kind": KIND,
                "sid": s.get("sid"),
                "name": s.get("name"),
                "season": s.get("season"),
                "basin": s.get("basin"),
                "month": month,
                "max_wind_kn": s.get("max_wind_kn"),
                "wind_source": s.get("wind_source"),
                "color": _color(float(s.get("max_wind_kn") or 0)),
                "url": f"{NCEI_STORM_URL}",
            },
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "attribution": LICENSE_IBTRACS,
        "_climatology": {
            "kind": KIND,
            "month": month,
            "period": CYCLONE_PERIOD,
            "source_ids": [SOURCE_IDS["cyclone"]],
            "snapshot_present": has_snapshot(),
            "wind_note": (
                "USA_WIND (1 min) when present, else WMO_WIND "
                "(10 min, basin-dependent). Field wind_source names the mix."
            ),
        },
    }


def _in_dayrange(storm: dict, month: int, day: int | None, dayrange: int) -> bool:
    if day is None:
        return month in (storm.get("months") or [])
    start = storm.get("start")
    end = storm.get("end")
    if not start or not end:
        return month in (storm.get("months") or [])
    try:
        s = datetime.fromisoformat(str(start)[:10]).date()
        e = datetime.fromisoformat(str(end)[:10]).date()
    except ValueError:
        return month in (storm.get("months") or [])
    # Fenêtre autour du jour calendaire. Un cyclone peut franchir le Nouvel An :
    # on teste donc l'année de début *et* l'année de fin, et on retient la fenêtre
    # qui intersecte réellement l'activité de la tempête. Sinon un cyclone déc→jan
    # est cherché dans l'année de début et silencieusement écarté.
    for year in {s.year, e.year}:
        try:
            center = date(year, month, min(day, calendar.monthrange(year, month)[1]))
        except ValueError:
            continue
        lo, hi = center - timedelta(days=dayrange), center + timedelta(days=dayrange)
        if s <= hi and e >= lo:
            return True
    return False


def crossings(
    lat1: float, lon1: float, lat2: float, lon2: float,
    month: int, *, day: int | None = None, dayrange: int | None = None,
    radius_nm: float | None = None, prefilter: bool = True,
) -> dict:
    """OpenCPN ``CycloneTrackCrossings`` spirit: integer + list, never prose.

    ``prefilter=False`` runs the brute force (tests compare both)."""
    month = parse_month(month)
    window = int(dayrange if dayrange is not None else rule("climatology.cyclone_dayrange", 21))
    radius = float(radius_nm if radius_nm is not None else rule("climatology.cyclone_radius_nm", 120))
    if not prefilter:
        return _crossings_compute(lat1, lon1, lat2, lon2, month, day, window, radius, False)
    # 3 decimals ≈ 100 m: an identical leg (film tick, twin tab) is free.
    return copy.deepcopy(_crossings_cached(
        round(float(lat1), 3), round(float(lon1), 3), round(float(lat2), 3), round(float(lon2), 3),
        month, day, window, radius,
    ))


@lru_cache(maxsize=4096)
def _crossings_cached(lat1, lon1, lat2, lon2, month, day, window, radius) -> dict:
    return _crossings_compute(lat1, lon1, lat2, lon2, month, day, window, radius, True)


def _crossings_compute(lat1, lon1, lat2, lon2, month, day, window, radius, prefilter) -> dict:
    min_kn = _min_kn()
    hits = []
    for s in _load_index().get("storms") or []:
        if float(s.get("max_wind_kn") or 0) < min_kn:
            continue
        if not _in_dayrange(s, month, day, window):
            continue
        if prefilter:
            bb = _storm_bbox(s)
            if bb is None or not bbox_near_segment(bb, lat1, lon1, lat2, lon2, radius):
                continue
        coords = _unwrap_coords(s.get("coords") or [])
        near = False
        for pt in coords:
            if len(pt) < 2:
                continue
            if point_to_segment_nm(pt[1], pt[0], lat1, lon1, lat2, lon2) <= radius:
                near = True
                break
        if not near and len(coords) >= 2:
            for a, b in zip(coords, coords[1:]):
                if (
                    point_to_segment_nm(lat1, lon1, a[1], a[0], b[1], b[0]) <= radius
                    or point_to_segment_nm(lat2, lon2, a[1], a[0], b[1], b[0]) <= radius
                ):
                    near = True
                    break
        if near:
            hits.append({
                "sid": s.get("sid"),
                "name": s.get("name"),
                "season": s.get("season"),
                "basin": s.get("basin"),
                "max_wind_kn": s.get("max_wind_kn"),
                "wind_source": s.get("wind_source"),
                "start": s.get("start"),
                "end": s.get("end"),
            })
    return {
        "kind": KIND,
        "count": len(hits),
        "storms": hits,
        "month": month,
        "dayrange": window,
        "radius_nm": radius,
        "period": CYCLONE_PERIOD,
        "source": SOURCE_IDS["cyclone"],
        "snapshot_present": has_snapshot(),
    }


def tracks_in_month(month: int) -> int:
    return len(storms_for_month(month))


def nearby_count(lat: float, lon: float, month: int, radius_nm: float | None = None,
                 prefilter: bool = True) -> int:
    radius = float(radius_nm if radius_nm is not None else rule("climatology.cyclone_radius_nm", 120))
    if not prefilter:
        return _nearby_compute(float(lat), float(lon), parse_month(month), radius, False)
    return _nearby_cached(round(float(lat), 3), round(float(lon), 3), parse_month(month), radius)


@lru_cache(maxsize=8192)
def _nearby_cached(lat, lon, month, radius) -> int:
    return _nearby_compute(lat, lon, month, radius, True)


def _nearby_compute(lat, lon, month, radius, prefilter) -> int:
    n = 0
    for s in storms_for_month(month):
        if prefilter:
            bb = _storm_bbox(s)
            if bb is None or not bbox_near_point(bb, lat, lon, radius):
                continue
        for pt in s.get("coords") or []:
            if len(pt) >= 2 and haversine_nm(lat, lon, pt[1], pt[0]) <= radius:
                n += 1
                break
    return n
