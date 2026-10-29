"""Registry check: tier candidate ports with official-source and independent-registry evidence.

Two kinds of evidence, kept separate:

- Official source: an official URL (``is_official_source()``) whose text names the port
  (normalised, with alternates) within PROXIMITY_CHARS of a strong entry term (port of
  entry, clear customs, designated marina, ... in EN/FR/ES/PT/IT/GR), or of a weak one
  (customs, immigration, ...) with a port word nearby and the full name, not in an address. The text is either
  fetched now or a stored snippet from an earlier fetch (app/adapters/evidence_store.py);
  never model output. No text, no evidence: a URL alone proves nothing.
- Registry: a UN/LOCODE port (UNECE) or World Port Index entry (NGA Pub 150) in the same
  country whose name matches, within MAX_DISTANCE_KM when both sides have coordinates. A
  name + country match further away is recorded as ``coord_mismatch`` (with the distance),
  not as a match. Blue Intelligence's own seed GeoJSON is never the reference.

Tiers (``registry.tier``):

- tier1 "Official Commercial Port of Entry": official source + registry match
- tier2 "Official Yachting Port of Entry": official source, no registry match
- tier_registered "Registered port, entry status unverified": registry match only
- tier3 "Unconfirmed - review needed": neither

A registry listing alone never makes a port of entry: UN/LOCODE and the WPI list ports,
not clearance status. WPI "First Port of Entry" is recorded as extra evidence only.
``zetaris_validation`` stays backward compatible: "official" for tiers 1-2 only. Each check
is traced as a ZetarisAdapter ``registry_check`` event with the tier and why. See
app/adapters/registry_sources.py for the sources and licences.
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from urllib.parse import urlparse

from app.adapters.zetaris import ZetarisClient, ZetarisUnavailable
from app.agents.evaluator import is_official_source

LOG = logging.getLogger(__name__)
MAX_DISTANCE_KM = 25.0
OFFICIAL = "official"
UNCONFIRMED = "unconfirmed - review needed"
REGISTERED = "registered - entry status unverified"
TIER1, TIER2, TIER_REGISTERED, TIER3 = "tier1", "tier2", "tier_registered", "tier3"
TIER_LABELS = {
    TIER1: "Official Commercial Port of Entry",
    TIER2: "Official Yachting Port of Entry",
    TIER_REGISTERED: "Registered port, entry status unverified",
    TIER3: "Unconfirmed - review needed",
}
VALIDATION = {TIER1: OFFICIAL, TIER2: OFFICIAL, TIER_REGISTERED: REGISTERED, TIER3: UNCONFIRMED}
_SOURCE_NAMES = {"unlocode": "UN/LOCODE", "wpi": "WPI"}
_SOURCE_BLOCK = re.compile(r"^\[SOURCE: (\S+)\]\n", re.MULTILINE)
SOURCES = ("unlocode", "wpi")
REGISTRY_LIMIT = 10000

_APOSTROPHES = re.compile(r"[\'’‘`´]")
_POSSESSIVE = re.compile(r"[\'’‘`´]s\b", re.IGNORECASE)
# Leading words that name the facility, not the place ("Port of X", "Porto de X", "Luka X").
_PREFIXES = (
    "port autonome de", "port autonome d", "pkk porti i", "porti i", "port of", "port de", "port d",
    "porto de", "porto do", "porto da", "puerto de", "puerto del", "harbour of", "harbor of",
    "marina de", "marina di", "marina", "luka", "porto", "puerto", "port", "the",
)
# Trailing facility words ("Bridgetown Port", "Anchorage Small Boat Harbor").
_SUFFIXES = (
    "small boat harbour", "small boat harbor", "deep water harbour", "deep water harbor",
    "harbour", "harbor", "port", "marina", "jetty", "terminal",
)
_TOKENS = {"st": "saint", "ste": "sainte", "mt": "mount", "ft": "fort", "pt": "point"}
# A facility word on its own ("Porto" from "Porto / Marina de X") never names a port.
_GENERIC = set(_PREFIXES) | set(_SUFFIXES) | {"harbour", "harbor", "bay", "quay", "pier", "dock"}

# Official evidence: the port's name must sit within PROXIMITY_CHARS of an entry term.
# A strong term designates on its own; a weak term (a department or office, or "point of
# entry", which also covers airports and land borders) counts only
# with a port word in the same window and a match on the full name, not on the place
# left after stripping "Port of" (so "the Hamilton ... Long Room ... Customs" does not count).
PROXIMITY_CHARS = 200
SNIPPET_CHARS = 400
STRONG_ENTRY_TERMS = (
    # English
    "port of entry", "ports of entry", "entry port", "entry ports", "clearing customs",
    "clear customs", "clear in", "clearance port", "clearance ports", "designated port",
    "designated ports", "designated marina", "designated marinas", "port of call for clearing",
    # French
    "port d'entrée", "ports d'entrée", "dédouanement", "dédouaner", "port désigné", "ports désignés",
    # Spanish
    "puerto de entrada", "puertos de entrada", "puerto habilitado", "puertos habilitados",
    "despacho aduanero", "puerto designado", "paso internacional", "pasos internacionales",
    # Portuguese
    "porto de entrada", "portos de entrada", "desembaraço aduaneiro", "porto designado",
    "portos designados", "posto de fronteira", "postos de fronteira",
    # Italian
    "porto di entrata", "porti di entrata", "porto di ingresso", "sdoganamento", "porto designato",
    # Greek
    "λιμένας εισόδου", "λιμάνι εισόδου", "πύλη εισόδου", "εκτελωνισμός",
)
WEAK_ENTRY_TERMS = (
    "customs", "immigration", "quarantine", "harbour master", "harbor master", "harbourmaster",
    "harbormaster", "point of entry", "points of entry",
    "douane", "douanes", "quarantaine", "capitainerie", "point d'entrée", "points d'entrée",
    "aduana", "aduanas", "migraciones", "inmigración", "cuarentena", "capitanía de puerto",
    "punto de entrada", "puntos de entrada",
    "alfândega", "alfândegas", "imigração", "quarentena", "capitania", "ponto de entrada",
    "pontos de entrada",
    "dogana", "dogane", "immigrazione", "quarantena", "capitaneria di porto", "punto di ingresso",
    "punti di ingresso", "punto di entrata",
    "τελωνείο", "τελωνεία", "λιμεναρχείο", "καραντίνα", "σημείο εισόδου", "σημεία εισόδου",
)
ENTRY_TERMS = STRONG_ENTRY_TERMS + WEAK_ENTRY_TERMS
# Words that put a weak term in a port context.
PORT_WORDS = (
    "port", "ports", "harbour", "harbours", "harbor", "harbors", "marina", "marinas", "quay", "quays",
    "quai", "quais", "puerto", "puertos", "muelle", "muelles", "porto", "portos", "cais", "porti",
    "banchina", "approdo", "λιμάνι", "λιμάνια", "λιμένας", "λιμένα", "μαρίνα",
)
# A name inside an address or contact block is not a designation ("Cabinet Building, Hamilton").
ADDRESS_WINDOW = 40
ADDRESS_TOKENS = {
    "street", "road", "rd", "avenue", "ave", "lane", "drive", "highway", "row", "building",
    "bldg", "floor", "suite", "box", "tel", "telephone", "phone", "fax", "email", "rue",
    "boulevard", "bp", "calle", "avenida", "edificio", "piso", "rua", "andar", "piazza",
    "palazzo", "οδος",
}
_POSTCODE_AFTER = re.compile(r"[\s,]*(?:[A-Z]{2}\s?\d{1,2}|\d{4,6})\b")
# A numbered street address shortly after (or before) the name marks a contact listing.
_STREET_ADDRESS = re.compile(
    r"\b\d{1,4}\s+(?:[A-Z][\w'’.-]*\s+){1,3}(?:Street|St\.|Road|Rd\.?|Avenue|Ave\.?|Lane|Way|Drive|Highway|Boulevard)\b")
# Entry terms inside e-mail addresses or URLs ("immigration.seaport@...") do not count.
_EMAIL_OR_URL = re.compile(r"\S+@\S+|https?://\S+|www\.\S+")


def name_key(name: str | None) -> str:
    """Casefold, strip accents and punctuation: "Luka Brčko" -> "luka brcko"."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = _APOSTROPHES.sub("", text)
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def _strip(key: str, words: tuple[str, ...], leading: bool) -> str:
    for word in words:
        if leading and key.startswith(word + " "):
            return key[len(word) + 1:]
        if not leading and key.endswith(" " + word):
            return key[:-len(word) - 1]
    return key


