"""GPS audit of `confirmed` seeds — Nominatim homonyms, inland_river.

Does not change `verify_verdict` / `confirmation_status`. Does not run
`seeds/build`. Writes `poe_ports` only if the same key there carries the same
aberrant GPS.

Tanjung Pinang and Bandar Bintan Telani stay two distinct marinas:
never copy one GPS onto the other.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from app.core.dedup import normalize_name, text_similarity
from app.core.geo import (
    haversine_km,
    inland_exception_flags,
    spatial_class_for_point,
)
from app.services.listing_ref import project_listing
from app.services.poe_gps_registry import accepted_by_key
from app.services.poe_pipeline import MAP_FILE, now_iso

INLAND_FAR_KM = 30.0
OUTSIDE_EEZ_FAR_KM = 15.0
OTHER_WATER_FAR_KM = 8.0
OBS_BETTER_KM = 80.0
PAREN_PEER_KM = 300.0
GROUP_OUTLIER_KM = 300.0
GROUP_OUTLIER_IN_EEZ_KM = 1500.0

REASON_INLAND_FAR = "inland_far"
REASON_OUTSIDE_EEZ_FAR = "outside_eez_far"
REASON_HOMONYM_PAREN = "homonym_paren_mismatch"
REASON_GROUP_OUTLIER = "listing_group_outlier"
REASON_NOMINATIM_INLAND = "nominatim_inland_listing_only"

COASTAL_OK = {"in_eez", "coastal_land"}
INLAND_KINDS = {"inland_river", "inland", "other_water"}

TANJUNG_PINANG_KEY = "8492:tanjungpinangbintanislandriauislands"
# Bintan island centroid (not Bandar Bintan Telani 1.1605 / 104.3202).
# Arbitrated lat/lon live in docs/data/poe-gps-arbitrated.json.
BINTAN_CLUSTER = (1.08, 104.42)
BANDAR_BINTAN_TELANI_KEY = "8492:bandarbintantelani"

# Parenthesis tokens → island centroid, never another marina's GPS.
ISLAND_CLUSTER_GPS: dict[str, tuple[float, float]] = {
    "bintan": BINTAN_CLUSTER,
    "bintanisland": BINTAN_CLUSTER,
}

_PAREN_RE = re.compile(r"\((.+)\)")
_STRIP_PAREN = re.compile(r"\([^)]*\)")
_PORT_PREFIX = re.compile(
    r"\b(port of|porto de|puerto de|harbour|harbor|marina)\b", re.I)
_STOP_TOKENS = {
    "island", "islands", "city", "port", "harbour", "harbor", "marina",
    "coast", "west", "east", "north", "south",
}


@lru_cache(maxsize=1)
def load_eez_geoms(path: str | None = None) -> dict[int, object]:
    from shapely.geometry import shape
    p = Path(path) if path else MAP_FILE
    data = json.loads(p.read_text(encoding="utf-8"))
    out: dict[int, object] = {}
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        mid = props.get("mrgid") if props.get("mrgid") is not None else props.get("MRGID")
        geom = feat.get("geometry")
        if mid is None or not geom:
            continue
        try:
            out[int(mid)] = shape(geom)
        except Exception:
            continue
    return out


def core_name_norm(name: str) -> str:
    """Comparable name: no parentheses, no “Port of”, no numbers."""
    n = _STRIP_PAREN.sub(" ", name or "")
    n = _PORT_PREFIX.sub(" ", n)
    n = re.sub(r"\b\d+\b", " ", n)
    return normalize_name(n)


def _coords(doc: dict) -> tuple[float, float] | None:
    try:
        lat, lon = doc.get("lat"), doc.get("lon")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _same_gps(a: dict, lat: float, lon: float, tol: float = 1e-4) -> bool:
    xy = _coords(a)
    if xy is None:
        return False
    return abs(xy[0] - lat) <= tol and abs(xy[1] - lon) <= tol


def _seed_key(seed: dict) -> str:
    return str(seed.get("dedup_key") or seed.get("_id") or "")


def _mid(seed: dict) -> int | None:
    try:
        return int(seed["mrgid"]) if seed.get("mrgid") is not None else None
    except (TypeError, ValueError):
        return None


def _listing_only(seed: dict) -> bool:
    src = [s for s in (seed.get("seed_sources") or []) if s]
    return src == ["listing"]


def _geom_for(seed: dict, geoms: dict[int, object] | None):
    mid = _mid(seed)
    if mid is None:
        return None
    return (geoms or {}).get(mid)


def classify_seed_point(seed: dict, geoms: dict[int, object] | None) -> dict:
    pts = _coords(seed)
    geom = _geom_for(seed, geoms)
    if pts is None:
        return {"kind": "no_coords", "validated": False, "dist_km": None}
    if geom is None:
        return {"kind": "no_geom", "validated": False, "dist_km": None}
    inland = inland_exception_flags(
        seed, {"iso2": seed.get("country_iso2")}, official_list=True)
    return spatial_class_for_point(pts[0], pts[1], geom, inland=inland)


def _paren_tokens(*names: str) -> list[str]:
    tokens: list[str] = []
    for raw in names:
        if not raw:
            continue
        m = _PAREN_RE.search(raw)
        if not m:
            continue
        for bit in re.split(r"\s*[,/;&–-]\s*", m.group(1)):
            bit = bit.strip()
            if len(bit) >= 4:
                tokens.append(bit)
            for w in re.findall(r"[A-Za-zÀ-ÿ]{4,}", bit):
                if w.lower() not in _STOP_TOKENS:
                    tokens.append(w)
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _spatial_reasons(spatial: dict) -> list[str]:
    kind = spatial.get("kind")
    dist = spatial.get("dist_km")
    dist_f = float(dist) if dist is not None else None
    reasons: list[str] = []
    if kind in INLAND_KINDS and dist_f is not None:
        if kind == "inland_river" and dist_f > INLAND_FAR_KM:
            reasons.append(REASON_INLAND_FAR)
        elif kind in ("inland", "other_water") and dist_f > OTHER_WATER_FAR_KM:
            reasons.append(REASON_INLAND_FAR)
    # inland_river ≤ 30 km = accepted river exception (Shanghai, Bristol):
    # not “far outside EEZ”. Beyond that, same threshold as inland_far.
    if kind not in COASTAL_OK and dist_f is not None:
        far = INLAND_FAR_KM if kind == "inland_river" else OUTSIDE_EEZ_FAR_KM
        if dist_f > far:
            reasons.append(REASON_OUTSIDE_EEZ_FAR)
    return list(dict.fromkeys(reasons))


def _best_same_name_in_eez_obs(
    seed: dict, geoms: dict[int, object] | None, spatial: dict | None = None,
) -> dict:
    """in_eez / coastal observation of the same nom_norm, far from the kept GPS."""
    kind = (spatial or {}).get("kind")
    if kind in COASTAL_OK:
        return {}
    seed_xy = _coords(seed)
    geom = _geom_for(seed, geoms)
    if seed_xy is None or geom is None:
        return {}
    want = core_name_norm(seed.get("name") or "")
    if not want:
        return {}
    inland = inland_exception_flags(
        seed, {"iso2": seed.get("country_iso2")}, official_list=True)
    better = []
    for obs in seed.get("observations") or []:
        xy = _coords(obs)
        if xy is None:
            continue
        onorm = core_name_norm(obs.get("name") or seed.get("name") or "")
        if onorm != want:
            continue
        d = haversine_km(seed_xy[0], seed_xy[1], xy[0], xy[1])
        if d < OBS_BETTER_KM:
            continue
        cls = spatial_class_for_point(xy[0], xy[1], geom, inland=inland)
        if cls.get("kind") in COASTAL_OK and cls.get("validated"):
            better.append((d, obs, cls, xy))
    if not better:
        return {}
    want_name = seed.get("listing_name") or seed.get("name") or ""

    def _rank(t):
        _d, obs, cls, _xy = t
        kind = cls.get("kind")
        dist = cls.get("dist_km")
        dist_f = float(dist) if dist is not None else 999.0
        # in_eez sliver (0.0 vs 0.1 km) must not beat a better name
        coast_rank = 0 if kind == "in_eez" or dist_f <= 2.2 else (
            1 if kind == "coastal_land" else 2)
        return (
            coast_rank,
            -text_similarity(obs.get("name") or "", want_name),
            dist_f,
            -_d,
        )

    better.sort(key=_rank)
    _, obs, _, xy = better[0]
    return {
        "suggested_lat": xy[0],
        "suggested_lon": xy[1],
        "suggested_source": "observation",
        "suggested_origin": obs.get("origin") or "observation",
        "suggested_name": obs.get("name"),
    }


def _island_cluster_for(tokens: list[str]) -> tuple[float, float] | None:
    for t in tokens:
        hit = ISLAND_CLUSTER_GPS.get(normalize_name(t))
        if hit:
            return hit
    return None


def _paren_mismatch(seed: dict, by_zone: dict[int, list[dict]],
                    spatial: dict | None = None) -> tuple[list[str], dict]:
    """The name cites an island/region; the GPS is not near that island's peers."""
    kind = (spatial or {}).get("kind")
    xy = _coords(seed)
    mid = _mid(seed)
    if xy is None or mid is None:
        return [], {}
    tokens = _paren_tokens(seed.get("name") or "", seed.get("listing_name") or "")
    tokens = [t for t in tokens if len(t) >= 4]
    if not tokens:
        return [], {}
    # A coastal in_eez confirmed is not a Nominatim homonym (Sidney BC).
    if kind in COASTAL_OK:
        return [], {}
    peers = []
    key = _seed_key(seed)
    for other in by_zone.get(mid) or []:
        if _seed_key(other) == key:
            continue
        oxy = _coords(other)
        if oxy is None:
            continue
        oname = f"{other.get('name') or ''} {other.get('listing_name') or ''}"
        if any(t.lower() in oname.lower() for t in tokens):
            peers.append((other, oxy))
    extra: dict = {}
    cluster = _island_cluster_for(tokens)
    if cluster:
        dist = haversine_km(xy[0], xy[1], cluster[0], cluster[1])
        if dist > PAREN_PEER_KM:
            extra = {
                "suggested_lat": cluster[0],
                "suggested_lon": cluster[1],
                "suggested_source": "island_cluster",
                "peer_dist_km": round(dist, 1),
                "peer_names": [p[0].get("name") for p in peers[:8]],
            }
            return [REASON_HOMONYM_PAREN], extra
        return [], {}
    if not peers:
        return [], {}
    # Cluster centroid, not a single marina's GPS.
    if len(peers) < 2:
        return [REASON_HOMONYM_PAREN] if haversine_km(
            xy[0], xy[1], peers[0][1][0], peers[0][1][1]
        ) > PAREN_PEER_KM else [], {
            "peer_names": [peers[0][0].get("name")],
            "peer_dist_km": round(haversine_km(
                xy[0], xy[1], peers[0][1][0], peers[0][1][1]), 1),
        }
    ml = sum(p[1][0] for p in peers) / len(peers)
    mn = sum(p[1][1] for p in peers) / len(peers)
    dist = haversine_km(xy[0], xy[1], ml, mn)
    if dist <= PAREN_PEER_KM:
        return [], {}
    return [REASON_HOMONYM_PAREN], {
        "peer_dist_km": round(dist, 1),
        "peer_names": [p[0].get("name") for p in peers[:8]],
        "suggested_lat": round(ml, 7),
        "suggested_lon": round(mn, 7),
        "suggested_source": "island_cluster",
    }


