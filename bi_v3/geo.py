"""Geo helpers (ported from Blue Intelligence V2 core/geo.py, trimmed)."""
from __future__ import annotations
import math, time, json, urllib.parse, urllib.request

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

_last = [0.0]
def nominatim(query: str, country_codes: str | None = None) -> dict | None:
    """Free geocoder #1 (1 req/s policy respected)."""
    wait = 1.1 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    q = {"q": query, "format": "jsonv2", "limit": 1}
    if country_codes:
        q["countrycodes"] = country_codes
    req = urllib.request.Request("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(q),
                                 headers={"User-Agent": "BlueIntelligenceV3/0.1 (Berry-Mappemonde)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.load(r)
    except Exception:
        return None
    finally:
        _last[0] = time.time()
    if not res:
        return None
    return {"lat": float(res[0]["lat"]), "lon": float(res[0]["lon"]), "method": "nominatim",
            "osm_type": res[0].get("type"), "display": res[0].get("display_name")}

def geo_agreement(a: dict | None, b: dict | None, max_km: float = 0.5) -> dict:
    if not a or not b or a.get("lat") is None or b.get("lat") is None:
        return {"agree": False, "km": None}
    km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
    return {"agree": km <= max_km, "km": round(km, 3)}
