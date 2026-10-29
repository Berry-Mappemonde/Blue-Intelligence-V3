"""
poe.py — Sovereign pipeline [EEZ / Country] -> [pleasure-craft Ports of Entry].

Architecture:
  1. EEZ delimitation      : VLIZ Marine Regions WFS (World EEZ v12, ~285 zones), shapely-simplified.
  2. Targeted search       : SearXNG ∥ TinyFish Search (free) -> net
                             include_domains if disagreement / 0 official ->
                             grounded search (OpenRouter :online) as last resort
                             + round 2 "lessons" (indirect official document).
  3. Automatic whitelist   : ISO 3166-1 alpha-2 x PSL-validated state patterns (tldextract)
                             + auto-enriched exceptions.json + SERP regex filter (extract_core).
  4. Collection & parsing  : hybrid extract_core cascade — N1 httpx+trafilatura/PyMuPDF ->
                             N2 Readability/BS4 -> N3 TinyFish (1 call max/zone, last resort).
                             Selective depth=2 on internal regulatory links.
  5. Structured extraction : llm_core (OpenRouter, strict JSON)
                             + local RAG (rag_core) on long contexts.
  6. Geocoding             : geo_core (Nominatim -> GeoNames, semantic re-ranking).
                             Spatial point-in-EEZ validation (shapely).
  7. Monitoring            : MD5 hash + semantic similarity (skip if minor change).
  8. Storage               : non-destructive upsert via dedup_core (Bottom-Up
                             osm_confidence / anomaly enrichments are preserved).
"""
import asyncio
import fcntl
import json
import os
import re
import threading
import time
import uuid

import httpx
import pycountry
import tldextract
from shapely.geometry import shape, mapping
from shapely.prepared import prep
from urllib.parse import unquote, urlparse, urlunparse

from app.core.dedup import deduplicate_list, find_duplicate_in_list, merge_docs, normalize_name, text_similarity
from app.core.events import ZoneRecorder, emit
from app.core.extract import (
    audit_serp_filter, catalog_is_sufficient, catalog_ports_to_keep,
    catalog_ports_with_coords, extract_cascade, extract_structured_ports,
    internal_followups, is_geocodeable_name, looks_like_port_catalog,
    official_attachments, serp_filter, should_follow_attachments,
)
from app.core.geo import classify_poe_point, geocode_port_dual, inland_exception_flags
from app.core.llm import (
    LLM_CONTEXT_CHARS, extract_ports, grounded_search, llm_context_chars, llm_geocode_port,
)
from app.core.rag import select_list_context, semantic_similarity
from app.adapters import evidence_store
from app.adapters.zetaris import ZetarisClient
from app.agents import registry_check
from app.services.poe_confidence import apply_score, listing_role, score_port, zone_confidence_avg
from app.services.poe_zone_label import (
    keep_extracted_in_zone, search_polygon_name, serp_place_name,
    zone_qualifier, zone_search_location,
)

from app.config import DATA_DIR as DATA

EXCEPTIONS_FILE = DATA / "poe_exceptions.json"
MAP_FILE = DATA / "eez_world_map.geojson"

WFS_URL = "https://geo.vliz.be/geoserver/MarineRegions/wfs"
WFS_PAGE = 10
UA = "BerryMappemonde-BlueIntelligence/1.0 (+https://berrymappemonde.org; contact: clementfilisetti@berrymappemonde.org)"
EEZ_ATTRIBUTION = "Flanders Marine Institute — Marine Regions, Maritime Boundaries v12 (CC-BY 4.0)"

STALE_DAYS = 180
SIMPLIFY_STORE = 0.03
SIMPLIFY_MAP = 0.06

# Offline Public Suffix List snapshot (bundled with tldextract) — no network at import.
_tld = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=str(DATA / ".tld_cache"))

GOV_LABELS = ["gov", "gouv", "gob", "go", "govt", "gub", "gv"]
OFFICIAL_TOKENS = re.compile(
    r"gov|gouv|gob|douane|customs|aduana|zoll|immigration|border|maritime|port.?authority|coast.?guard|admin",
    re.I,
)

SEARX_PUBLIC_INSTANCES = [
    "https://searx.be",
    "https://search.inetol.net",
    "https://priv.au",
    "https://searx.tiekoetter.com",
    "https://paulgo.io",
    "https://search.sapti.me",
]

# Our local process (see infra/searxng/). Not a vendor key. Port 8890, because
# Zetaris uses 8888-8889 on the same machine.
LOCAL_SEARXNG_URL = "http://127.0.0.1:8890"

PIPELINE_VARIANTS = ("v1", "v2", "tinyfish")
_VARIANT_ALIASES = {
    "searx_only": "v2", "searx": "v2", "tf": "tinyfish", "v3": "tinyfish",
}


def normalize_variant(value: str | None) -> str:
    """v1 = sequential SearXNG; v2 = parallel SearXNG; tinyfish = union."""
    raw = (value or "tinyfish").strip().lower()
    v = _VARIANT_ALIASES.get(raw, raw)
    if v not in PIPELINE_VARIANTS:
        raise ValueError(
            f"variant inconnue {value!r} — attendu {PIPELINE_VARIANTS}")
    return v


def configured_searxng_url() -> str:
    """URL of our instance. SEARXNG_URL if set, otherwise loopback:8890."""
    own = (os.environ.get("SEARXNG_URL") or "").strip().rstrip("/")
    return own or LOCAL_SEARXNG_URL


def searx_instances() -> list[str]:
    """Self-hosted SearXNG instance first, public instances as fallback.

    If SEARXNG_URL is empty, still prefix http://127.0.0.1:8890
    (without duplicating). Public instances remain as fallback.
    """
    own = configured_searxng_url()
    rest = [u.rstrip("/") for u in SEARX_PUBLIC_INSTANCES if u.rstrip("/") != own]
    return [own] + rest


# ---------------------------------------------------------------------------
# Multilingual search matrix per EEZ (localized query generation)
# ---------------------------------------------------------------------------
LANG_BY_ISO2 = {
    # fr
    "FR": "fr", "MC": "fr", "BE": "fr", "SN": "fr", "CI": "fr", "CM": "fr", "GA": "fr",
    "MG": "fr", "DJ": "fr", "KM": "fr", "BJ": "fr", "TG": "fr", "GN": "fr", "CG": "fr",
    "CD": "fr", "HT": "fr", "NC": "fr", "PF": "fr", "PM": "fr", "WF": "fr", "YT": "fr",
    "RE": "fr", "GP": "fr", "MQ": "fr", "GF": "fr", "BL": "fr", "MF": "fr",
    # es
    "ES": "es", "MX": "es", "AR": "es", "CL": "es", "PE": "es", "CO": "es", "EC": "es",
    "VE": "es", "UY": "es", "PA": "es", "CR": "es", "GT": "es", "HN": "es", "NI": "es",
    "SV": "es", "DO": "es", "CU": "es", "GQ": "es",
    # pt
    "PT": "pt", "BR": "pt", "AO": "pt", "MZ": "pt", "CV": "pt", "GW": "pt", "ST": "pt", "TL": "pt",
    # ar
    "MA": "ar", "DZ": "ar", "TN": "ar", "LY": "ar", "EG": "ar", "SA": "ar", "AE": "ar",
    "QA": "ar", "KW": "ar", "BH": "ar", "OM": "ar", "YE": "ar", "JO": "ar", "LB": "ar", "SY": "ar", "IQ": "ar", "SD": "ar",
    # other
    "ID": "id", "IT": "it", "GR": "el", "TR": "tr", "RU": "ru", "CN": "zh", "JP": "ja",
    "DE": "de", "NL": "nl", "TH": "th", "VN": "vi", "KR": "ko",
}
QUERY_TEMPLATES = {
    "fr": "ports d'entrée officiels liste douane décret désignés {name}",
    "es": "puertos habilitados puertos de entrada oficiales decreto aduana lista {name}",
    "pt": "portos de entrada oficiais lista alfândega decreto designados {name}",
    "ar": "موانئ الدخول الرسمية قائمة الجمارك مرسوم {name}",
    "id": "pelabuhan masuk resmi daftar bea cukai keputusan {name}",
    "it": "porti di ingresso ufficiali elenco dogana decreto {name}",
    "el": "επίσημα λιμάνια εισόδου κατάλογος τελωνείο διάταγμα {name}",
    "tr": "resmi giriş limanları liste gümrük kararname {name}",
    "ru": "официальные порты въезда список таможня указ {name}",
    "zh": "官方入境港口 名单 海关 法令 {name}",
    "ja": "公式入国港 一覧 税関 政令 {name}",
    "de": "offizielle Eingangshäfen Liste Zoll Verordnung {name}",
    "nl": "officiële havens van binnenkomst lijst douane besluit {name}",
    "th": "ท่าเรือเข้าเมืองอย่างเป็นทางการ รายชื่อ ศุลกากร {name}",
    "vi": "cảng nhập cảnh chính thức danh sách hải quan nghị định {name}",
    "ko": "공식 입국 항구 목록 세관 법령 {name}",
}

# *Path* tokens (not the hostname). "douane" / "customs" in
# the domain give a false bonus to a customs homepage.
_LIST_PATH_TOKENS = (
    "port-of-entry", "ports-of-entry", "ports-entree", "portos-de-entrada",
    "habilit", "puertos", "designated", "decreto", "decree", "decret",
    "arrete", "arrêté", "gazette", "legislat", "jorf", "liste", "listen",
    "list-of", "listing", "ordonnance", "ordinance", "plaisance",
    "points-d-entree", "points-of-entry", "eligibles", "ppf",
    "first-arrival", "places-of-first", "seaports", "small-craft",
    "sailing-to", "terminales", "formalit", "niue_laws", "habilitados",
    "marina-mercante", "vous-naviguez", "inventario", "pages/customs",
    "capitanias", "jurisdiccion", "ley-de-marinas", "actividades-conexas",
    "yacht-tourism", "autorizim", "akciz", "peshkimit", "anijet",
    "annexe", "pleasure-craft", "pleasure_craft", "c1331", "jorftext",
)
_JUNK_PATH_TOKENS = (
    "formulaire", "immigration", "export", "brexit", "leaflet",
    "brochure", "travellers", "tax-refund", "counterfeit",
)
_HOME_PATH_RE = re.compile(
    r"(^/?$|/accueil\b|/home\b|/en/?$|information-available|"
    r"what-know-when-travelling|/fiche/|/particuliers/?$)",
    re.I,
)


# Language of this polygon's state pages, not of the sovereign
# (Sint Maarten: EN site, NL sovereign).
_POLYGON_SEARCH_LANG = {
    "SX": None,
    "AW": None,
    "CW": None,
    "BQ": None,
}


def zone_search_lang(zone: dict) -> str | None:
    """SERP language: polygon ISO2 if it is in the matrix, otherwise the sovereign."""
    iso = (zone.get("iso2") or "").upper()
    if iso in _POLYGON_SEARCH_LANG:
        return _POLYGON_SEARCH_LANG[iso]
    for cc in (zone.get("iso2"), zone.get("sov_iso2")):
        lang = LANG_BY_ISO2.get((cc or "").upper())
        if lang:
            return lang
    return None


def is_france_mainland_eez(zone: dict) -> bool:
    """Mainland hexagon / joint metropolitan regimes — not Mayotte or the DROM."""
    if (zone.get("iso2") or "").upper() != "FR":
        return False
    key, _ = zone_qualifier(zone)
    return key in ("hexagone", "metropole", "joint")


def localized_query(zone: dict) -> str | None:
    """Query translated into the VLIZ polygon language (not the country aggregate)."""
    place = serp_place_name(zone)
    if not place:
        return None
    if is_france_mainland_eez(zone):
        return (
            "liste ports de plaisance éligibles PPF "
            f"points de passage frontaliers douane {place}"
        )
    if polygon_iso2(zone) == "SX":
        return f"Sint Maarten customs department official ports of entry {place}"
    if polygon_iso2(zone) == "YT":
        return (
            f"arrêté 4 février 2015 points de passage contrôlés "
            f"annexe I frontières maritimes {place}"
        )
    if polygon_iso2(zone) == "GB":
        return f"submit a pleasure craft report sPCR ports of entry {place}"
    if polygon_iso2(zone) == "EG":
        return f"Egypt SIS yacht tourism specialized marinas official {place}"
    if polygon_iso2(zone) == "AL":
        return (
            f"dogana autorizim akcizë anijet e peshkimit porteve detare {place}"
        )
    lang = zone_search_lang(zone)
    tpl = QUERY_TEMPLATES.get(lang or "")
    if tpl:
        return tpl.format(name=place)
    return family_classic_query(place, lang)


# Generic families — the 11 polygons stay on if iso (hints / site: / localized).
_LIST_CANDIDATE_BONUS = 0.45


def family_classic_query(place: str, lang: str | None = None) -> str:
    """"Ports of entry / gazette" query for a polygon without an iso if."""
    if lang == "fr":
        return f"ports d'entrée officiels liste douane décret yacht plaisance {place}"
    if lang == "es":
        return f"puertos habilitados decreto lista oficial yates marinas {place}"
    return f"official ports of entry yacht marina customs gazette list {place}"


def family_default_hints(place: str) -> list[str]:
    """legal + pleasure + arrival — never a port name."""
    generic = f"{place} customs act designated ports of entry official legislation gazette"
    return [
        f"{place} yacht marina small craft pleasure craft official list",
        f"{place} first arrival points of entry clearance gazette",
        generic,
    ]


def lessons_learned_query(zone: dict) -> str:
    """Round 2: official document that is not titled "ports of entry"."""
    place = serp_place_name(zone) or search_polygon_name(zone) or ""
    return (
        f"{place} yacht marina small craft pleasure craft "
        f"first arrival points de passage autorizim akcizë anije "
        f"kartelë formalités plaisance sPCR"
    )


def google_style_tld_tokens(zone: dict) -> str:
    """Polygon ccTLD; + .fr for a DROM (Légifrance is not on .yt)."""
    iso = (zone.get("iso2") or "").strip().lower()
    sov = (zone.get("sov_iso2") or "").strip().lower()
    toks: list[str] = []
    if iso:
        toks.append(f".{iso}")
    if sov == "fr" and iso and iso != "fr" and ".fr" not in toks:
        toks.append(".fr")
    return " ".join(toks)


def google_style_query(zone: dict, lang: str | None = None) -> str:
    """Short Google-bar style phrase: place + PoE + sailing + official + TLD."""
    place = serp_place_name(zone) or search_polygon_name(zone) or ""
    place = place.replace("-", " ")
    tld_tok = google_style_tld_tokens(zone)
    if lang == "fr":
        q = f"{place} port d'entrée officiel liste plaisance {tld_tok}"
    elif lang == "es":
        q = f"{place} puertos habilitados lista oficial yates {tld_tok}"
    elif lang == "nl":
        q = f"{place} officiële lijst toegangshavens pleziervaartuigen {tld_tok}"
    else:
        q = f"{place} Ports of Entry sailing list official {tld_tok}"
    return " ".join(q.split())


