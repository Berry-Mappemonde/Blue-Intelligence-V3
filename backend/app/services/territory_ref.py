"""Curated official URLs (territories.json) attached to a VLIZ polygon.

Read-only. Do not import zee_crossings (WFS / shapely) from the card.
The MRGID → code mapping comes from each record's "mrgids" (any sovereign); for France it
agrees with zee_crossings.MRGID_TO_TERRITORY. The PoE whitelist of a mapped polygon is built
from its record (poe_pipeline.build_whitelist, docs/TERRITORY-WHITELIST.md).
"""
from __future__ import annotations

import json
from functools import lru_cache
from urllib.parse import urlparse

from app.config import DATA_DIR


@lru_cache(maxsize=1)
def _table() -> dict:
    path = DATA_DIR / "territories.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def never_attach_to_sovereign() -> dict[str, str]:
    """Self-governing polygons (ISO2 → reason) that are never attached to a sovereign:
    Sint Maarten, Aruba, Curaçao, Cook Islands, Niue, Greenland, Faroes."""
    raw = _table().get("never_attach_to_sovereign") or {}
    return {str(k).upper(): str(v) for k, v in raw.items()}


@lru_cache(maxsize=1)
def _territories() -> dict[str, dict]:
    """Curated records by code. A record for a never-attach polygon is dropped."""
    banned = never_attach_to_sovereign()
    out: dict[str, dict] = {}
    for rec in _table().get("territories") or []:
        code = rec.get("code")
        if not code or str(rec.get("iso2") or "").upper() in banned:
            continue
        out[str(code)] = rec
    return out


def _build_mrgid_map() -> dict[int, str]:
    out: dict[int, str] = {}
    for code, rec in _territories().items():
        for mid in rec.get("mrgids") or []:
            try:
                out.setdefault(int(mid), code)
            except (TypeError, ValueError):
                continue
    return out


# VLIZ v12 mrgid → territory code, read from territories.json ("mrgids" of each record).
# Any sovereign: France's 23 polygons, the Canary Islands → ES, … Unlisted mrgid → no record.
MRGID_TO_TERRITORY: dict[int, str] = _build_mrgid_map()


def territory_for_mrgid(mrgid) -> dict:
    """Curated record of the territory this VLIZ polygon belongs to, or {} when unmapped."""
    try:
        mid = int(mrgid or 0)
    except (TypeError, ValueError):
        return {}
    code = MRGID_TO_TERRITORY.get(mid)
    if not code:
        return {}
    return _territories().get(code) or {}


def sovereign_runs_entry(terr: dict | None) -> bool:
    """True only when the record says the sovereign's national services run entry
    (default false). Never for a never-attach polygon."""
    terr = terr or {}
    if str(terr.get("iso2") or "").upper() in never_attach_to_sovereign():
        return False
    return terr.get("sovereign_runs_entry") is True


def validate_territories() -> list[str]:
    """Schema problems of territories.json (empty list = valid)."""
    errors: list[str] = []
    banned = never_attach_to_sovereign()
    seen: dict[int, str] = {}
    for rec in _table().get("territories") or []:
        code = rec.get("code") or "?"
        iso2 = rec.get("iso2")
        if iso2 is not None and (not isinstance(iso2, str) or len(iso2) != 2):
            errors.append(f"{code}: iso2 must be a 2-letter code or null")
        if str(iso2 or "").upper() in banned:
            errors.append(f"{code}: {iso2} is self-governing and is never attached to a sovereign")
        sov = rec.get("sovereign_iso2")
        if not isinstance(sov, str) or len(sov) != 2:
            errors.append(f"{code}: sovereign_iso2 missing")
        mrgids = rec.get("mrgids")
        if not isinstance(mrgids, list) or not mrgids or not all(isinstance(m, int) for m in mrgids):
            errors.append(f"{code}: mrgids must be a non-empty list of ints")
            mrgids = []
        for m in mrgids:
            if m in seen:
                errors.append(f"{code}: mrgid {m} already mapped to {seen[m]}")
            seen[m] = code
        if not isinstance(rec.get("sovereign_runs_entry", False), bool):
            errors.append(f"{code}: sovereign_runs_entry must be a bool")
        if rec.get("sovereign_runs_entry") and not str(rec.get("sovereign_runs_entry_evidence") or "").strip():
            errors.append(f"{code}: sovereign_runs_entry=true needs sovereign_runs_entry_evidence")
        doms = rec.get("official_domains")
        if not isinstance(doms, list) or not doms:
            errors.append(f"{code}: official_domains missing")
        if not str(rec.get("ref_url") or "").startswith("http"):
            errors.append(f"{code}: ref_url missing")
    return errors


def curated_landing_urls(mrgid) -> list[dict]:
    """State page of THIS polygon — crawl entry point, not a frozen PDF.

    Linked PDFs change (pleasure-craft list 2025 → 2026). Start from the page
    and follow current attachments. Never port ref_urls
    (they pin a stale file).
    """
    raw = (territory_for_mrgid(mrgid).get("ref_url") or "").strip()
    if not raw.startswith("http"):
        return []
    return [{"url": raw, "official": True, "from_arm": "landing"}]


def curated_td_urls(mrgid) -> list[dict]:
    """Curated state pages / PDFs for THIS polygon — not the sovereign aggregate."""
    terr = territory_for_mrgid(mrgid)
    if not terr:
        return []
    urls: list[str] = []
    for port in terr.get("ports_of_entry") or []:
        if isinstance(port, dict) and port.get("ref_url"):
            urls.append(str(port["ref_url"]))
    if terr.get("ref_url"):
        urls.append(str(terr["ref_url"]))
    seen: set[str] = set()
    out: list[dict] = []
    for raw in urls:
        u = raw.strip()
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        out.append({"url": u, "official": True, "from_arm": "td"})
    return out


def official_domains_for_mrgid(mrgid) -> tuple[str, ...]:
    terr = territory_for_mrgid(mrgid)
    return tuple(
        str(d).lower().lstrip(".")
        for d in (terr.get("official_domains") or [])
        if d
    )


def _host_matches_domain(host: str, domain: str) -> bool:
    host = (host or "").lower().lstrip(".")
    domain = (domain or "").lower().lstrip(".")
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def td_url_fits_polygon(url: str, zone: dict | None) -> bool:
    """True si la page peut appartenir à CE polygone.

    Quand le polygone n'est pas le souverain (NC ≠ FR), on n'accepte que
    les domaines curés / le ccTLD local. Légifrance et mer.gouv.fr restent
    sur la France hexagone, pas sur Nouméa.
    Sans référentiel (Saba…) : on ne filtre pas.
    """
    zone = zone or {}
    iso2 = str(zone.get("iso2") or "").strip().upper()
    sov = str(zone.get("sov_iso2") or "").strip().upper()
    official = official_domains_for_mrgid(zone.get("mrgid"))
    if not official or not iso2 or not sov or iso2 == sov:
        return True
    host = (urlparse(url or "").hostname or "").lower().lstrip(".")
    if not host:
        return False
    if any(_host_matches_domain(host, d) for d in official):
        return True
    cc = iso2.lower()
    return host == cc or host.endswith("." + cc)
