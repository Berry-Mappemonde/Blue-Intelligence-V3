"""Cyclone crossings — the bounding-box prefilter must be invisible in results
and visible in CPU time (incident 2026-09-21: ~1 s of Python per request,
inside the event loop, 600-800 req/s → prod down)."""
from __future__ import annotations

import inspect
import random
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.routers import climatology as router_mod  # noqa: E402
from app.services import climatology_cyclones as cyc  # noqa: E402

HAS_IBTRACS = cyc.has_snapshot()
needs_ibtracs = pytest.mark.skipif(not HAS_IBTRACS, reason="ibtracs_since1980.json absent")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router_mod.router)
    return TestClient(app)


# ── Prefilter geometry (synthetic, no dataset needed) ─────────────────────────

def test_bbox_near_segment_across_the_antimeridian():
    # Storm unwrapped past 180 (170 → 190), leg west of the line at −175.
    bb = (-20.0, -10.0, 170.0, 190.0)
    assert cyc.bbox_near_segment(bb, -15.0, -175.0, -15.0, -178.0, 120.0) is True
    # Same storm, leg far away in the Atlantic.
    assert cyc.bbox_near_segment(bb, -15.0, -30.0, -15.0, -35.0, 120.0) is False


def test_bbox_near_segment_leg_crossing_the_antimeridian():
    # Leg 175 → −175 goes the short way round; storm at 179.
    bb = (10.0, 12.0, 178.0, 180.0)
    assert cyc.bbox_near_segment(bb, 11.0, 175.0, 11.0, -175.0, 120.0) is True
    # A storm 20° away in longitude at the same latitude: rejected.
    bb_far = (10.0, 12.0, 150.0, 155.0)
    assert cyc.bbox_near_segment(bb_far, 11.0, 175.0, 11.0, -175.0, 120.0) is False


def test_bbox_padding_grows_with_latitude():
    # At 70°N, 120 nm is ~5.8° of longitude: the box must still be accepted.
    bb = (69.0, 71.0, 10.0, 12.0)
    assert cyc.bbox_near_segment(bb, 70.0, 17.0, 70.0, 17.5, 120.0) is True
    # Latitude: 120 nm = 2°; 5° away is certainly out.
    assert cyc.bbox_near_segment(bb, 76.5, 11.0, 76.5, 11.5, 120.0) is False


def test_storm_bbox_memoised_and_empty_track_skipped():
    storm = {"coords": [[172.5, -12.5], [172.4, -11.9], [-179.0, -11.0]]}
    bb = cyc._storm_bbox(storm)
    assert bb == (-12.5, -11.0, 172.4, 181.0)
    assert storm["_bbox"] is bb
    assert cyc._storm_bbox({"coords": []}) is None


# ── Equivalence with the brute force on the real dataset ─────────────────────

LEGS = [
    # (lat1, lon1, lat2, lon2, month) — recettes + edge cases
    (14.6, -61.0, 38.7, -27.2, 9),      # Martinique → Açores, septembre
    (14.6, -61.0, 38.7, -27.2, 3),      # même route, mars (0 attendu)
    (30.1, -60.5, 4.9333, -52.3533, 9), # Bermudes → Cayenne
    (20.1, -110.2, 15.0, -150.0, 9),    # Pacifique Est
    (-15.0, 175.0, -15.0, -170.0, 1),   # antiméridien, Pacifique Sud
    (35.0, 139.0, 13.0, 145.0, 8),      # Japon → Guam
    (-20.0, 55.0, -12.0, 45.0, 2),      # Réunion → Madagascar
    (46.15, -1.2, 41.9, 8.7, 9),        # La Rochelle → Ajaccio (0 attendu)
    (60.0, -20.0, 64.0, -22.0, 9),      # hautes latitudes
]


@needs_ibtracs
@pytest.mark.parametrize("leg", LEGS)
def test_prefilter_gives_the_same_crossings_as_brute_force(leg):
    lat1, lon1, lat2, lon2, month = leg
    fast = cyc.crossings(lat1, lon1, lat2, lon2, month)
    slow = cyc.crossings(lat1, lon1, lat2, lon2, month, prefilter=False)
    assert fast["count"] == slow["count"]
    assert [s["sid"] for s in fast["storms"]] == [s["sid"] for s in slow["storms"]]


