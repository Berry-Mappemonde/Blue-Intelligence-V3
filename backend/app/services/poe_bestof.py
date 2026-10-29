"""poe_bestof — N-run comparison + “best of worlds” synthesis.

NEVER touches poe_ports / eez_zones. The synthesis is written into a new
versioned run (poe_run_ports / poe_run_zones / poe_runs).

Judges (map of true Ports of Entry, not an engine vote):
  1. place name (not a legal turn of phrase);
  2. point in the EEZ if geocoded;
  3. identity seen in 2+ sources (v1 and/or runs) = strong signal;
  4. validated v1 is the default when a run wipes or pollutes;
  5. graft a run only if geocoded + validated + non-legal.
"""
from __future__ import annotations

import re
import time
import uuid
from collections import defaultdict

from app.core.dedup import find_duplicate_in_list, normalize_name
from app.services.poe_pipeline import now_iso

LEGAL_NOTE = "tournure légale"
_GARBAGE_NORMS = {
    "etat", "peche", "escale", "arrival", "embarkation", "destino",
    "outremer", "interet", "hydrocarbures", "croisiere", "rilevanza",
}
_GARBAGE_HEAD = re.compile(
    r"^(etat|pêche|peche|escale|arrival|embarkation|destino|outre-mer|"
    r"outre mer|interet|intérêt|hydrocarbures|croisiere|croisière|"
    r"described|consists of|areas|rilevanza)\b",
    re.I,
)
_GARBAGE_IN = re.compile(
    r"(consists of| described$| areas$|embarkation|rilevanza|"
    r"intérêt national|interet national|connection channel)",
    re.I,
)
_PREFIX = re.compile(
    r"^(porto de |port of |port de |pkk porti i |puerto de |haven )",
    re.I,
)


def is_legal_fragment(port: dict) -> bool:
    note = port.get("note") or ""
    name = (port.get("name") or "").strip()
    if LEGAL_NOTE in note:
        return True
    if normalize_name(name) in _GARBAGE_NORMS:
        return True
    if _GARBAGE_HEAD.search(name) or _GARBAGE_IN.search(name):
        return True
    return len(normalize_name(name)) < 4


def _alias(port: dict) -> dict:
    name = (port.get("name") or "").strip()
    stripped = _PREFIX.sub("", name).strip()
    return {**port, "name": stripped or name}


def _geo(port: dict) -> bool:
    return port.get("lat") is not None and port.get("lon") is not None


def _match_in(port: dict, pool: list[dict]):
    """Matching with Porto de / Port of aliases (fuzzy alone misses too many)."""
    if port.get("dedup_key"):
        hit = next((x for x in pool if x.get("dedup_key") == port.get("dedup_key")), None)
        if hit:
            return hit
    hit = find_duplicate_in_list(port, pool, title_key="name")
    if hit:
        return hit
    aliased = _alias(port)
    for other in pool:
        if find_duplicate_in_list(aliased, [_alias(other)], title_key="name"):
            return other
    return None


def _slim(port: dict) -> dict:
    return {
        "name": port.get("name"),
        "city": port.get("city"),
        "lat": port.get("lat"),
        "lon": port.get("lon"),
        "validated": bool(port.get("validated")),
        "note": port.get("note"),
        "source_urls": port.get("source_urls") or [],
        "legal": is_legal_fragment(port),
        "geo": _geo(port),
    }


def cluster_zone_ports(origin_lists: dict[str, list[dict]]) -> list[dict]:
    """Group ports of an EEZ by identity. origin_lists = {label: [ports]}."""
    leftover: list[tuple[str, dict]] = []
    for origin, ports in origin_lists.items():
        for p in ports:
            leftover.append((origin, p))

    clusters: list[dict] = []
    while leftover:
        origin, port = leftover.pop(0)
        members = [(origin, port)]
        kept = []
        for o2, p2 in leftover:
            if any(_match_in(p2, [m]) for _, m in members):
                members.append((o2, p2))
            else:
                kept.append((o2, p2))
        leftover = kept
        clusters.append({"members": members})
    return clusters


