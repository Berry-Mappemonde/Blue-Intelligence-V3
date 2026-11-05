"""Certification levels C0..C4.

Same ladder as the V3 prototype (`certify.level`): a level never rests on an
LLM-only point above C1. C1 needs an extracted source on a whitelisted
State host, C2 a NemotronJudge "poe" verdict with a verified excerpt, or an
LLM extraction plus an independent source (LLM∩NER, or two official hosts);
the catalogue parser alone is not corroboration. A "not_poe" verdict caps the
level at C1. C3 two geocoders within 500 m, at sea or at the quay, C4 a human
confirmation on top of C3. An obvious name fragment ("Saint", "nterior") is C0.

`confidence_level` applies that ladder after a verified-quote check: the
quoted evidence must literally appear in the fetched page (whitespace and
case normalized). Otherwise that evidence is not extracted and the level
is recomputed — it can only fall.

Two per-port guards (pilot: 1 of 19 C2 ports had correct per-port evidence):
the "two official hosts" corroboration also needs the port's name next to
entry wording in an official text (`entry_context`; tariff and port-dues
schedules do not count), and every C2+ is capped at C1 ("evidence not
attributable to zone") unless the pipeline placed the port in this zone
(8face05: a GPS outside the mrgid is never written), or its evidence
row/section names this zone's territory (`zone_attributable`). Unplaced and
unattributed ports (name only, no GPS) stay at C1 at most.

`level_from_v2` maps a pipeline port onto the same scale from its
extraction, source, judge and geo fields. It never reads the V2 score
(`confidence`, `confidence_parts`, `confidence_reasons`) and never writes
it: the two stay separate (PLAN-V3 §7, relation still to confirm).
"""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from functools import lru_cache

LEVELS = ("C0", "C1", "C2", "C3", "C4")
# Fields attach_confidence_level writes. Run collections only, never poe_ports.
LEVEL_FIELDS = ("confidence_level", "confidence_downgraded_from", "quote_verified",
                "judge_contradicted", "name_fragment", "confidence_cap_reason")
CAP_NOT_IN_ZONE = "evidence not attributable to zone"
CAP_NO_ENTRY_CONTEXT = "no per-port port-of-entry context"
CAP_NOT_DESIGNATING = "evidence from a study, not a designation"
CAP_NOT_CLEARANCE_POINT = "row of a clearance list that is not a marina or harbour office"
CAP_CARGO_LIST = "evidence from the cargo ICS list, not a designation"
CAP_NO_CLEARANCE_POINT = "commune with no clearance point on the zone's clearance list"
_RANK = {name: i for i, name in enumerate(LEVELS)}

GEO_AGREE_KM = 0.5
GEO_KINDS = frozenset({"in_eez", "coastal_land"})
_WS = re.compile(r"\s+")


def is_official(url: str, whitelist: Sequence[str] | None) -> bool:
    """Official = the URL's host is on the zone's State whitelist.

    The whitelist is the pipeline's own (`poe_pipeline.build_whitelist`,
    matched with `url_allowed`): host only, never path or query, and no
    name-token or keyword heuristic. No whitelist, nothing is official.
    """
    if not isinstance(url, str) or not url.strip() or not whitelist:
        return False
    from app.agents.evaluator import is_official_source

    return is_official_source([url], list(whitelist))


def _evidence(entity: dict) -> list[dict]:
    return [e for e in (entity.get("evidence") or []) if isinstance(e, dict)]


def level(entity: dict) -> str:
    """C0–C4 ladder on resolved evidence (`official` already set from the
    whitelist, see `with_official`). Does not look at quote text — see
    `confidence_level`."""
    ev = _evidence(entity)
    if not ev:
        return "C0"
    lvl = "C0"
    if any(e.get("extracted") and e.get("official") is True for e in ev):
        lvl = "C1"
    if lvl == "C1" and any(e.get("judge") == "supported" for e in ev):
        lvl = "C2"
    g = entity.get("geo") if isinstance(entity.get("geo"), dict) else {}
    if lvl == "C2" and _geo_agrees(g):
        lvl = "C3"
    if lvl == "C3" and entity.get("human") == "confirmed":
        lvl = "C4"
    return lvl


def _geo_agrees(geo: dict) -> bool:
    """PLAN-V3 §5 C3 gate: two independent geocoders within GEO_AGREE_KM of
    each other, and the point at sea or at the quay (land mask).

    The pipeline's own `geocode_agree` flag uses a 2 km tolerance
    (core.geo.GEOCODE_AGREE_KM) and is not enough on its own.
    """
    if geo.get("agree") is not True or not _enough_sources(geo):
        return False
    km = geo.get("agreement_km")
    if isinstance(km, bool) or not isinstance(km, (int, float)) or km < 0:
        return False
    if km > GEO_AGREE_KM:
        return False
    return geo.get("spatial_kind") in GEO_KINDS


def _enough_sources(geo: dict) -> bool:
    raw = geo.get("independent_sources", 0)
    if isinstance(raw, bool) or raw is None:
        return False
    try:
        return float(raw) >= 2
    except (TypeError, ValueError):
        return False


def normalize_quote(text: str) -> str:
    """Collapse whitespace and case-fold. Punctuation stays literal."""
    if not isinstance(text, str):
        return ""
    return _WS.sub(" ", text).strip().casefold()


def verified_quote(quote: str, page_text: str) -> bool:
    """True when the whole quote appears in the page after normalization.

    An empty quote is not verified. A matching prefix is not enough: the
    full quote has to be in the page.
    """
    needle = normalize_quote(quote)
    if not needle:
        return False
    return needle in normalize_quote(page_text)


def _page_for(evidence: dict, entity: dict, pages: Mapping[str, str] | None) -> str:
    url = evidence.get("url") if isinstance(evidence.get("url"), str) else ""
    if pages is not None and url in pages:
        value = pages[url]
        return value if isinstance(value, str) else ""
    for source in (evidence, entity):
        page = source.get("page_text")
        if isinstance(page, str) and page:
            return page
    return ""


def _with_verified_quotes(entity: dict, pages: Mapping[str, str] | None = None) -> dict:
    """Copy whose unverified quotes are not extracted and cannot stay judged.

    A quote that is present and verified does not raise `extracted` when
    the caller already set it to false — verification only downgrades.
    """
    out = dict(entity)
    ev_out = []
    for raw in _evidence(entity):
        e = dict(raw)
        quote = e.get("quote")
        if isinstance(quote, str) and quote.strip():
            ok = verified_quote(quote, _page_for(e, entity, pages))
            e["quote_verified"] = ok
            if not ok:
                e["extracted"] = False
                if e.get("judge") == "supported":
                    e.pop("judge", None)
        ev_out.append(e)
    out["evidence"] = ev_out
    return out


