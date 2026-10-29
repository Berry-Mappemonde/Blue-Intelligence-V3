"""listing_ref — Project the community listing (all_countries.json) into
a PoE run space: VLIZ mrgid + iso2 + name + role (poe | other).

Touch neither poe_ports nor poe_run_ports. The listing is not an
extraction source: it is a control reference.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from app.config import DATA_DIR
from app.core.dedup import normalize_name, text_similarity

LISTING_DIR = DATA_DIR / "listing_control"
LISTING_FILE = LISTING_DIR / "all_countries.json"
OVERRIDES_FILE = LISTING_DIR / "slug_overrides.json"
EEZ_INDEX_FILE = LISTING_DIR / "eez_index.json"

AUTO_THRESHOLD = 0.86
_SLUG_TAIL = re.compile(r"-\d+$")

# Listing group / name → VLIZ mrgid (the first override is not always the right one).
_GROUP_MRGID = (
    ("usa", "alaska", 8463),
    ("usa", "hawaii", 8453),
    ("kiribati", "line", 8441),
    ("kiribati", "phoenix", 8450),
    ("kiribati", "gilbert", 8488),
    ("norway", "svalbard", 33181),
    ("india", "andaman", 8333),
    ("india", "nicobar", 8333),
)
_NAME_MRGID = (
    ("usa", ("ketchikan", "unalaska", "dutchharbor", "juneau", "anchorage"), 8463),
    ("kiribati", ("kiritimati", "christmasisland"), 8441),
    ("india", ("andaman", "portblair", "nicobar"), 8333),
    ("norway", ("longyearbyen",), 33181),
)

# Listing slugs → label comparable to eez_index.name
_ALIASES = {
    "france-2": "france",
    "georgia-3": "georgia",
    "cape-verdes": "cape verde",
    "ivory-coast-cote-divoire": "ivory coast",
}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_listing_doc(path: str | None = None) -> dict:
    return _load_json(Path(path) if path else LISTING_FILE)


@lru_cache(maxsize=1)
def load_overrides(path: str | None = None) -> dict[str, list[int]]:
    raw = _load_json(Path(path) if path else OVERRIDES_FILE)
    out: dict[str, list[int]] = {}
    for slug, val in raw.items():
        if slug.startswith("_"):
            continue
        if isinstance(val, dict):
            val = val.get("mrgids") or []
        out[slug] = [int(x) for x in val]
    return out


@lru_cache(maxsize=1)
def load_eez_index(path: str | None = None) -> list[dict]:
    raw = _load_json(Path(path) if path else EEZ_INDEX_FILE)
    return list(raw.get("zones") or raw)


def listing_ref_id(listing: dict | None = None) -> str:
    doc = listing or load_listing_doc()
    return f"listing:{doc.get('generated_at') or 'unknown'}:{doc.get('stats', {}).get('ports_of_entry', 0)}"


def _queries(slug: str, name: str) -> list[str]:
    base = _SLUG_TAIL.sub("", slug)
    alias = _ALIASES.get(slug, "")
    out = [name, slug.replace("-", " "), base.replace("-", " "), alias]
    return [q for q in out if q]


def _best_score(queries: list[str], zone: dict) -> float:
    labels = [zone.get("name") or "", zone.get("geoname") or ""]
    score = 0.0
    for q in queries:
        for lab in labels:
            if not lab:
                continue
            score = max(score, text_similarity(q, lab))
            if normalize_name(q) and normalize_name(q) == normalize_name(lab):
                score = 1.0
    return score


def _prefer_sovereign(zones: list[dict]) -> list[dict]:
    """Drop joint / claim regimes if a 200NM exists."""
    nm = [z for z in zones if (z.get("pol_type") or "") == "200NM"]
    return nm or zones


def resolve_slug(slug: str, name: str, zones: list[dict] | None = None,
                 overrides: dict[str, list[int]] | None = None) -> dict:
    """Retourne {slug, name, mrgids, iso2, zone_names, method}."""
    zones = zones if zones is not None else load_eez_index()
    overrides = overrides if overrides is not None else load_overrides()
    by_mrgid = {int(z["mrgid"]): z for z in zones}

    if slug in overrides:
        picked = [by_mrgid[m] for m in overrides[slug] if m in by_mrgid]
        return _resolution(slug, name, picked, "override")

    queries = _queries(slug, name)
    scored: list[tuple[float, dict]] = []
    for z in zones:
        s = _best_score(queries, z)
        if s >= AUTO_THRESHOLD:
            scored.append((s, z))
    if not scored:
        return _resolution(slug, name, [], "unresolved")
    top = max(s for s, _ in scored)
    kept = [z for s, z in scored if s >= top - 0.02]
    exact = [z for z in kept if normalize_name(z.get("name") or "") in
             {normalize_name(name), normalize_name(_ALIASES.get(slug, slug.replace("-", " ")))}]
    if exact:
        kept = exact
    kept = _prefer_sovereign(kept)
    return _resolution(slug, name, kept, "auto")


def _resolution(slug: str, name: str, zones: list[dict], method: str) -> dict:
    iso = next((z.get("iso2") for z in zones if z.get("iso2")), None)
    return {
        "slug": slug,
        "name": name,
        "mrgids": [int(z["mrgid"]) for z in zones],
        "iso2": iso,
        "zone_names": [z.get("name") for z in zones],
        "method": method,
    }


def pick_listing_mrgid(res: dict, port: dict | str | None,
                       zones: list[dict] | None = None) -> int:
    """Pick the VLIZ polygon of a listing port (group / name, not the first override)."""
    mrgids = [int(x) for x in (res.get("mrgids") or [])]
    slug = (res.get("slug") or "").lower()
    if isinstance(port, dict):
        group = normalize_name(port.get("group") or "")
        name_n = normalize_name(port.get("name") or "")
    else:
        group, name_n = "", normalize_name(port or "")
    hinted = None
    for s, needle, mid in _GROUP_MRGID:
        if slug == s and needle in group:
            hinted = mid
            break
    if hinted is None:
        for s, needles, mid in _NAME_MRGID:
            if slug == s and any(n in name_n for n in needles):
                hinted = mid
                break
    if hinted:
        return hinted
    return mrgids[0] if mrgids else 0


def _emit_port(country: dict, res: dict, port: dict, role: str,
               zones: list[dict] | None = None) -> dict:
    name = (port.get("name") if isinstance(port, dict) else port) or ""
    group = port.get("group") if isinstance(port, dict) else None
    by_mrgid = {int(z["mrgid"]): z for z in (zones or [])}
    primary = pick_listing_mrgid(res, port, zones)
    mrgids = list(res["mrgids"])
    if primary and primary not in mrgids:
        mrgids = [primary, *mrgids]
    z = by_mrgid.get(primary) or {}
    return {
        "mrgid": primary,
        "mrgids": mrgids,
        "zone_name": z.get("name") or (res["zone_names"] or [country.get("name")])[0],
        "country_iso2": z.get("iso2") or res.get("iso2"),
        "name": name,
        "slug": country.get("slug"),
        "country": country.get("name"),
        "group": group,
        "role": role,
        "dedup_key": f"{primary}:{normalize_name(name)}",
    }


def project_listing(listing: dict | None = None, zones: list[dict] | None = None,
                    overrides: dict[str, list[int]] | None = None) -> dict:
    """Build listing_ref: ports in run format + slug resolution."""
    listing = listing if listing is not None else load_listing_doc()
    zones = zones if zones is not None else load_eez_index()
    overrides = overrides if overrides is not None else load_overrides()

    ports: list[dict] = []
    resolutions: list[dict] = []
    unresolved: list[dict] = []
    for country in listing.get("countries") or []:
        res = resolve_slug(country.get("slug") or "", country.get("name") or "",
                           zones=zones, overrides=overrides)
        resolutions.append(res)
        if not res["mrgids"]:
            unresolved.append({"slug": res["slug"], "name": res["name"],
                               "poe": len(country.get("ports_of_entry") or []),
                               "other": len(country.get("other_ports") or [])})
            continue
        for p in country.get("ports_of_entry") or []:
            ports.append(_emit_port(country, res, p, "poe", zones=zones))
        for p in country.get("other_ports") or []:
            ports.append(_emit_port(country, res, p, "other", zones=zones))

    poe_n = sum(1 for p in ports if p["role"] == "poe")
    other_n = sum(1 for p in ports if p["role"] == "other")
    return {
        "listing_ref_id": listing_ref_id(listing),
        "generated_at": listing.get("generated_at"),
        "disclaimer": listing.get("disclaimer"),
        "stats": {
            "countries": len(listing.get("countries") or []),
            "resolved": sum(1 for r in resolutions if r["mrgids"]),
            "unresolved": len(unresolved),
            "ports_of_entry": poe_n,
            "other_ports": other_n,
        },
        "unresolved": unresolved,
        "resolutions": resolutions,
        "ports": ports,
    }


def listing_ports_for_mrgid(ports: list[dict], mrgid: int) -> list[dict]:
    """Listing ports applicable to an EEZ (slug → one or more mrgid)."""
    mrgid = int(mrgid)
    out = []
    for p in ports:
        ids = p.get("mrgids") or ([p["mrgid"]] if p.get("mrgid") else [])
        if mrgid in {int(x) for x in ids}:
            # copy with the compared zone's mrgid (same space as a run)
            out.append({**p, "mrgid": mrgid,
                        "dedup_key": f"{mrgid}:{normalize_name(p.get('name'))}"})
    return out
