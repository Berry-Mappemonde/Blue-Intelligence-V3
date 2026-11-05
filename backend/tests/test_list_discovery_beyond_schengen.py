"""List discovery outside Schengen. Cape Verde's PoE list is a decree (DL 13/2025) in the
Boletim Oficial, and no search of run 20261104-125841-ac33e9 returned it, so Mindelo was
never read. Replay on that run's Cape Verde candidates
(tests/fixtures/run_ac33e9_candidates.json). Offline: search and fetches are stubbed.
"""
import asyncio
import inspect
import json
from pathlib import Path

import pytest

from app.services import authority_roles, poe_pipeline as poe

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "run_ac33e9_candidates.json"
RUN = json.loads(FIXTURE.read_text(encoding="utf-8"))["zones"]
CV = {**RUN["8362"]["zone"], "sov_iso2": "CV"}
LC = {**RUN["8416"]["zone"], "sov_iso2": "LC"}
MQ = {**RUN["33178"]["zone"], "sov_iso2": "FR"}
CANARIES = {**RUN["8364"]["zone"], "sov_iso2": "ES"}
# Shapes of the real URLs. The gazette's says nothing of what it holds.
GAZETTE = "https://boe.incv.cv/Bulletins/View/85465"
AAI = "https://aai.gov.cv/cat-geral/56-os-postos-habilitados-de-fronteiras-phf-de-cabo-verde-sao"


def _candidate(url, rank=None):
    c = {"url": url, "domain": poe.domain_of(url), "official": True}
    return dict(c, list_discovery=rank) if rank else c


def _official():
    return [dict(c, official=True) for c in RUN["8362"]["official"]]


def _queue(found):
    """The end of the gatekeeper, with ``found`` added to the run's candidates."""
    pinned = [dict(c, official=True) for c in RUN["8362"]["pinned"]]
    norm = {poe._normalize_url(c["url"]) for c in pinned}
    official = [c for c in [*found, *_official()] if poe._normalize_url(c["url"]) not in norm]
    lists = poe.designating_list_candidates(official, CV)
    held = norm | {poe._normalize_url(c["url"]) for c in lists}
    return pinned + lists + poe._best_per_domain(
        [c for c in official if poe._normalize_url(c["url"]) not in held])


def _fetched(queue, max_fetch=14):
    calls = []

    async def cascade(url, min_chars=200, log=None):
        calls.append(url)
        return {"text": "Informação oficial. " * 40, "html": "", "md5": url, "level": "N1",
                "blocked": False}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(poe, "extract_cascade", cascade)
        asyncio.run(poe._collect_texts(queue, lambda m: None, max_fetch=max_fetch, zone=CV))
    return calls


# --- the queries -----------------------------------------------------------------------

def test_cape_verde_queries_come_from_its_language_and_name():
    assert poe.entry_list_queries(CV) == [
        ("pt", "postos de fronteira habilitados Cabo Verde decreto-lei"),
        ("en", "designated ports of entry Cape Verde border posts customs regulations"),
        ("pt", "site:incv.cv postos de fronteira habilitados Cabo Verde decreto-lei")]


def test_other_zones_outside_schengen_get_theirs():
    assert [lang for lang, _ in poe.entry_list_queries(LC)] == ["en"]
    assert "Saint Lucia" in poe.entry_list_queries(LC)[0][1]
    assert dict(poe.entry_list_queries(MQ, {"seed_urls_mrgid": {}}))["fr"] == (
        "postes frontaliers habilités ports d'entrée Martinique arrêté")


def test_a_zone_with_its_list_pinned_sends_no_query():
    assert poe.entry_list_queries(MQ) == []


def test_a_pinned_page_that_is_no_list_does_not_stop_discovery():
    seeds = {"seed_urls_mrgid": {"8362": ["https://www.imp.cv/formalidades"]}}
    assert poe.entry_list_queries(CV, seeds)
    seeds = {"seed_urls_mrgid": {"8362": ["https://www.imp.cv/postos-habilitados-de-fronteiras"]}}
    assert poe.entry_list_queries(CV, seeds) == []


def test_a_schengen_zone_keeps_the_border_crossing_queries_only():
    assert poe.entry_list_queries(CANARIES) == []
    assert poe.border_crossing_queries(CANARIES)


def test_every_search_language_has_a_query_with_the_place():
    for lang in {*poe.LANG_BY_ISO2.values(), "en"}:
        assert "{place}" in poe.ENTRY_LIST_QUERIES[lang], lang


def test_a_zone_with_no_place_name_gets_no_query():
    assert poe.entry_list_queries({"iso2": "CV", "sov_iso2": "CV"}) == []


class _Rec:
    def __init__(self):
        self.events = []

    async def event(self, step, **payload):
        self.events.append((step, payload))


def test_the_search_step_sends_them_and_tags_what_they_find(monkeypatch):
    calls: list[str] = []

    async def fake_searx(query, log):
        calls.append(query)
        urls = [GAZETTE] if query.startswith("postos de fronteira") else []
        return [{"url": u, "domain": poe.domain_of(u), "engine": "searxng"} for u in urls]

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
    monkeypatch.setattr(poe, "save_exceptions", lambda exc: None)
    exc = poe.load_exceptions()
    rec, logs = _Rec(), []
    official, _s, _y = asyncio.run(poe._find_sources(CV, poe.zone_whitelist(CV, exc), exc,
                                                     logs.append, rec=rec, tf_key="", variant="v2"))
    assert "postos de fronteira habilitados Cabo Verde decreto-lei" in calls
    assert GAZETTE in [c["url"] for c in official]
    langs = {p["lang"] for s, p in rec.events if s == "search" and p.get("lang", "").startswith("entry")}
    assert langs == {"entry-list-pt", "entry-list-en"}
    assert any("liste nationale des postes d'entrée: 3 requête(s)" in m for m in logs)
    assert any(e[0] == "designating_lists" and GAZETTE in str(e[1]) for e in rec.events)


