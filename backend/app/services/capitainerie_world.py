"""World dump of OSM harbormasters (harbour_master) + SHOM / NOAA overlays.

Contract:
  * object = harbormaster office, not the water body, not the marina
  * OSM identity = osm_id (type/id); orphan SHOM = shom:{layer}:{fid}
    ; orphan NOAA = noaa:{service}:{layer}:{fid}
  * no attachment to marinas
  * no purge (upsert)
  * resumable tiled Overpass job, then SHOM overlay
    (BUISGL FUNCTN=2 = harbour-master's office; SMCFAC CATSCF=6 if it exists)
    then NOAA ENC Direct overlay (same FUNCTN=2, points + area centroids)
  * phone / VHF read from tags first, never invented
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import quote_plus

import httpx

from app.core.events import HeartbeatWatch, emit
from app.core.identity import OVERLAY_RADIUS_KM, building_radius_km, find_building
from app.services.marina_build import (
    KEPT_TAGS,
    SHOM_CATSCF_LABELS,
    SHOM_WFS,
    USER_AGENT,
    _merc_to_wgs84,
    overpass_fetch_bbox,
)
from app.services.marina_world import (
    MIN_TILE_DEG,
    OVERPASS_THROTTLE_S,
    OVERPASS_TILE_TIMEOUT_S,
    WORLD_TILES,
    element_latlon,
    element_name,
    merge_website,
    osm_website_from_tags,
    split_bbox,
    tile_key,
)

SCHEMA = "capitainerie_world_v1"
CURSOR_ID = "world_harbour_master"
TILE_UPSERT_EVERY = 10
SHOM_CATSCF = "6"  # SMCFAC: rarely populated on the public WFS
SHOM_BUISGL_FUNCTN = "2"  # S-57 FUNCTN = harbour-master's office
SHOM_MERGE_KM = OVERLAY_RADIUS_KM
NOAA_MERGE_KM = OVERLAY_RADIUS_KM
SHOM_LAYERS: tuple[tuple[str, str], ...] = (
    ("INFORMATIONS_PORTUAIRES_BDD_WFS:buisgl_point", "buisgl"),
    ("INFORMATIONS_PORTUAIRES_BDD_WFS:smcfac_point", "smcfac"),
)
# ENC Direct to GIS — FUNCTN=2 lives mostly on harbour *areas*, not points.
NOAA_MAPSERVER = "https://gis.charttools.noaa.gov/arcgis/rest/services/encdirect"
NOAA_FUNCTN_WHERE = (
    "FUNCTN='2' OR FUNCTN LIKE '2,%' OR FUNCTN LIKE '%,2' "
    "OR FUNCTN LIKE '%,2,%' OR FUNCTN LIKE '2;%' OR FUNCTN LIKE '%;2' "
    "OR FUNCTN LIKE '%;2;%'"
)
# (service, layer_id, kind) — kind = point | area
NOAA_ENC_LAYERS: tuple[tuple[str, int, str], ...] = (
    ("enc_harbour", 22, "point"),
    ("enc_harbour", 143, "area"),
    ("enc_approach", 24, "point"),
    ("enc_approach", 148, "area"),
    ("enc_coastal", 21, "point"),
    ("enc_coastal", 110, "area"),
    ("enc_berthing", 12, "point"),
    ("enc_berthing", 66, "area"),
)
OSM_SOURCE = "openstreetmap"
SHOM_SOURCE = "shom"
NOAA_SOURCE = "noaa"
MERGED_SOURCE = "osm+shom"
GENERIC_OFFICE_NAMES = frozenset({
    "capitainerie",
    "harbour office",
    "harbour office (unnamed)",
    "harbour master's office",
    "harbour master",
    "harbourmaster",
    "harbormaster",
    "harbor master",
})

# WGS84 bboxes (south, west, north, east) where the SHOM WFS has SMCFAC.
SHOM_BBOXES: tuple[tuple[float, float, float, float], ...] = (
    (41.0, -6.0, 52.0, 10.0),       # mainland France + Corsica
    (14.0, -62.0, 18.6, -60.5),     # Antilles
    (-21.6, 55.0, -20.7, 56.1),     # Reunion
    (-13.1, 44.9, -12.5, 45.4),     # Mayotte
    (-23.0, 163.0, -19.5, 168.6),   # New Caledonia
    (-18.2, -152.0, -7.8, -134.0),  # Polynesia
    (46.7, -56.5, 47.2, -56.0),     # Saint-Pierre-et-Miquelon
    (-14.5, -178.3, -13.0, -176.0), # Wallis-et-Futuna
)

PRESERVE_ON_UPDATE = (
    "enriched",
    "enrichment_source",
    "enriched_at",
    "stale",
    "telephone",
    "canal_vhf",
    "shom_id",
    "noaa_id",
    "sources",
    "image",
)

SLIM_PROJECTION = {
    "_id": 1,
    "name": 1,
    "lat": 1,
    "lon": 1,
    "osm_id": 1,
    "shom_id": 1,
    "noaa_id": 1,
    "source": 1,
    "sources": 1,
    "website": 1,
    "website_status": 1,
    "website_source": 1,
    "telephone": 1,
    "canal_vhf": 1,
    "fetched_at": 1,
}

PHONE_TAG_KEYS = (
    "phone", "contact:phone", "telephone", "contact:telephone",
    "contact:mobile", "mobile",
)
VHF_TAG_KEYS = (
    "vhf", "vhf_channel", "channel", "harbour:vhf", "communication:vhf",
    "seamark:communication:channel", "seamark:harbour:vhf", "comcha",
    "shom:comcha",
)
INFO_TAG_KEYS = (
    "description", "seamark:information", "seamark:information:fr",
    "inform", "ninfom", "shom:inform", "shom:ninfom", "note",
    "noaa:inform", "noaa:objnam",
)

# ITU marine VHF : 1–28 simplex, 60–88 duplex (US 68/72, UK 80, …).
_VHF_BLOCK_RE = re.compile(
    r"(?:vhf|canal(?:\s*vhf)?|channel|ch\.?|comcha)\s*[:n°#]?\s*"
    r"([\d][\d\s/;,&-]{0,24})",
    re.I,
)
_PHONE_RE = re.compile(
    r"(?:\+|00)?\d[\d.\s()/.-]{6,20}\d",
)
_PHONE_CONTEXT_RE = re.compile(
    r"(?:tél\.?(?:éphone)?|telephone|\bphone\b|\btel\b|"
    r"harbour\s*master|harbor\s*master|capitainerie)"
    r"[^\d+]{0,48}"
    r"((?:\+|00)?\d[\d.\s()/.-]{6,18}\d)",
    re.I,
)
_TEL_LINK_RE = re.compile(r"tel:(\+?[\d\s().-]{8,22})", re.I)
_FAX_ONLY_RE = re.compile(r"\bfax\b", re.I)
_TEL_WORD_RE = re.compile(r"tel|phone|tél", re.I)

FetchTile = Callable[
    [httpx.AsyncClient, tuple[float, float, float, float]],
    Awaitable[list[dict]],
]
FetchShom = Callable[
    [httpx.AsyncClient, tuple[float, float, float, float]],
    Awaitable[list[dict]],
]
FetchNoaa = Callable[
    [httpx.AsyncClient],
    Awaitable[list[dict]],
]


def google_maps_url(name: str | None, lat: float, lon: float) -> str:
    if name and str(name).strip():
        q = f"{str(name).strip()} {lat:.5f},{lon:.5f}"
    else:
        q = f"{lat:.5f},{lon:.5f}"
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q)


def is_harbour_master(tags: dict | None) -> bool:
    tags = tags or {}
    if tags.get("office") == "harbour_master":
        return True
    if tags.get("seamark:building:function") == "harbour_master":
        return True
    if tags.get("harbour") == "harbour_master":
        return True
    return False


def harbour_master_bbox_ql(
    south: float, west: float, north: float, east: float,
    timeout: int = OVERPASS_TILE_TIMEOUT_S,
) -> str:
    bbox = f"({south:.4f},{west:.4f},{north:.4f},{east:.4f})"
    return (
        f"[out:json][timeout:{int(timeout)}];\n"
        f"(\n"
        f"  nwr[\"office\"=\"harbour_master\"]{bbox};\n"
        f"  nwr[\"seamark:building:function\"=\"harbour_master\"]{bbox};\n"
        f"  nwr[\"harbour\"=\"harbour_master\"]{bbox};\n"
        f");\n"
        f"out center tags;"
    )


def kept_tags(tags: dict | None) -> dict[str, str]:
    tags = tags or {}
    out: dict[str, str] = {}
    extra = ("office", "harbour", "building")
    for k, v in tags.items():
        if v is None or isinstance(v, (dict, list)):
            continue
        if (
            k in KEPT_TAGS or k in extra or k in PHONE_TAG_KEYS or k in VHF_TAG_KEYS
            or k.startswith("seamark:") or k.startswith("name") or k.startswith("contact:")
            or k.startswith("shom:") or k.startswith("noaa:")
        ):
            out[str(k)] = str(v)[:240]
    return out


def _digit_count(text: str) -> int:
    return len(re.sub(r"\D", "", text or ""))


def _looks_like_year(digits: str) -> bool:
    return bool(re.fullmatch(r"(?:19|20)\d{2}", digits))


def _format_phone(raw: str) -> str:
    cand = re.sub(r"\s+", " ", raw).strip()
    if cand.startswith("00") and _digit_count(cand) >= 10:
        cand = "+" + cand[2:].lstrip()
    return cand[:40]


def _phone_candidate_ok(raw: str) -> bool:
    n = _digit_count(raw)
    if n < 8 or n > 15:
        return False
    compact = re.sub(r"\D", "", raw)
    if _looks_like_year(compact):
        return False
    return True


def _iter_phone_chunks(text: str) -> list[str]:
    return [c.strip() for c in re.split(r"[;|]", text) if c.strip()]


def _phones_in_chunk(chunk: str) -> list[str]:
    if _FAX_ONLY_RE.search(chunk) and not _TEL_WORD_RE.search(chunk):
        return []
    found: list[str] = []
    for m in _TEL_LINK_RE.finditer(chunk):
        cand = _format_phone(m.group(1))
        if _phone_candidate_ok(cand):
            found.append(cand)
    for m in _PHONE_CONTEXT_RE.finditer(chunk):
        cand = _format_phone(m.group(1))
        if _phone_candidate_ok(cand) and cand not in found:
            found.append(cand)
    if found:
        return found
    for m in _PHONE_RE.finditer(chunk):
        cand = _format_phone(m.group(0))
        if _phone_candidate_ok(cand) and cand not in found:
            found.append(cand)
    return found


def _clean_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text or text.lower() in ("no", "none", "null"):
        return None
    preferred = None
    fallback = None
    for chunk in _iter_phone_chunks(text):
        cands = _phones_in_chunk(chunk)
        for cand in cands:
            if cand.startswith("+"):
                return cand
            if preferred is None:
                preferred = cand
            elif fallback is None:
                fallback = cand
    if preferred:
        return preferred
    if fallback:
        return fallback
    if _FAX_ONLY_RE.search(text) and not _TEL_WORD_RE.search(text):
        return None
    digits = re.sub(r"[^\d+]", "", text)
    if _digit_count(digits) < 8:
        return None
    return text[:40]


def _is_marine_channel(n: int) -> bool:
    return 1 <= n <= 28 or 60 <= n <= 88


def _norm_vhf_part(part: str) -> str | None:
    part = str(part or "").strip()
    if not part.isdigit():
        return None
    n = int(part)
    if _is_marine_channel(n):
        return str(n)
    return None


def _channels_from_fragment(fragment: str) -> list[str]:
    nums: list[str] = []
    for p in re.findall(r"\d{1,2}", fragment or ""):
        n = _norm_vhf_part(p)
        if n and n not in nums:
            nums.append(n)
    return nums


def _clean_vhf(raw: str | None) -> str | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text or text.lower() in ("no", "none", "null"):
        return None
    compact = re.sub(r"\D", "", text)
    if _looks_like_year(compact):
        return None
    nums: list[str] = []
    for m in _VHF_BLOCK_RE.finditer(text):
        for n in _channels_from_fragment(m.group(1)):
            if n not in nums:
                nums.append(n)
    if nums:
        return "/".join(nums)[:20]
    if len(text) <= 24:
        nums = _channels_from_fragment(text)
        if nums:
            return "/".join(nums)[:20]
    return None


def contact_from_text(text: str | None) -> tuple[str | None, str | None]:
    """Phone / VHF extracted from a page (Fetch markdown, readability, INFORM)."""
    if not text or not str(text).strip():
        return None, None
    blob = str(text)
    return _clean_phone(blob), _clean_vhf(blob)


def contact_from_tags(tags: dict | None) -> tuple[str | None, str | None]:
    """Phone and VHF channel read from OSM/SHOM tags. Never invented."""
    tags = tags or {}
    phone = None
    for key in PHONE_TAG_KEYS:
        phone = _clean_phone(tags.get(key))
        if phone:
            break
    vhf = None
    for key in VHF_TAG_KEYS:
        vhf = _clean_vhf(tags.get(key))
        if vhf:
            break
    if not phone or not vhf:
        blobs = [str(tags.get(k) or "") for k in INFO_TAG_KEYS]
        blob = " ".join(b for b in blobs if b)
        if blob:
            phone = phone or _clean_phone(blob)
            vhf = vhf or _clean_vhf(blob)
    return phone, vhf


def fill_contact(doc: dict, phone: str | None, vhf: str | None) -> dict:
    """Fill empty fields only."""
    if phone and not doc.get("telephone"):
        doc["telephone"] = phone
    if vhf and not doc.get("canal_vhf"):
        doc["canal_vhf"] = vhf
    return doc


def capitainerie_from_overpass(elem: dict) -> dict | None:
    typ = elem.get("type")
    eid = elem.get("id")
    if not typ or eid is None:
        return None
    coords = element_latlon(elem)
    if coords is None:
        return None
    lat, lon = coords
    tags = elem.get("tags") or {}
    if not is_harbour_master(tags):
        return None
    kept = kept_tags(tags)
    phone, vhf = contact_from_tags(tags)
    return {
        "osm_id": f"{typ}/{eid}",
        "name": element_name(tags),
        "lat": lat,
        "lon": lon,
        "source": OSM_SOURCE,
        "sources": [OSM_SOURCE],
        "tags": kept,
        "website": osm_website_from_tags(tags),
        "telephone": phone,
        "canal_vhf": vhf,
    }


def is_catscf_harbour_master(catscf: Any) -> bool:
    return str(catscf or "").strip() == SHOM_CATSCF


def is_buisgl_harbour_master(functn: Any) -> bool:
    """S-57 FUNCTN 2 = harbour-master's office (list-valued, e.g. '2,3')."""
    raw = str(functn or "").strip()
    if raw.endswith(".0") and raw[:-2].replace("-", "", 1).isdigit():
        raw = raw[:-2]
    parts = [
        p.strip()
        for p in raw.replace(";", ",").split(",")
        if p.strip()
    ]
    return SHOM_BUISGL_FUNCTN in parts


