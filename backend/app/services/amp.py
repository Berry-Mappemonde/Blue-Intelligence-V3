"""AMP mode — Marine Protected Areas (ProtectedSeas Navigator).

Two distinct URLs per site:

* ``manager_url`` — ProtectedSeas ``url`` field (Website alias),
  institutional / manager page.
* ``visit_url`` — visit or entry procedures (permits, anchoring,
  formalities). **Never** a copy of the manager site.

The polygon layer is served by bbox; visit enrichment is
stored in ``amp_sites`` (not the old ``mpa_cache``).
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any, Iterable

import httpx

from app.core.run_rules import catalog_default

ARCGIS_AMP_URL = (
    "https://services9.arcgis.com/lm7wE8a9YA9rKfzy/arcgis/rest/services/"
    "Navigator_AllSites_010925_attributes/FeatureServer/0/query"
)

LAST_REFRESH: dict = {}

# site_id = champ ArcGIS réel. Entry / Speed : 0 autorisé, 1 interdit,
# 2 restreint, 3 inconnu (dictionnaire Navigator, activités marines).
AMP_OUT_FIELDS = (
    "site_id,SITE_ID,site_name,url,country,state,managing_authority,designation,"
    "category_name,wdpa_id,iucn_cat,purpose,lfp,other_helpful_links,"
    "gov_level,year_est,entry,speed"
)
ATTR_OUT_FIELDS = (
    "site_id,SITE_ID,site_name,url,other_helpful_links,purpose,entry,speed"
)
ATTR_REFRESH_BATCH = 80

VISIT_STATUSES = (
    "none",
    "found",
    "rejected_same_as_manager",
    "not_found",
)

_URL_RE = re.compile(r"https?://[^\s,;|<>\"']+", re.I)
_VISIT_HINT = (
    r"visite|visits?|visiter|plaisance|mouillage|anchor(?:ing)?|moorings?|"
    r"permits?|permis|autorisation|r[eé]glement(?:ation)?s?|"
    r"entr[eé]e|entrer|entry|formalit\w*|pleasure.?craft|yachts?|"
    r"recreational|clearance|access|acceso|fondeo|amarr\w*|navegac\w*|"
    r"div(?:e|ing)|plonge(?:e|r)?|rules?|normativa|visitors?|"
    r"turismo|tourisme|nautism\w*|zoning|regulat\w*"
)
VISIT_HINT_RE = re.compile(rf"(?:^|[\W_])(?:{_VISIT_HINT})(?:$|[\W_])", re.I)
_NAME_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ0-9]{4,}")
_NAME_STOP = frozenset({
    "area", "aire", "amp", "mpa", "zone", "zona", "park", "parc", "marine",
    "marin", "natural", "naturel", "naturelle", "national", "nacional",
    "reserve", "reserva", "protected", "site", "sites", "gulf", "golfe",
    "golf", "port", "ports", "isla", "isle", "illes", "islas", "cape",
    "cabo", "west", "east", "north", "south", "nord", "sud", "ouest",
    "france", "french", "spain", "spanish", "italia", "italy", "waters",
    "mediterranean", "mediterranee", "special", "integral", "partial",
    "partiale", "protection", "protegida", "this", "that", "with", "from",
})

SLIM_PROJECTION = {
    "_id": 1,
    "site_id": 1,
    "name": 1,
    "country": 1,
    "state": 1,
    "designation": 1,
    "category_name": 1,
    "iucn_cat": 1,
    "purpose": 1,
    "lfp": 1,
    "managing_authority": 1,
    "gov_level": 1,
    "wdpa_id": 1,
    "year_est": 1,
    "entry": 1,
    "speed": 1,
    "manager_url": 1,
    "other_helpful_links": 1,
    "visit_url": 1,
    "visit_url_status": 1,
    "visit_url_source": 1,
    "lat": 1,
    "lon": 1,
    "fetched_at": 1,
    "enriched_at": 1,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_bbox(raw: str, *, max_w: float = 180.0,
               max_h: float = 90.0) -> tuple[float, float, float, float]:
    """Default caps suited to ProtectedSeas refresh; cache
    reads (GET /amp) pass world-wide caps (360×180).
    """
    parts = [p.strip() for p in (raw or "").split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be minx,miny,maxx,maxy")
    minx, miny, maxx, maxy = (float(p) for p in parts)
    if minx >= maxx or miny >= maxy:
        raise ValueError("bbox min must be < max")
    if abs(maxx - minx) > max_w or abs(maxy - miny) > max_h:
        raise ValueError("bbox too large")
    return minx, miny, maxx, maxy


def bbox_span_deg(bbox: tuple[float, float, float, float]) -> float:
    return max(bbox[2] - bbox[0], bbox[3] - bbox[1])


def iter_world_bboxes(step: float | None = None):
    """Split WORLD_TILES into AMP bboxes (minx, miny, maxx, maxy)."""
    from app.services.osm_seeds import WORLD_TILES
    step = float(step if step is not None else catalog_default("amp.bbox_max_deg", 8))
    step = max(1.0, min(step, 8.0))
    for south, west, north, east in WORLD_TILES:
        lat = south
        while lat < north - 1e-9:
            nlat = min(lat + step, north)
            lon = west
            while lon < east - 1e-9:
                nlon = min(lon + step, east)
                yield (lon, lat, nlon, nlat)
                lon = nlon
            lat = nlat


async def harvest_world_polygons(db, *, state=None, force: bool = True) -> dict:
    """Refresh ProtectedSeas polygons tile by tile (from scratch)."""
    tiles = list(iter_world_bboxes())
    fetched = 0
    errors = 0
    if state is not None:
        state.log(f"AMP polygones : {len(tiles)} tuiles (force={force})")
    for i, box in enumerate(tiles, 1):
        if state is not None and getattr(state, "cancel", False):
            if state is not None:
                state.log("Stop demandé — harvest polygones interrompu")
            break
        try:
            _docs, meta = await sites_in_bbox(db, box, force=force)
            fetched += int(meta.get("fetched") or 0)
            if state is not None and i % 20 == 0:
                state.log(f"AMP polygones {i}/{len(tiles)} — fetched={fetched}")
        except Exception:
            errors += 1
    summary = {"tiles": len(tiles), "fetched": fetched, "errors": errors}
    if state is not None:
        state.log(f"AMP polygones terminé: {summary}")
    return summary


def bbox_polygon(bbox: tuple[float, float, float, float]) -> dict:
    minx, miny, maxx, maxy = bbox
    ring = [[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]
    return {"type": "Polygon", "coordinates": [ring]}


def normalize_url(url: str | None) -> str | None:
    raw = (url or "").strip()
    if not raw or raw.lower() in ("null", "none", "n/a", "-"):
        return None
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/")
    return f"{(parsed.scheme or 'https').lower()}://{host}{path}"


def urls_equivalent(a: str | None, b: str | None) -> bool:
    na, nb = normalize_url(a), normalize_url(b)
    return bool(na and nb and na == nb)


def extract_urls(blob: str | None) -> list[str]:
    if not blob:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.findall(str(blob)):
        cleaned = match.rstrip(").,;]")
        norm = normalize_url(cleaned)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(cleaned.strip().rstrip(").,;]"))
    return out


def url_host(url: str | None) -> str | None:
    nu = normalize_url(url)
    if not nu:
        return None
    host = (urlparse(nu).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def is_manager_suburl(url: str | None, manager_url: str | None) -> bool:
    """Same host as the manager, distinct path — most common case."""
    if urls_equivalent(url, manager_url):
        return False
    hu, hm = url_host(url), url_host(manager_url)
    return bool(hu and hm and hu == hm)


def fold_text(value: str | None) -> str:
    raw = unicodedata.normalize("NFD", value or "")
    return "".join(ch for ch in raw if unicodedata.category(ch) != "Mn").lower()


def name_tokens(name: str | None) -> set[str]:
    return {
        tok for tok in _NAME_TOKEN_RE.findall(fold_text(name))
        if tok not in _NAME_STOP
    }


def name_matches(name: str | None, blob: str | None) -> bool:
    """The site name must appear in the URL / title (search off-host)."""
    toks = name_tokens(name)
    if not toks:
        return False
    hay = fold_text(blob)
    long_toks = {tok for tok in toks if len(tok) >= 5}
    if long_toks:
        return any(tok in hay for tok in long_toks)
    if len(toks) >= 2:
        return sum(1 for tok in toks if tok in hay) >= 2
    return next(iter(toks)) in hay


def split_protectedseas_website(raw: str | None) -> tuple[str | None, list[str]]:
    """Champ Website PS : parfois ``Label|https://a; Autre|https://b``."""
    urls = extract_urls(raw)
    if not urls:
        return normalize_url(raw), []
    return normalize_url(urls[0]), urls[1:]


