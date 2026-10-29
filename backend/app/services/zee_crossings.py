"""
EEZ Detection — Phase 8 (Exclusive Economic Zones).

Detects EEZs crossed by the Berry-Mappemonde route by intersecting
maritime segments with MarineRegions.org EEZ polygons.

Data sources (priority order):
  1. Local file  data/eez_french.geojson — generated from the official
     Maritime Boundaries v12 GeoPackage supplied by the user (Flanders
     Marine Institute, 2023 — doi:10.14284/632, CC-BY 4.0).
     Contains every French-sovereignty EEZ + foreign EEZs
     crossed by the route, geometries simplified to 0.01° (~1 km).
  2. VLIZ GeoServer WFS (MarineRegions:eez, sovereign1='France') — re-downloads
     French EEZs if the local file is missing, then simplifies + caches.
  3. MRGID REST fallback — downloads each French EEZ individually.
  4. point-in-EEZ fallback — samples the route every 100 NM and
     queries the getGazetteerRecordsByLatLng REST API (no polygon required).

Intersection: shapely ≥ 2.0 (run in a thread, CPU-bound).
Territory mapping: MRGID (primary key) → territory_code (territories.json),
geoname substring fallback. Non-French EEZs are kept
in crossings with territory_code=None (they do not trigger
formalities generation). Multi-claim zones (pol_type
"Overlapping claim" / "Joint regime") are exposed as-is.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Callable

import httpx

try:
    from app.services.marina_build import USER_AGENT
except ImportError:
    USER_AGENT = "BlueIntelligence/1.0 (Berry-Mappemonde expedition)"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

from app.config import DATA_DIR

EEZ_FILE = DATA_DIR / "eez_french.geojson"

VLIZ_WFS_URL = "https://geo.vliz.be/geoserver/MarineRegions/wfs"
VLIZ_WFS_PARAMS: dict = {
    "service": "WFS",
    "version": "1.0.0",
    "request": "GetFeature",
    "typeName": "MarineRegions:eez",
    "outputFormat": "application/json",
    "CQL_FILTER": "sovereign1 = 'France'",
}

MRGID_GEOJSON_URL = "https://www.marineregions.org/rest/getGazetteerGeometryAsGeoJson.json/"
MRGID_LATLON_URL = "https://www.marineregions.org/rest/getGazetteerRecordsByLatLng.json/"

# Simplification tolerance applied before caching (degrees).
SIMPLIFY_TOLERANCE_DEG = 0.01

# ---------------------------------------------------------------------------
# Mapping MRGID (MarineRegions v12) → territory_code (territories.json)
# MRGIDs verified on real VLIZ WFS + GeoPackage v12 data (2026-06).
# ---------------------------------------------------------------------------
MRGID_TO_TERRITORY: dict[int, str | None] = {
    5677: "france_metropolitaine",     # French Exclusive Economic Zone (mainland)
    48966: "france_metropolitaine",    # Joint regime area: Spain / France (Golfe de Gascogne)
    48976: "france_metropolitaine",    # Joint regime area: France / Italy
    8440: "polynesie_francaise",       # French Polynesia EEZ
    8312: "nouvelle_caledonie",        # New Caledonia EEZ
    48948: "nouvelle_caledonie",       # Overlapping claim Matthew & Hunter: France / Vanuatu
    33178: "martinique",               # Martinique EEZ
    33177: "guadeloupe",               # Guadeloupe EEZ
    48952: "saint_barthelemy",         # Saint-Barthélemy EEZ
    8495: "saint_martin",              # Collectivity of Saint Martin EEZ
    8462: "guyane",                    # French Guiana EEZ
    8454: "wallis_et_futuna",          # Wallis and Futuna EEZ
    8338: "la_reunion",                # Reunion EEZ
    48944: "mayotte",                  # Overlapping claim Mayotte: France / Comores
    8494: "saint_pierre_et_miquelon",  # Saint-Pierre and Miquelon EEZ
    # TAAF — Scattered Islands + southern districts
    48946: "taaf",                     # Overlapping claim Ile Tromelin: France / Madagascar / Mauritius
    48945: "taaf",                     # Overlapping claim Glorioso Islands: France / Madagascar
    8341: "taaf",                      # Europa Island EEZ
    8339: "taaf",                      # Juan de Nova Island EEZ
    8340: "taaf",                      # Bassas da India EEZ
    8386: "taaf",                      # Amsterdam & Saint Paul Islands EEZ
    8385: "taaf",                      # Crozet Islands EEZ
    8387: "taaf",                      # Kerguélen EEZ
    8401: None,                        # Clipperton Island — French but outside territories.json
}

# Fallback: substring match on the geoname (lower-cased),
# from most specific to most generic.
_EEZ_NAME_PATTERNS: list[tuple[str, str | None]] = [
    ("french polynesia", "polynesie_francaise"),
    ("polynésie", "polynesie_francaise"),
    ("new caledonia", "nouvelle_caledonie"),
    ("nouvelle-calédonie", "nouvelle_caledonie"),
    ("matthew and hunter", "nouvelle_caledonie"),
    ("martinique", "martinique"),
    ("guadeloupe", "guadeloupe"),
    ("saint-barthélemy", "saint_barthelemy"),
    ("saint barthélemy", "saint_barthelemy"),
    ("st. barthélemy", "saint_barthelemy"),
    ("saint-martin", "saint_martin"),
    ("saint martin", "saint_martin"),
    ("st. martin", "saint_martin"),
    ("french guiana", "guyane"),
    ("guyane", "guyane"),
    ("wallis and futuna", "wallis_et_futuna"),
    ("wallis", "wallis_et_futuna"),
    ("réunion", "la_reunion"),
    ("reunion", "la_reunion"),
    ("mayotte", "mayotte"),
    ("tromelin", "taaf"),
    ("glorioso", "taaf"),
    ("glorieuses", "taaf"),
    ("europa island", "taaf"),
    ("juan de nova", "taaf"),
    ("bassas da india", "taaf"),
    ("amsterdam and saint paul", "taaf"),
    ("crozet", "taaf"),
    ("kerguélen", "taaf"),
    ("kerguelen", "taaf"),
    ("southern and antarctic", "taaf"),
    ("terres australes", "taaf"),
    ("clipperton", None),
    ("saint-pierre", "saint_pierre_et_miquelon"),
    ("saint pierre", "saint_pierre_et_miquelon"),
    ("st. pierre", "saint_pierre_et_miquelon"),
    # Catchall: other labels containing France → mainland (joint regimes)
    ("france", "france_metropolitaine"),
    ("french", "france_metropolitaine"),
]


def _map_geoname_to_territory(
    geoname: str,
    sovereign: str = "",
    mrgid: int | None = None,
) -> str | None:
    """Known MRGID > geoname substring (if French sovereignty) > None."""
    if mrgid and mrgid in MRGID_TO_TERRITORY:
        return MRGID_TO_TERRITORY[mrgid]
    lower = (geoname or "").lower()
    sov = (sovereign or "").lower()
    if sov and "france" not in sov and "french" not in lower and "france" not in lower:
        return None
    for pattern, code in _EEZ_NAME_PATTERNS:
        if pattern in lower:
            return code
    return None


# ---------------------------------------------------------------------------
# Simplification (before caching) — reduces ~80 MB of raw WFS to ~2 MB
# ---------------------------------------------------------------------------

def _simplify_and_strip(features: list[dict], tolerance_deg: float = SIMPLIFY_TOLERANCE_DEG) -> list[dict]:
    """Simplify each geometry (shapely, topology-preserving) and keep
    only useful properties. Synchronous/CPU-bound — call via to_thread."""
    from shapely.geometry import mapping as _mapping, shape as _shape
    try:
        from shapely.validation import make_valid as _make_valid
    except ImportError:
        def _make_valid(g):  # type: ignore[misc]
            return g.buffer(0)

    out: list[dict] = []
    for feat in features:
        props = feat.get("properties") or {}
        try:
            geom = _shape(feat["geometry"])
            if not geom.is_valid:
                geom = _make_valid(geom)
            simple = geom.simplify(tolerance_deg, preserve_topology=True)
            if simple.is_empty:
                simple = geom
            gj = _mapping(simple)
        except Exception:
            gj = feat.get("geometry")
        out.append({
            "type": "Feature",
            "geometry": gj,
            "properties": {
                "mrgid": props.get("mrgid") or props.get("MRGID"),
                "geoname": props.get("geoname") or props.get("GEONAME"),
                "sovereign1": props.get("sovereign1") or props.get("SOVEREIGN1") or "France",
                "pol_type": props.get("pol_type") or props.get("POL_TYPE"),
                "territory1": props.get("territory1") or props.get("TERRITORY1"),
            },
        })
    return out


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

async def _download_via_wfs(client: httpx.AsyncClient, *, logger: Callable | None = None) -> dict:
    """French EEZs via the VLIZ WFS. Raises on failure."""
    if logger:
        logger("[zee-wfs] Connecting to VLIZ GeoServer (MarineRegions:eez, sovereign1='France')…")
    resp = await client.get(
        VLIZ_WFS_URL,
        params=VLIZ_WFS_PARAMS,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"},
        timeout=httpx.Timeout(connect=20.0, read=300.0, write=20.0, pool=8.0),
    )
    resp.raise_for_status()
    data = resp.json()
    n = len(data.get("features") or [])
    if n == 0:
        raise RuntimeError("WFS returned 0 features — possibly wrong typeName or CQL filter")
    if logger:
        logger(f"[zee-wfs] {n} French EEZ features received from WFS")
    return data


async def _download_via_mrgid_rest(client: httpx.AsyncClient, *, logger: Callable | None = None) -> dict:
    """Fallback: download each French EEZ individually via MRGID."""
    targets = {m: c for m, c in MRGID_TO_TERRITORY.items() if c is not None}
    if logger:
        logger(f"[zee-mrgid] Downloading {len(targets)} French EEZs via MRGID REST…")
    features = []
    for mrgid, territory_code in targets.items():
        try:
            r = await client.get(
                f"{MRGID_GEOJSON_URL}{mrgid}/",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=httpx.Timeout(connect=15.0, read=90.0, write=10.0, pool=8.0),
            )
            r.raise_for_status()
            data = r.json()
            if data and data.get("type"):
                features.append({
                    "type": "Feature",
                    "geometry": data,
                    "properties": {"mrgid": mrgid, "geoname": territory_code, "sovereign1": "France"},
                })
            if logger:
                logger(f"[zee-mrgid] mrgid={mrgid} ({territory_code}) → ok")
            await asyncio.sleep(0.5)
        except Exception as e:
            if logger:
                logger(f"[zee-mrgid] mrgid={mrgid} ({territory_code}) FAILED: {type(e).__name__}: {e}")
    if not features:
        raise RuntimeError("All MRGID downloads failed")
    if logger:
        logger(f"[zee-mrgid] {len(features)}/{len(targets)} EEZs downloaded")
    return {"type": "FeatureCollection", "features": features}


async def ensure_eez_file(
    eez_path: Path = EEZ_FILE,
    *,
    force_download: bool = False,
    logger: Callable | None = None,
) -> dict:
    """
    Guarantee a valid local EEZ file is available; load it.
    Local (pre-extracted v12 GeoPackage or WFS cache) → WFS → MRGID REST.
    The downloaded result is simplified then cached.
    """
    if eez_path.exists() and not force_download:
        try:
            data = json.loads(eez_path.read_text(encoding="utf-8"))
            n = len(data.get("features") or [])
            if n > 0:
                if logger:
                    src = data.get("_source", "local cache")
                    logger(f"[zee] Local EEZ file loaded: {n} features ({eez_path.name} — {src})")
                return data
        except Exception as exc:
            if logger:
                logger(f"[zee] Cache load failed ({exc}), re-downloading")

    async with httpx.AsyncClient() as client:
        eez_data: dict | None = None
        try:
            eez_data = await _download_via_wfs(client, logger=logger)
        except Exception as exc:
            if logger:
                logger(f"[zee] WFS download failed ({exc}), trying MRGID REST fallback…")
        if eez_data is None:
            try:
                eez_data = await _download_via_mrgid_rest(client, logger=logger)
            except Exception as exc:
                raise RuntimeError(
                    f"Both WFS and MRGID download failed. Last error: {exc}. "
                    "The point-in-EEZ REST fallback will be used instead."
                ) from exc

    # Simplification + strip avant cache (CPU-bound → thread)
    try:
        if logger:
            logger("[zee] Simplifying downloaded geometries (0.01°)…")
        simplified = await asyncio.to_thread(_simplify_and_strip, eez_data.get("features") or [])
        eez_data = {
            "type": "FeatureCollection",
            "features": simplified,
            "_source": "MarineRegions.org VLIZ WFS (simplified 0.01°) — CC-BY 4.0",
        }
    except Exception as exc:
        if logger:
            logger(f"[zee] Simplification failed (non-fatal, caching raw): {exc}")

    try:
        eez_path.parent.mkdir(parents=True, exist_ok=True)
        eez_path.write_text(json.dumps(eez_data, ensure_ascii=False), encoding="utf-8")
        if logger:
            logger(f"[zee] EEZ data cached → {eez_path.name} ({len(eez_data.get('features') or [])} features)")
    except Exception as exc:
        if logger:
            logger(f"[zee] Cache write failed (non-fatal): {exc}")

    return eez_data


# ---------------------------------------------------------------------------
# Point-in-EEZ API fallback (route sampling)
# ---------------------------------------------------------------------------

async def _point_in_eez(lat: float, lon: float, client: httpx.AsyncClient, *, logger: Callable | None = None) -> list[dict]:
    try:
        r = await client.get(
            MRGID_LATLON_URL,
            params={"lat": f"{lat:.5f}", "lng": f"{lon:.5f}", "offset": 0, "count": 10, "placeType": "EEZ"},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0),
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as exc:
        if logger:
            logger(f"[zee-api] point ({lat:.3f},{lon:.3f}) error: {exc}")
        return []


async def _crossings_via_point_api(
    maritime_segments: list[dict],
    step_nm: float = 100.0,
    *,
    logger: Callable | None = None,
) -> list[dict]:
    """Polygon-free fallback: EEZ transitions by sampling every
    step_nm NM + REST API. Less precise (no exact entry/exit)."""
    NM_TO_DEG = 1.0 / 60.0
    sample_points: list[tuple[int, float, float, str, str]] = []

    for seg_idx, seg in enumerate(maritime_segments):
        coords = seg.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        seg_from = seg.get("properties", {}).get("from", "")
        seg_to = seg.get("properties", {}).get("to", "")
        for i in range(len(coords) - 1):
            lon1, lat1 = coords[i][0], coords[i][1]
            lon2, lat2 = coords[i + 1][0], coords[i + 1][1]
            dlat, dlon = lat2 - lat1, lon2 - lon1
            dist_nm = math.sqrt(dlat ** 2 + dlon ** 2) / NM_TO_DEG
            n_steps = max(1, int(dist_nm / step_nm))
            for k in range(n_steps + 1):
                t = k / max(n_steps, 1)
                sample_points.append((seg_idx, lat1 + t * dlat, lon1 + t * dlon, seg_from, seg_to))

    if logger:
        logger(f"[zee-api] {len(sample_points)} sample points at {step_nm} NM intervals")

    crossings: list[dict] = []
    prev_territory: str | None = "__start__"

    async with httpx.AsyncClient() as client:
        for (seg_idx, lat, lon, seg_from, seg_to) in sample_points:
            records = await _point_in_eez(lat, lon, client, logger=logger)
            await asyncio.sleep(0.3)

            territory_code: str | None = None
            geoname: str = "High Seas"
            for rec in records:
                if rec.get("placeType") != "EEZ":
                    continue
                geoname = rec.get("preferredGazetteerName", "")
                mrgid = rec.get("MRGID") or rec.get("mrgid")
                try:
                    mrgid = int(mrgid) if mrgid else None
                except (TypeError, ValueError):
                    mrgid = None
                territory_code = _map_geoname_to_territory(geoname, "", mrgid)
                break

            if territory_code != prev_territory:
                crossings.append({
                    "order": len(crossings) + 1,
                    "seg_from": seg_from,
                    "seg_to": seg_to,
                    "territory_code": territory_code,
                    "geoname": geoname,
                    "sovereign": "France" if territory_code else "",
                    "pol_type": None,
                    "entry_lat": round(lat, 5),
                    "entry_lon": round(lon, 5),
                    "exit_lat": round(lat, 5),
                    "exit_lon": round(lon, 5),
                    "intersection_length_nm": None,
                    "detection_method": "point_api",
                })
                prev_territory = territory_code
                if logger:
                    label = territory_code or f"foreign/high_seas ({geoname})"
                    logger(f"[zee-api] transition → {label} at ({lat:.3f},{lon:.3f})")

    return crossings


# ---------------------------------------------------------------------------
# Shapely intersection engine (primary path)
# ---------------------------------------------------------------------------

def _build_eez_polys(eez_features: list[dict], *, logger: Callable | None = None) -> list[dict]:
    """GeoJSON features → validated shapely polygons + mapped metadata."""
    try:
        from shapely.geometry import shape as _shape
    except ImportError as exc:
        raise ImportError(
            "shapely >= 2.0.0 is required for ZEE detection. Install with: pip install 'shapely>=2.0.0'"
        ) from exc
    try:
        from shapely.validation import make_valid as _make_valid
    except ImportError:
        def _make_valid(g):  # type: ignore[misc]
            return g.buffer(0)

    polys: list[dict] = []
    skipped = 0
    for feat in eez_features:
        props = feat.get("properties") or {}
        geoname = str(props.get("geoname") or props.get("GEONAME") or props.get("geoname1") or "")
        sovereign = str(props.get("sovereign1") or props.get("SOVEREIGN1") or props.get("sovereign") or "")
        pol_type = props.get("pol_type") or props.get("POL_TYPE")
        mrgid_raw = props.get("mrgid") or props.get("MRGID")
        try:
            mrgid = int(mrgid_raw) if mrgid_raw is not None else None
        except (TypeError, ValueError):
            mrgid = None

        territory_code = _map_geoname_to_territory(geoname, sovereign, mrgid)
        try:
            geom = _shape(feat["geometry"])
            if geom.is_empty:
                skipped += 1
                continue
            if not geom.is_valid:
                geom = _make_valid(geom)
            if geom.is_empty:
                skipped += 1
                continue
            polys.append({
                "territory_code": territory_code,
                "geoname": geoname,
                "sovereign": sovereign or "France",
                "pol_type": pol_type,
                "mrgid": mrgid,
                "poly": geom,
            })
        except Exception as exc:
            skipped += 1
            if logger:
                logger(f"[zee-polys] skip geoname={geoname!r}: {exc}")

    if logger:
        logger(f"[zee-polys] {len(polys)} valid polygons ({skipped} skipped)")
    return polys


def _sync_compute_crossings(maritime_segments: list[dict], eez_polys: list[dict]) -> list[dict]:
    """
    Synchronous intersection (via asyncio.to_thread). Each maritime segment
    is intersected with each EEZ polygon; entries/exits are
    interpolated on the boundary; route order is preserved; consecutive
    crossings of the same EEZ are merged.
    """
    from shapely.geometry import LineString as _LS, Point as _P

    raw: list[dict] = []

    for seg_idx, seg in enumerate(maritime_segments):
        geom = seg.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        props = seg.get("properties") or {}
        if props.get("type") != "maritime":
            continue
        coords = geom["coordinates"]
        if len(coords) < 2:
            continue

        line = _LS([(float(c[0]), float(c[1])) for c in coords])
        line_start_xy = (line.coords[0][0], line.coords[0][1])
        line_end_xy = (line.coords[-1][0], line.coords[-1][1])

        for eez in eez_polys:
            poly = eez["poly"]
            try:
                if not line.intersects(poly):
                    continue
                inter = line.intersection(poly)
            except Exception:
                continue
            if inter is None or inter.is_empty:
                continue
            try:
                inter_len_deg = float(inter.length)
            except Exception:
                inter_len_deg = 0.0

            boundary_pts_raw: list[tuple[float, float]] = []
            try:
                boundary_cross = line.intersection(poly.boundary)
                if boundary_cross is not None and not boundary_cross.is_empty:
                    if hasattr(boundary_cross, "geoms"):
                        for item in boundary_cross.geoms:
                            if hasattr(item, "x"):
                                boundary_pts_raw.append((float(item.x), float(item.y)))
                    elif hasattr(boundary_cross, "x"):
                        boundary_pts_raw.append((float(boundary_cross.x), float(boundary_cross.y)))
            except Exception:
                pass

            def _proj(xy: tuple[float, float]) -> float:
                try:
                    return float(line.project(_P(xy[0], xy[1]), normalized=True))
                except Exception:
                    return 0.0

            boundary_pts_sorted = sorted(boundary_pts_raw, key=_proj)
            try:
                starts_inside = poly.contains(_P(*line_start_xy))
            except Exception:
                starts_inside = False
            try:
                ends_inside = poly.contains(_P(*line_end_xy))
            except Exception:
                ends_inside = False

            if boundary_pts_sorted:
                entry_x, entry_y = line_start_xy if starts_inside else boundary_pts_sorted[0]
                exit_x, exit_y = line_end_xy if ends_inside else boundary_pts_sorted[-1]
            else:
                entry_x, entry_y = line_start_xy
                exit_x, exit_y = line_end_xy

            try:
                entry_pos = float(line.project(_P(entry_x, entry_y), normalized=True))
                exit_pos = float(line.project(_P(exit_x, exit_y), normalized=True))
            except Exception:
                entry_pos, exit_pos = 0.0, 1.0
            mid_pos = (entry_pos + exit_pos) / 2.0

            raw.append({
                "_sort_key": (seg_idx, mid_pos),
                "seg_from": props.get("from", ""),
                "seg_to": props.get("to", ""),
                "territory_code": eez["territory_code"],
                "geoname": eez["geoname"],
                "sovereign": eez["sovereign"],
                "pol_type": eez.get("pol_type"),
                "entry_lat": round(entry_y, 5),
                "entry_lon": round(entry_x, 5),
                "exit_lat": round(exit_y, 5),
                "exit_lon": round(exit_x, 5),
                "intersection_length_nm": round(inter_len_deg * 60.0, 1),
                "detection_method": "shapely",
            })

    raw.sort(key=lambda c: c["_sort_key"])

    # Merge consecutive crossings of the same EEZ (e.g. 3 successive
    # segments in the mainland EEZ → 1 single list entry).
    merged: list[dict] = []
    for c in raw:
        del c["_sort_key"]
        prev = merged[-1] if merged else None
        if prev and prev["geoname"] == c["geoname"] and prev["territory_code"] == c["territory_code"]:
            prev["exit_lat"] = c["exit_lat"]
            prev["exit_lon"] = c["exit_lon"]
            prev["seg_to"] = c["seg_to"]
            if prev.get("intersection_length_nm") is not None and c.get("intersection_length_nm") is not None:
                prev["intersection_length_nm"] = round(prev["intersection_length_nm"] + c["intersection_length_nm"], 1)
            prev["merged_segments"] = prev.get("merged_segments", 1) + 1
        else:
            c["merged_segments"] = 1
            merged.append(c)

    for i, c in enumerate(merged):
        c["order"] = i + 1
    return merged


# ---------------------------------------------------------------------------
# Build orchestrator
# ---------------------------------------------------------------------------

async def build_zee_crossings(
    *,
    route_path: Path,
    eez_path: Path = EEZ_FILE,
    force_download: bool = False,
    use_point_api_fallback: bool = True,
    logger: Callable | None = None,
) -> list[dict]:
    """
    1. Load/download EEZ polygons.
    2. Intersect maritime segments of route.geojson (shapely, thread).
    3. point-in-EEZ REST fallback if shapely/the file fails.
    Return the ordered list of crossings.
    """
    start = time.time()

    if logger:
        logger(f"[zee] Loading route from {route_path.name}")
    try:
        route_data = json.loads(route_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Cannot read route.geojson: {exc}") from exc

    maritime_segments = [
        f for f in route_data.get("features", [])
        if f.get("geometry", {}).get("type") == "LineString"
        and (f.get("properties") or {}).get("type") == "maritime"
    ]
    if logger:
        logger(f"[zee] {len(maritime_segments)} maritime segment(s) found in route")
    if not maritime_segments:
        if logger:
            logger("[zee] WARNING: no maritime segments found — crossings will be empty")
        return []

    crossings: list[dict] = []
    shapely_ok = False

    try:
        import shapely  # noqa: F401

        eez_data = await ensure_eez_file(eez_path, force_download=force_download, logger=logger)
        eez_features = eez_data.get("features") or []

        if logger:
            logger(f"[zee] Building shapely polygons from {len(eez_features)} EEZ features…")
        eez_polys = await asyncio.to_thread(_build_eez_polys, eez_features, logger=logger)

        if logger:
            logger(f"[zee] Running shapely intersection ({len(maritime_segments)} segs × {len(eez_polys)} polys)…")
        crossings = await asyncio.to_thread(_sync_compute_crossings, maritime_segments, eez_polys)
        shapely_ok = True

        unique_territories = list({c["territory_code"] for c in crossings if c["territory_code"]})
        if logger:
            logger(
                f"[zee] Shapely: {len(crossings)} crossing(s), "
                f"{len(unique_territories)} unique FR territories — {unique_territories}"
            )
    except ImportError as exc:
        if logger:
            logger(f"[zee] shapely not available: {exc}")
    except Exception as exc:
        if logger:
            logger(f"[zee] Shapely path failed: {type(exc).__name__}: {exc}")

    if not shapely_ok and use_point_api_fallback:
        if logger:
            logger("[zee] Falling back to point-in-EEZ REST API (MarineRegions.org)…")
        try:
            crossings = await _crossings_via_point_api(maritime_segments, step_nm=100.0, logger=logger)
        except Exception as exc:
            if logger:
                logger(f"[zee] Point-in-EEZ fallback also failed: {exc}")

    elapsed = round(time.time() - start, 1)
    if logger:
        logger(f"[zee] Build complete in {elapsed}s — {len(crossings)} crossing(s) total")
    return crossings


# ---------------------------------------------------------------------------
# Utilitaires de sortie
# ---------------------------------------------------------------------------

def crossings_to_summary(crossings: list[dict]) -> dict:
    territory_codes: list[str] = []
    seen: set[str] = set()
    foreign = 0
    for c in crossings:
        code = c.get("territory_code")
        if code and code not in seen:
            territory_codes.append(code)
            seen.add(code)
        elif not code:
            foreign += 1
    return {
        "total_crossings": len(crossings),
        "unique_territories": len(territory_codes),
        "territory_codes": territory_codes,
        "foreign_crossings": foreign,
    }


def filter_french_territories(crossings: list[dict]) -> list[dict]:
    """Keep only crossings mapped onto a territory in territories.json."""
    return [c for c in crossings if c.get("territory_code") is not None]
