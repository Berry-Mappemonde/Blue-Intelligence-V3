"""The douane ICS list is a cargo-only document: it can never give C2, judge verdict included,
like tariff schedules and studies (#45). Exact text and rows of run 20261103-181506-d7963f.
Offline."""
import pytest

from app.services import v3_certify

URL = ("https://www.douane.gouv.fr/sites/default/files/uploads/files/Services-en-ligne/ICS/"
       "ics-liste-points-d-entree-francais.pdf")
OTHER = "https://www.douane.gouv.fr/particuliers/liste.pdf"
GUIDE = "https://www.martinique.gouv.fr/guide.pdf"
HEAD = ("Bureau A3 mars 2018 LISTE DES BUREAUX POINTS D'ENTRÉE FRANÇAIS DANS L'UNION EUROPÉENNE "
        "Sont ici repris les noms des ports et aéroports qui constituent des premiers points "
        "d'entrée, ou des points subséquents, ainsi que les codes des bureaux de douane qui leur "
        "sont rattachés. ")
FORT_DE_FRANCE = "Fort-de-France (Martinique) Fort-de-France port FR006340 Maritime (P) Routier (R) MQFDF"
LE_ROBERT = "Le Robert (Martinique) Fort-de-France port FR006340 Maritime (P) Routier (R)"
SAINT_PIERRE = "Saint-Pierre (Martinique) Fort-de-France port FR006340 Maritime (P) Routier (R) MQSPI"
ICS = (HEAD + "I- Liste des ports maritimes et fluvio-maritimes Nom du point d'entrée Nom du "
       "bureau Code bureau Trafic Code port " + "Ajaccio Ajaccio FR000040 Maritime (P) " * 100
       + f"DOM {FORT_DE_FRANCE} {LE_ROBERT} {SAINT_PIERRE} Longoni (Mayotte) Longoni port")
TEXTS = [{"url": URL, "text": ICS}]
WHITELIST = ["douane.gouv.fr", "martinique.gouv.fr"]
MARTINIQUE = {"name": "Martinique", "geoname": "Martinique", "mrgid": 33178}
ROWS = [("Le Robert (Martinique)", "Le Robert", LE_ROBERT),
        ("Saint-Pierre (Martinique)", "Saint-Pierre", SAINT_PIERRE),
        ("Fort-de-France (Martinique)", "Fort-de-France", FORT_DE_FRANCE)]


def _port(name, city, excerpt=None, **extra):
    port = {"name": name, "city": city, "mrgid": 33178, "zone_name": "Martinique", "lat": 14.7,
            "lon": -61.0, "validated": True, "spatial_kind": "in_eez",
            "extraction_engine": "nightshift", "source_urls": [URL], "confidence": 60, **extra}
    if excerpt:
        port["judge"] = {"verdict": "poe", "reason": "", "quoted_excerpt": excerpt,
                         "excerpt_verified": True}
    return port


def _certified(port, texts=TEXTS):
    return v3_certify.attach_confidence_level(port, whitelist=WHITELIST, zone=MARTINIQUE,
                                              texts=texts)


@pytest.mark.parametrize("name,city,row", ROWS)
def test_a_judge_poe_on_an_ics_row_is_not_c2(name, city, row):
    port = _certified(_port(name, city, row))
    assert port["confidence_level"] == "C1"
    assert port["confidence_cap_reason"] == v3_certify.CAP_CARGO_LIST
    assert port["confidence_downgraded_from"] == "C2"


def test_the_excerpt_is_located_the_way_the_judge_verified_it():
    # poe_judge.excerpt_verified sets quotes, case and spacing aside.
    excerpt = '"le robert  (Martinique)   Fort-de-France PORT"'
    assert _certified(_port("Le Robert (Martinique)", "Le Robert", excerpt))[
        "confidence_cap_reason"] == v3_certify.CAP_CARGO_LIST


@pytest.mark.parametrize("name,city,row", ROWS)
def test_the_same_excerpt_off_the_ics_list_is_c2(name, city, row):
    # The document decides: the same row in a list that is not the ICS list lifts.
    assert _certified(_port(name, city, row), [{"url": OTHER, "text": row}])[
        "confidence_level"] == "C2"


@pytest.mark.parametrize("name,city,row", ROWS)
def test_the_llm_ner_agreement_on_the_ics_list_alone_is_not_c2(name, city, row):
    port = _certified(_port(name, city, extraction_agreement=True))
    assert port["confidence_level"] == "C1"
    assert port["confidence_cap_reason"] == v3_certify.CAP_CARGO_LIST


def test_two_official_hosts_never_count_the_ics_list_as_entry_context():
    near = HEAD + LE_ROBERT  # "bureaux de douane" within reach of the row
    assert v3_certify.entry_mentions("Le Robert (Martinique)", [near])
    texts = [{"url": URL, "text": near}]
    assert v3_certify._official_texts(texts, WHITELIST) == []
    port = _certified(_port("Le Robert (Martinique)", "Le Robert", source_urls=[URL, GUIDE]), texts)
    assert port["confidence_level"] == "C1"


def test_the_list_is_recognised_by_its_title_or_its_url():
    assert v3_certify.is_cargo_list({"url": OTHER, "text": ICS})
    assert v3_certify.is_cargo_list({"url": URL, "text": "Ajaccio FR000040"})
    assert not v3_certify.is_cargo_list({"url": OTHER, "text": LE_ROBERT})


def test_a_text_that_cites_the_list_is_not_the_list():
    text = ("Guide des formalités. " + "Le port de plaisance vous accueille. " * 10
            + "Voir la liste des bureaux points d'entrée français.")
    assert not v3_certify.is_cargo_list({"url": OTHER, "text": text})


def test_other_points_d_entree_wording_is_not_the_cargo_list():
    text = "Liste des points d'entrée ouverts au trafic international : Le Marin"
    assert not v3_certify.is_cargo_list({"url": OTHER, "text": text})


def test_another_document_still_counts_for_a_port_on_the_ics_list():
    # The rule takes the ICS list out; it doesn't blacklist the port.
    row = "Port de Fort-de-France : points de clearance"
    port = _certified(_port("Fort-de-France (Martinique)", "Fort-de-France", row),
                      TEXTS + [{"url": OTHER, "text": row}])
    assert port["confidence_level"] == "C2"


def test_a_judge_poe_with_no_ics_list_among_the_texts_is_untouched():
    assert not v3_certify._cargo_list_only(_port("Le Robert", "Le Robert"), [], WHITELIST,
                                           MARTINIQUE)