def pick_cluster(cluster: dict) -> dict | None:
    """Pick the cluster representative, or None if everything is noise."""
    members: list[tuple[str, dict]] = cluster["members"]
    origins = sorted({o for o, _ in members})
    clean = [(o, p) for o, p in members if not is_legal_fragment(p)]
    if not clean:
        return None
    v1 = next((p for o, p in clean if o == "v1"), None)
    validated_geo = next((p for _, p in clean if _geo(p) and p.get("validated")), None)
    any_geo = next((p for _, p in clean if _geo(p)), None)
    chosen = v1 or validated_geo or any_geo or clean[0][1]
    urls: list[str] = []
    for _, p in members:
        for u in p.get("source_urls") or []:
            if u not in urls:
                urls.append(u)
    return {
        **_slim(chosen),
        "name": chosen.get("name"),
        "origins": origins,
        "multi_run": len(origins) >= 2,
        "kept_from": "v1" if v1 is not None else origins[0],
        "source_urls": urls,
        "note": chosen.get("note"),
    }


def synthesize_zone(origin_lists: dict[str, list[dict]]) -> list[dict]:
    out = []
    for cluster in cluster_zone_ports(origin_lists):
        picked = pick_cluster(cluster)
        if picked:
            out.append(picked)
    return out


def _by_mrgid(ports: list[dict]) -> dict[int, list[dict]]:
    d: dict[int, list[dict]] = defaultdict(list)
    for p in ports:
        d[int(p.get("mrgid") or 0)].append(p)
    return d


async def compare_runs(db, run_ids: list[str], include_v1: bool = True) -> dict:
    """Identity statistics + zone verdicts. Read-only."""
    labels = []
    origin_ports: dict[str, list[dict]] = {}
    if include_v1:
        labels.append("v1")
        origin_ports["v1"] = await db.poe_ports.find({}).to_list(20000)
    run_meta = {}
    for rid in run_ids:
        doc = await db.poe_runs.find_one({"_id": rid}) or {}
        variant = (doc.get("params") or {}).get("variant") or doc.get("label") or rid
        label = f"{variant}:{rid[-6:]}" if rid in origin_ports else (doc.get("label") or variant or rid)
        # unique label
        base = doc.get("params", {}).get("variant") or "run"
        label = f"{base}:{rid}"
        labels.append(label)
        origin_ports[label] = await db.poe_run_ports.find({"run_id": rid}).to_list(20000)
        run_meta[label] = {
            "run_id": rid, "label": doc.get("label"), "variant": base,
            "state": doc.get("state"), "zones_done": doc.get("zones_done"),
            "ports": len(origin_ports[label]),
        }

    by = {k: _by_mrgid(v) for k, v in origin_ports.items()}
    mrgids = set()
    for d in by.values():
        mrgids |= set(d)
    zone_names = {}
    for k, d in by.items():
        for m, ports in d.items():
            if ports and ports[0].get("zone_name"):
                zone_names[m] = ports[0]["zone_name"]

    buckets = defaultdict(int)
    zone_rows = []
    for mrgid in sorted(mrgids):
        lists = {lab: by[lab].get(mrgid, []) for lab in labels}
        chosen = synthesize_zone(lists)
        counts = {lab: len(lists[lab]) for lab in labels}
        legal = {lab: sum(1 for p in lists[lab] if is_legal_fragment(p)) for lab in labels}
        multi = sum(1 for p in chosen if p.get("multi_run"))
        buckets["kept"] += len(chosen)
        buckets["multi_run"] += multi
        buckets["legal_dropped"] += sum(legal.values())
        zone_rows.append({
            "mrgid": mrgid,
            "name": zone_names.get(mrgid),
            "counts": counts,
            "legal": legal,
            "kept": len(chosen),
            "multi_run": multi,
        })

    return {
        "labels": labels,
        "runs": run_meta,
        "include_v1": include_v1,
        "buckets": dict(buckets),
        "zones_compared": len(zone_rows),
        "kept_total": buckets["kept"],
        "zones": zone_rows,
    }


