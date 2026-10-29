"""Same action site (Projects / ports of entry) — not a building overlay.

Algorithm: Haversine < 500 m AND similarity ≥ 60%, OR similarity ≥ 90% alone.
Non-destructive: never deletes — merges by additive enrichment.

This is not the harbormaster overlay (SHOM / NOAA on OSM, 250 m, distance
only). See ``app.core.identity.find_building``. Do not reuse
``is_duplicate`` to glue two official cards of the same office.
"""
from __future__ import annotations

import difflib
import math
import re
import unicodedata

from app.core.geo import haversine_km

DIST_THRESHOLD_KM = 0.5
SIM_THRESHOLD_LOW = 0.60
SIM_THRESHOLD_HIGH = 0.90


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s)[:40]


def text_similarity(a: str, b: str) -> float:
    """Max of raw ratio, normalized ratio and sorted-token ratio
    (detects "Port de Papeete" vs "Papeete Port")."""
    if not a or not b:
        return 0.0
    a_low, b_low = a.lower().strip(), b.lower().strip()
    raw = difflib.SequenceMatcher(None, a_low, b_low).ratio()
    norm = difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()
    tok_a = " ".join(sorted(re.findall(r"[a-z0-9]+", a_low)))
    tok_b = " ".join(sorted(re.findall(r"[a-z0-9]+", b_low)))
    tokens = difflib.SequenceMatcher(None, tok_a, tok_b).ratio()
    return max(raw, norm, tokens)


def _dedup_thresholds() -> tuple[float, float, float]:
    from app.core.run_rules import get_rule
    return (
        float(get_rule("shared.dedup_dist_km", DIST_THRESHOLD_KM)),
        float(get_rule("shared.dedup_sim_low", SIM_THRESHOLD_LOW)),
        float(get_rule("shared.dedup_sim_high", SIM_THRESHOLD_HIGH)),
    )


def _xy(doc: dict, lat_key: str, lon_key: str):
    try:
        lat, lon = doc.get(lat_key), doc.get(lon_key)
        if lat is None or lon is None or lat == "" or lon == "":
            return None
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lat_f) or not math.isfinite(lon_f):
        return None
    return lat_f, lon_f


def is_duplicate(doc_a: dict, doc_b: dict, lat_key: str = "lat", lon_key: str = "lon",
                 title_key: str = "title") -> bool:
    """True if it is the same action site. Never an OSM↔SHOM overlay."""
    dist_km, sim_low, sim_high = _dedup_thresholds()
    sim = text_similarity(str(doc_a.get(title_key) or ""), str(doc_b.get(title_key) or ""))
    if sim >= sim_high:
        return True
    xy_a, xy_b = _xy(doc_a, lat_key, lon_key), _xy(doc_b, lat_key, lon_key)
    if xy_a is None or xy_b is None:
        return False
    dist = haversine_km(xy_a[0], xy_a[1], xy_b[0], xy_b[1])
    return dist < dist_km and sim >= sim_low


def merge_docs(existing: dict, incoming: dict) -> dict:
    """Non-destructive merge: fill ONLY missing fields of the existing doc.
    Return a MongoDB $set-compatible dict."""
    updates = {}
    for key, value in incoming.items():
        if key.startswith("_"):
            continue
        cur = existing.get(key)
        if cur is None or cur == "" or cur == []:
            if value is not None and value != "" and value != []:
                updates[key] = value
    return updates


def find_duplicate_in_list(candidate: dict, existing_docs: list[dict], lat_key: str = "lat",
                           lon_key: str = "lon", title_key: str = "title"):
    for doc in existing_docs:
        if is_duplicate(candidate, doc, lat_key, lon_key, title_key):
            return doc
    return None


def deduplicate_list(docs: list[dict], lat_key: str = "lat", lon_key: str = "lon",
                     title_key: str = "title") -> list[dict]:
    result: list[dict] = []
    for doc in docs:
        dup = find_duplicate_in_list(doc, result, lat_key, lon_key, title_key)
        if dup is None:
            result.append(doc)
        else:
            dup.update(merge_docs(dup, doc))
    return result


async def upsert_with_dedup(collection, candidate: dict, base_query: dict | None = None,
                            lat_key: str = "lat", lon_key: str = "lon",
                            title_key: str = "title", extra_set: dict | None = None) -> dict:
    """Non-destructive Mongo upsert: merge into an existing duplicate
    (±1° spatial window + fuzzy), else insert. Return {action, id}."""
    lat, lon = candidate.get(lat_key), candidate.get(lon_key)
    query = dict(base_query or {})
    if lat is not None and lon is not None:
        query[lat_key] = {"$gte": float(lat) - 1.0, "$lte": float(lat) + 1.0}
        query[lon_key] = {"$gte": float(lon) - 1.0, "$lte": float(lon) + 1.0}
    nearby = await collection.find(query).limit(300).to_list(300)
    for existing in nearby:
        if is_duplicate(candidate, existing, lat_key, lon_key, title_key):
            updates = merge_docs(existing, candidate)
            if extra_set:
                updates.update(extra_set)
            if updates:
                await collection.update_one({"_id": existing["_id"]}, {"$set": updates})
            return {"action": "merged", "id": str(existing["_id"])}
    await collection.insert_one(candidate)
    return {"action": "inserted", "id": str(candidate.get("_id"))}


def dedup_stats(docs: list[dict], title_key: str = "title") -> dict:
    total = len(docs)
    deduped = deduplicate_list([dict(d) for d in docs], title_key=title_key)
    return {"total_input": total, "after_dedup": len(deduped),
            "duplicates_removed": total - len(deduped)}
