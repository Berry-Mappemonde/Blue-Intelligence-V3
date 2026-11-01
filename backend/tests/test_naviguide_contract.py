"""NAVIGUIDE contract: the routes naviguide.fr consumes keep their recorded shape.

tests/contracts/naviguide.json was recorded on feat/integrate-poe-pipeline (production)
with tests/contracts/naviguide_harness.py. Each route is called here on the current code,
with the same seeded offline data (mongomock, outbound HTTP blocked, remote fetches
stubbed). A recorded top-level key, nested key or feature property must still be there
with the same JSON type; new keys are allowed. See docs/NAVIGUIDE-CONTRACT.md.
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests" / "contracts"))

import naviguide_harness as harness  # noqa: E402

CONTRACT = json.loads(harness.CONTRACT_FILE.read_text(encoding="utf-8"))
ROUTE_IDS = sorted(CONTRACT["routes"])


@pytest.fixture(scope="module")
def current():
    return harness.call_all()


# NAVIGUIDE's calls, from its code: (path, query parameter names it sends), and whether
# it sends X-Admin-Key. Each must be recorded in the contract.
NAVIGUIDE_CALLS = [
    ("/api/climatology/point", {"lat", "lon", "month"}),
    ("/api/climatology/point", {"lat", "lon", "month", "dest_lat", "dest_lon"}),
    ("/api/climatology/point", {"lat", "lon", "month", "day"}),
    ("/api/climatology/point", {"lat", "lon", "month", "day", "dest_lat", "dest_lon"}),
    ("/api/climatology/crossings", {"lat1", "lon1", "lat2", "lon2", "month"}),
    ("/api/climatology/crossings", {"lat1", "lon1", "lat2", "lon2", "month", "day"}),
    *[(f"/api/climatology/{k}.geojson", q) for k in ("wind", "current", "cyclones")
      for q in ({"month"}, {"month", "spacing_deg"})],
    ("/api/climatology/wave.geojson", {"month", "stat"}),
    ("/api/climatology/wave.geojson", {"month", "stat", "spacing_deg"}),
    ("/api/climatology/wind/tiles/2/1/1.json", {"month"}),
    ("/api/climatology/current/tiles/2/1/1.json", {"month"}),
    ("/api/climatology/wave/tiles/2/1/1.json", {"month", "stat"}),
    ("/api/climatology/meta", set()),
    ("/api/amp", {"bbox"}), ("/api/amp/stats", set()), ("/api/amp/nogo", set()),
    ("/api/export/amp.geojson", set()),
    ("/api/poe/ports", {"mrgid"}), ("/api/poe/zones/geojson", set()),
    ("/api/export/poe.geojson", set()),
    ("/api/export/geojson", set()), ("/api/export/science.geojson", set()),
    ("/api/export/marinas.geojson", set()), ("/api/export/capitaineries.geojson", set()),
    ("/api/marinas", set()), ("/api/capitaineries", set()), ("/api/anchorages", set()),
    ("/api/depth", {"lat", "lon"}),
    ("/api/noaa/aids", {"bbox"}),
    ("/api/review/fiche", {"kind", "id"}),
]
WAVE_STATS = {"p50", "p90", "mean"}


def test_contract_covers_every_harness_route():
    assert set(CONTRACT["routes"]) == {rid for rid, *_ in harness.ROUTES}
    assert CONTRACT["recorded_from"].startswith("feat/integrate-poe-pipeline@")


@pytest.mark.parametrize("path, params", NAVIGUIDE_CALLS,
                         ids=[f"{p}?{'&'.join(sorted(q))}" for p, q in NAVIGUIDE_CALLS])
def test_every_naviguide_call_is_in_the_contract(path, params):
    hits = [v for v in CONTRACT["routes"].values()
            if v["path"] == path and set(v["query"]) == params]
    assert hits, f"{path} with {sorted(params)} is not recorded"


def test_wave_stats_and_review_admin_variants_are_recorded():
    routes = CONTRACT["routes"].values()
    for path in ("/api/climatology/wave.geojson", "/api/climatology/wave/tiles/2/1/1.json"):
        assert {v["query"]["stat"] for v in routes if v["path"] == path} == WAVE_STATS
    for kind in ("eez", "amp"):
        fiche = [v for v in routes if v["path"] == "/api/review/fiche"
                 and v["query"]["kind"] == kind]
        assert {(v["admin_key"], v["status"]) for v in fiche} == {(True, 200), (False, 401)}


@pytest.mark.parametrize("route_id", ROUTE_IDS)
def test_route_keeps_its_recorded_shape(route_id, current):
    recorded = CONTRACT["routes"][route_id]
    got = current[route_id]
    assert got["path"] == recorded["path"], "route path changed"
    assert got["status"] == recorded["status"], got.get("error") or got["shape"]
    errs = harness.breaking_changes(recorded["shape"], got["shape"])
    assert not errs, f"{recorded['path']} breaks the NAVIGUIDE contract: {errs}"


@pytest.mark.parametrize("route_id", [r for r in ROUTE_IDS
                                      if "features" in CONTRACT["routes"][r]["shape"]])
def test_geojson_routes_return_a_feature_collection(route_id, current):
    shape = current[route_id]["shape"]
    assert shape["top"].get("features") == "array"
    assert shape["top"].get("type") == "string"
    if CONTRACT["routes"][route_id]["shape"]["features"]["count_nonzero"]:
        assert shape["features"]["count_nonzero"], "seeded data must produce features"


def test_review_fiche_needs_the_admin_key_like_production(current):
    for kind in ("eez", "amp"):
        assert current[f"review_fiche_{kind}"]["status"] == 200
        assert current[f"review_fiche_{kind}_no_key"]["status"] == 401
        assert current[f"review_fiche_{kind}_no_key"]["shape"]["top"] == {"detail": "string"}


# --- the comparator itself ---------------------------------------------------------

BASE = {
    "top": {"type": "string", "features": "array", "metadata": "object"},
    "nested": {"metadata": {"version": "string"}},
    "features": {"count_nonzero": True, "geometry_types": ["Point"],
                 "properties": {"name": ["string"], "lat": ["number"], "note": ["null", "string"]}},
}


def _with(**changes):
    shape = deepcopy(BASE)
    for path, value in changes.items():
        node = shape
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        if value is None:
            node.pop(leaf)
        else:
            node[leaf] = value
    return shape


def test_new_fields_are_allowed():
    cur = deepcopy(BASE)
    cur["top"]["attribution"] = "string"
    cur["nested"]["metadata"]["license"] = "string"
    cur["features"]["properties"]["confidence_level"] = ["string"]
    assert harness.breaking_changes(BASE, cur) == []


@pytest.mark.parametrize("changes, expected", [
    ({"top__metadata": None}, "top-level key removed: metadata"),
    ({"top__type": "object"}, "top-level type"),
    ({"nested__metadata__version": None}, "metadata.version removed"),
    ({"features__properties__name": None}, "feature property removed: name"),
    ({"features__properties__lat": ["string"]}, "feature property lat"),
    ({"features__properties__name": ["null", "string"]}, "feature property name"),
    ({"features__geometry_types": ["Point", "Polygon"]}, "geometry types changed"),
    ({"features__count_nonzero": False}, "features list is empty"),
    ({"features": None}, "features list removed"),
])
def test_breaking_changes_are_detected(changes, expected):
    errs = harness.breaking_changes(BASE, _with(**changes))
    assert errs and any(expected in e for e in errs), errs


def test_a_rename_is_a_removal():
    cur = deepcopy(BASE)
    cur["features"]["properties"]["title"] = cur["features"]["properties"].pop("name")
    assert harness.breaking_changes(BASE, cur) == ["feature property removed: name"]


def test_harness_is_offline():
    import httpx
    with harness.offline_app():
        with pytest.raises(RuntimeError, match="offline"):
            httpx.get("https://naviguide.fr/")
