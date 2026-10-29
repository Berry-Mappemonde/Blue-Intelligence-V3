"""poe_diff — Port-by-port comparison of two PoE sets (v1 ↔ run).

Matching by zone (mrgid):
  1. exact dedupe key (mrgid + normalized name);
  2. else dedup_core spatial-text fuzzy (similarity > 60% + Haversine
     < 500 m, or similarity > 90% alone).

Classification of matched ports (stackable flags):
  - renamed   : displayed name changed (different normalization);
  - moved     : both geocoded and more than moved_km apart;
  - resourced : the set of source domains changed.
Unmatched: added (in the run only) / removed (in the database only).
"""
from app.core.dedup import find_duplicate_in_list, normalize_name
from app.core.geo import haversine_km

MOVED_KM_DEFAULT = 2.0


def _slim(p: dict) -> dict:
    return {
        "name": p.get("name"), "city": p.get("city"),
        "lat": p.get("lat"), "lon": p.get("lon"),
        "validated": bool(p.get("validated")),
        "source_urls": p.get("source_urls") or [],
        "osm_confidence": p.get("osm_confidence"),
        "extraction_agreement": p.get("extraction_agreement"),
        "geocode_agree": p.get("geocode_agree"),
        "note": p.get("note"),
    }


def _domains(urls: list) -> set:
    out = set()
    for u in urls or []:
        try:
            host = str(u).split("//", 1)[-1].split("/", 1)[0].lower()
            out.add(host[4:] if host.startswith("www.") else host)
        except Exception:
            continue
    return out


def diff_ports(baseline: list[dict], candidate: list[dict],
               moved_km: float = MOVED_KM_DEFAULT) -> dict:
    """Port-by-port diff. baseline = v1 (poe_ports), candidate = run ports.
    Return {summary, zones, matched, added, removed}.
    """
    base_by_zone: dict[int, list[dict]] = {}
    for p in baseline:
        base_by_zone.setdefault(int(p.get("mrgid") or 0), []).append(p)
    cand_by_zone: dict[int, list[dict]] = {}
    for p in candidate:
        cand_by_zone.setdefault(int(p.get("mrgid") or 0), []).append(p)

    matched, added, removed = [], [], []
    zone_rows = []
    for mrgid in sorted(set(base_by_zone) | set(cand_by_zone)):
        base_left = list(base_by_zone.get(mrgid, []))
        cands = cand_by_zone.get(mrgid, [])
        zone_name = ((cands or base_left or [{}])[0]).get("zone_name")
        z_matched = z_added = 0
        for cp in cands:
            match = next((b for b in base_left
                          if b.get("dedup_key") == cp.get("dedup_key")), None)
            if match is None:
                match = find_duplicate_in_list(cp, base_left, title_key="name")
            if match is None:
                added.append({"mrgid": mrgid, "zone_name": zone_name, **_slim(cp)})
                z_added += 1
                continue
            base_left.remove(match)
            z_matched += 1
            flags = []
            move_km = None
            if normalize_name(match.get("name")) != normalize_name(cp.get("name")):
                flags.append("renamed")
            if all(v is not None for v in (match.get("lat"), match.get("lon"),
                                           cp.get("lat"), cp.get("lon"))):
                move_km = round(haversine_km(match["lat"], match["lon"],
                                             cp["lat"], cp["lon"]), 2)
                if move_km > moved_km:
                    flags.append("moved")
            d1, d2 = _domains(match.get("source_urls")), _domains(cp.get("source_urls"))
            if d1 != d2:
                flags.append("resourced")
            matched.append({
                "mrgid": mrgid, "zone_name": zone_name,
                "flags": flags, "move_km": move_km,
                "v1": _slim(match), "v2": _slim(cp),
            })
        for b in base_left:
            removed.append({"mrgid": mrgid, "zone_name": zone_name or b.get("zone_name"),
                            **_slim(b)})
        zone_rows.append({
            "mrgid": mrgid, "zone_name": zone_name,
            "v1_count": len(base_by_zone.get(mrgid, [])),
            "v2_count": len(cands),
            "matched": z_matched, "added": z_added,
            "removed": len(base_left),
        })

    flag_counts: dict[str, int] = {}
    for m in matched:
        for f in m["flags"]:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    unchanged = sum(1 for m in matched if not m["flags"])
    summary = {
        "baseline_total": len(baseline),
        "candidate_total": len(candidate),
        "matched": len(matched),
        "unchanged": unchanged,
        "renamed": flag_counts.get("renamed", 0),
        "moved": flag_counts.get("moved", 0),
        "resourced": flag_counts.get("resourced", 0),
        "added": len(added),
        "removed": len(removed),
        "zones_compared": len(zone_rows),
        "moved_km_threshold": moved_km,
    }
    return {"summary": summary, "zones": zone_rows,
            "matched": matched, "added": added, "removed": removed}


async def diff_run_vs_baseline(db, run_id: str, ports_coll: str = "poe_run_ports",
                               moved_km: float = MOVED_KM_DEFAULT,
                               restrict_to_run_zones: bool = True) -> dict:
    """Diff a run space against the v1 database (poe_ports).
    restrict_to_run_zones: compare only zones the run actually generated
    (fair for partial runs).
    """
    candidate = await db[ports_coll].find({"run_id": run_id}).to_list(20000)
    run_mrgids = {z["mrgid"] async for z in db.poe_run_zones.find(
        {"run_id": run_id}, {"mrgid": 1})}
    q: dict = {}
    if restrict_to_run_zones:
        q["mrgid"] = {"$in": sorted(run_mrgids)}
    baseline = await db.poe_ports.find(q).to_list(20000)
    out = diff_ports(baseline, candidate, moved_km=moved_km)
    out["summary"]["run_id"] = run_id
    out["summary"]["restricted_to_run_zones"] = restrict_to_run_zones
    return out
