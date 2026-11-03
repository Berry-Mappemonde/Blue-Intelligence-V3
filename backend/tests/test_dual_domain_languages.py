"""Dual languages and dual domains (Clément's plan, BI-V3 implementation plan, 3 Nov).
A zone searches in its own language, its sovereign's, and English; an overseas territory
searches its own ccTLD and its sovereign's domains (.mq + .fr/gouv.fr, .vg + .gov.uk,
.pr/.vi + .gov). Search only: the whitelist, and
so what counts as the territory's authority (never_attach_to_sovereign, C2), is unchanged.
Offline: every search provider is stubbed, no Mongo.
"""
import asyncio

import pytest

from app.core.run_rules import bind_rules, catalog_default, reset_rules, snapshot_for_run
from app.services import poe_pipeline as poe

SX = {"mrgid": 21803, "name": "Sint-Maarten", "geoname": "Sint-Maarten", "iso2": "SX",
      "sov_iso2": "NL", "sovereign": "Netherlands"}
MQ = {"mrgid": 33178, "name": "Martinique", "geoname": "Martinique", "iso2": "MQ",
      "sov_iso2": "FR", "sovereign": "France"}
VG = {"mrgid": 8411, "name": "British Virgin Islands", "iso2": "VG", "sov_iso2": "GB",
      "sovereign": "United Kingdom"}
PR = {"mrgid": 33179, "name": "Puerto Rico", "iso2": "PR", "sov_iso2": "US",
      "sovereign": "United States"}
VI = {"mrgid": 33180, "name": "United States Virgin Islands", "iso2": "VI", "sov_iso2": "US",
      "sovereign": "United States"}
CX = {"mrgid": 8309, "name": "Christmas Island", "iso2": "CX", "sov_iso2": "AU",
      "sovereign": "Australia"}
NU = {"mrgid": 8447, "name": "Niue", "iso2": "NU", "sov_iso2": "NZ", "sovereign": "New Zealand"}
LC = {"mrgid": 8416, "name": "Saint Lucia", "iso2": "LC", "sov_iso2": "LC", "sovereign": "Saint Lucia"}
CANARIES = {"mrgid": 8364, "name": "Canary Islands", "iso2": "ES", "sov_iso2": "ES",
            "sovereign": "Spain"}


CV = {"mrgid": 8362, "name": "Cape Verde", "iso2": "CV", "sov_iso2": "CV", "sovereign": "Cape Verde"}


def _rules(**overrides):
    return bind_rules(snapshot_for_run(mode="formalities", overrides=overrides))


# --- zone_search_languages ----------------------------------------------------------

@pytest.mark.parametrize("zone, langs", [
    (MQ, ["fr", "en"]),
    (CV, ["pt", "en"]),
    (CANARIES, ["es", "en"]),
    (LC, ["en"]),
    (SX, ["en"]),   # autonomous: _POLYGON_SEARCH_LANG keeps SX English, not Dutch
])
def test_zone_search_languages(zone, langs):
    assert poe.zone_search_languages(zone) == langs
    assert poe.zone_search_lang(zone) == (langs[0] if langs[0] != "en" else None)


def test_languages_fill_the_sovereign_and_keep_the_order():
    bare = {k: v for k, v in MQ.items() if k != "sov_iso2"}
    assert poe.zone_search_languages(bare) == ["fr", "en"]
    # a polygon whose language differs from its sovereign's: local, sovereign, en
    mixed = {"mrgid": 990201, "name": "Testland", "iso2": "CV", "sov_iso2": "FR"}
    assert poe.zone_search_languages(mixed) == ["pt", "fr", "en"]
    assert [l for l, _ in poe.google_style_shots(mixed)] == ["en", "pt", "fr"]
    hints = poe.default_search_hints(mixed)
    assert hints[0].startswith("Testland portos habilitados") and "ports d'entrée officiels" in hints[2]
    assert hints[-1].endswith("customs act designated ports of entry official legislation gazette")
    lessons = poe.lessons_learned_query(mixed)
    assert poe.LOCAL_SEARCH_TERMS["pt"]["lessons"] in lessons
    assert poe.LOCAL_SEARCH_TERMS["fr"]["lessons"] in lessons


PR_ES = {"mrgid": 33179, "name": "Puerto Rico", "iso2": "PR", "sov_iso2": "US",
         "sovereign": "United States"}
GALAPAGOS = {"mrgid": 8403, "name": "Galapagos", "iso2": "EC", "sov_iso2": "EC",
             "sovereign": "Ecuador"}
NC = {"mrgid": 8312, "name": "New Caledonia", "iso2": "NC", "sov_iso2": "FR", "sovereign": "France"}