def protectedseas_visit_blobs(doc: dict) -> list[str]:
    """PS texts where a visit sub-URL is often already written."""
    return [
        doc.get("other_helpful_links") or "",
        doc.get("ps_website_raw") or "",
        doc.get("purpose") or "",
    ]


def rank_visit_candidate(
    url: str | None,
    manager_url: str | None,
    *,
    curated: bool = False,
    title: str = "",
    snippet: str = "",
    name: str = "",
    require_name: bool = False,
) -> int:
    """Score > 0 = candidat. 0 = homepage, pub, ou hors sujet."""
    if not normalize_url(url) or urls_equivalent(url, manager_url):
        return 0
    path = urlparse(normalize_url(url) or "").path or ""
    blob = f"{url} {title} {snippet}"
    hint_path = bool(VISIT_HINT_RE.search(path))
    hint_blob = bool(VISIT_HINT_RE.search(blob))
    same = is_manager_suburl(url, manager_url)
    if require_name and not same and not name_matches(name, blob):
        return 0
    if require_name and not same and not path.strip("/"):
        return 0
    score = 0
    if hint_path:
        score += 4
    if hint_blob:
        score += 2
    if same and (hint_path or hint_blob or curated):
        score += 3
    if curated and same:
        score += 3
    if curated and not same and (hint_path or hint_blob):
        score += 2
    if curated and not same and not hint_path and not hint_blob:
        # Extra PS link off-host, no keyword: brochure / dive-map often useful.
        score += 2
    if not same and name_matches(name, blob):
        score += 2
    return score


def pick_visit_url(
    manager_url: str | None,
    other_helpful_links: str | None = None,
    discovered: str | None = None,
    extra_blobs: list[str] | None = None,
) -> tuple[str | None, str]:
    """Pick a visit URL distinct from the manager.

    Prefer a same-host sub-URL if it exists, but accept
    another domain already present in ProtectedSeas or discovered.
    Return ``(url, status)``. ``status`` ∈ VISIT_STATUSES.
    """
    if discovered:
        if urls_equivalent(discovered, manager_url):
            return None, "rejected_same_as_manager"
        if normalize_url(discovered):
            return discovered.strip(), "found"

    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    had_manager_copy = False
    had_distinct = False

    def _consider(raw: str, *, curated: bool, count_manager_copy: bool) -> None:
        nonlocal had_manager_copy, had_distinct
        if not normalize_url(raw):
            return
        if urls_equivalent(raw, manager_url):
            if count_manager_copy:
                had_manager_copy = True
            return
        had_distinct = True
        key = normalize_url(raw)
        if not key or key in seen:
            return
        score = rank_visit_candidate(raw, manager_url, curated=curated)
        if score <= 0:
            return
        seen.add(key)
        ranked.append((score, raw.strip()))

    for raw in extract_urls(other_helpful_links):
        _consider(raw, curated=True, count_manager_copy=True)
    for blob in extra_blobs or []:
        for raw in extract_urls(blob):
            # Any distinct link already written by ProtectedSeas counts, even off-host.
            _consider(raw, curated=True, count_manager_copy=False)

    if ranked:
        ranked.sort(key=lambda item: (-item[0], -len(urlparse(item[1]).path or "")))
        return ranked[0][1], "found"
    if had_manager_copy and not had_distinct:
        return None, "rejected_same_as_manager"
    blobs = [other_helpful_links or "", *(extra_blobs or [])]
    return None, "none" if not manager_url and not any(blobs) else "not_found"


def apply_visit_choice(doc: dict, *, discovered: str | None = None,
                       source: str | None = None) -> dict:
    """Write visit_url / status. Never overwrite visit_url with manager_url."""
    raw_web = doc.get("ps_website_raw") or ""
    if not raw_web and "|" in str(doc.get("manager_url") or ""):
        raw_web = doc.get("manager_url") or ""
        doc["ps_website_raw"] = raw_web
    if raw_web:
        parsed, _ = split_protectedseas_website(raw_web)
        if parsed:
            doc["manager_url"] = parsed
    manager = doc.get("manager_url")
    extras = [b for b in protectedseas_visit_blobs(doc) if b]
    url, status = pick_visit_url(
        manager,
        doc.get("other_helpful_links"),
        discovered=discovered,
        extra_blobs=extras,
    )
    if url and urls_equivalent(url, manager):
        url, status = None, "rejected_same_as_manager"
    if url:
        doc["visit_url"] = url
        doc["visit_url_status"] = "found"
        helpful = extract_urls(doc.get("other_helpful_links"))
        default_src = "other_helpful_links" if any(
            urls_equivalent(url, h) for h in helpful) else "protectedseas"
        doc["visit_url_source"] = source or doc.get("visit_url_source") or default_src
        doc["enriched_at"] = now_iso()
    else:
        if not doc.get("visit_url") or urls_equivalent(doc.get("visit_url"), manager):
            doc["visit_url"] = None
        doc["visit_url_status"] = status
        if status != "found":
            doc.setdefault("visit_url_source", None)
    return doc


