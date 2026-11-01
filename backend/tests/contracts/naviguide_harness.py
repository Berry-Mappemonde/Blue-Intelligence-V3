"""NAVIGUIDE contract harness: call the routes NAVIGUIDE (naviguide.fr) consumes on a
seeded in-memory Mongo and reduce each response to its shape.

Used by tests/test_naviguide_contract.py (checks the current code against
tests/contracts/naviguide.json) and by ``python tests/contracts/naviguide_harness.py
--record`` (writes that file; run it on the production branch to record the contract).

Offline: Mongo is mongomock-motor, every outbound HTTP request raises, and the two
routes that fetch remote data (ProtectedSeas for /api/amp, NOAA ENC for the NOAA export)
read stubs instead.

Shape of a response (``shape_of``):
  {"top": {key: type}, "nested": {key: {subkey: type}},
   "features": {"geometry_types": [...], "properties": {key: [types]}}}   # GeoJSON only
Types are JSON types: string, number (int or float), boolean, null, object, array.
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
CONTRACT_FILE = Path(__file__).with_name("naviguide.json")

# One sample zone with ports in seed/ports_of_entry.geojson (filled at seed time).
SAMPLE = {"mrgid": None}

# (id, path, query) — the GET routes NAVIGUIDE reads. "{mrgid}" is filled at seed time.
ROUTES: list[tuple[str, str, dict]] = [
    ("poe_ports", "/api/poe/ports", {"mrgid": "{mrgid}"}),
    ("poe_zones_geojson", "/api/poe/zones/geojson", {}),
    ("export_poe", "/api/export/poe.geojson", {}),
    ("export_route", "/api/export/route.geojson", {}),
    ("export_marinas", "/api/export/marinas.geojson", {}),
    ("export_anchorages", "/api/export/anchorages.geojson", {}),
    ("export_capitaineries", "/api/export/capitaineries.geojson", {}),
    ("export_noaa_aids", "/api/export/noaa-aids.geojson", {}),
    ("export_amp", "/api/export/amp.geojson", {}),
    ("export_science", "/api/export/science.geojson", {}),
    ("marinas", "/api/marinas", {}),
    ("capitaineries", "/api/capitaineries", {}),
    ("amp", "/api/amp", {"bbox": "-61.3,14.3,-60.8,14.9"}),  # = AMP_BBOX
    ("amp_nogo", "/api/amp/nogo", {}),
    ("amp_stats", "/api/amp/stats", {}),
    ("amp_site", "/api/amp/sites/amp-1", {}),
    ("climatology_meta", "/api/climatology/meta", {"month": 1}),
    ("climatology_point", "/api/climatology/point", {"lat": 14.6, "lon": -61.0, "month": 1}),
    ("climatology_crossings", "/api/climatology/crossings",
     {"lat1": 14.6, "lon1": -61.0, "lat2": 16.2, "lon2": -61.5, "month": 9}),
    ("climatology_wind", "/api/climatology/wind.geojson", {"month": 1, "spacing_deg": 4.0}),
    ("climatology_wave", "/api/climatology/wave.geojson", {"month": 1, "spacing_deg": 4.0}),
    ("climatology_current", "/api/climatology/current.geojson", {"month": 1, "spacing_deg": 4.0}),
    ("climatology_cyclones", "/api/climatology/cyclones.geojson", {"month": 9}),
    ("climatology_wind_tile", "/api/climatology/wind/tiles/2/1/1.json", {"month": 1}),
]
ROUTER_MODULES = ("formalities", "misc", "marinas", "capitaineries", "amp", "science",
                  "climatology")

NOW = "2026-10-01T00:00:00+00:00"
MQ_POLY = {"type": "Polygon", "coordinates": [[[-61.2, 14.4], [-60.9, 14.4], [-60.9, 14.8],
                                               [-61.2, 14.8], [-61.2, 14.4]]]}


# ── types and shapes ──────────────────────────────────────────────────

def jtype(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def shape_of(body) -> dict:
    """Top-level keys and types, one level into nested objects, and for a GeoJSON
    FeatureCollection the geometry types and the union of feature properties."""
    if not isinstance(body, dict):
        return {"top_type": jtype(body)}
    out: dict = {"top": {k: jtype(v) for k, v in body.items()}, "nested": {}}
    for k, v in body.items():
        if isinstance(v, dict) and k != "features":
            out["nested"][k] = {sk: jtype(sv) for sk, sv in v.items()}
    feats = body.get("features")
    if isinstance(feats, list):
        props: dict[str, set] = {}
        geoms: set = set()
        for f in feats:
            geoms.add(((f or {}).get("geometry") or {}).get("type") or "null")
            for k, v in ((f or {}).get("properties") or {}).items():
                props.setdefault(k, set()).add(jtype(v))
        out["features"] = {
            "count_nonzero": bool(feats),
            "geometry_types": sorted(geoms),
            "properties": {k: sorted(v) for k, v in sorted(props.items())},
        }
    return out


def breaking_changes(recorded: dict, current: dict) -> list[str]:
    """What breaks a consumer: a recorded key removed (or renamed), or a type that
    changed. New keys and new properties are allowed."""
    errs: list[str] = []
    if "top_type" in recorded:
        if recorded["top_type"] != current.get("top_type"):
            errs.append(f"body type {recorded['top_type']} -> {current.get('top_type')}")
        return errs
    cur_top = current.get("top") or {}
    for k, t in recorded.get("top", {}).items():
        if k not in cur_top:
            errs.append(f"top-level key removed: {k}")
        elif cur_top[k] != t:
            errs.append(f"top-level {k}: {t} -> {cur_top[k]}")
    for k, sub in recorded.get("nested", {}).items():
        cur_sub = (current.get("nested") or {}).get(k) or {}
        for sk, t in sub.items():
            if sk not in cur_sub:
                errs.append(f"{k}.{sk} removed")
            elif cur_sub[sk] != t:
                errs.append(f"{k}.{sk}: {t} -> {cur_sub[sk]}")
    rf = recorded.get("features")
    if rf is not None:
        cf = current.get("features")
        if cf is None:
            return errs + ["features list removed"]
        if rf.get("count_nonzero") and not cf.get("count_nonzero"):
            errs.append("features list is empty (seed data no longer returned)")
        extra_geoms = set(cf["geometry_types"]) - set(rf["geometry_types"])
        if extra_geoms and cf.get("count_nonzero"):
            errs.append(f"geometry types changed: {rf['geometry_types']} -> {cf['geometry_types']}")
        for k, types in rf["properties"].items():
            if k not in cf["properties"]:
                errs.append(f"feature property removed: {k}")
                continue
            changed = set(cf["properties"][k]) - set(types)
            if changed:
                errs.append(f"feature property {k}: {types} -> {cf['properties'][k]}")
    return errs


# ── seed data ─────────────────────────────────────────────────────────

def _seed_docs() -> dict[str, list[dict]]:
    sys.path.insert(0, str(BACKEND / "scripts"))
    import seed_ci_mongo as ci  # noqa: E402  (repo files only)

    ports_fc = json.loads((REPO / "seed" / "ports_of_entry.geojson").read_text(encoding="utf-8"))
    ports = ci._ports(ports_fc)
    by_zone: dict[int, int] = {}
    for p in ports:
        if p.get("mrgid") is not None:
            by_zone[int(p["mrgid"])] = by_zone.get(int(p["mrgid"]), 0) + 1
    SAMPLE["mrgid"] = max(sorted(by_zone), key=lambda m: by_zone[m])
    # Full-field docs so each property has a concrete type.
    for p in ports:
        p.setdefault("confidence", 60)
        p.setdefault("confidence_reasons", ["source d'État"])
        p.setdefault("confidence_parts", {"source": 30, "reading": 12, "map": 18, "external": 0})
    tags = {"seamark:type": "harbour", "website": "https://example.org"}
    return {
        "poe_ports": ports,
        "marinas": [
            {"_id": f"marina-{i}", "name": f"Marina {i}", "lat": 14.47 + i / 100, "lon": -60.87,
             "osm_id": f"node/{100 + i}", "source": "osm", "website": "https://example.org/m",
             "website_status": "ok", "website_source": "osm", "image": "https://example.org/i.jpg",
             "fetched_at": NOW, "tags": tags, "maps_place_url": "https://maps.example/p",
             "maps_place_status": "ok", "priority": 1}
            for i in range(2)],
        "capitaineries": [
            {"_id": "cap-1", "name": "Capitainerie du Marin", "lat": 14.468, "lon": -60.868,
             "osm_id": "node/200", "shom_id": "SHOM-1", "noaa_id": None, "source": "osm",
             "sources": ["osm", "shom"], "website": "https://example.org/c",
             "website_status": "ok", "website_source": "osm", "telephone": "+596 596 00 00 00",
             "canal_vhf": "9", "fetched_at": NOW}],
        "anchorages": [
            {"_id": "anch-1", "name": "Anse Mitan", "lat": 14.55, "lon": -61.06,
             "anchorage_type": "anchorage", "source": "osm", "osm_id": "node/300", "priority": 1,
             "nearest_waypoint": {"name": "Martinique", "dist_nm": 3.2}, "tags": tags,
             "fetched_at": NOW, "stale": False}],
        "science_items": [
            {"_id": "sci-1", "kind": "argo", "source": "argo", "name": "Float 6902", "provider":
             "Argo", "abstract": "profile", "url": "https://example.org/s", "doi": "10.1/x",
             "lat": 14.0, "lon": -60.0, "bbox": [-60.1, 13.9, -59.9, 14.1], "date": "2026-09-01",
             "profile_date": "2026-09-01", "cycle": 12, "ocean": "A", "wmo": "6902", "track": None,
             "ship": None, "start": "2026-09-01", "end": "2026-09-02", "fetched_at": NOW,
             "error_m": 5.0, "method": "gps"}],
        "amp_sites": [
            {"_id": "amp-1", "site_id": "amp-1", "name": "Réserve naturelle de Sainte-Anne",
             "country": "France", "state": "Martinique", "designation": "Réserve naturelle",
             "category_name": "Marine reserve", "iucn_cat": "IV", "purpose": "conservation",
             "lfp": 3, "managing_authority": "PNR Martinique", "gov_level": "National",
             "wdpa_id": "12345", "year_est": 1995, "entry": 1, "speed": 0,
             "manager_url": "https://example.org/amp", "other_helpful_links": "",
             "visit_url": "https://example.org/visit", "visit_url_status": "ok",
             "visit_url_source": "manager", "lat": 14.6, "lon": -61.05, "fetched_at": NOW,
             "enriched_at": NOW, "geometry": MQ_POLY}],
    }


NOAA_DOCS = [{"_id": "noaa-1", "noaa_id": "noaa-1", "name": "Ambrose Light", "kind": "light",
              "service": "aids", "lat": 40.45, "lon": -73.8}]


AMP_BBOX = (-61.3, 14.3, -60.8, 14.9)


async def _seed(db) -> None:
    from datetime import datetime, timezone

    from app.services import amp as amp_svc
    for coll, docs in _seed_docs().items():
        if docs:
            await db[coll].insert_many([dict(d) for d in docs])
    # A fresh tile for the /api/amp bbox: the cache answers, nothing is fetched.
    await db.amp_tiles.insert_one({"_id": amp_svc.tile_key(AMP_BBOX),
                                   "fetched_at": datetime.now(timezone.utc).isoformat()})


# ── app, database and network ─────────────────────────────────────────

@contextmanager
def offline_app():
    """FastAPI app with the contract routers, a seeded mongomock db in place of
    app.db.db everywhere, no outbound HTTP, and remote fetches stubbed."""
    import importlib

    import httpx
    from fastapi import FastAPI
    from mongomock_motor import AsyncMongoMockClient

    import app.db as appdb

    real_db = appdb.db
    fake_db = AsyncMongoMockClient()["naviguide_contract"]
    asyncio.run(_seed(fake_db))
    patched: list[tuple[object, str, object]] = []

    def patch(obj, name, value):
        patched.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    modules = [importlib.import_module(f"app.routers.{m}") for m in ROUTER_MODULES]
    patch(appdb, "db", fake_db)
    for mod in list(sys.modules.values()):
        name = getattr(mod, "__name__", "") or ""
        if not name.startswith("app."):
            continue
        for attr, val in list(vars(mod).items()):
            if val is real_db:
                patch(mod, attr, fake_db)

    def no_network(*a, **k):
        raise RuntimeError("contract tests are offline: outbound HTTP blocked")

    async def no_network_async(*a, **k):
        no_network()

    # The real network transports (TestClient uses its own in-process transport).
    patch(httpx.HTTPTransport, "handle_request", no_network)
    patch(httpx.AsyncHTTPTransport, "handle_async_request", no_network_async)

    from app.services import amp as amp_svc
    from app.services import noaa_enc

    async def noaa_stub(client, bbox, *a, **k):
        return [dict(d) for d in NOAA_DOCS]

    async def arcgis_stub(bbox, *a, **k):
        return []  # nothing remote: the cache (seeded) is the answer

    async def query_cache_stub(db, bbox, limit=400):
        # mongomock has no $geoIntersects: same selection, done with shapely.
        from shapely.geometry import box, shape
        area = box(*bbox)
        docs = await db.amp_sites.find({}).to_list(None)
        return [d for d in docs if d.get("geometry") and shape(d["geometry"]).intersects(area)][:limit]

    patch(noaa_enc, "fetch_aids", noaa_stub)
    patch(amp_svc, "fetch_arcgis", arcgis_stub)
    patch(amp_svc, "query_cache", query_cache_stub)

    marinas = sys.modules["app.routers.marinas"]
    if hasattr(marinas, "_MARINAS_FC_CACHE"):
        patch(marinas, "_MARINAS_FC_CACHE", {"fc": None, "built_at": 0.0})
    if hasattr(marinas, "_marinas_fc_task"):
        patch(marinas, "_marinas_fc_task", None)

    app = FastAPI()
    for mod in modules:
        app.include_router(mod.router)
    try:
        yield app
    finally:
        for obj, name, old in reversed(patched):
            setattr(obj, name, old)


def fill(query: dict) -> dict:
    return {k: (SAMPLE["mrgid"] if v == "{mrgid}" else v) for k, v in query.items()}


def call_all() -> dict[str, dict]:
    """{route id: {"path", "status", "shape"}} on the current code."""
    from fastapi.testclient import TestClient

    out = {}
    with offline_app() as app, TestClient(app) as client:
        for rid, path, query in ROUTES:
            resp = client.get(path, params=fill(query))
            entry = {"path": path, "query": query, "status": resp.status_code}
            if resp.status_code == 200:
                entry["shape"] = shape_of(resp.json())
            else:
                entry["error"] = resp.text[:300]
            out[rid] = entry
    return out


def record(source_ref: str) -> dict:
    routes = call_all()
    bad = {k: v for k, v in routes.items() if v["status"] != 200}
    if bad:
        raise SystemExit(f"routes failed while recording: {json.dumps(bad, indent=1)[:2000]}")
    return {
        "about": ("Response shape of the Blue Intelligence routes NAVIGUIDE (naviguide.fr) "
                  "consumes, recorded on the production branch with the seeded offline harness "
                  "(tests/contracts/naviguide_harness.py). A recorded key or feature property "
                  "must not be removed, renamed or change JSON type; new ones are allowed."),
        "recorded_from": source_ref,
        "types": "string | number | boolean | null | object | array",
        "routes": {k: {"path": v["path"], "query": v["query"], "shape": v["shape"]}
                   for k, v in routes.items()},
    }


if __name__ == "__main__":
    import argparse
    import os

    os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:1")
    os.environ.setdefault("DB_NAME", "naviguide_contract")
    sys.path.insert(0, str(BACKEND))
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", metavar="REF", help="write naviguide.json, noting REF as source")
    ap.add_argument("--out", default=str(CONTRACT_FILE))
    args = ap.parse_args()
    if args.record:
        Path(args.out).write_text(json.dumps(record(args.record), ensure_ascii=False, indent=1)
                                  + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(json.dumps(call_all(), ensure_ascii=False, indent=1)[:20000])
