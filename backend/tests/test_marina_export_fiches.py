"""GET /api/export/marinas/fiches — capped public marina fiches."""
from __future__ import annotations

import os

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "bi_test_marina_fiches")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import marina_world as mw


class _FakeFindCursor:
    def __init__(self, docs, query):
        self._docs = docs
        self._query = query
        self._limit = mw.MARINA_FICHES_MAX_IDS

    def limit(self, n):
        self._limit = n
        return self

    async def to_list(self, length):
        allowed = set((self._query.get("osm_id") or {}).get("$in") or [])
        out = []
        for doc in self._docs:
            if doc.get("osm_id") not in allowed:
                continue
            out.append(doc)
            if len(out) >= min(length, self._limit):
                break
        return out


class _FakeMarinasColl:
    def __init__(self, docs):
        self.docs = list(docs)

    def find(self, query, projection=None):
        return _FakeFindCursor(self.docs, query)


class _FakeDB:
    def __init__(self, docs):
        self.marinas = _FakeMarinasColl(docs)


def test_parse_osm_ids_splits_commas():
    assert mw.parse_marina_fiche_osm_ids("way/1, way/2,,way/3") == ["way/1", "way/2", "way/3"]


def test_marina_export_fiche_merges_slim_and_sourced_enrichment():
    doc = {
        "osm_id": "way/741789648",
        "name": "Port des Minimes",
        "lat": 46.1467598,
        "lon": -1.1660647,
        "source": "openstreetmap",
        "website": "https://portlarochelle.com",
        "website_source": "osm",
        "maps_place_url": "https://www.google.com/maps/place/Minimes",
        "maps_place_source": "google_place",
        "canal_vhf": "9",
        "places_visiteurs": 12,
        "field_sources": {"canal_vhf": "fetch", "places_visiteurs": "nvidia"},
    }
    row = mw.marina_export_fiche(doc)
    assert row["osm_id"] == "way/741789648"
    assert row["feature"]["properties"]["osm_id"] == "way/741789648"
    assert row["enrichment"]["website"]["value"] == "https://portlarochelle.com"
    assert row["enrichment"]["website"]["source"] == "osm"
    assert row["enrichment"]["canal_vhf"] == {"value": "9", "source": "fetch"}
    assert row["enrichment"]["places_visiteurs"]["source"] == "nvidia"


def test_export_route_respects_cap(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import marinas as marinas_router

    app = FastAPI()
    app.include_router(marinas_router.router)
    client = TestClient(app)
    too_many = ",".join(f"way/{i}" for i in range(mw.MARINA_FICHES_MAX_IDS + 1))
    res = client.get("/api/export/marinas/fiches", params={"osm_ids": too_many})
    assert res.status_code == 400


def test_export_route_returns_fiches(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import marinas as marinas_router

    fake = _FakeDB(
        [
            {
                "osm_id": "way/a",
                "name": "Marina A",
                "lat": 46.0,
                "lon": -1.0,
                "source": "openstreetmap",
                "canal_vhf": "12",
                "field_sources": {"canal_vhf": "tags"},
            }
        ]
    )
    monkeypatch.setattr(marinas_router, "db", fake)
    app = FastAPI()
    app.include_router(marinas_router.router)
    client = TestClient(app)
    res = client.get("/api/export/marinas/fiches", params={"osm_ids": "way/a,way/missing"})
    assert res.status_code == 200
    body = res.json()
    assert body["attribution"].startswith("© OpenStreetMap")
    assert body["license"] == "ODbL"
    assert len(body["fiches"]) == 1
    assert body["fiches"][0]["enrichment"]["canal_vhf"]["source"] == "tags"


def test_export_route_requires_osm_ids():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import marinas as marinas_router

    app = FastAPI()
    app.include_router(marinas_router.router)
    client = TestClient(app)
    res = client.get("/api/export/marinas/fiches")
    assert res.status_code == 400