@needs_ibtracs
def test_prefilter_matches_brute_force_on_random_legs():
    rng = random.Random(20260921)
    for _ in range(40):
        lat1 = rng.uniform(-45, 45)
        lon1 = rng.uniform(-180, 180)
        lat2 = max(-60.0, min(60.0, lat1 + rng.uniform(-15, 15)))
        lon2 = cyc.wrap_lon(lon1 + rng.uniform(-40, 40))
        month = rng.randint(1, 12)
        fast = cyc.crossings(lat1, lon1, lat2, lon2, month)
        slow = cyc.crossings(lat1, lon1, lat2, lon2, month, prefilter=False)
        assert [s["sid"] for s in fast["storms"]] == [s["sid"] for s in slow["storms"]], (lat1, lon1, lat2, lon2, month)


@needs_ibtracs
def test_nearby_count_matches_brute_force():
    rng = random.Random(7)
    for _ in range(60):
        lat = rng.uniform(-40, 40)
        lon = rng.uniform(-180, 180)
        month = rng.randint(1, 12)
        assert cyc.nearby_count(lat, lon, month) == cyc.nearby_count(lat, lon, month, prefilter=False), (lat, lon, month)


PREFILTER_LEG = (30.1, -60.5, 4.9333, -52.3533, 9)  # Bermuda area -> Cayenne, September


def _distance_calls(monkeypatch, prefilter: bool) -> tuple[int, dict]:
    """Distance computations (point_to_segment_nm) one uncached crossings() run makes.

    A count, not a wall-clock time: it does not depend on machine load, so the
    test cannot flake under the full suite (it used to time both runs)."""
    calls = {"n": 0}
    real = cyc.point_to_segment_nm

    def counting(*args):
        calls["n"] += 1
        return real(*args)

    monkeypatch.setattr(cyc, "point_to_segment_nm", counting)
    lat1, lon1, lat2, lon2, month = PREFILTER_LEG
    window = int(cyc.rule("climatology.cyclone_dayrange", 21))
    radius = float(cyc.rule("climatology.cyclone_radius_nm", 120))
    out = cyc._crossings_compute(lat1, lon1, lat2, lon2, month, None, window, radius, prefilter)
    monkeypatch.setattr(cyc, "point_to_segment_nm", real)
    return calls["n"], out


@needs_ibtracs
def test_prefilter_is_much_faster_than_brute_force(monkeypatch):
    """The prefilter skips most of the distance work (incident 2026-09-21: ~1 s per call)."""
    fast, fast_out = _distance_calls(monkeypatch, prefilter=True)
    slow, slow_out = _distance_calls(monkeypatch, prefilter=False)
    assert fast_out["count"] == slow_out["count"] > 0
    assert [s["sid"] for s in fast_out["storms"]] == [s["sid"] for s in slow_out["storms"]]
    assert fast * 5 < slow, f"prefilter {fast} distance computations vs brute force {slow}"


@needs_ibtracs
def test_distance_count_measures_the_prefilter(monkeypatch):
    """With the bbox test disabled the prefilter path does the brute-force work, so the
    count above really measures the prefilter."""
    slow, _ = _distance_calls(monkeypatch, prefilter=False)
    monkeypatch.setattr(cyc, "bbox_near_segment", lambda *a, **k: True)
    disabled, _ = _distance_calls(monkeypatch, prefilter=True)
    assert disabled == slow


@needs_ibtracs
def test_lru_returns_independent_copies():
    a = cyc.crossings(14.6, -61.0, 38.7, -27.2, 9)
    a["storms"].clear()
    a["count"] = -1
    b = cyc.crossings(14.6, -61.0, 38.7, -27.2, 9)
    assert b["count"] > 0 and b["storms"]


# ── HTTP contract: cacheable, off the event loop ──────────────────────────────

def test_point_and_crossings_handlers_run_in_the_threadpool():
    for fn in (
        router_mod.climatology_point,
        router_mod.climatology_crossings,
        router_mod.climatology_wind_geojson,
        router_mod.climatology_cyclones_geojson,
    ):
        assert not inspect.iscoroutinefunction(fn), fn.__name__


@needs_ibtracs
def test_climatology_responses_are_cacheable_for_a_month():
    c = _client()
    point = c.get("/api/climatology/point", params={"lat": 46.15, "lon": -1.2, "month": 9})
    cross = c.get("/api/climatology/crossings", params={
        "lat1": 14.6, "lon1": -61.0, "lat2": 38.7, "lon2": -27.2, "month": 9,
    })
    assert point.status_code == 200 and cross.status_code == 200
    assert point.headers["cache-control"] == "public, max-age=2592000"
    assert cross.headers["cache-control"] == "public, max-age=2592000"
    layer = c.get("/api/climatology/cyclones.geojson", params={"month": 9})
    assert layer.headers["cache-control"] == "public, max-age=86400"
    bad = c.get("/api/climatology/point", params={"lat": 95, "lon": 0, "month": 9})
    assert bad.status_code == 400
