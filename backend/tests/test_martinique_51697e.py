"""Run 20261104-021155-51697e lost Martinique: 0 of the 4 gold PoEs, on the sources of that
run (tests/fixtures/martinique_51697e_sources.json, as fetched, md5 in the file).

1. The arrêté du 18 juillet 2023 gave the catalogue six names, the mer.gouv.fr annex made the
   text look like a catalogue, and the LLM was skipped: nobody read the clearance list.
2. #49's area filter dropped Fort-de-France, Le Marin, Le Robert and Trinité, because a commune
   is a boundary=administrative feature for Nominatim.

Offline: the LLM reader answers with what it read in run d7963f on the same list, the
geocoders with what Nominatim returned, and the judge records what it is given.
"""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents import poe_judge
from app.core import geo, ml
from app.services import listing_ref
from app.services import poe_pipeline as poe

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "martinique_51697e_sources.json"
SOURCES = json.loads(FIXTURE.read_text(encoding="utf-8"))["sources"]
RAW = "\n\n".join(f"[SOURCE: {s['url']}]\n{s['text']}" for s in SOURCES)
USED = [{"url": s["url"], "official": True} for s in SOURCES]
MARTINIQUE = {"mrgid": 33178, "name": "Martinique", "geoname": "Martinique", "iso2": "MQ",
              "sov_iso2": "FR", "sovereign": "France",
              "geometry": {"type": "Polygon", "coordinates": [[
                  [-62.5, 13.5], [-59.5, 13.5], [-59.5, 16.0], [-62.5, 16.0], [-62.5, 13.5]]]}}
ARRETE_NAMES = ["Fort-de-France", "Le Marin", "Le Robert", "Saint-Pierre", "Trinité",
                "Martinique-Aimé-Césaire"]
LE_FRANCOIS = "Capitainerie Marina - Pointe Bateau"
LE_MARIN = "Port de plaisance Capitainerie du port (nouvelle extension)"
TROIS_ILETS = "Régie Port de Plaisance Marina de la pointe du bout"
ETANG = "Port de plaisance communautaire de l'Étang Z'abricots"
GOLD_ROWS = [LE_FRANCOIS, LE_MARIN, TROIS_ILETS, ETANG]
# What the LLM read on this list in run d7963f.
LLM_ROWS = [
    ("Station CAP DCML Centre de carénage - Baie des Tourelles", "Fort-de-France"),
    (ETANG, "Le Lamentin"), (LE_FRANCOIS, "Le François"), (LE_MARIN, "Le Marin"),
    ("Station-service CAP Ponton N°3", "Le Marin"), (TROIS_ILETS, "Les Trois-Îlets"),
    ("Brigade de surveillance des douanes Cité des douanes Quartier Raisinier", "La Trinité"),
]
BOUNDARY = {"class": "boundary", "type": "administrative"}
# Nominatim's first answer per name: communes are boundary=administrative.
NOMINATIM = {
    "Fort-de-France": (14.6027962, -61.0676724, BOUNDARY),
    "Le Marin": (14.4710778, -60.870212, BOUNDARY),
    "Le Robert": (14.6776144, -60.939021, BOUNDARY),
    "Trinité": (14.7380399, -60.9645707, BOUNDARY),
    "Saint-Pierre": (14.7417183, -61.1763904, {"class": "place", "type": "village"}),
    "Martinique-Aimé-Césaire": (14.590977, -61.0042393, {"class": "aeroway", "type": "aerodrome"}),
    "Station CAP DCML": (14.6027962, -61.0676724, BOUNDARY),
    "Port de plaisance communautaire": (14.614557, -61.0018145, {"class": "leisure", "type": "marina"}),
    "Capitainerie Marina": (14.6149952, -60.9028968, BOUNDARY),
    "Port de plaisance Capitainerie": (14.4710778, -60.870212, BOUNDARY),
    "Station-service CAP": (14.4710778, -60.870212, BOUNDARY),
    "Régie Port": (14.5387672, -61.033545, {"class": "leisure", "type": "marina"}),
    "Brigade de surveillance": (14.7380399, -60.9645707, BOUNDARY),
}