def _pack(before: str, after: str, checked: dict) -> dict:
    out = {"confidence_level": after}
    if _RANK[after] < _RANK[before]:
        out["downgraded_from"] = before
    flags = [
        e.get("quote_verified")
        for e in _evidence(checked)
        if isinstance(e.get("quote"), str) and e.get("quote").strip()
    ]
    if flags:
        out["quote_verified"] = all(flags)
    return out


def with_official(entity: dict, whitelist: Sequence[str] | None) -> dict:
    """Copy whose evidence `official` flags come from the whitelist only.

    A caller-supplied `official: True` is not trusted.
    """
    out = dict(entity)
    out["evidence"] = [
        {**e, "official": is_official(e.get("url") or "", whitelist)}
        for e in _evidence(entity)
    ]
    return out


def assess_level(entity: dict, pages: Mapping[str, str] | None = None, *,
                 whitelist: Sequence[str] | None = None) -> dict:
    """Ladder before and after the quote check.

    Returns confidence_level, and downgraded_from / quote_verified when
    a quote was checked.
    """
    entity = with_official(entity, whitelist)
    before = level(entity)
    checked = _with_verified_quotes(entity, pages)
    after = level(checked)
    return _pack(before, after, checked)


def confidence_level(entity: dict, pages: Mapping[str, str] | None = None, *,
                     whitelist: Sequence[str] | None = None) -> str:
    """C0–C4 after dropping evidence whose quote is not in the fetched page."""
    return assess_level(entity, pages, whitelist=whitelist)["confidence_level"]


def _urls(port: dict) -> list[str]:
    out: list[str] = []
    for url in port.get("source_urls") or []:
        if isinstance(url, str) and url.strip() and url not in out:
            out.append(url)
    return out


# Readers that are not an LLM extraction: the catalogue parser and the local NER.
NON_LLM_ENGINES = frozenset({"catalog", "ner"})


def _official_hosts(port: dict, whitelist: Sequence[str] | None) -> set[str]:
    from urllib.parse import urlsplit

    hosts = set()
    for url in _urls(port):
        if is_official(url, whitelist):
            host = (urlsplit(url).hostname or "").lower()
            hosts.add(host[4:] if host.startswith("www.") else host)
    return hosts


def _corroborated_llm_extraction(port: dict, whitelist: Sequence[str] | None,
                                 texts: Sequence[Mapping] | None = None,
                                 zone: Mapping | None = None) -> bool:
    """C2 without the judge: an LLM extraction plus an independent source.

    Independent source = the local NER found the same name in the text
    (`extraction_agreement`, LLM∩NER), or the port comes from two distinct official
    hosts. Either way, one official text must put this port's name next to entry wording
    (`entry_context`): LLM∩NER says a name is in a text, not that the text designates it
    (Palmeira, a locality in a list of a Decreto Regulamentar), and two hosts alone are
    zone-level, not per-port: every port of the zone gets the same source_urls. A mention in
    a row of a list is no such context (``list_row_spans``): a row reaches C2 by the judge. The
    catalogue parser alone (or NER alone) is a single non-LLM reader: it never counts as
    corroboration. Two LLM readers do not count either. The V2 score's reasons are never read.
    """
    engine = (port.get("extraction_engine") or "").strip().lower()
    if not engine or engine in NON_LLM_ENGINES:
        return False
    if port.get("extraction_agreement") is not True and len(_official_hosts(port, whitelist)) < 2:
        return False
    return bool(entry_mentions(port.get("name"), _official_texts(texts, whitelist),
                               places=_places(port, zone), rows=False))


def _corroborated_without_context(port: dict, whitelist: Sequence[str] | None,
                                  texts: Sequence[Mapping] | None,
                                  zone: Mapping | None = None) -> bool:
    """LLM∩NER or two hosts would have lifted this port without its entry context."""
    engine = (port.get("extraction_engine") or "").strip().lower()
    return (bool(engine) and engine not in NON_LLM_ENGINES
            and judge_outcome(port) != JUDGE_SUPPORTED
            and (port.get("extraction_agreement") is True
                 or len(_official_hosts(port, whitelist)) >= 2)
            and not entry_mentions(port.get("name"), _official_texts(texts, whitelist),
                                   places=_places(port, zone), rows=False))


# --- per-port evidence: entry context and zone attribution ---------------------------

# Entry context reuses registry_check.entry_mention (the official-evidence check of the
# evidence store: the port's name within PROXIMITY_CHARS of an entry term, outside
# addresses; strong terms alone, weak terms with a port word and the full name). Added
# here only: wording that check does not have yet. Bare "clearance" was narrowed to
# "clearance port" in 974da96 (too broad on its own), so it comes back as a WEAK term:
# it needs a port word and the full name, like customs / douane / alfândega / aduana.
ENTRY_TERMS_STRONG = (
    "ouvert au trafic international", "ouverts au trafic international",
    "ouverte au trafic international", "ouvertes au trafic international",
    "approved port", "approved ports", "approved marina", "approved marinas",
    "bureau de douane", "bureaux de douane",
    "porto alfandegado", "portos alfandegados",
    # "porto(s) habilitado(s)" moved to registry_check.STRONG_ENTRY_TERMS: shared with the
    # evidence store, so it also makes judge evidence.
)
ENTRY_TERMS_WEAK = ("clearance",)
# Tariff and fee schedules list ports for pricing, not for entry: never entry context.
TARIFF_TERMS = (
    "tarifa", "tarifas", "tariff", "tariffs", "tarif", "tarifs", "tarifario", "tarifário",
    "tarifaire", "tarifaires", "tipo i", "tipo ii", "tipo iii", "tipo iv",
    "port dues", "harbour dues", "harbor dues", "port charges", "harbour fees", "harbor fees",
    "port fees", "droits de port", "redevance", "redevances", "taxe portuaire",
    "taxes portuaires", "tasas portuarias", "tasa portuaria", "taxa portuária",
    "taxas portuárias", "fee schedule", "schedule of fees", "schedule of charges",
)
# Clément, 3 Nov 2026: a study describing its scope (the WCO clearance-time study, "Estudo
# de Tempo de Desalfandegamento", for Porto da Praia / Praia) is not designation; C2 needs
# a decree, an order, an official list or a row of one. Studies are excluded like tariff
# schedules: such a word near the mention, or on its line, and the mention does not count.
# Nouns only, never "report" (a customs act's "report to the proper officer").
STUDY_TERMS = (
    "study", "studies", "estudo", "estudos", "étude", "études", "estudio", "estudios",
    "studio di", "studie",
)
NOT_DESIGNATING_TERMS = TARIFF_TERMS + STUDY_TERMS
# The douane ICS list, "Liste des bureaux points d'entrée français dans l'Union européenne":
# cargo offices of the Import Control System, not yacht entry. Le Robert and Saint-Pierre are
# rows of it and no PoE (Clément's gold, §10.1). Its title is far from its rows, so unlike
# tariff and study wording this is read per document, from the head of the text or the URL.
CARGO_LIST_TITLES = ("liste des bureaux points d'entrée",)
CARGO_LIST_URLS = ("ics-liste-points-d-entree",)
CARGO_TITLE_CHARS = 300


