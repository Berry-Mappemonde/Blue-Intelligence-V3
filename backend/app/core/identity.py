"""Identity: two rules, two codes.

Same taxonomic family ("is it already there?"), two business decisions.
Do not mix them.

* ``same_site`` — merge two cards of the same *action place*
  (Projects / ports of entry). 500 m **and** a close name, or 90%
  similarity alone. One enriched card.
* ``find_building`` — overlay an official layer (SHOM / NOAA) on an
  OSM office. 250 m, **distance only**. A different name does not block
  the overlay; a close name at 400 m does not glue two offices.

Reusing ``same_site`` for harbormasters would glue offices too
far, or refuse a legitimate overlay. This is not an Overpass
unification project (already shared). It is a guardrail.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.core.dedup import is_duplicate
from app.core.geo import haversine_km

OVERLAY_RADIUS_KM = 0.25
_KM_PER_DEG = 111.32
_BBOX_SLACK = 1.05
_EPS_KM = 1e-6  # 1 mm: Haversine and destination_point do not land bit-exact


@dataclass(frozen=True)
class OverlayHit:
    """Overlay: the nearest office, and at what distance."""

    doc: dict
    distance_km: float


def same_site(
    doc_a: dict,
    doc_b: dict,
    lat_key: str = "lat",
    lon_key: str = "lon",
    title_key: str = "title",
) -> bool:
    """Same action site (Projects / PoE). Not a building overlay."""
    return is_duplicate(
        doc_a, doc_b, lat_key=lat_key, lon_key=lon_key, title_key=title_key,
    )


def building_radius_km() -> float:
    """OSM↔SHOM↔NOAA overlay radius. Catalogue ``capitaineries.merge_km``."""
    from app.core.run_rules import get_rule
    return float(get_rule("capitaineries.merge_km", OVERLAY_RADIUS_KM))


def coords_of(doc: dict, lat_key: str = "lat", lon_key: str = "lon"):
    """(lat, lon) or None. No NaN, no disguised empty string."""
    try:
        lat, lon = doc.get(lat_key), doc.get(lon_key)
        if lat is None or lon is None or lat == "" or lon == "":
            return None
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError, AttributeError):
        return None
    if not math.isfinite(lat_f) or not math.isfinite(lon_f):
        return None
    if not -90.0 <= lat_f <= 90.0:
        return None
    return lat_f, lon_f


def _lon_delta_deg(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _in_bbox(lat: float, lon: float, plat: float, plon: float, radius_km: float) -> bool:
    """Degree prefilter: avoid a haversine over the whole world dump."""
    reach = radius_km * _BBOX_SLACK
    if abs(plat - lat) * _KM_PER_DEG > reach:
        return False
    km_lon = _KM_PER_DEG * max(abs(math.cos(math.radians(lat))), 0.05)
    return _lon_delta_deg(plon, lon) * km_lon <= reach


def find_building(
    lat: float,
    lon: float,
    pts: Sequence[dict] | Iterable[dict],
    radius_km: float | None = None,
    lat_key: str = "lat",
    lon_key: str = "lon",
) -> OverlayHit | None:
    """Nearest office within ≤ ``radius_km`` (default 250 m).

    Distance only: the name does not enter the decision. On a strict
    tie, the first document in the list wins (stable, not last-wins).
    """
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lat_f) or not math.isfinite(lon_f):
        return None
    radius = float(radius_km if radius_km is not None else building_radius_km())
    if radius < 0:
        return None
    best: OverlayHit | None = None
    for doc in pts or ():
        xy = coords_of(doc, lat_key=lat_key, lon_key=lon_key)
        if xy is None:
            continue
        plat, plon = xy
        if not _in_bbox(lat_f, lon_f, plat, plon, radius):
            continue
        dist = haversine_km(lat_f, lon_f, plat, plon)
        if dist > radius + _EPS_KM:
            continue
        if best is None or dist < best.distance_km:
            best = OverlayHit(doc=doc, distance_km=dist)
    return best
