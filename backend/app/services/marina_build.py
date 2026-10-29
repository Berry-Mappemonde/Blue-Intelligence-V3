"""
Marinas layer — Phase 2.

Data sources:
  - OpenStreetMap via the Overpass API (worldwide)
  - SHOM WFS (metropolitan France + French overseas territories where available)

Build pipeline:
  * Queries around each waypoint of the Berry-Mappemonde official route
  * Corridor pass every ~25 NM along maritime segments
  * Dedup key = normalize(name[:25]) + geohash(precision=6) → newest wins
  * Enrichment placeholder: `enriched=false` (Phase 3 will fill this with TinyFish/OpenRouter)

No third-party heavy deps: geohash + haversine + Overpass QL client are all local.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

# ------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------

NM_PER_METER = 1.0 / 1852.0
METERS_PER_NM = 1852.0

# Metropolitan France + Corsica bbox — SHOM only queries within this envelope
# (SHOM has territorial data outside this, but it is patchy; keep it tight and clear.)
FRANCE_METRO_BBOX = (41.0, -6.0, 52.0, 10.0)  # (south, west, north, east)

# Public Overpass endpoints — round-robin on failure.
# NB verified 2026-08-24 from this container's egress IP:
#   * overpass-api.de   → TCP CONN REFUSED at network layer (both /24s of Hetzner IPs blocked)
#   * overpass.kumi.systems → TCP connects but /api/interpreter returns 502 for all queries
#   * overpass.openstreetmap.fr → WORKS. Used as primary.
OVERPASS_ENDPOINTS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_STATUS_URL = "https://overpass-api.de/api/status"

SHOM_WFS = "https://services.data.shom.fr/INSPIRE/wfs"

# SHOM public INSPIRE WFS — verified 2026-08. Free (Licence Ouverte Etalab), no auth.
# Data is stored in EPSG:3857 (Web Mercator meters); bbox must be given as
# `west,south,east,north,EPSG:4326` (LON-FIRST despite the WFS 2.0 axis-order convention).
# The `INFORMATIONS_PORTUAIRES_BDD_WFS:smcfac_point` layer is the S-57 "Small Craft Facilities"
# — marinas, yacht clubs, pontoons, workshops. Available for mainland France + Antilles + Polynesia
# + Reunion/Mayotte. French Guiana not covered.
SHOM_TYPENAMES = [
    "INFORMATIONS_PORTUAIRES_BDD_WFS:smcfac_point",
    "INFORMATIONS_PORTUAIRES_BDD_WFS:hrbfac_point",
]

# S-57 "catscf" (category of small craft facility) → French label used as fallback name/tag.
SHOM_CATSCF_LABELS = {
    "1": "Ponton",
    "2": "Yacht club",
    "3": "Marina",
    "4": "Bassin de plaisance",
    "5": "Port",
    "6": "Capitainerie",
    "7": "Ship chandler",
    "8": "Station carburant",
    "9": "Service de mise à l'eau",
    "10": "Déchets",
    "11": "Grue",
    "12": "Mécanicien",
    "13": "Atelier",
    "14": "Sanitaires",
    "16": "Slip",
    "17": "Hangar",
    "18": "Zone d'hivernage",
    "20": "Cales",
    "22": "Café / Bar",
    "23": "Bureau des douanes",
    "24": "Restaurant",
    "25": "Hôtel",
    "26": "Épicerie",
    "27": "Bureau de tourisme",
    "28": "Poste de secours",
    "29": "Base nautique",
    "30": "Point d'eau",
}


from app.config import DATA_DIR

CURATED_SEED_FILE = DATA_DIR / "curated_marinas.json"

# Compliant User-Agent per https://wiki.openstreetmap.org/wiki/API_usage_policy
# — identify the app + a contact so mirror operators can reach us if needed.
USER_AGENT = (
    "BerryMappemonde-BlueIntelligence/1.0 "
    "(+https://berrymappemonde.org; contact: clementfilisetti@berrymappemonde.org)"
)

# Tags we keep on the marina document (raw useful OSM tags)
KEPT_TAGS = (
    "vhf_channel", "vhf", "seamark:harbour:category", "seamark:type",
    "phone", "contact:phone", "email", "contact:email",
    "website", "contact:website", "url",
    "opening_hours", "operator", "operator:type",
    "capacity", "capacity:persons", "seamark:harbour:capacity",
    "max_depth", "depth", "seamark:harbour:draught",
    "fee", "charge",
    "shower", "toilets", "drinking_water", "electricity", "fuel",
    "sanitary_dump_station", "pumpout", "waste_disposal",
    "wheelchair", "harbour", "harbour:type", "mooring", "leisure",
    "shop", "restaurant", "wifi", "internet_access",
    "addr:city", "addr:street", "addr:postcode", "addr:country",
    "description",
)


# ------------------------------------------------------------------------
# Small utils — geohash + haversine + name normalisation
# ------------------------------------------------------------------------

_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_encode(lat: float, lon: float, precision: int = 6) -> str:
    """Standard geohash (base32) encoding. Precision 6 ~= 1.2 km × 0.61 km at the equator."""
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    bit = 0
    ch = 0
    even = True  # start with longitude bit
    out = []
    while len(out) < precision:
        if even:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                ch |= 1 << (4 - bit)
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                ch |= 1 << (4 - bit)
                lat_range[0] = mid
            else:
                lat_range[1] = mid
        even = not even
        bit += 1
        if bit == 5:
            out.append(_GEOHASH_ALPHABET[ch])
            bit = 0
            ch = 0
    return "".join(out)


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    R = 6371008.8  # WGS-84 mean earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    d_m = 2 * R * math.asin(math.sqrt(a))
    return d_m * NM_PER_METER


def normalize_name(s: str) -> str:
    """
    Aggressive normalisation for dedup: strip accents, lowercase, remove
    punctuation, collapse whitespace, cap length.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s).strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s[:25]


