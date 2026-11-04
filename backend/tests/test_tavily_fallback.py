"""Tavily stands in for SearXNG when SearXNG returns nothing; scoped Tavily below
tavily.scoped_min_official_hosts; curated territories get their sovereign's keys.

Pilot of 2 Nov 2026 (runs 20261102-175449-395249, 20261102-184320-df37fb): with SearXNG's
engines suspended, Saint Lucia and Cape Verde searched with Tavily's two first-round
queries only, and the Canary Islands searched without Spanish queries. Offline: every
search provider is stubbed, no Mongo.
"""
import asyncio

import httpx
import pytest

from app.core.run_rules import bind_rules, reset_rules
from app.services import poe_pipeline as poe
from app.services.territory_ref import with_sovereign_iso2

SAINT_LUCIA = {"mrgid": 8416, "name": "Saint Lucia", "geoname": "Saint Lucia", "iso2": "LC",
               "sov_iso2": "LC", "sovereign": "Saint Lucia"}
HINTS = ["Saint Lucia customs pleasure craft clearance", "Saint Lucia port of entry order"]


class _Rec:
    def __init__(self):
        self.events = []

    async def event(self, step, **payload):
        self.events.append((step, payload))


def _hit(url, engine):
    return {"url": url, "domain": poe.domain_of(url), "engine": engine}


@pytest.fixture
def providers(monkeypatch):
    """Stub every engine; SearXNG / Tavily answers are set per test."""
    calls = {"searxng": [], "tavily": [], "grounded": 0}
    answers = {"searxng": lambda q: [], "tavily": lambda q, inc: []}

    async def fake_searx(query, log):
        calls["searxng"].append(query)
        return list(answers["searxng"](query))

    async def fake_tavily(query, log, *, include_domains=None, search_depth="basic",
                          max_results=10):
        calls["tavily"].append({"query": query, "include_domains": include_domains})
        return list(answers["tavily"](query, include_domains))

    async def fake_grounded(zone, whitelist, log, query_override=None):
        calls["grounded"] += 1
        return [], None

    async def no_key(*a, **k):
        return ""

    async def no_serper(query, key, log, gl=None, hl="en"):
        return []

    monkeypatch.setattr(poe, "search_searxng", fake_searx)
    monkeypatch.setattr(poe, "_tavily_search_safe", fake_tavily)
    monkeypatch.setattr(poe, "_tavily_key", lambda: "test-key")
    monkeypatch.setattr(poe, "_resolve_tf_key", no_key)
    monkeypatch.setattr(poe, "_resolve_serper_key", no_key)
    monkeypatch.setattr(poe, "_serper_search_safe", no_serper)
    monkeypatch.setattr(poe, "search_grounded", fake_grounded)
    monkeypatch.setattr(poe, "search_hint_queries", lambda zone, exceptions=None: list(HINTS))
    monkeypatch.setattr(poe, "save_exceptions", lambda exc: None)
    return calls, answers


def _find(zone=SAINT_LUCIA):
    rec = _Rec()
    whitelist = poe.zone_whitelist(zone, {})
    official, _strict, _syn = asyncio.run(poe._find_sources(
        zone, whitelist, {}, lambda m: None, rec=rec, tf_key="", variant="tinyfish"))
    return official, whitelist, rec


def test_searxng_empty_tavily_runs_the_full_query_set(providers):
    calls, answers = providers
    # Tavily finds a State page (not a list PDF), so the site: PDF and page steps run too.
    answers["tavily"] = lambda q, inc: [_hit("https://www.customs.gov.lc/about", "tavily")]
    official, _wl, rec = _find()
    assert official and calls["searxng"]
    tavily = {(c["query"], tuple(c["include_domains"] or ())) for c in calls["tavily"]}
    for q in calls["searxng"]:
        if "site:" in q:
            dom = q.split("site:", 1)[1].split()[0]
            assert any(dom in inc and "site:" not in tq for tq, inc in tavily), q
        else:
            assert any(tq == q for tq, _ in tavily), q
    # Pinned hints and the site: lookups reached Tavily only because SearXNG was empty.
    assert {h for h in HINTS} <= {tq for tq, _ in tavily}
    assert any(inc == ("customs.gov.lc",) for _, inc in tavily)
    # Grounded search stays the last resort and was not needed.
    assert calls["grounded"] == 0
    searches = [p for s, p in rec.events if s == "search"]
    # So did the list-discovery query of a zone outside Schengen: one more Tavily call.
    fallback = set(HINTS) | {q for _lang, q in poe.entry_list_queries(SAINT_LUCIA)}
    assert len(fallback) == len(HINTS) + 1
    assert {p["query"] for p in searches if p.get("fallback_for") == "searxng"} == fallback
    summary = [p["engines"] for s, p in rec.events if s == "search_providers"]
    assert summary and summary[0]["tavily"]["fallback"] == len(fallback)
    assert summary[0]["searxng"]["results"] == 0


