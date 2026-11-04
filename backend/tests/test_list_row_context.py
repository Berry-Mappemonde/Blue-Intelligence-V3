"""A row of a list reaches C2 only through a judge "poe" on its own row.

Runs 20261104-171958-eb1b0c and 20261104-210014-db5bc9: Trois-Îlets stood at C2 with no verdict,
on two hosts and the word "douanes" of the next row. Exact texts: the douane list as those runs
read it and as d7963f read it, and the rows of the posts table on puertos.es.
"""
import inspect
import json
from pathlib import Path

import pytest

from app.services import border_crossing
from app.services import v3_certify as vc

FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = ("https://www.douane.gouv.fr/sites/default/files/2026-01/30/"
       "liste-points-clearance-agrees-mise-a-jour.pdf")
OTHER = "https://www.martinique.gouv.fr/guide.html"
WHITELIST = ["douane.gouv.fr", "martinique.gouv.fr", "puertos.es", "boe.es", "europa.eu"]
MARTINIQUE = {"name": "Martinique", "geoname": "Martinique", "mrgid": 33178}
CANARIES = {"name": "Canary Islands", "geoname": "Canary Islands", "mrgid": 8364}
REGIE = "Régie Port de Plaisance Marina de la pointe du bout"
POINTE_BATEAU = "Capitainerie Marina - Pointe Bateau"
CAPITAINERIE = "Port de plaisance Capitainerie du port (nouvelle extension)"
ETANG = "Port de plaisance communautaire de l'Étang Z'abricots"
ROWS = (REGIE, POINTE_BATEAU, CAPITAINERIE)
BRIGADE = "Trinité Brigade de surveillance des douanes Cité des douanes Quartier Raisinier"

POSTS = (
    "Punto Entrada (Puerto Exterior) Arrecife (Lanzarote) ES ACE 1P Arrecife (Lanzarote) I III IV "
    "Punto de entrada autorizado (Aduana marítima) SANIDAD ANIMAL 8-20 horas "
    "Puerto Rosario (Fuerteventura) ES FUE 1P Puerto Rosario (Fuerteventura) I III IV "
    "Punto de entrada autorizado (Aduana marítima) I III IV Las Palmas de Gran Canaria ES LPA 1 "
    "Las Palmas Las Palmas LAS PALMAS R (UE) 2017/625 art 44 PNAO HC (food) "
    "CASTELLÓN Castellón ES CAS 1 R (UE) 2017/625 art 44 GIJÓN Gijón ES GIJ 1 BILBAO ES BIO 1 ")
POSTS_URL = "https://www.puertos.es/system/files/2024-09/Lista.pdf"


def _flat_list() -> str:
    """The list as the runs read it (N0-tavily-extract): one line, md5 28596626…"""
    sources = json.loads((FIXTURES / "martinique_51697e_sources.json").read_text(encoding="utf-8"))
    return next(s["text"] for s in sources["sources"] if s["url"] == URL)


def _lined_list() -> str:
    raw = (FIXTURES / "douane_liste_points_clearance.txt").read_text(encoding="utf-8")
    return "".join(ln for ln in raw.splitlines(True) if not ln.startswith("# FIXTURE "))


def _port(name, *, excerpt=None, verdict=None, agreement=False, mrgid=33178, zone="Martinique"):
    """A record read by the LLM from two official hosts, as every port of a zone is."""
    port = {"name": name, "mrgid": mrgid, "zone_name": zone, "lat": 14.5, "lon": -61.0,
            "validated": True, "spatial_kind": "in_eez", "extraction_engine": "nightshift",
            "extraction_agreement": agreement, "source_urls": [URL, OTHER], "confidence": 60}
    if verdict:
        port["judge"] = {"verdict": verdict, "reason": "stub", "quoted_excerpt": excerpt or name,
                         "excerpt_verified": True}
    return port


def _level(port, texts, zone=MARTINIQUE):
    return vc.assess_port(port, whitelist=WHITELIST, texts=texts, zone=zone)


# --- the rows of the douane list -------------------------------------------------------

@pytest.mark.parametrize("name", ROWS)
def test_a_row_gets_no_entry_context_from_the_list(name):
    texts = [{"url": URL, "text": _flat_list()}]
    out = _level(_port(name), texts)
    assert out["confidence_level"] == "C1"
    assert out["confidence_cap_reason"] == vc.CAP_NO_ENTRY_CONTEXT


def test_the_neighbouring_row_was_what_gave_the_context():
    text = _flat_list()
    assert vc.entry_mentions(REGIE, [text])  # "douanes", on the next row
    assert not vc.entry_mentions(REGIE, [text], rows=False)


def test_llm_ner_agreement_is_no_way_round_it():
    out = _level(_port(REGIE, agreement=True), [{"url": URL, "text": _flat_list()}])
    assert out["confidence_level"] == "C1"


@pytest.mark.parametrize("name", ROWS + (ETANG,))
def test_a_judge_poe_on_its_own_row_gives_c2(name):
    out = _level(_port(name, verdict="poe"), [{"url": URL, "text": _flat_list()}])
    assert out["confidence_level"] == "C2"


def test_a_judge_poe_on_a_neighbouring_row_gives_nothing():
    out = _level(_port(REGIE, verdict="poe", excerpt=BRIGADE), [{"url": URL, "text": _flat_list()}])
    assert out["confidence_level"] == "C1"