def google_style_shots(zone: dict) -> list[tuple[str, str]]:
    """(lang, query) — EN always; + fr/es/nl if the polygon language is one of those.

    SX has zone_search_lang=None: a single EN shot, not Dutch.
    """
    shots = [("en", google_style_query(zone))]
    lang = zone_search_lang(zone)
    if lang in ("fr", "es", "nl"):
        loc = google_style_query(zone, lang)
        if loc != shots[0][1]:
            shots.append((lang, loc))
    return shots


def has_list_candidate(rows: list | None, min_bonus: float = _LIST_CANDIDATE_BONUS) -> bool:
    """List PDF or sufficiently deep page — not a customs homepage."""
    for c in rows or []:
        u = c.get("url") or ""
        if not u:
            continue
        bonus = list_url_bonus(u)
        if bonus >= min_bonus:
            return True
        if ".pdf" in u.lower() and bonus >= 0.15:
            return True
    return False


def needs_lessons_round(official: list | None) -> bool:
    return not has_list_candidate(official)


def bootstrap_national_hits(rejected: list | None, zone: dict) -> list[dict]:
    """State domain even outside PSL / outside .{iso2} (sintmaartengov.org lesson)."""
    cc_ok = {c for c in ((zone.get("iso2") or "").lower(),) if c}
    out: list[dict] = []
    for c in rejected or []:
        dom = (c.get("domain") or "").lower()
        u = c.get("url") or ""
        if not dom or not OFFICIAL_TOKENS.search(dom):
            continue
        if is_foreign_gov_domain(dom, cc_ok):
            continue
        path = urlparse(u).path or "/"
        if list_url_bonus(u) <= 0 and _HOME_PATH_RE.search(path):
            continue
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# UNCLOS legal qualification of EEZs without a PoE (business logic)
# ---------------------------------------------------------------------------
UNINHABITED_RE = re.compile(
    r"bouvet|heard|mcdonald|clipperton|crozet|kerguelen|amsterdam|saint.?paul"
    r"|south georgia|sandwich|peter i|baker|howland|jarvis|johnston|kingman"
    r"|palmyra|wake|navassa|ashmore|cartier|coral sea|macquarie|prince edward isl"
    r"|tromelin|europa|glorioso|glorieuses|juan de nova|bassas da india|chagos"
    r"|clipperton|midway|paracel|spratly|scarborough|matthew|hunter"
    r"|abu musa|tunb|enenkio",
    re.I,
)


def qualify_unclos(zone: dict) -> dict | None:
    """Categorize an EEZ with no physical PoE under maritime law.
    Return None if the zone has PoEs (no qualification required)."""
    if (zone.get("poe_count") or 0) > 0:
        return None
    name = f"{zone.get('name') or ''} {zone.get('geoname') or ''}"
    pol = (zone.get("pol_type") or "").lower()
    anchor = zone.get("anchor") or [0, 0]
    lat = anchor[1] if len(anchor) > 1 else 0
    if lat < -60:
        return {"code": "antarctic", "basis": "Traité sur l'Antarctique art. VI"}
    if "overlapping" in pol:
        return {"code": "overlapping_claim", "basis": "UNCLOS art. 74/83"}
    if "joint" in pol:
        return {"code": "joint_regime", "basis": "UNCLOS art. 74(3)/83(3)"}
    if UNINHABITED_RE.search(name):
        return {"code": "uninhabited", "basis": "UNCLOS art. 2 & 25"}
    return {"code": "sovereign_entry", "basis": "UNCLOS art. 17-19"}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_stale(doc: dict) -> bool:
    g = doc.get("generated_at")
    if not g:
        return False
    try:
        gen = time.mktime(time.strptime(g[:19], "%Y-%m-%dT%H:%M:%S"))
        return (time.time() - gen) > STALE_DAYS * 86400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Whitelist / Gatekeeper
# ---------------------------------------------------------------------------
_exceptions_thread_lock = threading.Lock()

_EXCEPTION_BUCKETS = (
    "manual", "auto", "seed_urls", "search_hints",
    "seed_urls_mrgid", "blacklist_urls_mrgid", "blacklist_domains_mrgid",
)


def _empty_exceptions() -> dict:
    return {"manual": {}, "auto": {}}


def _parse_exceptions_text(raw: str) -> dict:
    if not (raw or "").strip():
        return _empty_exceptions()
    data = json.loads(raw)
    return data if isinstance(data, dict) else _empty_exceptions()


def _merge_bucket(base: dict | None, incoming: dict | None) -> dict:
    out = {k: list(v) for k, v in (base or {}).items() if isinstance(v, list)}
    for key, vals in (incoming or {}).items():
        cur = out.setdefault(key, [])
        for item in vals or []:
            if item not in cur:
                cur.append(item)
    return out


def merge_exceptions(disk: dict, incoming: dict) -> dict:
    """Union of buckets (auto / seed_urls / …) so a concurrent bootstrap
    is not lost. Keys outside buckets are taken from disk then overwritten
    by the incoming payload if it carries them."""
    merged = dict(disk or {})
    merged.update({k: v for k, v in (incoming or {}).items()
                   if k not in _EXCEPTION_BUCKETS})
    for bucket in _EXCEPTION_BUCKETS:
        merged[bucket] = _merge_bucket((disk or {}).get(bucket),
                                       (incoming or {}).get(bucket))
    return merged


def load_exceptions() -> dict:
    try:
        with _exceptions_thread_lock:
            if not EXCEPTIONS_FILE.is_file():
                return _empty_exceptions()
            with open(EXCEPTIONS_FILE, "r", encoding="utf-8") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                    return _parse_exceptions_text(fh.read())
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        return _empty_exceptions()


def polygon_iso2(zone: dict) -> str:
    """VLIZ polygon ISO2, never the sovereign (Niue ≠ NZ, Mayotte ≠ FR)."""
    return (zone.get("iso2") or "").strip().upper()


def seed_url_candidates(zone: dict, exceptions: dict | None = None) -> list[dict]:
    """Pinned official URLs (state page or PDF — never a list of names).

    Polygon grain (`seed_urls_mrgid`) first — Mayotte ≠ hexagon — then ISO2.
    """
    exc = exceptions or load_exceptions()
    urls: list[str] = []
    try:
        mid = str(int(zone.get("mrgid") or 0))
    except (TypeError, ValueError):
        mid = ""
    if mid and mid != "0":
        urls.extend((exc.get("seed_urls_mrgid") or {}).get(mid) or [])
    cc = polygon_iso2(zone)
    if cc:
        urls.extend((exc.get("seed_urls") or {}).get(cc) or [])
    blocked = set(review_blacklist_urls(zone, exc))
    seen, out = set(), []
    for u in urls:
        if not u or u in seen or _normalize_url(u) in blocked:
            continue
        seen.add(u)
        out.append({"url": u, "domain": domain_of(u)})
    return out


def default_search_hints(zone: dict) -> list[str]:
    """Niue / Mexico lesson, for EVERY VLIZ polygon: customs law + list.
    Never a port name. The name is the polygon's (Mayotte ≠ France)."""
    poly = serp_place_name(zone)
    if not poly:
        return []
    generic = f"{poly} customs act designated ports of entry official legislation gazette"
    iso = polygon_iso2(zone)
    if is_france_mainland_eez(zone):
        return [
            f"liste ports de plaisance éligibles douane {poly}",
            f"carte PPF maritimes points de passage frontaliers {poly}",
            generic,
        ]
    if iso == "NZ":
        return [
            f"places of first arrival seaports MPI {poly}",
            f"small craft sailing customs {poly}",
            generic,
        ]
    if iso == "VE":
        return [
            f"Ley de Marinas y Actividades Conexas INEA {poly}",
            f"Reglamento jurisdicción capitanías de puerto {poly}",
            generic,
        ]
    if iso == "NC":
        return [
            f"formalités douanières navires de plaisance douane {poly}",
            f"ports d'entrée officiels liste douane décret {poly}",
            generic,
        ]
    if iso == "MX":
        return [
            f"{poly} puertos habilitados decreto Coordinación General de Puertos y Marina Mercante",
            generic,
        ]
    if iso == "NU":
        return [
            f"Niue Customs Act Port of Entry Order official legislation",
            generic,
        ]
    if iso == "SX":
        return [
            f"Sint Maarten customs department official {poly}",
            generic,
        ]
    if iso == "YT":
        return [
            f"arrêté 4 février 2015 points de passage contrôlés annexe I {poly}",
            f"Légifrance JORFTEXT000030235682 frontières maritimes {poly}",
            generic,
        ]
    if iso == "GB":
        return [
            f"submit a pleasure craft report ports of entry {poly}",
            f"sPCR C1331 pleasure craft sailing {poly}",
            generic,
        ]
    if iso == "EG":
        return [
            f"SIS Egypt yacht tourism specialized marinas {poly}",
            generic,
        ]
    if iso == "AL":
        return [
            f"dogana autorizim përjashtimin akcizës anijet e peshkimit "
            f"porteve detare {poly}",
            generic,
        ]
    lang = zone_search_lang(zone)
    if lang == "es":
        return [
            f"{poly} puertos habilitados decreto lista oficial",
            f"{poly} yates marinas turismo náutico lista oficial",
            generic,
        ]
    if lang == "fr":
        return [
            f"ports d'entrée officiels liste douane décret {poly}",
            f"yacht marina plaisance liste officielle {poly}",
            generic,
        ]
    return family_default_hints(poly)


def search_hint_queries(zone: dict, exceptions: dict | None = None) -> list[str]:
    """Extra queries for each EEZ (defaults + country exceptions)."""
    exc = exceptions or load_exceptions()
    seen, out = set(), []
    for q in default_search_hints(zone):
        if q not in seen:
            seen.add(q)
            out.append(q)
    cc = polygon_iso2(zone)
    if cc:
        for q in (exc.get("search_hints") or {}).get(cc, []) or []:
            q = (q or "").strip()
            if q and q not in seen:
                seen.add(q)
                out.append(q)
    return out[:5]


def list_url_bonus(url: str) -> float:
    """Prefer decree / PDF / list page — not the customs homepage (CDC §18)."""
    raw = (url or "").strip()
    if not raw:
        return 0.0
    parsed = urlparse(raw)
    path = unquote((parsed.path or "") + "?" + (parsed.query or "")).lower()
    score = sum(0.15 for tok in _LIST_PATH_TOKENS if tok in path)
    if path.endswith(".pdf") or ".pdf" in path:
        score += 0.35
    score -= sum(0.2 for tok in _JUNK_PATH_TOKENS if tok in path)
    if _HOME_PATH_RE.search(path.split("?", 1)[0]):
        score -= 0.45
    leaf = path.split("?", 1)[0].strip("/")
    if len(leaf) < 8:
        score -= 0.3
    return score


def remember_seed_urls(zone: dict, urls: list[str], exceptions: dict | None = None,
                       persist: bool = True, max_per_country: int = 4) -> list[str]:
    """Remember official URLs that produced a catalog — the next EEZ of
    the same country no longer waits on SERP (generalized Mexico / Niue lesson)."""
    cc = polygon_iso2(zone)
    if not cc or not urls:
        return []
    exc = exceptions if exceptions is not None else load_exceptions()
    bucket = exc.setdefault("seed_urls", {}).setdefault(cc, [])
    added = []
    for u in urls:
        if not u or not str(u).startswith("http") or u in bucket:
            continue
        if not url_allowed(u, build_whitelist(zone.get("iso2"), zone.get("sov_iso2"), exc)):
            if not OFFICIAL_TOKENS.search(domain_of(u) or ""):
                continue
        if len(bucket) >= max_per_country:
            break
        bucket.append(u)
        added.append(u)
    if added and persist:
        save_exceptions(exc)
    return added


def review_blacklist_urls(zone: dict, exceptions: dict | None = None) -> list[str]:
    """URLs dropped in Review for this polygon (normalized)."""
    exc = exceptions or load_exceptions()
    try:
        mid = str(int(zone.get("mrgid") or 0))
    except (TypeError, ValueError):
        mid = ""
    raw = list((exc.get("blacklist_urls_mrgid") or {}).get(mid) or []) if mid else []
    return [_normalize_url(u) for u in raw if u]


def remember_review_pins(mrgid, kept_urls: list[str] | None,
                         dropped_urls: list[str] | None = None,
                         persist: bool = True) -> dict:
    """Pin Gold lists at the ``mrgid`` grain (not the country)."""
    try:
        mid = str(int(mrgid))
    except (TypeError, ValueError):
        return {"kept": [], "dropped": []}
    if mid == "0":
        return {"kept": [], "dropped": []}
    exc = load_exceptions()
    seed = exc.setdefault("seed_urls_mrgid", {}).setdefault(mid, [])
    kept_added = []
    for u in kept_urls or []:
        u = str(u or "").strip()
        if not u.startswith("http") or u in seed:
            continue
        seed.append(u)
        kept_added.append(u)
    dropped = exc.setdefault("blacklist_urls_mrgid", {}).setdefault(mid, [])
    drop_added = []
    for u in dropped_urls or []:
        u = str(u or "").strip()
        if not u.startswith("http"):
            continue
        if u not in dropped:
            dropped.append(u)
            drop_added.append(u)
    if persist and (kept_added or drop_added) and not os.environ.get("PYTEST_CURRENT_TEST"):
        save_exceptions(exc)
    return {"kept": kept_added, "dropped": drop_added}


def urls_with_catalog(texts: list[str]) -> list[str]:
    out = []
    for block in texts or []:
        m = re.match(r"\[SOURCE: ([^\]]+)\]", (block or "").lstrip())
        if m and extract_structured_ports(block):
            out.append(m.group(1).strip())
    return out


