"""wpi_ports — World Port Index (NGA Pub 150) as a commercial counter-list.

WPI is an inventory of commercial / industrial ports (US public
domain). Ingest it, match it to already-known seeds (name + ~1 km),
set `wpi_commercial`. It is **never** proof that a yacht can
clear: no new seed, no `seed_sources`, no GPS, no
confirmed/probable verdict, no token sent to the judge.

The WPI “First Port of Entry” field is ignored as legal status.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from app.config import DATA_DIR
from app.core.dedup import normalize_name
from app.core.geo import haversine_km

WPI_FILE = DATA_DIR / "wpi_ports.json"
WPI_PROXIMITY_KM = 1.0
WPI_SOURCE = "NGA World Port Index Pub 150 (UpdatedPub150.csv)"

_INDEX_KEYS = (
    "World Port Index Number", "WPI Number", "wpinumber", "INDEX_NO", "index",
)
_NAME_KEYS = ("Main Port Name", "main_port_", "PORT_NAME", "name")
_ALT_KEYS = ("Alternate Port Name", "alternate_", "alt")
_LAT_KEYS = ("Latitude", "LATITUDE", "Y", "lat")
_LON_KEYS = ("Longitude", "LONGITUDE", "X", "lon")
_USE_KEYS = ("Harbor Use", "harbor_use", "harborUse")
_COUNTRY_KEYS = ("Country Code", "countryCode", "COUNTRY", "country")
_UNLOCODE_KEYS = ("UN/LOCODE", "unlocode", "UNLOCODE")

_DMS_RE = re.compile(
    r"(?P<hem1>[NSEW])?\s*(?P<deg>\d+(?:\.\d+)?)\s*[°\s]\s*"
    r"(?:(?P<min>\d+(?:\.\d+)?)\s*['′]?\s*)?"
    r"(?:(?P<sec>\d+(?:\.\d+)?)\s*[\"″]?\s*)?"
    r"(?P<hem2>[NSEW])?",
    re.I,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell(row: dict, keys: tuple[str, ...]) -> str:
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        raw = row.get(key)
        if raw is None:
            raw = low.get(key.lower())
        text = str(raw or "").strip()
        if text and text.lower() not in ("none", "nan"):
            return text
    return ""


def _wpi_index(raw: str) -> str:
    text = (raw or "").strip()
    if text.endswith(".0") and text.replace(".", "", 1).replace("-", "", 1).isdigit():
        text = text[:-2]
    try:
        num = float(text)
        if num.is_integer():
            return str(int(num))
    except ValueError:
        pass
    return text


def parse_wpi_coord(raw) -> float | None:
    """Decimal, or DMS like 36°50'00"N. None if empty / invalid."""
    if raw is None:
        return None
    text = str(raw).strip().strip('"').replace("''", '"')
    if not text or text.lower() in ("none", "nan"):
        return None
    try:
        val = float(text)
        if abs(val) <= 180:
            return val
    except ValueError:
        pass
    m = _DMS_RE.search(text.replace(" ", ""))
    if not m:
        m = _DMS_RE.search(text)
    if not m:
        return None
    deg = float(m.group("deg") or 0)
    minutes = float(m.group("min") or 0)
    sec = float(m.group("sec") or 0)
    hem = (m.group("hem2") or m.group("hem1") or "").upper()
    val = deg + minutes / 60.0 + sec / 3600.0
    if hem in ("S", "W"):
        val = -abs(val)
    if abs(val) > 180:
        return None
    return val


def parse_wpi_row(row: dict) -> dict | None:
    """One CSV/JSON row → slim record, or None if unusable."""
    name = _cell(row, _NAME_KEYS)
    lat = parse_wpi_coord(_cell(row, _LAT_KEYS) or row.get("lat"))
    lon = parse_wpi_coord(_cell(row, _LON_KEYS) or row.get("lon"))
    if not name or lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    rec = {
        "index": _wpi_index(_cell(row, _INDEX_KEYS)),
        "name": name[:200],
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "country": _cell(row, _COUNTRY_KEYS),
        "harbor_use": _cell(row, _USE_KEYS) or "Unknown",
    }
    alt = _cell(row, _ALT_KEYS)
    if alt and alt.lower() != name.lower():
        rec["alt"] = alt[:200]
    unlocode = _cell(row, _UNLOCODE_KEYS)
    if unlocode:
        rec["unlocode"] = unlocode[:12]
    return rec


