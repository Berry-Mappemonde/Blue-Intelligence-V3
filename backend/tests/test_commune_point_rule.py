"""Two C2 rules accepted on 4 Nov 2026, offline.

1. In a zone with an official clearance list and a point read on it, a record named after a
   commune reaches C2 only if the list has a clearance point under that commune (§10.2).
2. LLM∩NER needs the same entry context as two official hosts (§10.1).

Exact text: the douane list of run 20261103-181506-d7963f.
"""
from pathlib import Path

import pytest

from app.services import poe_pipeline as poe
from app.services import v3_certify

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "douane_liste_points_clearance.txt"
URL = ("https://www.douane.gouv.fr/sites/default/files/2026-01/30/"
       "liste-points-clearance-agrees-mise-a-jour.pdf")
ARRETE = "https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000047858148"
WHITELIST = ["douane.gouv.fr", "legifrance.gouv.fr"]
MARTINIQUE = {"name": "Martinique", "geoname": "Martinique", "mrgid": 33178}
GUADELOUPE = {"name": "Guadeloupe", "geoname": "Guadeloupe"}
CAPITAINERIE = "Port de plaisance Capitainerie du port (nouvelle extension)"
REGIE = "Régie Port de Plaisance Marina de la pointe du bout"
POINTE_BATEAU = "Capitainerie Marina - Pointe Bateau"
ETANG = "Port de plaisance communautaire de l'Étang Z'abricots"


def _list() -> str:
    raw = FIXTURE.read_text(encoding="utf-8")
    return "".join(ln for ln in raw.splitlines(True) if not ln.startswith("# FIXTURE "))


TEXTS = [{"url": URL, "text": _list()},
         {"url": ARRETE, "text": "Ports ouverts au trafic international. Martinique "
                                 "Fort-de-France, le Marin, Le Robert, Saint-Pierre, Trinité."}]


def _judged(name, city=None, excerpt=None, mrgid=33178, zone="Martinique"):
    """A record the judge calls a PoE on a verified excerpt."""
    return {"name": name, "city": city, "mrgid": mrgid, "zone_name": zone, "lat": 14.7, "lon": -61.0,
            "validated": True, "spatial_kind": "in_eez", "extraction_engine": "nightshift",
            "source_urls": [URL], "confidence": 60,
            "judge": {"verdict": "poe", "reason": "stub", "quoted_excerpt": excerpt or name,
                      "excerpt_verified": True}}


def _run(records, texts=TEXTS, zone=MARTINIQUE):
    """The levels, then the grouping, as generate_zone_poe does. Returns ({name: doc}, grouped)."""
    for doc in records:
        v3_certify.attach_confidence_level(doc, whitelist=WHITELIST, texts=texts, zone=zone)
    docs, grouped = poe.group_clearance_points(records, texts, zone, WHITELIST)
    return {d["name"]: d for d in docs}, grouped


def _with_points(*records):
    """The records, with rows of the list read, so the list counts and its headings are known."""
    return [*records, _judged(CAPITAINERIE, "Le Marin"), _judged(REGIE, "Les Trois-Îlets")]


# --- 1. a commune with no clearance point --------------------------------------------

@pytest.mark.parametrize("name,city", [("Trinité", None), ("Trinité", "La Trinité"),
                                       ("La Trinité", None), ("Trinité (Martinique)", "Trinité")])
def test_trinite_with_only_its_brigade_row_stays_c1_even_with_a_judge_poe(name, city):
    brigade = "Trinité Brigade de surveillance des douanes Cité des douanes Quartier Raisinier"
    record = _judged(name, city, brigade)
    v3_certify.attach_confidence_level(record, whitelist=WHITELIST, texts=TEXTS, zone=MARTINIQUE)
    assert record["confidence_level"] == "C2"  # what the judge alone would give
    docs, _ = _run(_with_points(record))
    capped = docs[name]
    assert capped["confidence_level"] == "C1" and capped["confidence_downgraded_from"] == "C2"
    assert capped["confidence_cap_reason"] == v3_certify.CAP_NO_CLEARANCE_POINT
    assert capped["clearance_excluded"]["reason"] == v3_certify.NO_POINT_ON_THE_LIST
    assert capped["clearance_excluded"]["quote"].startswith("Brigade de surveillance")
    assert capped["clearance_excluded"]["source"] == URL