def _as_lfp(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if 0 <= n <= 5 else 0


def _as_activity(value: Any) -> int | None:
    """Navigator : 0 autorisé, 1 interdit, 2 restreint, 3 inconnu. None = pas encore codé."""
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n in (0, 1, 2, 3) else None


def traffic_kind(entry: Any, speed: Any) -> str:
    e = _as_activity(entry)
    s = _as_activity(speed)
    if e == 1 or s == 1:
        return "forbidden"
    if e == 2 or s == 2:
        return "regulated"
    if e == 0 and s in (None, 0, 3):
        return "allowed"
    if e is None and s == 0:
        return "allowed"
    return "unknown"


def is_forbidden_pleasure(doc: dict | None) -> bool:
    if not isinstance(doc, dict):
        return False
    return traffic_kind(doc.get("entry"), doc.get("speed")) == "forbidden"


def is_area_geom(geom: dict | None) -> bool:
    return bool(isinstance(geom, dict) and geom.get("type") in ("Polygon", "MultiPolygon"))


def _as_geojson_geom(geom: dict | None) -> dict | None:
    """GeoJSON, ou anneaux / x,y ArcGIS."""
    if not geom or not isinstance(geom, dict):
        return None
    if geom.get("type") in ("Polygon", "MultiPolygon", "Point", "GeometryCollection"):
        return geom
    if "rings" in geom:
        return {"type": "Polygon", "coordinates": geom.get("rings") or []}
    if "x" in geom and "y" in geom:
        try:
            return {"type": "Point", "coordinates": [float(geom["x"]), float(geom["y"])]}
        except (TypeError, ValueError):
            return None
    return geom


def _ring_span_ok(ring) -> bool:
    lons, lats = [], []
    for pt in ring or []:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        try:
            lons.append(float(pt[0]))
            lats.append(float(pt[1]))
        except (TypeError, ValueError):
            continue
    if len(lons) < 3:
        return False
    return (max(lons) - min(lons)) <= 179.9 and (max(lats) - min(lats)) <= 89.9


def _area_span_ok(geom: dict | None) -> bool:
    if not is_area_geom(geom):
        return False
    coords = geom.get("coordinates") or []
    if geom.get("type") == "Polygon":
        return _ring_span_ok(coords[0] if coords else [])
    return all(_ring_span_ok(poly[0]) for poly in coords if poly)


def _source_is_area(geom: dict | None) -> bool:
    if not isinstance(geom, dict):
        return False
    if geom.get("type") in ("Polygon", "MultiPolygon", "GeometryCollection"):
        return True
    return "rings" in geom


def missing_entry_speed_query() -> dict:
    """Champ absent **ou** null — ProtectedSeas n'a pas encore été recopié."""
    return {"$or": [
        {"entry": {"$exists": False}},
        {"speed": {"$exists": False}},
        {"entry": None},
        {"speed": None},
    ]}


def repair_geometry_for_mongo(geom: dict | None, *, max_bytes: int = 800_000) -> dict | None:
    """Rendre un polygone indexable (2dsphere). Jamais un Point à la place d'une forme."""
    geom = _as_geojson_geom(geom)
    if not geom or not isinstance(geom, dict):
        return None
    import json
    cleaned = sanitize_geometry(geom)
    if is_area_geom(cleaned) and len(json.dumps(cleaned)) <= max_bytes and _area_span_ok(cleaned):
        return cleaned
    if geom.get("type") not in ("Polygon", "MultiPolygon", "GeometryCollection"):
        return None
    try:
        from shapely.geometry import mapping, shape
        from shapely.ops import unary_union
    except Exception:
        return None
    try:
        raw = shape(geom)
        if raw.is_empty:
            return None
        if not raw.is_valid:
            raw = raw.buffer(0)
        if raw.is_empty:
            return None
        if raw.geom_type == "GeometryCollection":
            parts = [g for g in raw.geoms if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
            raw = unary_union(parts) if parts else None
        if raw is None or raw.is_empty:
            return None
        if raw.geom_type not in ("Polygon", "MultiPolygon"):
            return None
        raw = _parts_under_hemisphere(raw)
        if raw is None or raw.is_empty:
            return None
        tol = 0.0002
        current = raw
        while True:
            gj = mapping(current)
            blob = json.dumps(gj)
            cleaned = sanitize_geometry(gj)
            if is_area_geom(cleaned) and len(blob) <= max_bytes and _area_span_ok(cleaned):
                return cleaned
            if tol > 8.0:
                return _local_area_geom(current)
            try:
                nxt = current.simplify(tol, preserve_topology=True)
            except Exception:
                nxt = None
            if nxt is None or nxt.is_empty:
                return _local_area_geom(raw)
            current = nxt
            tol *= 2
    except Exception:
        return None


# Vue large (emprise > amp.bbox_max_deg, 8°). Chaque contour reste assez
# petit pour qu'une page de cache (8 000 aires) tienne en quelques Mo.
WIDE_VIEW_GEOM_MAX_BYTES = 2_048
_WIDE_VIEW_MAX_RING_POINTS = 96
# En dessous, un morceau tient dans un pixel d'une carte continentale.
WIDE_VIEW_DISPLAY_TOL_DEG = 0.02
# Documents sources gardés en même temps pendant la lecture Mongo.
WIDE_VIEW_BATCH_SIZE = 32
WIDE_VIEW_FETCH_LIMIT = 8000
# Au-delà, un polygone de bbox dépasse l'hémisphère accepté par l'index 2dsphere.
WIDE_VIEW_WORLD_SPAN_DEG = 60.0
_WIDE_VIEW_BYTES_PER_PART = 140

# Champs lus par public_properties, plus la géométrie. Le reste du document
# (texte ProtectedSeas, liens bruts) ne part pas dans la vue large.
WIDE_VIEW_PROJECTION = {
    "_id": 1,
    "site_id": 1,
    "name": 1,
    "country": 1,
    "state": 1,
    "designation": 1,
    "category_name": 1,
    "iucn_cat": 1,
    "purpose": 1,
    "lfp": 1,
    "entry": 1,
    "speed": 1,
    "managing_authority": 1,
    "gov_level": 1,
    "wdpa_id": 1,
    "year_est": 1,
    "manager_url": 1,
    "visit_url": 1,
    "visit_url_status": 1,
    "visit_url_source": 1,
    "lat": 1,
    "lon": 1,
    "fetched_at": 1,
    "enriched_at": 1,
    "geometry": 1,
}


def _decimate_ring(ring, max_pts: int):
    """Garde la fermeture de l'anneau. Les petits anneaux ne sont pas touchés."""
    if not isinstance(ring, (list, tuple)) or len(ring) <= max_pts:
        return ring
    step = max(1, len(ring) // max_pts)
    thin = list(ring[::step])
    last = ring[-1]
    if not thin or thin[-1] != last:
        thin.append(last)
    if len(thin) >= 2 and thin[0] != thin[-1]:
        thin.append(thin[0])
    if len(thin) < 4:
        return ring
    return thin


def _decimate_dense_geometry(geom: dict, max_pts: int = _WIDE_VIEW_MAX_RING_POINTS) -> dict:
    """Moins de sommets avant la réparation, pour une vue déjà très éloignée."""
    kind = geom.get("type")
    coords = geom.get("coordinates")
    if kind == "Polygon" and isinstance(coords, list):
        return {
            "type": "Polygon",
            "coordinates": [_decimate_ring(ring, max_pts) for ring in coords],
        }
    if kind == "MultiPolygon" and isinstance(coords, list):
        polys = []
        for poly in coords:
            if isinstance(poly, list):
                polys.append([_decimate_ring(ring, max_pts) for ring in poly])
        return {"type": "MultiPolygon", "coordinates": polys}
    return geom


def _simplified_outline(geom: dict) -> dict | None:
    """Rectangle englobant : dernier recours quand la réparation échoue."""
    pts: list[tuple[float, float]] = []

    def walk(node):
        if not node or not isinstance(node, (list, tuple)):
            return
        if isinstance(node[0], (int, float)) and len(node) >= 2:
            try:
                pts.append((float(node[0]), float(node[1])))
            except (TypeError, ValueError):
                return
            return
        for child in node:
            walk(child)

    walk(geom.get("coordinates"))
    if len(pts) < 3:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    if maxx - minx < 1e-4:
        minx -= 0.01
        maxx += 0.01
    if maxy - miny < 1e-4:
        miny -= 0.01
        maxy += 0.01
    outline = {
        "type": "Polygon",
        "coordinates": [[
            [minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny],
        ]],
    }
    return outline if is_area_geom(outline) else None


def _area_parts(raw):
    kind = getattr(raw, "geom_type", None)
    if kind == "Polygon":
        return [] if raw.is_empty else [raw]
    if kind == "MultiPolygon":
        return [g for g in raw.geoms if g.geom_type == "Polygon" and not g.is_empty]
    if kind == "GeometryCollection":
        parts = []
        for piece in raw.geoms:
            parts.extend(_area_parts(piece))
        return parts
    return []


def _polygons_only(raw):
    parts = _area_parts(raw)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    from shapely.geometry import MultiPolygon
    return MultiPolygon(parts)


def _part_span_deg(part) -> float:
    try:
        minx, miny, maxx, maxy = part.bounds
    except Exception:
        return 0.0
    return max(maxx - minx, maxy - miny)


def _part_area(part) -> float:
    try:
        return float(part.area)
    except Exception:
        return 0.0


def _drop_parts_below_tolerance(parts, tol: float):
    """Retire les morceaux plus petits que la tolérance d'affichage."""
    kept = [part for part in parts if _part_span_deg(part) >= tol]
    if kept or not parts:
        return kept
    largest = max(parts, key=_part_area)
    return [largest]


def _simplify_wide_view_once(geom: dict, *, max_bytes: int) -> dict | None:
    """Un seul passage Douglas-Peucker, sans buffer(0) répété."""
    from shapely.geometry import MultiPolygon, mapping, shape
    from shapely.validation import make_valid

    raw = shape(geom)
    if raw.is_empty:
        return None
    tol = WIDE_VIEW_DISPLAY_TOL_DEG
    kept = _drop_parts_below_tolerance(_area_parts(raw), tol)
    if not kept:
        return None
    n_keep = max(1, (int(max_bytes) - 32) // _WIDE_VIEW_BYTES_PER_PART)
    if len(kept) > n_keep:
        kept.sort(key=_part_area, reverse=True)
        kept = kept[:n_keep]
    merged = kept[0] if len(kept) == 1 else MultiPolygon(kept)
    simple = merged.simplify(tol, preserve_topology=False)
    simple = _polygons_only(simple)
    if simple is None or simple.is_empty:
        return None
    if not simple.is_valid:
        simple = _polygons_only(make_valid(simple))
    if simple is None or simple.is_empty:
        return None
    gj = mapping(simple)
    if len(json.dumps(gj)) > max_bytes:
        budget = max(4, int(max_bytes) // 48)
        gj = _decimate_dense_geometry(gj, max_pts=budget)
    cleaned = sanitize_geometry(gj)
    if not is_area_geom(cleaned) or len(json.dumps(cleaned)) > max_bytes:
        return None
    checked = shape(cleaned)
    if not checked.is_valid:
        checked = _polygons_only(make_valid(checked))
        if checked is None or checked.is_empty:
            return None
        cleaned = sanitize_geometry(mapping(checked))
        if (
            not is_area_geom(cleaned)
            or len(json.dumps(cleaned)) > max_bytes
            or not shape(cleaned).is_valid
        ):
            return None
    return cleaned


def lighten_geometry_for_wide_view(
    geom: dict | None, *, max_bytes: int = WIDE_VIEW_GEOM_MAX_BYTES,
) -> dict | None:
    """Contour allégé pour la carte zoomée arrière. Un point reste un point."""
    parsed = _as_geojson_geom(geom) if isinstance(geom, dict) else None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("type") == "Point":
        return parsed
    if parsed.get("type") not in ("Polygon", "MultiPolygon", "GeometryCollection"):
        return _simplified_outline(parsed)
    cleaned = sanitize_geometry(parsed)
    if (
        is_area_geom(cleaned)
        and len(json.dumps(cleaned)) <= max_bytes
        and _area_span_ok(cleaned)
    ):
        return cleaned
    try:
        cheap = _simplify_wide_view_once(parsed, max_bytes=max_bytes)
    except Exception:
        cheap = None
    if is_area_geom(cheap) and len(json.dumps(cheap)) <= max_bytes:
        return cheap
    return _simplified_outline(parsed)


def wide_view_area_query(bbox, span: float) -> dict:
    """Filtre spatial seulement quand l'index 2dsphere peut l'utiliser.

    ``ensure_amp_indexes`` pose un index 2dsphere sur ``geometry``.
    ``$geoIntersects`` s'en sert. À partir de 60° le polygone de bbox
    dépasse un hémisphère : Mongo le refuse, on lit donc la collection.
    """
    if float(span) >= WIDE_VIEW_WORLD_SPAN_DEG:
        return {}
    return {"geometry": {"$geoIntersects": {"$geometry": bbox_polygon(bbox)}}}


def _lighten_wide_batch(docs: list) -> list:
    out = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        item = dict(doc)
        geom = item.pop("geometry", None)
        try:
            item["geometry"] = lighten_geometry_for_wide_view(geom)
        except Exception:
            item["geometry"] = _simplified_outline(geom) if isinstance(geom, dict) else None
        out.append(item)
    return out


async def collect_light_areas_for_wide_view(
    db, bbox, span: float, *, limit: int = WIDE_VIEW_FETCH_LIMIT,
    batch_size: int | None = None,
) -> list[dict]:
    """Lit les aires par lots et n'en garde qu'un lot de géométries brutes.

    Le curseur Mongo a le même ``batch_size`` : le tampon du pilote ne
    dépasse pas ce lot. Chaque lot est allégé avant de lire le suivant.
    """
    limit = max(1, int(limit))
    batch_size = max(1, int(batch_size or WIDE_VIEW_BATCH_SIZE))
    query = wide_view_area_query(bbox, span)
    try:
        cursor = db.amp_sites.find(query, WIDE_VIEW_PROJECTION)
    except Exception:
        return []
    if hasattr(cursor, "batch_size"):
        sized = cursor.batch_size(batch_size)
        if sized is not None:
            cursor = sized
    if hasattr(cursor, "limit"):
        limited = cursor.limit(limit)
        if limited is not None:
            cursor = limited
    light: list[dict] = []
    batch: list[dict] = []
    try:
        async for doc in cursor:
            batch.append(doc)
            if len(batch) >= batch_size:
                light.extend(await asyncio.to_thread(_lighten_wide_batch, batch))
                batch = []
                if len(light) >= limit:
                    break
        if batch and len(light) < limit:
            light.extend(await asyncio.to_thread(_lighten_wide_batch, batch))
    except AssertionError:
        raise
    except Exception:
        return light[:limit]
    return light[:limit]


def _span_ok_shape(g) -> bool:
    try:
        minx, miny, maxx, maxy = g.bounds
    except Exception:
        return False
    return (maxx - minx) <= 179.9 and (maxy - miny) <= 89.9


def _parts_under_hemisphere(raw):
    """Mongo refuse une pièce plus grande qu'un hémisphère ou qui traverse 180°."""
    from shapely.geometry import box
    from shapely.ops import unary_union

    if raw.geom_type == "Polygon":
        parts = [raw]
    elif raw.geom_type == "MultiPolygon":
        parts = list(raw.geoms)
    else:
        return raw
    kept = []
    west = box(-180.0, -90.0, 0.0, 90.0)
    east = box(0.0, -90.0, 180.0, 90.0)
    for part in parts:
        if _span_ok_shape(part):
            kept.append(part)
            continue
        for cut in (west, east):
            try:
                piece = part.intersection(cut)
            except Exception:
                continue
            if piece.is_empty:
                continue
            if piece.geom_type == "Polygon" and _span_ok_shape(piece):
                kept.append(piece)
            elif piece.geom_type == "MultiPolygon":
                kept.extend(g for g in piece.geoms if not g.is_empty and _span_ok_shape(g))
            elif piece.geom_type == "GeometryCollection":
                kept.extend(
                    g for g in piece.geoms
                    if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty and _span_ok_shape(g)
                )
    if not kept:
        return raw
    if len(kept) == 1:
        return kept[0]
    try:
        return unary_union(kept)
    except Exception:
        return kept[0]


def _local_area_geom(raw, radius: float = 25.0) -> dict | None:
    """Dernier recours : enveloppe convexe autour du morceau le plus dense."""
    try:
        from shapely.geometry import MultiPoint, mapping
    except Exception:
        return None
    pts: list[tuple[float, float]] = []

    def walk(g):
        if g.geom_type == "Polygon":
            pts.extend((float(x), float(y)) for x, y in g.exterior.coords)
        elif g.geom_type == "MultiPolygon":
            for p in g.geoms:
                walk(p)

    try:
        walk(raw)
    except Exception:
        return None
    if len(pts) < 3:
        return None
    if raw.geom_type == "MultiPolygon" and raw.geoms:
        best = max(
            (g for g in raw.geoms if g.geom_type == "Polygon"),
            key=lambda g: len(g.exterior.coords),
            default=None,
        )
        cx, cy = (best.centroid.x, best.centroid.y) if best is not None else (raw.centroid.x, raw.centroid.y)
    else:
        cx, cy = raw.centroid.x, raw.centroid.y
    local = [(x, y) for x, y in pts if abs(x - cx) <= radius and abs(y - cy) <= radius]
    if len(local) < 3:
        return {
            "type": "Polygon",
            "coordinates": [[
                [cx - 0.5, cy - 0.5], [cx + 0.5, cy - 0.5],
                [cx + 0.5, cy + 0.5], [cx - 0.5, cy + 0.5],
                [cx - 0.5, cy - 0.5],
            ]],
        }
    try:
        hull = MultiPoint(local).convex_hull
    except Exception:
        return None
    if hull.geom_type != "Polygon" or hull.is_empty:
        return None
    return sanitize_geometry(mapping(hull))


def _hull_from_geom(geom) -> dict | None:
    if geom is None:
        return None
    try:
        from shapely.geometry import shape
        raw = geom if getattr(geom, "geom_type", None) else shape(geom)
        return _local_area_geom(raw, radius=8.0)
    except Exception:
        return None


def _ring_ok(ring) -> bool:
    """Mongo 2dsphere requires ≥ 3 distinct vertices (closed ring ≥ 4 positions)."""
    if not isinstance(ring, (list, tuple)) or len(ring) < 4:
        return False
    uniq: list[tuple[float, float]] = []
    for pt in ring:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        try:
            pair = (float(pt[0]), float(pt[1]))
        except (TypeError, ValueError):
            continue
        if not uniq or pair != uniq[-1]:
            uniq.append(pair)
    if len(uniq) >= 2 and uniq[0] == uniq[-1]:
        uniq = uniq[:-1]
    return len(uniq) >= 3


def sanitize_geometry(geom: dict | None) -> dict | None:
    """Drop degenerate rings (ArcGIS simplification) before the 2dsphere index."""
    if not geom or not isinstance(geom, dict):
        return None
    kind = geom.get("type")
    coords = geom.get("coordinates")
    if kind == "Polygon":
        rings = [r for r in (coords or []) if _ring_ok(r)]
        return {"type": "Polygon", "coordinates": rings} if rings else None
    if kind == "MultiPolygon":
        polys = []
        for poly in coords or []:
            rings = [r for r in (poly or []) if _ring_ok(r)]
            if rings:
                polys.append(rings)
        if not polys:
            return None
        if len(polys) == 1:
            return {"type": "Polygon", "coordinates": polys[0]}
        return {"type": "MultiPolygon", "coordinates": polys}
    if kind == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        try:
            return {"type": "Point", "coordinates": [float(coords[0]), float(coords[1])]}
        except (TypeError, ValueError):
            return None
    return None


def _point_geom(lat: float | None, lon: float | None) -> dict | None:
    if lat is None or lon is None:
        return None
    return {"type": "Point", "coordinates": [float(lon), float(lat)]}


def _ring_area(ring: list) -> float:
    if not ring or len(ring) < 3:
        return 0.0
    area = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[i + 1][0]), float(ring[i + 1][1])
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5


def _ring_centroid(ring: list) -> tuple[float | None, float | None]:
    if not ring or len(ring) < 1:
        return None, None
    if len(ring) < 3:
        return float(ring[0][1]), float(ring[0][0])
    area2 = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[i + 1][0]), float(ring[i + 1][1])
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area2) < 1e-12:
        return float(ring[0][1]), float(ring[0][0])
    cx /= (3.0 * area2)
    cy /= (3.0 * area2)
    return cy, cx


def _centroid(geom: dict | None) -> tuple[float | None, float | None]:
    """Centroid of the outer ring (or of the largest polygon)."""
    if not geom:
        return None, None
    kind = geom.get("type")
    coords = geom.get("coordinates")
    if kind == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        return float(coords[1]), float(coords[0])
    if kind == "Polygon" and coords:
        return _ring_centroid(coords[0])
    if kind == "MultiPolygon" and coords:
        best = None
        best_a = -1.0
        for poly in coords:
            if not poly:
                continue
            area = _ring_area(poly[0])
            if area > best_a:
                best_a = area
                best = poly[0]
        if best:
            return _ring_centroid(best)
    flat: list[list[float]] = []

    def walk(node):
        if not node:
            return
        if isinstance(node[0], (int, float)) and len(node) >= 2:
            flat.append([float(node[0]), float(node[1])])
            return
        for child in node:
            walk(child)

    walk(coords)
    if not flat:
        return None, None
    lon = sum(c[0] for c in flat) / len(flat)
    lat = sum(c[1] for c in flat) / len(flat)
    return lat, lon


def attrs_from_feature(feat: dict) -> dict:
    props = feat.get("properties") or {}
    site_id = str(props.get("SITE_ID") or props.get("site_id") or "").strip()
    website_raw = props.get("url") or ""
    manager, _ps_extras = split_protectedseas_website(website_raw)
    raw_geom = _as_geojson_geom(feat.get("geometry"))
    lat, lon = _centroid(raw_geom)
    source_area = _source_is_area(raw_geom)
    geom = sanitize_geometry(raw_geom)
    if not is_area_geom(geom) or not _area_span_ok(geom):
        geom = repair_geometry_for_mongo(raw_geom) or (geom if is_area_geom(geom) else None)
    if not is_area_geom(geom) and source_area:
        geom = repair_geometry_for_mongo(raw_geom, max_bytes=200_000)
    if not is_area_geom(geom):
        # Une forme ProtectedSeas trop simplifiée ou refusée par la base
        # ne devient pas un point silencieux : on n'enregistre pas de géométrie.
        geom = None if source_area else _point_geom(lat, lon)
    doc = {
        "_id": site_id,
        "site_id": site_id,
        "name": (props.get("site_name") or "").strip() or site_id or "—",
        "country": props.get("country") or "",
        "state": props.get("state") or "",
        "designation": props.get("designation") or "",
        "category_name": props.get("category_name") or "",
        "iucn_cat": props.get("iucn_cat") or "",
        "purpose": props.get("purpose") or "",
        "lfp": _as_lfp(props.get("lfp")),
        "entry": _as_activity(props.get("entry")),
        "speed": _as_activity(props.get("speed")),
        "managing_authority": props.get("managing_authority") or "",
        "gov_level": props.get("gov_level") or "",
        "wdpa_id": props.get("wdpa_id") or "",
        "year_est": props.get("year_est"),
        "manager_url": manager,
        "ps_website_raw": website_raw,
        "other_helpful_links": props.get("other_helpful_links") or "",
        "geometry": geom,
        "lat": lat,
        "lon": lon,
        "fetched_at": now_iso(),
    }
    apply_visit_choice(doc, source="other_helpful_links")
    return doc


def merge_cached(existing: dict | None, incoming: dict) -> dict:
    """Keep the already-validated visit URL; do not replace it with the manager."""
    if not existing:
        return incoming
    out = dict(incoming)
    if is_area_geom(existing.get("geometry")) and not is_area_geom(incoming.get("geometry")):
        out["geometry"] = existing["geometry"]
        if existing.get("lat") is not None:
            out["lat"] = existing.get("lat")
            out["lon"] = existing.get("lon")
    prev = existing.get("visit_url")
    prev_status = existing.get("visit_url_status")
    if prev and not urls_equivalent(prev, incoming.get("manager_url")):
        out["visit_url"] = prev
        out["visit_url_status"] = prev_status or "found"
        out["visit_url_source"] = existing.get("visit_url_source")
        out["enriched_at"] = existing.get("enriched_at")
    elif not out.get("visit_url"):
        apply_visit_choice(out, source="other_helpful_links")
    return out


def public_properties(doc: dict, *, include_geometry_meta: bool = True) -> dict:
    manager = doc.get("manager_url")
    visit = doc.get("visit_url")
    if urls_equivalent(visit, manager):
        visit = None
    props = {
        "id": doc.get("site_id") or doc.get("_id"),
        "site_id": doc.get("site_id") or doc.get("_id"),
        "name": doc.get("name"),
        "country": doc.get("country"),
        "state": doc.get("state"),
        "designation": doc.get("designation"),
        "category_name": doc.get("category_name"),
        "iucn_cat": doc.get("iucn_cat"),
        "purpose": doc.get("purpose"),
        "lfp": _as_lfp(doc.get("lfp")),
        "entry": _as_activity(doc.get("entry")),
        "speed": _as_activity(doc.get("speed")),
        "managing_authority": doc.get("managing_authority"),
        "gov_level": doc.get("gov_level"),
        "wdpa_id": doc.get("wdpa_id"),
        "year_est": doc.get("year_est"),
        "manager_url": manager,
        "visit_url": visit,
        "visit_url_status": "none" if not visit else (doc.get("visit_url_status") or "found"),
        "visit_url_source": doc.get("visit_url_source") if visit else None,
        "lat": doc.get("lat"),
        "lon": doc.get("lon"),
    }
    if include_geometry_meta:
        props["fetched_at"] = doc.get("fetched_at")
        props["enriched_at"] = doc.get("enriched_at")
    return props


def to_feature(doc: dict, *, geometry: bool = True) -> dict:
    geom = doc.get("geometry") if geometry else None
    # L'export léger (geometry=False) sert le centroïde. L'API polygones
    # ne fabrique pas un point quand la forme manque : un point inventé
    # était enregistré comme si c'était l'aire.
    if not geometry and doc.get("lon") is not None and doc.get("lat") is not None:
        geom = {"type": "Point", "coordinates": [doc["lon"], doc["lat"]]}
    return {
        "type": "Feature",
        "id": doc.get("site_id") or doc.get("_id"),
        "geometry": geom,
        "properties": public_properties(doc),
    }


def to_feature_collection(docs: Iterable[dict], *, geometry: bool = True,
                          extra: dict | None = None) -> dict:
    fc = {
        "type": "FeatureCollection",
        "features": [to_feature(d, geometry=geometry) for d in docs if d.get("site_id") or d.get("_id")],
    }
    if extra:
        fc.update(extra)
    return fc


def _offset_for_span(span: float) -> float:
    """Simplification ArcGIS. 0,05° (~5 km) détruisait les petits parcs → Point."""
    if span >= 8:
        return 0.002
    if span >= 4:
        return 0.001
    if span >= 2:
        return 0.0005
    if span >= 1:
        return 0.0003
    return 0.0001


async def fetch_arcgis(bbox: tuple[float, float, float, float], *,
                       max_features: int = 400) -> list[dict]:
    minx, miny, maxx, maxy = bbox
    span = bbox_span_deg(bbox)
    params = {
        "geometry": f"{minx},{miny},{maxx},{maxy}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": AMP_OUT_FIELDS,
        "outSR": 4326,
        "f": "geojson",
        "returnGeometry": "true",
        "maxAllowableOffset": _offset_for_span(span),
        "resultRecordCount": min(int(max_features), 2000),
    }
    async with httpx.AsyncClient(timeout=35.0) as client:
        r = await client.get(ARCGIS_AMP_URL, params=params)
        r.raise_for_status()
        data = r.json()
    feats = data.get("features") or []
    docs = []
    for feat in feats:
        doc = attrs_from_feature(feat)
        if not doc.get("site_id"):
            continue
        docs.append(doc)
    return docs


def _sql_site_ids(site_ids: list[str]) -> str:
    parts = []
    for sid in site_ids:
        safe = str(sid).replace("'", "''")
        if safe:
            parts.append(f"'{safe}'")
    return f"site_id IN ({','.join(parts)})"


async def fetch_arcgis_attrs(site_ids: list[str]) -> dict[str, dict]:
    """ProtectedSeas attributes without geometry — Website / Other Helpful Links extras."""
    out: dict[str, dict] = {}
    ids = [str(s).strip() for s in site_ids if str(s).strip()]
    if not ids:
        return out
    async with httpx.AsyncClient(timeout=35.0) as client:
        for i in range(0, len(ids), ATTR_REFRESH_BATCH):
            chunk = ids[i:i + ATTR_REFRESH_BATCH]
            params = {
                "where": _sql_site_ids(chunk),
                "outFields": ATTR_OUT_FIELDS,
                "returnGeometry": "false",
                "f": "json",
            }
            r = await client.get(ARCGIS_AMP_URL, params=params)
            r.raise_for_status()
            data = r.json()
            for feat in data.get("features") or []:
                attrs = feat.get("attributes") or feat.get("properties") or {}
                sid = str(attrs.get("SITE_ID") or attrs.get("site_id") or "").strip()
                if sid:
                    out[sid] = attrs
    return out


async def fetch_arcgis_by_ids(site_ids: list[str], *, offset: float = 0.0) -> list[dict]:
    """Polygones + attributs (dont entry/speed) pour une liste de SITE_ID. offset=0 = forme native."""
    ids = [str(s).strip() for s in site_ids if str(s).strip()]
    docs: list[dict] = []
    if not ids:
        return docs
    async with httpx.AsyncClient(timeout=90.0) as client:
        for i in range(0, len(ids), 20):
            chunk = ids[i:i + 20]
            params = {
                "where": _sql_site_ids(chunk),
                "outFields": AMP_OUT_FIELDS,
                "returnGeometry": "true",
                "outSR": 4326,
                "f": "geojson",
                "maxAllowableOffset": offset,
            }
            r = await client.get(ARCGIS_AMP_URL, params=params)
            r.raise_for_status()
            data = r.json()
            for feat in data.get("features") or []:
                if not feat.get("properties") and feat.get("attributes"):
                    feat = {
                        "type": "Feature",
                        "properties": feat.get("attributes") or {},
                        "geometry": feat.get("geometry"),
                    }
                doc = attrs_from_feature(feat)
                if doc.get("site_id"):
                    docs.append(doc)
    return docs


async def _still_point_ids(db, ids: list[str]) -> list[str]:
    left = []
    for sid in ids:
        row = await db.amp_sites.find_one({"_id": sid}, {"geometry.type": 1})
        if not row:
            row = await db.amp_sites.find_one({"site_id": sid}, {"geometry.type": 1})
        if ((row or {}).get("geometry") or {}).get("type") == "Point":
            left.append(sid)
    return left


async def _fetch_ids_resilient(fetch, ids: list[str], *, offset: float, log=None) -> tuple[list[dict], int]:
    """Un lot trop gros (ZEE) ne doit pas faire rater les petits parcs du même paquet."""
    docs: list[dict] = []
    fetched = 0
    batch = 8 if offset <= 0.001 else 4
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        try:
            got = await fetch(chunk, offset=offset)
        except TypeError:
            try:
                got = await fetch(chunk)
            except Exception as exc:
                got = None
                if log:
                    log(f"AMP repair lot {i}/{len(ids)} : {type(exc).__name__}: {str(exc)[:80]}")
        except Exception as exc:
            got = None
            if log:
                log(f"AMP repair lot {i}/{len(ids)} : {type(exc).__name__}: {str(exc)[:80]}")
        if got is None:
            for sid in chunk:
                try:
                    one = await fetch([sid], offset=offset)
                except TypeError:
                    try:
                        one = await fetch([sid])
                    except Exception as exc:
                        if log:
                            log(f"AMP repair {sid} : {type(exc).__name__}: {str(exc)[:60]}")
                        continue
                except Exception as exc:
                    if log:
                        log(f"AMP repair {sid} : {type(exc).__name__}: {str(exc)[:60]}")
                    continue
                docs.extend(one)
                fetched += len(one)
            continue
        docs.extend(got)
        fetched += len(got)
        if log:
            log(f"AMP repair {min(i + batch, len(ids))}/{len(ids)} offset={offset} fetched={fetched}")
    return docs, fetched


async def repair_point_sites(db, *, limit: int = 0, log=None, fetch_fn=None,
                             offsets: tuple[float, ...] | None = None) -> dict:
    """Reprendre chez ProtectedSeas les sites stockés en Point. Ne jamais les y laisser si un polygone existe."""
    q = {"geometry.type": "Point"}
    cur = db.amp_sites.find(q, {"site_id": 1, "_id": 1})
    if hasattr(cur, "to_list"):
        rows = await cur.to_list(int(limit) if limit else 20_000)
    else:
        rows = []
        async for doc in cur:
            rows.append(doc)
            if limit and len(rows) >= limit:
                break
    ids = [str(d.get("site_id") or d.get("_id") or "").strip() for d in rows]
    ids = [i for i in ids if i]
    fetch = fetch_fn or fetch_arcgis_by_ids
    fetched = 0
    upserted = 0
    remaining = list(ids)
    for offset in (offsets if offsets is not None else (0.0, 0.02, 0.08, 0.2)):
        if not remaining:
            break
        docs, n = await _fetch_ids_resilient(fetch, remaining, offset=offset, log=log)
        fetched += n
        if docs:
            upserted += await upsert_sites(db, docs)
        remaining = await _still_point_ids(db, remaining)
        if log:
            log(f"AMP repair offset={offset} still_point={len(remaining)}")
    still_point = len(await _still_point_ids(db, ids))
    summary = {
        "candidates": len(ids),
        "fetched": fetched,
        "upserted": upserted,
        "still_point": still_point,
    }
    if log:
        log(f"AMP repair terminé : {summary}")
    return summary


async def backfill_entry_speed(db, *, log=None, fetch_fn=None) -> dict:
    """Moissonner Entry / Speed pour les fiches qui ne les ont pas encore."""
    q = missing_entry_speed_query()
    cur = db.amp_sites.find(q, {"site_id": 1, "_id": 1})
    if hasattr(cur, "to_list"):
        rows = await cur.to_list(20_000)
    else:
        rows = [d async for d in cur]
    ids = [str(d.get("site_id") or d.get("_id") or "").strip() for d in rows]
    ids = [i for i in ids if i]
    fetch = fetch_fn or fetch_arcgis_attrs
    updated = 0
    try:
        remote = await fetch(ids)
    except Exception as exc:
        if log:
            log(f"AMP entry/speed : {type(exc).__name__}: {str(exc)[:80]}")
        return {"candidates": len(ids), "updated": 0}
    for sid, attrs in remote.items():
        entry = _as_activity(attrs.get("entry"))
        speed = _as_activity(attrs.get("speed"))
        if entry is None:
            entry = 3
        if speed is None:
            speed = 3
        await db.amp_sites.update_one(
            {"_id": sid},
            {"$set": {"entry": entry, "speed": speed}},
        )
        updated += 1
    summary = {"candidates": len(ids), "updated": updated}
    if log:
        log(f"AMP entry/speed : {summary}")
    return summary


def apply_protectedseas_attrs(doc: dict, attrs: dict | None) -> dict:
    """Rewrite manager_url / extras from an ArcGIS record (no polygon)."""
    if not attrs:
        return doc
    website_raw = attrs.get("url") or ""
    helpful = attrs.get("other_helpful_links")
    purpose = attrs.get("purpose")
    if website_raw:
        doc["ps_website_raw"] = website_raw
        manager, _extras = split_protectedseas_website(website_raw)
        if manager:
            doc["manager_url"] = manager
    if helpful is not None:
        doc["other_helpful_links"] = helpful
    if purpose:
        doc["purpose"] = purpose
    if "entry" in attrs:
        doc["entry"] = _as_activity(attrs.get("entry"))
    if "speed" in attrs:
        doc["speed"] = _as_activity(attrs.get("speed"))
    return doc


async def refresh_protectedseas_attrs(
    db, docs: list[dict], *, fetch_fn=None, log=None,
) -> int:
    """Reload ProtectedSeas extras from cache (like OSM Harbormaster tags)."""
    ids = [str(d.get("site_id") or d.get("_id") or "").strip() for d in docs]
    ids = [i for i in ids if i]
    if not ids:
        return 0
    fetch = fetch_fn or fetch_arcgis_attrs
    try:
        remote = await fetch(ids)
    except Exception as exc:
        if log:
            log(f"ProtectedSeas attributs : {type(exc).__name__}: {str(exc)[:80]}")
        return 0
    n = 0
    for doc in docs:
        sid = str(doc.get("site_id") or doc.get("_id") or "").strip()
        attrs = remote.get(sid)
        if not attrs:
            continue
        apply_protectedseas_attrs(doc, attrs)
        if doc.get("visit_url") and urls_equivalent(doc.get("visit_url"), doc.get("manager_url")):
            doc["visit_url"] = None
            doc["visit_url_status"] = "not_found"
            doc["visit_url_source"] = None
            doc["visit_url_judge"] = None
        n += 1
        from app.services.isolated_runs import current_run_id
        if db is not None and doc.get("_id") is not None and not current_run_id():
            await db.amp_sites.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "manager_url": doc.get("manager_url"),
                    "ps_website_raw": doc.get("ps_website_raw"),
                    "other_helpful_links": doc.get("other_helpful_links"),
                    "purpose": doc.get("purpose"),
                    "entry": doc.get("entry"),
                    "speed": doc.get("speed"),
                    "visit_url": doc.get("visit_url"),
                    "visit_url_status": doc.get("visit_url_status"),
                    "visit_url_source": doc.get("visit_url_source"),
                    "visit_url_judge": doc.get("visit_url_judge"),
                }},
            )
    if log:
        log(f"ProtectedSeas attributs : {n}/{len(ids)} fiche(s) mises à jour")
    return n


