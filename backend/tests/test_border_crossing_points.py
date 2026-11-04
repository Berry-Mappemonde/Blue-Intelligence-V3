"""Schengen zones discover the list of border crossing points (Schengen Handbook Annex 4,
a State's own list such as Orden INT/859/2023 Annex V) and its publishers are official
(Clément, 3 Nov: the Canaries' five PoEs). Offline: search providers stubbed, no Mongo.
"""
import asyncio

import pytest

from app.services import poe_pipeline as poe
from app.services import territory_ref as tr

CANARIES = {"mrgid": 8364, "name": "Canary Islands", "geoname": "Canary Islands",
            "sov_iso2": "ES", "sovereign": "Spain"}
MQ = {"mrgid": 33178, "name": "Martinique", "geoname": "Martinique", "iso2": "MQ",
      "sov_iso2": "FR", "sovereign": "France"}
ANNEX4 = ("https://home-affairs.ec.europa.eu/document/download/"
          "43debf3f-adfc-4f7a-908a-49fe29f65aa5_en?filename=handbook-annex-04_en.pdf")
ORDEN = "https://www.boe.es/boe/dias/2023/07/25/pdfs/BOE-A-2023-17072.pdf"


# --- Schengen membership (territories.json) ------------------------------------------

@pytest.mark.parametrize("iso2, mrgid, state", [
    (None, 8364, "ES"),        # Canaries: curated, in the area
    ("FR", 5677, "FR"),        # France métropolitaine: curated, in the area
    ("MQ", 33178, ""),         # Martinique: French, outside the area (CISA art. 138)
    ("RE", 8338, ""),          # La Réunion
    ("NC", 8312, ""),          # New Caledonia
    ("PT", 990401, "PT"),      # an Azores-like polygon (synthetic mrgid): not curated, ISO2 PT
    ("ES", 5693, "ES"),        # mainland Spain
    ("SX", 21803, ""),         # Sint Maarten: own ISO2
    ("FO", 990402, ""),        # a Faroe-like polygon (synthetic mrgid): own ISO2
    ("LC", 8416, ""),          # not Schengen
])
def test_schengen_state(iso2, mrgid, state):
    assert tr.schengen_state(iso2, mrgid) == state


def test_every_curated_record_has_a_sourced_flag_where_it_matters():
    recs = {r["code"]: r for r in tr._table()["territories"]}
    assert recs["islas_canarias"]["schengen"] is True
    assert recs["france_metropolitaine"]["schengen"] is True
    french_overseas = [r for c, r in recs.items() if r.get("sovereign_iso2") == "FR"
                       and c != "france_metropolitaine"]
    assert len(french_overseas) == 12 and all(r["schengen"] is False for r in french_overseas)
    assert all(r.get("schengen_source") for r in recs.values() if "schengen" in r)


# --- publishers pass the gatekeeper ----------------------------------------------------

def test_publishers_are_official_for_schengen_zones_only():
    canaries = poe.zone_whitelist(CANARIES)
    assert poe.url_allowed(ANNEX4, canaries) and poe.url_allowed(ORDEN, canaries)
    mainland = poe.zone_whitelist({"mrgid": 5693, "iso2": "ES", "sov_iso2": "ES"})
    assert {"boe.es", "home-affairs.ec.europa.eu"} <= set(mainland)
    france = poe.zone_whitelist({"mrgid": 5677, "iso2": "FR", "sov_iso2": "FR"})
    assert poe.url_allowed(ANNEX4, france) and not poe.url_allowed(ORDEN, france)
    for zone in (MQ, {"mrgid": 8416, "iso2": "LC", "sov_iso2": "LC"},
                 {"mrgid": 21803, "iso2": "SX", "sov_iso2": "NL"}):
        assert not poe.url_allowed(ANNEX4, poe.zone_whitelist(zone))


# --- discovery queries -----------------------------------------------------------------

def test_queries_in_the_zone_languages_and_english():
    assert poe.border_crossing_queries(CANARIES) == [
        ("es", "puestos fronterizos habilitados marítimos España anexo orden"),
        ("en", "list of border crossing points Spain sea borders Schengen Annex 4")]
    fr = dict(poe.border_crossing_queries({"mrgid": 5677, "name": "France", "iso2": "FR",
                                           "sov_iso2": "FR"}))
    assert fr["fr"] == "liste des points de passage frontaliers maritimes France Schengen"
    assert [l for l, _ in poe.border_crossing_queries({"mrgid": 990401, "iso2": "PT",
                                                       "name": "Azores"})] == ["pt", "en"]
    assert poe.border_crossing_queries(MQ) == []


class _Rec:
    def __init__(self):
        self.events = []

    async def event(self, step, **payload):
        self.events.append((step, payload))


@pytest.fixture
def search(monkeypatch):
    calls: list[str] = []
    answers = {"fn": lambda q: []}

    async def fake_searx(query, log):
        calls.append(query)
        return [{"url": u, "domain": poe.domain_of(u), "engine": "searxng"} for u in answers["fn"](query)]

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
    return calls, answers


def _find(zone):
    exc = poe.load_exceptions()
    rec, logs = _Rec(), []
    official, _s, _y = asyncio.run(poe._find_sources(zone, poe.zone_whitelist(zone, exc), exc,
                                                     logs.append, rec=rec, tf_key="", variant="v2"))
    return [c["url"] for c in official], logs, rec


def test_canaries_discover_the_lists_and_keep_them_as_official(search):
    calls, answers = search
    answers["fn"] = lambda q: ([ANNEX4] if "border crossing points" in q
                               else [ORDEN] if "puestos fronterizos" in q else [])
    official, logs, rec = _find(CANARIES)
    assert any(q.startswith("puestos fronterizos habilitados") for q in calls)
    assert any(q.startswith("list of border crossing points Spain") for q in calls)
    assert ANNEX4 in official and ORDEN in official
    langs = {p["lang"] for s, p in rec.events if s == "search" and p.get("lang", "").startswith("border")}
    assert langs == {"border-crossing-es", "border-crossing-en"}
    assert any("points de passage frontaliers (Schengen): 2 requête(s)" in m for m in logs)


def test_martinique_sends_no_border_crossing_query(search):
    calls, _answers = search
    _find(MQ)
    assert not any("border crossing points" in q or "puestos fronterizos" in q for q in calls)