def shom_feature_id(
    feat: dict, props: dict, lat: float, lon: float, layer: str = "smcfac",
) -> str:
    raw = (
        feat.get("id") or props.get("inspireid") or props.get("gml_id")
        or props.get("id") or props.get("fid")
    )
    if raw not in (None, "", "null"):
        return f"shom:{layer}:{raw}"
    return f"shom:{layer}:{lat:.5f}:{lon:.5f}"


def _shom_latlon(feat: dict) -> tuple[float, float] | None:
    geom = feat.get("geometry") or {}
    coords = geom.get("coordinates")
    if geom.get("type") == "Point" and coords:
        x, y = coords[:2]
    elif geom.get("type") == "MultiPoint" and coords:
        x, y = coords[0][:2]
    else:
        return None
    try:
        xf, yf = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if abs(xf) > 180 or abs(yf) > 90:
        lat, lon = _merc_to_wgs84(xf, yf)
    else:
        lon, lat = xf, yf
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def capitainerie_from_shom(feat: dict, *, layer: str = "smcfac") -> dict | None:
    """SHOM office: BUISGL FUNCTN=2, or SMCFAC CATSCF=6 if it exists."""
    props = feat.get("properties") or {}
    if layer == "buisgl":
        if not is_buisgl_harbour_master(props.get("functn")):
            return None
        cat_label = "Capitainerie"
        layer_tags = {
            "shom:functn": str(props.get("functn") or "").strip(),
            "shom:layer": "buisgl",
        }
    else:
        if not is_catscf_harbour_master(props.get("catscf")):
            return None
        cat_label = SHOM_CATSCF_LABELS.get(SHOM_CATSCF, "Capitainerie")
        layer_tags = {
            "shom:catscf": SHOM_CATSCF,
            "shom:category": cat_label,
            "shom:layer": "smcfac",
        }
    coords = _shom_latlon(feat)
    if coords is None:
        return None
    lat, lon = coords
    name = _chart_office_name(
        str(props.get("objnam") or "").strip()
        or str(props.get("nobjnm") or "").strip()
        or str(props.get("toponyme") or "").strip(),
        str(props.get("inform") or "").strip(),
        cat_label,
    )
    tags = dict(layer_tags)
    for k, v in props.items():
        if v is None or isinstance(v, (dict, list)):
            continue
        val = str(v).strip()
        if not val or val in ("null", "None"):
            continue
        tags[f"shom:{k}"] = val[:120]
    phone, vhf = contact_from_tags(tags)
    shom_id = shom_feature_id(feat, props, lat, lon, layer=layer)
    return {
        "shom_id": shom_id,
        "name": name[:120],
        "lat": lat,
        "lon": lon,
        "source": SHOM_SOURCE,
        "sources": [SHOM_SOURCE],
        "tags": tags,
        "website": None,
        "telephone": phone,
        "canal_vhf": vhf,
    }


