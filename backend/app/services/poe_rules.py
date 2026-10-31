"""Ports-of-entry business rules (Formalities).

Three laws, one module. Confidence, Gold and the listing gap call these
functions; they do not re-implement them. Nothing here reads the environment:
the laws stay in force when no extra settings are present.

1. United Kingdom (ISO2 ``GB``, VLIZ ``mrgid`` 5696). Every port is a port
   of entry. Clearance is the official sPCR app. There is no designated-port
   list, and that absence is not missing data. The joint regime with
   Denmark (Faroe, ``mrgid`` 48967) is not this regime. Overseas territories
   (another ISO2, even under UK sovereignty) are not this regime.
2. Golden rule. Publish a place only when a State source says a foreign
   sailing yacht can make entry there. A community listing, OSM or the
   World Port Index does not.
3. Noonsite is a signal only. A ``noonsite.com`` URL never makes an item
   Gold or confirmed. A URL seen by both arms (top-down and bottom-up) is
   Gold, unless it is Noonsite.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

NOONSITE_HOST = "noonsite.com"
UK_ISO2 = "GB"
UK_APP_MRGID = 5696
# Joint regime United Kingdom / Denmark (Faroe). Not the sPCR regime.
UK_JOINT_MRGIDS = frozenset({48967})
ENTRY_OFFICIAL_APP = "official_app"
ENTRY_DESIGNATED_PORT = "designated_port"

_YACHT_TEXT = re.compile(
    r"pleasure\s*craft|sailing\s+yachts?|foreign\s+yachts?|"
    r"\byachts?\b|plaisance|recreational|\brecreo\b|"
    r"tur[ií]stic|small\s+craft|navires?\s+de\s+plaisance",
    re.I,
)
_CARGO_TEXT = re.compile(
    r"cargo[- ]only|container terminal|tanker terminal|"
    r"commerce uniquement|sin actividad",
    re.I,
)
_YACHT_KINDS = frozenset({
    "pleasure", "mixed", "yacht", "recreational", "plaisance", "pleasure_craft",
})


def _host(url: str) -> str:
    try:
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def is_noonsite_url(url: str | None) -> bool:
    """True for noonsite.com and its subdomains. Never a State source."""
    host = _host(url or "")
    return bool(host) and (host == NOONSITE_HOST or host.endswith("." + NOONSITE_HOST))


def url_key(url: str) -> str:
    """Same page: ignore scheme and a leading www. Distinct paths stay distinct."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parsed.path or "").rstrip("/").lower()
    query = (parsed.query or "").lower()
    if not host and not path:
        return ""
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def _as_url(item) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        raw = item.get("url")
        if isinstance(raw, str):
            return raw.strip()
    return ""


def http_urls(place: dict | None) -> list[str]:
    """HTTP(S) URLs cited on a port or a zone sheet."""
    place = place or {}
    found: list[str] = []
    for key in ("source_urls", "judge_sources", "sources_td", "sources_bu"):
        for item in place.get(key) or []:
            url = _as_url(item)
            if url.startswith("http"):
                found.append(url)
    return found


def urls_are_noonsite_only(urls: list[str] | None) -> bool:
    cleaned = [u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()]
    return bool(cleaned) and all(is_noonsite_url(u) for u in cleaned)


def is_state_url(url: str | None) -> bool:
    """Government host, same test as the evaluator. Noonsite never qualifies."""
    if not url or is_noonsite_url(url):
        return False
    from app.agents.evaluator import is_official_source
    return is_official_source([url])


def _pol(zone: dict) -> str:
    return str(zone.get("pol_type") or "").lower()


def _is_joint_or_overlap(zone: dict) -> bool:
    pol = _pol(zone)
    geo = str(zone.get("geoname") or "").lower()
    return (
        "joint" in pol or "overlapping" in pol
        or "joint regime" in geo or "overlapping claim" in geo
    )


def _mrgid(zone: dict) -> int:
    try:
        return int(zone.get("mrgid") or 0)
    except (TypeError, ValueError):
        return 0


def is_united_kingdom(zone: dict | None) -> bool:
    """Metropolitan United Kingdom. Not an overseas territory, not a port name."""
    if not isinstance(zone, dict):
        return False
    iso = str(zone.get("iso2") or zone.get("country_iso2") or "").strip().upper()
    if iso == UK_ISO2:
        return True
    if iso:
        return False
    if _mrgid(zone) == UK_APP_MRGID:
        return True
    country = str(zone.get("country") or "").strip().casefold()
    if country in {"united kingdom", "great britain"}:
        return True
    # Zone sheet (API qualify omits iso2). A port merely named "United Kingdom"
    # has neither geoname, pol_type nor anchor.
    if not (zone.get("geoname") or zone.get("pol_type") or zone.get("anchor")):
        return False
    name = str(zone.get("name") or "").strip().casefold()
    if name in {"united kingdom", "great britain"}:
        return True
    geoname = str(zone.get("geoname") or "").strip().casefold()
    return geoname == "british exclusive economic zone"


def is_uk_app_entry(zone: dict | None) -> bool:
    """UK sPCR regime: any port, official app, no designated list."""
    if not isinstance(zone, dict):
        return False
    mid = _mrgid(zone)
    if mid in UK_JOINT_MRGIDS or _is_joint_or_overlap(zone):
        return False
    if mid == UK_APP_MRGID:
        return True
    return is_united_kingdom(zone)