async def ensure_amp_indexes(db) -> None:
    try:
        await db.amp_sites.create_index("site_id", unique=True)
        await db.amp_sites.create_index("name")
        await db.amp_sites.create_index("lfp")
        await db.amp_sites.create_index([("geometry", "2dsphere")])
        await db.amp_tiles.create_index("fetched_at")
    except Exception:
        pass


def tile_key(bbox: tuple[float, float, float, float]) -> str:
    minx, miny, maxx, maxy = bbox
    return f"{minx:.3f},{miny:.3f},{maxx:.3f},{maxy:.3f}"


def cache_covers_tile(cached: list[dict], tile_doc: dict | None,
                      ttl_days: int, *, force: bool) -> bool:
    """An isolated site from a failed upsert does not count for a tile."""
    if force or not cached or not tile_doc:
        return False
    return _is_fresh(tile_doc, ttl_days)


def _is_fresh(doc: dict, ttl_days: int) -> bool:
    raw = doc.get("fetched_at") or ""
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
    return age.total_seconds() < ttl_days * 86400


async def query_cache(db, bbox: tuple[float, float, float, float],
                      limit: int = 400) -> list[dict]:
    q = {"geometry": {"$geoIntersects": {"$geometry": bbox_polygon(bbox)}}}
    try:
        return await db.amp_sites.find(q).limit(int(limit)).to_list(int(limit))
    except Exception:
        return []