def test_puerto_rico_searches_in_spanish_too():
    assert poe.LANG_BY_ISO2["PR"] == "es"
    assert poe.zone_search_languages(PR_ES) == ["es", "en"]
    assert [l for l, _ in poe.google_style_shots(PR_ES)] == ["en", "es"]


@pytest.mark.parametrize("zone, lang, name", [
    (CV, "pt", "Cabo Verde"),              # pycountry ISO 3166-1, whole-country polygon
    (CANARIES, "es", "Islas Canarias"),    # territories.json name_es
    (NC, "fr", "Nouvelle-Calédonie"),      # territories.json name_fr
    (MQ, "fr", "Martinique"),
    (GALAPAGOS, "es", "Galapagos"),        # a polygon of Ecuador: never "Ecuador"
    (CV, "en", "Cape Verde"),              # English keeps the English name
    (MQ, "es", "Martinique"),              # curated record without name_es: fallback
])
def test_local_place_name(zone, lang, name):
    assert poe.local_place_name(zone, lang) == name


def test_local_language_queries_use_the_local_name():
    shots = dict(poe.google_style_shots(CV))
    assert shots["pt"].startswith("Cabo Verde ") and shots["en"].startswith("Cape Verde ")
    hints = poe.default_search_hints(CANARIES)
    assert hints[0] == "Islas Canarias puertos habilitados decreto lista oficial"
    assert hints[-1].startswith("Canary Islands customs act")  # the EN legal hint stays English
    assert "Islas Canarias" in poe.localized_query(CANARIES)


# --- caps as run rules ---------------------------------------------------------------

def test_caps_are_run_rules_with_unchanged_defaults():
    assert catalog_default("formalities.search_hint_queries_max") == poe.SEARCH_HINT_QUERIES_MAX == 5
    assert catalog_default("formalities.site_search_domains") == poe.SITE_SEARCH_DOMAINS_MAX == 3
    mixed = {"mrgid": 990201, "name": "Testland", "iso2": "CV", "sov_iso2": "FR"}
    assert len(poe.search_hint_queries(mixed)) == 5
    token = _rules(**{"formalities.search_hint_queries_max": 3,
                      "formalities.site_search_domains": 1})
    try:
        assert len(poe.search_hint_queries(mixed)) == 3
        assert poe._site_hops(["a.sx", "b.sx", "c.sx"], SX) == [("a.sx", False),
                                                               ("douane.nl", True)]
    finally:
        reset_rules(token)
    assert len(poe._site_hops(["a.sx", "b.sx", "c.sx", "d.sx"], SX)) == 6


# --- priority: sovereign_runs_entry ---------------------------------------------------

def test_sovereign_runs_entry_puts_the_sovereign_domains_first():
    assert poe.sovereign_first(MQ) and not poe.sovereign_first(SX) and not poe.sovereign_first(VG)
    hops = poe._site_hops(["martinique.gouv.fr"], MQ)
    assert [d for d, sov in hops if sov] == ["gouv.fr", "douane.gouv.fr", "service-public.fr"]
    assert hops[0] == ("gouv.fr", True) and hops[-1] == ("martinique.gouv.fr", False)
    wl = poe.zone_whitelist(MQ)
    scoped = poe.scoped_search_domains(wl, MQ)
    assert scoped[0] == "gouv.fr"
    # SX (never attached): its own domains first, the Dutch ones after
    assert poe._site_hops(["sintmaartengov.org"], SX)[0] == ("sintmaartengov.org", False)


# --- per sovereign -----------------------------------------------------------------

@pytest.mark.parametrize("zone, domains, tokens", [
    (MQ, ["gouv.fr", "douane.gouv.fr", "service-public.fr"], ".mq .fr"),   # France
    (VG, ["gov.uk"], ".vg .gov.uk"),                                      # United Kingdom
    (PR, ["gov", "cbp.gov"], ".pr .gov"),                                 # United States
    (VI, ["gov", "cbp.gov"], ".vi .gov"),
    (CX, ["gov.au", "abf.gov.au"], ".cx .gov.au"),                        # Australia
    (NU, ["govt.nz"], ".nu .govt.nz"),                                    # New Zealand
    (SX, ["douane.nl", "government.nl", "rijksoverheid.nl"], ".sx .nl"),  # Netherlands
])
def test_overseas_territory_gets_its_sovereign_domains(zone, domains, tokens):
    assert poe.sovereign_search_domains(zone) == domains
    assert poe.google_style_tld_tokens(zone) == tokens
    assert tokens in poe.google_style_query(zone)


@pytest.mark.parametrize("zone, tokens", [(LC, ".lc"), (CANARIES, ".es")])
def test_a_zone_that_is_its_own_sovereign_is_unchanged(zone, tokens):
    assert poe.sovereign_search_domains(zone) == []
    assert poe.google_style_tld_tokens(zone) == tokens
    wl = poe.zone_whitelist(zone)
    assert poe.scoped_search_domains(wl, zone) == wl[:8]