def entry_mode(zone: dict | None) -> str:
    if is_uk_app_entry(zone):
        return ENTRY_OFFICIAL_APP
    return ENTRY_DESIGNATED_PORT


def every_port_is_entry(zone: dict | None) -> bool:
    """True when the State rule makes every port of this zone a port of entry."""
    return is_uk_app_entry(zone)


def uk_app_entry_qualification() -> dict:
    """Legal label for a UK sheet with no extracted designated list.

    ``data_gap`` is false: the missing list is the regime, not missing data.
    """
    return {
        "code": "app_entry",
        "basis": (
            "Border Force sPCR: every United Kingdom port is a port of entry; "
            "clearance is via the official app; there is no designated port"
        ),
        "data_gap": False,
        "entry_mode": ENTRY_OFFICIAL_APP,
    }


def counts_as_data_gap(zone: dict | None, *, missing_designated_list: bool = False) -> bool:
    """A missing designated-port list is a data gap, except under the UK app regime."""
    if is_uk_app_entry(zone):
        return False
    return bool(missing_designated_list)


def _kind(place: dict) -> str:
    raw = place.get("judge_kind")
    if raw is None or raw == "":
        raw = place.get("kind")
    return str(raw or "").strip().lower().replace("-", " ").replace(" ", "_")


def _evidence_text(place: dict) -> str:
    parts = []
    for key in ("note", "judge_reason", "activity", "kind", "judge_kind"):
        val = place.get(key)
        if val:
            parts.append(str(val))
    return "\n".join(parts)


def cargo_only(place: dict | None) -> bool:
    place = place or {}
    kind = _kind(place)
    if kind == "cargo":
        return True
    if kind in _YACHT_KINDS:
        return False
    return bool(_CARGO_TEXT.search(_evidence_text(place)))


def foreign_yacht_entry_attested(place: dict | None) -> bool:
    """The text says a foreign sailing yacht (or mixed pleasure traffic) can enter."""
    place = place or {}
    if _kind(place) in _YACHT_KINDS:
        return True
    return bool(_YACHT_TEXT.search(_evidence_text(place)))


def publish_decision(place: dict | None, *, official_source: bool = False) -> dict:
    """Golden rule. UK: the State source is the sPCR regime, not a per-port decree.

    A Noonsite URL is ignored. It never supplies the State source. For the
    United Kingdom the regime itself is that source, so a Noonsite citation
    beside a UK port does not create the right to publish — and does not
    remove the regime either.
    """
    place = place or {}
    urls = http_urls(place)
    noonsite_only = urls_are_noonsite_only(urls)
    if is_uk_app_entry(place):
        return {
            "publish": True,
            "basis": "uk_official_app",
            "noonsite_ignored": noonsite_only,
        }
    if noonsite_only:
        return {"publish": False, "basis": "noonsite_not_a_source", "noonsite_ignored": True}
    state = bool(official_source) or any(is_state_url(u) for u in urls)
    if not state:
        return {"publish": False, "basis": "no_state_source", "noonsite_ignored": False}
    if cargo_only(place):
        return {"publish": False, "basis": "not_foreign_yacht", "noonsite_ignored": False}
    if foreign_yacht_entry_attested(place):
        return {"publish": True, "basis": "state_foreign_yacht", "noonsite_ignored": False}
    return {
        "publish": False,
        "basis": "no_foreign_yacht_attestation",
        "noonsite_ignored": False,
    }


def may_publish(place: dict | None, *, official_source: bool = False) -> bool:
    """Publish only where a State source allows entry of a foreign sailing yacht."""
    return bool(publish_decision(place, official_source=official_source)["publish"])


def source_is_gold(url: str | None, *,
                   seen_by_top_down: bool = False,
                   seen_by_bottom_up: bool = False) -> bool:
    """A URL seen by both arms is Gold. A Noonsite URL never is."""
    if not url or is_noonsite_url(url):
        return False
    return bool(seen_by_top_down and seen_by_bottom_up)


def _arm_keys(items, *, reject_noonsite: bool = True) -> set[str]:
    keys: set[str] = set()
    for item in items or []:
        url = _as_url(item)
        if not url:
            continue
        if reject_noonsite and is_noonsite_url(url):
            continue
        key = url_key(url)
        if key:
            keys.add(key)
    return keys


def both_arms_are_gold(place: dict | None) -> bool:
    """True when a non-Noonsite URL is on both the top-down and bottom-up arms."""
    place = place or {}
    td = _arm_keys(place.get("sources_td"))
    bu = _arm_keys(place.get("sources_bu"))
    if td & bu:
        return True
    for item in list(place.get("sources_td") or []) + list(place.get("sources_bu") or []):
        if not isinstance(item, dict):
            continue
        url = _as_url(item)
        if not url or is_noonsite_url(url):
            continue
        if item.get("gold") is True or item.get("from_arm") == "both":
            return True
    return False


def noonsite_blocks_confirmation(place: dict | None) -> bool:
    """Noonsite cannot make the item Gold or confirmed.

    Blocks only when every cited URL is Noonsite. No URL at all does not
    block: callers keep their previous rule. A both-arms State URL does
    not block.
    """
    place = place or {}
    if both_arms_are_gold(place):
        return False
    return urls_are_noonsite_only(http_urls(place))
