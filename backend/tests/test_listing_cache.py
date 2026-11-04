"""project_listing() is built once per process on the default sources and rebuilt when one
of them changes on disk (it took ~17 s and the pipeline asked for it once per zone).
Offline: tiny fixture sources, stubbed extractor and geocoders, no Mongo."""
import asyncio
import json
import os

import pytest

from app.core import geo
from app.services import listing_ref as lr
from app.services import poe_pipeline as poe

CV_ROW = {"mrgid": 8362, "name": "Cape Verde", "iso2": "CV", "sovereign": "Cape Verde",
          "pol_type": "200NM"}
LC_ROW = {"mrgid": 8416, "name": "Saint Lucia", "iso2": "LC", "sovereign": "Saint Lucia",
          "pol_type": "200NM"}


def _write(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def sources(monkeypatch, tmp_path):
    """Fixture listing / overrides / EEZ index in place of the real ones; counts builds."""
    listing, overrides, index = (tmp_path / "all_countries.json", tmp_path / "slug_overrides.json",
                                 tmp_path / "eez_index.json")
    _write(listing, {"generated_at": "2026-11-03", "countries": [
        {"slug": "cape-verdes", "name": "Cape Verdes",
         "ports_of_entry": ["Praia (Santiago)", "Palmeira (Sal)"], "other_ports": ["Fogo"]},
        {"slug": "st-lucia", "name": "St Lucia",
         "ports_of_entry": ["Rodney Bay", "Castries"], "other_ports": []}]})
    _write(overrides, {"cape-verdes": [8362], "st-lucia": [8416]})
    _write(index, {"zones": [CV_ROW, LC_ROW]})
    monkeypatch.setattr(lr, "LISTING_FILE", listing)
    monkeypatch.setattr(lr, "OVERRIDES_FILE", overrides)
    monkeypatch.setattr(lr, "EEZ_INDEX_FILE", index)
    lr.clear_projection_cache()
    builds = []
    real = lr._project_listing

    def counting(*a, **k):
        builds.append(1)
        return real(*a, **k)

    monkeypatch.setattr(lr, "_project_listing", counting)
    yield {"listing": listing, "builds": builds}
    lr.clear_projection_cache()


def test_second_call_is_served_from_the_cache(sources):
    first = lr.project_listing()
    second = lr.project_listing()
    assert sources["builds"] == [1]
    assert first == second and first is not second
    assert {p["name"] for p in first["ports"]} == {"Praia (Santiago)", "Palmeira (Sal)", "Fogo",
                                                    "Rodney Bay", "Castries"}


def test_callers_cannot_mutate_the_cached_projection(sources):
    proj = lr.project_listing()
    proj["ports"][0]["seed_line"] = "changed"
    proj["ports"].clear()
    again = lr.project_listing()
    assert len(again["ports"]) == 5 and "seed_line" not in again["ports"][0]


def test_a_changed_source_rebuilds_the_projection(sources):
    assert len(lr.project_listing()["ports"]) == 5
    doc = json.loads(sources["listing"].read_text(encoding="utf-8"))
    doc["countries"][1]["ports_of_entry"].append("Vieux Fort")
    _write(sources["listing"], doc)
    st = sources["listing"].stat()
    os.utime(sources["listing"], ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    proj = lr.project_listing()
    assert sources["builds"] == [1, 1]
    assert "Vieux Fort" in {p["name"] for p in proj["ports"]}


def test_explicit_arguments_are_never_cached(sources):
    doc = lr.load_listing_doc()
    lr.project_listing(listing=doc)
    lr.project_listing(listing=doc)
    assert sources["builds"] == [1, 1]


def test_second_zone_of_a_run_does_not_rebuild_it(sources, monkeypatch):
    """Two zones through _extract_and_geocode, as generate_zone_poe runs them: one build."""
    async def fake_extract(context, zone, log, rec=None, catalog_text=None, settings=None):
        return [{"name": "Porto Inglês", "extraction_engine": "llm"}]

    async def empty(*a, **k):
        return []

    async def no_llm(*a, **k):
        return None

    monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
    monkeypatch.setattr(geo, "_nominatim_rows", empty)
    monkeypatch.setattr(geo, "_geonames_rows", empty)
    monkeypatch.setattr(poe, "llm_geocode_port", no_llm)
    monkeypatch.setattr(poe, "_hmem_recall", lambda docs, run_id: [[] for _ in docs])
    monkeypatch.setattr(poe, "_hmem_remember", lambda docs, run_id: {})

    async def zone(z):
        with poe.zone_extract_budget():
            return await poe._extract_and_geocode(dict(z), "ctx", [{"url": "https://x.example/a"}],
                                                  lambda m: None, whitelist=["x.example"])

    asyncio.run(zone(CV_ROW))
    asyncio.run(zone(LC_ROW))
    assert sources["builds"] == [1]