def dedup_key(name: str, lat: float, lon: float) -> str:
    return f"{normalize_name(name)}@{geohash_encode(lat, lon, 6)}"


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    s, w, n, e = bbox
    return s <= lat <= n and w <= lon <= e


# ------------------------------------------------------------------------
# Overpass QL client — polite, sequential
# ------------------------------------------------------------------------


def _overpass_query_body(lat: float, lon: float, radius_m: int) -> str:
    """
    Build a bulk Overpass QL query covering:
      - leisure=marina (nodes/ways/relations)
      - harbour=yes
      - seamark:type=harbour
    All within a circle of radius_m meters around (lat, lon).
    Emits `out center;` so ways/relations get a computed centroid.
    """
    r = int(radius_m)
    return f"""[out:json][timeout:60];
(
  node["leisure"="marina"](around:{r},{lat:.5f},{lon:.5f});
  way["leisure"="marina"](around:{r},{lat:.5f},{lon:.5f});
  relation["leisure"="marina"](around:{r},{lat:.5f},{lon:.5f});
  node["harbour"="yes"](around:{r},{lat:.5f},{lon:.5f});
  way["harbour"="yes"](around:{r},{lat:.5f},{lon:.5f});
  node["seamark:type"="harbour"](around:{r},{lat:.5f},{lon:.5f});
);
out center tags;
""".strip()


def _overpass_bbox_body(south: float, west: float, north: float, east: float) -> str:
    """
    Bulk Overpass QL query for marinas/harbours inside a bbox.
    Much more efficient than many small `around:` queries when we need to cover
    a whole coastal region (e.g. metropolitan France).
    """
    return f"""[out:json][timeout:120];
(
  node["leisure"="marina"]({south:.5f},{west:.5f},{north:.5f},{east:.5f});
  way["leisure"="marina"]({south:.5f},{west:.5f},{north:.5f},{east:.5f});
  relation["leisure"="marina"]({south:.5f},{west:.5f},{north:.5f},{east:.5f});
  node["harbour"="yes"]({south:.5f},{west:.5f},{north:.5f},{east:.5f});
  way["harbour"="yes"]({south:.5f},{west:.5f},{north:.5f},{east:.5f});
  node["seamark:type"="harbour"]({south:.5f},{west:.5f},{north:.5f},{east:.5f});
);
out center tags;
""".strip()


async def overpass_fetch_bbox(
    south: float,
    west: float,
    north: float,
    east: float,
    client: httpx.AsyncClient,
    max_retries: int = 5,
    logger=None,
    body: str | None = None,
    meta: dict | None = None,
) -> list[dict]:
    """
    Fetch all marina/harbour features inside a bbox. Retries across endpoints.
    Returns raw Overpass `elements` list. Pass a custom `body` (Overpass QL)
    to reuse the same client for other feature classes (e.g. anchorages).
    `meta` is filled with http / mirror / latency_ms / n of the successful call.
    """
    body = body or _overpass_bbox_body(south, west, north, east)
    last_err: Exception | None = None
    for attempt in range(max_retries):
        endpoint = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        t0 = time.time()
        try:
            r = await client.post(
                endpoint,
                data={"data": body},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=httpx.Timeout(connect=10.0, read=120.0, write=15.0, pool=8.0),
            )
            latency_ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                payload = r.json() if r.content else {}
                remark = str((payload or {}).get("remark") or "")
                if "timed out" in remark.lower():
                    raise TimeoutError(f"Overpass remark timeout on {endpoint}: {remark[:120]}")
                elements = payload.get("elements") or []
                if meta is not None:
                    meta.update({
                        "http": 200,
                        "mirror": endpoint,
                        "latency_ms": latency_ms,
                        "n": len(elements),
                    })
                return elements
            if meta is not None:
                meta.update({
                    "http": r.status_code,
                    "mirror": endpoint,
                    "latency_ms": latency_ms,
                })
            if r.status_code in (429, 502, 503, 504):
                sleep_s = 3 * (2 ** min(attempt, 3))
                if logger:
                    logger(f"Overpass bbox {r.status_code} on {endpoint}, backoff {sleep_s}s")
                await asyncio.sleep(sleep_s)
                continue
            last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        except (httpx.TimeoutException, httpx.HTTPError) as e:
            last_err = e
            if meta is not None:
                meta.update({
                    "http": None,
                    "mirror": endpoint,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": type(e).__name__,
                })
            if logger and attempt < 2:
                logger(f"Overpass bbox {type(e).__name__} on {endpoint}: {str(e)[:60]}")
            await asyncio.sleep(2)
    if last_err:
        raise last_err
    return []


