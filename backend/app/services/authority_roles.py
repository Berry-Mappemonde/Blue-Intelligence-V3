"""Authority roles of fetch candidates (docs/REGLES_PARAMETRES.md, formalities.max_fetch).

backend/data/whitelist_sources.json lists, per audited zone, the authorities a foreign
yacht deals with on arrival (customs, immigration, maritime authority, port authority)
and the whitelist domain each one lives on. The whitelist only says "official"; this
module says *which* authority a candidate URL belongs to, so ``_collect_texts`` can keep
one fetch slot per role instead of spending the whole cap on the best-ranked ministry
(Canaries pilot: 8 fetches on transport and tax pages, no port authority read).

A domain that is a bare government label (``gov.cv``, ``govt.lc``) is shared by every
ministry of the country: the role is then matched on the authority URL's own host
(``mf.gov.cv``), never on the label, so a random ``*.gov.cv`` page is not "customs".
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

SOURCES = Path(__file__).resolve().parents[2] / "data" / "whitelist_sources.json"

# Order in which roles get a reserved slot when the cap cannot hold one per role,
# and the order of a second, third... round. The port authority and the maritime
# authority publish the list of ports; customs and immigration publish the procedure.
ROLE_PRIORITY = ("port_authority", "maritime_authority", "customs", "immigration")

_GOV_LABELS = {"gov", "gouv", "gob", "go", "govt", "gub", "gv"}


@lru_cache(maxsize=1)
def _zones() -> dict:
    try:
        return (json.loads(SOURCES.read_text(encoding="utf-8")) or {}).get("zones") or {}
    except (OSError, ValueError):
        return {}


def reload_sources() -> None:
    _zones.cache_clear()


def _host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def _match_key(auth: dict) -> str:
    """Domain a candidate must sit on (host or subdomain) to hold this role."""
    dom = (auth.get("domain") or "").lower().lstrip(".")
    labels = dom.split(".")
    if not dom or (len(labels) <= 2 and labels[0] in _GOV_LABELS):
        return _host(auth.get("url") or "") or dom
    return dom


def _zone_record(zone: dict | None) -> dict:
    """The zone's audited record: by mrgid, else by whitelist_key == ISO2; {} when none."""
    zone = zone or {}
    zones = _zones()
    rec = None
    try:
        mid = str(int(zone.get("mrgid") or 0))
    except (TypeError, ValueError):
        mid = ""
    if mid and mid != "0":
        rec = zones.get(mid)
    if rec is None:
        cc = (zone.get("iso2") or "").upper()
        if cc:
            rec = next((z for z in zones.values()
                        if (z.get("whitelist_key") or "") == cc), None)
    return rec or {}


def zone_gazettes(zone: dict | None) -> list[str]:
    """The zone's official gazettes (``gazettes`` of whitelist_sources.json), as hosts."""
    keys = [_match_key(g) for g in _zone_record(zone).get("gazettes") or ()]
    return [k for k in keys if k]


def list_publishers(zone: dict | None) -> list[str]:
    """Hosts that publish the designating lists of a zone outside Schengen: its official
    gazettes (``gazettes``), then its authorities, all from whitelist_sources.json. A
    bare government label stays on the authority's own host, as for a role: a random
    ``*.gov.cv`` page is no publisher. An unaudited zone has none."""
    keys = [_match_key(a) for a in _zone_record(zone).get("authorities") or ()]
    return list(dict.fromkeys([*zone_gazettes(zone), *(k for k in keys if k)]))


def zone_authorities(zone: dict | None) -> list[dict]:
    """[{role, key}] for the zone: by mrgid, else by whitelist_key == ISO2."""
    out = []
    for a in _zone_record(zone).get("authorities") or []:
        role, key = a.get("role"), _match_key(a)
        if role and key:
            out.append({"role": role, "key": key, "url": a.get("url") or "",
                        "name": a.get("name") or ""})
    return out


def audited_urls(authorities: list[dict], role: str) -> list[str]:
    """The audited official URLs of one role, in whitelist_sources.json order."""
    out: list[str] = []
    for a in authorities:
        u = a.get("url") or ""
        if a.get("role") == role and u and u not in out:
            out.append(u)
    return out


def missing_roles(candidates: list[dict], authorities: list[dict]) -> list[str]:
    """Roles of the zone (ROLE_PRIORITY order) that no candidate URL holds."""
    held = {r for c in candidates or [] for r in roles_of(c.get("url") or "", authorities)}
    roles = list(dict.fromkeys(a["role"] for a in authorities))
    order = [r for r in ROLE_PRIORITY if r in roles] + sorted(r for r in roles if r not in ROLE_PRIORITY)
    return [r for r in order if r not in held]


def roles_of(url: str, authorities: list[dict]) -> set[str]:
    h = _host(url)
    if not h:
        return set()
    return {a["role"] for a in authorities
            if h == a["key"] or h.endswith("." + a["key"])}


def role_quotas(candidates: list[dict], authorities: list[dict], cap: int,
                per_role: int = 1) -> dict[str, int]:
    """Slots reserved per role: ``per_role`` for each role that has a candidate,
    handed out round-robin in ROLE_PRIORITY order, never more than ``cap`` in all
    nor more than the role has distinct candidate URLs."""
    available: dict[str, int] = {}
    seen: set[str] = set()
    for c in candidates or []:
        u = c.get("url") or ""
        if not u or u in seen:
            continue
        seen.add(u)
        for r in roles_of(u, authorities):
            available[r] = available.get(r, 0) + 1
    order = [r for r in ROLE_PRIORITY if r in available]
    order += sorted(r for r in available if r not in ROLE_PRIORITY)
    quotas = {r: 0 for r in order}
    left = max(0, int(cap))
    for _round in range(max(0, int(per_role))):
        for r in order:
            if left and quotas[r] < available[r]:
                quotas[r] += 1
                left -= 1
    return {r: n for r, n in quotas.items() if n}
