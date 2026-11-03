"""Duplicate sources and duplicate ports (runs f3993a, 608d85), and the Canaries'
geocoder country filter. Offline: fetches and geocoders stubbed, no Mongo."""
import asyncio

import pytest

from app.core import geo
from app.core.run_rules import bind_rules, catalog_default, reset_rules, snapshot_for_run
from app.services import poe_pipeline as poe

PROSE = ("Customs Control and Management. Every vessel arriving shall proceed to a "
         "boarding station and report to the proper officer. ")


def _act(n: int, seed: str = "") -> str:
    """A long act whose text varies sentence by sentence (shingles are distinct)."""
    out, i = [], 0
    while sum(map(len, out)) < n:
        out.append(f"Section {i}{seed}. {PROSE}Clause {i * 7 % 1013} applies to schedule {i % 97}. ")
        i += 1
    return "".join(out)[:n]


def _distinct(n: int, seed: str) -> str:
    """A text of its own (random words): shares no shingles with another seed's."""
    import random
    rnd = random.Random(seed)
    words = ["vessel", "harbour", "officer", "customs", "report", "arrival", "permit", "fee",
             "schedule", "master", "crew", "cargo", "yacht", "anchorage", "notice", "form",
             "island", "duty", "entry", "clearance", "section", "act", "port", "station"]
    out = []
    while sum(len(w) + 1 for w in out) < n:
        out.append(rnd.choice(words))
    return " ".join(out)[:n]


def _collect(monkeypatch, pages: dict[str, str], ranked: list[str], overrides=None):
    calls: list[str] = []

    async def fake_cascade(url, min_chars=200, log=None):
        calls.append(url)
        text = pages[url]
        import hashlib
        return {"text": text, "html": "", "md5": hashlib.md5(text.encode()).hexdigest(),
                "level": "N1-pymupdf", "blocked": False}

    monkeypatch.setattr(poe, "extract_cascade", fake_cascade)
    logs: list[str] = []
    token = bind_rules(snapshot_for_run(mode="formalities", overrides=overrides or {}))
    try:
        texts, _h, used, _e = asyncio.run(poe._collect_texts(
            [{"url": u, "domain": poe.domain_of(u), "official": True} for u in ranked],
            logs.append))
    finally:
        reset_rules(token)
    return calls, [u["url"] for u in used], texts, logs


# --- 1. duplicate sources ----------------------------------------------------------

def test_same_md5_at_two_urls_is_read_once_and_not_counted(monkeypatch):
    a, dup, b = ("https://www.govt.lc/act.pdf", "https://www.customs.gov.lc/act.pdf",
                 "https://www.customs.gov.lc/yachts")
    body = _act(30000)
    pages = {a: body, dup: body, b: _distinct(5000, "b")}
    calls, used, texts, logs = _collect(monkeypatch, pages, [a, dup, b],
                                        {"formalities.max_fetch": 4})
    assert calls == [a, dup, b]
    assert used == [a, b] and len(texts) == 2
    assert any("doublon de https://www.govt.lc/act.pdf (md5)" in m for m in logs)


def test_saint_lucia_two_copies_of_the_act_take_one_slot(monkeypatch):
    """~566k and ~579k chars, different URLs and md5: the same act extracted twice."""
    act = _act(566000)
    # the second copy: running headers and page numbers every ~2,100 chars
    copy = "".join(act[i:i + 2100] + f"\nCustoms (Control and Management) Act — page {i // 2100 + 1}\n"
                   for i in range(0, len(act), 2100))
    assert 575000 < len(copy) < 583000
    first = "https://www.govt.lc/media.govt.lc/www/resources/legislation/CustomsAct.pdf"
    second = "https://www.slaspa.com/contentPages/download/customs-act-2015"
    others = [f"https://www.customs.gov.lc/yacht-clearance-{i}" for i in range(3)]
    pages = {first: act, second: copy, **{u: _distinct(8000, u) for u in others}}
    calls, used, texts, logs = _collect(monkeypatch, pages, [first, second, *others],
                                        {"formalities.max_fetch": 4})
    # the duplicate is fetched but given back to the cap: all four real sources fit
    assert calls == [first, second, *others]
    assert used == [first, *others]
    assert any("doublon de" in m and "% identique" in m for m in logs)


def test_two_different_acts_of_the_same_length_are_both_kept(monkeypatch):
    a, b = "https://www.govt.lc/customs-act.pdf", "https://www.govt.lc/shipping-act.pdf"
    pages = {a: _distinct(100000, "customs"), b: _distinct(100000, "shipping")}
    _calls, used, _t, _l = _collect(monkeypatch, pages, [a, b])
    assert used == [a, b]


# --- 2. duplicate ports ------------------------------------------------------------

def _port(name, lat=None, lon=None, level=None, urls=()):
    return {"_id": name, "name": name, "lat": lat, "lon": lon, "confidence_level": level,
            "source_urls": list(urls)}


@pytest.mark.parametrize("a, b", [
    (_port("Praia", 14.9150, -23.5087), _port("Porto da Praia", 14.9195, -23.5050)),
    (_port("Soufriere Bay", 13.8560, -61.0640), _port("Soufrière", 13.8137, -61.0560)),  # 4.7 km
    (_port("Le Marin", 14.4690, -60.8690), _port("La marina du Marin", 14.4664, -60.8740)),
    (_port("Fort de France", 14.6040, -61.0680), _port("Fort-de-France", 14.6050, -61.0700)),
])
def test_same_port_pairs_merge(a, b):
    assert poe.same_port(a, b, poe.PORT_MERGE_KM)
    merged, merges = poe.merge_same_ports([dict(a), dict(b)])
    assert len(merged) == 1 and len(merges) == 1