def save_exceptions(exc: dict):
    """Flock write + merge-on-write: two concurrent zones no longer
    overwrite each other's bootstraps (corrupt JSON or lost domains)."""
    try:
        EXCEPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _exceptions_thread_lock:
            with open(EXCEPTIONS_FILE, "a+", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.seek(0)
                    disk = _parse_exceptions_text(fh.read())
                    merged = merge_exceptions(disk, exc or {})
                    fh.seek(0)
                    fh.truncate()
                    json.dump(merged, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                    if isinstance(exc, dict):
                        exc.clear()
                        exc.update(merged)
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


def iso2_of(iso3: str | None) -> str | None:
    if not iso3:
        return None
    try:
        c = pycountry.countries.get(alpha_3=iso3.upper())
        return c.alpha_2 if c else None
    except Exception:
        return None


def _is_public_suffix(cand: str) -> bool:
    try:
        return _tld("x." + cand).suffix == cand
    except Exception:
        return False


def eez_iso2_set() -> set[str]:
    """ISO codes of countries / territories that actually have an EEZ."""
    try:
        from app.services.listing_ref import load_eez_index
        out = set()
        for z in load_eez_index():
            cc = (z.get("iso2") or "").upper()
            if cc:
                out.add(cc)
        return out
    except Exception:
        return set()


def _preferred_gov_label(iso2: str) -> str:
    lang = LANG_BY_ISO2.get((iso2 or "").upper())
    if lang == "fr":
        return "gouv"
    if lang == "es":
        return "gob"
    return "gov"


def build_whitelist(iso2: str | None, sov_iso2: str | None, exceptions: dict | None = None) -> list[str]:
    """State domains of countries that have an EEZ (a hint, no longer a gate).

    A wildcard PSL suffix (Nepal, Jamaica…) no longer produces the 7 patterns:
    we keep the usual label (gov / gouv / gob). Landlocked countries are
    ignored — even if NP or SK is passed by mistake."""
    exc = exceptions or load_exceptions()
    maritime = eez_iso2_set()
    out: set[str] = set()
    primary = iso2 or sov_iso2
    for cc in {c for c in (primary,) if c}:
        up = cc.upper()
        if maritime and up not in maritime:
            continue
        low = cc.lower()
        hits = []
        for lbl in GOV_LABELS:
            cand = f"{lbl}.{low}"
            if _is_public_suffix(cand):
                hits.append(cand)
        if len(hits) >= 3:
            pref = f"{_preferred_gov_label(up)}.{low}"
            if pref in hits:
                out.add(pref)
            elif f"gov.{low}" in hits:
                out.add(f"gov.{low}")
            else:
                out.add(hits[0])
        else:
            out.update(hits)
        for bucket in ("manual", "auto"):
            for dom in (exc.get(bucket) or {}).get(up, []):
                out.add(dom.lower())
    return sorted(out)


def url_allowed(url: str, whitelist: list[str]) -> bool:
    try:
        ext = _tld(url)
    except Exception:
        return False
    fqdn = ".".join(p for p in [ext.subdomain, ext.domain, ext.suffix] if p)
    for w in whitelist:
        if "." not in w:  # bare public suffix, e.g. "gov" (USA)
            if ext.suffix == w or ext.suffix.endswith("." + w):
                return True
        elif fqdn == w or fqdn.endswith("." + w):
            return True
    return False


def domain_of(url: str) -> str:
    try:
        ext = _tld(url)
        return ".".join(p for p in [ext.domain, ext.suffix] if p)
    except Exception:
        return ""


def is_foreign_gov_domain(domain: str, cc_ok: set[str]) -> bool:
    """Drop a state site that does not belong to the EEZ (Samoa lesson:
    congress.gov / customs.gov.ph are not Samoan sources)."""
    if not domain or not OFFICIAL_TOKENS.search(domain):
        return False
    suffix = domain.rsplit(".", 1)[-1].lower()
    if len(suffix) == 2 and suffix not in cc_ok:
        return True
    if suffix == "gov" and "us" not in cc_ok:
        return True
    return False


# ---------------------------------------------------------------------------
# 1. EEZ referential (VLIZ WFS)
# ---------------------------------------------------------------------------
def _round_coords(obj, nd=3):
    if isinstance(obj, (list, tuple)):
        return [_round_coords(x, nd) for x in obj]
    if isinstance(obj, float):
        return round(obj, nd)
    return obj


def _simplify_for_mongo(geom):
    """Simplify until serialized size is under ~4 MB (Mongo 16 MB limit)."""
    for tol in (SIMPLIFY_STORE, 0.08, 0.15, 0.3):
        simp = geom.simplify(tol, preserve_topology=True)
        gj = mapping(simp)
        gj["coordinates"] = _round_coords(gj["coordinates"], 4)
        if len(json.dumps(gj)) < 4_000_000:
            return gj, simp
    return gj, simp  # last attempt regardless


def _anchor_of(simp):
    """Representative point of the largest component — antimeridian-safe."""
    try:
        geoms = list(simp.geoms) if hasattr(simp, "geoms") else [simp]
        big = max(geoms, key=lambda g: g.area)
        pt = big.representative_point()
        return [round(pt.x, 4), round(pt.y, 4)]
    except Exception:
        c = simp.centroid
        return [round(c.x, 4), round(c.y, 4)]


async def build_referential(db, state, force: bool = False):
    """Download the ~285 world EEZs (paginated WFS), simplify, upsert Mongo,
    write the map GeoJSON. Idempotent — preserves existing status/poe_count."""
    log = state.log
    exceptions = load_exceptions()
    async with httpx.AsyncClient(timeout=300, headers={"User-Agent": UA}) as client:
        base = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": "MarineRegions:eez", "outputFormat": "application/json",
            "srsName": "EPSG:4326", "sortBy": "mrgid",
        }
        r = await client.get(WFS_URL, params={**base, "count": 1, "propertyName": "mrgid"})
        r.raise_for_status()
        total = int(r.json().get("numberMatched") or 0)
        state.total = total
        log(f"VLIZ Marine Regions WFS: {total} ZEE mondiales à intégrer ({EEZ_ATTRIBUTION})")

        done = 0
        for start in range(0, total, WFS_PAGE):
            page = None
            for attempt in range(3):
                try:
                    r = await client.get(WFS_URL, params={**base, "count": WFS_PAGE, "startIndex": start})
                    r.raise_for_status()
                    page = r.json().get("features") or []
                    break
                except Exception as e:
                    log(f"page {start}: tentative {attempt + 1} échouée ({type(e).__name__}) — retry")
                    await asyncio.sleep(3)
            if page is None:
                raise RuntimeError(f"WFS page {start} unreachable after 3 attempts")

            for feat in page:
                props = feat.get("properties") or {}
                mrgid = props.get("mrgid")
                if not mrgid or not feat.get("geometry"):
                    continue

                def _work(f=feat):
                    g = shape(f["geometry"])
                    gj, simp = _simplify_for_mongo(g)
                    return gj, list(simp.bounds), _anchor_of(simp)

                gj, bbox, anchor = await asyncio.to_thread(_work)
                iso2 = iso2_of(props.get("iso_ter1"))
                sov2 = iso2_of(props.get("iso_sov1"))
                wl = build_whitelist(iso2, sov2, exceptions)
                base_doc = {
                    "mrgid": int(mrgid),
                    "geoname": props.get("geoname"),
                    "name": props.get("territory1") or props.get("geoname"),
                    "pol_type": props.get("pol_type"),
                    "iso_ter1": props.get("iso_ter1"),
                    "iso2": iso2,
                    "sovereign": props.get("sovereign1"),
                    "sov_iso2": sov2,
                    "area_km2": props.get("area_km2"),
                    "geometry": gj,
                    "bbox": bbox,
                    "anchor": anchor,
                    "whitelist": wl,
                    "updated_at": now_iso(),
                }
                await db.eez_zones.update_one(
                    {"mrgid": int(mrgid)},
                    {
                        "$set": base_doc,
                        "$setOnInsert": {
                            "_id": str(uuid.uuid4()), "status": "non_generee",
                            "poe_count": 0, "generated_at": None,
                            "sources": [], "source_hashes": {}, "last_error": None,
                        },
                    },
                    upsert=True,
                )
                done += 1
            state.progress = done
            log(f"{done}/{total} ZEE intégrées")

    # --- map file (extra simplification) ---
    log("Construction du GeoJSON carte…")

    def _map_feature(doc):
        g = shape(doc["geometry"]).simplify(SIMPLIFY_MAP, preserve_topology=True)
        gj = mapping(g)
        gj["coordinates"] = _round_coords(gj["coordinates"], 3)
        return {
            "type": "Feature",
            "geometry": gj,
            "properties": {
                "mrgid": doc["mrgid"], "name": doc.get("name"),
                "geoname": doc.get("geoname"), "iso2": doc.get("iso2"),
                "sovereign": doc.get("sovereign"), "pol_type": doc.get("pol_type"),
            },
        }

    feats = []
    async for doc in db.eez_zones.find({}):
        feats.append(await asyncio.to_thread(_map_feature, doc))
    fc = {"type": "FeatureCollection", "attribution": EEZ_ATTRIBUTION, "features": feats}
    txt = await asyncio.to_thread(json.dumps, fc)
    MAP_FILE.write_text(txt, encoding="utf-8")
    log(f"GeoJSON carte écrit: {len(feats)} zones, {len(txt) // 1024} Ko")
    return {"zones": done, "map_file_kb": len(txt) // 1024}


# ---------------------------------------------------------------------------
# 2. Search: SearXNG -> grounded search (llm_core) + Level-2 Retry
# ---------------------------------------------------------------------------
async def search_searxng(query: str, log) -> list[dict]:
    for inst in searx_instances():
        local = inst.startswith("http://127.0.0.1") or inst.startswith("http://localhost")
        timeout = 20 if local else 8
        try:
            async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA}) as client:
                r = await client.get(f"{inst}/search", params={"q": query, "format": "json"})
                if r.status_code != 200:
                    continue
                ctype = (r.headers.get("content-type") or "").lower()
                if "json" not in ctype:
                    try:
                        payload = r.json()
                    except Exception:
                        continue
                else:
                    payload = r.json()
                results = (payload.get("results") or [])[:10]
                if results:
                    log(f"SearXNG {inst}: {len(results)} résultats")
                    return [{"url": x.get("url"), "domain": domain_of(x.get("url") or ""),
                             "engine": "searxng"} for x in results if x.get("url")]
        except Exception:
            continue
    log("SearXNG: aucune instance exploitable — bascule sur recherche groundée")
    return []


async def search_grounded(zone: dict, whitelist: list[str], log, query_override: str | None = None):
    """Grounded search via llm_core (OpenRouter :online)."""
    name = search_polygon_name(zone)
    hints = ", ".join(whitelist[:6]) if whitelist else "official government domains"
    prompt = query_override or (
        f"Find the OFFICIAL government sources (customs, immigration, maritime/port authority) that list "
        f"the designated ports of entry (clearance ports) for FOREIGN PLEASURE CRAFT / YACHTS in {name} "
        f"(VLIZ polygon mrgid={zone.get('mrgid')}, sovereign {zone.get('sovereign')}). "
        f"Do not mix in ports that belong to another exclusive economic zone of the same country. "
        f"Prefer official domains such as: {hints}. "
        f"List each official port of entry you find with its town, and cite the official URLs used."
    )
    return await grounded_search(prompt, log=log, domain_fn=domain_of)


def _looks_state_candidate(c: dict, whitelist: list[str] | None = None) -> bool:
    u = c.get("url") or ""
    if whitelist and url_allowed(u, whitelist):
        return True
    return bool(OFFICIAL_TOKENS.search(c.get("domain") or u))


def rank_candidates_ml(candidates: list[dict], log, scores_out: list | None = None,
                       whitelist: list[str] | None = None) -> list[dict]:
    """Rank URLs before download. A state domain is never dropped for a
    low ML score — only pushed back. scores_out is the log."""
    if len(candidates) < 2:
        return candidates
    try:
        from app.core.ml import predict_serp
        scored = []
        for c in candidates:
            s = (predict_serp(c.get("url") or "") or {}).get("score")
            c["score_serp"] = s
            scored.append((s, c))
    except Exception:
        return candidates
    if scores_out is not None:
        scores_out.extend({"url": c.get("url"), "domain": c.get("domain"), "score": s}
                          for s, c in scored)
    if all(s is None for s, _ in scored):
        return candidates
    scored.sort(key=lambda x: x[0] if x[0] is not None else 0.5, reverse=True)
    strong = []
    for s, c in scored:
        if s is None or s >= 0.1 or _looks_state_candidate(c, whitelist):
            strong.append(c)
    ranked = strong if len(strong) >= 3 else [c for _, c in scored]
    dropped = len(scored) - len(ranked)
    top = ", ".join(f"{c.get('domain') or '?'}={s:.2f}" for s, c in scored[:3] if s is not None)
    log(f"classifieur SERP: priorisation avant téléchargement — {top}"
        + (f" ({dropped} écarté(s), score < 0.1)" if dropped else ""))
    return ranked


# ---------------------------------------------------------------------------
# 3. Collection & parsing — hybrid N1/N2/N3 cascade (extract_core)
# ---------------------------------------------------------------------------
async def fetch_and_parse(url: str, log) -> tuple[str | None, str | None]:
    """Return (text, md5). N1/N2 cascade + local Chromium render (free).
    A blocked page (anti-bot interstitial) is invalidated, never ingested."""
    try:
        res = await extract_cascade(url, min_chars=200, log=log)
    except Exception as e:
        log(f"fetch {domain_of(url)}: échec ({type(e).__name__})")
        return None, None
    if res.get("blocked"):
        log(f"fetch {domain_of(url)}: page de blocage anti-bot — source invalidée")
        return None, None
    text = res["text"]
    if text:
        log(f"fetch {domain_of(url)}: {len(text)} chars via {res['level']} (md5 {(res['md5'] or '')[:8]}…)")
    return (text[:60000] if text else None), res["md5"]


# ---------------------------------------------------------------------------
# Zetaris validation & H-MEM integration
# ---------------------------------------------------------------------------
def zetaris_name_key(name: str | None) -> str:
    """Matching key: casefold, no accents, no apostrophes or punctuation.

    "Port of St. George's" == "Port of St. George’s"; "LUKA BRCKO" == "Luka Brčko".
    """
    return registry_check.name_key(name)


async def validate_ports_zetaris(zone: dict | str, run_id: str, settings: dict | None = None,
                                 candidates: list[dict] | None = None) -> dict:
    """Independent registry entries (UN/LOCODE + World Port Index) for the zone, via Zetaris.

    The zone resolves to a country ISO2 (its iso2, the EEZ index, or its sovereign);
    without one, a box around the candidates' coordinates is queried instead. A live
    Zetaris failure falls back to the sample fixtures (traced as zetaris_fallback). The
    client is synchronous, so it runs in a worker thread.
    Returns {mode, country_iso2, entries, registry_entries}.
    """
    registry = await asyncio.to_thread(registry_check.fetch_for_zone, zone, candidates or [], run_id)
    return {**registry, "registry_entries": len(registry["entries"])}


async def cross_reference_zetaris(ports: list[dict], zone: dict, zetaris_result: dict,
                                  source_texts: dict[str, str] | None = None) -> list[dict]:
    """Tier each port (see app.agents.registry_check): official-source evidence needs the
    fetched text of an official URL to name the port (source_texts, {url: text}); registry
    evidence is a same-country UN/LOCODE / WPI name match within 25 km. zetaris_validation
    is "official" for tiers 1-2 only. Traces one registry_check per port.
    """
    run_id = zetaris_result.get("run_id") or f"poe-{zone.get('mrgid', 'zone')}-{uuid.uuid4().hex[:8]}"
    ports, _ = await asyncio.to_thread(registry_check.annotate, ports, zetaris_result, run_id, source_texts)
    return ports


# ---------------------------------------------------------------------------
# 4. Compared PARALLEL extraction: LLM (OpenRouter) ∥ local NER (spaCy)
# ---------------------------------------------------------------------------
def _pipeline_run_id(rec, zone: dict) -> str:
    """Run id of the versioned run when recording, else a filename-safe one for traces."""
    recorder = getattr(rec, "_rec", None)
    run_id = getattr(recorder, "run_id", None)
    return run_id or f"poe-{zone.get('mrgid', 'zone')}-{uuid.uuid4().hex[:8]}"