def test_a_commune_with_a_tourist_office_and_a_shop_stays_c1():
    docs, _ = _run(_with_points(_judged("Saint-Pierre", None)))
    assert docs["Saint-Pierre"]["confidence_level"] == "C1"
    assert docs["Saint-Pierre"]["clearance_excluded"]["quote"].startswith("Office municipal")


@pytest.mark.parametrize("name", ["Le Robert", "Le Robert (Martinique)"])
def test_a_commune_the_list_does_not_head_stays_c1(name):
    docs, _ = _run(_with_points(_judged(name, None)))
    assert docs[name]["confidence_level"] == "C1"
    assert docs[name]["confidence_cap_reason"] == v3_certify.CAP_NO_CLEARANCE_POINT


@pytest.mark.parametrize("name,city", [("Le Marin", None), ("Le François", None),
                                       ("Les Trois-Îlets", None), ("Fort-de-France", None),
                                       ("Fort de France", None),
                                       ("Fort-de-France (Martinique)", "Fort-de-France")])
def test_a_commune_with_a_point_on_the_list_can_reach_c2(name, city):
    # The list decides: no row of Le François or Fort-de-France was read here.
    docs, _ = _run(_with_points(_judged(name, city)))
    assert docs[name]["confidence_level"] == "C2" and "clearance_excluded" not in docs[name]


@pytest.mark.parametrize("row,city,poe_name", [
    (POINTE_BATEAU, "Le François", "Le François"), (CAPITAINERIE, "Le Marin", "Le Marin"),
    (REGIE, "Les Trois-Îlets", "Trois-Îlets"), (ETANG, "Le Lamentin", "Fort-de-France")])
def test_the_rows_still_reach_c2_as_their_communes_poe(row, city, poe_name):
    extra = [_judged("Station CAP DCML Centre de carénage - Baie des Tourelles", "Fort-de-France")]
    docs, grouped = _run([_judged(row, city), *extra])
    assert docs[poe_name]["confidence_level"] == "C2"
    assert [p["name"] for p in docs[poe_name]["clearance_points"]] == [row]


def test_the_qualifying_terms_are_read_under_the_heading():
    # A capitainerie, a marina, a port de plaisance, a harbour service.
    for name, city, zone in (("Gourbeyre", "Gourbeyre", GUADELOUPE),
                             ("Pointe-à-Pitre", "Pointe-à-Pitre", GUADELOUPE),
                             ("Deshaies", "Deshaies", GUADELOUPE)):
        row = _judged("Capitainerie Marina Bas du Fort", "Pointe-à-Pitre", zone="Guadeloupe")
        record = _judged(name, city, zone="Guadeloupe")
        for doc in (row, record):
            doc.pop("mrgid")
        docs, _ = _run([record, row], zone=zone)
        assert docs[name]["confidence_level"] == "C2", name


def test_any_zone_of_the_list_reads_its_own_communes():
    record = _judged("Marie-Galante", None, zone="Guadeloupe")
    row = _judged("Capitainerie Marina Bas du Fort", "Pointe-à-Pitre", zone="Guadeloupe")
    for doc in (row, record):
        doc.pop("mrgid")
    docs, _ = _run([record, row], zone=GUADELOUPE)
    assert docs["Marie-Galante"]["confidence_level"] == "C1"  # a brigade and a shop


def test_a_catalogue_commune_is_not_read_as_a_row_of_the_commune_above():
    # No city on the record, a known heading above it on the list.
    above = _judged("Station CAP DCML Centre de carénage - Baie des Tourelles", "Fort-de-France")
    docs, _ = _run(_with_points(_judged("Le François", None), above))
    assert docs["Le François"]["confidence_level"] == "C2"
    assert "clearance_excluded" not in docs["Le François"]


def test_a_commune_of_another_section_has_no_point_in_this_zone():
    docs, _ = _run(_with_points(_judged("Marigot", None)))
    assert docs["Marigot"]["confidence_level"] == "C1"


def test_a_facility_is_not_a_record_named_after_a_commune():
    name = "Port de commerce du Robert"
    docs, _ = _run(_with_points(_judged(name, "Le Robert")))
    assert docs[name]["confidence_level"] == "C2"