def is_cargo_list(item: Mapping) -> bool:
    """The document is the cargo ICS list: it can never give C2, judge verdict included."""
    from app.agents import registry_check as rc

    body = item.get("text")
    return (any(u in (item.get("url") or "").lower() for u in CARGO_LIST_URLS)
            or (isinstance(body, str)
                and _has_term(rc._tokens(body[:CARGO_TITLE_CHARS]), CARGO_LIST_TITLES)))


def _official_items(texts: Sequence[Mapping] | None,
                    whitelist: Sequence[str] | None) -> list[Mapping]:
    return [item for item in texts or [] if isinstance(item, Mapping)
            and isinstance(item.get("text"), str) and item["text"].strip()
            and is_official(item.get("url") or "", whitelist)]


def _official_texts(texts: Sequence[Mapping] | None,
                    whitelist: Sequence[str] | None) -> list[str]:
    """Bodies of the given [{url, text}] whose URL is on the zone's State whitelist. The
    cargo ICS list is left out: nothing it says is evidence of entry."""
    return [i["text"] for i in _official_items(texts, whitelist) if not is_cargo_list(i)]


def _places(port: Mapping, zone: Mapping | None) -> tuple[str, ...]:
    """The zone's own names, never a port-name variant (grounding_variants ``places``)."""
    return tuple(sorted(zone_terms(port, zone)))


def _name_spans(text: str, tokens, name, places: tuple = ()) -> list[tuple[int, int]]:
    """(start, end) of the port name in text, contact addresses excluded."""
    from app.agents import registry_check as rc

    out = []
    for variant in rc.grounding_variants(name, places=places):
        for ns, ne in rc.name_spans(text, tokens, variant):
            if not rc._in_address(text, tokens, ns, ne):
                out.append((ns, ne))
    return out


def _contact_rows(text: str) -> list[tuple[int, int]]:
    """A clearance list reaches us as flat text: each row is what stands between two contact
    blocks, its name, address and hours. Prose has no such run of short stretches."""
    rows, start = [], 0
    for mark in _CONTACT.finditer(text):
        if ROW_MIN_CHARS <= mark.start() - start <= ROW_MAX_CHARS:
            rows.append((start, mark.start()))
        start = mark.end()
    return rows if len(rows) >= ROW_LIST_MIN_ROWS else []


@lru_cache(maxsize=64)
def list_row_spans(text: str) -> tuple[tuple[int, int], ...]:
    """(start, end) of the rows of a list in ``text``; none in prose, whatever it says.

    Rows are read from the structure, because a word doesn't make a list: a page may say
    "point de clearance" in a sentence. A clearance list's rows sit between contact blocks,
    a border crossing list's are its items and cells (``border_crossing.row_spans``), and a
    table of border control posts is rows throughout (its post codes, ``_POST_CODE``)."""
    from app.agents import registry_check as rc
    from app.services import border_crossing

    spans = list(border_crossing.row_spans(text))
    if is_clearance_list(rc._tokens(text)):
        spans += _contact_rows(text)
    if len(_POST_CODE.findall(text)) >= ROW_LIST_MIN_CODES:
        spans.append((0, len(text)))
    return tuple(spans)


@lru_cache(maxsize=64)
def _without_list_rows(text: str) -> str:
    """``text`` with its list rows blanked: same length, so offsets still hold."""
    out = text
    for start, end in list_row_spans(text):
        out = out[:start] + " " * (end - start) + out[end:]
    return out


def entry_mentions(name, texts: Sequence[str], places: tuple = (),
                   exclude: tuple = NOT_DESIGNATING_TERMS, rows: bool = True
                   ) -> list[tuple[str, int, int]]:
    """(text, name_start, name_end) per text where registry_check.entry_mention finds the
    port name next to entry wording (port of entry, clearance, customs / alfândega /
    douane / aduana with a port word, "ouvert au trafic international", designated /
    approved port). A mention inside a tariff or port-dues schedule, or a study, never
    counts. ``places``: the zone's names, never a variant of the port's name. ``rows=False``:
    the rows of a list are left out, for the routes that lift a port without the judge. A row
    is a name and a phone number, and the entry word nearest to it is its neighbour's."""
    if is_name_fragment(name):
        return []
    from app.agents import registry_check as rc

    variants = rc.grounding_variants(name, places=places)
    out = []
    for text in texts or []:
        text = text if rows else _without_list_rows(text)
        m = rc.entry_mention(text, variants, extra_strong=ENTRY_TERMS_STRONG,
                             extra_weak=ENTRY_TERMS_WEAK, exclude=exclude)
        if m:
            out.append((text, m["name_start"], m["name_end"]))
    return out


def all_mentions(name, texts: Sequence[str], places: tuple = ()) -> list[tuple[str, int, int]]:
    if is_name_fragment(name):
        return []
    from app.agents import registry_check as rc

    out = []
    for text in texts or []:
        tokens = rc._tokens(text)
        out += [(text, ns, ne) for ns, ne in _name_spans(text, tokens, name, places)]
    return out


def _near_phrase(text: str, start: int, end: int, phrases: tuple[str, ...]) -> bool:
    """One of ``phrases`` within PROXIMITY_CHARS of [start, end) or on its line: the
    window registry_check.entry_mention uses for ``exclude``."""
    from app.agents import registry_check as rc

    tokens = rc._tokens(text)
    lo, hi = start - rc.PROXIMITY_CHARS, end + rc.PROXIMITY_CHARS
    line_lo, line_hi = rc._line_bounds(text, start, end)
    return any((lo <= xs and xe <= hi) or (line_lo <= xs and xe <= line_hi)
               for p in rc._extra_phrases(tuple(phrases)) for xs, xe in rc._spans(tokens, p))