def test_a_name_written_with_its_article_is_on_its_own_row():
    port = _port("La Trinité", verdict="poe", excerpt=BRIGADE)
    assert not vc.judge_excerpt_on_other_row(port, [{"url": URL, "text": _flat_list()}], WHITELIST)


@pytest.mark.parametrize("read", [_flat_list, _lined_list])
@pytest.mark.parametrize("name", ROWS + (ETANG, "Station-service CAP Ponton N°3"))
def test_rows_are_read_in_both_forms_of_the_list(read, name):
    text = read()
    start = " ".join(text.split()).casefold().find(name.split(" - ")[0].casefold()[:20])
    assert start >= 0
    assert vc.list_row_spans(text)
    assert not vc.entry_mentions(name, [text], rows=False)


# --- prose keeps the window rule -------------------------------------------------------

PROSE = ("Les plaisanciers peuvent effectuer leur clearance à la marina du Vieux Bassin, "
         "point de clearance de la commune, ouvert tous les jours. La douane du port y reçoit "
         "les équipages à leur arrivée de l'étranger.")
FOOTER = (" " + "Les formalités sont décrites sur le site de la préfecture. " * 8
          + "Contact : prefecture@martinique.gouv.fr, +596 596 391 500, "
            "accueil@martinique.gouv.fr, +596 596 391 501.")


@pytest.mark.parametrize("page", [PROSE, PROSE + FOOTER])
def test_a_prose_page_saying_point_de_clearance_keeps_its_context(page):
    assert vc.is_clearance_list(vc_tokens(page))
    assert vc.list_row_spans(page) == ()
    assert vc.entry_mentions("Marina du Vieux Bassin", [page], rows=False)
    out = _level(_port("Marina du Vieux Bassin"), [{"url": OTHER, "text": page}])
    assert out["confidence_level"] == "C2"


def test_prose_in_a_document_that_also_has_rows_keeps_its_context():
    text = PROSE + " " * 400 + "\n" + _flat_list()
    assert vc.entry_mentions("Marina du Vieux Bassin", [text], rows=False)


def vc_tokens(text):
    from app.agents import registry_check as rc
    return rc._tokens(text)


# --- border crossing lists -------------------------------------------------------------

ANNEX4 = ("Border crossing points. SPAIN Sea borders (26) Arrecife (Lanzarote) "
          "(27) Puerto del Rosario (Fuerteventura) (28) La Luz (Las Palmas) (29) Aduana del Puerto\n")
ORDEN = ("Puestos fronterizos. Puerto de Arrecife. Comisaría de la aduana de Lanzarote. "
         "Puerto de Las Palmas. Aeropuerto de Gando.")


@pytest.mark.parametrize("text,name", [(ANNEX4, "Arrecife (Lanzarote)"),
                                       (ORDEN, "Puerto de Arrecife")])
def test_a_row_of_a_border_crossing_list_gets_no_context_from_its_neighbours(text, name):
    assert border_crossing.row_spans(text)
    assert vc.entry_mentions(name, [text])
    assert not vc.entry_mentions(name, [text], rows=False)


def test_a_sentence_of_a_border_crossing_list_is_not_a_row():
    text = ("Border crossing points of the State. Puerto de la Marina is open to customs clearance "
            "for vessels arriving from third countries all year round, as the port of entry of the "
            "island and of every neighbouring island of the group.")
    assert border_crossing.row_spans(text) == []


def test_a_text_that_is_no_border_crossing_list_has_no_rows():
    assert border_crossing.row_spans("(1) Puerto de Arrecife (2) Puerto de Las Palmas") == []


# --- a table of border control posts ---------------------------------------------------

@pytest.mark.parametrize("name", ["Las Palmas de Gran Canaria", "Arrecife (Lanzarote)",
                                  "Puerto del Rosario (Fuerteventura)"])
def test_a_post_of_the_table_reaches_c2_only_through_the_judge(name):
    texts = [{"url": POSTS_URL, "text": POSTS}]
    port = _port(name, mrgid=8364, zone="Canary Islands")
    assert _level(port, texts, CANARIES)["confidence_level"] == "C1"
    judged = _port(name, verdict="poe", excerpt=name, mrgid=8364, zone="Canary Islands")
    assert _level(judged, texts, CANARIES)["confidence_level"] == "C2"


def test_the_word_of_the_post_above_gave_las_palmas_its_context():
    assert vc.entry_mentions("Las Palmas de Gran Canaria", [POSTS])
    assert not vc.entry_mentions("Las Palmas de Gran Canaria", [POSTS], rows=False)


def test_a_few_codes_do_not_make_a_table():
    assert vc.list_row_spans("The post ES ACE 1P and the post ES FUE 1P are open.") == ()


# --- what stays ------------------------------------------------------------------------

def test_the_zone_guard_still_reads_a_row():
    assert vc.entry_mentions(REGIE, [_flat_list()], rows=True)


def test_the_rule_names_no_port():
    code = "".join(inspect.getsource(f) for f in (
        vc.list_row_spans, vc._contact_rows, vc._without_list_rows, vc.judge_excerpt_on_other_row,
        border_crossing.row_spans))
    for name in ("Martinique", "Canar", "Palmas", "Arrecife", "Trois", "Trinité", "puertos.es"):
        assert name not in code
