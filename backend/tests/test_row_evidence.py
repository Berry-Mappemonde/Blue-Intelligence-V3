"""A row of a border crossing list or of a table of posts reaches the judge with its own text.

Run 20261105-002857-f7d809: the Canaries' judge had one text, from a numbered list of towns, and
answered "Not mentioned" for every post, because `zone_rows` reads no row of the flat Annex 4.
Offline.
"""
import pytest

from app.adapters import evidence_store
from app.agents import poe_judge
from app.agents import registry_check as rc
from app.services import border_crossing
from app.services import poe_pipeline as poe
from app.services import v3_certify as vc

URL = "https://home-affairs.ec.europa.eu/document/annex-04.pdf"
TABLE_URL = "https://www.puertos.es/lista.pdf"
WHITELIST = ["europa.eu", "puertos.es", "boe.es"]
CANARIES = {"name": "Canary Islands", "geoname": "Canary Islands", "mrgid": 8364}
ANNEX = ("Border crossing points. SPAIN Air borders (11) Fuerteventura (20) Lanzarote "
         "Sea borders (4) Arrecife (Lanzarote) (18) La Luz (Las Palmas) "
         "(28) Santa Cruz de Tenerife (29) Santander (34) Vigo Land borders (1) Ceuta\n")
TABLE = (
    "Puerto Rosario (Fuerteventura) ES FUE 1P Puerto Rosario (Fuerteventura) I III IV "
    "Punto de entrada autorizado (Aduana marítima) I III IV Las Palmas de Gran Canaria ES LPA 1 "
    "Las Palmas Las Palmas LAS PALMAS R (UE) 2017/625 art 44 PNAO HC (food) "
    "CASTELLÓN Castellón ES CAS 1 R (UE) 2017/625 art 44 GIJÓN Gijón ES GIJ 1 BILBAO ES BIO 1 "
    "HUELVA ES HUV 1 ")


@pytest.fixture(autouse=True)
def everywhere_in_zone(monkeypatch):
    monkeypatch.setattr(border_crossing, "in_zone", lambda name, zone: (True, "stub"))


def _records(names, text=ANNEX, url=URL):
    return evidence_store.build_records(8364, names, [{"url": url, "text": text}],
                                        places=("Canary Islands",), whitelist=WHITELIST,
                                        zone=CANARIES)


def test_zone_rows_reads_nothing_here_which_is_the_bug():
    assert border_crossing.zone_rows(ANNEX, CANARIES, "Spain")[0] == []


@pytest.mark.parametrize("name,row", [("Santa Cruz de Tenerife", "(28) Santa Cruz de Tenerife"),
                                      ("La Luz (Las Palmas)", "(18) La Luz (Las Palmas)"),
                                      ("Puerto de Arrecife", "(4) Arrecife (Lanzarote)")])
def test_a_row_of_the_flat_annex_reaches_the_judge_with_its_own_text(name, row):
    (record,) = _records([name])
    assert record["snippet"] == row
    assert record["list_row"] == vc.LIST_ROW_BORDER


def test_an_island_in_brackets_is_not_the_port():
    (record,) = _records(["Arrecife (Lanzarote)"])
    assert record["snippet"] == "(4) Arrecife (Lanzarote)"


def test_the_row_of_a_table_stops_before_the_next_post_and_leaves_the_post_above_out():
    (record,) = _records(["Las Palmas de Gran Canaria"], TABLE, TABLE_URL)
    assert record["snippet"].startswith("Las Palmas de Gran Canaria ES LPA 1")
    assert "Aduana" not in record["snippet"] and "CASTELLÓN Castellón ES CAS" not in record["snippet"]
    assert record["list_row"] == vc.LIST_ROW_POSTS


def test_a_row_of_a_table_keeps_its_own_wording():
    (record,) = _records(["Puerto Rosario (Fuerteventura)"], TABLE, TABLE_URL)
    assert "Punto de entrada autorizado (Aduana marítima)" in record["snippet"]


def test_a_row_outside_the_zone_gets_no_row_mention(monkeypatch):
    monkeypatch.setattr(border_crossing, "in_zone", lambda name, zone: (False, "elsewhere"))
    assert not [r for r in _records(["Santander"]) if r.get("list_row")]


def test_an_entry_of_a_numbered_list_of_towns_is_not_a_row():
    text = ("Puestos fronterizos. 86. Puerto Real. 87. Puerto del Rosario. 88. Puertollano. "
            "Comisaría de Arrecife. Puerto de Arrecife. Comisaría de Las Palmas.")
    rows = [text[a:b] for a, b in border_crossing.row_spans(text)]
    assert "Puerto de Arrecife." in rows
    assert not [r for r in rows if "Rosario" in r or "Real" in r]


def test_prose_gets_no_row_mention():
    text = "The port of La Luz receives foreign yachts all year round at its marina office."
    assert not [r for r in _records(["La Luz"], text) if r.get("list_row")]


def test_a_clearance_list_still_goes_through_its_own_mention():
    assert vc.own_row("Port de plaisance Bureau du Port +596 596 751 157 a@b.fr", 0, 10) is None


def test_the_judge_sees_what_the_text_is_a_row_of():
    prompt = poe_judge.judge_prompt("Canary Islands", [{"name": "Vigo"}], [
        {"url": URL, "text": "(34) Vigo", "list_row": vc.LIST_ROW_BORDER}])
    assert f"[1] {URL} ({vc.LIST_ROW_BORDER})\n(34) Vigo" in prompt


def test_a_short_row_quoted_whole_is_a_verified_excerpt():
    texts = [{"url": URL, "text": "(34) Vigo", "list_row": vc.LIST_ROW_BORDER}]
    assert poe_judge.excerpt_verified("(34) Vigo", texts)
    assert not poe_judge.excerpt_verified("(34) Vigo", [{"url": URL, "text": "(34) Vigo"}])
    assert not poe_judge.excerpt_verified("Vigo", texts)


def test_the_judge_texts_carry_the_row(monkeypatch):
    monkeypatch.setattr(evidence_store, "documents_from_context",
                        lambda raw, sources: [{"url": URL, "text": ANNEX}])
    (text,) = poe.judge_texts(CANARIES, [{"name": "Santa Cruz de Tenerife"}], "", [], WHITELIST)
    assert text == {"url": URL, "text": "(28) Santa Cruz de Tenerife",
                    "list_row": vc.LIST_ROW_BORDER}