def test_a_missing_heading_can_only_spare_a_record():
    # No record gives the commune after Saint-Pierre: its text runs on to the next marina.
    docs, _ = _run([_judged("Saint-Pierre", None), _judged(CAPITAINERIE, "Le Marin")])
    assert docs["Saint-Pierre"]["confidence_level"] == "C2"


def test_a_capped_commune_stays_out_of_the_same_port_merge():
    docs, _ = _run(_with_points(_judged("Trinité", None), _judged("La Trinité", None)))
    merged, _ = poe.merge_same_ports(list(docs.values()))
    assert len([d for d in merged if "Trinité" in d["name"]]) == 2


# --- a zone with no list, or no point read, is unchanged -----------------------------

def test_a_zone_with_no_clearance_list_is_unchanged():
    texts = [{"url": URL, "text": _list().replace("Point de clearance", "Point d'accueil")}]
    records = _with_points(_judged("Trinité", None), _judged("Le Robert", None))
    docs, grouped = _run(records, texts)
    assert not grouped and {d["confidence_level"] for d in docs.values()} == {"C2"}


def test_a_list_with_no_point_read_caps_no_commune():
    docs, grouped = _run([_judged("Trinité", "Trinité"), _judged("Le Robert", "Le Robert")])
    assert not grouped and {d["confidence_level"] for d in docs.values()} == {"C2"}


def test_a_list_off_an_official_source_caps_no_commune():
    texts = [{"url": "https://www.marina-martinique.fr/liste.pdf", "text": _list()}]
    docs, _ = _run(_with_points(_judged("Trinité", None)), texts)
    assert "clearance_excluded" not in docs["Trinité"]


# --- 2. LLM∩NER needs an entry context ------------------------------------------------

DECRETO = "https://www.mf.gov.cv/documents/89129/1869986/Decreto-Regulamentar-8-2015.pdf"
LOCALITY = ("de Ribeira das Patas, no Concelho de Porto Novo; de Barreiro e de Calheta, no Concelho "
            "do Maio; de Palmeira, no Concelho do Sal; de Nossa Senhora de Monte, no Concelho da "
            "Brava; de Chã de Tanque")
CAPE_VERDE = {"name": "Cape Verde", "geoname": "Cape Verde", "mrgid": 8362}


def _palmeira(**extra):
    return {"name": "Porto de Palmeira", "city": "Palmeira (Ilha do Sal)", "mrgid": 8362,
            "zone_name": "Cape Verde", "lat": 16.7584756, "lon": -22.9811102, "validated": True,
            "spatial_kind": "in_eez", "extraction_engine": "nightshift",
            "extraction_agreement": True, "source_urls": [DECRETO], "confidence": 66, **extra}


def _level(port, text):
    v3_certify.attach_confidence_level(port, whitelist=["gov.cv"], zone=CAPE_VERDE,
                                       texts=[{"url": DECRETO, "text": text}])
    return port


def test_palmeira_on_its_current_evidence_stays_c1():
    port = _level(_palmeira(), LOCALITY)
    assert port["confidence_level"] == "C1"
    assert port["confidence_cap_reason"] == v3_certify.CAP_NO_ENTRY_CONTEXT


def test_llm_ner_with_an_entry_context_still_reaches_c2():
    assert _level(_palmeira(), "São portos habilitados: Porto de Palmeira, Ilha do Sal.")[
        "confidence_level"] == "C2"


def test_a_tariff_is_no_entry_context_for_llm_ner_either():
    text = "Tarifas portuárias. Port of entry: Porto de Palmeira."
    assert _level(_palmeira(), text)["confidence_level"] == "C1"


def test_a_judge_poe_does_not_need_the_entry_context():
    judge = {"verdict": "poe", "reason": "stub", "quoted_excerpt": "de Palmeira, no Concelho do Sal",
             "excerpt_verified": True}
    assert _level(_palmeira(judge=judge), LOCALITY)["confidence_level"] == "C2"


# --- 3. one public name ---------------------------------------------------------------

def test_is_clearance_list_is_public_and_alone():
    import inspect
    source = inspect.getsource(v3_certify)
    assert source.count("def is_clearance_list") == 1 and "_is_clearance_list" not in source
    assert poe.is_clearance_list is v3_certify.is_clearance_list
