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


# --- reading the lists (§10.1: a row of a designating list) -------------------------------
# Verbatim extracts (pdftotext -layout) of the real documents, from data/v3-bcp-sources:
# Schengen Handbook Annex 4, Spain, sea borders (PDF pp.26-27), and Orden INT/859/2023
# Annex V "Puestos Fronterizos" (BOE-A-2023-17072, PDF pp.34-36). Source URL, fetch date
# and the PDF's sha256 are in each file's header.

from pathlib import Path  # noqa: E402

from app.adapters import evidence_store  # noqa: E402
from app.services import border_crossing as bc  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
ANNEX4_TXT = (FIX / "annex4_spain_sea_borders.txt").read_text(encoding="utf-8")
ORDEN_TXT = (FIX / "orden_int_859_2023_annex_v.txt").read_text(encoding="utf-8")
# The Canaries' five posts, as each list writes them.
ANNEX4_CANARIES = {"Arrecife (Lanzarote)", "La Luz (Las Palmas)", "Puerto del Rosario (Fuerteventura)",
                   "Puerto de Santa Cruz de La Palma (La Palma)", "Santa Cruz de Tenerife"}
ORDEN_CANARIES = {"Arrecife", "Las Palmas", "Puerto Rosario", "Santa Cruz de la Palma",
                  "Santa Cruz de Tenerife"}
# Place names that appear only in the Orden's police-station column ("Comisaría Local de
# Telde.") or in its airport rows: never a post.
COMISARIA_ONLY = {"Telde", "Irún", "San Cristóbal de La Laguna", "Quart de Poblet-Manises",
                  "Elche", "Jerez de la Frontera", "San Andrés de Rabanedo", "Reus", "Lleida",
                  "San Sebastián-Donostia", "Santiago de Compostela"}


def test_fixtures_are_verbatim_extracts_with_provenance():
    for text, host in ((ANNEX4_TXT, "home-affairs.ec.europa.eu"), (ORDEN_TXT, "www.boe.es")):
        head = [line for line in text.splitlines() if line.startswith("# FIXTURE")]
        assert any(host in line for line in head)
        assert any(line.startswith("# FIXTURE sha256 of the PDF: ") for line in head)
        assert any(line.startswith("# FIXTURE Fetched: 2026-11-04") for line in head)


def test_annex4_real_pages_are_a_list_and_read_all_34_sea_items():
    """The real State pages carry no list title: Annex 4's own structure (a sea-border
    heading, numbered items) identifies it."""
    assert "border crossing point" not in ANNEX4_TXT.lower()
    assert bc.is_border_crossing_list(ANNEX4_TXT)
    rows = bc.annex4_rows(ANNEX4_TXT, "Spain")
    assert [r["number"] for r in rows] == list(range(1, 35))  # across the page break
    by = {r["number"]: r["name"] for r in rows}
    assert by[4] == "Arrecife (Lanzarote)" and by[18] == "La Luz (Las Palmas)"
    assert by[24] == "Puerto del Rosario (Fuerteventura)"
    assert by[25] == "Puerto de Santa Cruz de La Palma (La Palma)"
    assert by[27] == "San Sebastian" and by[28] == "Santa Cruz de Tenerife"


def test_orden_int_reads_the_post_column_only():
    rows = bc.orden_int_rows(ORDEN_TXT)
    names = [r["name"] for r in rows]
    assert len(rows) == 34 and names[0] == "A Coruña" and names[-1] == "Vigo"
    assert "Puerto Rosario" in names and "Pasaia" in names
    # every post is a "Puerto de …" cell: never the police-station column, never an airport
    assert all(r["row"].startswith("Puerto de ") for r in rows)
    assert not any(n in COMISARIA_ONLY for n in names)
    assert not any("Aeropuerto" in r["row"] or "Comisaría" in r["row"] or "Jefatura" in r["row"]
                   for r in rows)


@pytest.mark.parametrize("text, posts", [(ANNEX4_TXT, ANNEX4_CANARIES), (ORDEN_TXT, ORDEN_CANARIES)])
def test_canaries_give_exactly_their_five_posts(text, posts):
    kept, dropped = bc.zone_rows(text, CANARIES, bc.zone_state_name(CANARIES))
    assert {r["name"] for r in kept} == posts
    assert any(r["matched"] == "Las Palmas" for r in kept)
    gone = {d["name"] for d in dropped}
    # (27) San Sebastian is the Basque port (Clément), never La Gomera's
    assert ("San Sebastian" in gone) if text is ANNEX4_TXT else ("Pasaia" in gone)
    assert gone & {"Algeciras (Cádiz)", "Bahía de Algeciras"} and "Barcelona" in gone
    # no place of the police-station column became a post
    assert not {r["name"] for r in kept} & COMISARIA_ONLY


def test_mainland_spain_keeps_its_own_posts_not_the_canaries():
    mainland = {"mrgid": 5693, "name": "Spain", "iso2": "ES", "sov_iso2": "ES"}
    for text, canaries in ((ANNEX4_TXT, ANNEX4_CANARIES), (ORDEN_TXT, ORDEN_CANARIES)):
        kept, _dropped = bc.zone_rows(text, mainland, bc.zone_state_name(mainland))
        names = {r["matched"] for r in kept}
        assert {"Barcelona", "Tarragona", "Vigo"} <= names
        assert not {r["name"] for r in kept} & canaries


def test_rows_enter_the_zone_catalogue():
    ports, _missed = poe.zone_catalog(ORDEN_TXT, CANARIES)
    border = {p["name"] for p in ports if p.get("extraction_engine") == "border_list"}
    assert border == ORDEN_CANARIES


def test_a_row_is_evidence_for_the_judge_on_an_official_list_only():
    docs = [{"url": ORDEN, "text": ORDEN_TXT}]
    wl = poe.zone_whitelist(CANARIES)
    names = ["Santa Cruz de Tenerife", "Arrecife", "San Sebastián", "Corralejo"]
    recs = evidence_store.build_records(8364, names, docs, whitelist=wl, zone=CANARIES)
    by = {r["matched_name"]: r for r in recs}
    assert set(by) == {"Santa Cruz de Tenerife", "Arrecife"}
    assert "Puerto de Santa Cruz de Tenerife." in by["Santa Cruz de Tenerife"]["snippet"]
    # not official for this zone (Martinique's whitelist): no record
    assert not evidence_store.build_records(8364, names, docs, whitelist=poe.zone_whitelist(MQ),
                                            zone=CANARIES)


def test_not_a_border_crossing_list_reads_nothing():
    text = "Puerto de Arrecife. Horario de atención al público. Comisaría Local de Arrecife."
    assert not bc.is_border_crossing_list(text)
    assert bc.zone_rows(text, CANARIES, "Spain") == ([], [])