def cluster_points_to_bboxes(
    points: list[tuple[float, float, str]],
    radius_nm: float,
    max_bbox_span_nm: float = 500.0,
) -> list[dict]:
    """
    Greedy clustering of (lat, lon, label) points into a list of bounding boxes.
    Each bbox spans at most `max_bbox_span_nm` NM in either direction and pads by
    `radius_nm` NM. Returns list of {south,west,north,east,labels,pt_count}.
    Purpose: replace hundreds of `around:` queries with a handful of bbox queries.
    """
    pad_deg = radius_nm / 60.0
    max_span_deg = max_bbox_span_nm / 60.0
    unassigned = list(points)
    clusters: list[list[tuple[float, float, str]]] = []
    while unassigned:
        seed = unassigned.pop(0)
        cluster = [seed]
        s_lat, s_lon, _ = seed
        i = 0
        while i < len(unassigned):
            la, lo, lb = unassigned[i]
            # accept the point if adding it keeps the bbox within max_span
            all_lats = [p[0] for p in cluster] + [la]
            all_lons = [p[1] for p in cluster] + [lo]
            span_lat = max(all_lats) - min(all_lats)
            span_lon = max(all_lons) - min(all_lons)
            if span_lat <= max_span_deg and span_lon <= max_span_deg:
                cluster.append(unassigned.pop(i))
            else:
                i += 1
        clusters.append(cluster)
    out = []
    for cl in clusters:
        lats = [p[0] for p in cl]
        lons = [p[1] for p in cl]
        # Longitude padding must widen with latitude (1' of lon < 1 NM off-equator)
        max_abs_lat = min(85.0, max(abs(la) for la in lats))
        pad_lon = pad_deg / max(0.2, math.cos(math.radians(max_abs_lat)))
        out.append({
            "south": max(-90.0, min(lats) - pad_deg),
            "west": max(-180.0, min(lons) - pad_lon),
            "north": min(90.0, max(lats) + pad_deg),
            "east": min(180.0, max(lons) + pad_lon),
            "labels": [p[2] for p in cl],
            "points": cl,
            "pt_count": len(cl),
        })
    return out


def _marina_doc(cand: dict, wps, now_iso: str) -> dict:
    """Build the canonical marina document for a candidate."""
    prio, nearest_wp, dist_nm = priority_for(cand["lat"], cand["lon"], wps)
    return {
        "_id": str(uuid.uuid4()),
        "name": cand["name"],
        "lat": cand["lat"],
        "lon": cand["lon"],
        "source": cand["source"],
        "osm_id": cand.get("osm_id"),
        "tags": cand.get("tags") or {},
        "priority": prio,
        "nearest_waypoint": {
            "id": nearest_wp.id,
            "name": nearest_wp.name,
            "kind": nearest_wp.kind,
            "distance_nm": round(dist_nm, 2),
        },
        "dedup_key": dedup_key(cand["name"], cand["lat"], cand["lon"]),
        "fetched_at": now_iso,
        "enriched": False,
        "stale": False,
    }


async def flush_docs_incremental(
    coll,
    docs: list[dict],
    *,
    preserve_fields: tuple = ("enriched", "enrichment", "enriched_at"),
) -> None:
    """
    Crash-safe incremental persistence (Phase 8): upsert each doc by dedup_key
    as soon as its batch is fetched, so a backend reload mid-build (hot reload,
    deploy…) loses nothing. Best-effort — the final dedup pass at the end of a
    build re-writes everything with the full source/distance preference logic.
    """
    for doc in docs:
        try:
            from app.services.isolated_runs import current_run_id, stamp
            rid = current_run_id()
            payload = stamp(doc, source_id=doc.get("dedup_key"), wrote_flag="wrote_marinas")
            q = {"dedup_key": payload["dedup_key"]}
            if rid:
                q["run_id"] = rid
            existing = await coll.find_one(q)
            if existing:
                d2 = dict(payload)
                d2["_id"] = existing["_id"]
                for f in preserve_fields:
                    if existing.get(f):
                        d2[f] = existing[f]
                await coll.replace_one({"_id": existing["_id"]}, d2)
            else:
                if rid:
                    payload["_id"] = f"{rid}:{payload['dedup_key']}"
                await coll.insert_one(payload)
        except Exception:
            pass


