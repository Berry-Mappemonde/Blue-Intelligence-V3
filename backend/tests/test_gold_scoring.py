"""Gold scoring: Clément's hand-checked pilot (backend/data/gold/pilot_4zones.json) against
run ports. Offline: the 59c1e5 export (tests/fixtures/run_59c1e5_ports.csv), synthetic
runs and an in-memory store; no Mongo, no network."""
import asyncio
import json
from pathlib import Path

import pytest

from app.services import gold_score as gs

BACKEND = Path(__file__).resolve().parents[1]
GOLD = BACKEND / "data" / "gold" / "pilot_4zones.json"
CSV_59C1E5 = BACKEND / "tests" / "fixtures" / "run_59c1e5_ports.csv"
LC, MQ, CV, CN = 8416, 33178, 8362, 8364


@pytest.fixture(scope="module")
def gold():
    return gs.load_gold(GOLD)


def _zone(score, mrgid):
    return next(z for z in score["zones"] if z["mrgid"] == mrgid)


def _p(mrgid, name, level="C1", merged=(), points=(), quote="", judge=None):
    return gs.run_port({"mrgid": mrgid, "name": name, "merged_names": list(merged),
                        "clearance_points": [{"name": n, "quote": f"row {n}"} for n in points],
                        "confidence_level": level,
                        "judge": {"verdict": judge, "quoted_excerpt": quote} if judge else {}})


# --- the gold file ---------------------------------------------------------------------

def test_gold_file_is_clements_pilot(gold):
    assert gold["checked_by"] == "Clément" and gold["run"] == "20261103-161330-59c1e5"
    assert set(gold["criteria"]) == {"all", "Martinique", "Cape Verde", "Canary Islands"}
    assert gs.gold_mrgids(gold) == {LC, MQ, CV, CN}
    assert sum(len(z["poe"]) for z in gold["zones"]) == 23
    assert sum(len(z["not_poe"]) for z in gold["zones"]) == 23
    assert all(e.get("note") for z in gold["zones"] for e in z["poe"] + z["not_poe"])


def test_gold_lookup_by_zones():
    path, gold = gs.gold_for_zones({LC, MQ})
    assert path.name == "pilot_4zones.json"
    assert gs.gold_for_zones({LC, 5677}) is None  # a zone the gold does not cover
    assert gs.gold_for_zones(set()) is None


# --- run 59c1e5 (the run the gold was checked against) ----------------------------------

def test_59c1e5_counts(gold):
    score = gs.score_ports(gs.ports_from_csv(CSV_59C1E5), gold)
    o = score["overall"]
    assert (o["c2_precision"]["n"], o["c2_precision"]["of"]) == (8, 8)
    assert (o["c2_recall"]["n"], o["c2_recall"]["of"]) == (8, 23)
    assert (o["any_level_recall"]["n"], o["any_level_recall"]["of"]) == (16, 23)
    assert o["wrong_c2"] == 0 and o["unmatched"] == 0
    # 22, not 23: the record named after the Étang Z'abricots row is the gold marina.
    assert o["missed"] == 7 and o["c1_noise"] == 22
    assert _zone(score, LC)["c2_recall"]["n"] == 5
    assert _zone(score, MQ)["c2_recall"]["n"] == 3 and _zone(score, MQ)["any_level_recall"]["n"] == 4
    assert _zone(score, CV)["any_level_recall"]["n"] == 7
    assert _zone(score, CV)["missed"] == ["Mindelo – Porto Grande de São Vicente", "Porto Inglês (Maio)"]
    assert len(_zone(score, CN)["missed"]) == 5 and len(_zone(score, CN)["c1_noise"]) == 15
    # #47's merged names are read: Palmeira's record also carries "Porto de Palmeira"
    assert "Palmeira" not in _zone(score, CV)["missed"]


def test_cli_scores_an_export(capsys):
    import sys
    sys.path.insert(0, str(BACKEND / "scripts"))
    import score_gold
    assert score_gold.main(["--csv", str(CSV_59C1E5), "--gold", str(GOLD)]) == 0
    out = capsys.readouterr().out
    assert "C2 precision 8/8" in out and "C2 recall 8/23" in out and "any-level recall 16/23" in out
    assert "MISSED    Porto Inglês (Maio)" in out


# --- a synthetic run ------------------------------------------------------------------

