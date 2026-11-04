"""Clearance points of a list reach the judge (run 20261103-181506-d7963f: Étang Z'abricots had
no evidence record, so the judge answered "not listed"). Offline."""
from pathlib import Path

import pytest

from app.adapters import evidence_store
from app.agents import registry_check as rc
from app.services import poe_pipeline as poe
from app.services import v3_certify

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "douane_liste_points_clearance.txt"
URL = ("https://www.douane.gouv.fr/sites/default/files/2026-01/30/"
       "liste-points-clearance-agrees-mise-a-jour.pdf")
WHITELIST = ["douane.gouv.fr"]
MRGID = 33178
TOURELLES = "Station CAP DCML Centre de carénage - Baie des Tourelles"
ZABRICOTS = "Port de plaisance communautaire de l'Étang Z'abricots"
MARTINIQUE = {"name": "Martinique", "geoname": "Martinique", "mrgid": MRGID}


def _list() -> str:
    raw = FIXTURE.read_text(encoding="utf-8")
    return "".join(ln for ln in raw.splitlines(True) if not ln.startswith("# FIXTURE "))


def _records(names, text=None, url=URL):
    return evidence_store.build_records(
        MRGID, names, [{"url": url, "text": _list() if text is None else text}],
        places=("Martinique",), whitelist=WHITELIST)


def test_fort_de_france_marina_has_evidence():
    (record,) = _records([ZABRICOTS])
    assert record["port_key"] == rc.port_key(ZABRICOTS) and record["url"] == URL
    assert "Bureau du Port : ZAC de l’Etang Z’Abricots" in record["snippet"]
    assert "Fort-de-France" in record["snippet"]
    assert len(record["snippet"]) <= rc.SNIPPET_CHARS


@pytest.mark.parametrize("name", [TOURELLES, "Cyberbase", "Snack BOUBOU", "L'alsace en Kay",
                                  "Office municipal du tourisme", "Sea Services ShipChandler",
                                  "Fort de France"])
def test_a_row_that_is_not_a_clearance_point_gets_no_row_mention(name):
    assert v3_certify.clearance_row_mention(name, _list(), MRGID, ("Martinique",)) is None


def test_the_clearance_point_is_a_judge_text(monkeypatch):
    monkeypatch.setattr(evidence_store, "documents_from_context",
                        lambda raw, sources: [{"url": URL, "text": _list()}])
    texts = poe.judge_texts(MARTINIQUE, [{"name": TOURELLES}, {"name": ZABRICOTS}], "", [],
                            WHITELIST)
    assert len(texts) == 1 and texts[0]["url"] == URL and "Z’abricots" in texts[0]["text"]


def test_a_row_near_an_entry_term_keeps_its_entry_mention():
    (record,) = _records(["Station-service CAP Ponton N°3"])
    assert record["matched_name"] == "station service cap ponton n 3"
    assert "Capitainerie du port" in record["snippet"]
    # entry_mention is unchanged; the row is capped at certification (test_clearance_points).


def test_a_row_is_no_evidence_off_a_clearance_list():
    no_list = _list().replace("Point de clearance", "Point d'accueil")
    assert not _records([ZABRICOTS], no_list)


def test_a_row_is_no_evidence_off_an_official_source():
    assert not _records([ZABRICOTS], url="https://www.marina-martinique.fr/liste.pdf")


def test_a_row_of_another_territorys_section_is_no_evidence_for_this_zone():
    for row in ("Capitainerie Marina Bas du Fort", "Marina Marigot Gare maritime"):
        assert not v3_certify.clearance_row_mention(row, _list(), MRGID, ("Martinique",))


def test_a_name_fragment_is_not_a_row():
    assert v3_certify.clearance_row_mention("Port", _list(), MRGID, ("Martinique",)) is None


def _port(verdict=None, excerpt="", verified=True):
    port = {"name": ZABRICOTS, "city": "Fort-de-France", "mrgid": MRGID, "zone_name": "Martinique",
            "lat": 14.6027962, "lon": -61.0676724, "validated": True, "spatial_kind": "in_eez",
            "extraction_engine": "nightshift", "source_urls": [URL], "confidence": 60}
    if verdict:
        port["judge"] = {"verdict": verdict, "reason": "", "quoted_excerpt": excerpt,
                         "excerpt_verified": verified}
    return port


def _level(port):
    v3_certify.attach_confidence_level(port, whitelist=WHITELIST, zone=MARTINIQUE,
                                       texts=[{"url": URL, "text": _list()}])
    return port["confidence_level"]


def test_a_row_alone_lifts_nothing():
    assert not v3_certify.entry_mentions(ZABRICOTS, [_list()])
    assert _level(_port()) == "C1"


def test_the_judge_decides_on_the_row():
    excerpt = "Port de plaisance communautaire de l’Étang Z’abricots"
    assert _level(_port("poe", excerpt)) == "C2"
    assert _level(_port("not_poe", excerpt)) == "C1"


def test_fort_de_france_takes_its_marinas_level_not_the_careening_stations():
    excerpt = "Port de plaisance communautaire de l’Étang Z’abricots"
    row = _port("poe", excerpt)
    _level(row)
    careening = {**_port("poe", "Baie des Tourelles"), "name": TOURELLES}
    assert _level(careening) == "C1"
    docs, _ = poe.group_clearance_points([careening, row], [{"url": URL, "text": _list()}],
                                         MARTINIQUE, WHITELIST)
    names = {d["name"]: d for d in docs}
    assert sorted(names) == ["Fort-de-France", TOURELLES]
    assert names["Fort-de-France"]["confidence_level"] == "C2"
    assert [p["name"] for p in names["Fort-de-France"]["clearance_points"]] == [ZABRICOTS]
    assert names[TOURELLES]["clearance_excluded"]["reason"] == "careening station"


# --- a name with an apostrophe -------------------------------------------------------

@pytest.mark.parametrize("name,text", [
    (ZABRICOTS, "Port de plaisance communautaire de l’Étang Z’abricots"),
    (ZABRICOTS, "Port de plaisance communautaire de l'Etang Z'Abricots"),
    ("Port d'Ajaccio", "le port d’Ajaccio"),
])
def test_a_name_with_an_apostrophe_matches_the_text(name, text):
    tokens = rc._tokens(text)
    assert any(next(rc.name_spans(text, tokens, v), None) for v in rc.grounding_variants(name))


def test_the_apostrophe_form_is_a_full_name_for_weak_terms():
    text = "Capitainerie de la marina. Port de plaisance communautaire de l'Étang Z'abricots."
    assert rc.entry_mention(text, rc.grounding_variants(ZABRICOTS))


def test_a_possessive_stays_dropped():
    variants = {" ".join(v) for v in rc.grounding_variants("St George's Harbour")}
    assert "saint george harbour" in variants
    assert not any(v.endswith(" s") or " s " in v for v in variants)


def test_a_name_without_an_apostrophe_is_unchanged():
    assert rc._split_at_apostrophes("Fort-de-France", None) == []