def name_variants(*names: str | None) -> set[str]:
    """Matching keys for one or more names (a registry name and its alternates).

    Splits "A / B" and "A (B)", drops the possessive "'s" (with or without the apostrophe),
    strips facility words ("Port of", "Harbour", ...) and spells out St/Ste/Mt/Ft/Pt. The full
    key is always kept.
    """
    out: set[str] = set()
    for name in names:
        if not name:
            continue
        name = _POSSESSIVE.sub("", name)
        for part in re.split(r"[/;()]|,\s", name):
            key = name_key(part)
            if not key:
                continue
            stripped = _strip(_strip(key, _PREFIXES, leading=True), _SUFFIXES, leading=False)
            forms = {key, stripped}
            # A possessive written without its apostrophe: "St Georges" ~ "St George".
            forms |= {f[:-1] for f in forms if re.search(r"[a-z]{3}[^s]s$", f)}
            for candidate in forms:
                if candidate:
                    out.add(candidate)
                    out.add(" ".join(_TOKENS.get(t, t) for t in candidate.split()))
    return out


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def _coords(item: dict) -> tuple[float, float] | None:
    lat, lon = item.get("lat"), item.get("lon")
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) else None


def _entry_names(entry: dict) -> tuple:
    if entry.get("source") == "wpi":
        return entry.get("name"), entry.get("alternate_name")
    return entry.get("name"), entry.get("name_ascii")