async def upsert_sites(db, docs: list[dict]) -> int:
    n = 0
    for incoming in docs:
        sid = incoming.get("site_id")
        if not sid:
            continue
        existing = await db.amp_sites.find_one({"_id": sid})
        merged = merge_cached(existing, incoming)
        if not is_area_geom(merged.get("geometry")):
            # Pas de nouveau point. Un point déjà en base reste, pour que
            # repair_point_sites puisse encore le retrouver et le remplacer.
            if existing and (existing.get("geometry") or {}).get("type") == "Point":
                merged["geometry"] = existing["geometry"]
            else:
                continue
        if is_area_geom(merged.get("geometry")):
            fitted = repair_geometry_for_mongo(merged.get("geometry"))
            if is_area_geom(fitted):
                merged["geometry"] = fitted
        try:
            await db.amp_sites.update_one({"_id": sid}, {"$set": merged}, upsert=True)
        except Exception:
            repaired = repair_geometry_for_mongo(merged.get("geometry"))
            if not is_area_geom(repaired):
                repaired = _hull_from_geom(merged.get("geometry"))
            if not is_area_geom(repaired):
                continue
            merged["geometry"] = repaired
            try:
                await db.amp_sites.update_one({"_id": sid}, {"$set": merged}, upsert=True)
            except Exception:
                hull = _hull_from_geom(repaired)
                if not is_area_geom(hull):
                    continue
                merged["geometry"] = hull
                try:
                    await db.amp_sites.update_one({"_id": sid}, {"$set": merged}, upsert=True)
                except Exception:
                    continue
        n += 1
    return n


