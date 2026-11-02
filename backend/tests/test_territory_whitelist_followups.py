"""Follow-ups to the per-territory whitelist (docs/TERRITORY-WHITELIST.md).

1. The Review filter (territory_ref.td_url_fits_polygon) uses the pipeline's rule:
   a curated polygon that is not its sovereign accepts its build_whitelist (which
   reads sovereign_runs_entry) or its local ccTLD; other zones are not filtered.
2. run_pipeline.check_registries scopes official evidence to the polygon's whitelist
   for a curated mrgid; unmapped zones keep the naming heuristic.
Offline: repo JSON files only, no Mongo.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.agents import registry_check  # noqa: E402
from app.pipeline import run_pipeline  # noqa: E402
from app.services.poe_pipeline import build_whitelist  # noqa: E402
from app.services.territory_ref import td_url_fits_polygon  # noqa: E402

MAYOTTE = {"mrgid": 48944, "iso2": "YT", "sov_iso2": "FR"}
MARTINIQUE = {"mrgid": 33178, "iso2": "MQ", "sov_iso2": "FR"}
NEW_CALEDONIA = {"mrgid": 8312, "iso2": "NC", "sov_iso2": "FR"}
ST_BARTH = {"mrgid": 48952, "iso2": "BL", "sov_iso2": "FR"}
TROMELIN = {"mrgid": 48946, "iso2": None, "sov_iso2": "FR"}


# --- 1. Review filter = pipeline rule -------------------------------------------------
def test_review_accepts_national_labels_when_the_sovereign_runs_entry():
    for zone in (MAYOTTE, MARTINIQUE):  # DROMs: sovereign_runs_entry = true
        assert td_url_fits_polygon("https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000047858148", zone)
        assert td_url_fits_polygon("https://www.mer.gouv.fr/acteurs-reseau", zone)
        assert not td_url_fits_polygon("https://www.france.fr/en/", zone)
        assert not td_url_fits_polygon("https://dictionary.cambridge.org/x", zone)
    assert td_url_fits_polygon("https://martinique.port.fr/escales", MARTINIQUE)  # whitelist_mrgid
    assert td_url_fits_polygon("https://www.example.yt/plaisance", MAYOTTE)  # local ccTLD


def test_review_keeps_national_labels_out_when_the_sovereign_does_not_run_entry():
    for zone in (NEW_CALEDONIA, ST_BARTH):  # sovereign_runs_entry = false
        assert not td_url_fits_polygon("https://www.legifrance.gouv.fr/jorf/id/X", zone)
        assert not td_url_fits_polygon("https://www.mer.gouv.fr/acteurs-reseau", zone)
        assert not td_url_fits_polygon("https://www.education.gouv.fr/", zone)
    assert td_url_fits_polygon("https://douane.gouv.nc/particuliers/plaisance", NEW_CALEDONIA)
    # Curated + per-mrgid bucket: portdegustavia.fr was dropped by the old curated-only rule.
    assert td_url_fits_polygon("https://portdegustavia.fr/port-plaisance-gustavia", ST_BARTH)
    assert td_url_fits_polygon("https://www.douane.gouv.fr/fiche/x", ST_BARTH)


@pytest.mark.parametrize("zone", [MAYOTTE, MARTINIQUE, NEW_CALEDONIA, ST_BARTH])
def test_review_accepts_every_pipeline_whitelist_domain(zone):
    wl = build_whitelist(zone["iso2"], zone["sov_iso2"], None, mrgid=zone["mrgid"])
    assert wl
    for dom in wl:
        assert td_url_fits_polygon(f"https://{dom}/page", zone), dom
        assert not td_url_fits_polygon(f"https://{dom}.example.com/page", zone), dom


@pytest.mark.parametrize("zone", [
    {"mrgid": 26518, "iso2": "BQ", "sov_iso2": "NL"},   # Saba: unmapped
    {"mrgid": 21803, "iso2": "SX", "sov_iso2": "NL"},   # Sint Maarten: never attached
    {"mrgid": 8401, "iso2": None, "sov_iso2": "FR"},    # Clipperton: unmapped
    {"mrgid": 5677, "iso2": "FR", "sov_iso2": "FR"},    # the sovereign's own polygon
    {"mrgid": 8364, "iso2": None, "sov_iso2": "ES"},    # Canaries: no polygon ISO2
    TROMELIN,                                           # no polygon ISO2
])
def test_review_does_not_filter_unmapped_or_sovereign_zones(zone):
    for url in ("https://www.legifrance.gouv.fr/x", "https://www.france.fr/en/",
                "https://www.government.nl/x"):
        assert td_url_fits_polygon(url, zone)


# --- 2. run_pipeline.check_registries -----------------------------------------------
def test_zone_registry_whitelist_mapped_and_unmapped():
    assert run_pipeline.zone_registry_whitelist(8312) == build_whitelist(
        "NC", "FR", None, mrgid=8312)
    assert "douane.gouv.fr" not in run_pipeline.zone_registry_whitelist(8312)
    assert "gouv.fr" in run_pipeline.zone_registry_whitelist(48944)
    for mrgid in (8402, 26518, 21803, None, "x"):  # Bermuda, Saba, Sint Maarten, none
        assert run_pipeline.zone_registry_whitelist(mrgid) is None


def _capture_check_ports(monkeypatch):
    seen = {}

    def fake_check_ports(ports, zone, run_id, live_failed=False, source_texts=None,
                         stored_records=None, *, whitelist=None):
        seen.update(zone=zone, whitelist=whitelist, stored=stored_records)
        return ports, {}

    monkeypatch.setattr(registry_check, "check_ports", fake_check_ports)
    monkeypatch.setattr(run_pipeline.evidence_store, "lookup", lambda *a, **k: [])
    return seen


def test_check_registries_passes_the_whitelist_for_a_mapped_mrgid(monkeypatch):
    seen = _capture_check_ports(monkeypatch)
    run_pipeline.check_registries([{"name": "Nouméa", "mrgid": 8312}], "New Caledonia", "fu-nc")
    assert seen["zone"] == {"name": "New Caledonia", "mrgid": 8312}
    assert seen["whitelist"] == build_whitelist("NC", "FR", None, mrgid=8312)


def test_check_registries_keeps_the_heuristic_for_an_unmapped_zone(monkeypatch):
    seen = _capture_check_ports(monkeypatch)
    run_pipeline.check_registries([{"name": "Port of St. George's", "mrgid": 8402}], "Bermuda", "fu-bm")
    assert seen["whitelist"] is None
    run_pipeline.check_registries([{"name": "Somewhere"}], "Nowhere", "fu-none")
    assert seen["whitelist"] is None


def _record(url: str, name: str) -> dict:
    return {"url": url, "port_key": registry_check.port_key(name),
            "snippet": f"{name} est le port d'entrée désigné pour les navires de plaisance."}


def test_stored_evidence_is_scoped_for_mapped_and_heuristic_for_unmapped():
    port = {"name": "Nouméa"}
    national = _record("https://www.douane.gouv.fr/fiche/noumea", "Nouméa")
    local = _record("https://douane.gouv.nc/particuliers/plaisance", "Nouméa")
    assert registry_check.stored_snippet_evidence(port, [national])  # heuristic: any gouv.fr
    nc = run_pipeline.zone_registry_whitelist(8312)
    assert registry_check.stored_snippet_evidence(port, [national], whitelist=nc) == []
    assert registry_check.stored_snippet_evidence(port, [local], whitelist=nc)
    # Unmapped: whitelist None, so the naming heuristic is unchanged.
    bm = _record("https://www.gov.bm/ports", "Port of St. George's")
    assert registry_check.stored_snippet_evidence(
        {"name": "Port of St. George's"}, [bm],
        whitelist=run_pipeline.zone_registry_whitelist(8402))