def test_aliases_and_name_forms(gold):
    ports = [
        _p(MQ, "Le Marin", "C2"),                       # alias of the Le Marin capitainerie
        _p(MQ, "Le François", "C2"),                    # alias of Pointe Bateau
        _p(CV, "São Nicolau", "C1"),                    # alias of Tarrafal
        _p(CV, "Porto Grande", "C2"),                   # alias of Mindelo – Porto Grande
        _p(CN, "Puerto de La Luz", "C2"),               # one side of the gold name's en dash
        _p(CN, "Arrecife", "C1"),                       # the gold name without its parenthesis
        _p(LC, "Vieux Fort", "C2"),                     # punctuation
    ]
    score = gs.score_ports(ports, gold)
    assert _zone(score, MQ)["c2_recall"]["n"] == 2
    assert "Tarrafal" not in _zone(score, CV)["missed"]
    assert "Mindelo – Porto Grande de São Vicente" not in _zone(score, CV)["missed"]
    assert _zone(score, CV)["c2_recall"]["n"] == 1
    assert _zone(score, CN)["c2_recall"]["n"] == 1 and _zone(score, CN)["any_level_recall"]["n"] == 2
    assert _zone(score, LC)["c2_recall"]["n"] == 1
    assert score["overall"]["unmatched"] == 0


def test_wrong_c2_carries_its_quote_and_counts_against_precision(gold):
    ports = [_p(MQ, "Le Robert (Martinique)", "C2", judge="poe",
                quote="Le Robert : point d'entrée ICS fret"),
             _p(MQ, "Capitainerie Marina - Pointe Bateau", "C2")]
    z = _zone(gs.score_ports(ports, gold), MQ)
    assert (z["c2_precision"]["n"], z["c2_precision"]["of"]) == (1, 2)
    (w,) = z["wrong_c2"]
    assert w["gold"] == "Le Robert (Martinique)" and w["quote"] == "Le Robert : point d'entrée ICS fret"
    assert "douane list" in w["gold_note"]


def test_unmatched_names_are_listed_not_guessed(gold):
    ports = [_p(CN, "Puerto Calero", "C2"), _p(CN, "Marina Rubicón", "C1")]
    score = gs.score_ports(ports, gold)
    z = _zone(score, CN)
    assert [u["name"] for u in z["unmatched"]] == ["Puerto Calero", "Marina Rubicón"]
    # an unmatched C2 is neither right nor wrong
    assert z["c2_precision"]["of"] == 0 and not z["wrong_c2"]


def test_clearance_points_and_merged_names_after_50(gold):
    """#50 groups a commune's rows into one PoE: one gold PoE among its names wins over a
    merged rejected name."""
    le_marin = _p(MQ, "Le Marin", "C2", merged=["La marina du Marin"],
                  points=["Port de plaisance Capitainerie du port (nouvelle extension)",
                          "Station-service CAP Ponton N°3"])
    trois_ilets = _p(MQ, "Trois-Îlets", "C2",
                     points=["Régie Port de Plaisance Marina de la pointe du bout"])
    z = _zone(gs.score_ports([le_marin, trois_ilets], gold), MQ)
    assert z["c2_recall"]["n"] == 2 and z["c2_precision"]["n"] == 2 and not z["unmatched"]


ETANG_ROW = "Port de plaisance communautaire de l'Étang Z'abricots"
ETANG_GOLD = "La marina de l'Étang Z'Abricot"


def test_fort_de_france_counts_for_its_marina(gold):
    """Run ac33e9: the record's name is rejected in the gold, and so is its row's exact name
    ("Duplicate of row 12"); the row is the same marina as the gold PoE."""
    z = _zone(gs.score_ports([_p(MQ, "Fort-de-France", "C1", points=[ETANG_ROW])], gold), MQ)
    assert ETANG_GOLD not in z["missed"]
    assert z["any_level_recall"]["n"] == 1 and not z["c1_noise"] and not z["unmatched"]


def test_a_poe_through_a_clearance_point_is_a_right_c2_not_a_wrong_one(gold):
    z = _zone(gs.score_ports([_p(MQ, "Fort-de-France", "C2", points=[ETANG_ROW])], gold), MQ)
    assert z["c2_recall"]["n"] == 1 and z["c2_precision"]["n"] == 1 and not z["wrong_c2"]


def test_a_rejected_name_with_no_poe_among_its_points_stays_rejected(gold):
    z = _zone(gs.score_ports([_p(MQ, "Fort-de-France", "C1", points=["Sea Services"]),
                              _p(MQ, "Fort-de-France", "C2")], gold), MQ)
    assert [c["gold"] for c in z["c1_noise"]] == ["Fort de France"]
    assert [w["gold"] for w in z["wrong_c2"]] == ["Fort de France"]