def test_soufriere_is_4_7_km_apart_and_the_distance_is_a_rule():
    a, b = _port("Soufriere Bay", 13.8560, -61.0640), _port("Soufrière", 13.8137, -61.0560)
    assert 4.5 < geo.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) < 5.0
    assert catalog_default("formalities.port_merge_km") == poe.PORT_MERGE_KM == 5.0
    assert not poe.same_port(a, b, 4.0)


def test_martinique_facilities_on_the_commune_point_stay_separate():
    commune = (14.4840, -60.8640)  # both records got Le Marin's commune point
    tourelles = _port("Baie des Tourelles careening station", *commune)
    zabricot = _port("Étang Z'Abricot marina", *commune)
    assert not poe.same_port(tourelles, zabricot, poe.PORT_MERGE_KM)
    merged, merges = poe.merge_same_ports([tourelles, zabricot])
    assert len(merged) == 2 and not merges


def test_two_named_facilities_of_one_commune_stay_separate():
    assert not poe.same_port(_port("Port du Marin", 14.47, -60.87),
                             _port("Marina du Marin", 14.47, -60.87), poe.PORT_MERGE_KM)


def test_same_name_far_apart_stays_separate():
    assert not poe.same_port(_port("Marigot", 14.0, -61.0), _port("Marigot", 14.2, -61.0),
                             poe.PORT_MERGE_KM)  # 22 km


def test_merge_keeps_the_best_evidence_and_level():
    praia = _port("Praia", 14.9150, -23.5087, level="C1", urls=["https://www.gov.cv/a"])
    porto = _port("Porto da Praia", 14.9195, -23.5050, level="C2",
                  urls=["https://www.enapor.cv/portos/praia"])
    porto["judge"] = {"verdict": "poe"}
    merged, ((keep, rest),) = poe.merge_same_ports([praia, porto])
    assert merged == [keep] and keep["name"] == "Porto da Praia"
    assert keep["confidence_level"] == "C2" and keep["judge"] == {"verdict": "poe"}
    assert keep["merged_names"] == ["Praia"]
    assert keep["source_urls"] == ["https://www.enapor.cv/portos/praia", "https://www.gov.cv/a"]


def test_a_record_without_gps_merges_with_its_placed_twin():
    merged, _ = poe.merge_same_ports([_port("Fort-de-France"), _port("Fort de France", 14.6, -61.07)])
    assert len(merged) == 1 and merged[0]["lat"] == 14.6


# --- 3. the Canaries' geocoder country filter -----------------------------------------

CANARIES_DOC = {"mrgid": 8364, "name": "Canary Islands", "geoname": "Canary Islands"}  # no iso2


def test_canaries_without_iso2_are_filtered_on_spain():
    assert geo.nominatim_countrycodes(CANARIES_DOC) == "es"
    # unchanged elsewhere
    assert geo.nominatim_countrycodes({"mrgid": 33178, "iso2": "MQ"}) == "mq,fr"
    assert geo.nominatim_countrycodes({"mrgid": 21803, "iso2": "SX", "sov_iso2": "NL"}) == "sx"
    # an uncurated polygon without ISO2 keeps no filter (9412331's rule)
    assert geo.nominatim_countrycodes({"mrgid": 990301, "sov_iso2": "FR"}) is None


def test_canaries_geonames_lookup_has_a_country(monkeypatch):
    seen: list = []

    async def fake_rows(q, country, limit=5):
        seen.append(country)
        return []

    monkeypatch.setattr(geo, "_geonames_rows", fake_rows)
    asyncio.run(geo._collect_geonames_candidates({"name": "Puerto de Arrecife"}, CANARIES_DOC))
    assert seen and seen[0] == "ES"


# --- end to end: one record leaves _extract_and_geocode ---------------------------------

def test_le_marin_and_its_marina_leave_the_pipeline_as_one_port(monkeypatch):
    """Two spellings of Fort-de-France are already folded upstream (the rows are keyed on
    the normalized name); "Le Marin" / "La marina du Marin" (0.6 km) were not."""
    from tests.test_geocode_overseas import MQ, _fake_nominatim

    town = {"lat": "14.4690", "lon": "-60.8690", "display_name": "Le Marin, Martinique, France",
            "class": "place", "type": "town"}
    marina = {"lat": "14.4664", "lon": "-60.8740",
              "display_name": "Marina du Marin, Le Marin, Martinique, France",
              "class": "leisure", "type": "marina"}
    _fake_nominatim(monkeypatch, {"La marina du Marin": marina, "Le Marin": town})

    async def fake_extract(context, zone, log, rec=None, catalog_text=None, settings=None):
        return [{"name": "Le Marin", "extraction_engine": "catalog"},
                {"name": "La marina du Marin", "extraction_engine": "catalog"}]

    async def no_llm_geocode(*a, **k):
        return None

    monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
    monkeypatch.setattr(poe, "llm_geocode_port", no_llm_geocode)
    monkeypatch.setattr(poe, "_hmem_recall", lambda docs, run_id: [[] for _ in docs])
    monkeypatch.setattr(poe, "_hmem_remember", lambda docs, run_id: {})
    from app.services import listing_ref
    monkeypatch.setattr(listing_ref, "project_listing", lambda: {"ports": []})
    logs: list[str] = []
    docs = asyncio.run(poe._extract_and_geocode(dict(MQ), "ctx", [{"url": "https://www.mer.gouv.fr/x"}],
                                                logs.append, whitelist=["gouv.fr"]))
    assert len(docs) == 1
    assert {docs[0]["name"], *docs[0]["merged_names"]} == {"Le Marin", "La marina du Marin"}
    assert any("fusion:" in m for m in logs)
