"""Per-territory lists: a zone reads its own row, never another territory's.

Fixtures are text taken from run 20261102-130435-60aa6f's sources (header comments give
the URL). Offline: stubbed LLM and NER, no Mongo.
"""
import asyncio
from pathlib import Path

import pytest

from app.core.extract import extract_structured_ports, split_territory_rows, territory_rows
from app.services import poe_pipeline as poe

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ARRETE = "arrete_2023_07_18_ports_trafic_international.txt"
ENAPOR = "enapor_2019_tariff_port_types.txt"
SOUFRIERE = "lc_customs_port_of_soufriere_bay_order.txt"
MAYOTTE_2015 = "yt_arrete_2015_02_04_annexe_i.txt"

MARTINIQUE = {"mrgid": 33178, "name": "Martinique", "iso2": "MQ"}
SPM = {"mrgid": 8494, "name": "Saint-Pierre and Miquelon", "iso2": "PM"}
CAPE_VERDE = {"mrgid": 8362, "name": "Cape Verde", "iso2": "CV"}
MAYOTTE = {"mrgid": 48944, "name": "Mayotte", "iso2": "YT"}

MQ_ROW = ["Fort-de-France", "Le Marin", "Le Robert", "Saint-Pierre", "Trinité"]
SPM_ROW = ["Miquelon", "Saint-Pierre"]
# Names the parser read from other rows of the same list in that run (Martinique, 8 ports).
OTHER_ROWS = {"Givet", "Gustavia", "Haute-Corse", "Bayonne", "Papeete-Tahiti", "Le Port",
              "Basse-Terre", "Mata UtuLiens relatifs"}


def _text(name: str) -> str:
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    return "".join(ln for ln in raw.splitlines(True) if not ln.startswith("#"))


def _source(url: str, body: str) -> str:
    return f"[SOURCE: {url}]\n{body}"


@pytest.mark.parametrize("name", [ARRETE, ENAPOR, SOUFRIERE, MAYOTTE_2015])
def test_fixtures_are_small_and_cite_their_source(name):
    path = FIXTURES / name
    assert path.stat().st_size < 20_000
    header = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("#")]
    assert any(ln.startswith("# Source: https://") for ln in header)


def test_martinique_reads_its_own_row_only():
    ports, missed = poe.zone_catalog(_text(ARRETE), MARTINIQUE)
    assert [p["name"] for p in ports] == MQ_ROW
    assert not missed
    assert all(p["territory_row"] == "Martinique" for p in ports)
    assert not {p["name"] for p in ports} & OTHER_ROWS


def test_saint_pierre_et_miquelon_reads_its_own_row_only():
    ports, missed = poe.zone_catalog(_text(ARRETE), SPM)
    assert [p["name"] for p in ports] == SPM_ROW
    assert not missed
    assert {p["territory_row"] for p in ports} == {"Saint-Pierre-et-Miquelon"}


def test_rows_are_read_inside_a_multi_source_text():
    raw = "\n\n".join([
        _source("https://gov.example/unrelated", "General information for visitors."),
        _source("https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000047858148", _text(ARRETE)),
    ])
    assert [p["name"] for p in poe.zone_catalog(raw, MARTINIQUE)[0]] == MQ_ROW
    assert [p["name"] for p in poe.zone_catalog(raw, SPM)[0]] == SPM_ROW


def test_zone_without_a_row_gets_nothing_from_the_list():
    ports, missed = poe.zone_catalog(_text(ARRETE), CAPE_VERDE)
    assert ports == [] and missed


def test_without_a_zone_the_parser_is_unchanged():
    text = _text(ARRETE)
    assert split_territory_rows(text, None, None) == (text, [], False)
    # The legacy parser still reads "Port de X" phrasing across rows (the bug the zone fixes).
    assert {"Gustavia", "Miquelon"} <= {p["name"] for p in extract_structured_ports(text)}


