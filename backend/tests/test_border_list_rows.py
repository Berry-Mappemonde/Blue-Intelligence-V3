"""One post, one row: a designating list as a run reads it (extract_cascade), not as
`pdftotext -layout` gives it. Fixtures are the two documents through extract_cascade,
verbatim. Offline: no network, no Mongo.

Read that way, each document gave its five Canary posts plus one duplicate: Annex 4's line
"Puerto de Santa Cruz de La Palma (La Palma)" is seen by both parsers, and the index of
police stations in the Orden ("87. Puerto del Rosario.") looks like a post.
"""
import hashlib
import inspect
from pathlib import Path

import pytest

from app.adapters import evidence_store
from app.services import border_crossing as bc
from app.services import gold_score, poe_pipeline as poe

FIXTURES = Path(__file__).parent / "fixtures"
ANNEX4 = (FIXTURES / "annex4_extract_cascade.txt").read_text(encoding="utf-8")
ORDEN = (FIXTURES / "orden_int_act_extract_cascade.txt").read_text(encoding="utf-8")
CANARIES = {"mrgid": 8364, "name": "Canary Islands", "geoname": "Canary Islands",
            "sov_iso2": "ES", "sovereign": "Spain"}
ANNEX4_POSTS = ["Arrecife (Lanzarote)", "La Luz (Las Palmas)", "Puerto del Rosario (Fuerteventura)",
                "Puerto de Santa Cruz de La Palma (La Palma)", "Santa Cruz de Tenerife"]
ORDEN_POSTS = ["Arrecife", "Las Palmas", "Puerto Rosario", "Santa Cruz de la Palma",
               "Santa Cruz de Tenerife"]


def _posts(text):
    return bc.zone_rows(text, CANARIES, "Spain")[0]


def _row(name):
    return {"name": name, "row": name, "matched": name, "how": "test"}


def _names(rows):
    return [r["name"] for r in bc.fold_contained_rows([_row(n) for n in rows])]


# --- the two real documents ----------------------------------------------------------

@pytest.mark.parametrize("text, chars, md5", [
    (ANNEX4, 57837, "0aa8dcf0bf37b981fef7ced4ab86450d"),
    (ORDEN, 119674, "1de736f41589c029a2ff03efa22867be"),
])
def test_fixtures_are_the_documents_as_extract_cascade_reads_them(text, chars, md5):
    assert len(text) == chars
    assert hashlib.md5(text.encode("utf-8")).hexdigest() == md5


@pytest.mark.parametrize("text, posts", [(ANNEX4, ANNEX4_POSTS), (ORDEN, ORDEN_POSTS)])
def test_exactly_five_canary_posts_each_once(text, posts):
    assert sorted(r["name"] for r in _posts(text)) == sorted(posts)


def test_the_duplicates_are_folded_not_lost():
    annex = {r["name"]: r for r in _posts(ANNEX4)}
    orden = {r["name"]: r for r in _posts(ORDEN)}
    assert [f["name"] for f in annex["Puerto de Santa Cruz de La Palma (La Palma)"]["folded"]] == [
        "Santa Cruz de La Palma (La Palma)"]
    assert [f["name"] for f in orden["Puerto Rosario"]["folded"]] == ["Rosario"]
    assert [r for r in (*annex.values(), *orden.values())
            if r["name"] not in ("Puerto de Santa Cruz de La Palma (La Palma)", "Puerto Rosario")
            and r.get("folded")] == []


def test_both_documents_read_together_are_still_five_posts():
    rows = _posts(ANNEX4 + "\n" + ORDEN)
    assert sorted(r["name"] for r in rows) == sorted(ANNEX4_POSTS)


@pytest.mark.parametrize("text", [ANNEX4, ORDEN, ANNEX4 + "\n" + ORDEN])
def test_each_post_is_a_different_gold_port(text):
    gold = gold_score.load_gold(gold_score.GOLD_DIR / "pilot_4zones.json")
    zone = next(z for z in gold["zones"] if z["mrgid"] == 8364)
    index = gold_score._GoldIndex(zone, gold_score.load_aliases().get(8364, {}))
    hits = [index.match([r["name"]])[0] for r in _posts(text)]
    assert all(h and h["kind"] == "poe" for h in hits)
    assert sorted(h["name"] for h in hits) == sorted(e["name"] for e in zone["poe"])


@pytest.mark.parametrize("text, posts", [(ANNEX4, ANNEX4_POSTS), (ORDEN, ORDEN_POSTS)])
def test_a_folded_row_makes_no_candidate(text, posts):
    ports, _missed = poe.zone_catalog(text, CANARIES)
    listed = [p["name"] for p in ports if p.get("extraction_engine") == "border_list"]
    assert sorted(listed) == sorted(posts)


@pytest.mark.parametrize("text, name, post", [
    (ORDEN, "Rosario", "Puerto Rosario"),
    (ORDEN, "Puerto del Rosario", "Puerto Rosario"),
    (ANNEX4, "Santa Cruz de La Palma (La Palma)", "Puerto de Santa Cruz de La Palma (La Palma)"),
])
def test_a_folded_name_still_finds_the_row_as_evidence(text, name, post):
    mention = evidence_store._border_row_mention({"url": "https://example.org/list", "text": text},
                                                 name, CANARIES, {})
    assert mention and mention["matched_name"] == post and mention["gap"] == 0


# --- the rule ------------------------------------------------------------------------

def test_the_post_is_found_whichever_row_was_read_first():
    assert _names(["Rosario", "Arrecife", "Puerto Rosario"]) == ["Arrecife", "Puerto Rosario"]
    assert _names(["Puerto Rosario", "Arrecife", "Rosario"]) == ["Puerto Rosario", "Arrecife"]


def test_reading_order_is_kept():
    assert _names(["Vigo", "Bilbao", "Cartagena"]) == ["Vigo", "Bilbao", "Cartagena"]


def test_short_particles_do_not_count():
    assert _names(["Puerto del Rosario", "Puerto Rosario"]) == ["Puerto del Rosario"]


def test_same_name_twice_is_one_post():
    assert _names(["Santa Cruz de Tenerife", "Santa Cruz de Tenerife"]) == ["Santa Cruz de Tenerife"]


def test_only_whole_words_are_contained():
    assert _names(["Las Palmas", "Palma"]) == ["Las Palmas", "Palma"]
    assert _names(["Portosanto", "Santo Domingo"]) == ["Portosanto", "Santo Domingo"]


def test_two_posts_that_share_words_stay_two_posts():
    names = ["Santa Cruz de Tenerife", "Santa Cruz de La Palma"]
    assert _names(names) == names


def test_a_row_inside_several_posts_is_never_guessed():
    names = ["Santa Cruz", "Santa Cruz de Tenerife", "Santa Cruz de La Palma"]
    assert _names(names) == names


def test_the_rule_names_no_port():
    code = inspect.getsource(bc.fold_contained_rows) + inspect.getsource(bc._contains)
    for word in ("Rosario", "Palma", "Tenerife", "Arrecife", "Canar"):
        assert word not in code