async def fetch_corridor_band(
    corridor: list["CorridorPoint"],
    corridor_radius_nm: float,
    client: httpx.AsyncClient,
    *,
    state: "BuildState",
    bbox_body_builder=None,
    point_fetcher=None,
    max_bbox_span_nm: float = 240.0,
    throttle_s: float = 3.0,
    on_batch=None,
) -> tuple[list[dict], int]:
    """
    True spatial-buffer corridor coverage (±corridor_radius_nm band — Phase 8).

    1. Corridor sample points are greedily clustered into padded bboxes
       (cluster_points_to_bboxes) — a handful of bulk bbox queries instead of
       hundreds of `around:` calls.
    2. Each bbox runs ONE Overpass bbox query; on failure it falls back to
       per-point `around:` queries for that bbox's own points.
    3. Exact band post-filter: an element is kept only if its distance to the
       nearest corridor sample is <= sqrt(r² + (step/2)²) — which keeps every
       feature inside the true ±r band around the route polyline.

    Returns (unique_elements, error_count).
    """
    if not corridor:
        return [], 0
    fetch_point = point_fetcher or overpass_fetch
    pts = [(cp.lat, cp.lon, f"c{i}") for i, cp in enumerate(corridor)]
    bboxes = cluster_points_to_bboxes(pts, radius_nm=corridor_radius_nm, max_bbox_span_nm=max_bbox_span_nm)
    state.total += len(bboxes)
    state.log(
        f"Corridor band: {len(corridor)} sample points → {len(bboxes)} bbox queries "
        f"(true ±{corridor_radius_nm:.0f} NM buffer = {corridor_radius_nm*2:.0f} NM band)"
    )

    # Sample step estimate (median of consecutive distances) for the exact cutoff
    steps = [
        haversine_nm(corridor[i].lat, corridor[i].lon, corridor[i + 1].lat, corridor[i + 1].lon)
        for i in range(min(len(corridor) - 1, 50))
    ]
    step_nm = sorted(steps)[len(steps) // 2] if steps else corridor_radius_nm
    band_cutoff_nm = math.sqrt(corridor_radius_nm ** 2 + (step_nm / 2.0) ** 2)

    radius_m = int(corridor_radius_nm * METERS_PER_NM)
    seen: set[str] = set()
    kept: list[dict] = []
    errors = 0

    def _elem_latlon(elem: dict):
        if elem.get("type") == "node":
            return elem.get("lat"), elem.get("lon")
        c = elem.get("center") or {}
        return c.get("lat"), c.get("lon")

    for bi, bb in enumerate(bboxes):
        label = f"bbox {bi + 1}/{len(bboxes)}"
        elements: list[dict] = []
        try:
            body = (
                bbox_body_builder(bb["south"], bb["west"], bb["north"], bb["east"])
                if bbox_body_builder else None
            )
            elements = await overpass_fetch_bbox(
                bb["south"], bb["west"], bb["north"], bb["east"], client, logger=state.log, body=body,
            )
            state.log(
                f"Corridor {label} ({bb['pt_count']} pts, "
                f"{bb['south']:.1f},{bb['west']:.1f}→{bb['north']:.1f},{bb['east']:.1f}): "
                f"{len(elements)} raw"
            )
        except Exception as e:
            state.log(
                f"Corridor {label} bbox query failed ({type(e).__name__}: {str(e)[:60]}) — "
                f"falling back to per-point around:"
            )
            for (la, lo, plabel) in bb.get("points") or []:
                try:
                    elements.extend(await fetch_point(la, lo, radius_m, client, logger=state.log))
                except Exception as pe:
                    errors += 1
                    state.log(f"Corridor {plabel}: {type(pe).__name__}: {str(pe)[:60]}")
                await asyncio.sleep(throttle_s)

        # Pre-filter corridor points near this bbox (perf), then exact band filter
        pad_lat = band_cutoff_nm / 60.0 * 2
        cand_pts = [
            cp for cp in corridor
            if (bb["south"] - pad_lat) <= cp.lat <= (bb["north"] + pad_lat)
            and (bb["west"] - pad_lat * 3) <= cp.lon <= (bb["east"] + pad_lat * 3)
        ] or corridor
        in_band = 0
        batch: list[dict] = []
        for elem in elements:
            ekey = f"{elem.get('type')}/{elem.get('id')}"
            if ekey in seen:
                continue
            la, lo = _elem_latlon(elem)
            if la is None or lo is None:
                continue
            d = min(haversine_nm(la, lo, cp.lat, cp.lon) for cp in cand_pts)
            if d <= band_cutoff_nm:
                seen.add(ekey)
                kept.append(elem)
                batch.append(elem)
                in_band += 1
        if in_band:
            state.log(f"Corridor {label}: {in_band} kept within ±{corridor_radius_nm:.0f} NM band")
        if on_batch and batch:
            try:
                await on_batch(batch)
            except Exception as pe:
                state.log(f"Corridor {label} incremental persist failed: {type(pe).__name__}")
        state.progress += 1
        await asyncio.sleep(throttle_s)
    return kept, errors


async def overpass_status(client: httpx.AsyncClient, logger=None) -> dict:
    """
    Fetch overpass-api.de's /api/status. The response is plain text like:
        Connected as: 1234567
        Current time: 2026-08-24T07:11:22Z
        Rate limit: 2
        3 slots available now.
        Currently running queries (pid, space limit, time limit, start time):
    We parse it into {slots_available, waits: [seconds], running: int}.
    """
    out = {"slots_available": None, "waits": [], "running": 0, "raw": ""}
    try:
        r = await client.get(
            OVERPASS_STATUS_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        if r.status_code != 200:
            if logger:
                logger(f"[overpass-status] HTTP {r.status_code}")
            return out
        out["raw"] = r.text
        for line in r.text.splitlines():
            line = line.strip()
            m1 = re.match(r"(\d+)\s+slots?\s+available\s+now", line)
            if m1:
                out["slots_available"] = int(m1.group(1))
                continue
            m2 = re.match(r"Slot available after:.*in\s+(\d+)\s+seconds", line)
            if m2:
                out["waits"].append(int(m2.group(1)))
                continue
            m3 = re.match(r"Currently running queries \((\d+)", line)
            if m3:
                out["running"] = int(m3.group(1))
        if logger:
            summary = f"slots={out['slots_available']} waits={out['waits']} running={out['running']}"
            logger(f"[overpass-status] {summary}")
    except Exception as e:
        if logger:
            logger(f"[overpass-status] error: {type(e).__name__}: {str(e)[:80]}")
    return out


async def overpass_await_slot(client: httpx.AsyncClient, logger=None, cap_s: int = 60) -> bool:
    """
    Consult /api/status; if no slot available, sleep up to `cap_s` seconds
    waiting for one, respecting the reported wait times.
    Returns True if a slot is likely free, False if we gave up.
    """
    st = await overpass_status(client, logger=logger)
    if st["slots_available"] and st["slots_available"] > 0:
        return True
    # No status info or 0 slots — wait a bit, respecting reported wait.
    wait = min(cap_s, (min(st["waits"]) if st["waits"] else 5))
    if logger:
        logger(f"[overpass-status] no slot; sleeping {wait}s")
    await asyncio.sleep(max(1, wait))
    st = await overpass_status(client, logger=logger)
    return bool(st["slots_available"] and st["slots_available"] > 0)


async def overpass_fetch(
    lat: float,
    lon: float,
    radius_m: int,
    client: httpx.AsyncClient,
    max_retries: int = 3,
    logger=None,
) -> list[dict]:
    """
    Compliant `around:` query, one small radius at a time, sequential.
    Steps per attempt:
      1. GET /api/status → wait for a free slot (or up to 60s).
      2. POST /api/interpreter with the tiny query and long UA.
      3. On 429/504: honour the returned Retry-After / cool-down before retrying.
      4. On 500/502: rotate to the next mirror.
    """
    body = _overpass_query_body(lat, lon, radius_m)
    last_err: Exception | None = None
    for attempt in range(max_retries):
        endpoint = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        try:
            # Preflight slot check — only meaningful for the canonical .de endpoint,
            # but harmless to skip for mirrors.
            if endpoint.startswith("https://overpass-api.de"):
                ok = await overpass_await_slot(client, logger=logger)
                if not ok and logger:
                    logger("[overpass] proceeding despite no reported slot")
            r = await client.post(
                endpoint,
                data={"data": body},
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=httpx.Timeout(connect=10.0, read=90.0, write=15.0, pool=8.0),
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("elements") or []
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                sleep_s = int(retry_after) if retry_after and retry_after.isdigit() else 30
                if logger:
                    logger(f"[overpass] 429 on {endpoint}, Retry-After={sleep_s}s")
                await asyncio.sleep(sleep_s)
                continue
            if r.status_code in (502, 503, 504):
                sleep_s = 5 * (attempt + 1)
                if logger:
                    logger(f"[overpass] {r.status_code} on {endpoint}, backoff {sleep_s}s")
                await asyncio.sleep(sleep_s)
                continue
            if r.status_code == 500:
                # 500 with generic-UA is often an auto-ban; with the compliant UA it means the
                # server actually rejected the query. Log and give up on this endpoint.
                if logger:
                    logger(f"[overpass] 500 on {endpoint}: {r.text[:120]}")
                last_err = RuntimeError(f"HTTP 500: {r.text[:120]}")
                await asyncio.sleep(3)
                continue
            last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        except (httpx.TimeoutException, httpx.HTTPError) as e:
            last_err = e
            if logger and attempt == 0:
                logger(f"[overpass] {type(e).__name__} on {endpoint}: {str(e)[:60]}")
            await asyncio.sleep(3)
    if last_err:
        raise last_err
    return []


def _overpass_elem_to_marina(elem: dict) -> dict | None:
    """Convert one Overpass element into a marina candidate. Returns None if unusable."""
    tags = elem.get("tags") or {}
    typ = elem.get("type")
    if typ == "node":
        lat = elem.get("lat")
        lon = elem.get("lon")
    else:
        center = elem.get("center") or {}
        lat = center.get("lat")
        lon = center.get("lon")
    if lat is None or lon is None:
        return None
    name = (
        tags.get("name")
        or tags.get("name:fr")
        or tags.get("name:en")
        or tags.get("official_name")
        or tags.get("alt_name")
    )
    if not name:
        # Try a fallback: harbour tag with the road/addr name
        name = (
            tags.get("addr:city")
            or tags.get("harbour:name")
            or f"Marina @ {lat:.3f},{lon:.3f}"
        )
    # Only keep useful tag subset
    kept = {k: v for k, v in tags.items() if k in KEPT_TAGS or k.startswith("seamark:")}
    return {
        "name": str(name)[:120],
        "lat": float(lat),
        "lon": float(lon),
        "source": "openstreetmap",
        "osm_id": f"{typ}/{elem.get('id')}",
        "tags": kept,
    }


# ------------------------------------------------------------------------
# SHOM WFS client (best-effort; failures are non-fatal)
# ------------------------------------------------------------------------

async def shom_fetch(
    bbox: tuple[float, float, float, float],
    client: httpx.AsyncClient,
    logger=None,
) -> list[dict]:
    """
    Query SHOM WFS for small-craft-facility / harbour points inside `bbox`.
    bbox is (south, west, north, east) in WGS84 — we convert to LON-FIRST
    per SHOM's expected axis order.
    Coordinates in the response are EPSG:3857 metres → converted to WGS84 here.
    Returns list of {name, lat, lon, source='shom', osm_id=None, tags{}}.
    """
    s, w, n, e = bbox
    # SHOM expects west,south,east,north (LON-FIRST), not the WFS 2.0 default
    bbox_str = f"{w:.6f},{s:.6f},{e:.6f},{n:.6f},EPSG:4326"
    out: list[dict] = []
    for typename in SHOM_TYPENAMES:
        try:
            params = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typenames": typename,
                "bbox": bbox_str,
                "outputFormat": "application/json",
                "count": "2000",
            }
            r = await client.get(
                SHOM_WFS,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=60,
            )
            if r.status_code != 200 or "json" not in r.headers.get("content-type", "").lower():
                if logger:
                    logger(f"SHOM {typename}: HTTP {r.status_code}, ct={r.headers.get('content-type')}")
                continue
            fc = r.json()
            feats = fc.get("features") or []
            if logger:
                logger(f"SHOM {typename}: {len(feats)} features returned "
                       f"(totalFeatures={fc.get('totalFeatures')})")
            for f in feats:
                geom = f.get("geometry") or {}
                props = f.get("properties") or {}
                coords = geom.get("coordinates")
                if geom.get("type") == "Point" and coords:
                    x, y = coords[:2]
                elif geom.get("type") == "MultiPoint" and coords:
                    x, y = coords[0][:2]
                else:
                    continue
                # Response comes in EPSG:3857 Web Mercator despite srsName request
                lat, lon = _merc_to_wgs84(float(x), float(y))
                # Name resolution:
                # 1. objnam (S-57 name) if present
                # 2. inform / ninfom (informative text)
                # 3. SHOM cst / toponyme (SPM_PORTS_WFS style)
                # 4. Fallback: S-57 category label + rounded coordinates
                catscf = str(props.get("catscf") or "").strip()
                cat_label = SHOM_CATSCF_LABELS.get(catscf, f"cat {catscf}") if catscf else "SMCFAC"
                name = (
                    props.get("objnam")
                    or props.get("nobjnm")
                    or props.get("inform")
                    or props.get("toponyme")
                    or f"{cat_label} SHOM @ {lat:.4f},{lon:.4f}"
                )
                # Keep the raw + labelled tags for the UI
                tags = {"shom:catscf": catscf, "shom:category": cat_label}
                for k, v in props.items():
                    if v is None or isinstance(v, (dict, list)):
                        continue
                    val = str(v).strip()
                    if not val or val in ("null", "None"):
                        continue
                    tags[f"shom:{k}"] = val[:120]
                out.append({
                    "name": str(name)[:120],
                    "lat": lat,
                    "lon": lon,
                    "source": "shom",
                    "osm_id": None,
                    "tags": tags,
                })
        except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
            if logger:
                logger(f"SHOM {typename}: {type(e).__name__}: {str(e)[:100]}")
            continue
    if logger and not out:
        logger("SHOM: no candidates returned from any typename")
    return out


def _merc_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Convert EPSG:3857 Web Mercator meters → WGS84 (lat, lon)."""
    lon = x / 20037508.34 * 180.0
    lat = math.atan(math.exp(y / 20037508.34 * math.pi)) * 360.0 / math.pi - 90.0
    return lat, lon


# ------------------------------------------------------------------------
# Route helpers — waypoints, corridor sampling, priority assignment
# ------------------------------------------------------------------------


@dataclass
class Waypoint:
    id: str
    name: str
    lat: float
    lon: float
    kind: str  # "escale" | "intermediate"


@dataclass
class CorridorPoint:
    lat: float
    lon: float


def load_route(route_path: Path) -> tuple[list[Waypoint], list[list[tuple[float, float]]]]:
    """Return (waypoints, maritime_segments).
    - waypoints: 17 stops + 19 intermediates, in file order.
    - maritime_segments: list of polylines [(lat, lon), ...] for maritime segments only.
    """
    data = json.loads(route_path.read_text(encoding="utf-8"))
    wps: list[Waypoint] = []
    lines: list[list[tuple[float, float]]] = []
    for f in data.get("features") or []:
        g = f.get("geometry") or {}
        p = f.get("properties") or {}
        if g.get("type") == "Point":
            lon, lat = g["coordinates"][:2]
            wps.append(Waypoint(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"berry-wp:{p.get('name','')}")),
                name=str(p.get("name") or "waypoint"),
                lat=float(lat), lon=float(lon),
                kind=str(p.get("point_type") or "intermediate"),
            ))
        elif g.get("type") == "LineString" and p.get("type") == "maritime":
            lines.append([(float(la), float(lo)) for lo, la in g["coordinates"]])
    return wps, lines


def sample_corridor(lines: list[list[tuple[float, float]]], step_nm: float = 25.0) -> list[CorridorPoint]:
    """Walk each maritime polyline and drop a point every step_nm nautical miles."""
    out: list[CorridorPoint] = []
    step_m = step_nm * METERS_PER_NM
    for poly in lines:
        acc = 0.0
        for i in range(len(poly) - 1):
            la1, lo1 = poly[i]
            la2, lo2 = poly[i + 1]
            seg_nm = haversine_nm(la1, lo1, la2, lo2)
            seg_m = seg_nm * METERS_PER_NM
            if seg_m == 0:
                continue
            # advance in fractions
            t = 0.0
            while acc + (1 - t) * seg_m >= step_m:
                # distance we need on this segment starting from current t
                need = step_m - acc
                # fraction of the segment we still need to travel
                dt = need / seg_m
                t += dt
                lat = la1 + (la2 - la1) * t
                lon = lo1 + (lo2 - lo1) * t
                out.append(CorridorPoint(lat=lat, lon=lon))
                acc = 0.0
            acc += (1 - t) * seg_m
    return out


def priority_for(lat: float, lon: float, wps: list[Waypoint]) -> tuple[int, Waypoint, float]:
    """
    Return (priority, nearest_waypoint, distance_nm).
    Priority: 1 if within priority_escale_nm of an escale, 2 if within
    that radius of an intermediate, else 3 (corridor).
    """
    from app.core.run_rules import get_rule
    near_nm = float(get_rule("marinas.priority_escale_nm", 15.0))
    best_wp: Waypoint | None = None
    best_d = math.inf
    for w in wps:
        d = haversine_nm(lat, lon, w.lat, w.lon)
        if d < best_d:
            best_d = d
            best_wp = w
    assert best_wp is not None
    if best_d <= near_nm and best_wp.kind == "escale":
        prio = 1
    elif best_d <= near_nm and best_wp.kind == "intermediate":
        prio = 2
    else:
        prio = 3
    return prio, best_wp, best_d


# ------------------------------------------------------------------------
# Build orchestrator (async task-friendly). Stateless — takes a Mongo collection
# and a logger callback. State/progress is held by the caller.
# ------------------------------------------------------------------------


# Unified build state (see app/core/tasks.py)
from app.core.tasks import BuildState  # noqa: E402


def load_curated_marinas() -> list[dict]:
    """
    Load the curated fallback list of well-known marinas.
    Used as an "always on" data source so the UI has something to show even
    when OSM/Overpass is unreachable. Marked source='curated' so it's clear.
    """
    if not CURATED_SEED_FILE.exists():
        return []
    try:
        data = json.loads(CURATED_SEED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for m in data.get("marinas", []):
        out.append({
            "name": m.get("name") or "",
            "lat": float(m["lat"]),
            "lon": float(m["lon"]),
            "source": "curated",
            "osm_id": None,
            "tags": m.get("tags") or {},
        })
    return out


async def build_marinas(
    *,
    marinas_coll,
    route_path: Path,
    radius_nm: float,
    state: BuildState,
    include_corridor: bool = True,
    corridor_step_nm: float = 25.0,
    corridor_radius_nm: float = 25.0,
) -> dict:
    """
    Full build: OSM around every waypoint + true ±corridor_radius_nm spatial
    buffer along the maritime corridor (bbox-batched via cluster_points_to_bboxes
    + overpass_fetch_bbox, exact band post-filter, per-point fallback) + SHOM,
    dedup, upsert into `marinas_coll`. Returns summary.

    Corridor 50 NM band (Phase 8): corridor points sampled every
    `corridor_step_nm` NM (default 25) along maritime segments, covered with a
    continuous ±`corridor_radius_nm` NM buffer (default 25 → 50 NM total band).
    Waypoints keep the focused `radius_nm` (stop areas).
    """
    state.running = True
    state.started_at = time.time()
    state.finished_at = None
    state.error = None
    state.logs = []
    state.summary = None

    try:
        wps, maritime_lines = load_route(route_path)
        corridor = sample_corridor(maritime_lines, step_nm=corridor_step_nm) if include_corridor else []

        # Waypoints keep small per-point `around:` queries (focused stop areas);
        # the corridor is covered by the bbox-batched band pass below.
        query_targets: list[tuple[str, float, float, str]] = []
        for w in wps:
            query_targets.append((f"wp:{w.name}", w.lat, w.lon, "waypoint"))

        state.total = len(query_targets)
        state.progress = 0
        state.log(
            f"Loaded route: {len(wps)} waypoints "
            f"({sum(1 for w in wps if w.kind=='escale')} escales, {sum(1 for w in wps if w.kind=='intermediate')} intermediates), "
            f"{len(maritime_lines)} maritime segments → {len(corridor)} corridor points (step={corridor_step_nm} NM, corridor={'on' if include_corridor else 'off'})"
        )
        state.log(
            f"Waypoint radius = {radius_nm:.1f} NM  |  corridor buffer = ±{corridor_radius_nm:.0f} NM "
            f"({corridor_radius_nm*2:.0f} NM total band)"
        )

        candidates: list[dict] = []
        now_build_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        radius_m = int(radius_nm * METERS_PER_NM)
        overpass_errors = 0

        # ---- Curated seed pass (always) — provides fallback marinas at every escale ----
        curated = load_curated_marinas()
        candidates.extend(curated)
        state.log(f"Curated seed: loaded {len(curated)} known marinas")

        async with httpx.AsyncClient() as client:
            # ---- OSM pass ----
            # Overpass Acceptable-Use compliance:
            #   * concurrency = 1 (strictly sequential)
            #   * throttle 3s between requests
            #   * per-request /api/status pre-flight (see overpass_await_slot)
            #   * compliant UA with contact
            counter = {"done": 0}

            for label, lat, lon, _kind in query_targets:
                try:
                    elements = await overpass_fetch(lat, lon, radius_m, client, logger=state.log)
                    n_kept = 0
                    for elem in elements:
                        m = _overpass_elem_to_marina(elem)
                        if m:
                            candidates.append(m)
                            n_kept += 1
                    if n_kept:
                        state.log(f"OSM {label} ({lat:.3f},{lon:.3f}): {n_kept} candidates")
                        # Crash-safe: persist this waypoint's finds immediately
                        await flush_docs_incremental(
                            marinas_coll,
                            [_marina_doc(c, wps, now_build_iso) for c in candidates[-n_kept:]],
                        )
                except Exception as e:
                    overpass_errors += 1
                    state.log(f"OSM {label}: {type(e).__name__}: {str(e)[:70]}")
                counter["done"] += 1
                state.progress = counter["done"]
                await asyncio.sleep(3.0)  # AUP-compliant throttle

            # ---- Corridor band pass (true ±corridor_radius_nm spatial buffer) ----
            if include_corridor and corridor:
                async def _persist_marina_batch(batch: list[dict]):
                    docs = []
                    for elem in batch:
                        m = _overpass_elem_to_marina(elem)
                        if m:
                            docs.append(_marina_doc(m, wps, now_build_iso))
                    if docs:
                        await flush_docs_incremental(marinas_coll, docs)

                corr_elements, corr_errors = await fetch_corridor_band(
                    corridor, corridor_radius_nm, client, state=state,
                    on_batch=_persist_marina_batch,
                )
                overpass_errors += corr_errors
                n_corr = 0
                for elem in corr_elements:
                    m = _overpass_elem_to_marina(elem)
                    if m:
                        candidates.append(m)
                        n_corr += 1
                state.log(f"Corridor band total: {n_corr} marina candidates")

            # ---- SHOM pass (French territory: mainland + DROM-COM) ----
            # SHOM's INFORMATIONS_PORTUAIRES layer covers metropolitan France + Corsica +
            # Antilles + Polynesia + Reunion / Mayotte. We query per-region so each bbox
            # stays small enough for the server (single global bbox would return >10k features).
            shom_errors = 0
            shom_regions = [
                ("Métropole+Corsica", (-6.0, 41.0, 10.5, 52.0)),
                ("Antilles+Saint-Pierre", (-63.5, 14.0, -55.0, 47.5)),
                ("Guyane", (-54.5, 2.0, -51.0, 6.5)),
                ("Polynésie", (-155.0, -25.0, -134.0, -7.0)),
                ("Réunion+Mayotte+TAAF", (41.0, -25.0, 58.0, -10.0)),
            ]
            # Each bbox is (west, south, east, north) but shom_fetch() takes (south, west, north, east)
            shom_total = 0
            for label, (w, s, e, n) in shom_regions:
                bbox = (s, w, n, e)
                try:
                    shom_pts = await shom_fetch(bbox, client, logger=state.log)
                    kept = 0
                    for sp in shom_pts:
                        near_wp = any(
                            haversine_nm(sp["lat"], sp["lon"], wp.lat, wp.lon) <= radius_nm
                            for wp in wps
                        )
                        near_corr = bool(include_corridor and corridor) and any(
                            haversine_nm(sp["lat"], sp["lon"], cp.lat, cp.lon) <= corridor_radius_nm
                            for cp in corridor
                        )
                        if near_wp or near_corr:
                            candidates.append(sp)
                            kept += 1
                    shom_total += kept
                    if kept:
                        await flush_docs_incremental(
                            marinas_coll,
                            [_marina_doc(c, wps, now_build_iso) for c in candidates[-kept:]],
                        )
                    state.log(
                        f"SHOM {label}: {len(shom_pts)} raw → {kept} within {radius_nm} NM of a waypoint "
                        f"or ±{corridor_radius_nm:.0f} NM of the corridor"
                    )
                except Exception as e:
                    shom_errors += 1
                    state.log(f"SHOM {label} error: {type(e).__name__}: {str(e)[:80]}")
                await asyncio.sleep(0.5)
            state.log(f"SHOM total kept: {shom_total} candidates")

        # ---- Dedup + priority + upsert ----
        by_key: dict[str, dict] = {}
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for cand in candidates:
            key = dedup_key(cand["name"], cand["lat"], cand["lon"])
            existing_cand = by_key.get(key)
            new_doc = _marina_doc(cand, wps, now_iso)
            if existing_cand:
                # Source preference: openstreetmap > shom > curated (curated is our fallback).
                src_rank = {"openstreetmap": 3, "shom": 2, "curated": 1}
                new_rank = src_rank.get(cand["source"], 0)
                old_rank = src_rank.get(existing_cand["source"], 0)
                if new_rank > old_rank:
                    by_key[key] = new_doc
                elif new_rank == old_rank:
                    # same rank → keep the one closer to a waypoint
                    if new_doc["nearest_waypoint"]["distance_nm"] < existing_cand["nearest_waypoint"]["distance_nm"]:
                        by_key[key] = new_doc
                # else: existing wins
            else:
                by_key[key] = new_doc

        deduped = list(by_key.values())
        dup_count = len(candidates) - len(deduped)

        inserted = updated = 0
        for doc in deduped:
            existing_doc = await marinas_coll.find_one({"dedup_key": doc["dedup_key"]}, {"_id": 1, "fetched_at": 1})
            if existing_doc:
                doc["_id"] = existing_doc["_id"]
                await marinas_coll.replace_one({"_id": existing_doc["_id"]}, doc)
                updated += 1
            else:
                await marinas_coll.insert_one(doc)
                inserted += 1

        try:
            await marinas_coll.create_index("dedup_key", unique=True)
            await marinas_coll.create_index([("priority", 1), ("name", 1)])
        except Exception:
            pass

        by_prio = {1: 0, 2: 0, 3: 0}
        by_src = {"openstreetmap": 0, "shom": 0, "curated": 0}
        for d in deduped:
            by_prio[d["priority"]] = by_prio.get(d["priority"], 0) + 1
            by_src[d["source"]] = by_src.get(d["source"], 0) + 1

        summary = {
            "queried_points": state.total,
            "found_raw": len(candidates),
            "unique_after_dedup": len(deduped),
            "duplicates_merged": dup_count,
            "inserted": inserted,
            "updated": updated,
            "by_priority": by_prio,
            "by_source": by_src,
            "overpass_errors": overpass_errors,
            "shom_errors": shom_errors,
            "radius_nm": radius_nm,
            "corridor_step_nm": corridor_step_nm if include_corridor else None,
            "corridor_radius_nm": corridor_radius_nm if include_corridor else None,
            "corridor_band_nm": corridor_radius_nm * 2 if include_corridor else None,
        }
        state.summary = summary
        state.log(f"Build complete: {json.dumps(summary)}")
        return summary

    except Exception as e:
        state.error = f"{type(e).__name__}: {e}"
        state.log(f"FATAL: {state.error}")
        raise
    finally:
        state.finished_at = time.time()
        state.running = False


# ------------------------------------------------------------------------
# GeoJSON serialiser
# ------------------------------------------------------------------------


def marinas_to_geojson(docs: Iterable[dict]) -> dict:
    feats = []
    for d in docs:
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(d["lon"]), float(d["lat"])]},
            "properties": {
                "id": d.get("_id"),
                "name": d.get("name"),
                "source": d.get("source"),
                "priority": d.get("priority"),
                "nearest_waypoint": d.get("nearest_waypoint") or {},
                "tags": d.get("tags") or {},
                "osm_id": d.get("osm_id"),
                "enriched": bool(d.get("enriched")),
                "enrichment_source": d.get("enrichment_source"),
                "enriched_at": d.get("enriched_at"),
                "stale": bool(d.get("stale")),
                # Phase 3 enrichment fields (null if unknown)
                "canal_vhf": d.get("canal_vhf"),
                "places_visiteurs": d.get("places_visiteurs"),
                "tirant_eau_max_metres": d.get("tirant_eau_max_metres"),
                "score_protection_meteo": d.get("score_protection_meteo"),
                "services_disponibles": d.get("services_disponibles"),
                "telephone_capitainerie": d.get("telephone_capitainerie"),
                "resume_avis": d.get("resume_avis"),
                "fetched_at": d.get("fetched_at"),
            },
        })
    return {"type": "FeatureCollection", "features": feats}