async def extract_ports_llm(context: str, zone: dict, log, rec=None,
                            catalog_text: str | None = None,
                            settings: dict | None = None) -> list[dict]:
    """Extract once (see ``_extract_ports_core``), then cross-reference Zetaris."""
    ports = await _extract_ports_core(context, zone, log, rec=rec,
                                      catalog_text=catalog_text, settings=settings)
    run_id = _pipeline_run_id(rec, zone)
    zetaris_result = await validate_ports_zetaris(zone, run_id, settings, candidates=ports)
    # Fetched documents, as the "[SOURCE: url]" blocks of the raw collected text.
    source_texts = registry_check.source_texts_from_context(catalog_text or context)
    ports = await cross_reference_zetaris(ports, zone, {**zetaris_result, "run_id": run_id}, source_texts)
    official = [p for p in ports if p.get("zetaris_validation") == "official"]
    tiers = {t: [p["name"] for p in ports if p["registry"]["tier"] == t] for t in registry_check.TIER_LABELS}
    log(f"Zetaris registries ({zetaris_result['mode']}, {zetaris_result['country_iso2'] or 'bbox'}): "
        + ", ".join(f"{len(names)} {registry_check.TIER_LABELS[t]}" for t, names in tiers.items())
        + f" ({len(source_texts)} fetched document(s) checked for official evidence)")
    await emit(rec, "zetaris_crossref", mode=zetaris_result["mode"],
               country_iso2=zetaris_result["country_iso2"],
               registry_entries=zetaris_result["registry_entries"],
               official_count=len(official), matched=[p["name"] for p in official],
               tiers=tiers, documents_with_text=len(source_texts))
    return ports


async def _extract_ports_core(context: str, zone: dict, log, rec=None,
                              catalog_text: str | None = None,
                              settings: dict | None = None) -> list[dict]:
    """LLM and local NER run IN PARALLEL on the same context and their
    lists are compared port by port (no longer a simple fallback):
      - port seen by both → extraction_agreement=True (confidence signal);
      - port seen by the LLM only → extraction_agreement=False (needs cross-check);
      - names seen by NER only → logged as candidates to verify;
      - LLM unavailable → the NER list becomes the fallback (behavior kept).
    A real table (names + coords) short-circuits the LLM; "port de X"
    fragments are no longer enough — JSON readers parse these pages.
    Second reader: gpt-oss (or Kimi if decree) if NVIDIA is on.
    Claude only steps in after both NIM/OR readers fail.
    """
    raw_catalog = catalog_text if catalog_text is not None else context
    catalog = extract_structured_ports(raw_catalog)
    if catalog_is_sufficient(catalog, raw_catalog):
        kept = catalog_ports_to_keep(catalog)
        if kept:
            n_coord = len(catalog_ports_with_coords(catalog))
            log(f"catalogue table suffisant ({n_coord} port(s) coordonnés, "
                f"{len(kept)} retenu(s)) — LLM sauté")
            await emit(rec, "extraction_compare", llm_n=None, ner_n=None,
                       catalog_n=len(kept), fallback="catalog", skipped_llm=True)
            return kept
        log("catalogue marqué suffisant mais 0 port retenu — LLM non sauté")

    _KNOWN_ENGINES = (
        "claude", "openrouter", "catalog", "ner", "nightshift",
        "nvidia-laguna", "nvidia-muse", "nvidia-kimi", "nvidia-deepseek",
        "nvidia-gpt-oss", "nvidia-gemma", "nvidia-minimax", "nvidia-nemotron",
        "nvidia-llama", "llm",
    )

    async def _primary():
        try:
            return await extract_ports(context, zone, settings=settings, log=log), None
        except Exception as e:
            return None, e

    async def _second():
        try:
            from app.core import nvidia
            if nvidia.nvidia_enabled(settings):
                choice = nvidia.second_extract_choice(context)
                if not choice:
                    return None
                model, engine = choice
                ports = await nvidia.extract_ports_nvidia(
                    context, zone, settings=settings, log=log,
                    model=model, engine=engine, fallback=False)
                return ports
            # NVIDIA off: no Claude here (last resort after both readers).
            return None
        except Exception as e:
            if log:
                log(f"second lecteur: échec ({type(e).__name__})")
            return None

    async def _ner():
        try:
            from app.core.ml import extract_entities
            ents = await asyncio.to_thread(extract_entities, context[:20000])
            return [e["text"].strip()[:120] for e in ents if e["label"] == "PORT_NAME"]
        except Exception:
            return None  # NER unavailable (model missing) — neutral signal

    (llm_ports, llm_err), second_ports, ner_names = await asyncio.gather(
        _primary(), _second(), _ner())
    ner_names = list(dict.fromkeys(ner_names)) if ner_names is not None else None
    claude_ports = second_ports
    claude_names = [p["name"] for p in (claude_ports or []) if p.get("name")]
    second_engine = next(
        (p.get("extraction_engine") for p in (claude_ports or [])
         if p.get("extraction_engine")),
        "claude",
    )

    if llm_ports is None and claude_ports:
        log("lecteur principal indisponible — second lecteur retenu")
        llm_ports, llm_err = claude_ports, None
        claude_names = []  # already the primary list
    if llm_ports is None:
        from app.core import claude
        if claude.claude_enabled(settings) and claude.budget_allows_call(settings):
            try:
                llm_ports = await claude.extract_ports_claude(
                    context, zone, settings=settings, log=log)
                llm_err = None
                if log:
                    log(f"LLM Claude Haiku (dernier recours): {len(llm_ports)} port(s)")
            except Exception as e:
                llm_err = e
    if llm_ports is None:
        log(f"LLM: échec ({type(llm_err).__name__}: {str(llm_err)[:100]})")
        fallback = catalog_ports_to_keep(catalog)
        if fallback:
            log(f"catalogue structuré: {len(fallback)} port(s) retenus (sans LLM)")
            await emit(rec, "extraction_compare", llm_n=None,
                       ner_n=(len(ner_names) if ner_names else 0),
                       catalog_n=len(fallback), fallback="catalog",
                       llm_error=str(llm_err)[:120])
            return fallback
        if ner_names:
            ports = [{"name": n, "city": None,
                      "note": "extraction NER locale (fallback sans LLM)",
                      "extraction_engine": "ner", "extraction_agreement": None}
                     for n in ner_names][:50]
            log(f"NER local: {len(ports)} port(s) extraits en fallback (sans LLM)")
            await emit(rec, "extraction_compare", llm_n=None, ner_n=len(ner_names),
                       fallback="ner", llm_error=str(llm_err)[:120])
            return ports
        await emit(rec, "extraction_compare", llm_n=None, ner_n=0,
                   fallback="none", llm_error=str(llm_err)[:120])
        return []

    has_ner_signal = bool(ner_names)
    has_claude = bool(claude_names)
    for p in llm_ports:
        if p.get("extraction_engine") not in _KNOWN_ENGINES:
            p["extraction_engine"] = "llm"
        p["extraction_agreement"] = (
            any(text_similarity(p["name"], n) >= 0.6 for n in ner_names)
            if has_ner_signal else None
        )
        p["claude_agreement"] = (
            any(text_similarity(p["name"], n) >= 0.6 for n in claude_names)
            if has_claude else None
        )
    both = [p["name"] for p in llm_ports if p.get("extraction_agreement")]
    llm_only = [p["name"] for p in llm_ports if p.get("extraction_agreement") is False]
    ner_only = ([n for n in ner_names
                 if not any(text_similarity(n, p["name"]) >= 0.6 for p in llm_ports)]
                if has_ner_signal else [])
    claude_only = ([n for n in claude_names
                    if not any(text_similarity(n, p["name"]) >= 0.6 for p in llm_ports)]
                   if has_claude else [])
    if has_ner_signal:
        log(f"extraction comparée: {len(both)} port(s) confirmés LLM∩NER, "
            f"{len(llm_only)} LLM seul, {len(ner_only)} NER seul (candidats à vérifier)")
    if has_claude:
        log(f"second lecteur {second_engine}: "
            f"{sum(1 for p in llm_ports if p.get('claude_agreement'))} "
            f"accord(s), {len(claude_only)} nom(s) second lecteur seul")
    for n in claude_only:
        llm_ports.append({
            "name": n, "city": None,
            "note": f"vu par {second_engine} seulement (à recouper)",
            "extraction_engine": second_engine,
            "extraction_agreement": (
                any(text_similarity(n, x) >= 0.6 for x in ner_names)
                if has_ner_signal else None
            ),
            "claude_agreement": None,
        })
    engines = sorted({p.get("extraction_engine") for p in llm_ports
                      if p.get("extraction_engine")})
    await emit(rec, "extraction_compare", llm_n=len(llm_ports),
               ner_n=(len(ner_names) if ner_names is not None else None),
               both=both, llm_only=llm_only, ner_only=ner_only,
               claude_n=(len(claude_names) if has_claude else None),
               claude_only=claude_only,
               llm_engine=(engines[0] if len(engines) == 1 else engines or None))
    if catalog:
        by_coord = {
            normalize_name(c["name"]): c
            for c in catalog_ports_with_coords(catalog)
            if normalize_name(c.get("name") or "")
        }
        for p in llm_ports:
            key = normalize_name(p["name"])
            c = by_coord.get(key)
            if c and p.get("lat") is None:
                p["lat"] = c["lat"]
                p["lon"] = c["lon"]
        have = {normalize_name(p["name"]) for p in llm_ports}
        extra = []
        extra_src = (
            catalog_ports_to_keep(catalog)
            if looks_like_port_catalog(raw_catalog)
            else catalog_ports_with_coords(catalog)
        )
        for c in extra_src:
            key = normalize_name(c.get("name") or "")
            if not key or key in have:
                continue
            extra.append({**c, "extraction_agreement": None})
            have.add(key)
        if extra:
            log(f"catalogue table: {len(extra)} port(s) ajoutés "
                f"(en plus des {len(llm_ports)} du LLM)")
            await emit(rec, "catalog_extract", n=len(extra),
                       names=[c["name"] for c in extra[:20]])
            llm_ports = llm_ports + extra
    return llm_ports


_normalize_name = normalize_name  # backward compat


# ---------------------------------------------------------------------------
# Full pipeline for ONE zone — orchestrator + 5 steps
# ---------------------------------------------------------------------------
async def _skip_if_unchanged(db, zone: dict, force: bool, log, rec=None) -> dict | None:
    """Step 1 — Monitor KNOWN sources before any search.
    PARALLEL DOUBLE VERDICT: the MD5 hash and semantic similarity are
    BOTH computed and recorded for each source (no longer a cascade), then
    a decision matrix decides:
      - identical MD5                      → unchanged (skip);
      - different MD5, similarity >= 0.95  → cosmetic change (skip, recorded);
      - similarity < 0.95 or source down   → full re-extraction.
    Return the (updated) zone doc if re-extraction can be skipped."""
    mrgid = int(zone["mrgid"])
    known_hashes = zone.get("source_hashes") or {}
    if force or not known_hashes or zone.get("status") not in ("ia", "ia_sans_source") or not zone.get("poe_count", 0):
        return None
    old_excerpts = zone.get("source_excerpts") or {}
    verdicts = []
    fresh_hashes = {}
    for url in list(known_hashes.keys())[:3]:
        text, md5 = await fetch_and_parse(url, log)
        md5_same = (md5 == known_hashes.get(url)) if md5 else None
        sim = None
        if text and old_excerpts.get(url):
            sim = round(semantic_similarity(old_excerpts[url], text), 4)
        verdicts.append({"url": url, "md5_same": md5_same, "semantic_sim": sim})
        if md5:
            fresh_hashes[url] = md5
        await emit(rec, "monitoring_source", url=url, md5_known=known_hashes.get(url),
                   md5_fresh=md5, md5_same=md5_same, semantic_sim=sim,
                   chars=len(text or ""))

    from app.core.run_rules import get_rule
    unchanged_sim = float(get_rule("shared.content_changed_sim", 0.95))

    def _source_unchanged(v):
        if v["md5_same"] is True:
            return True
        return v["semantic_sim"] is not None and v["semantic_sim"] >= unchanged_sim

    reachable = [v for v in verdicts if v["md5_same"] is not None or v["semantic_sim"] is not None]
    skip = bool(reachable) and all(_source_unchanged(v) for v in verdicts)
    if skip:
        cosmetic = [v["url"] for v in verdicts if v["md5_same"] is False]
        reason = "md5" if not cosmetic else "semantic"
        log("monitoring: sources inchangées "
            + ("(MD5 identiques)" if reason == "md5"
               else f"(MD5 modifiés mais similarité >= {unchanged_sim} : changement cosmétique sur {len(cosmetic)} source(s))")
            + " — ré-extraction sautée")
        await emit(rec, "monitoring_decision", skip=True, reason=reason, verdicts=verdicts)
        await db.eez_zones.update_one({"mrgid": mrgid}, {"$set": {
            "checked_at": now_iso(),
            "source_hashes": {**known_hashes, **fresh_hashes},
            "monitoring_verdicts": verdicts,
        }})
        return await db.eez_zones.find_one({"mrgid": mrgid})
    log("contenu source modifié ou source injoignable — pipeline complet relancé")
    await emit(rec, "monitoring_decision", skip=False, reason="changed_or_unreachable",
               verdicts=verdicts)
    return None


def _normalize_url(url: str) -> str:
    """Lowercased host, no www., no fragment, trailing slash stripped (query kept)."""
    if not url or not isinstance(url, str):
        return ""
    try:
        p = urlparse(url.strip())
    except Exception:
        return url.strip().rstrip("/").lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if p.port and p.port not in (80, 443):
        host = f"{host}:{p.port}"
    path = (p.path or "").rstrip("/")
    scheme = (p.scheme or "https").lower() or "https"
    return urlunparse((scheme, host, path, "", p.query or "", ""))


def _engine_set(c: dict) -> set[str]:
    e = c.get("engine")
    if isinstance(e, list):
        return {str(x) for x in e if x}
    if e:
        return {str(e)}
    return set()


def _merge_engines(*candidates: dict) -> str | list[str]:
    engines: set[str] = set()
    for c in candidates:
        engines |= _engine_set(c)
    if not engines:
        return "searxng"
    if len(engines) == 1:
        return next(iter(engines))
    return sorted(engines)


def _merge_candidates(*groups: list[dict]) -> list[dict]:
    """Deduped union by normalized URL (several paths of a domain survive)."""
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for group in groups:
        for c in group or []:
            u = c.get("url")
            if not u:
                continue
            key = _normalize_url(u)
            if not key:
                continue
            if key in seen:
                prev = seen[key]
                prev["engine"] = _merge_engines(prev, c)
                for field in ("title", "snippet", "score_serp"):
                    if prev.get(field) in (None, "") and c.get(field) not in (None, ""):
                        prev[field] = c[field]
                continue
            item = dict(c)
            item["url"] = u
            item["domain"] = item.get("domain") or domain_of(u)
            seen[key] = item
            out.append(item)
    return out


def _url_score(c: dict) -> float:
    serp = c.get("score_serp")
    return list_url_bonus(c.get("url") or "") + (serp if serp is not None else 0.5)