def _better_name(current: str | None, incoming: str | None) -> str | None:
    incoming = (incoming or "").strip()
    if not incoming:
        return None
    cur = (current or "").strip()
    if not cur or cur.lower() in GENERIC_OFFICE_NAMES:
        if incoming.lower() not in GENERIC_OFFICE_NAMES:
            return incoming
        if not cur:
            return incoming
    return None


def slim_feature(doc: dict) -> dict:
    lat = float(doc["lat"])
    lon = float(doc["lon"])
    name = doc.get("name") or ""
    website = doc.get("website") or osm_website_from_tags(doc.get("tags") or {})
    sources = doc.get("sources") or ([doc.get("source")] if doc.get("source") else [])
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "id": doc.get("_id"),
            "kind": "capitainerie",
            "name": name,
            "osm_id": doc.get("osm_id"),
            "shom_id": doc.get("shom_id"),
            "noaa_id": doc.get("noaa_id"),
            "source": doc.get("source") or OSM_SOURCE,
            "sources": sources,
            "website": website,
            "telephone": doc.get("telephone"),
            "canal_vhf": doc.get("canal_vhf"),
            "maps_url": google_maps_url(name, lat, lon),
            "fetched_at": doc.get("fetched_at"),
        },
    }


def to_slim_geojson(docs: Iterable[dict]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            slim_feature(d) for d in docs
            if d.get("lat") is not None and d.get("lon") is not None
        ],
        "attribution": (
            "© OpenStreetMap contributors (ODbL) · SHOM INFORMATIONS_PORTUAIRES "
            "(Licence Ouverte Etalab) · NOAA ENC Direct to GIS (not for navigation)"
        ),
    }