# --- the publishers come from the whitelist data ---------------------------------------

def test_cape_verde_publishers_are_its_gazette_and_authorities():
    assert authority_roles.list_publishers(CV) == [
        "incv.cv", "mf.gov.cv", "policianacional.cv", "imp.cv", "enapor.cv"]


def test_a_bare_government_label_is_no_publisher():
    assert "gov.cv" not in authority_roles.list_publishers(CV)
    assert "gov.lc" not in authority_roles.list_publishers(LC)


def test_an_unaudited_zone_has_no_publisher():
    assert authority_roles.list_publishers({"mrgid": 990403, "iso2": "ZZ"}) == []


def test_a_gazette_holds_no_authority_role():
    assert all(a["key"] != "incv.cv" for a in authority_roles.zone_authorities(CV))
    assert authority_roles.roles_of(GAZETTE, authority_roles.zone_authorities(CV)) == set()


def test_every_gazette_in_the_data_is_on_its_zone_whitelist():
    data = json.loads(authority_roles.SOURCES.read_text(encoding="utf-8"))["zones"]
    gazettes = [(z, g) for z in data.values() for g in z.get("gazettes") or []]
    assert gazettes
    for zone, g in gazettes:
        assert g["domain"] in poe.build_whitelist(zone["whitelist_key"], zone["whitelist_key"], None)
        assert g["url"].startswith("https://") and g["source"].startswith("https://")


def test_the_gazette_passes_the_gatekeeper():
    assert poe.url_allowed(GAZETTE, poe.zone_whitelist(CV))


def test_the_code_holds_no_publisher_or_url():
    code = (inspect.getsource(poe.entry_list_queries) + inspect.getsource(authority_roles.list_publishers)
            + inspect.getsource(poe.designating_list_candidates) + str(poe.ENTRY_LIST_QUERIES))
    for word in ("incv", "aai.gov", "http", "Mindelo", "13/2025"):
        assert word not in code


# --- replay on run ac33e9's Cape Verde candidates --------------------------------------

def test_the_run_had_neither_page():
    urls = [c["url"] for c in _official()]
    assert GAZETTE not in urls and AAI not in urls


def test_a_gazette_url_found_by_discovery_wins_a_designating_slot():
    found = poe.designating_list_candidates([*_official(), _candidate(GAZETTE, 1)], CV)
    assert GAZETTE in [c["url"] for c in found]


def test_it_is_fetched_inside_max_fetch_14():
    calls = _fetched(_queue([_candidate(GAZETTE, 1)]))
    assert len(calls) <= 14 and GAZETTE in calls


def test_it_comes_ahead_of_the_generic_pages():
    urls = [c["url"] for c in _queue([_candidate(GAZETTE, 1)])]
    generic = [c["url"] for c in poe._best_per_domain(_official())]
    assert urls.index(GAZETTE) < min(urls.index(u) for u in generic if u in urls)


def test_the_four_roles_keep_their_slots():
    authorities = authority_roles.zone_authorities(CV)
    before = {r for u in _fetched(_queue([])) for r in authority_roles.roles_of(u, authorities)}
    after = {r for u in _fetched(_queue([_candidate(GAZETTE, 1), _candidate(AAI, 5)]))
             for r in authority_roles.roles_of(u, authorities)}
    assert after == before


def test_without_discovery_the_same_url_has_no_slot():
    assert GAZETTE not in [c["url"] for c in poe.designating_list_candidates([_candidate(GAZETTE)], CV)]


def test_a_page_of_any_other_ministry_found_by_discovery_has_no_slot():
    url = "https://om-mne.gov.cv/np4/39/"
    assert not poe.designating_list_candidates([_candidate(url, 1)], CV)


def test_a_url_that_says_it_is_the_list_wins_on_a_host_that_is_no_publisher():
    assert authority_roles.roles_of(AAI, authority_roles.zone_authorities(CV)) == set()
    assert [c["url"] for c in poe.designating_list_candidates([_candidate(AAI, 5)], CV)] == [AAI]
    assert [c["url"] for c in poe.designating_list_candidates([_candidate(AAI)], CV)] == [AAI]


def test_at_most_three_slots_per_publisher():
    bulletins = [_candidate(f"https://boe.incv.cv/Bulletins/View/{i}", i) for i in range(1, 6)]
    assert len(poe.designating_list_candidates(bulletins, CV)) == 3


@pytest.mark.parametrize("key", ["8362", "8416", "33178"])
def test_nothing_changes_without_a_discovery_result(key):
    """The run's own candidates carry no tag and no list URL outside Schengen, except the
    lists already pinned: the new publishers alone move nothing."""
    zone = {**RUN[key]["zone"], "sov_iso2": {"33178": "FR"}.get(key, RUN[key]["zone"]["iso2"])}
    pinned = {poe._normalize_url(c["url"]) for c in RUN[key]["pinned"]}
    official = [dict(c, official=True) for c in RUN[key]["official"]
                if poe._normalize_url(c["url"]) not in pinned]
    assert poe.designating_list_candidates(official, zone) == []