def _best_per_domain(candidates: list[dict], max_per_domain: int = 3) -> list[dict]:
    """1 page + up to 2 list PDFs per domain (France lesson: list + PPF map)."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for c in candidates or []:
        d = c.get("domain") or domain_of(c.get("url") or "")
        if not d:
            continue
        if d not in groups:
            order.append(d)
            groups[d] = []
        groups[d].append(c)
    out: list[dict] = []
    for d in order:
        items = sorted(groups[d], key=_url_score, reverse=True)
        pdfs = [c for c in items if (c.get("url") or "").lower().endswith(".pdf")]
        pages = [c for c in items if not (c.get("url") or "").lower().endswith(".pdf")]
        picked: list[dict] = []
        if pages:
            picked.append(pages[0])
        list_pdfs = [c for c in pdfs if list_url_bonus(c.get("url") or "") >= 0.45]
        pool = list_pdfs or pdfs[:1]
        for c in pool:
            if len(picked) >= max_per_domain:
                break
            picked.append(c)
        if not picked:
            picked = items[:1]
        seen: set[str] = set()
        for c in picked:
            u = c.get("url") or ""
            if u and u not in seen:
                seen.add(u)
                out.append(c)
            if len(seen) >= max_per_domain:
                break
    return out


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _search_agreement(a: list[dict], b: list[dict]) -> dict:
    """SearXNG ∥ TinyFish agreement. An empty engine = outage, not disagreement."""
    urls_a = {_normalize_url(c.get("url")) for c in (a or []) if c.get("url")}
    urls_b = {_normalize_url(c.get("url")) for c in (b or []) if c.get("url")}
    urls_a.discard("")
    urls_b.discard("")
    doms_a = {(c.get("domain") or domain_of(c.get("url") or "")) for c in (a or [])}
    doms_b = {(c.get("domain") or domain_of(c.get("url") or "")) for c in (b or [])}
    doms_a.discard("")
    doms_b.discard("")
    n_a, n_b = len(urls_a), len(urls_b)
    j_urls = round(_jaccard(urls_a, urls_b), 3)
    j_doms = round(_jaccard(doms_a, doms_b), 3)
    return {
        "n_a": n_a,
        "n_b": n_b,
        "jaccard_urls": j_urls,
        "jaccard_domains": j_doms,
        "discordant": bool(n_a >= 3 and n_b >= 3 and j_doms < 0.3),
    }


async def _resolve_serper_key(serper_key: str | None = None) -> str:
    if serper_key:
        return serper_key.strip()
    from app.core.serper import serper_api_key
    key = serper_api_key()
    if key:
        return key
    try:
        from app.db import get_settings
        return serper_api_key(await get_settings())
    except Exception:
        return ""


async def _serper_search_safe(query: str, key: str, log, *, gl=None,
                              hl: str = "en") -> list[dict]:
    if not key or not query:
        return []
    from app.core.serper import serper_search
    try:
        return await serper_search(query, key, gl=gl, hl=hl, log=log)
    except Exception as e:
        log(f"Serper: échec ({type(e).__name__})")
        return []


async def _resolve_tf_key(tf_key: str | None = None) -> str:
    if tf_key:
        return tf_key.strip()
    from app.core.tinyfish import tf_api_key
    key = tf_api_key()
    if key:
        return key
    try:
        from app.db import get_settings
        return tf_api_key(await get_settings())
    except Exception:
        return ""


async def _tf_search_safe(query: str, key: str, log, *, location=None, language=None,
                          include_domains=None) -> list[dict]:
    if not key or not query:
        return []
    from app.core.tinyfish import tf_search
    from app.services.poe_seeds import SEARCH_EXCLUDE_DOMAINS
    try:
        return await tf_search(
            query, key, location=location, language=language,
            include_domains=include_domains,
            exclude_domains=SEARCH_EXCLUDE_DOMAINS, log=log)
    except Exception as e:
        log(f"TinyFish Search: échec ({type(e).__name__})")
        return []


def site_list_pdf_query(domain: str, zone: dict) -> str:
    """`site:` query once the state domain is known."""
    iso = polygon_iso2(zone)
    if is_france_mainland_eez(zone):
        return (f"site:{domain} filetype:pdf "
                f"(liste ports de plaisance OR carte PPF OR points de passage frontaliers)")
    if iso == "NZ":
        return f"site:{domain} filetype:pdf (places of first arrival OR small craft OR ports of entry)"
    if iso == "VE":
        return (f"site:{domain} filetype:pdf "
                f"(Ley de Marinas OR capitanías de puerto OR reglamento jurisdicción)")
    if iso == "NC":
        return f"site:{domain} filetype:pdf (plaisance OR formalités douanières)"
    if iso == "NU":
        return f"site:{domain} filetype:pdf (customs act OR port of entry OR niue laws)"
    if iso == "SX":
        return f"site:{domain} filetype:pdf (customs OR ports of entry OR pleasure craft)"
    if iso == "YT":
        return f"site:{domain} filetype:pdf (arrêté Mayotte points de passage OR annexe I)"
    if iso == "GB":
        return f"site:{domain} filetype:pdf (C1331 OR pleasure craft report)"
    if iso == "EG":
        return f"site:{domain} filetype:pdf (yacht tourism OR marinas OR ports of entry)"
    if iso == "AL":
        return (f"site:{domain} filetype:pdf "
                f"(akcizë OR anijet e peshkimit OR porteve detare OR kartelë)")
    lang = zone_search_lang(zone)
    if lang == "fr":
        return f"site:{domain} filetype:pdf (liste ports d'entrée OR décret OR arrêté)"
    if lang == "es":
        return f"site:{domain} filetype:pdf (puertos habilitados OR decreto lista)"
    return (f"site:{domain} filetype:pdf "
            f"(ports of entry OR yacht OR marina OR pleasure craft OR "
            f"first arrival OR akcizë OR designated)")


def site_list_page_query(domain: str, zone: dict) -> str:
    """Same hop for an HTML page (NZ MPI / VE INEA lesson: the list is not a PDF)."""
    iso = polygon_iso2(zone)
    if is_france_mainland_eez(zone):
        return f"site:{domain} (liste ports de plaisance OR vous naviguez pays non membre)"
    if iso == "NZ":
        return f"site:{domain} (places of first arrival seaports OR small craft sailing)"
    if iso == "VE":
        return (f"site:{domain} "
                f"(Ley de Marinas OR capitanías de puerto OR reglamento jurisdicción)")
    if iso == "NC":
        return f"site:{domain} (formalités douanières navires de plaisance)"
    if iso == "MX":
        return f"site:{domain} (puertos y terminales habilitados)"
    if iso == "NU":
        return f"site:{domain} (customs act port of entry)"
    if iso == "SX":
        return f"site:{domain} (customs department OR ports of entry)"
    if iso == "YT":
        return f"site:{domain} (JORFTEXT000030235682 OR arrêté points de passage Mayotte)"
    if iso == "GB":
        return f"site:{domain} (submit a pleasure craft report)"
    if iso == "EG":
        return f"site:{domain} (yacht tourism OR specialized marinas)"
    if iso == "AL":
        return f"site:{domain} (autorizim akcizë anijet e peshkimit OR porteve detare)"
    lang = zone_search_lang(zone)
    if lang == "fr":
        return f"site:{domain} (ports d'entrée OR liste douane)"
    if lang == "es":
        return f"site:{domain} (puertos habilitados OR decreto)"
    return (f"site:{domain} (ports of entry OR yacht tourism OR specialized marinas "
            f"OR pleasure craft OR first arrival OR porteve detare)")


def landing_url_candidates(zone: dict) -> list[dict]:
    """State landing page for THIS mrgid (territories.json) — crawl, do not display."""
    from app.services.territory_ref import curated_landing_urls
    out, seen = [], set()
    for rec in curated_landing_urls(zone.get("mrgid")):
        u = rec.get("url") or ""
        n = _normalize_url(u)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append({"url": u, "domain": domain_of(u)})
    return out


async def _site_list_pdf_search(domains: list[str], zone: dict, log) -> list[dict]:
    """Second hop: the state domain is known; the list is often a PDF
    outside the generic SERP (France lesson: Liste-ports-de-plaisance-eligibles.pdf)."""
    out: list[dict] = []
    for dom in domains[:3]:
        if not dom:
            continue
        q = site_list_pdf_query(dom, zone)
        res = await search_searxng(q, log)
        if res:
            log(f"site PDF {dom}: {len(res)} résultat(s)")
            out.extend(res)
    return out


async def _site_list_page_search(domains: list[str], zone: dict, log) -> list[dict]:
    """HTML hop: INEA / MPI / douane.nc expose a page, not a PDF."""
    out: list[dict] = []
    for dom in domains[:3]:
        if not dom:
            continue
        q = site_list_page_query(dom, zone)
        res = await search_searxng(q, log)
        if res:
            log(f"site page {dom}: {len(res)} résultat(s)")
            out.extend(res)
    return out


async def _find_sources(zone: dict, whitelist: list[str], exceptions: dict, log, rec=None,
                        tf_key: str | None = None, variant: str = "tinyfish",
                        ) -> tuple[list[dict], bool, str | None]:
    """Step 2 — classic round 1 then, if needed, lessons round 2.
    v1 = sequential SearXNG (EN then localized if empty);
    v2 = parallel EN ∥ local SearXNG, no TinyFish;
    tinyfish = SearXNG ∥ TinyFish + include_domains net.
    The if iso of the 11 polygons stay on hints / site: / localized_query.
    Serper (short phrase + TLD) runs in parallel with round 1.
    :online last. Never touches poe_ports."""
    name = search_polygon_name(zone)
    place = serp_place_name(zone) or name
    variant = normalize_variant(variant)
    use_tf = variant == "tinyfish"
    key = (await _resolve_tf_key(tf_key)) if use_tf else ""
    serper_key = await _resolve_serper_key()
    iso = zone_search_location(zone)
    lang = zone_search_lang(zone)

    query_en = (f"official designated ports of entry list customs gazette "
                f"decree legislation {place}")
    loc_q = localized_query(zone)

    searx_groups, tf_groups, serper_groups = [], [], []

    def _add_serper_jobs(jobs: list, labels: list) -> None:
        # The shots are always scheduled. `_serper_search_safe` returns []
        # without calling Serper when the key is empty, so a machine without
        # SERPER_API_KEY still builds the google-style queries and merges
        # nothing. A hermetic test can replace that function and see them.
        for qlang, q in google_style_shots(zone):
            jobs.append(_serper_search_safe(q, serper_key, log, gl=iso, hl=qlang))
            labels.append(("serper", qlang, q))

    if variant == "v1":
        jobs = [search_searxng(query_en, log)]
        labels = [("searxng", "en", query_en)]
        _add_serper_jobs(jobs, labels)
        results = await asyncio.gather(*jobs)
        res_en: list = []
        for res, (engine, qlang, query) in zip(results, labels):
            rnd = "google_style" if engine == "serper" else "classic"
            await emit(rec, "search", engine=engine, lang=qlang, query=query,
                       n=len(res or []), results=res or [], variant=variant,
                       round=rnd)
            if engine == "serper":
                serper_groups.append(res or [])
            else:
                res_en = res or []
                searx_groups.append(res_en)
        if not res_en and loc_q:
            log(f"v1: SearXNG EN vide — bascule localisée ({lang}): {loc_q[:80]}")
            res_loc = await search_searxng(loc_q, log)
            await emit(rec, "search", engine="searxng", lang=lang, query=loc_q,
                       n=len(res_loc or []), results=res_loc or [], variant=variant,
                       round="classic")
            searx_groups.append(res_loc or [])
    else:
        jobs = [search_searxng(query_en, log)]
        labels = [("searxng", "en", query_en)]
        if key:
            jobs.append(_tf_search_safe(query_en, key, log, location=iso, language="en"))
            labels.append(("tinyfish", "en", query_en))
        if loc_q:
            log(f"recherche parallèle EN ∥ localisée ({lang}): {loc_q[:80]}")
            jobs.append(search_searxng(loc_q, log))
            labels.append(("searxng", lang, loc_q))
            if key:
                jobs.append(_tf_search_safe(loc_q, key, log, location=iso, language=lang))
                labels.append(("tinyfish", lang, loc_q))
        _add_serper_jobs(jobs, labels)

        results = await asyncio.gather(*jobs)
        for res, (engine, qlang, query) in zip(results, labels):
            rnd = "google_style" if engine == "serper" else "classic"
            await emit(rec, "search", engine=engine, lang=qlang, query=query,
                       n=len(res or []), results=res or [], variant=variant,
                       round=rnd)
            if engine == "tinyfish":
                tf_groups.append(res or [])
            elif engine == "serper":
                serper_groups.append(res or [])
            else:
                searx_groups.append(res or [])

    searx_set = _merge_candidates(*searx_groups)
    tf_set = _merge_candidates(*tf_groups)
    compare = _search_agreement(searx_set, tf_set)
    await emit(rec, "search_compare", **compare)
    if compare["discordant"]:
        log(f"search_compare: discordance Jaccard domaines={compare['jaccard_domains']} "
            f"(n_searx={compare['n_a']} n_tf={compare['n_b']})")

    serper_set = _merge_candidates(*serper_groups)
    candidates = _merge_candidates(searx_set, tf_set, serper_set)
    if candidates:
        log(f"union des recherches: {compare['n_a']} SearXNG + {compare['n_b']} TinyFish "
            f"+ {len(serper_set)} Serper → {len(candidates)} candidats")

    hints = search_hint_queries(zone, exceptions)
    if hints:
        hint_res = await asyncio.gather(*[search_searxng(q, log) for q in hints])
        for q, res in zip(hints, hint_res):
            await emit(rec, "search", engine="searxng", lang="hint", query=q,
                       n=len(res), results=res, round="classic")
        candidates = _merge_candidates(candidates, *hint_res)
        log(f"requêtes épinglées: {len(hints)} → +{sum(len(r) for r in hint_res)} candidats")

    synthesis = None
    grounded_attempted = False

    protect = (lambda u: url_allowed(u, whitelist)
               or bool(OFFICIAL_TOKENS.search(u or "")))
    before_filter = list(candidates)
    audit = audit_serp_filter(before_filter, protect_fn=protect)
    candidates = serp_filter(candidates, protect_fn=protect)
    dropped_filter = [a["url"] for a in audit if not a["kept"]]
    if dropped_filter:
        await emit(rec, "serp_filter", kept=len(candidates), dropped=dropped_filter,
                   audit=audit)
    scores: list = []
    candidates = rank_candidates_ml(candidates, log, scores_out=scores,
                                    whitelist=whitelist)
    if scores:
        await emit(rec, "serp_rank", scores=scores)

    official = [c for c in candidates if url_allowed(c["url"], whitelist)]

    if key and whitelist and (not official or compare.get("discordant")):
        scoped = await _tf_search_safe(
            query_en, key, log, location=iso, language="en",
            include_domains=whitelist[:8])
        await emit(rec, "search", engine="tinyfish", lang="en", query=query_en,
                   n=len(scoped), results=scoped, scoped=True, round="classic")
        if scoped:
            log(f"TinyFish Search scoped: {len(scoped)} résultat(s) include_domains")
            candidates = _merge_candidates(candidates, scoped)
            before_s = list(candidates)
            audit_s = audit_serp_filter(before_s, protect_fn=protect)
            candidates = serp_filter(candidates, protect_fn=protect)
            dropped_s = [a["url"] for a in audit_s if not a["kept"]]
            if dropped_s:
                await emit(rec, "serp_filter", kept=len(candidates), dropped=dropped_s,
                           scoped=True, audit=audit_s)
            scores_s: list = []
            candidates = rank_candidates_ml(candidates, log, scores_out=scores_s,
                                            whitelist=whitelist)
            if scores_s:
                await emit(rec, "serp_rank", scores=scores_s, scoped=True)
            official = [c for c in candidates if url_allowed(c["url"], whitelist)]
    elif key and not whitelist and (not official or compare.get("discordant")):
        log("TinyFish scoped sauté : whitelist vide pour cette ZEE")

    seeded_preview = seed_url_candidates(zone, exceptions) + landing_url_candidates(zone)
    if needs_lessons_round(official + seeded_preview):
        q2 = lessons_learned_query(zone)
        log(f"round 2 leçons: {q2[:90]}")
        lesson_jobs = [search_searxng(q2, log)]
        lesson_labels = [("searxng", lang or "en", q2)]
        if key:
            lesson_jobs.append(_tf_search_safe(
                q2, key, log, location=iso, language="en"))
            lesson_labels.append(("tinyfish", "en", q2))
        lesson_parts = await asyncio.gather(*lesson_jobs)
        extra_groups = []
        for res, (engine, qlang, query) in zip(lesson_parts, lesson_labels):
            await emit(rec, "search", engine=engine, lang=qlang, query=query,
                       n=len(res or []), results=res or [], variant=variant,
                       round="lessons")
            extra_groups.append(res or [])
        if key and whitelist:
            scoped2 = await _tf_search_safe(
                q2, key, log, location=iso, language="en",
                include_domains=whitelist[:8])
            await emit(rec, "search", engine="tinyfish", lang="en", query=q2,
                       n=len(scoped2 or []), results=scoped2 or [],
                       scoped=True, variant=variant, round="lessons")
            extra_groups.append(scoped2 or [])
        extra = _merge_candidates(*extra_groups)
        known_urls = {_normalize_url(c.get("url")) for c in candidates}
        extra = serp_filter(
            [c for c in extra if _normalize_url(c.get("url") or "") not in known_urls],
            protect_fn=protect)
        if extra:
            log(f"round 2 leçons: {len(extra)} source(s) supplémentaires")
            candidates = _merge_candidates(candidates, extra)
            candidates = rank_candidates_ml(candidates, log, whitelist=whitelist)
        official = [c for c in candidates if url_allowed(c.get("url") or "", whitelist)]

    if not candidates and not grounded_attempted:
        candidates, synthesis = await search_grounded(zone, whitelist, log)
        grounded_attempted = True
        await emit(rec, "search", engine="grounded", lang="en", query="(prompt groundé)",
                   n=len(candidates), results=candidates,
                   synthesis_chars=len(synthesis or ""), round="lessons")
        if candidates:
            candidates = serp_filter(candidates, protect_fn=protect)
            candidates = rank_candidates_ml(candidates, log, whitelist=whitelist)
            official = [c for c in candidates if url_allowed(c.get("url") or "", whitelist)]

    # Bootstrapping: state domain even outside .{iso2} (SX .org lesson).
    rejected = [c for c in candidates if c not in official]
    if not official and rejected:
        boot = bootstrap_national_hits(rejected, zone)
        if boot:
            auto = exceptions.setdefault("auto", {}).setdefault(
                (zone.get("iso2") or "").upper(), [])
            for c in boot:
                if c["domain"] not in auto:
                    auto.append(c["domain"])
            save_exceptions(exceptions)
            log(f"bootstrapping: {[c['domain'] for c in boot]} ajoutés à exceptions.json (auto)")
            await emit(rec, "bootstrap", domains=[c["domain"] for c in boot])
            official = boot

    domains: list[str] = []
    for c in official:
        d = (c.get("domain") or domain_of(c.get("url") or "")).lower()
        if d and d not in domains:
            domains.append(d)
    if official and not any(
        (c.get("url") or "").lower().endswith(".pdf")
        and list_url_bonus(c.get("url") or "") >= 0.5
        for c in official
    ):
        extra_pdfs = await _site_list_pdf_search(domains, zone, log)
        await emit(rec, "search", engine="searxng", lang="site-pdf",
                   query="site:domain filetype:pdf", n=len(extra_pdfs),
                   results=extra_pdfs, domains=domains[:3],
                   round="lessons" if needs_lessons_round(official) else "classic")
        if extra_pdfs:
            extra_pdfs = serp_filter(extra_pdfs, protect_fn=protect)
            candidates = _merge_candidates(candidates, extra_pdfs)
            official = [c for c in candidates if url_allowed(c.get("url") or "", whitelist)]
            log(f"site PDF: {len(extra_pdfs)} candidat(s) sur {domains[:3]}")
    if official and not any(list_url_bonus(c.get("url") or "") >= 0.45 for c in official):
        extra_pages = await _site_list_page_search(domains, zone, log)
        await emit(rec, "search", engine="searxng", lang="site-page",
                   query="site:domain liste", n=len(extra_pages),
                   results=extra_pages, domains=domains[:3],
                   round="lessons" if needs_lessons_round(official) else "classic")
        if extra_pages:
            extra_pages = serp_filter(extra_pages, protect_fn=protect)
            candidates = _merge_candidates(candidates, extra_pages)
            official = [c for c in candidates if url_allowed(c.get("url") or "", whitelist)]
            log(f"site page: {len(extra_pages)} candidat(s) sur {domains[:3]}")

    seeds = seed_url_candidates(zone, exceptions)
    landings = landing_url_candidates(zone)
    pinned = []
    pinned_norm: set[str] = set()
    for c in seeds + landings:
        n = _normalize_url(c.get("url") or "")
        if not n or n in pinned_norm:
            continue
        pinned_norm.add(n)
        pinned.append(c)
    blocked = set(review_blacklist_urls(zone, exceptions))
    if blocked:
        official = [c for c in official
                    if _normalize_url(c.get("url") or "") not in blocked]
        candidates = [c for c in candidates
                      if _normalize_url(c.get("url") or "") not in blocked]
    if pinned:
        rest = _best_per_domain([
            c for c in official
            if _normalize_url(c.get("url") or "") not in pinned_norm
        ])
        official = pinned + rest
        log(f"sources épinglées: {[c['url'] for c in pinned]}")
    else:
        official = _best_per_domain(official)
    strictly_official = bool(official)
    cc_low = (zone.get("iso2") or "").lower()
    if not official:
        # National fallback: the whitelist is a hint, not a gate.
        # Government sites of other countries stay excluded.
        cc_ok = {c for c in (cc_low, (zone.get("sov_iso2") or "").lower()) if c}

        national = [c for c in rejected if cc_low and c["domain"].endswith("." + cc_low)]
        official = (national or [c for c in rejected if not is_foreign_gov_domain(c["domain"], cc_ok)])[:2]
        official = _best_per_domain(official)
        log(f"gatekeeper: 0 domaine whitelisté — {len(official)} source(s) nationales retenues "
            f"(confiance plus basse, pas un tampon de zone)")
    else:
        log(f"gatekeeper: {len(official)} source(s) officielles retenues, {len(rejected)} rejetées")
    for c in official:
        c["official"] = url_allowed(c.get("url") or "", whitelist) or bool(
            OFFICIAL_TOKENS.search(c.get("domain") or ""))
    await emit(rec, "gatekeeper", whitelist=whitelist[:12],
               official=[c["domain"] for c in official],
               rejected=[c["domain"] for c in rejected],
               strictly_official=strictly_official)
    return official, strictly_official, synthesis


async def _collect_texts(official: list[dict], log, rec=None, max_fetch: int = 8
                         ) -> tuple[list[str], dict, list[dict], dict]:
    """Step 3 — Collection: N1∥N2 cascade, anti-bot mirror, official attached PDFs
    (even if the page is already long), depth-2 if the text is thin.
    Attachments go before the rest of the SERP queue (France lesson: list + map)."""
    texts, hashes, used_sources, excerpts = [], {}, [], {}
    queue = list(official)
    seen_urls: set[str] = set()
    fetched = 0
    while queue and fetched < max_fetch:
        c = queue.pop(0)
        url = c.get("url") or ""
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        fetched += 1
        try:
            res = await extract_cascade(url, min_chars=200, log=log)
        except Exception as e:
            log(f"fetch {c.get('domain') or domain_of(url)}: échec ({type(e).__name__})")
            await emit(rec, "fetch", url=url, domain=c.get("domain"),
                       level="failed", error=type(e).__name__)
            continue
        await emit(rec, "fetch", url=url, domain=c.get("domain"), level=res["level"],
                   chars=len(res["text"]), md5=res["md5"], blocked=res.get("blocked", False),
                   render_used=res.get("render_used", False), parse=res.get("parse"))
        if res.get("fetch_compare"):
            await emit(rec, "fetch_compare", url=url, domain=c.get("domain"),
                       **res["fetch_compare"])
        if res.get("blocked"):
            log(f"fetch {c.get('domain')}: page de blocage anti-bot — source écartée")
            continue
        if res["md5"]:
            hashes[url] = res["md5"]
        text = res["text"]
        log(f"fetch {c.get('domain') or domain_of(url)}: {len(text)} chars via {res['level']}")
        blob = (res.get("html") or "") + "\n" + (text or "")
        # PDF hrefs are in the HTML; extracted text (trafilatura) loses them
        # (France lesson: list + PPF map linked from vous-naviguez).
        if should_follow_attachments(blob, url) or official_attachments(blob, url, limit=1):
            extra = []
            for att in official_attachments(blob, url, limit=4):
                if att not in seen_urls and att not in {q.get("url") for q in queue}:
                    log(f"pièce jointe officielle: {att[:90]}")
                    await emit(rec, "attachment", parent=url, url=att)
                    extra.append({"url": att, "domain": domain_of(att)})
            if extra:
                queue[0:0] = extra
        if (res.get("html") and not (url or "").lower().endswith(".pdf")
                and not looks_like_port_catalog(text or "")):
            for fu in internal_followups(res["html"], url, limit=3):
                try:
                    sub = await extract_cascade(fu, min_chars=200, log=log)
                except Exception:
                    continue
                if sub["text"] and not sub.get("blocked"):
                    log(f"depth-2: {fu[:80]} → {len(sub['text'])} chars")
                    await emit(rec, "depth2", parent=url, url=fu,
                               chars=len(sub["text"]), level=sub["level"])
                    text = (text + "\n" + sub["text"]).strip()
        keep_short_pdf = (
            (url or "").lower().endswith(".pdf")
            and list_url_bonus(url) >= 0.45
        )
        if (text and len(text) > 200) or keep_short_pdf:
            texts.append(f"[SOURCE: {url}]\n{(text or '')[:60000]}")
            excerpts[url] = (text or "")[:2500]
            used_sources.append({"url": url, "domain": c.get("domain") or domain_of(url),
                                 "md5": hashes.get(url), "collected_at": now_iso(),
                                 "official": bool(c.get("official")),
                                 "sha256": evidence_store.sha256_text(text or ""),
                                 "level": res.get("level")})
            await emit(rec, "source_used", url=url, domain=c.get("domain"),
                       chars=len(text or ""))
    return texts, hashes, used_sources, excerpts


def _spatial_cand(lat, lon, source, geom, prepared, inland=None) -> dict:
    """Geocoded candidate + EEZ / shore / river classification (no 300 km buffer)."""
    cand = {"source": source, "lat": float(lat), "lon": float(lon),
            "validated": False, "dist_km": None, "kind": "unknown"}
    if prepared is None and geom is None:
        return cand
    sp = classify_poe_point(float(lat), float(lon), geom, prepared, inland=inland)
    cand.update(sp)
    return cand


def _pick_from_cands(row: dict, picks: dict) -> tuple[dict | None, str | None]:
    """Pick a candidate: table/source, dual agreement, Haiku tie-break, EEZ."""
    if row.get("hint") in ("not_geocodeable", "geocode_deferred"):
        return None, row["hint"]
    cands = row["cands"]
    geo = row["geo"]
    if not cands:
        return None, None
    if len(cands) == 1:
        src = cands[0].get("source")
        return cands[0], "llm" if src == "llm" else "single"
    if geo.get("agree"):
        nom = next((c for c in cands if c["source"] == "nominatim"), cands[0])
        return nom, "agree"
    pname = row["port"]["name"]
    choice = picks.get(pname) or picks.get(pname.casefold())
    if choice in ("nominatim", "geonames"):
        chosen = next((c for c in cands if c["source"] == choice), None)
        if chosen:
            return chosen, "haiku_tiebreak"
    if choice == "none":
        return None, "haiku_none"
    valid = [c for c in cands if c["validated"]]
    if valid:
        return valid[0], "eez"
    return cands[0], "first"


def _apply_spatial(chosen, arbitration, cands, port_name, log):
    """After the Haiku tie-break: keep only EEZ / shore / river.

    Without zone geometry (`kind=unknown`), do not reject — unit tests
    and zones without a polygon keep the chosen point.
    """
    if not chosen:
        return chosen, arbitration
    if chosen.get("validated") or chosen.get("kind") in (None, "unknown"):
        return chosen, arbitration
    other = next((c for c in cands if c is not chosen and c.get("validated")), None)
    if other:
        log(f"  ⚠ {port_name}: {chosen['source']} hors zone ({chosen.get('kind')}) "
            f"— bascule sur {other['source']}")
        return other, "spatial_switch"
    log(f"  ⚠ {port_name}: hors ZEE, hors bord terrestre "
        f"et hors exception rivière "
        f"({chosen.get('kind')}, {chosen.get('dist_km')} km) — coordonnées rejetées")
    return None, "spatial_rejected"


async def _haiku_tiebreak_zone(zone, rows, settings, log, rec) -> dict:
    """One Haiku call for every Nominatim ≠ GeoNames disagreement in the zone."""
    items = []
    for row in rows:
        nom = next((c for c in row["cands"] if c["source"] == "nominatim"), None)
        geon = next((c for c in row["cands"] if c["source"] == "geonames"), None)
        if not nom or not geon:
            continue
        items.append({
            "name": row["port"]["name"],
            "nominatim": [nom["lat"], nom["lon"]],
            "geonames": [geon["lat"], geon["lon"]],
        })
    if not items:
        return {}
    try:
        from app.core.llm import arbitrate_geocode
        picks = await arbitrate_geocode(zone, items, settings=settings, log=log)
    except Exception as e:
        log(f"départage GPS: échec ({type(e).__name__}: {str(e)[:80]}) — repli EEZ")
        return {}
    await emit(rec, "geocode_tiebreak", n=len(items), picks=picks)
    return picks or {}


# ---------------------------------------------------------------------------
# H-MEM: prior verifications as evidence, outcomes remembered after scoring
# ---------------------------------------------------------------------------
PASS_CONFIDENCE = 60  # same threshold as EvaluatorAgent: pass above 60, else flag


def _hmem_recall(docs: list[dict], run_id: str) -> list[list[dict]]:
    """Prior verifications per port, through memory.py's locked engine (traced)."""
    from app.agents.memory import recall_node
    state = recall_node({"run_id": run_id, "ports": [
        {"name": d["name"], "zone_name": d.get("zone_name")} for d in docs]})
    return [p.get("memory") or [] for p in state["ports"]]