@pytest.fixture(scope="module")
def run_zone():
    """Extraction, geocoding and the judge on the run's sources, once for the module."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield _run_zone(monkeypatch)


def _run_zone(monkeypatch):
    seen = {}
    logs, events = [], []

    async def llm_reader(context, zone, log, rec=None, catalog_text=None, settings=None):
        seen["llm_read"] = True
        return [{"name": n, "city": c, "extraction_engine": "nightshift"} for n, c in LLM_ROWS]

    async def registry(zone, run_id, settings=None, candidates=None):
        return {"mode": "sample", "country_iso2": "MQ", "entries": [], "registry_entries": 0}

    async def nominatim(query, country_code=None, limit=1):
        for name, (lat, lon, kind) in NOMINATIM.items():
            if query.lower().startswith(name.lower()):
                return [{"lat": str(lat), "lon": str(lon), "display_name": f"{name}, Martinique",
                         **kind}]
        return []

    async def no_geonames(query, country_code=None, limit=1):
        return []

    async def no_llm_geocode(port, zone, settings=None, log=None):
        return None

    async def judge(name, docs, texts, run_id, timeout_s, gate=None):
        seen["judged"] = [d.get("name") for d in docs]
        seen["texts"] = texts
        return {"provider": "stub"}, {}, "stub"

    class Rec:
        async def event(self, step, **payload):
            events.append((step, payload))

    monkeypatch.setattr(poe, "_extract_primary", llm_reader)
    monkeypatch.setattr(poe, "validate_ports_zetaris", registry)
    monkeypatch.setattr(ml, "extract_entities", lambda text: [])
    monkeypatch.setattr(geo, "_nominatim_rows", nominatim)
    monkeypatch.setattr(geo, "_geonames_rows", no_geonames)
    monkeypatch.setattr(poe, "llm_geocode_port", no_llm_geocode)
    monkeypatch.setattr(poe, "_hmem_recall", lambda docs, run_id: [[] for _ in docs])
    monkeypatch.setattr(poe, "_hmem_remember", lambda docs, run_id: {})
    monkeypatch.setattr(listing_ref, "project_listing", lambda: {"ports": []})
    monkeypatch.setattr(poe_judge, "ask", judge)

    async def go():
        whitelist = poe.zone_whitelist(MARTINIQUE)
        with poe.zone_extract_budget():
            docs = await poe._extract_and_geocode(
                dict(MARTINIQUE), RAW[:40000], USED, logs.append, rec=Rec(), catalog_text=RAW,
                whitelist=whitelist)
            await poe._judge_zone_run(dict(MARTINIQUE), docs, RAW, USED, whitelist,
                                      SimpleNamespace(run_id="test"), logs.append, Rec())
        return docs

    docs = asyncio.run(go())
    return SimpleNamespace(docs=docs, judged=seen.get("judged") or [], texts=seen.get("texts") or [],
                           llm_read=seen.get("llm_read", False), logs=logs, events=events)


def test_the_fixture_is_what_the_run_fetched():
    # The mer.gouv.fr annex is kept at the run's 120,000-char cap, so its md5 is the full text's.
    for source in SOURCES:
        if "capped_from" not in source:
            assert hashlib.md5(source["text"].encode()).hexdigest() == source["md5"]
    assert any("legifrance" in s["url"] for s in SOURCES)


def test_the_catalogue_reads_the_arretes_six_names_and_calls_itself_sufficient():
    catalog, missed = poe.zone_catalog(RAW, MARTINIQUE)
    assert [p["name"] for p in catalog] == ARRETE_NAMES and not missed
    assert poe.catalog_is_sufficient(catalog, RAW)


def test_a_clearance_list_among_the_sources_keeps_the_llm(run_zone):
    assert run_zone.llm_read
    assert any("liste de clearance parmi les sources" in m for m in run_zone.logs)
    assert not any("LLM sauté" in m for m in run_zone.logs)


@pytest.mark.parametrize("row", GOLD_ROWS)
def test_the_gold_rows_reach_the_judge(run_zone, row):
    assert row in run_zone.judged


def test_the_judge_reads_the_rows_of_the_list(run_zone):
    text = " ".join(t["text"] for t in run_zone.texts)
    for row in ("Capitainerie Marina - Pointe Bateau", "Capitainerie du port (nouvelle extension)",
                "Marina de la pointe du bout", "Étang Z’abricots"):
        assert row in text


@pytest.mark.parametrize("commune", ["Fort-de-France", "Le Marin", "Le Robert", "Trinité"])
def test_the_arretes_communes_are_not_dropped_as_areas(run_zone, commune):
    assert commune in run_zone.judged
    assert not [p for s, p in run_zone.events
                if s == "geocode" and p.get("arbitration") == "area_hit"]


def test_the_rows_become_the_gold_poes(run_zone):
    texts = [{"url": s["url"], "text": s["text"]} for s in SOURCES]
    docs, grouped = poe.group_clearance_points(run_zone.docs, texts, MARTINIQUE,
                                               poe.zone_whitelist(MARTINIQUE))
    assert {poe_doc["name"]: [p["name"] for p in poe_doc["clearance_points"]]
            for poe_doc, _ in grouped} == {
        "Le François": [LE_FRANCOIS], "Le Marin": [LE_MARIN],
        "Trois-Îlets": [TROIS_ILETS], "Fort-de-France": [ETANG]}


def test_with_a_judge_saying_poe_to_everything_the_run_gives_the_gold(run_zone):
    """The rules hold on this run's records, whatever the judge answers."""
    texts = [{"url": s["url"], "text": s["text"]} for s in SOURCES]
    whitelist = poe.zone_whitelist(MARTINIQUE)
    docs = copy.deepcopy(run_zone.docs)
    for doc in docs:
        doc["judge"] = {"verdict": "poe", "reason": "stub", "quoted_excerpt": doc["name"],
                        "excerpt_verified": True}
        poe.attach_confidence_level(doc, whitelist=whitelist, texts=texts, zone=MARTINIQUE)
    docs, _ = poe.group_clearance_points(docs, texts, MARTINIQUE, whitelist)
    merged, _ = poe.merge_same_ports(docs)
    levels = {d["name"]: d["confidence_level"] for d in merged}
    for gold in ("Le François", "Le Marin", "Trois-Îlets", "Fort-de-France"):
        assert levels[gold] == "C2"
    for name in ("Trinité", "Le Robert", "Saint-Pierre", "Martinique-Aimé-Césaire"):
        assert levels[name] == "C1"
    assert sorted(n for n, level in levels.items() if level == "C2") == [
        "Fort-de-France", "Le François", "Le Marin", "Trois-Îlets"]


