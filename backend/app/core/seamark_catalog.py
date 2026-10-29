"""Read the OSM catalogue → BI fields (OpenSeaMap export target)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_CATALOG = Path(__file__).resolve().parents[2] / "data" / "seamark_catalog.json"


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    return json.loads(_CATALOG.read_text(encoding="utf-8"))


def exact_key(key: str) -> dict | None:
    for row in load_catalog().get("exact_keys") or []:
        if row.get("key") == key:
            return row
    return None


def seamark_type(typ: str) -> dict | None:
    for row in load_catalog().get("seamark_types") or []:
        if row.get("type") == typ:
            return row
    return None


def export_tags_for(badge: str | None = None, field: str | None = None) -> list[dict]:
    """Mapping inverse : badge / champ slim BI → tags OSM d'export."""
    rows = list(load_catalog().get("export_mapping") or [])
    if badge:
        rows = [r for r in rows if r.get("badge") == badge]
    if field:
        rows = [r for r in rows if r.get("field") == field]
    return rows


def osm_tags_for_field(field: str) -> dict:
    rows = export_tags_for(field=field)
    if not rows:
        return {}
    return dict(rows[0].get("osm_tags") or {})
