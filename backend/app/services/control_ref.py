"""250 m control reference (SHOM SMCFAC / NOAA) — not a merge."""
from __future__ import annotations

from app.core.identity import OVERLAY_RADIUS_KM, find_building

# ~400 m Mongo prefilter (a bit wider than 250 m).
_DELTA_DEG = 0.004


def control_ref_from_candidates(
    lat: float | None,
    lon: float | None,
    candidates: list[dict] | None,
    radius_km: float = OVERLAY_RADIUS_KM,
) -> dict:
    """Attach the nearest official neighbor. Distance only. No identity."""
    if lat is None or lon is None:
        return {"status": "osm_only", "ref": None}
    hit = find_building(lat, lon, candidates or [], radius_km=radius_km)
    if not hit:
        return {"status": "osm_only", "ref": None}
    doc = hit.doc
    tags = doc.get("tags") or {}
    return {
        "status": "published_ref",
        "ref": {
            "id": doc.get("shom_id") or doc.get("noaa_id") or str(doc.get("_id") or ""),
            "name": doc.get("name"),
            "source": doc.get("source"),
            "layer": tags.get("shom:layer") or tags.get("noaa:layer"),
            "lat": doc.get("lat"),
            "lon": doc.get("lon"),
            "distance_m": int(round(hit.distance_km * 1000)),
        },
    }


async def marina_control_candidates(db, lat: float | None, lon: float | None) -> list[dict]:
    if lat is None or lon is None or db is None:
        return []
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return []
    q = {
        "lat": {"$gte": lat_f - _DELTA_DEG, "$lte": lat_f + _DELTA_DEG},
        "lon": {"$gte": lon_f - _DELTA_DEG, "$lte": lon_f + _DELTA_DEG},
        "$or": [
            {"shom_id": {"$exists": True, "$nin": [None, ""]}},
            {"tags.shom:layer": "smcfac"},
        ],
    }
    try:
        cur = db.capitaineries.find(q)
        if hasattr(cur, "to_list"):
            return await cur.to_list(30)
        return [doc async for doc in cur]
    except Exception:
        return []


async def marina_control_ref(db, doc: dict) -> dict:
    cands = await marina_control_candidates(db, doc.get("lat"), doc.get("lon"))
    return control_ref_from_candidates(doc.get("lat"), doc.get("lon"), cands)
