"""Publication rules for a project site (CDC v2).

No snap_to_ocean, no ocean_fallback_coords: a point is
publishable only if it is already at sea or close enough to the coast (haven).
Thresholds come from settings, not magic pipeline constants.

Toponym geocoding is ``geocode_name`` (Nominatim ∥ GeoNames).
The space test stays here: haven, not a VLIZ polygon.
"""
from app.core.geo import geocode_name, is_ocean, coast_distance_km


def valid_coords(lat, lon) -> bool:
    try:
        return (
            lat is not None
            and lon is not None
            and -90 <= float(lat) <= 90
            and -180 <= float(lon) <= 180
            and not (float(lat) == 0 and float(lon) == 0)
        )
    except (TypeError, ValueError):
        return False


def site_publishable(lat, lon, settings: dict | None = None) -> tuple[bool, str]:
    """(ok, reason) — reason ∈ ocean | coastal | no_coords | inland."""
    s = settings or {}
    if not valid_coords(lat, lon):
        return False, "no_coords"
    lat_f, lon_f = float(lat), float(lon)
    if is_ocean(lat_f, lon_f):
        return True, "ocean"
    max_inland = float(s.get("max_inland_km", 15))
    dist = coast_distance_km(lat_f, lon_f)
    if dist <= max_inland:
        return True, "coastal"
    return False, "inland"


def apply_havre(hit: dict, settings: dict | None = None) -> dict:
    """Project space test: sea or haven. Not a VLIZ polygon.

    If the chosen point is inland, switch to the other gazetteer if it
    passes the haven — same gesture as PoE after the tie-break, other geography.
    ``haiku_none``: both points are off-topic, save neither.
    """
    settings = settings or {}
    hit = dict(hit or {})
    if hit.get("arbitration") in {"haiku_none", "none"}:
        hit.update({
            "lat": None, "lon": None, "source": None,
            "geo_kind": "no_coords",
        })
        return hit

    nomi = hit.get("nominatim")
    geon = hit.get("geonames")
    ordered: list[tuple[str, list, str]] = []
    if hit.get("lat") is not None and hit.get("lon") is not None:
        src = hit.get("source") or "nominatim"
        ordered.append((src, [hit["lat"], hit["lon"]], hit.get("arbitration") or "chosen"))
        other = "geonames" if src == "nominatim" else "nominatim"
        other_xy = geon if other == "geonames" else nomi
        if other_xy:
            ordered.append((other, other_xy, "spatial_switch"))
    else:
        if nomi:
            ordered.append(("nominatim", nomi, hit.get("arbitration") or "single"))
        if geon:
            arb = "spatial_switch" if nomi else (hit.get("arbitration") or "single")
            ordered.append(("geonames", geon, arb))

    last_kind = "no_coords"
    for i, (source, xy, arb) in enumerate(ordered):
        ok, kind = site_publishable(xy[0], xy[1], settings)
        last_kind = kind
        if ok:
            out = dict(hit)
            out.update({
                "lat": float(xy[0]),
                "lon": float(xy[1]),
                "source": source,
                "arbitration": arb if i == 0 else "spatial_switch",
                "geo_kind": kind,
            })
            return out
    out = dict(hit)
    out.update({
        "lat": None,
        "lon": None,
        "source": None,
        "arbitration": (
            "spatial_rejected" if (nomi or geon)
            else (hit.get("arbitration") or "miss")
        ),
        "geo_kind": last_kind,
    })
    return out


async def geocode_project_site(
    location: str,
    title: str,
    settings: dict | None = None,
    log=None,
) -> dict:
    """Geocode a Project action place: dual + tie-break, then haven.

    No VLIZ polygon, no snap, no ocean_fallback.
    ``llm_geocode`` only if both gazetteers are silent.
    """
    log = log or (lambda m: None)
    settings = settings or {}
    loc = (location or "").strip()
    tit = (title or "").strip()
    queries: list[tuple[str, str]] = []
    if loc:
        queries.append(("location", loc))
    if tit and tit.casefold() != loc.casefold():
        queries.append(("title", tit))

    last_kind = "no_coords"
    last_hit: dict | None = None
    for src_kind, q in queries:
        hit = await geocode_name(
            q,
            context={"name": q, "title": tit, "location": loc},
            settings=settings,
            log=log,
        )
        last_hit = hit
        picked = apply_havre(hit, settings)
        if picked.get("lat") is not None:
            picked["geo_source"] = f"geocoded:{src_kind}"
            return picked
        last_kind = picked.get("geo_kind") or last_kind

    from app.core.llm import llm_geocode
    g = await llm_geocode(loc, tit, settings)
    if g:
        ok, kind = site_publishable(g[0], g[1], settings)
        if ok:
            log("Smart geocoding: LLM estimated site coordinates")
            return {
                "lat": float(g[0]),
                "lon": float(g[1]),
                "source": "llm",
                "arbitration": "llm",
                "geo_source": "llm-geocoded",
                "geo_kind": kind,
                "agree": None,
                "agreement_km": None,
                "nominatim": (last_hit or {}).get("nominatim"),
                "geonames": (last_hit or {}).get("geonames"),
            }
        last_kind = kind
    return {
        "lat": None,
        "lon": None,
        "source": None,
        "arbitration": "miss",
        "geo_source": None,
        "geo_kind": last_kind,
        "agree": None,
        "nominatim": (last_hit or {}).get("nominatim"),
        "geonames": (last_hit or {}).get("geonames"),
    }
