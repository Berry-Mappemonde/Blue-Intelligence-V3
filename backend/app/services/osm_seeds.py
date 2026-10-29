"""OSM seeds for the PoE union — Overpass + Mongo cache + VLIZ attachment.

OSM has no single "port" object. Distinct layers:

  1. Control / PoE (wiki + Taginfo, not Key:port_of_entry which is 404):
     government=customs, amenity=customs (deprecated), barrier=border_control,
     government=border_control|immigration, seamark:building:function=customs.
     OpenSeaMap/Harbour: port_of_entry=* is an almanac field (59 yes).

  2. Port infrastructure (Harbour / CATHAF):
     harbour=yes, industrial=port, landuse=harbour|port, water=harbour,
     commercial / uncategorized HBRFAC.

  3. Pleasure craft (P seed, not a PoE):
     leisure=marina / CATHAF marina* only if a customs office, a
     border_control or a port_of_entry is within ≤ 800 m. We do not
     take all 31,792 marinas.

The old query (harbour=yes ∪ seamark:type=harbour ∪ industrial=port)
flooded the union with marinas: 21,344 seamark:type=harbour also carry
leisure=marina. v2 wrongly excluded customs and border_control (offices ≠ haven).
v3 reintroduces them as PoE seeds; airports are filtered; VLIZ
attachment (in_eez / coastal_land) drops inland land posts.
v4: marinas near a control = P seeds (Overpass around.ctrl:800 query).

GET /seeds/union reads the cache. Overpass refresh is explicit
(POST /seeds/osm/refresh or scripts/refresh_osm_seeds.py).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from shapely.geometry import shape
from shapely.prepared import prep

from app.core.dedup import normalize_name
from app.core.geo import classify_poe_point, haversine_km

logger = logging.getLogger(__name__)

TAGINFO_API = "https://taginfo.openstreetmap.org/api/4/tag/stats"
OVERPASS_ENDPOINTS = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "BlueIntelligence/1.0 (OSM port-seed cache)"

CACHE_COLLECTION = "osm_port_seeds"
CACHE_META = "osm_port_seeds_meta"
CACHE_SCHEMA = "port-candidates-v3"
OSM_SOURCE = "osm"

# Taginfo snapshot measured 2026-09-05 (data_until). Live overlay in taginfo_snapshot.
TAGINFO_DATA_UNTIL = "2026-09-05T00:59:10Z"

WIKI = {
    "harbour": "https://wiki.openstreetmap.org/wiki/Harbour",
    "key_harbour": "https://wiki.openstreetmap.org/wiki/Key:harbour",
    "landuse_port": "https://wiki.openstreetmap.org/wiki/Tag:landuse%3Dport",
    "industrial_port": "https://wiki.openstreetmap.org/wiki/Tag:industrial%3Dport",
    "landuse_harbour": "https://wiki.openstreetmap.org/wiki/Tag:landuse%3Dharbour",
    "water_harbour": "https://wiki.openstreetmap.org/wiki/Tag:water%3Dharbour",
    "marina": "https://wiki.openstreetmap.org/wiki/Tag:leisure%3Dmarina",
    "ferry": "https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dferry_terminal",
    "harbours": "https://wiki.openstreetmap.org/wiki/Seamarks/Harbours",
    "openseamap_harbours": "https://wiki.openstreetmap.org/wiki/OpenSeaMap/Harbours",
    "cathaf": "https://wiki.openstreetmap.org/wiki/Key:seamark:harbour:category",
    "seamark_objects": "https://wiki.openstreetmap.org/wiki/OpenSeaMap/Seamark_Objects",
    "key_port": "https://wiki.openstreetmap.org/wiki/Key:port",
    "government": "https://wiki.openstreetmap.org/wiki/Key:government",
    "government_customs": "https://wiki.openstreetmap.org/wiki/Tag:government%3Dcustoms",
    "amenity_customs": "https://wiki.openstreetmap.org/wiki/Tag:amenity%3Dcustoms",
    "barrier_border": "https://wiki.openstreetmap.org/wiki/Tag:barrier%3Dborder_control",
    "gov_border": "https://wiki.openstreetmap.org/wiki/Tag:government%3Dborder_control",
    "seamark_buildings": "https://wiki.openstreetmap.org/wiki/Seamarks/Buildings",
}

# Commercial CATHAF (IHO / OpenSeaMap). marina* is left out on purpose.
COMMERCIAL_CATHAF = frozenset({
    "roro", "ferry", "fishing", "naval", "container", "cargo", "passenger",
    "tanker", "bulk", "shipyard", "service_repair", "offshore_support",
    "port_support", "port_support_base", "quarantine", "timber", "seaplane",
    "harbour", "commercial", "mixed", "lay_up",
})
MARINA_CATHAF = frozenset({"marina", "marina_no_facilities"})
HARBOUR_COMMERCIAL_VALUES = frozenset({
    "fishing", "passenger", "terminal", "customs", "tanker",
})
POE_YES = frozenset({"yes", "all"})
# CDC: marina = P seed only if a control is within ≤ 800 m.
MARINA_CONTROL_RADIUS_M = 800
MARINA_CONTROL_RADIUS_KM = MARINA_CONTROL_RADIUS_M / 1000.0

# POSIX Overpass regex: a commercial category in an "a;b" list.
_CATHAF_ALT = "|".join(sorted(COMMERCIAL_CATHAF))
COMMERCIAL_CATHAF_REGEX = rf"(^|;)({_CATHAF_ALT})(;|$)"
_MARINA_CATHAF_ALT = "|".join(sorted(MARINA_CATHAF))
MARINA_CATHAF_REGEX = rf"(^|;)({_MARINA_CATHAF_ALT})(;|$)"
HARBOUR_COMMERCIAL_REGEX = r"^(fishing|passenger|terminal|customs|tanker)$"
# Listes « customs;migration » (Taginfo : government=customs;migration;military = 13).
GOVERNMENT_CONTROL_REGEX = r"(^|;)(customs|border_control|immigration)(;|$)"
CONTROL_GOVERNMENT = frozenset({"customs", "border_control", "immigration"})
CONTROL_AMENITY = frozenset({"customs", "border_control"})
CONTROL_BARRIER = frozenset({"border_control", "customs_control"})
_AIRPORT_NAME_RE = re.compile(
    r"\b(airport|a[ée]roport|aeropuerto|flughafen|aeroporto|"
    r"aerodrome|a[ée]rodrome)\b",
    re.I,
)

# Overpass clauses: documented ports + controls / customs (PoE).
OSM_SEED_CLAUSES: tuple[tuple, ...] = (
    ("eq", "port_of_entry", "yes"),
    ("eq", "port_of_entry", "all"),
    ("eq", "harbour", "yes"),
    ("eq", "industrial", "port"),
    ("eq", "landuse", "harbour"),
    ("eq", "landuse", "port"),
    ("eq", "water", "harbour"),
    ("eq", "seamark:type", "harbour_basin"),
    ("eq", "port:type", "seaport"),
    ("key", "port"),
    ("regex", "harbour", HARBOUR_COMMERCIAL_REGEX),
    ("regex", "seamark:harbour:category", COMMERCIAL_CATHAF_REGEX),
    ("and", (
        ("eq", "seamark:type", "harbour"),
        ("not_key", "seamark:harbour:category"),
    )),
    ("regex", "government", GOVERNMENT_CONTROL_REGEX),
    ("eq", "amenity", "customs"),
    ("eq", "amenity", "border_control"),
    ("eq", "office", "customs"),
    ("eq", "seamark:building:function", "customs"),
    ("eq", "barrier", "border_control"),
    ("eq", "barrier", "customs_control"),
    ("eq", "landuse", "border_control"),
)

# Control subset: Overpass `.ctrl` set for around:800 (not the 32k marinas).
OSM_CONTROL_CLAUSES: tuple[tuple, ...] = (
    ("eq", "port_of_entry", "yes"),
    ("eq", "port_of_entry", "all"),
    ("regex", "government", GOVERNMENT_CONTROL_REGEX),
    ("eq", "amenity", "customs"),
    ("eq", "amenity", "border_control"),
    ("eq", "office", "customs"),
    ("eq", "seamark:building:function", "customs"),
    ("eq", "barrier", "border_control"),
    ("eq", "barrier", "customs_control"),
    ("eq", "landuse", "border_control"),
)
MARINA_NEAR_CONTROL_CLAUSES: tuple[tuple, ...] = (
    ("eq", "leisure", "marina"),
    ("eq", "harbour", "marina"),
    ("regex", "seamark:harbour:category", MARINA_CATHAF_REGEX),
)

# Equalities still useful for osm_tag_list / tests. This is no longer THE query.
OSM_POE_TAGS = (
    ("port_of_entry", "yes"),
    ("harbour", "yes"),
    ("industrial", "port"),
    ("landuse", "harbour"),
    ("landuse", "port"),
    ("water", "harbour"),
    ("seamark:type", "harbour_basin"),
    ("port:type", "seaport"),
    ("government", "customs"),
    ("government", "border_control"),
    ("government", "immigration"),
    ("amenity", "customs"),
    ("amenity", "border_control"),
    ("office", "customs"),
    ("seamark:building:function", "customs"),
    ("barrier", "border_control"),
    ("barrier", "customs_control"),
    ("landuse", "border_control"),
    ("harbour", "customs"),
)
OSM_EXCLUDED_TAGS = (
    ("leisure", "marina"),
    ("amenity", "ferry_terminal"),
    ("seamark:type", "berth"),
    ("waterway", "dock"),
)

# Documented catalog: role + Taginfo count from 2026-09-05.
TAGINFO_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "tag": "port_of_entry=yes", "key": "port_of_entry", "value": "yes",
        "role": "poe_explicit", "seed": True, "measured": 59,
        "wiki": WIKI["harbours"],
        "note": "Seul tag qui signifie PoE (douane). Key:port_of_entry = 404 wiki.",
    },
    {
        "tag": "port_of_entry=no", "key": "port_of_entry", "value": "no",
        "role": "poe_explicit", "seed": False, "measured": 411,
        "wiki": WIKI["harbours"],
        "note": "Havre explicitement hors clearance. 411 no vs 59 yes.",
    },
    {
        "tag": "harbour=yes", "key": "harbour", "value": "yes",
        "role": "harbour_facility", "seed": True, "measured": 5115,
        "wiki": WIKI["harbour"],
        "note": "Tag havre OSM. Souvent redondant avec leisure=marina (Key:harbour).",
    },
    {
        "tag": "industrial=port", "key": "industrial", "value": "port",
        "role": "commercial_harbour", "seed": True, "measured": 5295,
        "wiki": WIKI["landuse_port"],
        "note": "Tagging recommandé : landuse=industrial + industrial=port.",
    },
    {
        "tag": "landuse=harbour", "key": "landuse", "value": "harbour",
        "role": "harbour_facility", "seed": True, "measured": 2561,
        "wiki": WIKI["harbour"],
        "note": "Emprise du havre. Manquait de l'ancienne requête.",
    },
    {
        "tag": "landuse=port", "key": "landuse", "value": "port",
        "role": "commercial_harbour", "seed": True, "measured": 2,
        "wiki": WIKI["landuse_port"],
        "note": "Déprécié (2 objets). Préférer industrial=port.",
    },
    {
        "tag": "water=harbour", "key": "water", "value": "harbour",
        "role": "harbour_basin", "seed": True, "measured": 1425,
        "wiki": WIKI["water_harbour"],
        "note": "Plan d'eau du bassin, pas le terminal. Gardé si nommé.",
    },
    {
        "tag": "seamark:type=harbour", "key": "seamark:type", "value": "harbour",
        "role": "openseamap_harbour", "seed": False, "measured": 25070,
        "wiki": WIKI["harbours"],
        "note": "HBRFAC IHO. 21 344 aussi leisure=marina — ne plus tout prendre.",
    },
    {
        "tag": "seamark:type=harbour_basin", "key": "seamark:type",
        "value": "harbour_basin",
        "role": "harbour_basin", "seed": True, "measured": 262,
        "wiki": WIKI["harbours"],
        "note": "hrbbsn : bassin à quai, pas un berth.",
    },
    {
        "tag": "seamark:harbour:category=marina", "key": "seamark:harbour:category",
        "value": "marina",
        "role": "marina_only", "seed": False, "measured": 20770,
        "wiki": WIKI["cathaf"],
        "note": "86 % des CATHAF. Plaisance, pas un candidat port.",
    },
    {
        "tag": "seamark:harbour:category=marina_no_facilities",
        "key": "seamark:harbour:category", "value": "marina_no_facilities",
        "role": "marina_only", "seed": False, "measured": 1084,
        "wiki": WIKI["cathaf"],
        "note": "Mouillage plaisance sans services.",
    },
    {
        "tag": "seamark:harbour:category=fishing", "key": "seamark:harbour:category",
        "value": "fishing",
        "role": "commercial_harbour", "seed": True, "measured": 1145,
        "wiki": WIKI["cathaf"], "note": "CATHAF fishing.",
    },
    {
        "tag": "seamark:harbour:category=ferry", "key": "seamark:harbour:category",
        "value": "ferry",
        "role": "commercial_harbour", "seed": True, "measured": 159,
        "wiki": WIKI["cathaf"], "note": "CATHAF ferry terminal.",
    },
    {
        "tag": "seamark:harbour:category=container", "key": "seamark:harbour:category",
        "value": "container",
        "role": "commercial_harbour", "seed": True, "measured": 136,
        "wiki": WIKI["cathaf"], "note": "CATHAF container.",
    },
    {
        "tag": "seamark:harbour:category=cargo", "key": "seamark:harbour:category",
        "value": "cargo",
        "role": "commercial_harbour", "seed": True, "measured": 125,
        "wiki": WIKI["cathaf"], "note": "CATHAF general cargo.",
    },
    {
        "tag": "seamark:harbour:category=passenger", "key": "seamark:harbour:category",
        "value": "passenger",
        "role": "commercial_harbour", "seed": True, "measured": 112,
        "wiki": WIKI["cathaf"], "note": "CATHAF passenger.",
    },
    {
        "tag": "seamark:harbour:category=tanker", "key": "seamark:harbour:category",
        "value": "tanker",
        "role": "commercial_harbour", "seed": True, "measured": 87,
        "wiki": WIKI["cathaf"], "note": "CATHAF tanker.",
    },
    {
        "tag": "seamark:harbour:category=bulk", "key": "seamark:harbour:category",
        "value": "bulk",
        "role": "commercial_harbour", "seed": True, "measured": 82,
        "wiki": WIKI["cathaf"], "note": "CATHAF bulk.",
    },
    {
        "tag": "seamark:harbour:category=naval", "key": "seamark:harbour:category",
        "value": "naval",
        "role": "commercial_harbour", "seed": True, "measured": 57,
        "wiki": WIKI["cathaf"], "note": "CATHAF naval base.",
    },
    {
        "tag": "seamark:harbour:category=roro", "key": "seamark:harbour:category",
        "value": "roro",
        "role": "commercial_harbour", "seed": True, "measured": 32,
        "wiki": WIKI["cathaf"], "note": "CATHAF RoRo.",
    },
    {
        "tag": "seamark:harbour:category=shipyard", "key": "seamark:harbour:category",
        "value": "shipyard",
        "role": "commercial_harbour", "seed": True, "measured": 86,
        "wiki": WIKI["cathaf"], "note": "CATHAF shipyard.",
    },
    {
        "tag": "leisure=marina", "key": "leisure", "value": "marina",
        "role": "marina_only", "seed": False, "measured": 31792,
        "wiki": WIKI["marina"],
        "note": (
            "Wiki : plaisance. Graine P seulement si douane / border / "
            "port_of_entry à ≤ 800 m — pas les 31 792."
        ),
    },
    {
        "tag": "amenity=ferry_terminal", "key": "amenity", "value": "ferry_terminal",
        "role": "not_port", "seed": False, "measured": 37308,
        "wiki": WIKI["ferry"],
        "note": "Embarcadère, pas un port. Souvent transport public terrestre.",
    },
    {
        "tag": "seamark:type=berth", "key": "seamark:type", "value": "berth",
        "role": "too_granular", "seed": False, "measured": 7958,
        "wiki": WIKI["harbours"],
        "note": "Poste à quai nommé/numéroté — trop fin pour une graine PoE.",
    },
    {
        "tag": "waterway=dock", "key": "waterway", "value": "dock",
        "role": "too_granular", "seed": False, "measured": 7093,
        "wiki": WIKI["harbour"],
        "note": "Beaucoup de docks fluviaux inland. Pas une graine.",
    },
    {
        "tag": "government=customs", "key": "government", "value": "customs",
        "role": "not_port", "seed": False, "measured": 2475,
        "wiki": "https://wiki.openstreetmap.org/wiki/Tag:government%3Dcustoms",
        "note": "Bureau, pas un havre. Signal de validation autour d'un PoE.",
    },
    {
        "tag": "barrier=border_control", "key": "barrier", "value": "border_control",
        "role": "not_port", "seed": False, "measured": 9560,
        "wiki": "https://wiki.openstreetmap.org/wiki/Tag:barrier%3Dborder_control",
        "note": "Frontières terrestres + aéroports. Pas une graine port.",
    },
    {
        "tag": "port=cargo", "key": "port", "value": "cargo",
        "role": "commercial_harbour", "seed": True, "measured": 536,
        "wiki": WIKI["key_port"], "note": "Qualificatif CATHAC sur une emprise port.",
    },
    {
        "tag": "port=fishing", "key": "port", "value": "fishing",
        "role": "commercial_harbour", "seed": True, "measured": 362,
        "wiki": WIKI["key_port"], "note": "Qualificatif fishing.",
    },
    {
        "tag": "port=roro", "key": "port", "value": "roro",
        "role": "commercial_harbour", "seed": True, "measured": 121,
        "wiki": WIKI["key_port"], "note": "Qualificatif RoRo.",
    },
    {
        "tag": "port:type=seaport", "key": "port:type", "value": "seaport",
        "role": "commercial_harbour", "seed": True, "measured": 160,
        "wiki": WIKI["landuse_port"], "note": "Type seaport (vs inland_port / dryport).",
    },
)

# Bay of Biscay — Console Test tile (WORLD_TILES[0] is too large).
TEST_TILE: tuple[float, float, float, float] = (46.0, -2.0, 48.5, 0.5)

# (south, west, north, east) — small enough for public Overpass; auto-split otherwise.
WORLD_TILES: tuple[tuple[float, float, float, float], ...] = (
    (-60.0, -180.0, -15.0, -90.0),
    (-60.0, -90.0, -15.0, 0.0),
    (-60.0, 0.0, -15.0, 90.0),
    (-60.0, 90.0, -15.0, 180.0),
    (-15.0, -180.0, 20.0, -90.0),
    (-15.0, -90.0, 20.0, 0.0),
    (-15.0, 0.0, 20.0, 90.0),
    (-15.0, 90.0, 20.0, 180.0),
    (20.0, -180.0, 50.0, -90.0),
    (20.0, -90.0, 50.0, 0.0),
    (20.0, 0.0, 50.0, 90.0),
    (20.0, 90.0, 50.0, 180.0),
    (50.0, -180.0, 85.0, -90.0),
    (50.0, -90.0, 85.0, 0.0),
    (50.0, 0.0, 85.0, 90.0),
    (50.0, 90.0, 85.0, 180.0),
)

MIN_TILE_DEG = 8.0


def split_categories(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {part.strip().lower() for part in raw.split(";") if part.strip()}


def has_commercial_category(tags: dict[str, str]) -> bool:
    return bool(split_categories(tags.get("seamark:harbour:category")) & COMMERCIAL_CATHAF)


def is_airport_like(name: str, tags: dict[str, str] | None = None) -> bool:
    """Airport / airfield: a customs office there does not anchor a marina."""
    tags = tags or {}
    if tags.get("aeroway"):
        return True
    blob = " ".join(
        str(tags.get(k) or "") for k in ("name", "name:en", "official_name")
    )
    text = f"{name or ''} {blob}".strip()
    return bool(text) and bool(_AIRPORT_NAME_RE.search(text))


def is_control_facility(tags: dict[str, str]) -> bool:
    """Customs / border / port_of_entry — proximity signal, not a haven."""
    if (tags.get("port_of_entry") or "").strip().lower() in POE_YES:
        return True
    if split_categories(tags.get("government")) & CONTROL_GOVERNMENT:
        return True
    if tags.get("amenity") in CONTROL_AMENITY:
        return True
    if tags.get("office") == "customs":
        return True
    if tags.get("seamark:building:function") == "customs":
        return True
    if tags.get("barrier") in CONTROL_BARRIER:
        return True
    if tags.get("landuse") == "border_control":
        return True
    return False


def control_kind_flags(tags: dict[str, str]) -> tuple[bool, bool]:
    """(customs, border) from the tags of the nearest control point."""
    gov = split_categories(tags.get("government"))
    customs = (
        "customs" in gov
        or tags.get("amenity") == "customs"
        or tags.get("office") == "customs"
        or tags.get("seamark:building:function") == "customs"
        or tags.get("barrier") == "customs_control"
    )
    border = (
        "border_control" in gov
        or tags.get("amenity") == "border_control"
        or tags.get("barrier") == "border_control"
        or tags.get("landuse") == "border_control"
        or "immigration" in gov
    )
    return customs, border


def nearest_control(
    lat: float, lon: float,
    controls: list[tuple[float, float, dict]],
    radius_km: float | None = None,
) -> tuple[float, dict] | None:
    """Nearest control within ≤ radius_km, else None."""
    from app.core.run_rules import get_rule
    if radius_km is None:
        radius_km = float(get_rule("formalities.marina_control_m", MARINA_CONTROL_RADIUS_M)) / 1000.0
    best: tuple[float, dict] | None = None
    for clat, clon, ctags in controls:
        try:
            dist = haversine_km(lat, lon, clat, clon)
        except (TypeError, ValueError):
            continue
        if dist <= radius_km and (best is None or dist < best[0]):
            best = (dist, ctags)
    return best


def collect_control_points(docs: list[dict]) -> list[tuple[float, float, dict]]:
    """Usable control points (except airport) from OSM extracts / cache."""
    out: list[tuple[float, float, dict]] = []
    for raw in docs or []:
        tags = {str(k): str(v) for k, v in (raw.get("tags") or {}).items()}
        if not is_control_facility(tags):
            continue
        try:
            lat = float(raw["lat"] if "lat" in raw else raw.get("center", {}).get("lat"))
            lon = float(raw["lon"] if "lon" in raw else raw.get("center", {}).get("lon"))
        except (KeyError, TypeError, ValueError):
            coords = element_coords(raw)
            if coords is None:
                continue
            lat, lon = coords
        name = element_name(tags) or str(raw.get("name") or "")
        if is_airport_like(name, tags):
            continue
        out.append((lat, lon, tags))
    return out


def is_marina_only(tags: dict[str, str]) -> bool:
    """Pleasure craft without a port / PoE signal on the same object.

    This is not a final reject: P seed if near_control (≤ 800 m).
    """
    if (tags.get("port_of_entry") or "").strip().lower() in POE_YES:
        return False
    if has_commercial_category(tags):
        return False
    if tags.get("industrial") == "port":
        return False
    if tags.get("landuse") in ("harbour", "port"):
        return False
    if tags.get("port") and tags.get("port") not in ("no", "marina"):
        return False
    if (tags.get("harbour") or "").strip().lower() in HARBOUR_COMMERCIAL_VALUES:
        return False
    if tags.get("port:type") == "seaport":
        return False
    cats = split_categories(tags.get("seamark:harbour:category"))
    marina_cat = bool(cats) and cats <= MARINA_CATHAF
    return (
        tags.get("leisure") == "marina"
        or (tags.get("harbour") or "").strip().lower() == "marina"
        or marina_cat
    )


def is_seed_candidate(tags: dict[str, str], *, near_control: bool = False) -> bool:
    """True if the OSM object is a port candidate, or a marina near a control."""
    if (tags.get("port_of_entry") or "").strip().lower() in POE_YES:
        return True
    if is_marina_only(tags):
        return bool(near_control)
    if tags.get("industrial") == "port":
        return True
    if tags.get("landuse") in ("harbour", "port"):
        return True
    if tags.get("water") == "harbour":
        return True
    if tags.get("seamark:type") == "harbour_basin":
        return True
    if tags.get("port:type") == "seaport":
        return True
    if tags.get("port") and tags.get("port") not in ("no", "marina"):
        return True
    harbour = (tags.get("harbour") or "").strip().lower()
    if harbour == "yes" or harbour in HARBOUR_COMMERCIAL_VALUES:
        return True
    if has_commercial_category(tags):
        return True
    if tags.get("seamark:type") == "harbour" and not tags.get("seamark:harbour:category"):
        return True
    return False


def osm_role(tags: dict[str, str], *, near_control: bool = False) -> str:
    if (tags.get("port_of_entry") or "").strip().lower() in POE_YES:
        return "poe_explicit"
    if is_marina_only(tags):
        return "marina_pleasure" if near_control else "marina_only"
    if (
        tags.get("industrial") == "port"
        or tags.get("landuse") == "port"
        or tags.get("port:type") == "seaport"
        or (tags.get("port") and tags.get("port") not in ("no", "marina"))
        or has_commercial_category(tags)
        or (tags.get("harbour") or "").strip().lower() in HARBOUR_COMMERCIAL_VALUES
    ):
        return "commercial_harbour"
    if tags.get("seamark:type") == "harbour_basin" or tags.get("water") == "harbour":
        return "harbour_basin"
    if tags.get("harbour") == "yes" or tags.get("landuse") == "harbour":
        return "harbour_facility"
    if tags.get("seamark:type") == "harbour":
        return "harbour_facility"
    return "not_port"


def taginfo_count(key: str, value: str) -> int | None:
    try:
        r = httpx.get(
            TAGINFO_API,
            params={"key": key, "value": value},
            timeout=20.0,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        data = r.json().get("data") or []
        all_row = next((row for row in data if row.get("type") == "all"), None)
        if all_row:
            return int(all_row.get("count") or 0)
    except Exception as exc:
        logger.warning("taginfo %s=%s: %s", key, value, exc)
    return None


def _catalog_row(entry: dict[str, Any], live: int | None) -> dict[str, Any]:
    count = live if live is not None else entry.get("measured")
    return {
        **{k: entry[k] for k in (
            "tag", "key", "value", "role", "seed", "measured", "wiki", "note",
        ) if k in entry},
        "count": count,
        "live": live,
    }


def how_many_ports(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """There is no single count of "OSM ports" — answers per layer."""
    by_tag = {r["tag"]: r.get("count") for r in rows}

    def n(tag: str) -> int | None:
        return by_tag.get(tag)

    commercial_sum = sum(
        (n(f"seamark:harbour:category={v}") or 0)
        for v in (
            "fishing", "ferry", "container", "cargo", "passenger",
            "tanker", "bulk", "naval", "roro", "shipyard",
        )
    )
    harbour_facilities = n("seamark:type=harbour")
    marina_cathaf = (n("seamark:harbour:category=marina") or 0) + (
        n("seamark:harbour:category=marina_no_facilities") or 0)
    uncategorized = None
    if harbour_facilities is not None:
        # 24,113 objects have seamark:harbour:category (a key, not a value).
        uncategorized = max(0, harbour_facilities - 24113)
    return {
        "no_single_count": True,
        "data_until": TAGINFO_DATA_UNTIL,
        "poe_explicit_yes": n("port_of_entry=yes"),
        "poe_explicit_no": n("port_of_entry=no"),
        "openseamap_harbour_facilities": harbour_facilities,
        "openseamap_marina_cathaf": marina_cathaf,
        "openseamap_uncategorized_harbour_est": uncategorized,
        "openseamap_commercial_cathaf_sum": commercial_sum,
        "harbour_yes": n("harbour=yes"),
        "industrial_port": n("industrial=port"),
        "landuse_harbour": n("landuse=harbour"),
        "water_harbour": n("water=harbour"),
        "leisure_marina": n("leisure=marina"),
        "ferry_terminal": n("amenity=ferry_terminal"),
        "old_query_problem": (
            "L'ancienne union harbour=yes ∪ seamark:type=harbour ∪ "
            "industrial=port prenait les 25 070 HBRFAC, dont ~21 344 "
            "leisure=marina (Taginfo combinations). Ce n'est pas une liste "
            "de ports."
        ),
        "best_candidate_layers": {
            "poe_or": n("port_of_entry=yes"),
            "commercial_landuse": n("industrial=port"),
            "harbour_yes_then_drop_marina": n("harbour=yes"),
            "landuse_harbour": n("landuse=harbour"),
            "commercial_cathaf_sum_with_dupes": commercial_sum,
            "uncategorized_hbrfac_est": uncategorized,
            "harbour_basin": n("seamark:type=harbour_basin"),
        },
        "unique_candidates_need_overpass": (
            "Taginfo compte par tag, pas l'union dédupliquée. Un même "
            "polygone porte souvent industrial=port + landuse=industrial "
            "+ harbour=yes. L'union unique = cache osm_port_seeds après "
            "refresh (filtre is_seed_candidate)."
        ),
    }


def taginfo_snapshot(*, live: bool = True) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for entry in TAGINFO_CATALOG:
        count = taginfo_count(entry["key"], entry["value"]) if live else None
        rows.append(_catalog_row(entry, count))
    included = {r["tag"]: r.get("count") for r in rows if r.get("seed")}
    excluded = {r["tag"]: r.get("count") for r in rows if not r.get("seed")}
    return {
        "data_until": TAGINFO_DATA_UNTIL,
        "wiki": WIKI,
        "catalog": rows,
        "included_tags": included,
        "excluded_tags": excluded,
        "how_many_ports": how_many_ports(rows),
        "seed_clauses": [list(c) if c[0] != "and" else ["and", [list(p) for p in c[1]]]
                         for c in OSM_SEED_CLAUSES],
        "note": (
            "Comptes Taginfo mondiaux (tous types OSM). Recouvrement : un "
            "objet peut porter industrial=port ET harbour=yes. Le cache "
            "déduplique par osm_id. leisure=marina n'est une graine P que "
            "si douane / border / port_of_entry à ≤ 800 m (around.ctrl). "
            "amenity=ferry_terminal n'est pas une graine."
        ),
    }


def element_coords(el: dict[str, Any]) -> tuple[float, float] | None:
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    center = el.get("center") or {}
    if "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None


def element_name(tags: dict[str, str]) -> str:
    for key in ("name:en", "name", "official_name", "alt_name", "loc_name",
                "seamark:name", "seamark:harbour:name"):
        raw = (tags.get(key) or "").strip()
        if raw:
            return raw[:200]
    return ""


def osm_tag_list(tags: dict[str, str]) -> list[str]:
    """Relevant tags present — not only the 3 old tags."""
    out: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        if item not in seen:
            seen.add(item)
            out.append(item)

    poe = (tags.get("port_of_entry") or "").strip()
    if poe:
        add(f"port_of_entry={poe}")
    for key, value in OSM_POE_TAGS:
        if key == "port_of_entry":
            continue
        if tags.get(key) == value:
            add(f"{key}={value}")
    if tags.get("seamark:type") == "harbour":
        add("seamark:type=harbour")
    cat = tags.get("seamark:harbour:category")
    if cat:
        add(f"seamark:harbour:category={cat}")
    if tags.get("port"):
        add(f"port={tags['port']}")
    if tags.get("leisure") == "marina":
        add("leisure=marina")
    if tags.get("amenity") == "ferry_terminal":
        add("amenity=ferry_terminal")
    harbour = (tags.get("harbour") or "").strip()
    if harbour and harbour != "yes":
        add(f"harbour={harbour}")
    return out


def osm_confidence(name: str, tags: dict[str, str], in_eez: bool,
                   *, near_control: bool = False) -> float:
    if is_marina_only(tags) and (tags.get("port_of_entry") or "") not in POE_YES:
        if near_control:
            return 0.55 if name else 0.45
        return 0.35
    conf = 0.55 if name else 0.35
    if (
        tags.get("harbour") == "yes"
        or tags.get("industrial") == "port"
        or tags.get("landuse") in ("harbour", "port")
    ):
        conf = max(conf, 0.6)
    role = osm_role(tags, near_control=near_control)
    if role == "commercial_harbour":
        conf = max(conf, 0.75 if name else 0.5)
    if role == "harbour_basin" and name:
        conf = max(conf, 0.55)
    if (tags.get("port_of_entry") or "").strip().lower() in POE_YES:
        conf = max(conf, 0.95 if name else 0.85)
    if in_eez and name:
        conf = max(conf, 0.7)
    return round(min(conf, 1.0), 2)


def cache_doc_to_seed(doc: dict) -> dict | None:
    """Convert a cache doc into a union seed (same schema as _slim)."""
    try:
        lat = float(doc["lat"])
        lon = float(doc["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    name = (doc.get("name") or "").strip()
    mid = doc.get("mrgid")
    try:
        mid = int(mid) if mid is not None else None
    except (TypeError, ValueError):
        mid = None
    osm_id = str(doc.get("osm_id") or "")
    tags = {str(k): str(v) for k, v in (doc.get("tags") or {}).items()}
    near = bool(doc.get("osm_near_control"))
    if tags and not is_seed_candidate(tags, near_control=near):
        return None
    tag_list = list(doc.get("osm_tags") or osm_tag_list(tags))
    urls = [f"https://www.openstreetmap.org/{osm_id}"] if osm_id else []
    kinds = list(doc.get("osm_kinds") or [])
    if near and is_marina_only(tags) and "marina" not in kinds:
        kinds.append("marina")
    customs = bool(doc.get("osm_customs"))
    border = bool(doc.get("osm_border"))
    return {
        "name": name,
        "mrgid": mid,
        "zone_name": doc.get("zone_name"),
        "country_iso2": doc.get("iso") or doc.get("country_iso2") or "",
        "lat": lat,
        "lon": lon,
        "validated": bool(doc.get("in_eez")),
        "osm_confidence": doc.get("osm_confidence"),
        "osm_tags": tag_list,
        "osm_role": doc.get("osm_role") or (
            osm_role(tags, near_control=near) if tags else None),
        "osm_id": osm_id or None,
        "osm_ids": [osm_id] if osm_id else [],
        "source_urls": urls,
        "extraction_engine": OSM_SOURCE,
        "confidence": doc.get("osm_confidence"),
        "dedup_key": (
            f"{mid}:{normalize_name(name)}" if mid is not None and name else None
        ),
        "seed_sources": [OSM_SOURCE],
        "has_coords": True,
        "osm_near_control": near,
        "osm_customs": customs,
        "osm_border": border,
        "osm_kinds": kinds,
    }


def _filters_for_part(part: tuple) -> str:
    kind = part[0]
    if kind == "eq":
        return f'["{part[1]}"="{part[2]}"]'
    if kind == "regex":
        return f'["{part[1]}"~"{part[2]}"]'
    if kind == "key":
        return f'["{part[1]}"]'
    if kind == "not_key":
        return f'[!"{part[1]}"]'
    raise ValueError(f"clause Overpass inconnue: {part!r}")


def _clause_to_overpass(clause: tuple, bbox: str) -> str:
    if clause[0] == "and":
        filters = "".join(_filters_for_part(p) for p in clause[1])
        return f"  nwr{filters}({bbox});"
    return f"  nwr{_filters_for_part(clause)}({bbox});"


def _overpass_query(south: float, west: float, north: float, east: float,
                    clauses: tuple[tuple, ...] = OSM_SEED_CLAUSES) -> str:
    bbox = f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}"
    parts = [_clause_to_overpass(clause, bbox) for clause in clauses]
    return (
        "[out:json][timeout:90];\n(\n"
        + "\n".join(parts)
        + "\n);\nout center tags;"
    )


def _overpass_marina_near_control_query(
    south: float, west: float, north: float, east: float,
    radius_m: int | None = None,
) -> str:
    """Marinas within a radius (m) of a control — not the worldwide leisure=marina dump."""
    from app.core.run_rules import get_rule
    if radius_m is None:
        radius_m = int(get_rule("formalities.marina_control_m", MARINA_CONTROL_RADIUS_M))
    bbox = f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}"
    ctrl = "\n".join(_clause_to_overpass(c, bbox) for c in OSM_CONTROL_CLAUSES)
    around = f"(around.ctrl:{int(radius_m)})"
    marinas = "\n".join(
        f"  nwr{_filters_for_part(c)}{around};" for c in MARINA_NEAR_CONTROL_CLAUSES
    )
    return (
        "[out:json][timeout:90];\n"
        f"(\n{ctrl}\n)->.ctrl;\n"
        f"(\n{marinas}\n);\n"
        "out center tags;"
    )


def _split_tile(south: float, west: float, north: float, east: float
                ) -> list[tuple[float, float, float, float]]:
    mid_lat = (south + north) / 2.0
    mid_lon = (west + east) / 2.0
    return [
        (south, west, mid_lat, mid_lon),
        (south, mid_lon, mid_lat, east),
        (mid_lat, west, north, mid_lon),
        (mid_lat, mid_lon, north, east),
    ]


async def _overpass_post(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    last_error = ""
    for url in OVERPASS_ENDPOINTS:
        try:
            r = await client.post(
                url,
                data={"data": query},
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=httpx.Timeout(connect=15.0, read=130.0, write=20.0, pool=10.0),
            )
            if r.status_code == 429:
                last_error = f"{url} HTTP 429"
                await asyncio.sleep(20)
                continue
            if r.status_code != 200:
                last_error = f"{url} HTTP {r.status_code}"
                logger.warning("Overpass %s", last_error)
                await asyncio.sleep(3)
                continue
            payload = r.json()
            remark = str(payload.get("remark") or "")
            if "timed out" in remark.lower():
                last_error = f"{url} timeout: {remark}"
                raise TimeoutError(last_error)
            return list(payload.get("elements") or [])
        except TimeoutError:
            raise
        except Exception as exc:
            last_error = f"{url}: {exc}"
            logger.warning("Overpass %s", last_error)
            await asyncio.sleep(2)
    raise RuntimeError(f"Overpass indisponible: {last_error}")


async def _fetch_tile(
    client: httpx.AsyncClient,
    tile: tuple[float, float, float, float],
    clauses: tuple[tuple, ...] = OSM_SEED_CLAUSES,
) -> list[dict[str, Any]]:
    south, west, north, east = tile
    query = _overpass_query(south, west, north, east, clauses)
    try:
        return await _overpass_post(client, query)
    except (TimeoutError, RuntimeError) as exc:
        span = max(north - south, east - west)
        if span > MIN_TILE_DEG:
            logger.warning("Overpass tuile trop lourde %s — split (%s)", tile, exc)
            out: list[dict[str, Any]] = []
            for sub in _split_tile(south, west, north, east):
                await asyncio.sleep(1.2)
                out.extend(await _fetch_tile(client, sub, clauses))
            return out
        if len(clauses) > 1:
            logger.warning("Overpass tuile %s — clauses une par une", tile)
            out = []
            for clause in clauses:
                await asyncio.sleep(1.2)
                out.extend(await _fetch_tile(client, tile, (clause,)))
            return out
        raise


async def _fetch_tile_marinas_near_control(
    client: httpx.AsyncClient,
    tile: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    south, west, north, east = tile
    query = _overpass_marina_near_control_query(south, west, north, east)
    try:
        return await _overpass_post(client, query)
    except (TimeoutError, RuntimeError) as exc:
        span = max(north - south, east - west)
        if span > MIN_TILE_DEG:
            logger.warning("Overpass marinas tuile trop lourde %s — split (%s)", tile, exc)
            out: list[dict[str, Any]] = []
            for sub in _split_tile(south, west, north, east):
                await asyncio.sleep(1.2)
                out.extend(await _fetch_tile_marinas_near_control(client, sub))
            return out
        raise


async def fetch_osm_elements(
    tiles: tuple[tuple[float, float, float, float], ...] = WORLD_TILES,
    log=None,
) -> list[dict[str, Any]]:
    log = log or (lambda m: logger.info("%s", m))
    by_id: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient() as client:
        for i, tile in enumerate(tiles, start=1):
            log(f"Overpass tuile {i}/{len(tiles)} {tile}")
            elements = await _fetch_tile(client, tile)
            await asyncio.sleep(1.2)
            marinas = await _fetch_tile_marinas_near_control(client, tile)
            for el in elements + marinas:
                osm_id = f"{el.get('type')}/{el.get('id')}"
                by_id[osm_id] = el
            log(
                f"  → {len(elements)} ports/contrôles + {len(marinas)} marinas "
                f"≤{MARINA_CONTROL_RADIUS_M}m, {len(by_id)} uniques"
            )
            await asyncio.sleep(1.5)
    return list(by_id.values())


def _bbox_hit(bbox: list[float] | None, lon: float, lat: float) -> bool:
    if not bbox or len(bbox) < 4:
        return True
    minx, miny, maxx, maxy = (float(bbox[0]), float(bbox[1]),
                              float(bbox[2]), float(bbox[3]))
    if not (miny <= lat <= maxy):
        return False
    if minx <= maxx:
        return minx <= lon <= maxx
    return lon >= minx or lon <= maxx


def load_eez_index_sync(zone_docs: list[dict]) -> list[dict]:
    zones: list[dict] = []
    for z in zone_docs:
        geom_raw = z.get("geometry")
        if not geom_raw:
            continue
        try:
            geom = shape(geom_raw)
            prepared = prep(geom)
        except Exception:
            continue
        bbox = z.get("bbox") or list(geom.bounds)
        zones.append({
            "mrgid": int(z["mrgid"]),
            "name": z.get("name") or z.get("geoname"),
            "iso2": z.get("iso2") or "",
            "_geom": geom,
            "_prep": prepared,
            "_bbox": bbox,
        })
    return zones


def assign_eez_point(lat: float, lon: float, zones: list[dict]) -> dict | None:
    """First VLIZ EEZ that accepts the point (polygon, sliver, coast ≤ 15 km)."""
    hits: list[tuple[float, dict]] = []
    for z in zones:
        if not _bbox_hit(z.get("_bbox"), lon, lat):
            continue
        cls = classify_poe_point(lat, lon, z["_geom"], z["_prep"], inland={})
        if not cls.get("validated"):
            continue
        if cls.get("kind") not in ("in_eez", "coastal_land"):
            continue
        hits.append((float(cls.get("dist_km") or 0.0), z))
    if not hits:
        return None
    hits.sort(key=lambda row: row[0])
    return hits[0][1]


def enrich_elements(elements: list[dict], zones: list[dict]) -> list[dict]:
    parsed: list[tuple[dict, float, float, dict[str, str]]] = []
    for el in elements:
        coords = element_coords(el)
        if coords is None:
            continue
        lat, lon = coords
        tags = {str(k): str(v) for k, v in (el.get("tags") or {}).items()}
        parsed.append((el, lat, lon, tags))
    controls = collect_control_points([
        {"lat": lat, "lon": lon, "tags": tags, "name": element_name(tags)}
        for _, lat, lon, tags in parsed
    ])
    docs: list[dict] = []
    seen: set[str] = set()
    now = datetime.now(timezone.utc).isoformat()
    for el, lat, lon, tags in parsed:
        hit = nearest_control(lat, lon, controls)
        near = hit is not None and is_marina_only(tags)
        if not is_seed_candidate(tags, near_control=near):
            continue
        name = element_name(tags)
        osm_id = f"{el.get('type')}/{el.get('id')}"
        if osm_id in seen:
            continue
        seen.add(osm_id)
        eez = assign_eez_point(lat, lon, zones)
        in_eez = bool(eez)
        mrgid = int(eez["mrgid"]) if eez else None
        iso = str((eez or {}).get("iso2") or "")
        zone_name = (eez or {}).get("name")
        customs = border = False
        if near and hit is not None:
            customs, border = control_kind_flags(hit[1])
        kinds = ["marina"] if near else []
        docs.append({
            "osm_id": osm_id,
            "name": name,
            "lat": lat,
            "lon": lon,
            "tags": tags,
            "osm_tags": osm_tag_list(tags),
            "osm_role": osm_role(tags, near_control=near),
            "osm_near_control": near,
            "osm_customs": customs,
            "osm_border": border,
            "osm_kinds": kinds,
            "mrgid": mrgid,
            "iso": iso,
            "zone_name": zone_name,
            "in_eez": in_eez,
            "osm_confidence": osm_confidence(
                name, tags, in_eez, near_control=near),
            "schema": CACHE_SCHEMA,
            "updated_at": now,
        })
    return docs


async def load_eez_index(db) -> list[dict]:
    raw = await db.eez_zones.find(
        {},
        {"mrgid": 1, "name": 1, "geoname": 1, "iso2": 1, "bbox": 1, "geometry": 1},
    ).to_list(500)
    return await asyncio.to_thread(load_eez_index_sync, raw)


async def cache_stats(db) -> dict[str, Any]:
    col = db[CACHE_COLLECTION]
    total = await col.count_documents({})
    named = await col.count_documents({"name": {"$nin": ["", None]}})
    in_eez = await col.count_documents({"in_eez": True})
    named_eez = await col.count_documents({"in_eez": True, "name": {"$nin": ["", None]}})
    meta = await db[CACHE_META].find_one({"_id": "latest"}) or {}
    schema = meta.get("schema")
    return {
        "cached_total": total,
        "cached_named": named,
        "cached_in_eez": in_eez,
        "cached_named_in_eez": named_eez,
        "refreshed_at": meta.get("refreshed_at"),
        "tiles": meta.get("tiles"),
        "overpass_elements": meta.get("overpass_elements"),
        "schema": schema,
        "expected_schema": CACHE_SCHEMA,
        "stale_schema": bool(total) and schema != CACHE_SCHEMA,
    }


async def load_cached_osm(db, *, in_eez_only: bool = True) -> list[dict]:
    query: dict[str, Any] = {}
    if in_eez_only:
        query = {"in_eez": True, "mrgid": {"$ne": None}}
    return await db[CACHE_COLLECTION].find(query, {"_id": 0}).to_list(50000)


async def refresh_osm_cache(db, log=None) -> dict[str, Any]:
    """Download Overpass, attach VLIZ, rewrite osm_port_seeds. Slow."""
    log = log or (lambda m: logger.info("%s", m))
    log("Overpass : candidats port + marinas ≤ 800 m d'un contrôle…")
    elements = await fetch_osm_elements(log=log)
    log(f"{len(elements)} objets OSM uniques — filtre candidats + index VLIZ…")
    zones = await load_eez_index(db)
    log(f"{len(zones)} ZEE avec géométrie — rattachement…")
    docs = await asyncio.to_thread(enrich_elements, elements, zones)
    tmp = db[f"{CACHE_COLLECTION}_tmp"]
    await tmp.drop()
    if docs:
        await tmp.insert_many(docs)
        await tmp.create_index("osm_id", unique=True)
        await tmp.create_index([("mrgid", 1), ("in_eez", 1)])
    await db[CACHE_COLLECTION].drop()
    if docs:
        await tmp.rename(CACHE_COLLECTION)
    stats = {
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "tiles": len(WORLD_TILES),
        "overpass_elements": len(elements),
        "cached_total": len(docs),
        "schema": CACHE_SCHEMA,
    }
    await db[CACHE_META].replace_one({"_id": "latest"}, {"_id": "latest", **stats}, upsert=True)
    out = await cache_stats(db)
    log(f"cache OSM : {out}")
    return out


async def osm_inventory(db) -> dict[str, Any]:
    """Taginfo (world) + local cache state. No Overpass."""
    taginfo = await asyncio.to_thread(taginfo_snapshot)
    stats = await cache_stats(db)
    return {
        "taginfo": taginfo,
        "cache": stats,
        "union_uses": (
            "osm_port_seeds where in_eez and named and is_seed_candidate "
            "(marina_pleasure si douane/border/PoE ≤ 800 m ; plus proximité unnamed)"
        ),
    }