@pytest.mark.parametrize("name", [ENAPOR, SOUFRIERE])
def test_other_official_texts_are_not_read_as_per_territory_lists(name):
    known = poe.zone_row_names(CAPE_VERDE)[1]
    assert territory_rows(_text(name), known) == []
    text = _text(name)
    ports, missed = poe.zone_catalog(text, CAPE_VERDE)
    assert not missed
    assert [p["name"] for p in ports] == [p["name"] for p in extract_structured_ports(text)]


def test_mayotte_reads_both_arretes_through_the_same_path():
    # 2015 Annex I (Mayotte only, by border type) is not a per-territory list: the
    # 711840b reader keeps the maritime site and drops the airport, as before.
    annexe = _text(MAYOTTE_2015)
    assert territory_rows(annexe, poe.zone_row_names(MAYOTTE)[1]) == []
    ports, missed = poe.zone_catalog(annexe, MAYOTTE)
    assert [p["name"] for p in ports] == ["Dzaoudzi"] and not missed
    # 2023 list: Mayotte's own row.
    assert [p["name"] for p in poe.zone_catalog(_text(ARRETE), MAYOTTE)[0]] == ["Longoni", "Dzaoudzi"]
    # Both sources in one zone text: the row and the maritime site, no airport, no other row.
    raw = "\n\n".join([
        _source("https://www.legifrance.gouv.fr/loda/id/JORFTEXT000030235682/", annexe),
        _source("https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000047858148", _text(ARRETE)),
    ])
    names = {p["name"] for p in poe.zone_catalog(raw, MAYOTTE)[0]}
    assert names == {"Longoni", "Dzaoudzi"}


def test_bottom_up_harvest_reads_the_same_row():
    # judge_one's catalogue double reading (§6.3) uses the same rule as Top-Down.
    from app.services.poe_seed_enrich import harvest_bu_catalog
    url = "https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000047858148"
    fetched = {url: {"text": _text(ARRETE)}}
    mq = harvest_bu_catalog(fetched, [url], MARTINIQUE)
    assert [p["name"] for p in mq["ports"]] == MQ_ROW
    assert mq["sufficient"] and mq["urls"] == [url]
    cv = harvest_bu_catalog(fetched, [url], CAPE_VERDE)
    assert cv["ports"] == [] and not cv["sufficient"]
    # Without a zone (older callers) the harvest is unchanged.
    assert "Gustavia" in {p["name"] for p in harvest_bu_catalog(fetched, [url])["ports"]}


def test_row_headers_use_the_territory_records_names():
    zone_names, known = poe.zone_row_names(SPM)
    assert {"saint pierre et miquelon"} <= zone_names
    assert {"martinique", "guadeloupe", "mayotte", "wallis et futuna"} <= known


@pytest.fixture
def stub_readers(monkeypatch):
    """LLM returns nothing but records it was asked; NER is silent."""
    calls: list[str] = []

    async def fake_extract(context, zone, settings=None, log=None):
        calls.append(context)
        return []

    import app.core.ml as ml
    monkeypatch.setattr(poe, "extract_ports", fake_extract)
    monkeypatch.setattr(ml, "extract_entities", lambda text: [])
    return calls


def _compared(zone, raw, logs):
    return asyncio.run(poe._extract_ports_compared(raw, zone, logs.append, catalog_text=raw))


def test_llm_is_not_skipped_when_no_row_matches_the_zone(stub_readers, monkeypatch):
    # Even a catalog judged sufficient must not stand in for the LLM on a zone with no row.
    monkeypatch.setattr(poe, "catalog_is_sufficient", lambda ports, text="": True)
    logs: list[str] = []
    ports = _compared(CAPE_VERDE, _text(ARRETE), logs)
    assert stub_readers, logs
    assert any("sans ligne pour cette zone" in m for m in logs)
    assert not {p["name"] for p in ports} & (OTHER_ROWS | set(MQ_ROW) | set(SPM_ROW))


def test_zone_row_is_kept_when_the_llm_runs(stub_readers):
    logs: list[str] = []
    ports = _compared(MARTINIQUE, _text(ARRETE), logs)
    names = {p["name"] for p in ports}
    assert set(MQ_ROW) <= names
    assert not names & (OTHER_ROWS | {"Miquelon"})