def _entry_code(entry: dict) -> str | None:
    if entry.get("source") == "wpi":
        return entry.get("unlocode") or (f"WPI {entry['wpi_number']}" if entry.get("wpi_number") else None)
    return entry.get("code")


def match_port(port: dict, entries: list[dict], country_iso2: str | None = None,
               max_km: float = MAX_DISTANCE_KM) -> tuple[list[dict], list[dict]]:
    """(matches, coord_mismatch) for a port against registry entries.

    An entry matches when its country equals the port's (port country_iso2, else the given
    zone country; unknown country: no match), a name variant is shared (WPI alternate names,
    UN/LOCODE names without diacritics) and, when both sides have coordinates, it lies
    within max_km. Same name and country but further away goes to coord_mismatch. Keeps
    the closest entry per source; distance_km is None when either side lacks coordinates.
    """
    country = (port.get("country_iso2") or country_iso2 or "").upper() or None
    variants = name_variants(port.get("name"))
    here = _coords(port)
    best: dict[str, dict] = {}
    mismatched: list[dict] = []
    for entry in entries:
        if country is None or (entry.get("country_iso2") or "").upper() != country:
            continue
        if not variants & name_variants(*_entry_names(entry)):
            continue
        there = _coords(entry)
        distance = round(haversine_km(*here, *there), 2) if here and there else None
        evidence = dict(source=entry.get("source"), code=_entry_code(entry), name=entry.get("name"),
                        distance_km=distance)
        if entry.get("source") == "wpi" and entry.get("first_port_of_entry"):
            evidence["first_port_of_entry"] = entry["first_port_of_entry"]
        if distance is not None and distance > max_km:
            mismatched.append(evidence)
            continue
        current = best.get(evidence["source"])
        rank = math.inf if distance is None else distance
        if current is None or rank < (math.inf if current["distance_km"] is None else current["distance_km"]):
            best[evidence["source"]] = evidence
    mismatched.sort(key=lambda e: e["distance_km"])
    return [best[s] for s in SOURCES if s in best], mismatched