async def sites_in_bbox(db, bbox: tuple[float, float, float, float], *,
                        force: bool = False, max_features: int | None = None,
                        ttl_days: int | None = None) -> tuple[list[dict], dict]:
    max_features = int(max_features or catalog_default("amp.max_features", 400))
    ttl_days = int(ttl_days or catalog_default("amp.cache_ttl_days", 30))
    cached = await query_cache(db, bbox, limit=max_features)
    tile = None
    try:
        tile = await db.amp_tiles.find_one({"_id": tile_key(bbox)})
    except Exception:
        tile = None
    meta = {"source": "cache", "truncated": False, "fetched": 0}

    def _remember(docs, meta_out):
        LAST_REFRESH.clear()
        LAST_REFRESH.update({
            "bbox": list(bbox),
            "source": meta_out.get("source"),
            "count": len(docs or []),
            "fetched": meta_out.get("fetched"),
            "error": meta_out.get("error"),
        })
        return docs, meta_out

    if cache_covers_tile(cached, tile, ttl_days, force=force):
        meta["truncated"] = len(cached) >= max_features
        return _remember(cached, meta)
    try:
        remote = await fetch_arcgis(bbox, max_features=max_features)
    except Exception as exc:
        meta["error"] = str(exc)[:180]
        return _remember(cached, meta)
    if remote:
        try:
            meta["fetched"] = await upsert_sites(db, remote)
            meta["source"] = "arcgis"
            cached = await query_cache(db, bbox, limit=max_features) or remote
            try:
                await db.amp_tiles.update_one(
                    {"_id": tile_key(bbox)},
                    {"$set": {
                        "fetched_at": now_iso(),
                        "count": len(cached),
                        "bbox": list(bbox),
                    }},
                    upsert=True,
                )
            except Exception:
                pass
        except Exception as exc:
            meta["error"] = str(exc)[:180]
            cached = remote or cached
    meta["truncated"] = len(cached) >= max_features
    return _remember(cached, meta)


