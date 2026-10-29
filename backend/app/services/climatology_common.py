"""Shared Climatology-mode contracts (kind, periods, land, units).

Snapshots live in ``backend/data/climatology/`` and are generated off
the VPS (Mac). Here we serve them; we do not invent a wind / Hs / current.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

KIND = "climatology"

_BACKEND_DIR = Path(__file__).resolve().parents[2]
CLIMATOLOGY_DIR = _BACKEND_DIR / "data" / "climatology"

try:
    from app.core.run_rules import catalog_default as _catalog_default
except Exception:  # pragma: no cover — import NAVIGUIDE without Mongo
    _catalog_default = None

_RULE_FALLBACKS = {
    "climatology.wind_calm_kn": 3,
    "climatology.wind_gale_kn": 34,
    "climatology.wind_min_sector_pct": 2.5,
    "climatology.wave_nogo_m": 2.5,
    "climatology.current_min_kn": 0.15,
    "climatology.current_depth_m": 0.5,
    "climatology.cyclone_dayrange": 21,
    "climatology.cyclone_radius_nm": 120,
    "climatology.cyclone_min_kn": 34,
    "climatology.avoid_cyclone_tracks": True,
}

# Named periods — each product has its own. The point's `period` field
# is a summary, not a single truth (see plan §15).
PERIOD_SUMMARY = "1980-2020"
WIND_PERIOD = "1994-2020"
WAVE_PERIOD = "1993-2019"
CURRENT_PERIOD = "1993-2016"
CYCLONE_PERIOD = "1980-present"

PROVENANCE = {
    "wind": "CMEMS WIND_GLO_PHY_L4_MY_012_006 · cmems_obs-wind_glo_phy_my_l4_0.25deg_PT1H",
    "wind_average": (
        "CMEMS WIND_GLO_PHY_CLIMATE_L4_MY_012_003 · "
        "cmems_obs-wind_glo_phy_my_l4_P1M · AVERAGE only (not a rose)"
    ),
    "wave": "WAVERYS GLOBAL_MULTIYEAR_WAV_001_032",
    "current": "GLORYS12 GLOBAL_MULTIYEAR_PHY_001_030 climatology_P1M-m",
    "cyclone": "IBTrACS v04r01 since1980 · NOAA NCEI",
}

DOI = {
    "wind": "10.48670/moi-00183",
    "wave": "10.48670/moi-00022",
    "current": "10.48670/moi-00021",
}

SOURCE_IDS = {
    "wind": "WIND_GLO_PHY_L4_MY_012_006",
    "wave": "GLOBAL_MULTIYEAR_WAV_001_032",
    "current": "GLOBAL_MULTIYEAR_PHY_001_030",
    "cyclone": "IBTrACS_v04r01",
}

CMEMS_CREDIT = (
    "Generated using E.U. Copernicus Marine Service Information"
)
IBTRACS_CREDIT = "IBTrACS v04r01, NOAA NCEI"
LICENSE_CMEMS = (
    f"{CMEMS_CREDIT}. Copernicus Marine Service licence. "
    "Not for navigation."
)
LICENSE_IBTRACS = (
    f"{IBTRACS_CREDIT}. Redistribution unrestricted (ERDDAP). "
    "Not for navigation."
)

MS_TO_KN = 1.94384
SECTORS = 8
SECTOR_DEG = 45.0

# Land boxes (fallback if the packaged mask cannot be read).
# Recette : Sahara et Andes doivent tomber dedans ; 15°N 25°W, 40°S, 26°N 80°W hors.
_LAND_BOXES = [
    (30, 72, -130, -60),     # Interior North America
    (14, 32, -115, -88),     # Mexique / SW US
    (7, 18, -92, -77),       # Central America
    (-50, 10, -75, -40),     # Interior South America (Andes)
    (36, 71, -10, 40),       # Interior Europe
    (12, 37, -17, 50),       # Sahara / Afrique N + Sahel
    (-34, 15, 10, 42),       # Interior Africa
    (12, 32, 35, 60),        # Arabie
    (8, 35, 68, 97),         # Inde
    (-8, 28, 95, 122),       # SE Asia
    (18, 54, 73, 135),       # Chine / Asie centrale
    (-38, -12, 114, 152),    # Interior Australia
    (-90, -63, -180, 180),   # Antarctique
    (60, 84, -75, -12),      # Groenland
    (-25, -12, 43, 50),      # Madagascar
]


class LoopPauser:
    """Release the GIL during a long pure-Python walk so ``/api/`` stays up."""

    def __init__(self, interval: float = 0.2):
        self.interval = interval
        self._t = time.perf_counter()

    def tick(self) -> None:
        now = time.perf_counter()
        if now - self._t >= self.interval:
            time.sleep(0)
            self._t = time.perf_counter()


def loop_pauser() -> LoopPauser:
    return LoopPauser()


def climatology_dir() -> Path:
    return Path(CLIMATOLOGY_DIR)


def rule(rule_id: str, fallback: Any = None) -> Any:
    if _catalog_default is not None:
        try:
            return _catalog_default(rule_id, fallback)
        except Exception:
            pass
    if fallback is not None:
        return fallback
    return _RULE_FALLBACKS.get(rule_id)


def parse_month(month: int | str) -> int:
    try:
        m = int(month)
    except (TypeError, ValueError) as exc:
        raise ValueError("month must be an integer 1–12") from exc
    if m < 1 or m > 12:
        raise ValueError("month must be an integer 1–12")
    return m


def wrap_lon(lon: float) -> float:
    return ((float(lon) + 180.0) % 360.0) - 180.0


# Web Mercator latitude limit used by Leaflet; we clamp displayed tiles to ±85°.
TILE_MAX_LAT = 85.0


def tile_step_deg(z: int) -> float:
    """Native 1° from z=4; coarser farther out. z ≤ 2 → 4°, z = 3 → 2°, else 1°."""
    return max(1.0, min(4.0, 16.0 / (2 ** int(z))))


def tile_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """XYZ Web Mercator (Leaflet). Returns ``(west, south, east, north)`` in degrees."""
    z = int(z)
    x = int(x)
    y = int(y)
    n = 2.0 ** max(0, z)

    def _merc_lat(ty: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * ty / n))))

    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = max(-TILE_MAX_LAT, min(TILE_MAX_LAT, _merc_lat(float(y))))
    south = max(-TILE_MAX_LAT, min(TILE_MAX_LAT, _merc_lat(float(y + 1))))
    if south > north:
        south, north = north, south
    return (west, south, east, north)


def grid_step(bundle: dict, spacing_deg: float) -> int:
    """Index stride so the walk matches the historical ``spacing_deg`` geojson."""
    lats = bundle["lats"]
    res = abs(float(lats[1] - lats[0])) if len(lats) > 1 else 0.5
    return max(1, int(round(float(spacing_deg) / max(0.25, res))))


def _lon_in_span(lon: float, west: float, east: float) -> bool:
    lon_w = wrap_lon(lon)
    west_w = wrap_lon(west)
    east_w = wrap_lon(east)
    if west <= east and abs(east - west) < 360.0 - 1e-9:
        if west_w <= east_w:
            return west_w <= lon_w <= east_w
        return lon_w >= west_w or lon_w <= east_w
    return lon_w >= west_w or lon_w <= east_w


def iter_cells(bundle: dict, bbox: tuple[float, float, float, float] | None, step: int):
    """Yield ``(i, j, lat, lon)`` for sea cells. ``bbox`` is west/south/east/north or None (world)."""
    lats = bundle["lats"]
    lons = bundle["lons"]
    stride = max(1, int(step))
    west = south = east = north = None
    if bbox is not None:
        west, south, east, north = (float(v) for v in bbox)
    rows: list[tuple[int, float]] = []
    for i in range(0, len(lats), stride):
        lat = float(lats[i])
        if south is not None and (lat < south or lat > north):
            continue
        rows.append((i, lat))
    cols: list[tuple[int, float]] = []
    for j in range(0, len(lons), stride):
        lon = float(lons[j])
        if west is not None and not _lon_in_span(lon, west, east):
            continue
        cols.append((j, lon))
    if not rows or not cols:
        return
    flags = None
    try:
        from app.services.land_mask import land_flags
        flags = land_flags([lat for _, lat in rows], [lon for _, lon in cols])
    except Exception:
        flags = None
    for r, (i, lat) in enumerate(rows):
        for c, (j, lon) in enumerate(cols):
            if flags is not None:
                if flags[r, c]:
                    continue
            elif is_land(lat, lon):
                continue
            yield i, j, lat, lon


def is_land(lat: float, lon: float) -> bool:
    """True on land. Never invent a wind / Hs / current there."""
    if not (-90.0 <= lat <= 90.0):
        return True
    lon = wrap_lon(lon)
    try:
        from app.services.land_mask import is_land as _mask_is_land
        return bool(_mask_is_land(float(lat), float(lon)))
    except Exception:
        pass
    for la, lb, loa, lob in _LAND_BOXES:
        if la <= lat <= lb and loa <= lon <= lob:
            return True
    return False


def wind_from_uv(u_ms: float, v_ms: float) -> tuple[float, float]:
    """Direction the wind comes from (met. conv.) + speed in kn.

    ``atan2(-u, -v)`` — already the ``getWind.py`` convention.
    """
    spd_kn = math.hypot(u_ms, v_ms) * MS_TO_KN
    coming_from = (math.degrees(math.atan2(-u_ms, -v_ms)) + 360.0) % 360.0
    return spd_kn, coming_from


def current_to_uv(u_ms: float, v_ms: float) -> tuple[float, float]:
    """Direction it sets toward + speed in kn.

    ``atan2(u, v)`` — already the ``getCurrent.py`` convention.
    """
    spd_kn = math.hypot(u_ms, v_ms) * MS_TO_KN
    going_to = (math.degrees(math.atan2(u_ms, v_ms)) + 360.0) % 360.0
    return spd_kn, going_to


def sector_index(dir_from_deg: float, n: int = SECTORS) -> int:
    step = 360.0 / n
    return int((float(dir_from_deg) + step / 2.0) % 360.0 // step)


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(wrap_lon(lon2 - lon1))
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r_nm * math.asin(min(1.0, math.sqrt(a)))


def point_to_segment_nm(
    lat: float, lon: float, lat1: float, lon1: float, lat2: float, lon2: float,
) -> float:
    """Approx. distance from a point to a segment (samples along the GC)."""
    best = min(
        haversine_nm(lat, lon, lat1, lon1),
        haversine_nm(lat, lon, lat2, lon2),
    )
    dlon = wrap_lon(lon2 - lon1)
    for i in range(1, 8):
        t = i / 8.0
        plat = lat1 + t * (lat2 - lat1)
        plon = wrap_lon(lon1 + t * dlon)
        best = min(best, haversine_nm(lat, lon, plat, plon))
    return best


def empty_blocks() -> dict:
    return {
        "wind_atlas": None,
        "wave": None,
        "current": None,
        "cyclone": {"tracks_in_month": 0, "crossings_if_leg": None},
    }


def _sidecar_stat(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    stat = data.get("stat")
    return str(stat) if stat else None


def snapshot_status(month: int | None = None) -> dict:
    """Presence and ``stat`` of the displayed month (January if ``month`` is omitted)."""
    root = climatology_dir()
    wind_dir = root / "wind"
    wave_dir = root / "wave"
    current_dir = root / "current"
    cyc = root / "cyclones" / "ibtracs_since1980.json"
    try:
        mm = 1 if month is None else int(month)
    except (TypeError, ValueError):
        mm = 1
    if mm < 1 or mm > 12:
        mm = 1
    wind_present = (wind_dir / f"wind-{mm:02d}.npz").is_file()
    wave_present = (wave_dir / f"wave-{mm:02d}.npz").is_file()
    wind_stat = _sidecar_stat(wind_dir / f"wind-{mm:02d}.atlas.json")
    if wind_present and not wind_stat:
        wind_stat = "rose"
    wave_stat = _sidecar_stat(wave_dir / f"wave-{mm:02d}.json")
    return {
        "wind": wind_present,
        "wave": wave_present,
        "current": (current_dir / f"current-{mm:02d}.npz").is_file(),
        "cyclones": cyc.is_file(),
        "wind_stat": wind_stat,
        "wave_stat": wave_stat,
    }


def product_meta(product: str, month: int | None = None) -> dict:
    periods = {
        "wind": WIND_PERIOD,
        "wave": WAVE_PERIOD,
        "current": CURRENT_PERIOD,
        "cyclone": CYCLONE_PERIOD,
    }
    dois = {
        "wind": DOI["wind"],
        "wave": DOI["wave"],
        "current": DOI["current"],
        "cyclone": None,
    }
    snaps = snapshot_status(month)
    provenance = PROVENANCE[product]
    source_id = SOURCE_IDS[product]
    doi = dois[product]
    if product == "wind" and snaps.get("wind_stat") == "average":
        provenance = PROVENANCE["wind_average"]
        source_id = "WIND_GLO_PHY_CLIMATE_L4_MY_012_003"
        doi = None
    return {
        "kind": KIND,
        "source_id": source_id,
        "provenance": provenance,
        "period": periods[product],
        "doi": doi,
        "snapshot_present": snaps["cyclones" if product == "cyclone" else product],
        "stat": snaps.get("wind_stat" if product == "wind" else (
            "wave_stat" if product == "wave" else None
        )),
    }