def _hmem_remember(docs: list[dict], run_id: str) -> dict:
    """Remember each scored port: reinforce an unchanged verdict, supersede a flip (traced)."""
    from app.agents.evaluator import _reasons_en
    from app.agents.memory import remember_node
    state = remember_node({"run_id": run_id, "ports": [{
        "name": d["name"], "zone_name": d.get("zone_name"),
        "verdict": "pass" if (d.get("confidence") or 0) > PASS_CONFIDENCE else "flag",
        "source_urls": d.get("source_urls") or [],
        "reasons_en": _reasons_en(d.get("confidence_reasons") or []),
    } for d in docs]})
    outcomes: dict = {}
    for entry in state["trace"]:
        if entry.get("action") == "remember":
            outcomes[entry["outcome"]] = outcomes.get(entry["outcome"], 0) + 1
    return outcomes


async def _extract_and_geocode(zone: dict, context: str, used_sources: list[dict], log,
                               rec=None, run=None, catalog_text: str | None = None,
                               settings: dict | None = None) -> list[dict]:
    """Step 4 — Parallel extraction (NIM → OR → Claude ∥ NER) then geocoding.

    Coords already in the source (table or LLM copy) → no Nominatim.
    Non-toponym name → no geocode. Dual disagreement → NIM / OR / Claude.
    A point is kept only if it is in this EEZ, on its land shore
    (≤ 15 km), or — river exception — a port of THIS country within ≤ 400 km.
    Silent directories → ``llm_geocode_port`` (not Project ``llm_geocode``),
    then the same VLIZ test. No 300 km buffer, no snap_to_ocean.
    """
    mrgid = int(zone["mrgid"])
    name = zone.get("name") or zone.get("geoname")
    ports = await extract_ports_llm(context, zone, log, rec=rec,
                                   catalog_text=catalog_text, settings=settings)
    listing_ports: list[dict] = []
    try:
        from app.services.listing_ref import listing_ports_for_mrgid, project_listing
        listing_ports = listing_ports_for_mrgid(project_listing()["ports"], mrgid)
    except Exception:
        listing_ports = []
    official_source = any(s.get("official") for s in used_sources)
    # Official list without GPS (PPF, MPI…): Nominatim 1.1 s × N exceeds
    # the zone timeout (FR: 52 names → TimeoutError, 0 ports persisted).
    defer_geocode = (
        len(ports) >= 8
        and all(
            p.get("extraction_engine") == "catalog" and p.get("lat") is None
            for p in ports
        )
    )
    if defer_geocode:
        log(f"catalogue sans coords ({len(ports)} noms) — géocodage différé, "
            f"noms conservés")

    geom = None
    try:
        geom = await asyncio.to_thread(shape, zone["geometry"])
        prepared = prep(geom)
    except Exception:
        prepared = None

    rows = []
    seen_names = set()
    for p in ports:
        norm = normalize_name(p["name"])
        if not norm or norm in seen_names:
            continue
        seen_names.add(norm)
        role = listing_role(p.get("name") or "", listing_ports)
        port_ev = {**p, "listing_role": role}
        empty_geo = {"nominatim": None, "geonames": None, "agree": None,
                     "agreement_km": None, "geonames_available": True,
                     "nominatim_meta": {}, "geonames_meta": {}}
        if p.get("lat") is not None and p.get("lon") is not None:
            src = "official_list" if p.get("extraction_engine") == "catalog" else "source"
            inland = inland_exception_flags(port_ev, zone, official_list=True)
            rows.append({
                "port": p, "norm": norm, "geo": empty_geo, "hint": None,
                "cands": [_spatial_cand(p["lat"], p["lon"], src, geom, prepared, inland)],
            })
            continue
        if not is_geocodeable_name(p["name"], p.get("geocodeable")):
            log(f"  ⚓ {p['name']} → géocode sauté (nom non toponyme)")
            rows.append({
                "port": p, "norm": norm, "geo": empty_geo, "hint": "not_geocodeable",
                "cands": [],
            })
            continue
        if defer_geocode:
            rows.append({
                "port": p, "norm": norm, "geo": empty_geo, "hint": "geocode_deferred",
                "cands": [],
            })
            continue
        geo = await geocode_port_dual(p, zone, log)
        cands = []
        for source in ("nominatim", "geonames"):
            coords = geo.get(source)
            if not coords:
                continue
            meta = geo.get(f"{source}_meta") or {}
            inland = inland_exception_flags(port_ev, zone, meta)
            cands.append(_spatial_cand(
                coords[0], coords[1], source, geom, prepared, inland))
        rows.append({"port": p, "norm": norm, "geo": geo, "hint": None,
                     "cands": cands, "port_ev": port_ev})

    for row in rows:
        if row.get("hint") or row["cands"]:
            continue
        if row["geo"].get("nominatim") or row["geo"].get("geonames"):
            continue
        xy = await llm_geocode_port(
            row["port"], zone, settings=settings, log=log)
        if not xy:
            continue
        inland = inland_exception_flags(
            row.get("port_ev") or row["port"], zone, {},
            official_list=official_source)
        row["cands"].append(_spatial_cand(
            xy[0], xy[1], "llm", geom, prepared, inland))
        await emit(rec, "geocode_llm", port=row["port"]["name"],
                   lat=xy[0], lon=xy[1])

    disagreements = [
        r for r in rows
        if r["geo"].get("agree") is False
        and sum(1 for c in r["cands"] if c["source"] in ("nominatim", "geonames")) >= 2
    ]
    picks = {}
    if disagreements:
        picks = await _haiku_tiebreak_zone(zone, disagreements, settings, log, rec)

    docs = []
    built = []  # ports kept in this polygon, scored after the H-MEM recall below
    for row in rows:
        p, norm, geo, cands = row["port"], row["norm"], row["geo"], row["cands"]
        chosen, arbitration = _pick_from_cands(row, picks)
        chosen, arbitration = _apply_spatial(chosen, arbitration, cands, p["name"], log)
        if (arbitration == "spatial_rejected"
                and cands and all(c.get("source") == "llm" for c in cands)):
            # Invented outside THIS polygon: keep the name, not the GPS.
            chosen, arbitration = None, "llm_spatial_rejected"

        lat = chosen["lat"] if chosen else None
        lon = chosen["lon"] if chosen else None
        source = chosen["source"] if chosen else None
        validated = bool(chosen["validated"]) if chosen else False
        dist_km = chosen["dist_km"] if chosen else None
        spatial_kind = chosen.get("kind") if chosen else "rejected"

        if not keep_extracted_in_zone(arbitration):
            log(f"  ⚓ {p['name']} → hors de ce polygone VLIZ (mrgid {mrgid}) — non rattaché")
            await emit(rec, "geocode", port=p["name"],
                       nominatim=geo.get("nominatim"), geonames=geo.get("geonames"),
                       geonames_available=geo.get("geonames_available"),
                       agreement_km=geo.get("agreement_km"), agree=geo.get("agree"),
                       candidates=cands, chosen=source, arbitration=arbitration,
                       spatial_kind=spatial_kind, kept=False)
            continue

        await emit(rec, "geocode", port=p["name"],
                   nominatim=geo.get("nominatim"), geonames=geo.get("geonames"),
                   geonames_available=geo.get("geonames_available"),
                   agreement_km=geo.get("agreement_km"), agree=geo.get("agree"),
                   candidates=cands, chosen=source, arbitration=arbitration,
                   spatial_kind=spatial_kind)

        doc = {
            "_id": str(uuid.uuid4()),
            "mrgid": mrgid,
            "zone_name": name,
            "country_iso2": zone.get("iso2") or zone.get("sov_iso2"),
            "name": p["name"], "city": p.get("city"), "note": p.get("note"),
            "lat": lat, "lon": lon, "geocode_source": source,
            "validated": validated, "distance_km": dist_km,
            "spatial_kind": spatial_kind,
            "geocode_agree": geo.get("agree"),
            "geocode_agreement_km": geo.get("agreement_km"),
            "geocode_alternates": {"nominatim": geo.get("nominatim"),
                                   "geonames": geo.get("geonames")},
            "geocode_arbitration": arbitration,
            "extraction_engine": p.get("extraction_engine"),
            "extraction_agreement": p.get("extraction_agreement"),
            "claude_agreement": p.get("claude_agreement"),
            "source_urls": [s["url"] for s in used_sources],
            "extracted_at": now_iso(),
            "dedup_key": f"{mrgid}:{norm}",
        }
        if run is not None:
            doc["run_id"] = run.run_id
            doc["pipeline_variant"] = getattr(run, "variant", None)
        built.append((doc, p, geo, lat, lon, validated, dist_km, spatial_kind, arbitration))

    # H-MEM before scoring: prior verifications of these ports, as evidence.
    run_id = run.run_id if run is not None else f"poe-{mrgid}-{uuid.uuid4().hex[:8]}"
    memories = [[] for _ in built]
    if built:
        try:
            memories = await asyncio.to_thread(_hmem_recall, [b[0] for b in built], run_id)
        except Exception as e:
            log(f"H-MEM recall: {type(e).__name__}: {str(e)[:100]}")
        await emit(rec, "hmem_recall", ports=len(built), memories=sum(len(m) for m in memories))

    for (doc, p, geo, lat, lon, validated, dist_km, spatial_kind, arbitration), recalled in zip(built, memories):
        port_official = official_source
        if not official_source:
            # Weak port (no State source in this zone's sources): reuse the official
            # source a previous verification of this same port recorded.
            from app.pipeline.run_pipeline import remembered_official_sources
            fresh = [(u, mid) for u, mid in remembered_official_sources(recalled) if u not in doc["source_urls"]]
            if fresh:
                url, memory_id = fresh[0]
                doc["source_urls"] = doc["source_urls"] + [url]
                doc["source_provenance"] = {url: "hmem"}
                doc["hmem_memory_id"] = memory_id
                port_official = True
                log(f"  H-MEM: {p['name']} → source officielle mémorisée réutilisée ({url})")
                await emit(rec, "hmem_source_reused", port=p["name"], url=url, memory_id=memory_id)
        doc["memories_recalled"] = len(recalled)
        apply_score(doc, score_port(doc, official_source=port_official,
                                    listing_ports=listing_ports))
        docs.append(doc)
        agree_note = ""
        if geo.get("agree") is True:
            agree_note = f" ✓ accord géocodeurs ({geo.get('agreement_km')} km)"
        elif geo.get("agree") is False:
            agree_note = f" ⚠ désaccord géocodeurs ({geo.get('agreement_km')} km, arbitrage {arbitration})"
        kind_note = ""
        if spatial_kind == "in_eez":
            kind_note = " ✓ ZEE"
        elif spatial_kind == "coastal_land":
            kind_note = " ✓ bord terrestre"
        elif spatial_kind == "inland_river":
            kind_note = " ✓ exception rivière"
        elif lat is not None:
            kind_note = f" ⚠ {spatial_kind} ({dist_km} km)"
        log(f"  ⚓ {p['name']} → {'%.3f, %.3f' % (lat, lon) if lat is not None else 'non géocodé'}"
            f"{kind_note}  confiance {doc.get('confidence')} ({doc.get('confidence_level')})"
            + agree_note)
        await emit(rec, "port", name=p["name"], city=p.get("city"), lat=lat, lon=lon,
                   validated=validated, distance_km=dist_km,
                   spatial_kind=spatial_kind, confidence=doc.get("confidence"),
                   confidence_level=doc.get("confidence_level"),
                   extraction_agreement=p.get("extraction_agreement"),
                   geocode_agree=geo.get("agree"))

    # H-MEM after scoring: remember each outcome (reinforce unchanged, supersede flips).
    if docs:
        try:
            outcomes = await asyncio.to_thread(_hmem_remember, docs, run_id)
            log(f"H-MEM: {outcomes}")
            await emit(rec, "hmem_remember", outcomes=outcomes)
        except Exception as e:
            log(f"H-MEM remember: {type(e).__name__}: {str(e)[:100]}")
    return docs


