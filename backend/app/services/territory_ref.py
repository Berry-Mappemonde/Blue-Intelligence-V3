"""Curated official URLs (territories.json) attached to a VLIZ polygon.

Read-only. Do not import zee_crossings (WFS / shapely) from the card.
The MRGID → code mapping is the same as in zee_crossings.MRGID_TO_TERRITORY.
"""
from __future__ import annotations

import json
from functools import lru_cache
from urllib.parse import urlparse

from app.config import DATA_DIR

# VLIZ v12 → territories.json (checked 2026-06, same table as zee_crossings).
MRGID_TO_TERRITORY: dict[int, str | None] = {
    5677: "france_metropolitaine",
    48966: "france_metropolitaine",
    48976: "france_metropolitaine",
    8440: "polynesie_francaise",
    8312: "nouvelle_caledonie",
    48948: "nouvelle_caledonie",
    33178: "martinique",
    33177: "guadeloupe",
    48952: "saint_barthelemy",
    8495: "saint_martin",
    8462: "guyane",
    8454: "wallis_et_futuna",
    8338: "la_reunion",
    48944: "mayotte",
    8494: "saint_pierre_et_miquelon",
    48946: "taaf",
    48945: "taaf",
    8341: "taaf",
    8339: "taaf",
    8340: "taaf",
    8386: "taaf",
    8385: "taaf",
    8387: "taaf",
    8401: None,
}


@lru_cache(maxsize=1)
def _territories() -> dict[str, dict]:
    path = DATA_DIR / "territories.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    out: dict[str, dict] = {}
    for rec in data.get("territories") or []:
        code = rec.get("code")
        if code:
            out[str(code)] = rec
    return out


def _territory_for_mrgid(mrgid) -> dict:
    try:
        mid = int(mrgid or 0)
    except (TypeError, ValueError):
        return {}
    code = MRGID_TO_TERRITORY.get(mid)
    if not code:
        return {}
    return _territories().get(code) or {}


def curated_landing_urls(mrgid) -> list[dict]:
    """State page of THIS polygon — crawl entry point, not a frozen PDF.

    Linked PDFs change (pleasure-craft list 2025 → 2026). Start from the page
    and follow current attachments. Never port ref_urls
    (they pin a stale file).
    """
    raw = (_territory_for_mrgid(mrgid).get("ref_url") or "").strip()
    if not raw.startswith("http"):
        return []
    return [{"url": raw, "official": True, "from_arm": "landing"}]


def curated_td_urls(mrgid) -> list[dict]:
    """Curated state pages / PDFs for THIS polygon — not the sovereign aggregate."""
    terr = _territory_for_mrgid(mrgid)
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
    terr = _territory_for_mrgid(mrgid)
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