def test_searxng_answering_keeps_tavily_out_of_the_searxng_only_steps(providers):
    calls, answers = providers
    answers["searxng"] = lambda q: [_hit("https://www.customs.gov.lc/x", "searxng"),
                                    _hit("https://www.govt.lc/y", "searxng")]
    _official, whitelist, _rec = _find()
    # Tavily keeps its parallel calls (first round; lessons round, scoped on the whitelist,
    # PR #8) but takes no SearXNG-only query: no hint, no single-domain site: lookup.
    assert set(HINTS).isdisjoint(c["query"] for c in calls["tavily"])
    assert all(c["include_domains"] in (None, whitelist[:8]) for c in calls["tavily"])


def _scoped(calls, whitelist):
    return [c for c in calls["tavily"] if c["include_domains"] == whitelist[:8]]


# A fixture zone with exactly one official host: the test no longer depends on Saint
# Lucia's live whitelist (#39 gave LC five hosts, stlucia.org among them).
ONE_HOST_ZONE = {"mrgid": 990101, "name": "Testland", "geoname": "Testland", "iso2": "TL",
                 "sov_iso2": "TL", "sovereign": "Testland"}
ONE_HOST_WHITELIST = ["gov.testland.example"]


def test_one_official_host_runs_scoped_tavily(providers):
    calls, answers = providers
    answers["searxng"] = lambda q: [_hit("https://www.gov.testland.example/gazette.pdf", "searxng"),
                                    _hit("https://www.visit-testland.example/en/", "searxng")]
    rec = _Rec()
    asyncio.run(poe._find_sources(ONE_HOST_ZONE, list(ONE_HOST_WHITELIST), {}, lambda m: None,
                                  rec=rec, tf_key="", variant="tinyfish"))
    assert len(_scoped(calls, ONE_HOST_WHITELIST)) == 1


def test_two_official_hosts_skip_scoped_tavily(providers):
    calls, answers = providers
    answers["searxng"] = lambda q: [_hit("https://npc.govt.lc/gazette.pdf", "searxng"),
                                    _hit("https://www.customs.gov.lc/act.pdf", "searxng")]
    _official, whitelist, _rec = _find()
    assert _scoped(calls, whitelist) == []


def test_rule_at_one_keeps_the_old_trigger(providers):
    calls, answers = providers
    answers["searxng"] = lambda q: [_hit("https://npc.govt.lc/gazette.pdf", "searxng")]
    token = bind_rules({"chosen": {"tavily.scoped_min_official_hosts": {"value": 1}}})
    try:
        _official, whitelist, _rec = _find()
    finally:
        reset_rules(token)
    assert _scoped(calls, whitelist) == []


def test_fallback_keeps_the_credit_cap_and_sends_nothing_at_zero(monkeypatch, tmp_path):
    # The fallback goes through core.tavily.tavily_search: cache first, then the run's
    # credit reservation. At tavily.max_credits = 0 no request leaves the process.
    from app.core import tavily

    async def empty_searx(query, log):
        return []

    async def no_post(*a, **k):
        raise AssertionError("Tavily HTTP call despite tavily.max_credits = 0")

    monkeypatch.setattr(poe, "search_searxng", empty_searx)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setenv("TAVILY_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(httpx.AsyncClient, "post", no_post)
    token = bind_rules({"chosen": {"tavily.max_credits": {"value": 0}}})
    try:
        # The ledger outside a run is process-wide: compare before / after this call.
        before = tavily.credits_used()
        res, engine = asyncio.run(poe._search_with_fallback(
            "site:customs.gov.lc filetype:pdf ports of entry", lambda m: None))
        assert res == [] and engine == "tavily"
        assert tavily.credits_used() == before
    finally:
        reset_rules(token)


def test_canary_islands_search_in_spanish_from_the_territory_record():
    stored = {"mrgid": 8364, "name": "Canary Islands", "iso2": None, "sovereign": "Spain"}
    assert poe.zone_search_lang(stored) is None
    zone = with_sovereign_iso2(stored)
    assert zone["sov_iso2"] == "ES" and "sov_iso2" not in stored
    assert poe.zone_search_lang(zone) == "es"


def test_drom_gets_the_fr_token_and_other_zones_are_left_alone():
    mq = with_sovereign_iso2({"mrgid": 33178, "name": "Martinique", "iso2": "MQ"})
    assert mq["sov_iso2"] == "FR" and ".fr" in poe.google_style_tld_tokens(mq)
    kept = {"mrgid": 8364, "iso2": None, "sov_iso2": "PT"}
    assert with_sovereign_iso2(kept) is kept
    unmapped = {"mrgid": 8416, "iso2": "LC"}
    assert with_sovereign_iso2(unmapped) is unmapped
    never = {"mrgid": 21803, "iso2": "SX"}  # Sint Maarten: never attached to NL
    assert with_sovereign_iso2(never) is never