def _diameter_km(pts: list[tuple[float, float]]) -> float:
    best = 0.0
    for i, a in enumerate(pts):
        for b in pts[i + 1:]:
            best = max(best, haversine_km(a[0], a[1], b[0], b[1]))
    return best


def _listing_group_map(listing_ports: list[dict] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in listing_ports or []:
        grp = (p.get("group") or "").strip()
        if not grp:
            continue
        key = p.get("dedup_key")
        if key:
            out[str(key)] = grp
        mid = p.get("mrgid")
        name = (p.get("name") or "").strip()
        if mid is not None and name:
            out[f"{int(mid)}:{name}"] = grp
            out[f"{int(mid)}:{normalize_name(name)}"] = grp
    return out


def _seed_group(seed: dict, groups: dict[str, str]) -> str | None:
    key = _seed_key(seed)
    if key and groups.get(key):
        return groups[key]
    mid = _mid(seed)
    if mid is None:
        return None
    for name in (seed.get("listing_name"), seed.get("name")):
        if not name:
            continue
        hit = groups.get(f"{mid}:{name}") or groups.get(f"{mid}:{normalize_name(name)}")
        if hit:
            return hit
    return None


def _group_outlier(seed: dict, members: list[dict],
                   spatial: dict | None = None,
                   spatial_by_key: dict[str, dict] | None = None) -> tuple[list[str], dict]:
    """Isolated vs the group's *healthy* cluster (in_eez / coast), not vs a rotten GPS.

    BBT ↔ Tarempa (~310 km) stay together. Tanjung Pinang Sumatra (>300 km
    and outside the EEZ) is the outlier. Astoria NY vs west coast: in_eez but >1500 km.
    """
    xy = _coords(seed)
    if xy is None:
        return [], {}
    key = _seed_key(seed)
    healthy: list[tuple[float, float]] = []
    for o in members:
        if _seed_key(o) == key:
            continue
        oxy = _coords(o)
        if oxy is None:
            continue
        okind = ((spatial_by_key or {}).get(_seed_key(o)) or {}).get("kind")
        if okind in COASTAL_OK:
            healthy.append(oxy)
    if not healthy:
        return [], {}
    nearest = min(haversine_km(xy[0], xy[1], p[0], p[1]) for p in healthy)
    kind = (spatial or {}).get("kind")
    from app.core.run_rules import get_rule
    in_eez_km = float(get_rule("formalities.basin_split_km", GROUP_OUTLIER_IN_EEZ_KM))
    near_km = float(get_rule("formalities.peer_near_km", GROUP_OUTLIER_KM))
    if kind in COASTAL_OK:
        if nearest <= in_eez_km:
            return [], {}
    elif nearest <= near_km:
        return [], {}
    diam = _diameter_km(healthy) if len(healthy) >= 2 else 0.0
    ml = sum(p[0] for p in healthy) / len(healthy)
    mn = sum(p[1] for p in healthy) / len(healthy)
    extra = {
        "group_dist_km": round(nearest, 1),
        "group_diameter_km": round(diam, 1),
        "suggested_lat": round(ml, 7),
        "suggested_lon": round(mn, 7),
        "suggested_source": "listing_group_cluster",
    }
    return [REASON_GROUP_OUTLIER], extra


def _severity(reasons: list[str], spatial: dict, seed: dict, extra: dict) -> str:
    dist = spatial.get("dist_km") or 0
    high = {
        REASON_HOMONYM_PAREN, REASON_GROUP_OUTLIER, REASON_NOMINATIM_INLAND,
    }
    if any(r in high for r in reasons):
        return "high"
    if extra.get("suggested_source") == "observation":
        return "high"
    if REASON_INLAND_FAR in reasons and dist > 80:
        return "high"
    if _seed_key(seed) == TANJUNG_PINANG_KEY:
        return "high"
    return "medium"


def _already_at(seed: dict, lat: float, lon: float, tol: float = 5e-3) -> bool:
    xy = _coords(seed)
    if xy is None:
        return False
    return abs(xy[0] - lat) <= tol and abs(xy[1] - lon) <= tol


def _plan_correction(seed: dict, spatial: dict, extra: dict,
                     reasons: list[str]) -> dict | None:
    """Obvious cases + GPS from the git registry (accepted)."""
    key = _seed_key(seed)
    hit = accepted_by_key(key)
    if hit:
        if hit.get("action") == "keep":
            return None
        if hit.get("action") == "correct":
            lat, lon = float(hit["lat"]), float(hit["lon"])
            if _already_at(seed, lat, lon):
                return None
            src = hit.get("geocode_source") or "manual_audit"
            return {"lat": lat, "lon": lon, "geocode_source": src}
    if key == TANJUNG_PINANG_KEY:
        lat, lon = BINTAN_CLUSTER
        if extra.get("suggested_source") == "island_cluster" and extra.get("suggested_lat"):
            lat = float(extra["suggested_lat"])
            lon = float(extra["suggested_lon"])
        return {
            "lat": lat, "lon": lon, "geocode_source": "manual_audit",
        }
    kind = spatial.get("kind")
    dist = spatial.get("dist_km")
    if kind in COASTAL_OK:
        return None
    if dist is None or float(dist) <= INLAND_FAR_KM:
        return None
    if extra.get("suggested_source") == "observation" and extra.get("suggested_lat") is not None:
        return {
            "lat": float(extra["suggested_lat"]),
            "lon": float(extra["suggested_lon"]),
            "geocode_source": "observation",
        }
    return None


def flag_confirmed_seeds(
    seeds: list[dict], *,
    geoms: dict[int, object] | None = None,
    listing_ports: list[dict] | None = None,
) -> list[dict]:
    """Input: seed docs. Output: typed flags (does not mutate the docs)."""
    geoms = geoms if geoms is not None else load_eez_geoms()
    if listing_ports is None:
        listing_ports = project_listing().get("ports") or []
    groups = _listing_group_map(listing_ports)
    confirmed = [
        s for s in seeds
        if (s.get("verify_verdict") or s.get("confirmation_status") or "") == "confirmed"
        and _coords(s)
    ]
    by_zone: dict[int, list[dict]] = defaultdict(list)
    by_group: dict[tuple, list[dict]] = defaultdict(list)
    seed_group: dict[str, str | None] = {}
    for s in confirmed:
        mid = _mid(s)
        if mid is not None:
            by_zone[mid].append(s)
        grp = _seed_group(s, groups)
        seed_group[_seed_key(s)] = grp
        if grp and mid is not None:
            by_group[(mid, grp)].append(s)

    spatial_by_key: dict[str, dict] = {
        _seed_key(s): classify_seed_point(s, geoms) for s in confirmed
    }

    flags: list[dict] = []
    for seed in confirmed:
        spatial = spatial_by_key[_seed_key(seed)]
        key = _seed_key(seed)
        hit = accepted_by_key(key)
        if hit and hit.get("action") == "keep":
            continue
        reasons = _spatial_reasons(spatial)
        extra: dict = {}
        obs_extra = _best_same_name_in_eez_obs(seed, geoms, spatial)
        extra.update(obs_extra)
        paren_reasons, paren_extra = _paren_mismatch(seed, by_zone, spatial)
        reasons.extend(paren_reasons)
        for k, v in paren_extra.items():
            if k not in extra or extra.get(k) is None:
                extra[k] = v
        grp = seed_group.get(_seed_key(seed))
        mid = _mid(seed)
        members = by_group.get((mid, grp), []) if grp and mid is not None else []
        grp_reasons, grp_extra = _group_outlier(
            seed, members, spatial, spatial_by_key)
        reasons.extend(grp_reasons)
        for k, v in grp_extra.items():
            if k.startswith("suggested_") and extra.get(k) is not None:
                continue
            if k not in extra or extra.get(k) is None:
                extra[k] = v
        if (_listing_only(seed)
                and (seed.get("geocode_source") or "") == "nominatim"
                and spatial.get("kind") in INLAND_KINDS
                and (spatial.get("dist_km") or 0) > INLAND_FAR_KM):
            reasons.append(REASON_NOMINATIM_INLAND)

        reasons = list(dict.fromkeys(reasons))
        if not reasons:
            continue
        plan = _plan_correction(seed, spatial, extra, reasons)
        xy = _coords(seed)
        flags.append({
            "key": _seed_key(seed),
            "name": seed.get("name"),
            "mrgid": seed.get("mrgid"),
            "lat": xy[0] if xy else None,
            "lon": xy[1] if xy else None,
            "reasons": reasons,
            "suggested_lat": extra.get("suggested_lat"),
            "suggested_lon": extra.get("suggested_lon"),
            "suggested_source": extra.get("suggested_source"),
            "severity": _severity(reasons, spatial, seed, extra),
            "spatial_class": spatial.get("kind"),
            "dist_km_to_eez_poly": spatial.get("dist_km"),
            "geocode_source": seed.get("geocode_source"),
            "seed_sources": list(seed.get("seed_sources") or []),
            "listing_group": grp,
            "peer_names": extra.get("peer_names"),
            "peer_dist_km": extra.get("peer_dist_km"),
            "group_dist_km": extra.get("group_dist_km"),
            "will_correct": bool(plan),
            "correction": plan,
        })
    flags.sort(key=lambda r: (
        0 if r.get("will_correct") else 1,
        0 if r["severity"] == "high" else 1,
        -(r.get("dist_km_to_eez_poly") or 0),
        r.get("name") or "",
    ))
    return flags


def audit_confirmed_seeds(
    seeds: list[dict], *,
    geoms: dict[int, object] | None = None,
    listing_ports: list[dict] | None = None,
) -> dict:
    """Comparable report (workshop). No write."""
    flags = flag_confirmed_seeds(
        seeds, geoms=geoms, listing_ports=listing_ports)
    confirmed_n = sum(
        1 for s in seeds
        if (s.get("verify_verdict") or s.get("confirmation_status") or "") == "confirmed"
        and _coords(s)
    )
    return {
        "audited_at": now_iso(),
        "n_confirmed": confirmed_n,
        "n_flagged": len(flags),
        "n_high": sum(1 for s in flags if s["severity"] == "high"),
        "n_medium": sum(1 for s in flags if s["severity"] == "medium"),
        "n_will_correct": sum(1 for s in flags if s.get("will_correct")),
        "inland_far_km": INLAND_FAR_KM,
        "wrote_poe_ports": False,
        "seeds_build": False,
        "flags": flags,
        "suspects": flags,
    }


def _correction_set(seed: dict, plan: dict, spatial_after: dict, stamp: str,
                    reasons: list[str], prev: dict) -> dict:
    kind = spatial_after.get("kind")
    in_eez = kind == "in_eez"
    return {
        "lat": plan["lat"],
        "lon": plan["lon"],
        "geocode_source": plan["geocode_source"],
        "spatial_class": kind,
        "spatial_kind": kind,
        "in_eez": in_eez,
        "validated": bool(spatial_after.get("validated")),
        "dist_km_to_eez_poly": spatial_after.get("dist_km"),
        "distance_km": spatial_after.get("dist_km"),
        "gps_audit_status": "corrected",
        "gps_audit_reasons": reasons,
        "gps_audit_at": stamp,
        "gps_prev_lat": prev.get("lat"),
        "gps_prev_lon": prev.get("lon"),
        "gps_prev_geocode_source": prev.get("geocode_source"),
        "gps_suggested_lat": plan["lat"],
        "gps_suggested_lon": plan["lon"],
    }


def persist_gps_audit(db, report: dict, *,
                      geoms: dict[int, object] | None = None,
                      seeds: list[dict] | None = None) -> dict:
    """Write gps_audit_*; correct obvious cases; log `gps_audit_correct`.

    `db` exposes poe_seed_ports, poe_ports, poe_audit_log (sync).
    """
    geoms = geoms if geoms is not None else load_eez_geoms()
    stamp = report.get("audited_at") or now_iso()
    flags = report.get("flags") or report.get("suspects") or []
    by_key = {f["key"]: f for f in flags if f.get("key")}
    seed_coll = db.poe_seed_ports
    ports_coll = db.poe_ports
    log_coll = db.poe_audit_log
    before_ports = ports_coll.count_documents({})
    before_seeds = seed_coll.count_documents({})
    before_confirmed = seed_coll.count_documents({"verify_verdict": "confirmed"})

    if seeds is None:
        seeds = list(seed_coll.find({"verify_verdict": "confirmed"}))

    flagged = corrected = ok = ports_patched = 0
    corrections: list[dict] = []
    for seed in seeds:
        if (seed.get("verify_verdict") or "") != "confirmed":
            continue
        key = _seed_key(seed)
        if not key:
            continue
        finding = by_key.get(key)
        hit = seed_coll.find_one({"$or": [{"_id": key}, {"dedup_key": key}]})
        if not hit:
            continue
        if finding and finding.get("will_correct") and finding.get("correction"):
            plan = finding["correction"]
            prev = {
                "lat": hit.get("lat"), "lon": hit.get("lon"),
                "geocode_source": hit.get("geocode_source"),
            }
            tmp = dict(hit)
            tmp["lat"] = plan["lat"]
            tmp["lon"] = plan["lon"]
            spatial_after = classify_seed_point(tmp, geoms)
            upd = _correction_set(
                hit, plan, spatial_after, stamp,
                finding.get("reasons") or [], prev)
            # Never touch the verdict.
            seed_coll.update_one({"_id": hit["_id"]}, {"$set": upd})
            log_coll.insert_one({
                "action": "gps_audit_correct",
                "at": stamp,
                "key": key,
                "name": hit.get("name"),
                "reasons": finding.get("reasons") or [],
                "before": prev,
                "after": {
                    "lat": plan["lat"], "lon": plan["lon"],
                    "geocode_source": plan["geocode_source"],
                    "spatial_class": spatial_after.get("kind"),
                    "in_eez": spatial_after.get("kind") == "in_eez",
                    "dist_km_to_eez_poly": spatial_after.get("dist_km"),
                },
            })
            port = ports_coll.find_one({"dedup_key": key})
            if port and _same_gps(port, float(prev["lat"]), float(prev["lon"])):
                ports_coll.update_one({"_id": port["_id"]}, {"$set": {
                    "lat": plan["lat"],
                    "lon": plan["lon"],
                    "geocode_source": plan["geocode_source"],
                    "spatial_kind": spatial_after.get("kind"),
                    "spatial_class": spatial_after.get("kind"),
                    "in_eez": spatial_after.get("kind") == "in_eez",
                    "validated": bool(spatial_after.get("validated")),
                    "distance_km": spatial_after.get("dist_km"),
                    "dist_km_to_eez_poly": spatial_after.get("dist_km"),
                }})
                ports_patched += 1
            corrections.append({
                "key": key, "name": hit.get("name"),
                "before": prev, "after": {
                    "lat": plan["lat"], "lon": plan["lon"],
                    "geocode_source": plan["geocode_source"],
                    "spatial_class": spatial_after.get("kind"),
                },
                "reasons": finding.get("reasons") or [],
            })
            corrected += 1
            continue
        if finding:
            seed_coll.update_one({"_id": hit["_id"]}, {"$set": {
                "gps_audit_at": stamp,
                "gps_audit_status": "flagged",
                "gps_audit_reasons": finding.get("reasons") or [],
                "gps_suggested_lat": finding.get("suggested_lat"),
                "gps_suggested_lon": finding.get("suggested_lon"),
                "gps_suggested_source": finding.get("suggested_source"),
            }})
            flagged += 1
            continue
        if hit.get("gps_audit_status") == "corrected":
            # Rerun: the GPS is no longer aberrant, keep the marker.
            ok += 1
            continue
        seed_coll.update_one({"_id": hit["_id"]}, {"$set": {
            "gps_audit_at": stamp,
            "gps_audit_status": "ok",
            "gps_audit_reasons": [],
        }})
        ok += 1

    after_ports = ports_coll.count_documents({})
    after_seeds = seed_coll.count_documents({})
    after_confirmed = seed_coll.count_documents({"verify_verdict": "confirmed"})
    if after_ports != before_ports:
        raise RuntimeError("poe_ports count a bougé pendant l'audit GPS")
    if after_seeds != before_seeds:
        raise RuntimeError("poe_seed_ports count a bougé pendant l'audit GPS")
    if after_confirmed != before_confirmed:
        raise RuntimeError("confirmed count a bougé pendant l'audit GPS")
    return {
        "flagged": flagged,
        "corrected": corrected,
        "ok": ok,
        "poe_ports": after_ports,
        "poe_seed_ports": after_seeds,
        "n_confirmed": after_confirmed,
        "poe_ports_patched": ports_patched,
        "wrote_poe_ports": ports_patched > 0,
        "seeds_build": False,
        "corrections": corrections,
    }


async def audit_from_db(db) -> dict:
    """Dry-run Atlas (endpoint atelier). Aucun persist."""
    seeds = await db.poe_seed_ports.find(
        {"verify_verdict": "confirmed"}).to_list(20000)
    report = audit_confirmed_seeds(seeds)
    report["persist"] = None
    report["dry_run"] = True
    return report
