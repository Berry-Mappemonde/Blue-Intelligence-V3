"""Audited authority URLs (whitelist_sources.json) fill a role search left empty, and a
role collection gave up gets its audited URL tried once. Offline: every search provider
and every fetch is stubbed, no Mongo.

Canaries pilot (run 20261102-224042-f9be8d): search returned ministry and tax pages,
no port authority, and the fetch cap was spent on them.
"""
import asyncio

import pytest

from app.core.run_rules import bind_rules, reset_rules, snapshot_for_run
from app.services import authority_roles as ar
from app.services import poe_pipeline as poe

CANARIES = {"mrgid": 8364, "name": "Canary Islands", "geoname": "Canary Islands",
            "iso2": "ES", "sov_iso2": "ES", "sovereign": "Spain"}
BARBADOS = {"mrgid": 8418, "name": "Barbados", "geoname": "Barbados", "iso2": "BB",
            "sov_iso2": "BB", "sovereign": "Barbados"}
TENERIFE = "https://www.puertosdetenerife.org/en/port-authority-of-santa-cruz-de-tenerife/"

# What search returns for the Canaries: customs, immigration, maritime, ministry pages.
CANARIES_HITS = [
    "https://www.transportes.gob.es/marina-mercante/nautica-de-recreo",
    "https://www.agenciatributaria.es/AEAT.internet/Aduanas.shtml",
    "https://www.policia.es/_es/extranjeria_fronteras.php",
    "https://www.salvamentomaritimo.es/conocenos",
]
BARBADOS_HITS = ["https://www.gov.bb/Ministries/customs", "https://www.gov.bb/ports-of-entry"]


class _Rec:
    def __init__(self):
        self.events = []

    async def event(self, step, **payload):
        self.events.append((step, payload))


@pytest.fixture
def search(monkeypatch):
    """Every engine stubbed; SearXNG answers ``hits`` for every query."""
    hits: list[str] = []

    async def fake_searx(query, log):
        return [{"url": u, "domain": poe.domain_of(u), "engine": "searxng"} for u in hits]

    async def empty(*a, **k):
        return []

    async def no_key(*a, **k):
        return ""

    async def fake_grounded(zone, whitelist, log, query_override=None):
        return [], None

    monkeypatch.setattr(poe, "search_searxng", fake_searx)
    monkeypatch.setattr(poe, "_tavily_search_safe", empty)
    monkeypatch.setattr(poe, "_tavily_key", lambda: "")
    monkeypatch.setattr(poe, "_resolve_tf_key", no_key)
    monkeypatch.setattr(poe, "_resolve_serper_key", no_key)
    monkeypatch.setattr(poe, "_serper_search_safe", empty)
    monkeypatch.setattr(poe, "search_grounded", fake_grounded)
    monkeypatch.setattr(poe, "search_hint_queries", lambda zone, exceptions=None: [])
    monkeypatch.setattr(poe, "save_exceptions", lambda exc: None)
    return hits


def _find(zone, exceptions=None, overrides=None):
    rec, logs = _Rec(), []
    exc = exceptions if exceptions is not None else poe.load_exceptions()
    token = bind_rules(snapshot_for_run(mode="formalities", overrides=overrides or {}))
    try:
        official, _strict, _syn = asyncio.run(poe._find_sources(
            zone, poe.zone_whitelist(zone, exc), exc, logs.append, rec=rec, tf_key="",
            variant="v2"))
    finally:
        reset_rules(token)
    return [c["url"] for c in official], logs, rec


def _norm(urls):
    return {poe._normalize_url(u) for u in urls}


# --- _find_sources -----------------------------------------------------------------

def test_canaries_without_port_authority_candidate_pins_the_audited_ones(search):
    search[:] = CANARIES_HITS
    urls, logs, rec = _find(CANARIES)
    auths = ar.zone_authorities(CANARIES)
    audited = ar.audited_urls(auths, "port_authority")
    assert poe._normalize_url(TENERIFE) in _norm(urls)
    assert _norm(audited) <= _norm(urls)
    # only the port authority was missing: nothing else is added
    off, _l, _r = _find(CANARIES, overrides={"formalities.authority_seed_fallback": False})
    assert _norm(urls) - _norm(off) == _norm(audited)
    assert any("source d'autorité ajoutée (port_authority sans candidat" in m
               and "puertosdetenerife.org" in m for m in logs)
    (step, payload), = [e for e in rec.events if e[0] == "authority_seed"]
    assert payload["roles"] == ["port_authority"]