def test_a_rejected_clearance_point_never_takes_a_gold_poe_away(gold):
    p = _p(MQ, "Le Marin", "C2", merged=["La marina du Marin"],
           points=["Station CAP DCML Centre de carénage - Baie des Tourelles"])
    z = _zone(gs.score_ports([p], gold), MQ)
    assert z["c2_recall"]["n"] == 1 and not z["wrong_c2"]


def test_aliases_apply_to_clearance_point_names(gold):
    z = _zone(gs.score_ports([_p(CV, "São Vicente", "C2", points=["Porto Grande"])], gold), CV)
    assert z["c2_recall"]["n"] == 1 and "Mindelo – Porto Grande de São Vicente" not in z["missed"]


def test_the_port_own_name_decides_between_two_gold_poes(gold):
    p = _p(LC, "Castries", "C2", points=["Vieux-Fort"])
    z = _zone(gs.score_ports([p], gold), LC)
    assert not z["unmatched"] and z["c2_recall"]["n"] == 1 and "Castries" not in z["missed"]


@pytest.mark.parametrize("name, place", [
    (ETANG_ROW, "marina at letang zabricot"),
    (ETANG_GOLD, "marina at letang zabricot"),
    ("Marina du Marin", "marina at marin"),
    ("Puerto deportivo de Radazul", "marina at radazul"),
])
def test_two_marinas_at_one_place_have_one_key(name, place):
    assert gs._place_key(name) == place


@pytest.mark.parametrize("name", ["Fort-de-France", "Étang Z'abricots", "Marina",
                                  "Capitainerie Marina - Pointe Bateau"])
def test_a_name_that_is_no_marina_at_a_place_has_none(name):
    assert gs._place_key(name) is None


def test_a_bare_place_never_matches_a_marina(gold):
    z = _zone(gs.score_ports([_p(MQ, "Étang Z'Abricot", "C1"), _p(MQ, "Marin", "C1")], gold), MQ)
    assert z["any_level_recall"]["n"] == 0 and not z["c1_noise"] and len(z["unmatched"]) == 2


def test_a_marina_at_another_place_is_another_marina(gold):
    z = _zone(gs.score_ports([_p(MQ, "Marina de la Baie de Fort", "C1")], gold), MQ)
    assert z["any_level_recall"]["n"] == 0 and len(z["unmatched"]) == 1


def test_a_port_naming_two_gold_poes_is_ambiguous(gold):
    p = _p(LC, "Boarding stations", "C2", points=["Castries", "Vieux-Fort"])
    (u,) = _zone(gs.score_ports([p], gold), LC)["unmatched"]
    assert set(u["candidates"]) == {"Castries", "Vieux-Fort"}


def test_duplicates_count_a_gold_poe_once(gold):
    ports = [_p(LC, "Castries", "C2"), _p(LC, "Castries Harbour", "C2")]
    z = _zone(gs.score_ports(ports, gold), LC)
    assert z["c2_recall"]["n"] == 1 and z["c2_precision"]["n"] == 2


# --- stored run and run summary ---------------------------------------------------------

class _Coll:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query, projection=None):
        docs = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]

        async def gen():
            for d in docs:
                yield d
        return gen()


class _DB:
    def __init__(self, zones, ports):
        self.poe_run_zones, self.poe_run_ports = _Coll(zones), _Coll(ports)


def _stored(run_id, mrgids):
    ports = [{"run_id": run_id, "mrgid": p["mrgid"], "name": p["name"],
              "merged_names": p["names"][1:], "confidence_level": p["level"],
              "judge": {"verdict": p["judge"], "quoted_excerpt": p["quote"]}}
             for p in gs.ports_from_csv(CSV_59C1E5) if p["mrgid"] in mrgids]
    return _DB([{"run_id": run_id, "mrgid": m} for m in mrgids], ports)


def test_stored_run_is_scored_with_the_gold_that_covers_it():
    db = _stored("run-x", [LC, MQ, CV, CN])
    score = asyncio.run(gs.score_run(db, "run-x"))
    assert score["gold_file"] == "pilot_4zones.json"
    assert score["overall"]["c2_recall"]["n"] == 8 and score["overall"]["any_level_recall"]["n"] == 16
    view = gs.summary_view(score)
    json.dumps(view)  # storable
    assert view["overall"] == score["overall"]
    assert all("gold_note" not in w for z in view["zones"] for w in z["wrong_c2"])


def test_run_outside_the_gold_gets_no_score():
    db = _DB([{"run_id": "run-y", "mrgid": 5677}], [])
    assert asyncio.run(gs.score_run(db, "run-y")) is None


def test_run_summary_writes_the_score():
    src = (BACKEND / "app" / "services" / "poe_runs.py").read_text(encoding="utf-8")
    assert 'summary["gold_score"] = gold_score.summary_view(score) if score else None' in src
