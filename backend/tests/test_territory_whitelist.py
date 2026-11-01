"""Per-territory PoE whitelist (docs/TERRITORY-WHITELIST.md).

A polygon curated in territories.json gets who manages and publishes its ports of entry
(official_domains + poe_exceptions.json whitelist_mrgid[mrgid]); the sovereign's national
labels only when the record sets sovereign_runs_entry. Unmapped zones are unchanged.
Offline: repo JSON files only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.agents import registry_check  # noqa: E402
from app.services import poe_pipeline as poe  # noqa: E402
from app.services import territory_ref as tr  # noqa: E402
from app.services.poe_pipeline import build_whitelist, load_exceptions, url_allowed  # noqa: E402

SELF_GOVERNING = {"SX": 21803, "AW": 26519, "CW": 26517, "CK": 8446, "NU": 8447,
                  "GL": 8438, "FO": 8435}


@pytest.fixture(scope="module")
def exc():
    return load_exceptions()


def _curated(code: str) -> set[str]:
    return set(tr._territories()[code]["official_domains"])


def test_table_is_valid_and_generic():
    assert tr.validate_territories() == []
    sovereigns = {r["sovereign_iso2"] for r in tr._territories().values()}
    assert {"FR", "ES"} <= sovereigns
    assert tr.MRGID_TO_TERRITORY[8364] == "islas_canarias"
    assert tr.MRGID_TO_TERRITORY[48944] == "mayotte"
    # The sovereign's labels are opt-in: every true flag carries its evidence.
    for rec in tr._territories().values():
        assert isinstance(rec.get("sovereign_runs_entry"), bool), rec["code"]
        if rec["sovereign_runs_entry"]:
            assert rec["sovereign_runs_entry_evidence"].strip()


def test_mayotte_gets_its_curated_domains(exc):
    wl = build_whitelist("YT", "FR", exc, mrgid=48944)
    assert _curated("mayotte") <= set(wl)
    assert set(exc["whitelist_mrgid"]["48944"]) <= set(wl)
    assert url_allowed("https://www.mayotte.gouv.fr/Actions-de-l-Etat/Mer", wl)
    assert url_allowed("https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000000000000", wl)
    # DROM: France's national services run entry, so France's national label is added.
    assert tr.sovereign_runs_entry(tr.territory_for_mrgid(48944))
    assert "gouv.fr" in wl


def test_new_caledonia_gets_no_national_gouv_fr_labels(exc):
    for mrgid in (8312, 48948):  # New Caledonia and Matthew and Hunter
        wl = build_whitelist("NC", "FR", exc, mrgid=mrgid)
        allowed = _curated("nouvelle_caledonie") | set(exc.get("whitelist_mrgid", {}).get(str(mrgid), []))
        assert set(wl) <= allowed, sorted(set(wl) - allowed)
        assert "gouv.fr" not in wl and "douane.gouv.fr" not in wl
        gouv_fr = {d for d in wl if d == "gouv.fr" or d.endswith(".gouv.fr")}
        assert gouv_fr <= _curated("nouvelle_caledonie")
        assert url_allowed("https://douane.gouv.nc/particuliers/plaisance", wl)
        assert not url_allowed("https://www.douane.gouv.fr/particuliers/vous-naviguez", wl)
        assert not url_allowed("https://www.legifrance.gouv.fr/", wl)
        assert not url_allowed("https://www.mer.gouv.fr/", wl)


def test_mainland_france_no_longer_gets_taaf(exc):
    assert "taaf.fr" not in exc["manual"]["FR"]
    url = "https://taaf.fr/acceder-aux-territoires/demandes-dactivites-et-dacces/"
    for mrgid in (5677, 48966, 48976):
        wl = build_whitelist("FR", "FR", exc, mrgid=mrgid)
        assert "taaf.fr" not in wl and not url_allowed(url, wl)
        assert "gouv.fr" in wl and "douane.gouv.fr" in wl
    # Clipperton (unmapped, no ISO2) uses France's list: no taaf.fr there either.
    assert not url_allowed(url, build_whitelist(None, "FR", exc, mrgid=8401))
    # Tromelin and Europa get it from the TAAF record, with the DM sud océan Indien.
    for iso2, mrgid in ((None, 48946), ("TF", 8341)):
        wl = build_whitelist(iso2, "FR", exc, mrgid=mrgid)
        assert url_allowed(url, wl)
        assert "dm.sud-ocean-indien.developpement-durable.gouv.fr" in wl
        assert "gouv.fr" not in wl


@pytest.mark.parametrize("iso2,sov2,mrgid", [
    ("LC", "LC", 8416), ("CV", "CV", 8362), ("SX", "NL", 21803), ("NU", "NZ", 8447),
    ("ES", "ES", 5693), ("MX", "MX", 8429), (None, "FR", 8401), (None, "ES", 49002),
    ("BQ", "NL", 26518),
])
def test_unmapped_zone_is_unchanged(iso2, sov2, mrgid, exc):
    assert not tr.territory_for_mrgid(mrgid)
    with_zone = build_whitelist(iso2, sov2, exc, mrgid=mrgid)
    assert with_zone == build_whitelist(iso2, sov2, exc)
    # Same formula as before this change: the national labels of `iso2 or sov_iso2`.
    assert with_zone == sorted(poe._national_labels(iso2 or sov2, exc, poe.eez_iso2_set()))
    assert poe.registry_whitelist({"mrgid": mrgid, "iso2": iso2, "sov_iso2": sov2}) is None


def test_canaries_resolve_to_spain(exc):
    terr = tr.territory_for_mrgid(8364)
    assert terr["code"] == "islas_canarias" and terr["sovereign_iso2"] == "ES"
    wl = build_whitelist(None, "ES", exc, mrgid=8364)
    assert {"gob.es", "agenciatributaria.es", "policia.es"} <= set(wl)  # Spain runs entry
    assert {"palmasport.es", "puertosdetenerife.org", "puertoscanarios.es"} <= set(wl)
    assert url_allowed("https://sede.agenciatributaria.gob.es/x", wl)
    # The Canary port authorities no longer leak onto mainland Spain.
    mainland = build_whitelist("ES", "ES", exc, mrgid=5693)
    assert not {"palmasport.es", "puertosdetenerife.org", "puertoscanarios.es"} & set(mainland)
    assert "gob.es" in mainland


@pytest.mark.parametrize("iso2", sorted(SELF_GOVERNING))
def test_self_governing_never_attached_to_a_sovereign(iso2):
    assert iso2 in tr.never_attach_to_sovereign()
    assert all((r.get("iso2") or "").upper() != iso2 for r in tr._territories().values())
    assert SELF_GOVERNING[iso2] not in tr.MRGID_TO_TERRITORY
    # A record for one of them would be dropped and never get the sovereign's labels.
    assert not tr.sovereign_runs_entry({"iso2": iso2, "sovereign_runs_entry": True})


def test_self_governing_keep_their_own_lists(exc):
    sx = build_whitelist("SX", "NL", exc, mrgid=21803)
    assert "sintmaartengov.org" in sx and "government.nl" not in sx
    nu = build_whitelist("NU", "NZ", exc, mrgid=8447)
    assert "govt.nz" not in nu and "gov.nu" in nu


def test_zone_whitelist_reads_the_mrgid(exc):
    zone = {"mrgid": 8312, "iso2": "NC", "sov_iso2": "FR"}
    assert poe.zone_whitelist(zone, exc) == build_whitelist("NC", "FR", exc, mrgid=8312)
    assert poe.registry_whitelist(zone) == poe.zone_whitelist(zone)


def test_learned_domains_stay_on_the_polygon():
    assert poe.learned_domain_bucket({"mrgid": 8312, "iso2": "NC"}) == ("auto_mrgid", "8312")
    assert poe.learned_domain_bucket({"mrgid": 21803, "iso2": "SX"}) == ("auto", "SX")


def test_auto_mrgid_bucket_is_read_and_merged():
    exc = {"manual": {}, "auto": {}, "auto_mrgid": {"8312": ["learned.nc"]}}
    assert "learned.nc" in build_whitelist("NC", "FR", exc, mrgid=8312)
    assert "learned.nc" not in build_whitelist("NC", "FR", exc)
    merged = poe.merge_exceptions({"auto_mrgid": {"8312": ["a.nc"]}}, {"auto_mrgid": {"8312": ["b.nc"]}})
    assert merged["auto_mrgid"]["8312"] == ["a.nc", "b.nc"]


def test_registry_check_official_evidence_is_scoped_to_the_polygon(exc):
    text = "Port de Nouméa — port d'entrée désigné pour les navires de plaisance."
    url = "https://www.douane.gouv.fr/fiche/noumea"
    port = {"name": "Nouméa"}
    # Naming heuristic (unmapped zones, run_pipeline): any gouv.fr counts.
    assert registry_check.official_source_evidence(port, {url: text})
    nc = build_whitelist("NC", "FR", exc, mrgid=8312)
    assert registry_check.official_source_evidence(port, {url: text}, whitelist=nc) == []
    local = "https://douane.gouv.nc/particuliers/plaisance"
    assert registry_check.official_source_evidence(port, {local: text}, whitelist=nc)


def test_french_mapping_agrees_with_zee_crossings():
    pytest.importorskip("shapely")
    from app.services.zee_crossings import MRGID_TO_TERRITORY as ZC
    french = {m: c for m, c in tr.MRGID_TO_TERRITORY.items()
              if tr._territories()[c]["sovereign_iso2"] == "FR"}
    assert french == {m: c for m, c in ZC.items() if c is not None}