def test_the_catalogue_still_skips_the_llm_without_a_clearance_list(monkeypatch):
    async def no_llm(*a, **k):
        raise AssertionError("the catalogue was enough")

    monkeypatch.setattr(poe, "_extract_primary", no_llm)
    raw = "\n\n".join(f"[SOURCE: {s['url']}]\n{s['text']}" for s in SOURCES
                      if "clearance" not in s["text"].lower())
    assert not poe.clearance_list_in_sources(raw, MARTINIQUE)
    logs = []
    ports = asyncio.run(poe._extract_ports_compared(raw[:40000], MARTINIQUE, logs.append,
                                                    catalog_text=raw))
    assert [p["name"] for p in ports] == ARRETE_NAMES
    assert any("LLM sauté" in m for m in logs)


@pytest.mark.parametrize("url", [
    "https://www.marina-martinique.fr/liste.pdf",  # not official
])
def test_a_clearance_list_off_an_official_source_changes_nothing(url):
    body = next(s for s in SOURCES if "liste-points-clearance" in s["url"])["text"]
    assert not poe.clearance_list_in_sources(f"[SOURCE: {url}]\n{body}", MARTINIQUE)


def test_text_without_source_blocks_changes_nothing():
    assert not poe.clearance_list_in_sources("Liste des points de clearance", MARTINIQUE)


@pytest.mark.parametrize("hit", [
    {"osm_type": "island"}, {"osm_type": "islet"}, {"osm_type": "archipelago"},
    {"osm_type": "region"}, {"osm_type": "state"}, {"osm_type": "province"},
    {"osm_type": "county"}, {"geonames_fcode": "ISL"}, {"geonames_fcode": "ISLS"},
    {"geonames_fcode": "RGN"},
])
def test_islands_and_regions_stay_areas(hit):
    assert poe.only_area_hits({"candidates": [hit]}, "Santo Antão")


@pytest.mark.parametrize("hit", [
    {"osm_type": "administrative"}, {"geonames_fcode": "ADM1"}, {"geonames_fcode": "ADM2"},
    {"geonames_fcode": "ADM3"}, {"geonames_fcode": "ADM4"}, {"geonames_fcode": "ADMD"},
])
@pytest.mark.parametrize("name", ["Le Marin", "Trinité", "Soufrière", "Arrecife"])
def test_a_commune_is_not_an_area(name, hit):
    assert not poe.only_area_hits({"candidates": [hit]}, name)