def _merge_sources(*groups: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for group in groups:
        for src in group or []:
            if src and src not in out and src != MERGED_SOURCE:
                out.append(src)
    return out


def _source_label(sources: list[str]) -> str:
    has_osm = OSM_SOURCE in sources
    has_shom = SHOM_SOURCE in sources
    has_noaa = NOAA_SOURCE in sources
    parts: list[str] = []
    if has_osm:
        parts.append("osm" if (has_shom or has_noaa) else OSM_SOURCE)
    if has_shom:
        parts.append(SHOM_SOURCE)
    if has_noaa:
        parts.append(NOAA_SOURCE)
    if not parts:
        return OSM_SOURCE
    if parts == [OSM_SOURCE]:
        return OSM_SOURCE
    return "+".join(parts)


def nearest_office(
    lat: float, lon: float, pts: list[dict], radius_km: float | None = None,
):
    """Building overlay (≤ 250 m, distance only). Not Project/PoE card merge."""
    hit = find_building(lat, lon, pts, radius_km=radius_km)
    return None if hit is None else hit.doc


def nearest_osm(lat: float, lon: float, osm_pts: list[dict], radius_km: float | None = None):
    return nearest_office(lat, lon, osm_pts, radius_km=radius_km)


async def upsert_osm(coll, cand: dict, now_iso: str) -> str:
    from app.services.isolated_runs import identity_query, run_doc_id, stamp
    osm_id = cand["osm_id"]
    existing = await coll.find_one(identity_query("osm_id", osm_id))
    if existing is None:
        existing = await coll.find_one(identity_query("_id", osm_id))
    website, status, wsrc = merge_website(existing, cand.get("website"))
    sources = _merge_sources(
        (existing or {}).get("sources"),
        cand.get("sources"),
        [OSM_SOURCE],
    )
    patch: dict[str, Any] = {
        "name": cand.get("name") or "",
        "lat": cand["lat"],
        "lon": cand["lon"],
        "source": _source_label(sources),
        "sources": sources,
        "osm_id": osm_id,
        "tags": cand.get("tags") or {},
        "website": website,
        "website_status": status,
        "website_source": wsrc,
        "fetched_at": now_iso,
        "schema": SCHEMA,
    }
    if existing:
        for field in PRESERVE_ON_UPDATE:
            if existing.get(field) not in (None, "", [], {}):
                patch[field] = existing[field]
        fill_contact(patch, cand.get("telephone"), cand.get("canal_vhf"))
        patch = stamp(patch, source_id=osm_id, wrote_flag="wrote_capitaineries")
        await coll.update_one({"_id": existing["_id"]}, {"$set": patch})
        return "updated"
    patch = stamp(patch, source_id=osm_id, wrote_flag="wrote_capitaineries")
    patch["_id"] = run_doc_id(osm_id)
    patch["enriched"] = False
    patch["stale"] = False
    fill_contact(patch, cand.get("telephone"), cand.get("canal_vhf"))
    try:
        await coll.insert_one(patch)
        return "inserted"
    except Exception as exc:
        if type(exc).__name__ != "DuplicateKeyError":
            raise
        existing = await coll.find_one(identity_query("osm_id", osm_id))
        if existing:
            await coll.update_one({"_id": existing["_id"]}, {"$set": patch})
            return "updated"
        raise


async def upsert_shom(coll, cand: dict, now_iso: str, osm_pts: list[dict]) -> str:
    """SHOM overlay on an OSM within ≤ 250 m (distance only), else orphan."""
    hit = nearest_osm(cand["lat"], cand["lon"], osm_pts)
    if hit:
        sources = _merge_sources(hit.get("sources"), [OSM_SOURCE, SHOM_SOURCE])
        tags = dict(hit.get("tags") or {})
        tags.update(cand.get("tags") or {})
        patch: dict[str, Any] = {
            "shom_id": cand["shom_id"],
            "source": _source_label(sources),
            "sources": sources,
            "tags": tags,
            "fetched_at": now_iso,
            "schema": SCHEMA,
        }
        better = _better_name(hit.get("name"), cand.get("name"))
        if better:
            patch["name"] = better
        fill_contact(patch, cand.get("telephone"), cand.get("canal_vhf"))
        if not hit.get("telephone") and cand.get("telephone"):
            patch["telephone"] = cand["telephone"]
        if not hit.get("canal_vhf") and cand.get("canal_vhf"):
            patch["canal_vhf"] = cand["canal_vhf"]
        from app.services.isolated_runs import stamp
        patch = stamp(patch, source_id=hit.get("source_id") or hit.get("osm_id") or hit.get("_id"),
                      wrote_flag="wrote_capitaineries")
        await coll.update_one({"_id": hit["_id"]}, {"$set": patch})
        hit.update(patch)
        return "merged"
    shom_id = cand["shom_id"]
    from app.services.isolated_runs import identity_query, run_doc_id, stamp
    existing = (await coll.find_one(identity_query("_id", shom_id))
                or await coll.find_one(identity_query("shom_id", shom_id)))
    patch = {
        "name": cand.get("name") or "",
        "lat": cand["lat"],
        "lon": cand["lon"],
        "source": SHOM_SOURCE,
        "sources": [SHOM_SOURCE],
        "shom_id": shom_id,
        "tags": cand.get("tags") or {},
        "website": None,
        "fetched_at": now_iso,
        "schema": SCHEMA,
    }
    if existing:
        for field in PRESERVE_ON_UPDATE:
            if existing.get(field) not in (None, "", [], {}):
                patch[field] = existing[field]
        fill_contact(patch, cand.get("telephone"), cand.get("canal_vhf"))
        patch = stamp(patch, source_id=shom_id, wrote_flag="wrote_capitaineries")
        await coll.update_one(
            {"_id": existing["_id"]},
            {"$set": patch, "$unset": {"osm_id": ""}},
        )
        return "updated"
    patch = stamp(patch, source_id=shom_id, wrote_flag="wrote_capitaineries")
    patch["_id"] = run_doc_id(shom_id)
    patch["enriched"] = False
    patch["stale"] = False
    fill_contact(patch, cand.get("telephone"), cand.get("canal_vhf"))
    await coll.insert_one(patch)
    return "inserted"


async def upsert_noaa(coll, cand: dict, now_iso: str, pts: list[dict]) -> str:
    """NOAA overlay on an office within ≤ 250 m (distance only), else orphan."""
    hit = nearest_office(cand["lat"], cand["lon"], pts)
    if hit:
        sources = _merge_sources(hit.get("sources"), [NOAA_SOURCE])
        tags = dict(hit.get("tags") or {})
        tags.update(cand.get("tags") or {})
        patch: dict[str, Any] = {
            "noaa_id": cand["noaa_id"],
            "source": _source_label(sources),
            "sources": sources,
            "tags": tags,
            "fetched_at": now_iso,
            "schema": SCHEMA,
        }
        better = _better_name(hit.get("name"), cand.get("name"))
        if better:
            patch["name"] = better
        fill_contact(patch, cand.get("telephone"), cand.get("canal_vhf"))
        if not hit.get("telephone") and cand.get("telephone"):
            patch["telephone"] = cand["telephone"]
        if not hit.get("canal_vhf") and cand.get("canal_vhf"):
            patch["canal_vhf"] = cand["canal_vhf"]
        from app.services.isolated_runs import stamp
        patch = stamp(patch, source_id=hit.get("source_id") or hit.get("osm_id") or hit.get("_id"),
                      wrote_flag="wrote_capitaineries")
        await coll.update_one({"_id": hit["_id"]}, {"$set": patch})
        hit.update(patch)
        return "merged"
    noaa_id = cand["noaa_id"]
    from app.services.isolated_runs import identity_query, run_doc_id, stamp
    existing = (await coll.find_one(identity_query("_id", noaa_id))
                or await coll.find_one(identity_query("noaa_id", noaa_id)))
    patch = {
        "name": cand.get("name") or "",
        "lat": cand["lat"],
        "lon": cand["lon"],
        "source": NOAA_SOURCE,
        "sources": [NOAA_SOURCE],
        "noaa_id": noaa_id,
        "tags": cand.get("tags") or {},
        "website": None,
        "fetched_at": now_iso,
        "schema": SCHEMA,
    }
    if existing:
        for field in PRESERVE_ON_UPDATE:
            if existing.get(field) not in (None, "", [], {}):
                patch[field] = existing[field]
        fill_contact(patch, cand.get("telephone"), cand.get("canal_vhf"))
        patch = stamp(patch, source_id=noaa_id, wrote_flag="wrote_capitaineries")
        await coll.update_one(
            {"_id": existing["_id"]},
            {"$set": patch, "$unset": {"osm_id": ""}},
        )
        return "updated"
    patch = stamp(patch, source_id=noaa_id, wrote_flag="wrote_capitaineries")
    patch["_id"] = run_doc_id(noaa_id)
    patch["enriched"] = False
    patch["stale"] = False
    fill_contact(patch, cand.get("telephone"), cand.get("canal_vhf"))
    await coll.insert_one(patch)
    pts.append(patch)
    return "inserted"


def _cursor_key() -> str:
    from app.services.isolated_runs import cursor_id
    return cursor_id(CURSOR_ID)


async def load_done_tiles(cursor_coll) -> list[str]:
    cid = _cursor_key()
    doc = await cursor_coll.find_one({"_id": cid}) or {}
    return list(doc.get("done_tiles") or [])


async def mark_tile_done(cursor_coll, key: str, now_iso: str) -> None:
    from app.services.isolated_runs import current_run_id
    cid = _cursor_key()
    done = await load_done_tiles(cursor_coll)
    if key not in done:
        done.append(key)
    payload = {"_id": cid, "done_tiles": done, "updated_at": now_iso, "schema": SCHEMA}
    rid = current_run_id()
    if rid:
        payload["run_id"] = rid
    existing = await cursor_coll.find_one({"_id": cid})
    if existing:
        await cursor_coll.replace_one({"_id": cid}, payload)
    else:
        await cursor_coll.insert_one(payload)


async def reset_cursor(cursor_coll) -> None:
    cid = _cursor_key()
    existing = await cursor_coll.find_one({"_id": cid})
    if existing:
        await cursor_coll.replace_one(
            {"_id": cid},
            {"_id": cid, "done_tiles": [], "updated_at": None, "schema": SCHEMA},
        )


def _id_partial_index(field: str) -> dict:
    """Unique index (run_id, id): only if the id is a string.

    A compound *sparse* index (run_id, shom_id) indexes every document that has
    `run_id` — i.e. all of them. Mongo stores `shom_id: null` / missing field as
    the same key: the 2nd OSM card of an isolated run raises E11000. The Albania
    tile dump had only 1 harbour_master and did not see it.
    """
    return {
        "unique": True,
        "name": f"run_id_1_{field}_1",
        "partialFilterExpression": {field: {"$type": "string"}},
    }


async def _drop_index_quiet(coll, name: str) -> None:
    drop = getattr(coll, "drop_index", None)
    if drop is None:
        return
    try:
        await drop(name)
    except TypeError:
        drop(name)
    except Exception:
        pass


async def ensure_indexes(coll, *, isolated: bool = False) -> None:
    try:
        # Unique sparse: an explicit `osm_id: null` is indexed only once.
        if hasattr(coll, "update_many"):
            await coll.update_many({"osm_id": None}, {"$unset": {"osm_id": ""}})
            await coll.update_many({"shom_id": None}, {"$unset": {"shom_id": ""}})
            await coll.update_many({"noaa_id": None}, {"$unset": {"noaa_id": ""}})
        if isolated:
            for field in ("osm_id", "shom_id", "noaa_id"):
                await _drop_index_quiet(coll, f"run_id_1_{field}_1")
                await coll.create_index(
                    [("run_id", 1), (field, 1)], **_id_partial_index(field))
            await coll.create_index("run_id")
            await coll.create_index("name")
            await coll.create_index("source")
            return
        await coll.create_index("osm_id", unique=True, sparse=True)
        await coll.create_index("shom_id", unique=True, sparse=True)
        await coll.create_index("noaa_id", unique=True, sparse=True)
        await coll.create_index("name")
        await coll.create_index("source")
    except Exception:
        pass


def _throttle_s(explicit: float | None) -> float:
    if explicit is not None:
        return float(explicit)
    try:
        from app.core.run_rules import get_rule
        return float(get_rule("capitaineries.overpass_throttle_s", OVERPASS_THROTTLE_S))
    except Exception:
        return OVERPASS_THROTTLE_S


async def fetch_tile_harbour_masters(
    client: httpx.AsyncClient,
    tile: tuple[float, float, float, float],
    *,
    logger=None,
    timeout: int = OVERPASS_TILE_TIMEOUT_S,
    min_span: float = MIN_TILE_DEG,
    meta: dict | None = None,
) -> list[dict]:
    south, west, north, east = tile
    body = harbour_master_bbox_ql(south, west, north, east, timeout=timeout)
    try:
        return await overpass_fetch_bbox(
            south, west, north, east, client, logger=logger, body=body, meta=meta,
        )
    except Exception as exc:
        span = max(north - south, east - west)
        if span > min_span:
            if logger:
                logger(f"Tuile trop lourde {tile} ({type(exc).__name__}) — split")
            out: list[dict] = []
            for sub in split_bbox(south, west, north, east):
                await asyncio.sleep(1.2)
                out.extend(await fetch_tile_harbour_masters(
                    client, sub, logger=logger, timeout=timeout, min_span=min_span,
                ))
            return out
        raise


async def shom_fetch_harbour_offices(
    client: httpx.AsyncClient,
    bbox: tuple[float, float, float, float],
    logger=None,
) -> list[dict]:
    """WFS SHOM : BUISGL FUNCTN=2 (bureaux) + SMCFAC CATSCF=6. Filtre local (CQL en 403)."""
    s, w, n, e = bbox
    bbox_str = f"{w:.6f},{s:.6f},{e:.6f},{n:.6f},EPSG:4326"
    out: list[dict] = []
    for typename, layer in SHOM_LAYERS:
        start = 0
        page = 1000
        got = 0
        while True:
            params = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typenames": typename,
                "bbox": bbox_str,
                "outputFormat": "application/json",
                "count": str(page),
                "startIndex": str(start),
            }
            try:
                r = await client.get(
                    SHOM_WFS,
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=60,
                )
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                if logger:
                    logger(f"SHOM {layer} {bbox_str}: {type(exc).__name__}: {str(exc)[:80]}")
                break
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code != 200 or "json" not in ct:
                if logger:
                    logger(f"SHOM {typename}: HTTP {r.status_code}")
                break
            fc = r.json()
            feats = fc.get("features") or []
            for feat in feats:
                cand = capitainerie_from_shom(feat, layer=layer)
                if cand:
                    out.append(cand)
                    got += 1
            if len(feats) < page:
                break
            start += page
            if start > 20000:
                break
        if logger:
            logger(f"SHOM {layer} {bbox_str}: {got} capitaineries")
    return out


