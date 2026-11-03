"""Several clearance points in one port are one PoE (Clément, 3 Nov 2026), on the rows of
run 20261103-181506-d7963f: the douane list of clearance points, as flat text. Offline."""
import asyncio
import inspect
from pathlib import Path

import pytest

from app.services import poe_pipeline as poe
from app.services import v3_certify

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "douane_liste_points_clearance.txt"
URL = ("https://www.douane.gouv.fr/sites/default/files/2026-01/30/"
       "liste-points-clearance-agrees-mise-a-jour.pdf")
GUIDE = "https://www.martinique.gouv.fr/contenu/telechargement/22986/180253/file/Guide.pdf"
WHITELIST = ["douane.gouv.fr", "martinique.gouv.fr"]
MARTINIQUE = {"name": "Martinique", "geoname": "Martinique", "mrgid": 33178}
GUADELOUPE = {"name": "Guadeloupe", "geoname": "Guadeloupe"}


def _list() -> str:
    raw = FIXTURE.read_text(encoding="utf-8")
    return "".join(ln for ln in raw.splitlines(True) if not ln.startswith("# FIXTURE "))


TEXTS = [{"url": URL, "text": _list()}]


def _port(name, city, level="C1", verdict="not_poe", lat=14.47, lon=-60.87, **extra):
    return {"name": name, "city": city, "confidence_level": level, "mrgid": 33178,
            "judge": {"verdict": verdict, "excerpt": name}, "lat": lat, "lon": lon,
            "source_urls": [URL], "confidence": 60, **extra}


def _d7963f() -> list[dict]:
    """Martinique's records of the run, with the city the reader gave."""
    fdf = (14.6027962, -61.0676724)
    return [
        _port("Station CAP DCML Centre de carénage - Baie des Tourelles", "Fort-de-France",
              lat=fdf[0], lon=fdf[1]),
        _port("Port de plaisance communautaire de l'Étang Z'abricots", "Le Lamentin",
              lat=14.614557, lon=-61.0018145),
        _port("Capitainerie Marina - Pointe Bateau", "Le François", "C2", "poe",
              lat=14.6149952, lon=-60.9028968),
        _port("Port de plaisance Capitainerie du port (nouvelle extension)", "Le Marin",
              "C2", "poe", lat=14.4710778, lon=-60.870212),
        _port("Station-service CAP Ponton N°3", "Le Marin", "C2", "poe",
              lat=14.4710778, lon=-60.870212, confidence=55),
        _port("Régie Port de Plaisance Marina de la pointe du bout", "Les Trois-Îlets",
              "C2", "poe", lat=14.5387672, lon=-61.033545),
        _port("Brigade de surveillance des douanes Cité des douanes Quartier Raisinier",
              "La Trinité", lat=14.7380399, lon=-60.9645707),
        _port("Fort-de-France (Martinique)", "Fort-de-France", lat=14.590977, lon=-61.0042393),
        _port("Le Robert (Martinique)", "Le Robert", lat=14.6859513, lon=-60.9098491),
        _port("Saint-Pierre (Martinique)", "Saint-Pierre", lat=14.7319059, lon=-61.1765908),
        _port("La marina de l'Étang Z'Abricot", "Fort-de-France", lat=fdf[0], lon=fdf[1]),
        _port("La marina du Marin", "Le Marin", "C1", "unclear",
              lat=14.4676941, lon=-60.8662335),
        _port("Fort de France", None, lat=fdf[0], lon=fdf[1]),
    ]


def _grouped(docs=None, texts=TEXTS, zone=MARTINIQUE):
    return poe.group_clearance_points(_d7963f() if docs is None else docs, texts, zone, WHITELIST)


def _by_name(docs):
    return {d["name"]: d for d in docs}


def test_le_marin_is_one_poe_with_its_two_rows():
    poe_marin = _by_name(_grouped()[0])["Le Marin"]
    assert poe_marin["city"] == "Le Marin"
    assert [p["name"] for p in poe_marin["clearance_points"]] == [
        "Port de plaisance Capitainerie du port (nouvelle extension)",
        "Station-service CAP Ponton N°3"]
    assert {p["source"] for p in poe_marin["clearance_points"]} == {URL}
    assert poe_marin["clearance_points"][0]["quote"] == (
        "Port de plaisance Capitainerie du port (nouvelle extension) +596 596 748 383 "
        "contact@marina-martinique.fr lun-sam dim et jours fériés 8h-17h8h-12h30")
    assert poe_marin["clearance_points"][1]["quote"].startswith(
        "Station-service CAP Ponton N°3 + 596 596 765 161 lun-dim et jours fériés "
        "7h30-13h/14h-17h")