def parse_wpi_csv(src: str | Path | io.StringIO) -> list[dict]:
    """Parse the official UpdatedPub150.csv (utf-8 or latin-1)."""
    if isinstance(src, io.StringIO):
        text = src.getvalue()
    else:
        path = Path(src)
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    seen: set[str] = set()
    for row in reader or []:
        rec = parse_wpi_row(row or {})
        if rec is None:
            continue
        key = rec.get("index") or f"{rec['lat']}:{rec['lon']}:{normalize_name(rec['name'])}"
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def ingest_wpi_records(ports: list[dict], *, generated_at: str | None = None) -> dict:
    """Snapshot document (not PoE evidence)."""
    clean: list[dict] = []
    seen: set[str] = set()
    for raw in ports or []:
        rec = parse_wpi_row(raw)
        if rec is None:
            continue
        key = rec.get("index") or f"{rec['lat']}:{rec['lon']}:{normalize_name(rec['name'])}"
        if key in seen:
            continue
        seen.add(key)
        clean.append(rec)
    return {
        "generated_at": generated_at or _now_iso(),
        "source": WPI_SOURCE,
        "license": "United States public domain",
        "n": len(clean),
        "note": (
            "Contre-liste commerce/industriel. wpi_commercial n'est jamais "
            "une preuve PoE plaisance. First Port of Entry WPI est ignoré."
        ),
        "ports": clean,
    }


def ingest_wpi_csv(src: str | Path | io.StringIO, *,
                   generated_at: str | None = None) -> dict:
    return ingest_wpi_records(parse_wpi_csv(src), generated_at=generated_at)


def write_wpi_snapshot(doc: dict, path: str | Path | None = None) -> Path:
    dest = Path(path) if path else WPI_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return dest


@lru_cache(maxsize=1)
def load_wpi_ports(path: str | None = None) -> dict:
    p = Path(path) if path else WPI_FILE
    if not p.is_file():
        return {"n": 0, "ports": [], "source": WPI_SOURCE}
    return json.loads(p.read_text(encoding="utf-8"))


def wpi_is_commercial(_rec: dict | None = None) -> bool:
    """Every valid WPI entry is a commercial counter-list (Harbor Use often Unknown)."""
    return True


def wpi_name_keys(rec: dict) -> list[str]:
    out = []
    for raw in (rec.get("name"), rec.get("alt")):
        key = normalize_name(raw or "")
        if key and key not in out:
            out.append(key)
    return out


def _names_overlap(a: str, b: str) -> bool:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(short) >= 6 and short in long_


def wpi_matches_name(seed_name: str, rec: dict) -> bool:
    if not (seed_name or "").strip():
        return False
    return _names_overlap(seed_name, rec.get("name") or "") or (
        bool(rec.get("alt")) and _names_overlap(seed_name, rec.get("alt") or ""))


def match_wpi_port(seed: dict, ports: list[dict] | None = None,
                   radius_km: float | None = None) -> dict | None:
    """Match an already-known seed. None without name+proximity (or a unique name).

    Do not create a seed. Do not copy the WPI GPS.
    """
    from app.core.run_rules import get_rule
    if radius_km is None:
        radius_km = float(get_rule("formalities.wpi_proximity_km", WPI_PROXIMITY_KM))
    name = (seed.get("name") or "").strip()
    if not name:
        return None
    catalog = ports if ports is not None else (load_wpi_ports().get("ports") or [])
    lat = lon = None
    try:
        if seed.get("lat") is not None and seed.get("lon") is not None:
            lat, lon = float(seed["lat"]), float(seed["lon"])
    except (TypeError, ValueError):
        lat = lon = None
    if lat is not None:
        # ~2 km prefilter (1 km radius + slack).
        box = [
            rec for rec in catalog
            if abs(float(rec.get("lat") or 99) - lat) <= 0.02
            and abs(float(rec.get("lon") or 199) - lon) <= 0.02
        ]
        best = None
        for rec in box:
            if not wpi_matches_name(name, rec):
                continue
            try:
                dist = haversine_km(lat, lon, float(rec["lat"]), float(rec["lon"]))
            except (KeyError, TypeError, ValueError):
                continue
            if dist <= radius_km and (best is None or dist < best[0]):
                best = (dist, rec)
        return None if best is None else best[1]
    key = normalize_name(name)
    named = [rec for rec in catalog if key in wpi_name_keys(rec)]
    if len(named) == 1:
        return named[0]
    return None

