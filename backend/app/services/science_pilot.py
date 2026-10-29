"""Ingest Sentinel pilots (outside the SOURCES harvest)."""
from __future__ import annotations

from app.core.export_meta import versioned_fc

PILOT_SOURCE = "sentinel-pilot"


def docs_from_pilot_fc(fc: dict) -> list[dict]:
    """OSM-shaped GeoJSON → science_items documents. Never in SOURCES."""
    docs = []
    meta = fc.get("metadata") or {}
    for i, feat in enumerate(fc.get("features") or []):
        geom = feat.get("geometry") or {}
        p = feat.get("properties") or {}
        typ = geom.get("type")
        coords = geom.get("coordinates") or []
        lat = lon = None
        track = None
        if typ == "Point" and len(coords) >= 2:
            lon, lat = float(coords[0]), float(coords[1])
        elif typ == "LineString" and len(coords) >= 2:
            track = [[float(pt[0]), float(pt[1])] for pt in coords if len(pt) >= 2]
            mid = track[len(track) // 2]
            lon, lat = mid[0], mid[1]
        elif typ == "Polygon" and coords:
            ring = coords[0] if coords else []
            if ring:
                lon = sum(float(pt[0]) for pt in ring) / len(ring)
                lat = sum(float(pt[1]) for pt in ring) / len(ring)
        if lat is None or lon is None:
            continue
        native = str(p.get("id") or p.get("native_id") or i)
        docs.append({
            "_id": f"{PILOT_SOURCE}:{native}",
            "kind": p.get("kind") or ("coastline" if typ == "LineString" else "dataset"),
            "source": PILOT_SOURCE,
            "native_id": native,
            "name": str(p.get("name") or f"Sentinel pilote {native}")[:240],
            "abstract": str(p.get("abstract") or p.get("disclaimer") or "")[:600],
            "url": p.get("url"),
            "doi": p.get("doi"),
            "provider": p.get("provider") or "CDSE / pilote corridor",
            "lat": lat,
            "lon": lon,
            "track": track,
            "error_m": p.get("error_m"),
            "method": p.get("method"),
            "schema": "science_v1",
            "pilot_version": meta.get("version"),
        })
    return docs


def wrap_pilot_export(fc: dict, dataset: str = "sentinel-coastline") -> dict:
    return versioned_fc(fc, dataset, extra_metadata={"source": PILOT_SOURCE})
