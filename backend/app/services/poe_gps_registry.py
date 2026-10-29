"""Git registry of arbitrated PoE GPS — not a Python dict.

File: docs/data/poe-gps-arbitrated.json (PR review).
Claude never picks a point. WPI / UN/LOCODE / web = candidates,
not a truth set without a score.
"""
from __future__ import annotations

import json
from pathlib import Path

# backend/app/services/this.py → repo root (not app.config: Mongo).
GPS_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "data" / "poe-gps-arbitrated.json"
)

ALLOWED_STATUS = frozenset({"proposed", "accepted", "rejected"})
ALLOWED_ACTION = frozenset({"correct", "keep"})
ALLOWED_SOURCE = frozenset({"wpi", "unlocode", "manual", "web"})

_CACHE: dict | None = None
_BY_KEY: dict[str, dict] | None = None


def load_gps_registry(*, force: bool = False) -> dict:
    """Load the JSON. `force` for tests that rewrite the file."""
    global _CACHE, _BY_KEY
    if _CACHE is not None and not force:
        return _CACHE
    raw = GPS_REGISTRY_PATH.read_text(encoding="utf-8")
    data = json.loads(raw)
    entries = list(data.get("entries") or [])
    by_key: dict[str, dict] = {}
    for e in entries:
        key = str(e.get("dedup_key") or "")
        if not key:
            raise ValueError("entrée registre GPS sans dedup_key")
        if key in by_key:
            raise ValueError(f"dedup_key dupliquée dans le registre GPS : {key}")
        st = e.get("status")
        if st not in ALLOWED_STATUS:
            raise ValueError(f"{key}: status {st!r}")
        act = e.get("action")
        if act not in ALLOWED_ACTION:
            raise ValueError(f"{key}: action {act!r}")
        src = e.get("source")
        if src not in ALLOWED_SOURCE:
            raise ValueError(f"{key}: source {src!r}")
        float(e["lat"])
        float(e["lon"])
        by_key[key] = e
    _CACHE = data
    _BY_KEY = by_key
    return data


def registry_path() -> Path:
    return GPS_REGISTRY_PATH


def registry_hit(dedup_key: str) -> dict | None:
    """Raw entry (any status), or None."""
    load_gps_registry()
    return (_BY_KEY or {}).get(str(dedup_key or ""))


def accepted_by_key(dedup_key: str) -> dict | None:
    """`status=accepted` entry for a key, or None."""
    hit = registry_hit(dedup_key)
    if hit and hit.get("status") == "accepted":
        return hit
    return None


def keep_keys() -> frozenset[str]:
    load_gps_registry()
    return frozenset(
        e["dedup_key"]
        for e in ((_CACHE or {}).get("entries") or [])
        if e.get("status") == "accepted" and e.get("action") == "keep"
    )


def entries_with_candidates() -> list[dict]:
    """Accepted + wrong/right — offline regression battery."""
    data = load_gps_registry()
    out = []
    for e in data.get("entries") or []:
        if e.get("status") != "accepted":
            continue
        if e.get("wrong") and e.get("right"):
            out.append(e)
    return out


def public_view() -> dict:
    """Workshop payload: the JSON, no Mongo, no persist."""
    data = load_gps_registry()
    entries = list(data.get("entries") or [])
    return {
        "version": data.get("version"),
        "updated_at": data.get("updated_at"),
        "comment": data.get("comment"),
        "n_entries": len(entries),
        "n_accepted": sum(1 for e in entries if e.get("status") == "accepted"),
        "n_keep": sum(
            1 for e in entries
            if e.get("status") == "accepted" and e.get("action") == "keep"),
        "n_correct": sum(
            1 for e in entries
            if e.get("status") == "accepted" and e.get("action") == "correct"),
        "path": "docs/data/poe-gps-arbitrated.json",
        "wrote_poe_ports": False,
        "seeds_build": False,
        "entries": entries,
    }