async def shom_fetch_catscf6(
    client: httpx.AsyncClient,
    bbox: tuple[float, float, float, float],
    logger=None,
) -> list[dict]:
    return await shom_fetch_harbour_offices(client, bbox, logger=logger)


def _geojson_latlon(geom: dict | None) -> tuple[float, float] | None:
    geom = geom or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    try:
        if gtype == "Point" and coords and len(coords) >= 2:
            return float(coords[1]), float(coords[0])
        ring = None
        if gtype == "Polygon" and coords:
            ring = coords[0] or []
        elif gtype == "MultiPolygon" and coords:
            ring = (coords[0] or [[]])[0] or []
        if ring:
            lon = sum(float(p[0]) for p in ring) / len(ring)
            lat = sum(float(p[1]) for p in ring) / len(ring)
            return lat, lon
        if "x" in geom and "y" in geom:
            return float(geom["y"]), float(geom["x"])
        rings = geom.get("rings")
        if rings and rings[0]:
            lon = sum(float(p[0]) for p in rings[0]) / len(rings[0])
            lat = sum(float(p[1]) for p in rings[0]) / len(rings[0])
            return lat, lon
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None
    return None


def noaa_feature_id(feat: dict, props: dict, service: str, layer_id: int, kind: str) -> str:
    raw = (
        props.get("OBJECTID") or props.get("objectid") or props.get("FID")
        or props.get("fid") or feat.get("id")
    )
    if raw in (None, "", "null"):
        for key, val in props.items():
            ku = str(key).upper()
            if ku.endswith(".FID") or ku.endswith("OBJECTID"):
                raw = val
                break
    if raw not in (None, "", "null"):
        return f"noaa:{service}:{kind}:{layer_id}:{raw}"
    coords = _geojson_latlon(feat.get("geometry") or {})
    if coords:
        lat, lon = coords
        return f"noaa:{service}:{kind}:{lat:.5f}:{lon:.5f}"
    return f"noaa:{service}:{kind}:{layer_id}:unknown"