def source_texts_from_context(text: str | None) -> dict[str, str]:
    """{url: fetched text} from the pipeline's "[SOURCE: url]" blocks (synthesis blocks excluded)."""
    if not text:
        return {}
    marks = list(_SOURCE_BLOCK.finditer(text))
    out: dict[str, str] = {}
    for i, mark in enumerate(marks):
        body = text[mark.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        body = body.split("\n\n[SYNTHÈSE", 1)[0]
        out[mark.group(1)] = (out.get(mark.group(1), "") + "\n" + body).strip()
    return out


def port_key(name: str | None) -> str:
    """Storage key for a port: its name without facility words, St spelled out ("saint george")."""
    key = name_key(_POSSESSIVE.sub("", name or ""))
    stripped = _strip(_strip(key, _PREFIXES, leading=True), _SUFFIXES, leading=False) or key
    return " ".join(_TOKENS.get(t, t) for t in stripped.split())


class Variant(tuple):
    """A name variant's tokens. ``stripped``: only what is left after removing facility
    words ("hamilton" from "Port of Hamilton"), so it may just be the town."""
    stripped = False


def _full_keys(*names: str | None) -> set[str]:
    """Variant keys of the names as written (split on / ; ( ), possessive dropped), not stripped."""
    out = set()
    for name in names:
        for part in re.split(r"[/;()]|,\s", _POSSESSIVE.sub("", name or "")):
            key = name_key(part)
            forms = {key} | ({key[:-1]} if re.search(r"[a-z]{3}[^s]s$", key) else set())
            out |= {" ".join(_TOKENS.get(t, t) for t in f.split()) for f in forms if f}
    return out


def grounding_variants(name: str | None, alternates: tuple = ()) -> list[Variant]:
    """Name variants as token tuples, longest first: whole words, 4+ characters, not generic."""
    keys = {" ".join(_TOKENS.get(t, t) for t in v.split()) for v in name_variants(name, *alternates)}
    keys = {k for k in keys if len(k) >= 4 and k not in _GENERIC}
    full = _full_keys(name, *alternates)
    out = []
    for key in sorted(keys, key=len, reverse=True):
        variant = Variant(key.split())
        variant.stripped = key not in full
        out.append(variant)
    return out


def _fold(text: str) -> str:
    """Same-length casefolded text without accents, apostrophes or possessive "'s"."""
    text = _POSSESSIVE.sub(lambda m: " " * len(m.group()), text)
    text = _EMAIL_OR_URL.sub(lambda m: " " * len(m.group()), text)
    out = []
    for char in _APOSTROPHES.sub(" ", text):
        base = "".join(c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c))
        out.append((base.casefold() or " ")[0])
    return "".join(out)


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """(token, start, end) with offsets into the original text; St/Mt/... spelled out."""
    return [(_TOKENS.get(m.group(), m.group()), m.start(), m.end())
            for m in re.finditer(r"[^\W_]+", _fold(text))]


def _phrases(terms: tuple[str, ...]) -> list[tuple[str, ...]]:
    return sorted({tuple(t for t, _, _ in _tokens(term)) for term in terms}, key=len, reverse=True)


_STRONG_PHRASES = _phrases(STRONG_ENTRY_TERMS)
_WEAK_PHRASES = _phrases(WEAK_ENTRY_TERMS)
_PORT_WORDS = {t for word in PORT_WORDS for t, _, _ in _tokens(word)}


def _spans(tokens: list[tuple[str, int, int]], phrase: tuple[str, ...], plural_s: bool = False):
    n = len(phrase)
    for i in range(len(tokens) - n + 1):
        words = [t for t, _, _ in tokens[i:i + n]]
        if words[:-1] == list(phrase[:-1]) and (
                words[-1] == phrase[-1] or (plural_s and words[-1] == phrase[-1] + "s")):
            yield tokens[i][1], tokens[i + n - 1][2]