def test_missing_sov_iso2_is_filled_from_territories_json():
    bare = {k: v for k, v in MQ.items() if k != "sov_iso2"}
    assert poe.sovereign_search_domains(bare) == poe.sovereign_search_domains(MQ)
    assert poe.google_style_tld_tokens(bare) == ".mq .fr"


@pytest.mark.parametrize("zone", [SX, VG, PR, NU])
def test_search_only_the_whitelist_is_unchanged(zone):
    """never_attach_to_sovereign (SX, NU) and non-curated zones (VG, PR) keep their own
    whitelist: the sovereign's domains are searched, never accepted as the zone's."""
    wl = poe.zone_whitelist(zone)
    assert not set(poe.sovereign_search_domains(zone)) & set(wl)
    scoped = poe.scoped_search_domains(wl, zone)
    assert scoped[:len(wl[:8])] == wl[:8]
    assert set(poe.sovereign_search_domains(zone)) <= set(scoped)


def test_sovereign_site_query_names_the_territory():
    hops = poe._site_hops(["gov.sx"], SX)
    assert hops[0] == ("gov.sx", False)
    assert ("government.nl", True) in hops
    q = poe._sovereign_site_query(poe.site_list_pdf_query("government.nl", SX), SX)
    assert q.startswith("site:government.nl filetype:pdf") and q.endswith('"Sint-Maarten"')


def test_document_type_names_are_in_the_queries():
    assert '"points de clearance agréés"' in poe.site_list_pdf_query("gouv.fr", MQ)
    assert '"points de clearance agréés"' in poe.site_list_page_query("gouv.fr", MQ)
    assert '"boarding stations"' in poe.site_list_pdf_query("gov.lc", LC)
    assert '"clearance stations"' in poe.site_list_page_query("gov.lc", LC)
    assert "boarding stations" in poe.lessons_learned_query(LC)


# --- end to end through _find_sources (SX) ------------------------------------------

class _Rec:
    def __init__(self):
        self.events = []

    async def event(self, step, **payload):
        self.events.append((step, payload))


@pytest.fixture
def providers(monkeypatch):
    calls = {"searxng": [], "tavily": []}
    answers = {"searxng": lambda q: []}

    async def fake_searx(query, log):
        calls["searxng"].append(query)
        return [{"url": u, "domain": poe.domain_of(u), "engine": "searxng"}
                for u in answers["searxng"](query)]

    async def fake_tavily(query, log, *, include_domains=None, search_depth="basic",
                          max_results=10):
        calls["tavily"].append({"query": query, "include_domains": include_domains})
        return []

    async def no_key(*a, **k):
        return ""

    async def empty(*a, **k):
        return []

    async def fake_grounded(zone, whitelist, log, query_override=None):
        return [], None

    monkeypatch.setattr(poe, "search_searxng", fake_searx)
    monkeypatch.setattr(poe, "_tavily_search_safe", fake_tavily)
    monkeypatch.setattr(poe, "_tavily_key", lambda: "test-key")
    monkeypatch.setattr(poe, "_resolve_tf_key", no_key)
    monkeypatch.setattr(poe, "_resolve_serper_key", no_key)
    monkeypatch.setattr(poe, "_serper_search_safe", empty)
    monkeypatch.setattr(poe, "search_grounded", fake_grounded)
    monkeypatch.setattr(poe, "search_hint_queries", lambda zone, exceptions=None: [])
    monkeypatch.setattr(poe, "save_exceptions", lambda exc: None)
    return calls, answers


def _find(zone, logs=None, rec=None):
    exc = poe.load_exceptions()
    wl = poe.zone_whitelist(zone, exc)
    log = logs.append if logs is not None else (lambda m: None)
    official, _s, _y = asyncio.run(poe._find_sources(zone, wl, exc, log, rec=rec or _Rec(),
                                                     tf_key="", variant="v2"))
    return [c["url"] for c in official], wl


def test_search_volume_is_logged_per_zone(providers):
    calls, answers = providers
    answers["searxng"] = lambda q: [] if q.startswith("site:") else [
        "https://www.sintmaartengov.org/government/customs"]
    logs, rec = [], _Rec()
    _find(SX, logs, rec)
    (line,) = [m for m in logs if m.startswith("volume de recherche:")]
    (ev,) = [p for step, p in rec.events if step == "search_volume"]
    assert ev["site_hops"] == sum(q.startswith("site:") for q in calls["searxng"])
    assert ev["sovereign_site_hops"] == 6  # 3 Dutch domains × (PDF + page)
    assert ev["queries"] > 0 and ev["languages"] == ["en"]
    assert f"{ev['site_hops']} saut(s) site: dont 6 sur domaines souverains" in line