async def synthesize_best_of(db, run_ids: list[str], include_v1: bool = True,
                             dest_run_id: str | None = None, label: str = "") -> dict:
    """Write a synthetic map into a new run. poe_ports intact."""
    from app.services.poe_runs import new_run_id, ensure_run_indexes
    await ensure_run_indexes(db)
    dest_run_id = dest_run_id or new_run_id()
    label = label or "best-of-" + "-".join(run_ids)
    t0 = time.time()

    origin_ports: dict[str, list[dict]] = {}
    if include_v1:
        origin_ports["v1"] = await db.poe_ports.find({}).to_list(20000)
    for rid in run_ids:
        doc = await db.poe_runs.find_one({"_id": rid}) or {}
        variant = (doc.get("params") or {}).get("variant") or "run"
        origin_ports[f"{variant}:{rid}"] = await db.poe_run_ports.find(
            {"run_id": rid}).to_list(20000)

    v1_count_before = await db.poe_ports.count_documents({})

    by = {k: _by_mrgid(v) for k, v in origin_ports.items()}
    mrgids = set()
    for d in by.values():
        mrgids |= set(d)

    inserted = 0
    zone_docs = []
    for mrgid in sorted(mrgids):
        lists = {lab: by[lab].get(mrgid, []) for lab in origin_ports}
        picked = synthesize_zone(lists)
        name = None
        iso = None
        for ports in lists.values():
            if ports:
                name = ports[0].get("zone_name") or name
                iso = ports[0].get("country_iso2") or iso
        docs = []
        for p in picked:
            docs.append({
                "_id": str(uuid.uuid4()),
                "run_id": dest_run_id,
                "mrgid": mrgid,
                "zone_name": name,
                "country_iso2": iso,
                "name": p["name"],
                "city": p.get("city"),
                "note": p.get("note"),
                "lat": p.get("lat"),
                "lon": p.get("lon"),
                "validated": p.get("validated"),
                "source_urls": p.get("source_urls") or [],
                "origins": p.get("origins"),
                "kept_from": p.get("kept_from"),
                "multi_run": p.get("multi_run"),
                "pipeline_variant": "bestof",
                "extracted_at": now_iso(),
                "dedup_key": f"{mrgid}:{normalize_name(p.get('name'))}",
            })
        if docs:
            await db.poe_run_ports.insert_many(docs)
            inserted += len(docs)
        status = "ia" if docs else "erreur"
        zone_doc = {
            "run_id": dest_run_id, "mrgid": mrgid, "name": name,
            "status": status, "poe_count": len(docs),
            "pipeline_variant": "bestof", "generated_at": now_iso(),
        }
        await db.poe_run_zones.update_one(
            {"run_id": dest_run_id, "mrgid": mrgid}, {"$set": zone_doc}, upsert=True)
        zone_docs.append(zone_doc)

    v1_count_after = await db.poe_ports.count_documents({})
    if v1_count_after != v1_count_before:
        raise RuntimeError("best-of a modifié poe_ports — abort")

    summary = {
        "run_id": dest_run_id,
        "zones_total": len(zone_docs),
        "zones_done": len(zone_docs),
        "ports_total": inserted,
        "by_status": {
            "ia": sum(1 for z in zone_docs if z["status"] == "ia"),
            "erreur": sum(1 for z in zone_docs if z["status"] == "erreur"),
        },
        "errors": 0,
        "cancelled": False,
        "duration_s": round(time.time() - t0, 1),
        "synthetic": True,
        "sources": run_ids,
        "include_v1": include_v1,
    }
    from app.core.run_rules import attach_rules, snapshot_for_run
    from app.services.run_fingerprint import build_code_fingerprint, merge_run_params
    rules = snapshot_for_run(mode="formalities", settings={})
    bestof_params = attach_rules(merge_run_params(
        {"variant": "bestof", "sources": run_ids, "include_v1": include_v1,
         "profile": rules["profile"]},
        build_code_fingerprint({}, zone_timeout_s=0),
    ), rules)
    await db.poe_runs.update_one({"_id": dest_run_id}, {"$set": {
        "label": label,
        "params": bestof_params,
        "state": "done",
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "zones_total": len(zone_docs),
        "zones_done": len(zone_docs),
        "summary": summary,
        "synthetic": True,
        "created_at": now_iso(),
    }}, upsert=True)
    return summary
