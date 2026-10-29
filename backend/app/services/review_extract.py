"""Extract PoE from Gold documents — never poe_ports."""
from __future__ import annotations

from app.core.dedup import normalize_name
from app.core.extract import extract_cascade, extract_structured_ports
from app.core.geo import geocode_port_dual
from app.services.review_choices import now_iso
from app.services.review_gold import eez_is_published, get_override, gold_key

GEOCODE_CAP = 15


async def extract_gold_ports(db, entity_id: str, log=None) -> dict:
    """Reread only the kept URLs of the Gold snapshot."""
    log = log or (lambda m: None)
    eid = str(entity_id)
    override = await get_override(db, "eez", eid)
    if not eez_is_published(override):
        raise ValueError("gold off: extract needs a certified sheet")
    snap = dict(override.get("snapshot") or {})
    urls = [rec.get("url") for rec in (snap.get("sources_td") or []) if rec.get("url")]
    if not urls:
        snap["ports"] = []
        snap["ports_status"] = "empty"
        await _save_snap(db, eid, override, snap)
        return _out(snap, 0)

    texts: list[str] = []
    used: list[str] = []
    for url in urls:
        try:
            page = await extract_cascade(url, log=log)
        except Exception as e:
            log(f"extract gold {url[:70]}: {type(e).__name__}")
            continue
        text = (page or {}).get("text") or ""
        if text.strip():
            texts.append(f"[SOURCE: {url}]\n{text}")
            used.append(url)

    catalog = extract_structured_ports("\n\n".join(texts)) if texts else []
    ports: list[dict] = []
    seen: set[str] = set()
    for p in catalog:
        name = (p.get("name") or "").strip()
        norm = normalize_name(name)
        if not name or not norm or norm in seen:
            continue
        seen.add(norm)
        ports.append({
            "id": f"{eid}:{norm}",
            "port_id": f"{eid}:{norm}",
            "name": name,
            "city": p.get("city"),
            "lat": p.get("lat"),
            "lon": p.get("lon"),
            "extraction_engine": p.get("extraction_engine") or "catalog",
            "source_urls": list(used),
            "validated": False,
        })

    zone = None
    try:
        zone = await db.eez_zones.find_one({"mrgid": int(eid)})
    except Exception:
        zone = None
    if zone:
        n_geo = 0
        for p in ports:
            if p.get("lat") is not None or n_geo >= GEOCODE_CAP:
                continue
            try:
                geo = await geocode_port_dual(p, zone, log)
            except Exception:
                continue
            for src in ("nominatim", "geonames"):
                xy = (geo or {}).get(src)
                if xy:
                    p["lat"], p["lon"] = xy[0], xy[1]
                    n_geo += 1
                    break

    if not texts:
        snap["ports"] = []
        snap["ports_status"] = "failed"
    elif not ports:
        snap["ports"] = []
        snap["ports_status"] = "empty"
    else:
        snap["ports"] = ports
        snap["ports_status"] = "extracted"
    snap["extracted_at"] = now_iso()
    await _save_snap(db, eid, override, snap)
    log(f"extract gold {eid}: {len(ports)} port(s) status={snap['ports_status']}")
    return _out(snap, len(ports))


async def _save_snap(db, eid: str, override: dict, snap: dict) -> None:
    cid = gold_key("eez", eid)
    payload = dict(override or {})
    payload["_id"] = cid
    payload["kind"] = "eez"
    payload["entity_id"] = eid
    payload["snapshot"] = snap
    await db.review_gold.update_one({"_id": cid}, {"$set": payload}, upsert=True)


def _out(snap: dict, n: int) -> dict:
    return {
        "kind": "eez",
        "id": snap.get("mrgid"),
        "ports_status": snap.get("ports_status"),
        "port_count": n,
        "wrote_poe_ports": False,
    }