async def _persist_zone_run(db, zone: dict, docs: list[dict], status: str,
                            used_sources: list[dict], hashes: dict, run, rec, log) -> dict:
    """Step 5 (versioned-run mode) — Write in the RUN SPACE only:
    poe_run_ports / poe_run_zones. v1 collections (poe_ports, eez_zones) are
    never touched. Idempotent by (run_id, mrgid): regenerating the zone
    in the same run replaces its ports."""
    mrgid = int(zone["mrgid"])
    docs = deduplicate_list(docs, title_key="name") if docs else []
    ports_coll = db[run.ports_coll]
    await ports_coll.delete_many({"run_id": run.run_id, "mrgid": mrgid})
    if docs:
        await ports_coll.insert_many(docs)
    zone_doc = {
        "run_id": run.run_id,
        "mrgid": mrgid,
        "name": zone.get("name"),
        "geoname": zone.get("geoname"),
        "iso2": zone.get("iso2"),
        "sov_iso2": zone.get("sov_iso2"),
        "sovereign": zone.get("sovereign"),
        "status": status if docs else "erreur",
        "poe_count": len(docs),
        "sources": used_sources,
        "source_hashes": hashes,
        "generated_at": now_iso(),
        "last_error": None if docs else "aucun port d'entrée extrait des sources",
        "pipeline_variant": getattr(run, "variant", None),
        "sources_official": bool(any(s.get("official") for s in used_sources)),
        "confidence_avg": zone_confidence_avg(docs),
    }
    await db[run.zones_coll].update_one(
        {"run_id": run.run_id, "mrgid": mrgid}, {"$set": zone_doc}, upsert=True)
    await emit(rec, "persist", inserted=len(docs), status=zone_doc["status"],
               run_id=run.run_id)
    log(f"terminé (run {run.run_id[:8]}): {len(docs)} PoE écrits dans l'espace du run, "
        f"statut {zone_doc['status']}")
    return zone_doc


