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


def test_contract_covers_every_harness_route():
    assert set(CONTRACT["routes"]) == {rid for rid, _, _ in harness.ROUTES}
    assert CONTRACT["recorded_from"].startswith("feat/integrate-poe-pipeline@")
    paths = {v["path"] for v in CONTRACT["routes"].values()}
    for required in ("/api/poe/ports", "/api/poe/zones/geojson", "/api/marinas",
                     "/api/capitaineries", "/api/amp"):
        assert required in paths
    assert any(p.startswith("/api/export/") and p.endswith(".geojson") for p in paths)
    assert any(p.startswith("/api/climatology/") for p in paths)


@pytest.mark.parametrize("route_id", ROUTE_IDS)
def test_route_keeps_its_recorded_shape(route_id, current):
    recorded = CONTRACT["routes"][route_id]
    got = current[route_id]
    assert got["path"] == recorded["path"], "route path changed"
    assert got["status"] == 200, got.get("error")
    errs = harness.breaking_changes(recorded["shape"], got["shape"])
    assert not errs, f"{recorded['path']} breaks the NAVIGUIDE contract: {errs}"


@pytest.mark.parametrize("route_id", [r for r in ROUTE_IDS
                                      if "features" in CONTRACT["routes"][r]["shape"]])
def test_geojson_routes_return_a_feature_collection(route_id, current):
    shape = current[route_id]["shape"]
    assert shape["top"].get("features") == "array"
    assert shape["top"].get("type") == "string"
    assert shape["features"]["count_nonzero"], "seeded data must produce features"


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
