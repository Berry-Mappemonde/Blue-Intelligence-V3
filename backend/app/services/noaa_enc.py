"""NOAA ENC Direct — US lights / buoys via the ArcGIS API. No S-57 file."""
from __future__ import annotations

from app.core.export_meta import versioned_fc
from app.core.identity import OVERLAY_RADIUS_KM, find_building

NOAA_MAPSERVER = "https://gis.charttools.noaa.gov/arcgis/rest/services/encdirect"
USER_AGENT = "BlueIntelligence/1.0 (+https://blueintelligence.online)"

# H1 (2026-09-14): public layer_id lights / buoys. No vector DEPARE.
NOAA_AID_LAYERS: tuple[tuple[str, int, str], ...] = (
    ("enc_harbour", 11, "light"),
    ("enc_harbour", 6, "buoy_lateral"),
    ("enc_harbour", 1, "beacon"),
    ("enc_approach", 13, "light"),
    ("enc_approach", 8, "buoy"),
    ("enc_coastal", 10, "light"),
    ("enc_coastal", 5, "buoy"),
    ("enc_berthing", 6, "light"),
)

# CONUS + Alaska + Hawaii + Porto Rico (west, south, east, north).
US_BBOXES: tuple[tuple[float, float, float, float], ...] = (
    (-125.0, 24.0, -66.0, 49.5),
    (-179.5, 51.0, -129.0, 72.0),
    (-160.5, 18.8, -154.5, 22.4),
    (-68.0, 17.8, -65.2, 18.6),
)

LICENSE = (
    "NOAA ENC Direct to GIS — not for navigation. "
    "https://nauticalcharts.noaa.gov/"
)


def parse_bbox(raw: str) -> tuple[float, float, float, float] | None:
    parts = [p.strip() for p in (raw or "").split(",")]
    if len(parts) != 4:
        return None
    try:
        west, south, east, north = (float(p) for p in parts)
    except (TypeError, ValueError):
        return None
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        return None
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        return None
    if south > north:
        return None
    return west, south, east, north


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    aw, as_, ae, an = a
    bw, bs, be, bn = b
    return aw < be and ae > bw and as_ < bn and an > bs


def bbox_intersects_us(bbox: tuple[float, float, float, float]) -> bool:
    return any(_overlap(bbox, us) for us in US_BBOXES)


def _geojson_latlon(geom: dict) -> tuple[float, float] | None:
    if not geom:
        return None
    typ = geom.get("type")
    coords = geom.get("coordinates")
    if typ == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        lon, lat = float(coords[0]), float(coords[1])
        return lat, lon
    if typ in ("Polygon", "MultiPolygon", "LineString") and coords:
        # naive centroid of the first ring / first point
        ring = coords
        while isinstance(ring, (list, tuple)) and ring and isinstance(ring[0], (list, tuple)):
            ring = ring[0]
        if isinstance(ring, (list, tuple)) and len(ring) >= 2 and not isinstance(ring[0], (list, tuple)):
            return float(ring[1]), float(ring[0])
    return None


def aid_from_feature(feat: dict, *, service: str, layer_id: int, kind: str) -> dict | None:
    props = feat.get("properties") or feat.get("attributes") or {}
    coords = _geojson_latlon(feat.get("geometry") or {})
    if coords is None:
        return None
    lat, lon = coords
    fid = (
        feat.get("id")
        or props.get("OBJECTID")
        or props.get("FID")
        or props.get("objectid")
    )
    name = (
        props.get("OBJNAM") or props.get("objnam")
        or props.get("INFORM") or props.get("inform")
        or kind
    )
    noaa_id = f"noaa:{service}:{layer_id}:{fid}"
    return {
        "_id": noaa_id,
        "noaa_id": noaa_id,
        "name": str(name)[:120],
        "kind": kind,
        "service": service,
        "layer_id": layer_id,
        "lat": lat,
        "lon": lon,
        "source": "noaa",
        "tags": {
            "noaa:layer": f"{service}:{layer_id}:{kind}",
            "noaa:objnam": str(props.get("OBJNAM") or props.get("objnam") or "")[:80],
        },
    }


def overlay_osm_light(noaa_doc: dict, osm_pts: list[dict], radius_km: float = OVERLAY_RADIUS_KM):
    """Same 250 m gesture as the office: distance only, not 500 m same_site."""
    if not noaa_doc:
        return None
    return find_building(noaa_doc.get("lat"), noaa_doc.get("lon"), osm_pts, radius_km=radius_km)


async def fetch_aids(client, bbox: tuple[float, float, float, float],
                     layers: tuple | None = None) -> list[dict]:
    """Tiled ENC Direct query. Zero calls outside the US bbox."""
    if not bbox_intersects_us(bbox):
        return []
    west, south, east, north = bbox
    out: list[dict] = []
    seen: set[str] = set()
    for service, layer_id, kind in (layers or NOAA_AID_LAYERS):
        url = f"{NOAA_MAPSERVER}/{service}/MapServer/{layer_id}/query"
        params = {
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
            "resultRecordCount": "500",
        }
        try:
            r = await client.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=45)
        except Exception:
            continue
        ct = (getattr(r, "headers", {}) or {}).get("content-type") or ""
        if getattr(r, "status_code", 0) != 200 or "json" not in ct.lower():
            continue
        try:
            fc = r.json() if r.content else {}
        except Exception:
            continue
        for feat in fc.get("features") or []:
            doc = aid_from_feature(feat, service=service, layer_id=layer_id, kind=kind)
            if not doc or doc["_id"] in seen:
                continue
            seen.add(doc["_id"])
            out.append(doc)
    return out


def aids_geojson(docs: list[dict]) -> dict:
    features = []
    for d in docs:
        lat, lon = d.get("lat"), d.get("lon")
        if lat is None or lon is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
            "properties": {
                "id": d.get("_id") or d.get("noaa_id"),
                "name": d.get("name"),
                "kind": d.get("kind"),
                "service": d.get("service"),
                "source": "noaa",
            },
        })
    return versioned_fc(
        {"type": "FeatureCollection", "features": features},
        "noaa-aids",
        license_note=LICENSE,
    )