async def resolve_visit_urls(db, *, limit: int = 500) -> dict:
    """Apply the other_helpful_links heuristic on the cache (no TinyFish)."""
    docs = await db.amp_sites.find({
        "$or": [
            {"visit_url": {"$in": [None, ""]}},
            {"visit_url_status": {"$in": ["none", "not_found", None]}},
        ],
    }).to_list(int(limit))
    found = rejected = unchanged = 0
    for doc in docs:
        before = doc.get("visit_url")
        apply_visit_choice(doc, source="other_helpful_links")
        after = doc.get("visit_url")
        if after and not urls_equivalent(after, doc.get("manager_url")):
            found += 1
        elif doc.get("visit_url_status") == "rejected_same_as_manager":
            rejected += 1
        else:
            unchanged += 1
        if after != before or doc.get("visit_url_status"):
            await db.amp_sites.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "visit_url": doc.get("visit_url"),
                    "visit_url_status": doc.get("visit_url_status"),
                    "visit_url_source": doc.get("visit_url_source"),
                    "enriched_at": doc.get("enriched_at"),
                }},
            )
    return {
        "scanned": len(docs),
        "found": found,
        "rejected_same_as_manager": rejected,
        "unchanged": unchanged,
    }


async def set_visit_url(db, site_id: str, url: str | None) -> dict | None:
    doc = await db.amp_sites.find_one({"_id": site_id})
    if not doc:
        doc = await db.amp_sites.find_one({"site_id": site_id})
    if not doc:
        return None
    apply_visit_choice(doc, discovered=url, source="manual" if url else "other_helpful_links")
    await db.amp_sites.update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "visit_url": doc.get("visit_url"),
            "visit_url_status": doc.get("visit_url_status"),
            "visit_url_source": doc.get("visit_url_source"),
            "enriched_at": doc.get("enriched_at"),
        }},
    )
    return doc


async def amp_stats(db) -> dict:
    total = await db.amp_sites.count_documents({})
    with_manager = await db.amp_sites.count_documents(
        {"manager_url": {"$nin": [None, ""]}})
    with_visit = await db.amp_sites.count_documents(
        {"visit_url": {"$nin": [None, ""]}})
    return {
        "mode": "amp",
        "total": total,
        "with_manager_url": with_manager,
        "with_visit_url": with_visit,
        "visit_coverage": round(with_visit / total * 100, 1) if total else 0.0,
        "items_mapped": total,
    }


def slim_export_feature(doc: dict) -> dict:
    """Light export: centroid + both URLs, not the polygon."""
    return to_feature(doc, geometry=False)
