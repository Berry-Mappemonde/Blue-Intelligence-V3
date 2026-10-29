"""osm_validate.py — Bottom-Up validation of existing PoE via Overpass (OSM).

Loop over ports of entry already in the database and query the Overpass API around
each point (harbour, marina, customs, border_control, seamark…) to
assign an `osm_confidence` index — WITHOUT ever changing the
name, coordinates, or redoing text extraction.
"""
import asyncio
import time

import httpx

# overpass.openstreetmap.fr is the only mirror reachable from this container
OVERPASS_ENDPOINTS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
UA = {"User-Agent": "BerryMappemonde-BlueIntelligence/1.0 (contact: clementfilisetti@berrymappemonde.org)"}

_endpoint_idx = 0


def _build_query(lat: float, lon: float, radius_m: int) -> str:
    around = f"around:{radius_m},{lat:.6f},{lon:.6f}"
    return f"""[out:json][timeout:25];
(
  nwr({around})["harbour"];
  nwr({around})["port_of_entry"];
  nwr({around})["leisure"="marina"];
  nwr({around})["seamark:type"="harbour"];
  nwr({around})["seamark:type"="harbour_basin"];
  nwr({around})["seamark:harbour:category"];
  nwr({around})["customs"];
  nwr({around})["barrier"="border_control"];
  nwr({around})["government"~"customs|border_control|immigration"];
  nwr({around})["amenity"="ferry_terminal"];
  nwr({around})["landuse"="harbour"];
  nwr({around})["landuse"="port"];
  nwr({around})["water"="harbour"];
  nwr({around})["industrial"="port"];
);
out tags 80;"""


async def overpass_around(lat: float, lon: float, radius_m: int = 3000, log=None) -> list[dict]:
    global _endpoint_idx
    log = log or (lambda m: None)
    last_err = None
    for attempt in range(len(OVERPASS_ENDPOINTS)):
        url = OVERPASS_ENDPOINTS[(_endpoint_idx + attempt) % len(OVERPASS_ENDPOINTS)]
        try:
            async with httpx.AsyncClient(timeout=35, headers=UA) as client:
                r = await client.post(url, data={"data": _build_query(lat, lon, radius_m)})
                if r.status_code == 429:
                    await asyncio.sleep(5)
                    continue
                r.raise_for_status()
                _endpoint_idx = (_endpoint_idx + attempt) % len(OVERPASS_ENDPOINTS)
                return r.json().get("elements") or []
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"all overpass mirrors failed: {type(last_err).__name__}: {str(last_err)[:80]}")


def score_confidence(elements: list[dict]) -> tuple[float, list[str], int]:
    """(confidence 0-1, tags found, element count).
    Port infrastructure → max 0.5; customs/border evidence → max 0.4;
    ferry terminal / port area → +0.1.
    """
    tags_found = set()
    infra = customs = extra = 0.0
    for el in elements:
        t = el.get("tags") or {}
        if t.get("harbour") or t.get("seamark:type") == "harbour" or t.get("seamark:harbour:category"):
            infra = max(infra, 0.5)
            tags_found.add("harbour" if t.get("harbour") else "seamark:harbour")
        if t.get("seamark:type") == "harbour_basin" or t.get("water") == "harbour":
            infra = max(infra, 0.45)
            tags_found.add("harbour_basin")
        if t.get("leisure") == "marina":
            infra = max(infra, 0.45)
            tags_found.add("leisure=marina")
        if (t.get("port_of_entry") or "") in ("yes", "all"):
            customs = max(customs, 0.5)
            tags_found.add("port_of_entry=yes")
        if t.get("customs") or (t.get("government") or "") in ("customs",) or "customs" in (t.get("government") or ""):
            customs = max(customs, 0.4)
            tags_found.add("customs")
        if t.get("barrier") == "border_control" or "border_control" in (t.get("government") or ""):
            customs = max(customs, 0.4)
            tags_found.add("border_control")
        if "immigration" in (t.get("government") or ""):
            customs = max(customs, 0.35)
            tags_found.add("government=immigration")
        if t.get("amenity") == "ferry_terminal":
            extra = max(extra, 0.1)
            tags_found.add("amenity=ferry_terminal")
        if t.get("landuse") in ("port", "harbour") or t.get("industrial") == "port":
            extra = max(extra, 0.1)
            tags_found.add("port_area")
    return round(min(1.0, infra + customs + extra), 3), sorted(tags_found), len(elements)


async def validate_ports(db, state, only_unchecked: bool = True, limit: int = 0,
                         radius_m: int = 3000) -> dict:
    """Background task: enrich each PoE with osm_confidence / osm_tags.
    Touch NEITHER name NOR lat/lon NOR extraction fields.
    """
    q: dict = {"lat": {"$ne": None}, "lon": {"$ne": None}}
    if only_unchecked:
        q["osm_checked_at"] = {"$exists": False}
    ports = await db.poe_ports.find(q).to_list(20000)
    if limit > 0:
        ports = ports[:limit]
    state.total = len(ports)
    state.log(f"{len(ports)} PoE à valider via Overpass (rayon {radius_m} m, "
              f"{'non vérifiés seulement' if only_unchecked else 'tous'})")
    summary = {"checked": 0, "high_confidence": 0, "medium": 0, "no_osm_match": 0, "failed": 0}
    for i, p in enumerate(ports):
        if state.cancel:
            state.log("annulation demandée — arrêt propre")
            break
        try:
            elements = await overpass_around(p["lat"], p["lon"], radius_m)
            conf, tags, n = score_confidence(elements)
            fresh = {**p, "osm_confidence": conf, "osm_tags": tags}
            updates = {
                "osm_confidence": conf,
                "osm_tags": tags,
                "osm_matches": n,
                "osm_checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            try:
                from app.services.poe_confidence import apply_score, score_port
                official = any(
                    "gouv" in (u or "") or "gov" in (u or "") or "gob" in (u or "")
                    for u in (p.get("source_urls") or [])
                )
                apply_score(fresh, score_port(fresh, official_source=official))
                updates["confidence"] = fresh.get("confidence")
                updates["confidence_parts"] = fresh.get("confidence_parts")
                updates["confidence_reasons"] = fresh.get("confidence_reasons")
            except Exception:
                pass
            await db.poe_ports.update_one({"_id": p["_id"]}, {"$set": updates})
            summary["checked"] += 1
            if conf >= 0.5:
                summary["high_confidence"] += 1
            elif conf > 0:
                summary["medium"] += 1
            else:
                summary["no_osm_match"] += 1
            state.log(f"[{p.get('zone_name')}] {p['name']}: confiance {conf} ({', '.join(tags) or 'aucun tag'})")
        except Exception as e:
            summary["failed"] += 1
            state.log(f"[{p.get('zone_name')}] {p['name']}: FAILED {type(e).__name__}: {str(e)[:80]}")
        state.progress = i + 1
        await asyncio.sleep(1.2)  # politesse Overpass
    state.log(f"validation terminée: {summary}")
    return summary