def test_sx_searches_dutch_state_domains_but_never_counts_them(providers):
    calls, answers = providers
    nl_page = "https://www.government.nl/topics/caribbean-parts-of-the-kingdom/sint-maarten"

    def searx(q):
        if "site:government.nl" in q:
            return [nl_page]
        if q.startswith("site:"):
            return []
        return ["https://www.sintmaartengov.org/government/customs"]

    answers["searxng"] = searx
    official, wl = _find(SX)
    site_q = [q for q in calls["searxng"] if q.startswith("site:")]
    assert any(q.startswith("site:sintmaartengov.org") for q in site_q)  # own domain first
    assert any(q.startswith("site:government.nl") and '"Sint-Maarten"' in q for q in site_q)
    # scoped Tavily = the whitelist first, then the sovereign's domains (the one-domain
    # calls are Tavily standing in for an empty site: hop)
    scoped = [c["include_domains"] for c in calls["tavily"]
              if c["include_domains"] and c["include_domains"][:len(wl[:8])] == wl[:8]]
    assert scoped and all({"douane.nl", "government.nl", "rijksoverheid.nl"} <= set(inc)
                          for inc in scoped)
    # never_attach_to_sovereign: the Dutch page was found but is not the zone's authority
    assert nl_page not in official
    assert "https://www.sintmaartengov.org/government/customs" in official


def test_martinique_sovereign_page_counts_because_france_runs_entry(providers):
    """sovereign_runs_entry (Martinique): gouv.fr is already in the zone's whitelist, so
    a page found by the sovereign hop is official; #29's rules decide, not the search."""
    calls, answers = providers
    page = "https://www.douane.gouv.fr/martinique-points-de-passage.pdf"
    answers["searxng"] = lambda q: [page] if q.startswith("site:gouv.fr") else (
        [] if q.startswith("site:") else ["https://www.martinique.gouv.fr/plaisance"])
    official, wl = _find(MQ)
    assert "gouv.fr" in wl
    assert any(q.startswith("site:gouv.fr") and '"Martinique"' in q for q in calls["searxng"])
    assert page in official


def test_overseas_territory_with_no_official_hit_still_runs_the_sovereign_hops(providers):
    calls, answers = providers
    answers["searxng"] = lambda q: []
    _find(VG)
    assert any(q.startswith("site:gov.uk") and '"British Virgin Islands"' in q
               for q in calls["searxng"])


# --- gate: a list already found closes the own-domain hops, never the sovereign's ----
# Run 20261103-143422-608d85: search_volume showed 0 sovereign site: hops everywhere and
# 0 site: hops for Martinique. France's national pleasure-port PDF on douane.gouv.fr
# (list_url_bonus 0.8) closed both hops, gouv.fr included.
FR_LIST_PDF = ("https://www.douane.gouv.fr/sites/default/files/2026-07/06/"
               "Liste-ports-de-plaisance-eligibles.pdf")


def test_list_found_still_runs_the_sovereign_hops_of_an_overseas_territory(providers):
    calls, answers = providers
    answers["searxng"] = lambda q: [] if q.startswith("site:") else [
        FR_LIST_PDF, "https://www.martinique.gouv.fr/Actions-de-l-Etat/Mer/plaisance"]
    assert poe.list_url_bonus(FR_LIST_PDF) >= 0.5
    logs, rec = [], _Rec()
    _find(MQ, logs, rec)
    site_q = [q for q in calls["searxng"] if q.startswith("site:")]
    sov = poe.sovereign_search_domains(MQ)
    # only the sovereign's hops, PDF and page, each naming Martinique
    assert site_q and all(any(q.startswith(f"site:{d} ") for d in sov) for q in site_q)
    assert all('"Martinique"' in q for q in site_q)
    assert len(site_q) == 2 * len(sov)
    (ev,) = [p for step, p in rec.events if step == "search_volume"]
    assert ev["sovereign_site_hops"] == ev["site_hops"] == 2 * len(sov)
    assert any("hops propres sautés" in m for m in logs)


def test_list_found_still_closes_the_hops_of_a_zone_that_is_its_own_sovereign(providers):
    calls, answers = providers
    answers["searxng"] = lambda q: [] if q.startswith("site:") else [
        "https://www.govt.lc/media.govt.lc/www/resources/legislation/CustomsAct.pdf"]
    logs, rec = [], _Rec()
    _find(LC, logs, rec)
    assert not [q for q in calls["searxng"] if q.startswith("site:")]
    (ev,) = [p for step, p in rec.events if step == "search_volume"]
    assert ev["site_hops"] == 0 and ev["sovereign_site_hops"] == 0