def test_audited_urls_go_after_the_existing_seeds(search):
    search[:] = CANARIES_HITS
    exc = poe.load_exceptions()
    seed = "https://www.gobiernodecanarias.org/puertos/entrada-embarcaciones.pdf"
    exc = {**exc, "seed_urls_mrgid": {**(exc.get("seed_urls_mrgid") or {}), "8364": [seed]}}
    urls, _logs, _rec = _find(CANARIES, exc)
    assert urls[0] == seed
    assert urls.index(seed) < min(urls.index(u) for u in urls
                                  if poe.domain_of(u) == "puertosdetenerife.org")


def test_blacklisted_audited_url_is_skipped(search):
    search[:] = CANARIES_HITS
    exc = poe.load_exceptions()
    exc = {**exc, "blacklist_urls_mrgid": {**(exc.get("blacklist_urls_mrgid") or {}),
                                           "8364": [TENERIFE]}}
    urls, logs, _rec = _find(CANARIES, exc)
    assert poe._normalize_url(TENERIFE) not in _norm(urls)
    assert "palmasport.es" in {poe.domain_of(u) for u in urls}
    assert any("source d'autorité écartée" in m and "puertosdetenerife.org" in m for m in logs)


def test_zone_without_audit_is_unchanged(search):
    search[:] = BARBADOS_HITS
    assert not ar.zone_authorities(BARBADOS)
    on, logs, rec = _find(BARBADOS)
    off, _l, _r = _find(BARBADOS, overrides={"formalities.authority_seed_fallback": False})
    assert on == off
    assert not any("source d'autorité" in m for m in logs)
    assert not [e for e in rec.events if e[0] == "authority_seed"]


def test_rule_off_pins_nothing(search):
    search[:] = CANARIES_HITS
    urls, logs, _rec = _find(CANARIES, overrides={"formalities.authority_seed_fallback": False})
    assert "puertosdetenerife.org" not in {poe.domain_of(u) for u in urls}
    assert not any("source d'autorité" in m for m in logs)


# --- _collect_texts ----------------------------------------------------------------

PA_PAGES = ["https://www.palmasport.es/es/tarifas/", "https://www.puertosdetenerife.org/es/tasas/"]
COLLECT = [
    "https://www.transportes.gob.es/marina-mercante",
    "https://www.agenciatributaria.es/AEAT.internet/Aduanas.shtml",
    "https://www.policia.es/_es/extranjeria_fronteras.php",
    "https://www.salvamentomaritimo.es/conocenos",
] + PA_PAGES


def _collect(monkeypatch, failing, overrides=None):
    calls: list[str] = []

    async def fake_cascade(url, min_chars=200, log=None):
        calls.append(url)
        if url in failing:
            raise TimeoutError("read timeout")
        return {"text": "Información oficial. " * 40, "html": "", "md5": url,
                "level": "N1", "blocked": False}

    monkeypatch.setattr(poe, "extract_cascade", fake_cascade)
    official = [{"url": u, "domain": poe.domain_of(u), "official": True} for u in COLLECT]
    logs: list[str] = []
    token = bind_rules(snapshot_for_run(mode="formalities", overrides=overrides or {}))
    try:
        _t, _h, used, _e = asyncio.run(poe._collect_texts(official, logs.append,
                                                           zone=CANARIES))
    finally:
        reset_rules(token)
    return calls, [u["url"] for u in used], logs


def test_role_given_up_tries_its_audited_url_once(monkeypatch):
    calls, used, logs = _collect(monkeypatch, failing=set(PA_PAGES))
    audited = ar.audited_urls(ar.zone_authorities(CANARIES), "port_authority")[0]
    assert set(PA_PAGES) <= set(calls)
    assert audited in calls and audited in used
    assert calls.count(audited) == 1
    assert any("URL auditée tentée une fois" in m for m in logs)


def test_audited_url_failing_too_is_not_retried(monkeypatch):
    auths = ar.zone_authorities(CANARIES)
    audited = ar.audited_urls(auths, "port_authority")
    calls, used, _logs = _collect(monkeypatch, failing=set(PA_PAGES) | set(audited))
    tried = [u for u in calls if u in audited]
    assert len(tried) == 1  # once, then the role stays given up
    assert not {r for u in used for r in ar.roles_of(u, auths)} & {"port_authority"}


def test_rule_off_does_not_try_the_audited_url(monkeypatch):
    calls, _used, logs = _collect(monkeypatch, failing=set(PA_PAGES),
                                  overrides={"formalities.authority_seed_fallback": False})
    audited = set(ar.audited_urls(ar.zone_authorities(CANARIES), "port_authority"))
    assert not audited & set(calls)
    assert not any("URL auditée" in m for m in logs)