def _in_address(text: str, tokens: list[tuple[str, int, int]], start: int, end: int) -> bool:
    near = {t for t, s, e in tokens
            if (start - ADDRESS_WINDOW <= s < start) or (end < e <= end + ADDRESS_WINDOW)}
    return (bool(near & ADDRESS_TOKENS) or bool(_POSTCODE_AFTER.match(text, end, end + 12))
            or bool(_STREET_ADDRESS.search(text, end, end + 150))
            or bool(_STREET_ADDRESS.search(text, max(0, start - 60), start)))


def _port_word_near(tokens: list[tuple[str, int, int]], start: int, end: int, term: tuple[int, int]) -> bool:
    """A port word within PROXIMITY_CHARS of the name (the name counts), outside the term itself."""
    return any(t in _PORT_WORDS and start - PROXIMITY_CHARS <= s and e <= end + PROXIMITY_CHARS
               and not (term[0] <= s < term[1]) for t, s, e in tokens)


def entry_mention(text: str | None, variants: list[tuple[str, ...]]) -> dict | None:
    """Closest mention of a variant within PROXIMITY_CHARS of an entry term, outside addresses.

    A strong term (port of entry, clear customs, designated marina, ...) is enough. A weak
    term (customs, immigration, quarantine, harbour master) also needs a port word in the
    window and a full-name match, not a variant that is only the place left after stripping
    facility words. Returns {matched_name, entry_term, strength, gap, snippet}: snippet is a
    verbatim slice of at most SNIPPET_CHARS around both. A bare name gives None.
    """
    if not text or not variants:
        return None
    tokens = _tokens(text)
    terms = ([(" ".join(p), s, e, "strong") for p in _STRONG_PHRASES for s, e in _spans(tokens, p)]
             + [(" ".join(p), s, e, "weak") for p in _WEAK_PHRASES for s, e in _spans(tokens, p)])
    if not terms:
        return None
    best = None
    for variant in variants:
        for ns, ne in _spans(tokens, variant, plural_s=True):
            if _in_address(text, tokens, ns, ne):
                continue
            for term, ts, te, strength in terms:
                gap = max(ts - ne, ns - te, 0)
                if gap > PROXIMITY_CHARS:
                    continue
                if strength == "weak" and (getattr(variant, "stripped", False)
                                           or not _port_word_near(tokens, ns, ne, (ts, te))):
                    continue
                rank = (strength != "strong", gap)
                if best is None or rank < best[0]:
                    best = (rank, " ".join(variant), term, strength, min(ns, ts), max(ne, te))
    if best is None:
        return None
    (_, gap), matched, term, strength, core_start, core_end = best
    pad = max(0, (SNIPPET_CHARS - (core_end - core_start)) // 2)
    start = max(0, core_start - pad)
    end = min(len(text), start + max(SNIPPET_CHARS, core_end - core_start))
    start = max(0, min(start, end - SNIPPET_CHARS))
    return dict(matched_name=matched, entry_term=term, strength=strength, gap=gap,
                snippet=text[start:end].strip())


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def official_source_evidence(port: dict, source_texts: dict[str, str] | None,
                             alternates: tuple = ()) -> list[dict]:
    """Evidence from text fetched now: official URLs whose text designates the port.

    Candidate URLs are the port's source_urls (else every fetched document). A URL counts
    only if is_official_source() accepts it AND its text names the port (or an alternate)
    near an entry term, outside an address (see entry_mention). Text that was not fetched
    never counts. Each item: {url, domain, name_found, entry_term, snippet, evidence_source}.
    """
    if not source_texts:
        return []
    variants = grounding_variants(port.get("name"), alternates)
    out = []
    for url in port.get("source_urls") or list(source_texts):
        text = source_texts.get(url)
        if not text or not is_official_source([url]):
            continue
        mention = entry_mention(text, variants)
        if mention:
            out.append(dict(url=url, domain=_domain(url), name_found=mention["matched_name"],
                            entry_term=mention["entry_term"], snippet=mention["snippet"],
                            evidence_source="fetched_now"))
    return out


def stored_snippet_evidence(port: dict, records: list[dict] | None, alternates: tuple = ()) -> list[dict]:
    """Evidence from stored records (an earlier fetch): the same check, run on each snippet.

    Records are those of the port's zone; one applies when its port_key is one of the
    port's name variants and its snippet passes entry_mention for this port's names.
    """
    if not records:
        return []
    variants = grounding_variants(port.get("name"), alternates)
    keys = {" ".join(v) for v in variants}
    out = []
    for record in records:
        url = record.get("url") or ""
        if record.get("port_key") not in keys or not is_official_source([url]):
            continue
        mention = entry_mention(record.get("snippet"), variants)
        if mention:
            out.append(dict(url=url, domain=_domain(url), name_found=mention["matched_name"],
                            entry_term=mention["entry_term"], snippet=record["snippet"],
                            evidence_source="stored_snippet", fetched_at=record.get("fetched_at"),
                            content_sha256=record.get("content_sha256")))
    return out


def _merge_evidence(fetched: list[dict], stored: list[dict], limit: int = 3) -> list[dict]:
    """Fetched-now evidence first, then stored snippets for other URLs; at most `limit`."""
    out, seen = [], set()
    for item in fetched + stored:
        if item["url"] not in seen:
            seen.add(item["url"])
            out.append(item)
    return out[:limit]


def _label(match: dict) -> str:
    distance = "" if match.get("distance_km") is None else f" ({match['distance_km']:g} km)"
    return f"{_SOURCE_NAMES.get(match.get('source'), match.get('source'))} {match.get('code') or match.get('name')}{distance}"


def tier_for(official: list[dict], matches: list[dict]) -> str:
    if official:
        return TIER1 if matches else TIER2
    return TIER_REGISTERED if matches else TIER3


def tier_reasons(tier: str, official: list[dict], matches: list[dict], mismatched: list[dict],
                 country: str | None) -> list[str]:
    """English reasons: the tier, why, and what is missing. Never claims entry status from a registry."""
    registry = ", ".join(_label(m) for m in matches)
    sources = ", ".join(e["url"] + (" (stored snippet)" if e.get("evidence_source") == "stored_snippet" else "")
                        for e in official)
    reasons = {
        TIER1: f"{TIER_LABELS[TIER1]}: named in official source {sources}, and listed in {registry}",
        TIER2: f"{TIER_LABELS[TIER2]}: named in official source {sources}; not listed in UN/LOCODE "
               f"or the World Port Index (usual for marinas and yacht clearance points)",
        TIER_REGISTERED: f"{TIER_LABELS[TIER_REGISTERED]}: listed in {registry}, but no fetched official "
                         f"document names it; a registry listing does not establish port-of-entry status",
        TIER3: f"{TIER_LABELS[TIER3]}: no fetched official document names it and no UN/LOCODE or "
               f"World Port Index entry in {country or 'an unknown country'} matches it",
    }
    out = [reasons[tier]]
    if any(m.get("first_port_of_entry") == "Yes" for m in matches):
        out.append("World Port Index lists it as a First Port of Entry (supporting evidence only)")
    for m in mismatched[:3]:
        out.append(f"coord_mismatch: {_label(m)} has the same name and country but lies beyond the "
                   f"{MAX_DISTANCE_KM:g} km tolerance; check the coordinates")
    return out


def fetch_registry(country_iso2: str | None, run_id: str, bbox: tuple | None = None,
                   live_failed: bool = False) -> tuple[list[dict], str]:
    """Both registries for a country (or bbox); a live failure falls back to the sample fixtures.

    live_failed: Zetaris already failed earlier in this run, so go straight to the fixtures
    instead of waiting on the same outage again.
    """
    client = ZetarisClient(run_id=run_id)
    try:
        if live_failed and client.mode == "live":
            raise ZetarisUnavailable("Zetaris unavailable earlier in this run")
        entries = [e for source in SOURCES for e in client.get_registry_ports(
            country_iso2=country_iso2, bbox=bbox, source=source, limit=REGISTRY_LIMIT)]
        return entries, client.mode
    except ZetarisUnavailable as exc:
        if client.mode != "live":
            raise
        # Adapter messages are sanitized (no response bodies or credentials).
        client.trace_event("zetaris_fallback", status="fallback", reason=str(exc)[:200],
                           input_summary={"country_iso2": country_iso2, "bbox": bbox, "step": "registry"})
        client.mode = "sample"
        entries = [e for source in SOURCES for e in client.get_registry_ports(
            country_iso2=country_iso2, bbox=bbox, source=source, limit=REGISTRY_LIMIT)]
        return entries, "fallback"


def candidate_bbox(ports: list[dict], pad_deg: float = 0.5) -> tuple | None:
    """Padded box around the candidates' coordinates, used when the country is unknown."""
    points = [c for c in (_coords(p) for p in ports) if c]
    if not points or len(points) < len(ports):
        return None
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    return (max(-90.0, min(lats) - pad_deg), max(-180.0, min(lons) - pad_deg),
            min(90.0, max(lats) + pad_deg), min(180.0, max(lons) + pad_deg))


_PYCOUNTRY_WARNED = False


def _pycountry():
    """The pycountry module, or None after one warning per process.

    Without it, zones whose country comes from their sovereign (Alaska, the Azores) get
    no registry match, so their tiers drop; say so once instead of degrading silently.
    """
    global _PYCOUNTRY_WARNED
    try:
        import pycountry
        return pycountry
    except ImportError:
        if not _PYCOUNTRY_WARNED:
            _PYCOUNTRY_WARNED = True
            LOG.warning("pycountry is not installed: zones whose country comes from their sovereign "
                        "(e.g. Alaska, the Azores) get no registry match. "
                        "Install pycountry==26.2.16 (see docs/RUNNING.md).")
        return None


def zone_iso2(zone: dict | str | None) -> str | None:
    """ISO2 of a zone: its own iso2, else the EEZ index (by mrgid, then name), else its sovereign's."""
    zone = {"name": zone} if isinstance(zone, str) else dict(zone or {})
    for key in ("iso2", "country_iso2", "sov_iso2"):
        value = zone.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{2}", value):
            return value.upper()
    try:
        from app.services.listing_ref import load_eez_index
        index = load_eez_index()
    except Exception:  # noqa: BLE001 — missing catalogue: fall back to a bbox query
        return None
    mrgid, name = zone.get("mrgid"), name_key(zone.get("name") or zone.get("zone_name"))
    found = next((z for z in index if mrgid is not None and str(z.get("mrgid")) == str(mrgid)), None)
    found = found or next((z for z in index if name and name in (name_key(z.get("name")),
                                                                 name_key(z.get("geoname")))), None)
    if not found:
        return None
    if found.get("iso2"):
        return str(found["iso2"]).upper()
    pycountry = _pycountry()  # e.g. Alaska and the Azores have no iso2 of their own in the index
    if pycountry is None:
        return None
    try:
        return pycountry.countries.lookup(found.get("sovereign") or "").alpha_2
    except LookupError:
        return None


def fetch_for_zone(zone: dict | str | None, ports: list[dict], run_id: str,
                   live_failed: bool = False) -> dict:
    """Registry entries for a zone: {mode, country_iso2, entries}.

    Queries by country ISO2 when the zone resolves to one, else by a box around the
    candidates (matching then needs each port's own country_iso2). A Zetaris outage with
    no fallback gives mode "unavailable" and no entries, so no port gets a registry match.
    """
    country = zone_iso2(zone)
    bbox = None if country else candidate_bbox(ports)
    if country is None and bbox is None:
        return dict(mode="no-country", country_iso2=None, entries=[])
    try:
        entries, mode = fetch_registry(country, run_id, bbox, live_failed)
    except (ZetarisUnavailable, ValueError) as exc:
        ZetarisClient(run_id=run_id).trace_event(
            "registry_unavailable", status="error", reason=str(exc)[:200],
            input_summary={"country_iso2": country})
        entries, mode = [], "unavailable"
    return dict(mode=mode, country_iso2=country, entries=entries)


def annotate(ports: list[dict], registry: dict, run_id: str,
             source_texts: dict[str, str] | None = None,
             stored_records: list[dict] | None = None) -> tuple[list[dict], dict]:
    """Tier copies of ports and trace one registry_check event per port.

    Official evidence comes from source_texts (fetched now) or stored_records (stored
    snippets of the zone, see app/adapters/evidence_store.py), each passing the same check.

    Each port gets ``registry`` {tier, label, validation, matches, official_evidence,
    coord_mismatch, first_port_of_entry, reasons}, plus the backward-compatible
    ``registry_matches`` and ``zetaris_validation``. Returns (ports, summary) with summary
    {mode, country_iso2, registry_entries, official_count, tiers, coord_mismatch_count}.
    """
    entries, country = registry.get("entries") or [], registry.get("country_iso2")
    client = ZetarisClient(run_id=run_id)
    client.mode = registry.get("mode") or client.mode
    out = []
    for raw in ports:
        port = dict(raw)
        matches, mismatched = match_port(port, entries, country)
        alternates = tuple(n for e in entries for n in _entry_names(e)
                           if any(_entry_code(e) == m["code"] and e.get("source") == m["source"]
                                  for m in matches))
        official = _merge_evidence(official_source_evidence(port, source_texts, alternates),
                                   stored_snippet_evidence(port, stored_records, alternates))
        tier = tier_for(official, matches)
        fpoe = next((m["first_port_of_entry"] for m in matches if m.get("first_port_of_entry")), None)
        port["registry"] = dict(
            tier=tier, label=TIER_LABELS[tier], validation=VALIDATION[tier], matches=matches,
            official_evidence=official, coord_mismatch=mismatched, first_port_of_entry=fpoe,
            reasons=tier_reasons(tier, official, matches, mismatched, port.get("country_iso2") or country))
        port["registry_matches"] = matches
        port["zetaris_validation"] = VALIDATION[tier]
        client.trace_event(
            "registry_check", status=tier,
            input_summary={"port_name": port.get("name"), "country_iso2": port.get("country_iso2") or country,
                           "has_coordinates": _coords(port) is not None,
                           "documents_with_text": len(source_texts or {}),
                           "stored_records": len(stored_records or [])},
            registry_entries=len(entries), tier=tier, label=TIER_LABELS[tier],
            validation=VALIDATION[tier], matches=matches, official_evidence=official,
            coord_mismatch=mismatched[:3], max_distance_km=MAX_DISTANCE_KM,
            reasons=port["registry"]["reasons"])
        out.append(port)
    tiers = {t: sum(1 for p in out if p["registry"]["tier"] == t) for t in TIER_LABELS}
    summary = dict(mode=client.mode, country_iso2=country, registry_entries=len(entries),
                   official_count=tiers[TIER1] + tiers[TIER2], tiers=tiers,
                   coord_mismatch_count=sum(1 for p in out if p["registry"]["coord_mismatch"]))
    return out, summary


def check_ports(ports: list[dict], zone: dict | str | None, run_id: str,
                live_failed: bool = False,
                source_texts: dict[str, str] | None = None,
                stored_records: list[dict] | None = None) -> tuple[list[dict], dict]:
    """fetch_for_zone + annotate: the registry check step of the run pipeline."""
    return annotate(ports, fetch_for_zone(zone, ports, run_id, live_failed), run_id,
                    source_texts, stored_records)