def judge_excerpt_in_study(port: Mapping, texts: Sequence[Mapping] | None,
                           whitelist: Sequence[str] | None) -> bool:
    """The judge's verified excerpt sits in a study (STUDY_TERMS around it, located in an
    official text): the judge read a study's scope, not a designation. An excerpt that is
    not found in the texts is not judged here."""
    judge = port.get("judge") if isinstance(port.get("judge"), Mapping) else {}
    excerpt = judge.get("quoted_excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        return False
    quote = excerpt.strip().lower()
    for text in _official_texts(texts, whitelist):
        at = text.lower().find(quote)
        if at >= 0:
            return _near_phrase(text, at, at + len(quote), STUDY_TERMS)
    return False


def judge_excerpt_on_other_row(port: Mapping, texts: Sequence[Mapping] | None,
                               whitelist: Sequence[str] | None) -> bool:
    """The judge's verified excerpt is a row of a list and doesn't name this port: the judge
    read a neighbour's row. An excerpt that is not found in the texts is not judged here."""
    from app.agents import registry_check as rc

    judge = port.get("judge") if isinstance(port.get("judge"), Mapping) else {}
    excerpt = judge.get("quoted_excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        return False
    quote = excerpt.strip().lower()
    for text in _official_texts(texts, whitelist):
        at = text.lower().find(quote)
        if at >= 0 and any(lo <= at < hi for lo, hi in list_row_spans(text)):
            tokens = rc._tokens(excerpt)
            return not (_name_spans(excerpt, tokens, port.get("name"))
                        or next(rc.name_spans(excerpt, tokens, commune_key(port.get("name"))), None))
    return False


def _cargo_list_only(port: Mapping, texts: Sequence[Mapping] | None,
                     whitelist: Sequence[str] | None) -> bool:
    """The cargo ICS list is what the judge read: its excerpt is found there and in no other
    official text. ``_official_texts`` already keeps the list out of the entry context that
    LLM∩NER and two hosts need; the judge reads the fetched texts."""
    from app.agents.poe_judge import excerpt_verified

    cargo = [i for i in _official_items(texts, whitelist) if is_cargo_list(i)]
    if not cargo or judge_outcome(dict(port)) != JUDGE_SUPPORTED:
        return False
    # Located the way the judge verified it: quotes, case and spacing aside.
    excerpt = port["judge"]["quoted_excerpt"]
    official = _official_texts(texts, whitelist)
    return (excerpt_verified(excerpt, cargo)
            and not excerpt_verified(excerpt, [{"text": t} for t in official]))


def _study_only(port: Mapping, texts: Sequence[Mapping] | None,
                whitelist: Sequence[str] | None, zone: Mapping | None) -> bool:
    """The port's only entry context (or its judge excerpt) is a study."""
    if judge_outcome(dict(port)) == JUDGE_SUPPORTED:
        return judge_excerpt_in_study(port, texts, whitelist)
    official, places = _official_texts(texts, whitelist), _places(port, zone)
    return (bool(entry_mentions(port.get("name"), official, places, exclude=TARIFF_TERMS))
            and not entry_mentions(port.get("name"), official, places))


def _territory_names(rec: Mapping) -> set[str]:
    names = set()
    for key in ("name_fr", "name_en"):
        raw = rec.get(key)
        if isinstance(raw, str) and raw.strip():
            names.add(raw)
            names.add(re.sub(r"\(.*?\)", " ", raw))  # "Saint-Martin (French)"
            names |= set(re.findall(r"\(([^)]*)\)", raw))  # "(TAAF)"
    code = rec.get("code")
    if isinstance(code, str) and code:
        names.add(code.replace("_", " "))
    return names


def _all_territories() -> list[dict]:
    try:
        from app.services import territory_ref
        return list(territory_ref._territories().values())
    except Exception:  # noqa: BLE001 — no table: zone terms come from the zone itself
        return []


def zone_terms(port: Mapping, zone: Mapping | None = None) -> set[str]:
    """Names of this zone's territory: the VLIZ name and the territories.json record
    (name_fr, name_en, aliases) for the zone's mrgid."""
    zone = zone or {}
    names: set[str] = set()
    for raw in (zone.get("name"), zone.get("geoname"), zone.get("territory"),
                port.get("zone_name")):
        if isinstance(raw, str) and raw.strip():
            names.add(raw)
    for raw in list(zone.get("aliases") or []) + list(port.get("zone_aliases") or []):
        if isinstance(raw, str) and raw.strip():
            names.add(raw)
    mrgid = zone.get("mrgid", port.get("mrgid"))
    if mrgid is not None:
        try:
            from app.services.territory_ref import territory_for_mrgid
            rec = territory_for_mrgid(mrgid)
        except Exception:  # noqa: BLE001
            rec = {}
        if rec:
            names |= _territory_names(rec)
    return _term_keys(names)


def _term_keys(names) -> set[str]:
    from app.agents import registry_check as rc

    out = set()
    for raw in names:
        key = " ".join(t for t, _, _ in rc._tokens(raw))
        if len(key) >= 4:
            out.add(key)
    return out


def _other_territory_terms(own: set[str]) -> set[str]:
    out: set[str] = set()
    for rec in _all_territories():
        keys = _term_keys(_territory_names(rec))
        if not keys & own:
            out |= keys
    # A term that is a sub-phrase of this zone's name is not another territory.
    return {k for k in out if not any(k in o for o in own)}


def _territory_hits(text: str, own: set[str], other: set[str]) -> list[tuple[int, int, bool]]:
    from app.agents import registry_check as rc

    tokens = rc._tokens(text)
    hits = []
    for keys, mine in ((own, True), (other, False)):
        for key in keys:
            for s, e in rc._spans(tokens, tuple(key.split())):
                hits.append((s, e, mine))
    # Longest match wins on overlap ("saint martin" inside "saint martin de ...").
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept: list[tuple[int, int, bool]] = []
    for h in hits:
        if kept and h[0] < kept[-1][1]:
            continue
        kept.append(h)
    return kept


def _mention_in_zone(text: str, ns: int, ne: int, own: set[str], other: set[str]) -> bool:
    """The row (the name's line) or, failing that, the section (nearest territory
    named before the mention) is this zone's territory."""
    hits = [h for h in _territory_hits(text, own, other) if h[1] <= ns or h[0] >= ne]
    if not hits:
        return False
    from app.agents.registry_check import _line_bounds

    lo, hi = _line_bounds(text, ns, ne)
    row = [h for h in hits if lo <= h[0] and h[1] <= hi]
    if row:
        before = [h for h in row if h[1] <= ns]
        chosen = before[-1] if before else min(row, key=lambda h: h[0])
        return chosen[2]
    before = [h for h in hits if h[1] <= ns]
    return bool(before) and before[-1][2]


CLEARANCE_LIST_TERMS = ("points de clearance", "point de clearance",
                        "clearance points", "clearance point")
CLEARANCE_QUOTE_CHARS = 200
# Rows of a list, read from its structure (``list_row_spans``): a phone number or an email
# closes a row of a clearance list, and a table of border control posts gives each post its
# TRACES code (country, place, then a digit and a letter for the kind of post).
_CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\(?\+\)?\s?\d{2,3}(?:[ .-]\d{2,3}){2,}")
_POST_CODE = re.compile(r"\b[A-Z]{2} [A-Z]{3} \d[A-Z]?\b")
ROW_LIST_MIN_ROWS = 3
ROW_LIST_MIN_CODES = 5
ROW_MIN_CHARS = 12  # less is the comma between two contacts of one row
ROW_MAX_CHARS = 300
_COMMUNE_ARTICLE = re.compile(r"^(?:l[ae]s?\s+|l['’])", re.I)
_PRIVATE_USE = re.compile("[\ue000-\uf8ff]")  # PDF icon glyphs (phone, mail) in the list
_HOURS = re.compile(r"\d{1,2}h(?:\d{2})?")
# Clément's ground truth, 3 Nov 2026 (§10.2): only marinas and capitaineries / harbour offices
# of a clearance list are clearance points. The kinds below are on the list and are not,
# even when the name has a marina in it ("Restaurant Barracuda Marina Rivière Sens").
CLEARANCE_POINT_TERMS = ("marina", "capitainerie", "port de plaisance", "bureau du port",
                         "service portuaire", "harbour office", "harbor office",
                         "harbour master", "harbor master", "harbourmaster", "harbormaster")
NOT_CLEARANCE_POINTS = {
    "cybercafé": ("cyberbase", "cyber base", "cybercafe", "cyber cafe", "cyber"),
    "snack bar": ("snack", "restaurant"),
    "shop": ("shipchandler", "ship chandler", "chandler", "boutique", "magasin", "shop",
             "multiservices"),
    "tourist office": ("tourisme", "tourist office", "tourist information"),
    "customs brigade": ("brigade",),
    "fuel station": ("station service", "station essence", "fuel station", "petrol station"),
    "careening station": ("carenage", "careening"),
}
NOT_A_MARINA_OR_OFFICE = "not a marina or harbour office"
NO_POINT_ON_THE_LIST = "commune with no clearance point on the list"


def _has_term(tokens, terms: Sequence[str]) -> bool:
    from app.agents import registry_check as rc

    return any(next(rc._spans(tokens, tuple(t for t, _, _ in rc._tokens(term))), None)
               for term in terms)


def is_clearance_list(tokens) -> bool:
    return _has_term(tokens, CLEARANCE_LIST_TERMS)


def _zone_sections(text: str, own: set[str], other: set[str]) -> list[tuple[int, int, bool]]:
    """Territory heads of a list. "Antilles-Guyane" in an office's name opens no section:
    the head of a section is the territory's own word."""
    return [h for h in _territory_hits(text, own, other) if text[h[0] - 1:h[0]] != "-"]


def _in_zone_section(sections: Sequence[tuple[int, int, bool]], start: int) -> bool:
    section = [h for h in sections if h[1] <= start]
    return not section or section[-1][2]


def excluded_kind(name) -> str | None:
    """The kind of row Clément excludes, read from the row's name: "fuel station"."""
    from app.agents import registry_check as rc

    tokens = rc._tokens(name or "")
    return next((kind for kind, terms in NOT_CLEARANCE_POINTS.items()
                 if _has_term(tokens, terms)), None)


def clearance_point_exclusion(name) -> str | None:
    """Why this row of a clearance list is not a clearance point; None when it is one."""
    from app.agents import registry_check as rc

    kind = excluded_kind(name)
    if kind or _has_term(rc._tokens(name or ""), CLEARANCE_POINT_TERMS):
        return kind
    return NOT_A_MARINA_OR_OFFICE


def _zone_sections(text: str, own: set[str], other: set[str]) -> list[tuple[int, int, bool]]:
    """Territory heads of a list. "Antilles-Guyane" in an office's name opens no section:
    the head of a section is the territory's own word."""
    return [h for h in _territory_hits(text, own, other) if text[h[0] - 1:h[0]] != "-"]


def _row_quote(text: str, start: int, stop: int) -> str:
    """The row as the list writes it: from its name to the next known row or commune, or
    to its opening hours when an unread row follows them."""
    window = text[start:min(stop, start + CLEARANCE_QUOTE_CHARS)]
    hours = list(_HOURS.finditer(window))
    if hours and stop > start + CLEARANCE_QUOTE_CHARS:
        window = window[:hours[-1].end()]
    return " ".join(_PRIVATE_USE.sub(" ", window).split())


def _commune_headings(text: str, tokens, cities: Sequence[str]) -> list[tuple[int, int, tuple, str]]:
    """(start, end, commune key, commune name) where the list heads a commune. The key
    leaves out the article, which lists drop ("Trois-ilets" for "Les Trois-Îlets"); the
    name is the records' spelling, without an article the list does not write."""
    from app.agents import registry_check as rc

    out = []
    for city in cities:
        bare = _COMMUNE_ARTICLE.sub("", city)
        key = tuple(t for t, _, _ in rc._tokens(bare))
        spans = list(rc.name_spans(text, tokens, tuple(t for t, _, _ in rc._tokens(city))))
        name = city
        if not spans:
            spans, name = list(rc.name_spans(text, tokens, key)), bare
        out += [(s, e, key, name) for s, e in spans]
    return sorted(out)


def _bare_name(name: str | None) -> str:
    return re.sub(r"\(.*?\)", " ", name or "").strip()


def commune_key(name: str | None) -> tuple:
    """A name's tokens without its parentheses or article, as ``_commune_headings`` keys it."""
    from app.agents import registry_check as rc

    return tuple(t for t, _, _ in rc._tokens(_COMMUNE_ARTICLE.sub("", _bare_name(name))))


def _heading_names(ports: Sequence[Mapping], place_names: Collection[int]) -> list[str]:
    """Where headings are looked up: the records' cities, and the bare place names, because a
    catalogue's commune has no city and is a heading of the list, not a row of the one above."""
    return list(dict.fromkeys([p.get("city") for p in ports if (p.get("city") or "").strip()]
                              + [_bare_name(ports[i].get("name")) for i in place_names]))


def clearance_rows(ports: Sequence[Mapping], texts: Sequence[Mapping] | None,
                   zone: Mapping | None = None,
                   whitelist: Sequence[str] | None = None,
                   place_names: Collection[int] = ()) -> dict[int, dict]:
    """{index in ``ports``: {commune, commune_key, url, quote, position, exclusion}} for the
    ports that are rows of an official clearance list, under a commune that the list heads.
    ``exclusion``: why the row is not a clearance point (``clearance_point_exclusion``).

    The list gives the commune, not the records: a table reaches us as flat text, with the
    commune once before its rows, so a row's commune is the nearest heading above it, in
    the zone's own section. Headings are looked up from the records' ``city`` values. A
    record's own ``city`` can be wrong (Étang Z'abricots: "Le Lamentin", under
    Fort-de-France on the list), and only the position counts. A port is read on the first
    list that has it. ``place_names``: indexes of the records with a bare place name, from
    the caller, also looked up as headings (``_heading_names``)."""
    from app.agents import registry_check as rc

    if not ports:
        return {}
    cities = _heading_names(ports, place_names)
    own = zone_terms(ports[0], zone)
    other = _other_territory_terms(own)
    found: dict[int, dict] = {}
    for item in texts or []:
        url, text = item.get("url") or "", item.get("text")
        if not isinstance(text, str) or not is_official(url, whitelist):
            continue
        tokens = rc._tokens(text)
        if not is_clearance_list(tokens):
            continue
        heads = _commune_headings(text, tokens, cities)
        rows = []
        for i, port in enumerate(ports):
            name = port.get("name") or ""
            words = tuple(t for t, _, _ in rc._tokens(name))
            bare = commune_key(name)
            if i in found or not words or is_name_fragment(name) or any(h[2] == bare for h in heads):
                continue
            span = next(rc.name_spans(text, tokens, words), None)
            if span:
                rows.append((span[0], span[1], i))
        sections = _zone_sections(text, own, other)
        for start, end, i in sorted(rows):
            if not _in_zone_section(sections, start):
                continue
            floor = max([h[1] for h in sections if h[1] <= start], default=0)
            above = [h for h in heads if floor <= h[0] and h[1] <= start
                     and not any(r[0] <= h[0] and h[1] <= r[1] for r in rows)]
            if not above:
                continue
            _, _, key, name = above[-1]
            stop = min([s for s in [r[0] for r in rows] + [h[0] for h in heads + sections]
                        if s >= end] or [len(text)])
            found[i] = {"commune": name, "commune_key": key, "url": url, "position": start,
                        "quote": _row_quote(text, start, stop),
                        "exclusion": clearance_point_exclusion(ports[i].get("name"))}
    return found


def communes_without_points(ports: Sequence[Mapping], rows: Mapping[int, Mapping],
                            texts: Sequence[Mapping] | None, zone: Mapping | None = None,
                            place_names: Collection[int] = ()) -> dict[int, dict]:
    """{index in ``ports``: {commune, url, quote, exclusion}} for the records named after a
    commune that has no clearance point on the zone's list (Clément, 3 Nov 2026, §10.2).

    ``rows``: ``clearance_rows``. Only a list with a point read for this zone counts: the
    wording alone doesn't show that a text lists the zone's communes. The list decides, not
    the records: the reader may have missed a commune's capitainerie, so what is read is the
    text under the commune's heading, up to the next heading. A record is named after a
    commune when its name is a heading of the list, or when it is a bare place name
    (``place_names``, indexes the caller found without a facility word) that the list
    doesn't head at all. A heading no record gives is not a stop: the text then runs on into
    the next commune, which can spare a record, never cap one."""
    from app.agents import registry_check as rc

    urls = {r["url"] for r in rows.values() if not r["exclusion"]}
    if not urls:
        return {}
    records = {i: commune_key(p.get("name")) for i, p in enumerate(ports) if i not in rows}
    cities = _heading_names(ports, place_names)
    own = zone_terms(ports[0], zone)
    other = _other_territory_terms(own)
    no_point: dict[int, dict] = {}
    with_point: set[int] = set()
    headed: set[int] = set()
    for item in texts or []:
        url, text = item.get("url") or "", item.get("text")
        if url not in urls or not isinstance(text, str):
            continue
        tokens = rc._tokens(text)
        sections = _zone_sections(text, own, other)
        heads = [h for h in _commune_headings(text, tokens, cities)
                 if _in_zone_section(sections, h[0])]
        stops = sorted({h[0] for h in heads} | {h[0] for h in sections})
        for i, key in records.items():
            for _, end, head_key, name in heads:
                if head_key != key:
                    continue
                headed.add(i)
                stop = next((s for s in stops if s >= end), len(text))
                if _has_term(rc._tokens(text[end:stop]), CLEARANCE_POINT_TERMS):
                    with_point.add(i)
                else:
                    no_point.setdefault(i, {"commune": name, "url": url,
                                            "quote": _row_quote(text, end, stop),
                                            "exclusion": NO_POINT_ON_THE_LIST})
    for i in place_names:
        if i in records and i not in headed:
            no_point[i] = {"commune": _bare_name(ports[i].get("name")), "url": sorted(urls)[0],
                           "quote": "", "exclusion": NO_POINT_ON_THE_LIST}
    return {i: v for i, v in no_point.items() if i not in with_point}


def _is_excluded_row(port: Mapping, texts: Sequence[Mapping] | None,
                     whitelist: Sequence[str] | None, zone: Mapping | None) -> bool:
    """A kind Clément excludes, on a clearance list, in the zone's section. Read from the
    name alone: a row needs no commune for this, so a record without one is capped too."""
    from app.agents import registry_check as rc

    name = port.get("name")
    if not excluded_kind(name):
        return False
    own = zone_terms(port, zone)
    other = _other_territory_terms(own)
    words = tuple(t for t, _, _ in rc._tokens(name))
    for text in _official_texts(texts, whitelist):
        tokens = rc._tokens(text)
        if is_clearance_list(tokens):
            sections = _zone_sections(text, own, other)
            if any(_in_zone_section(sections, start)
                   for start, _ in rc.name_spans(text, tokens, words)):
                return True
    return False


def clearance_row_mention(name, text: str | None, mrgid: int | None = None,
                          places: tuple = ()) -> dict | None:
    """{matched_name, gap, snippet} when the port is a clearance point of a clearance list,
    in the zone's section. The list designates, not the row's wording: "Port de plaisance
    communautaire de l'Étang Z'abricots Bureau du Port : ZAC de l'Etang Z'Abricots" has no
    entry term of its own, and the list's title is not in its text, so ``entry_mention`` finds
    a row only when another row's "capitainerie" or "douanes" happens to be near. Evidence for
    the judge, which decides; a row alone lifts nothing (``entry_mentions`` with ``rows=False``). A
    row that is not a clearance point gets none: it can't reach C2 (§10.2)."""
    from app.agents import registry_check as rc

    if (not isinstance(text, str) or not name or is_name_fragment(name)
            or clearance_point_exclusion(name)):
        return None
    tokens = rc._tokens(text)
    if not is_clearance_list(tokens):
        return None
    own = zone_terms({"mrgid": mrgid, "zone_aliases": list(places)})
    sections = _zone_sections(text, own, _other_territory_terms(own))
    words = tuple(t for t, _, _ in rc._tokens(name))
    for start, end in rc.name_spans(text, tokens, words):
        if not _in_zone_section(sections, start):
            continue
        lo = max(0, start - max(0, rc.SNIPPET_CHARS - (end - start)) // 2)
        return dict(matched_name=" ".join(words), gap=0,
                    snippet=text[lo:lo + rc.SNIPPET_CHARS].strip())
    return None


def geo_in_zone(port: Mapping) -> bool:
    """Placed in this zone by the pipeline's existing polygon check (8face05,
    poe_zone_label.placed_in_zone): coordinates are only written for a point kept in
    this mrgid."""
    from app.services.poe_zone_label import placed_in_zone

    return placed_in_zone(dict(port))


def zone_attributable(port: Mapping, *, whitelist: Sequence[str] | None = None,
                      texts: Sequence[Mapping] | None = None,
                      zone: Mapping | None = None) -> bool:
    """C2 guard: this port's evidence belongs to this zone.

    True when the pipeline placed the port in this zone (8face05: a GPS outside the
    mrgid is never written, see `geo_in_zone`), or an evidence mention in an official
    text sits in a row/section naming this zone's territory. A point kept without a
    polygon check (zone without geometry) is not a placement. Evidence mentions:
    entry-context mentions, the verified judge excerpt and, when LLM∩NER agree, any
    mention of the name. Mentions under another territory's row of a national decree
    do not count.
    """
    if geo_in_zone(port):
        return True
    own = zone_terms(port, zone)
    if not own:
        return False
    other = _other_territory_terms(own)
    official = _official_texts(texts, whitelist)
    name = port.get("name")
    places = _places(port, zone)
    mentions = entry_mentions(name, official, places)
    if port.get("extraction_agreement") is True:
        mentions += all_mentions(name, official, places)
    if any(_mention_in_zone(t, s, e, own, other) for t, s, e in mentions):
        return True
    judge = port.get("judge") if isinstance(port.get("judge"), Mapping) else {}
    excerpt = judge.get("quoted_excerpt")
    if judge_outcome(dict(port)) == JUDGE_SUPPORTED and isinstance(excerpt, str):
        # Located in an official text: the row/section around it decides.
        quote = excerpt.strip().lower()
        for text in official:
            at = text.lower().find(quote)
            if at >= 0 and _mention_in_zone(text, at, at + len(quote), own, other):
                return True
        # Otherwise the excerpt itself must name this zone and no other territory.
        hits = _territory_hits(excerpt, own, other)
        if any(h[2] for h in hits) and not any(not h[2] for h in hits):
            return True
    # Prototype entity: evidence quotes are the rows.
    for e in _evidence(dict(port)):
        quote = e.get("quote")
        if isinstance(quote, str) and quote.strip():
            hits = _territory_hits(quote, own, other)
            if any(h[2] for h in hits) and not any(not h[2] for h in hits):
                return True
    return False


# Words that are never a port name on their own (articles, saint, port types).
GENERIC_NAME_WORDS = frozenset({
    "saint", "sainte", "st", "ste", "san", "santa", "santo", "sao", "são", "port", "porto",
    "puerto", "porte", "harbour", "harbor", "marina", "baie", "bay", "bahia", "bahía",
    "ile", "île", "isla", "island", "islands", "iles", "îles", "cap", "cape", "cabo", "pointe",
    "point", "anse", "le", "la", "les", "el", "los", "las", "de", "du", "des", "the", "of",
    "north", "south", "east", "west", "nord", "sud", "est", "ouest", "interior", "city",
    "town", "ville", "village", "customs", "douane", "dock", "quai", "wharf",
})
_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)


def is_name_fragment(name) -> bool:
    """An obvious fragment, not a port name: empty, a single generic word ("Saint",
    "Port"), or a single word cut at its start ("nterior" from "Interior").

    Multi-word names are kept: this rejects only the obvious cases.
    """
    if not isinstance(name, str) or not name.strip():
        return True
    tokens = _TOKEN.findall(name)
    if not tokens:
        return True
    if len(tokens) > 1:
        return False
    word = tokens[0]
    if len(word) < 3 or word.casefold() in GENERIC_NAME_WORDS:
        return True
    # A lone word starting lower-case is a token cut from a longer one.
    return word[0].islower() and name.strip()[0].islower()


JUDGE_SUPPORTED = "supported"
JUDGE_CONTRADICTED = "contradicted"


def judge_outcome(port: dict) -> str | None:
    """Read the NemotronJudge result (`agents.poe_judge`) on a port.

    port.judge = {verdict: poe | not_poe | unclear, reason, quoted_excerpt,
    excerpt_verified, disagrees}. `excerpt_verified` is computed by
    poe_judge against the official evidence text, not by the model.

    "poe" with a verified, non-empty excerpt -> supported (lifts to C2).
    "not_poe" -> contradicted (caps the level at C1). Anything else,
    including "poe" without a verified excerpt and "unclear" -> None.
    """
    judge = port.get("judge")
    if not isinstance(judge, dict):
        return None
    verdict = str(judge.get("verdict") or "").strip().lower()
    if verdict == "not_poe":
        return JUDGE_CONTRADICTED
    excerpt = judge.get("quoted_excerpt")
    if (verdict == "poe" and judge.get("excerpt_verified") is True
            and isinstance(excerpt, str) and excerpt.strip()):
        return JUDGE_SUPPORTED
    return None


def _lifts_above_c1(port: dict, whitelist: Sequence[str] | None,
                    texts: Sequence[Mapping] | None = None,
                    zone: Mapping | None = None) -> bool:
    if (judge_outcome(port) == JUDGE_SUPPORTED
            and not judge_excerpt_in_study(port, texts, whitelist)
            and not judge_excerpt_on_other_row(port, texts, whitelist)):
        return True
    return _corroborated_llm_extraction(port, whitelist, texts, zone)


def _cap_zone(result: dict, port: dict, whitelist: Sequence[str] | None,
              texts: Sequence[Mapping] | None, zone: Mapping | None) -> dict:
    """C2+ whose evidence is not attributable to this zone goes down to C1."""
    lvl = result["confidence_level"]
    if _RANK[lvl] <= _RANK["C1"]:
        if lvl == "C1" and "confidence_cap_reason" not in result:
            if _study_only(port, texts, whitelist, zone):
                result = dict(result)
                result["confidence_cap_reason"] = CAP_NOT_DESIGNATING
            elif _corroborated_without_context(port, whitelist, texts, zone):
                result = dict(result)
                result["confidence_cap_reason"] = CAP_NO_ENTRY_CONTEXT
        return result
    if zone_attributable(port, whitelist=whitelist, texts=texts, zone=zone):
        return result
    out = dict(result)
    out["confidence_level"] = "C1"
    out.setdefault("downgraded_from", lvl)
    out["confidence_cap_reason"] = CAP_NOT_IN_ZONE
    return out


def _cap_cargo_list(result: dict, port: dict, whitelist: Sequence[str] | None,
                    texts: Sequence[Mapping] | None) -> dict:
    """C2+ that rests on the cargo ICS list goes down to C1, whatever the judge said."""
    lvl = result["confidence_level"]
    if _RANK[lvl] <= _RANK["C1"] or not _cargo_list_only(port, texts, whitelist):
        return result
    out = dict(result)
    out["confidence_level"] = "C1"
    out.setdefault("downgraded_from", lvl)
    out["confidence_cap_reason"] = CAP_CARGO_LIST
    return out


def _cap_clearance_row(result: dict, port: dict, whitelist: Sequence[str] | None,
                       texts: Sequence[Mapping] | None, zone: Mapping | None) -> dict:
    """C2+ on a row that is not a clearance point goes down to C1, whatever the judge said
    (Clément, 3 Nov 2026, §10.2)."""
    lvl = result["confidence_level"]
    if _RANK[lvl] <= _RANK["C1"] or not _is_excluded_row(port, texts, whitelist, zone):
        return result
    out = dict(result)
    out["confidence_level"] = "C1"
    out.setdefault("downgraded_from", lvl)
    out["confidence_cap_reason"] = CAP_NOT_CLEARANCE_POINT
    return out


def _cap(result: dict, port: dict) -> dict:
    """A "not_poe" verdict contradicts the evidence: the level goes down to C1."""
    if judge_outcome(port) != JUDGE_CONTRADICTED:
        return result
    lvl = result["confidence_level"]
    if _RANK[lvl] <= _RANK["C1"]:
        return result
    out = dict(result)
    out["confidence_level"] = "C1"
    out.setdefault("downgraded_from", lvl)
    out["judge_contradicted"] = True
    return out


def _geo_from_v2(port: dict) -> dict:
    geo = port.get("geo")
    if isinstance(geo, dict) and ("agree" in geo or "independent_sources" in geo):
        return {
            "agree": geo.get("agree"),
            "independent_sources": geo.get("independent_sources", 0),
            "agreement_km": geo.get("agreement_km"),
            "spatial_kind": geo.get("spatial_kind", port.get("spatial_kind")),
        }
    alts = port.get("geocode_alternates") or {}
    n = 0
    if isinstance(alts, dict):
        n = sum(1 for key in ("nominatim", "geonames") if alts.get(key))
    if port.get("geocode_corroboration") and port.get("lat") is not None:
        n = max(n, 2)
    agree = port.get("geocode_agree")
    if agree is True:
        n = max(n, 2)
    km = port.get("geocode_agreement_km")
    if km is None:
        km = port.get("agreement_km")
    return {"agree": agree is True, "independent_sources": n,
            "agreement_km": km, "spatial_kind": port.get("spatial_kind")}


def _from_v2(port: dict, whitelist: Sequence[str] | None,
             texts: Sequence[Mapping] | None = None,
             zone: Mapping | None = None) -> dict:
    """Shape a scored V2 port as a certify entity. Does not mutate `port`."""
    urls = _urls(port)
    synthesis = bool(port.get("from_synthesis"))
    extracted = bool(urls) and not synthesis
    lift = _lifts_above_c1(port, whitelist, texts, zone)
    quote = port.get("quote") if isinstance(port.get("quote"), str) else ""
    page = port.get("page_text") if isinstance(port.get("page_text"), str) else ""
    evidence = []
    for url in urls:
        item = {"url": url, "extracted": extracted}
        if is_official(url, whitelist):
            item["official"] = True
        if lift:
            item["judge"] = "supported"
        if quote.strip():
            item["quote"] = quote
            if page:
                item["page_text"] = page
        evidence.append(item)
    name = port.get("name") if isinstance(port.get("name"), str) else ""
    return {
        "name": name,
        "evidence": evidence,
        "geo": _geo_from_v2(port),
        "human": port.get("human"),
        "page_text": page,
    }


def assess_port(port: dict, *, whitelist: Sequence[str] | None = None,
                pages: Mapping[str, str] | None = None,
                texts: Sequence[Mapping] | None = None,
                zone: Mapping | None = None) -> dict:
    """Map a prototype entity or a V2 scored port, then apply the quote check and the
    caps: judge "not_poe" -> C1, evidence not attributable to this zone -> C1, evidence from
    the cargo ICS list -> C1, a row of a clearance list that is not a clearance point -> C1.

    texts: the zone's fetched documents [{url, text}] (only whitelisted URLs are read),
    used for the per-port entry context and the row/section zone check. zone: the VLIZ
    zone ({mrgid, name, ...}); defaults to the port's own mrgid / zone_name.
    """
    if not isinstance(port, dict):
        return {"confidence_level": "C0"}
    if is_name_fragment(port.get("name")):
        # "Saint" from "Saint Lucia", "nterior": not a port, never certified.
        return {"confidence_level": "C0", "name_fragment": True}
    if isinstance(port.get("evidence"), list):
        result = _cap(assess_level(port, pages, whitelist=whitelist), port)
    else:
        entity = _from_v2(port, whitelist, texts, zone)
        before = level(entity)
        checked = _with_verified_quotes(entity, pages)
        result = _cap(_pack(before, level(checked), checked), port)
    result = _cap_zone(result, port, whitelist, texts, zone)
    result = _cap_cargo_list(result, port, whitelist, texts)
    return _cap_clearance_row(result, port, whitelist, texts, zone)


def level_from_v2(port: dict, *, whitelist: Sequence[str] | None = None,
                  pages: Mapping[str, str] | None = None,
                  texts: Sequence[Mapping] | None = None,
                  zone: Mapping | None = None) -> str:
    """C0–C4 for a pipeline port or a prototype entity.

    A whitelisted (State) host is required for C1+. An LLM-only reading (single extractor,
    or two LLM readers) and a catalogue-only port never go above C1. A judge "poe"
    with excerpt_verified can, and so can an LLM extraction with LLM∩NER agreement or two
    official hosts, plus a per-port entry context; a judge "not_poe" caps at C1, and so do
    evidence not attributable to this zone, evidence from the cargo ICS list and a
    clearance-list row that is not a clearance point. A name fragment is C0.
    `human == "confirmed"` lifts only C3 to C4.
    """
    return assess_port(port, whitelist=whitelist, pages=pages, texts=texts,
                       zone=zone)["confidence_level"]


def attach_confidence_level(port: dict, *, whitelist: Sequence[str] | None = None,
                            pages: Mapping[str, str] | None = None,
                            texts: Sequence[Mapping] | None = None,
                            zone: Mapping | None = None) -> dict:
    """Set `confidence_level` on a port. Does not change the numeric score."""
    try:
        assessed = assess_port(port, whitelist=whitelist, pages=pages, texts=texts,
                               zone=zone)
    except Exception:
        port["confidence_level"] = "C0"
        return port
    port["confidence_level"] = assessed["confidence_level"]
    for flag in ("judge_contradicted", "name_fragment"):
        if assessed.get(flag):
            port[flag] = True
        else:
            port.pop(flag, None)
    if assessed.get("downgraded_from"):
        port["confidence_downgraded_from"] = assessed["downgraded_from"]
    else:
        port.pop("confidence_downgraded_from", None)
    if assessed.get("confidence_cap_reason"):
        port["confidence_cap_reason"] = assessed["confidence_cap_reason"]
    else:
        port.pop("confidence_cap_reason", None)
    if "quote_verified" in assessed:
        port["quote_verified"] = assessed["quote_verified"]
    else:
        port.pop("quote_verified", None)
    return port