_OFFICE_NAME_RE = re.compile(
    r"harbour\s*master|harbor\s*master|harbormaster|capitainerie|"
    r"coast\s*guard|\bccg\b|harbour\s*office|harbor\s*office",
    re.I,
)


def _chart_office_name(objnam: str | None, inform: str | None, fallback: str) -> str:
    objnam = (objnam or "").strip()
    if objnam:
        return objnam[:120]
    inform = (inform or "").strip()
    if inform and _OFFICE_NAME_RE.search(inform):
        return inform[:120]
    return fallback


def capitainerie_from_noaa(
    feat: dict, *, service: str, layer_id: int, kind: str = "point",
) -> dict | None:
    """NOAA ENC office: BUISGL FUNCTN=2 (point or area centroid)."""
    props = feat.get("properties") or feat.get("attributes") or {}
    if not is_buisgl_harbour_master(props.get("FUNCTN") or props.get("functn")):
        return None
    coords = _geojson_latlon(feat.get("geometry") or {})
    if coords is None:
        return None
    lat, lon = coords
    name = _chart_office_name(
        props.get("OBJNAM") or props.get("objnam"),
        props.get("INFORM") or props.get("inform"),
        "Harbour master's office",
    )
    tags = {
        "noaa:functn": str(props.get("FUNCTN") or props.get("functn") or "").strip(),
        "noaa:layer": f"{service}:{layer_id}:{kind}",
        "noaa:service": service,
    }
    for k, v in props.items():
        if v is None or isinstance(v, (dict, list)):
            continue
        val = str(v).strip()
        if not val or val in ("null", "None"):
            continue
        tags[f"noaa:{k.lower()}"] = val[:120]
    phone, vhf = contact_from_tags(tags)
    noaa_id = noaa_feature_id(feat, props, service, layer_id, kind)
    return {
        "noaa_id": noaa_id,
        "name": name[:120],
        "lat": lat,
        "lon": lon,
        "source": NOAA_SOURCE,
        "sources": [NOAA_SOURCE],
        "tags": tags,
        "website": None,
        "telephone": phone,
        "canal_vhf": vhf,
    }