def test_the_poe_takes_the_best_level_and_evidence_of_its_points():
    docs = [_port("Station-service CAP Ponton N°3", "Le Marin", "C1", "not_poe"),
            _port("Port de plaisance Capitainerie du port (nouvelle extension)", "Le Marin",
                  "C2", "poe", lat=14.4711)]
    (only,), grouped = _grouped(docs)
    assert only["confidence_level"] == "C2" and only["judge"]["verdict"] == "poe"
    assert only["lat"] == 14.4711
    assert only["judge"]["excerpt"].startswith("Port de plaisance Capitainerie")
    assert len(grouped) == 1 and len(grouped[0][1]) == 2


def test_the_commune_is_the_lists_not_the_records_city():
    # The reader said Le Lamentin; the list has the row under Fort-de-France.
    fdf = _by_name(_grouped()[0])["Fort-de-France"]
    assert [p["name"] for p in fdf["clearance_points"]] == [
        "Station CAP DCML Centre de carénage - Baie des Tourelles",
        "Port de plaisance communautaire de l'Étang Z'abricots"]
    assert fdf["clearance_points"][1]["quote"].endswith("Accueil Permanence 7h-17h 6h-18h")


def test_a_commune_with_one_row_is_a_poe_too():
    docs = _by_name(_grouped()[0])
    assert [p["name"] for p in docs["Le François"]["clearance_points"]] == [
        "Capitainerie Marina - Pointe Bateau"]
    assert docs["Le François"]["confidence_level"] == "C2"


def test_the_name_is_the_list_s_with_the_records_spelling():
    # The list writes "Trois-ilets" and "Trinité"; the records "Les Trois-Îlets", "La Trinité".
    docs = _by_name(_grouped()[0])
    assert docs["Trois-Îlets"]["clearance_points"][0]["name"].startswith("Régie Port")
    assert docs["Trinité"]["confidence_level"] == "C1"
    assert docs["Le Marin"]["dedup_key"].startswith("33178:")


def test_martinique_list_of_d7963f_after_the_same_port_merge():
    docs, _ = _grouped()
    assert len(docs) == 11
    merged, merges = poe.merge_same_ports(docs)
    assert sorted(d["name"] for d in merged) == sorted([
        "Fort-de-France", "Fort-de-France (Martinique)", "Le François", "Le Marin",
        "Trois-Îlets", "Trinité", "Le Robert (Martinique)", "Saint-Pierre (Martinique)",
        "La marina de l'Étang Z'Abricot"])
    names = _by_name(merged)
    assert names["Le Marin"]["merged_names"] == ["La marina du Marin"]
    # "Fort de France" (legal wording, no row) joins by #47, and the list's name is kept.
    assert names["Fort-de-France"]["merged_names"] == ["Fort de France"]
    assert len(names["Fort-de-France"]["clearance_points"]) == 2
    assert {k["name"]: len(r) for k, r in merges} == {
        "Le Marin": 1, "Fort-de-France": 1}


def test_rows_of_two_communes_stay_apart():
    docs, _ = _grouped([_port("Capitainerie Marina - Pointe Bateau", "Le François", "C2", "poe"),
                        _port("Station-service CAP Ponton N°3", "Le Marin", "C2", "poe")])
    assert sorted(d["name"] for d in docs) == ["Le François", "Le Marin"]


def test_rows_of_two_documents_stay_apart_until_the_same_port_merge():
    head = "Liste des points de clearance Martinique Le Marin "
    texts = [{"url": URL, "text": head + "Station-service CAP Ponton N°3 lun-dim 8h-17h"},
             {"url": GUIDE, "text": head + "Capitainerie Marina Ouest lun-dim 8h-17h"}]
    docs, grouped = _grouped([_port("Station-service CAP Ponton N°3", "Le Marin"),
                              _port("Capitainerie Marina Ouest", "Le Marin")], texts)
    assert [d["name"] for d in docs] == ["Le Marin", "Le Marin"] and len(grouped) == 2
    merged, _ = poe.merge_same_ports(docs)
    assert len(merged) == 1


@pytest.mark.parametrize("texts", [
    [{"url": "https://www.marina-martinique.fr/liste.pdf", "text": _list()}],  # not official
    [{"url": URL, "text": _list().replace("Point de clearance", "Point d'accueil")}],
    [],
])
def test_only_an_official_clearance_list_groups(texts):
    docs, grouped = _grouped(texts=texts)
    assert len(docs) == 13 and not grouped
    assert not any(d.get("clearance_points") for d in docs)


def test_a_study_naming_two_marinas_of_a_town_groups_nothing():
    text = ("Étude sur la plaisance. Le Marin. Port de plaisance Capitainerie du port "
            "(nouvelle extension) et Station-service CAP Ponton N°3.")
    assert not _grouped(texts=[{"url": URL, "text": text}])[1]