async def _persist_zone(db, zone: dict, docs: list[dict], status: str,
                        used_sources: list[dict], hashes: dict, excerpts: dict, log) -> dict:
    """Step 5 — Non-destructive write: internal dedup, port upsert
    (Bottom-Up enrichments are preserved), zone status."""
    mrgid = int(zone["mrgid"])
    if docs:
        # Internal dedup (core.dedup: Haversine <500m + fuzzy >60%)
        docs = deduplicate_list(docs, title_key="name")
        # NON-DESTRUCTIVE upsert: existing PoEs are enriched, never purged
        # (preserves Bottom-Up fields: osm_confidence, spatial_anomaly…).
        existing_ports = await db.poe_ports.find({"mrgid": mrgid}).to_list(500)
        inserted = merged = 0
        for d in docs:
            match = next((e for e in existing_ports if e.get("dedup_key") == d["dedup_key"]), None)
            if match is None:
                match = find_duplicate_in_list(d, existing_ports, title_key="name")
            if match:
                updates = merge_docs(match, d)
                updates["note"] = d.get("note") or match.get("note")
                updates["source_urls"] = sorted(set((match.get("source_urls") or []) + (d.get("source_urls") or [])))
                updates["extracted_at"] = d["extracted_at"]
                if match.get("osm_confidence") is not None:
                    d["osm_confidence"] = match["osm_confidence"]
                    apply_score(d, score_port(
                        {**d, "osm_confidence": match["osm_confidence"]},
                        official_source=any(s.get("official") for s in used_sources)))
                    updates["confidence"] = d.get("confidence")
                    updates["confidence_parts"] = d.get("confidence_parts")
                    updates["confidence_reasons"] = d.get("confidence_reasons")
                    updates["confidence_level"] = d.get("confidence_level")
                await db.poe_ports.update_one({"_id": match["_id"]}, {"$set": updates})
                merged += 1
            else:
                await db.poe_ports.insert_one(d)
                existing_ports.append(d)
                inserted += 1
        poe_count = await db.poe_ports.count_documents({"mrgid": mrgid})
        await db.eez_zones.update_one({"mrgid": mrgid}, {"$set": {
            "status": status,
            "poe_count": poe_count,
            "generated_at": now_iso(),
            "sources": used_sources,
            "source_hashes": hashes,
            "source_excerpts": excerpts,
            "last_error": None,
            "sources_official": bool(any(s.get("official") for s in used_sources)),
            "confidence_avg": zone_confidence_avg(docs),
        }, "$unset": {"unclos": ""}})
        log(f"terminé: {inserted} nouveau(x) PoE, {merged} fusionné(s) — total zone {poe_count}, statut {status}")
    elif zone.get("poe_count", 0) > 0:
        # Empty re-extraction on an already populated zone: PRESERVE existing
        # ports (the search pipeline is non-deterministic).
        log(f"0 port extrait — {zone.get('poe_count')} PoE existants préservés (aucune purge)")
        await db.eez_zones.update_one({"mrgid": mrgid}, {"$set": {
            "checked_at": now_iso(),
            "last_error": "ré-extraction vide — ports précédents conservés",
        }})
    else:
        await db.eez_zones.update_one({"mrgid": mrgid}, {"$set": {
            "status": "erreur",
            "poe_count": 0,
            "generated_at": now_iso(),
            "sources": used_sources,
            "source_hashes": hashes,
            "last_error": "aucun port d'entrée extrait des sources",
        }})
        log("terminé: 0 PoE, statut erreur")
    return await db.eez_zones.find_one({"mrgid": mrgid})


async def _store_evidence(db, zone: dict, docs: list[dict], raw: str,
                          used_sources: list[dict], log, rec=None) -> int:
    """Build evidence records from the collected official text and store them; never fails."""
    try:
        records = evidence_store.build_records(
            zone.get("mrgid"), [d.get("name") for d in docs or []],
            evidence_store.documents_from_context(raw, used_sources))
        stored = await evidence_store.store(db, records)
    except Exception as e:  # noqa: BLE001 — evidence is optional, generation must finish
        log(f"evidence: non stockée ({type(e).__name__}: {str(e)[:100]})")
        return 0
    if stored:
        log(f"evidence: {stored} extrait(s) officiel(s) stocké(s) pour {len({r['port_key'] for r in records})} port(s)")
    await emit(rec, "evidence_stored", records=stored,
               ports=sorted({r["port_key"] for r in records}))
    return stored


CATALOG_CONTEXT_CHARS = 40000


def extraction_context(raw: str, raw_catalog: list, rag_q: str) -> tuple[str, int, str]:
    """Context for the LLM readers, the window they may send, and the branch that chose it.

    A port catalog or legal phrasing keeps the raw text (the readers send up to 40,000
    chars); longer text is condensed by RAG to 20,000; short text is sent as is.
    """
    if looks_like_port_catalog(raw) or raw_catalog:
        return raw[:CATALOG_CONTEXT_CHARS], CATALOG_CONTEXT_CHARS, "catalog"
    if len(raw) > 15000:
        return (select_list_context(rag_q, raw, max_chars=LLM_CONTEXT_CHARS),
                LLM_CONTEXT_CHARS, "rag")
    return raw, LLM_CONTEXT_CHARS, "raw"


async def generate_zone_poe(db, mrgid: int, logger=None, force: bool = False,
                            run=None, refresh: bool = False) -> dict:
    """Orchestrator: search -> collect -> extract -> write.

    refresh=True: periodic refresh only — MD5/semantic monitoring
    may then skip re-extraction. A manual generation, a run, or
    force=True always re-runs the search.

    run (RunContext, optional): "versioned from-scratch run" mode."""
    log = logger or (lambda m: None)
    zone = await db.eez_zones.find_one({"mrgid": int(mrgid)})
    if not zone:
        raise ValueError(f"ZEE mrgid={mrgid} inconnue — construire le référentiel d'abord")
    name = zone.get("name") or zone.get("geoname")
    rec = ZoneRecorder(run.recorder, int(mrgid), name) if run is not None else None
    t0 = time.time()
    settings: dict = {}
    try:
        from app.db import get_settings
        settings = await get_settings()
    except Exception:
        settings = {}
    exceptions = load_exceptions()
    whitelist = build_whitelist(zone.get("iso2"), zone.get("sov_iso2"), exceptions)
    variant = normalize_variant(getattr(run, "variant", None) if run is not None else "tinyfish")
    from app.core.extract import allow_tinyfish_fetch
    _tf_token = allow_tinyfish_fetch.set(variant == "tinyfish")
    _ctx_token = llm_context_chars.set(LLM_CONTEXT_CHARS)
    log(f"=== {name} (mrgid {mrgid}) — variant={variant} whitelist: {', '.join(whitelist[:8]) or 'vide'}")
    await emit(rec, "zone_start", iso2=zone.get("iso2"), sov_iso2=zone.get("sov_iso2"),
               sovereign=zone.get("sovereign"), pol_type=zone.get("pol_type"),
               whitelist=whitelist, variant=variant)

    try:
        # 1. Monitoring: only a periodic refresh.
        #    Generation / improvement / run: always search.
        if run is None and refresh:
            unchanged = await _skip_if_unchanged(db, zone, force, log, rec)
            if unchanged is not None:
                return unchanged

        # 2. Search + gatekeeper
        official, strictly_official, synthesis = await _find_sources(
            zone, whitelist, exceptions, log, rec, variant=variant)

        # 3. Collect texts
        texts, hashes, used_sources, excerpts = await _collect_texts(official, log, rec)

        # Post-collection MD5 monitoring: skip if content is strictly identical
        old_hashes = zone.get("source_hashes") or {}
        if (run is None and refresh and not force and hashes and old_hashes
                and hashes == old_hashes
                and zone.get("status") in ("ia", "ia_sans_source") and zone.get("poe_count", 0) > 0):
            log("MD5 inchangés — ré-extraction sautée (contenu source identique)")
            await db.eez_zones.update_one({"mrgid": int(mrgid)}, {"$set": {"checked_at": now_iso()}})
            return await db.eez_zones.find_one({"mrgid": int(mrgid)})

        # Assemble context (texts + optional grounded synthesis)
        context_parts = list(texts)
        if synthesis:
            # The synthesis comes from grounded search over official sources;
            # it is always joined to the context (gov pages rarely list
            # ports in raw HTML). Status remains driven by the gatekeeper.
            context_parts.append(f"[SYNTHÈSE DE RECHERCHE (à recouper)]\n{synthesis[:6000]}")
            if not texts:
                strictly_official = False
                log("aucun texte source exploitable — extraction depuis la seule synthèse (confiance basse)")
        if not context_parts:
            # No usable text: last-resort grounded search BEFORE declaring
            # the error (same net as the post-extraction retry, here earlier).
            log("aucun texte exploitable — recours à la synthèse groundée")
            _, syn_rescue = await search_grounded(zone, whitelist, log)
            await emit(rec, "search", engine="grounded", lang="en",
                       query="(rescue: aucun texte exploitable)", retry=True,
                       n=0, synthesis_chars=len(syn_rescue or ""))
            if syn_rescue:
                synthesis = syn_rescue
                context_parts.append(f"[SYNTHÈSE DE RECHERCHE (à recouper)]\n{syn_rescue[:6000]}")
                strictly_official = False
                log("synthèse groundée obtenue — extraction depuis la synthèse (confiance basse)")
        if not context_parts:
            log("ERREUR: aucun contenu exploitable")
            await emit(rec, "zone_error", error="aucun contenu source exploitable",
                       duration_s=round(time.time() - t0, 1))
            if run is not None:
                return await _persist_zone_run(db, zone, [], "erreur",
                                               used_sources, hashes, run, rec, log)
            await db.eez_zones.update_one({"mrgid": int(mrgid)}, {"$set": {
                "status": "erreur", "last_error": "aucun contenu source exploitable",
                "generated_at": now_iso(), "sources": used_sources, "source_hashes": hashes,
            }})
            return await db.eez_zones.find_one({"mrgid": int(mrgid)})

        # Local RAG: the LLM gets an excerpt; the catalog parser ALWAYS
        # reads the raw official text (else Alofi / SCT list tail vanish).
        raw = "\n\n".join(context_parts)
        raw_chars = len(raw)
        raw_catalog = extract_structured_ports(raw)
        if raw_catalog:
            remembered = remember_seed_urls(zone, urls_with_catalog(texts), exceptions)
            if remembered:
                log(f"sources productives mémorisées pour {zone.get('iso2')}: {remembered}")
                await emit(rec, "seed_remembered", iso2=zone.get("iso2"), urls=remembered)
        rag_q = (f"official designated ports of entry customs gazette decree "
                 f"puertos habilitados puertos de entrada port of entry {name}")
        context, window, branch = extraction_context(raw, raw_catalog, rag_q)
        llm_context_chars.set(window)
        sent_chars = min(len(context), window)
        if branch == "catalog":
            log(f"catalogue / tournure légale ({len(raw_catalog)} nom(s)) — "
                f"texte brut conservé pour le parseur, LLM sur {sent_chars} chars")
        elif branch == "rag":
            log(f"RAG: contexte condensé à {len(context)} chars (listes d'abord, puis similarité)")
        await emit(rec, "context", sources=len(texts), synthesis=bool(synthesis),
                   raw_chars=raw_chars, final_chars=len(context), llm_chars=sent_chars,
                   catalog_n=len(raw_catalog))

        # 4. Extraction + geocoding + spatial validation
        docs = await _extract_and_geocode(zone, context, used_sources, log, rec, run,
                                          catalog_text=raw, settings=settings)

        # Safety net: 0 ports extracted from collected pages AND no
        # grounded synthesis tried yet → one (single) grounded-search
        # fallback, whose synthesis is joined to the context then re-extracted.
        # (Gov pages rarely list ports in raw HTML; without this net,
        # thin-page zones all end in error.)
        if not docs and synthesis is None:
            log("0 port extrait des pages collectées — recours à la synthèse groundée (retry unique)")
            _, syn_retry = await search_grounded(zone, whitelist, log)
            await emit(rec, "search", engine="grounded", lang="en",
                       query="(retry synthèse après 0 port)", retry=True,
                       n=0, synthesis_chars=len(syn_retry or ""))
            if syn_retry:
                # Synthesis first: readers send only the start of the context (see
                # llm_context_chars), so appended after a full context it was never read.
                context_retry = f"[SYNTHÈSE DE RECHERCHE (à recouper)]\n{syn_retry[:6000]}\n\n{context}"
                docs = await _extract_and_geocode(zone, context_retry, used_sources, log, rec, run,
                                                  catalog_text=context_retry, settings=settings)
                if docs:
                    strictly_official = False
                    for d in docs:
                        d["from_synthesis"] = True
                        apply_score(d, score_port(
                            d, official_source=False,
                            listing_ports=None))

        # Official evidence records from the fetched text (never model output), stored for
        # the run pipeline and /api/poe/verify, which read ports from Zetaris.
        await _store_evidence(db, zone, docs, raw, used_sources, log, rec)

        # 5. Write: confidence lives on each port, no longer a zone buffer.
        status = "ia" if docs else "erreur"
        if run is not None:
            result = await _persist_zone_run(db, zone, docs, status,
                                             used_sources, hashes, run, rec, log)
        else:
            result = await _persist_zone(db, zone, docs, status, used_sources, hashes, excerpts, log)
        await emit(rec, "zone_done", status=(result or {}).get("status"),
                   poe_count=(result or {}).get("poe_count", 0),
                   duration_s=round(time.time() - t0, 1),
                   variant=variant)
        return result
    except Exception as e:
        await emit(rec, "zone_error", error=f"{type(e).__name__}: {str(e)[:200]}",
                   duration_s=round(time.time() - t0, 1))
        raise
    finally:
        llm_context_chars.reset(_ctx_token)
        allow_tinyfish_fetch.reset(_tf_token)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def zone_to_item(doc: dict) -> dict:
    return {
        "mrgid": doc.get("mrgid"),
        "name": doc.get("name"),
        "label": doc.get("label") or doc.get("name"),
        "qualifier": doc.get("qualifier"),
        "qualifier_key": doc.get("qualifier_key"),
        "disambiguated": bool(doc.get("disambiguated")),
        "geoname": doc.get("geoname"),
        "sovereign": doc.get("sovereign"),
        "iso2": doc.get("iso2"),
        "sov_iso2": doc.get("sov_iso2"),
        "pol_type": doc.get("pol_type"),
        "area_km2": doc.get("area_km2"),
        "bbox": doc.get("bbox"),
        "anchor": doc.get("anchor"),
        "status": doc.get("status", "non_generee"),
        "poe_count": doc.get("poe_count", 0),
        "generated_at": doc.get("generated_at"),
        "stale": is_stale(doc),
        "last_error": doc.get("last_error"),
        "unclos": doc.get("unclos"),
        "sources_official": doc.get("sources_official"),
        "confidence_avg": doc.get("confidence_avg"),
        "sources": [{"url": s.get("url"), "domain": s.get("domain"), "collected_at": s.get("collected_at"),
                     "official": s.get("official")}
                    for s in (doc.get("sources") or [])],
        "whitelist": (doc.get("whitelist") or [])[:8],
    }


def ports_to_geojson(docs: list[dict]) -> dict:
    from app.agents.evaluator import _reasons_en
    from app.services.v3_certify import LEVELS, level_from_v2

    feats = []
    for d in docs:
        if d.get("lat") is None or d.get("lon") is None:
            continue
        c_level = d.get("confidence_level")
        if c_level not in LEVELS:
            try:
                c_level = level_from_v2(d)
            except Exception:
                c_level = "C0"
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
            "properties": {
                "id": d["_id"], "name": d.get("name"), "city": d.get("city"), "note": d.get("note"),
                "mrgid": d.get("mrgid"), "zone_name": d.get("zone_name"),
                "country_iso2": d.get("country_iso2"),
                "validated": bool(d.get("validated")), "distance_km": d.get("distance_km"),
                "geocode_source": d.get("geocode_source"),
                "source_urls": d.get("source_urls") or [],
                "url_bu": d.get("url_bu"),
                "extracted_at": d.get("extracted_at"),
                "osm_confidence": d.get("osm_confidence"),
                "osm_tags": d.get("osm_tags"),
                "spatial_anomaly": d.get("spatial_anomaly"),
                "confidence": d.get("confidence"),
                "confidence_level": c_level,
                "confidence_reasons": d.get("confidence_reasons"),
                "confidence_parts": d.get("confidence_parts"),
                "reasons_en": (
                    _reasons_en(d.get("confidence_reasons") or [])
                    if d.get("confidence_reasons")
                    else None
                ),
                "spatial_kind": d.get("spatial_kind"),
            },
        })
    return {
        "type": "FeatureCollection",
        "attribution": f"{EEZ_ATTRIBUTION} · Geocoding © OpenStreetMap/Nominatim (ODbL)",
        "features": feats,
    }