async def noaa_fetch_harbour_offices(
    client: httpx.AsyncClient,
    logger=None,
    layers: tuple[tuple[str, int, str], ...] | None = None,
) -> list[dict]:
    """ENC Direct: FUNCTN=2 on points + areas, several scale bands."""
    out: list[dict] = []
    for service, layer_id, kind in (layers or NOAA_ENC_LAYERS):
        url = f"{NOAA_MAPSERVER}/{service}/MapServer/{layer_id}/query"
        start = 0
        page = 1000
        got = 0
        while True:
            params = {
                "where": NOAA_FUNCTN_WHERE,
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "resultOffset": str(start),
                "resultRecordCount": str(page),
                "f": "geojson",
            }
            try:
                r = await client.get(
                    url,
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=60,
                )
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                if logger:
                    logger(f"NOAA {service}/{layer_id}: {type(exc).__name__}: {str(exc)[:80]}")
                break
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code != 200 or "json" not in ct:
                if logger:
                    logger(f"NOAA {service}/{layer_id}: HTTP {r.status_code}")
                break
            fc = r.json() if r.content else {}
            feats = fc.get("features") or []
            for feat in feats:
                cand = capitainerie_from_noaa(
                    feat, service=service, layer_id=layer_id, kind=kind,
                )
                if cand:
                    out.append(cand)
                    got += 1
            if len(feats) < page:
                break
            start += page
            if start > 20000:
                break
        if logger:
            logger(f"NOAA {service}/{kind}/{layer_id}: {got} capitaineries")
    return out


async def _all_docs(coll) -> list[dict]:
    from app.services.isolated_runs import current_run_id
    rid = current_run_id()
    fake = getattr(coll, "docs", None)
    if isinstance(fake, list):
        docs = list(fake)
        if rid:
            docs = [d for d in docs if d.get("run_id") == rid]
        return docs
    q = {"run_id": rid} if rid else {}
    cur = coll.find(q)
    if hasattr(cur, "to_list"):
        return await cur.to_list(50000)
    return [doc async for doc in cur]


async def overlay_shom(
    coll,
    *,
    client: httpx.AsyncClient,
    logger=None,
    fetch_shom: FetchShom | None = None,
    bboxes: tuple[tuple[float, float, float, float], ...] | None = None,
) -> dict:
    osm_pts = [
        d for d in await _all_docs(coll)
        if d.get("osm_id") and d.get("lat") is not None
    ]
    if logger:
        radius_m = int(round(building_radius_km() * 1000))
        logger(f"Overlay SHOM : {len(osm_pts)} OSM en base pour fusion ≤ {radius_m} m")
    inserted = merged = updated = fetched = 0
    seen: set[str] = set()
    for bbox in (bboxes or SHOM_BBOXES):
        try:
            if fetch_shom:
                cands = await fetch_shom(client, bbox)
            else:
                cands = await shom_fetch_catscf6(client, bbox, logger=logger)
        except Exception as exc:
            if logger:
                logger(f"SHOM overlay {bbox}: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        fetched += len(cands)
        for cand in cands:
            sid = cand.get("shom_id")
            if sid in seen:
                continue
            seen.add(sid)
            result = await upsert_shom(coll, cand, time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
            ), osm_pts)
            if result == "inserted":
                inserted += 1
            elif result == "merged":
                merged += 1
            else:
                updated += 1
    return {
        "fetched": fetched,
        "inserted": inserted,
        "merged": merged,
        "updated": updated,
        "unique": len(seen),
    }


async def overlay_noaa(
    coll,
    *,
    client: httpx.AsyncClient,
    logger=None,
    fetch_noaa: FetchNoaa | None = None,
) -> dict:
    pts = [
        d for d in await _all_docs(coll)
        if d.get("lat") is not None and d.get("lon") is not None
    ]
    if logger:
        radius_m = int(round(building_radius_km() * 1000))
        logger(
            f"Overlay NOAA ENC : {len(pts)} bureaux en base pour fusion "
            f"≤ {radius_m} m"
        )
    inserted = merged = updated = 0
    seen: set[str] = set()
    try:
        if fetch_noaa:
            cands = await fetch_noaa(client)
        else:
            cands = await noaa_fetch_harbour_offices(client, logger=logger)
    except Exception as exc:
        if logger:
            logger(f"NOAA overlay: {type(exc).__name__}: {str(exc)[:80]}")
        cands = []
    fetched = len(cands)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for cand in cands:
        nid = cand.get("noaa_id")
        if nid in seen:
            continue
        seen.add(nid)
        result = await upsert_noaa(coll, cand, now_iso, pts)
        if result == "inserted":
            inserted += 1
        elif result == "merged":
            merged += 1
        else:
            updated += 1
    return {
        "fetched": fetched,
        "inserted": inserted,
        "merged": merged,
        "updated": updated,
        "unique": len(seen),
    }