def test_a_row_of_another_territorys_section_is_not_this_zones():
    row = _port("Capitainerie Marina Bas du Fort", "Pointe-à-Pitre")
    docs, grouped = _grouped([row, _port("Station-service CAP Ponton N°3", "Le Marin")])
    assert not any(d["name"] == "Pointe-à-Pitre" for d in docs)
    assert row in docs and len(grouped) == 1


def test_the_office_name_antilles_guyane_opens_no_section():
    # "Direction Interrégionale des douanes aux Antilles-Guyane" heads Martinique's section.
    rows = v3_certify.clearance_rows(
        [_port("Station-service CAP Ponton N°3", "Le Marin")], TEXTS, MARTINIQUE, WHITELIST)
    assert rows[0]["commune"] == "Le Marin"


def test_another_zone_reads_its_own_section_of_the_list():
    rows = [_port("SAS Sud Ancrage Capitainerie Marina Rivière Sens", "Gourbeyre"),
            _port("Restaurant Barracuda Marina Rivière Sens", "Gourbeyre"),
            _port("Station-service CAP Ponton N°3", "Le Marin")]
    for row in rows:
        row.pop("mrgid")
    docs, _ = _grouped(rows, zone=GUADELOUPE)
    assert sorted(d["name"] for d in docs) == ["Gourbeyre", "Station-service CAP Ponton N°3"]
    assert len(_by_name(docs)["Gourbeyre"]["clearance_points"]) == 2


def test_a_record_named_after_the_commune_is_not_a_row():
    docs, grouped = _grouped([_port("Le Marin", "Le Marin", "C1"),
                              _port("Station-service CAP Ponton N°3", "Le Marin", "C2", "poe")])
    assert sorted(d["name"] for d in docs) == ["Le Marin", "Le Marin"] and len(grouped) == 1
    merged, _ = poe.merge_same_ports(docs)
    assert len(merged) == 1 and merged[0]["confidence_level"] == "C2"
    assert merged[0]["clearance_points"]


def test_a_record_without_a_city_on_the_list_needs_a_heading():
    # No record gives the commune: no heading is known, so nothing is grouped.
    docs, grouped = _grouped([_port("Station-service CAP Ponton N°3", None)])
    assert docs[0]["name"] == "Station-service CAP Ponton N°3" and not grouped


def test_saint_lucia_boarding_stations_are_not_a_clearance_list():
    text = ("at the boarding stations (a) Castries and Vieux-Fort (A) for the boarding and "
            "clearance of vessels (ii) Rodney Bay Marina (A) for the boarding and clearance of "
            "vessels (iii) Marigot Bay Marina (A)")
    docs = [_port("Rodney Bay Marina", "Castries", "C2", "poe"),
            _port("Marigot Bay Marina", "Castries", "C2", "poe")]
    out, grouped = poe.group_clearance_points(
        docs, [{"url": "https://govt.lc/act", "text": text}], {"name": "Saint Lucia"}, ["govt.lc"])
    assert out == docs and not grouped


def test_same_port_still_keeps_two_facilities_apart():
    assert not poe.same_port(_port("Station-service CAP Ponton N°3", "Le Marin"),
                             _port("Capitainerie du port", "Le Marin"), poe.PORT_MERGE_KM)


def test_the_step_never_certifies_the_commune_name(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the commune's name is not certified")

    events = []

    class Rec:
        async def event(self, step, **payload):
            events.append((step, payload))

    monkeypatch.setattr(poe, "attach_confidence_level", refuse)
    monkeypatch.setattr(poe, "certify_texts", lambda raw, sources: TEXTS)
    docs = asyncio.run(poe._group_clearance_points(
        MARTINIQUE, _d7963f(), "", [], WHITELIST, lambda m: None, Rec()))
    assert len(docs) == 9
    steps = [s for s, _ in events]
    assert steps.count("clearance_points") == 5 and steps.count("port_merge") == 2
    marin = next(p for s, p in events if s == "clearance_points" and p["poe"] == "Le Marin")
    assert marin["confidence_level"] == "C2" and marin["source"] == URL
    assert [p["judge"] for p in marin["points"]] == ["poe", "poe"]


def test_rows_are_grouped_after_the_judge_and_the_evidence_and_only_in_a_run():
    src = inspect.getsource(poe.generate_zone_poe)
    assert src.index("_judge_zone_run(") < src.index("_store_evidence(") \
        < src.index("_group_clearance_points(") < src.index("_persist_zone_run(db, zone, docs")
    assert "if run is not None:\n            docs = await _group_clearance_points(" in src