async def build_world_capitaineries(
    *,
    coll,
    cursor_coll,
    state,
    resume: bool = True,
    tiles: tuple[tuple[float, float, float, float], ...] | None = None,
    client: httpx.AsyncClient | None = None,
    throttle_s: float | None = None,
    fetch_tile: FetchTile | None = None,
    fetch_shom: FetchShom | None = None,
    fetch_noaa: FetchNoaa | None = None,
    shom_bboxes: tuple[tuple[float, float, float, float], ...] | None = None,
    skip_shom: bool = False,
    skip_noaa: bool = False,
    run_id: str | None = None,
    recorder=None,
) -> dict:
    """Dump OSM harbour_master (tuiles), overlay SHOM, overlay NOAA ENC."""
    from app.services.isolated_runs import bind_run, reset_run

    token = bind_run(run_id) if run_id else None
    state.running = True
    state.started_at = time.time()
    state.finished_at = None
    state.error = None
    state.logs = []
    state.summary = None
    state.cancel = False
    state.run_id = run_id

    grid = tiles or WORLD_TILES
    pause = _throttle_s(throttle_s)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    hb = HeartbeatWatch(recorder).start(lambda: {
        "progress": state.progress,
        "total": state.total,
        "last_item": getattr(state, "last_item", None),
    })

    try:
        await ensure_indexes(coll, isolated=bool(run_id))
        if not resume:
            await reset_cursor(cursor_coll)
            state.log("Reprise désactivée — curseur tuiles remis à zéro (pas de purge)")

        done = set(await load_done_tiles(cursor_coll)) if resume else set()
        state.total = len(grid) + (0 if skip_shom else 1) + (0 if skip_noaa else 1)
        state.progress = 0
        state.log(
            f"Dump mondial harbour_master : {len(grid)} tuiles, "
            f"{len(done)} déjà faites, throttle={pause:.1f}s"
        )
        if skip_shom or skip_noaa:
            await emit(recorder, "skip_overlay", shom=bool(skip_shom), noaa=bool(skip_noaa))
            state.log(f"Overlay skip SHOM={skip_shom} NOAA={skip_noaa}")

        inserted = updated = fetched_raw = errors = skipped = 0
        named = unnamed = with_phone = with_vhf = 0
        cancelled = False
        own_client = client is None
        http = client or httpx.AsyncClient(headers={"User-Agent": USER_AGENT})
        try:
            for tile in grid:
                if state.cancel:
                    cancelled = True
                    state.log("Stop demandé — dump OSM interrompu")
                    break
                south, west, north, east = tile
                key = tile_key(tile)
                if key in done:
                    skipped += 1
                    state.progress += 1
                    state.log(f"Tuile {key} déjà faite — skip")
                    continue
                state.last_item = key
                hb.update(progress=state.progress, last_item=key)
                await emit(recorder, "tile_start", tile=key,
                           south=south, west=west, north=north, east=east)
                op_meta: dict = {}
                try:
                    await emit(
                        recorder, "overpass_query",
                        bbox=[south, west, north, east],
                        timeout=OVERPASS_TILE_TIMEOUT_S,
                    )
                    if fetch_tile:
                        elements = await fetch_tile(http, tile)
                        op_meta = {
                            "http": None, "mirror": "injected",
                            "latency_ms": 0, "n": len(elements),
                        }
                    else:
                        elements = await fetch_tile_harbour_masters(
                            http, tile, logger=state.log, meta=op_meta)
                except Exception as exc:
                    errors += 1
                    state.log(f"Tuile {key} : {type(exc).__name__}: {str(exc)[:80]}")
                    await emit(
                        recorder, "overpass_result",
                        n=0, http=op_meta.get("http"),
                        mirror=op_meta.get("mirror"),
                        latency_ms=op_meta.get("latency_ms"),
                        error=f"{type(exc).__name__}: {str(exc)[:120]}",
                    )
                    state.progress += 1
                    await asyncio.sleep(pause)
                    continue

                await emit(
                    recorder, "overpass_result",
                    n=len(elements),
                    http=op_meta.get("http"),
                    mirror=op_meta.get("mirror"),
                    latency_ms=op_meta.get("latency_ms"),
                )
                fetched_raw += len(elements)
                tile_ins = tile_upd = tile_skip = 0
                seen = 0
                last_osm = last_name = None
                stopped_mid_tile = False
                for elem in elements:
                    if state.cancel:
                        cancelled = True
                        stopped_mid_tile = True
                        state.log("Stop demandé — interruption entre fiches")
                        break
                    cand = capitainerie_from_overpass(elem)
                    if not cand:
                        tile_skip += 1
                        continue
                    result = await upsert_osm(coll, cand, now_iso)
                    if result == "inserted":
                        inserted += 1
                        tile_ins += 1
                    else:
                        updated += 1
                        tile_upd += 1
                    if cand["name"]:
                        named += 1
                    else:
                        unnamed += 1
                    if cand.get("telephone"):
                        with_phone += 1
                    if cand.get("canal_vhf"):
                        with_vhf += 1
                    seen += 1
                    last_osm = cand.get("osm_id")
                    last_name = cand.get("name")
                    state.last_item = last_osm or key
                    hb.update(progress=state.progress, last_item=state.last_item)
                    if seen % TILE_UPSERT_EVERY == 0:
                        await emit(
                            recorder, "tile_upsert",
                            tile=key, n=seen, total=len(elements),
                            inserted=tile_ins, updated=tile_upd,
                            osm_id=last_osm, name=last_name,
                        )

                if not stopped_mid_tile:
                    await mark_tile_done(cursor_coll, key, now_iso)
                state.progress += 1
                await emit(
                    recorder, "tile_done",
                    tile=key, raw=len(elements),
                    inserted=tile_ins, updated=tile_upd, skipped=tile_skip,
                    cancelled=stopped_mid_tile,
                    osm_id=last_osm, name=last_name,
                )
                state.log(
                    f"Tuile {key} : {len(elements)} brut → +{tile_ins} / ~{tile_upd}"
                )
                if stopped_mid_tile:
                    break
                await asyncio.sleep(pause)

            shom_summary = {
                "fetched": 0, "inserted": 0, "merged": 0, "updated": 0, "unique": 0,
            }
            noaa_summary = {
                "fetched": 0, "inserted": 0, "merged": 0, "updated": 0, "unique": 0,
            }
            if not skip_shom and not state.cancel:
                state.log("Overlay SHOM BUISGL FUNCTN=2 + SMCFAC CATSCF=6")
                shom_summary = await overlay_shom(
                    coll, client=http, logger=state.log,
                    fetch_shom=fetch_shom, bboxes=shom_bboxes,
                )
                state.progress += 1
                state.log(
                    f"SHOM : {shom_summary['unique']} uniques, "
                    f"+{shom_summary['inserted']} / fusion {shom_summary['merged']}"
                )
            if not skip_noaa and not state.cancel:
                state.log("Overlay NOAA ENC Direct BUISGL FUNCTN=2")
                noaa_summary = await overlay_noaa(
                    coll, client=http, logger=state.log, fetch_noaa=fetch_noaa,
                )
                state.progress += 1
                state.log(
                    f"NOAA : {noaa_summary['unique']} uniques, "
                    f"+{noaa_summary['inserted']} / fusion {noaa_summary['merged']}"
                )
        finally:
            if own_client:
                await http.aclose()

        summary = {
            "schema": SCHEMA,
            "tiles_total": len(grid),
            "tiles_skipped": skipped,
            "tiles_errors": errors,
            "fetched_raw": fetched_raw,
            "inserted": inserted,
            "updated": updated,
            "named": named,
            "unnamed": unnamed,
            "with_phone_tag": with_phone,
            "with_vhf_tag": with_vhf,
            "shom": shom_summary,
            "noaa": noaa_summary,
            "resume": resume,
            "cancelled": cancelled or bool(state.cancel),
        }
        state.summary = summary
        state.log(f"Dump capitaineries terminé: {json.dumps(summary)}")
        return summary
    except Exception as exc:
        state.error = f"{type(exc).__name__}: {exc}"
        state.log(f"FATAL: {state.error}")
        raise
    finally:
        await hb.aclose()
        state.finished_at = time.time()
        state.running = False
        if token is not None:
            reset_run(token)
