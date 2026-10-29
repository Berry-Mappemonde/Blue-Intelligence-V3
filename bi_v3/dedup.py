"""Spatial-text dedup (ported verbatim in logic from BI V2 core/dedup.py)."""
from __future__ import annotations
import difflib, math, re, unicodedata
from .geo import haversine_km

DIST_THRESHOLD_KM, SIM_LOW, SIM_HIGH = 0.5, 0.60, 0.90

def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s)[:40]

def text_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    al, bl = a.lower().strip(), b.lower().strip()
    raw = difflib.SequenceMatcher(None, al, bl).ratio()
    norm = difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()
    ta = " ".join(sorted(re.findall(r"[a-z0-9]+", al))); tb = " ".join(sorted(re.findall(r"[a-z0-9]+", bl)))
    return max(raw, norm, difflib.SequenceMatcher(None, ta, tb).ratio())

def _xy(d):
    try:
        lat, lon = float(d.get("lat")), float(d.get("lon"))
        return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) else None
    except (TypeError, ValueError):
        return None

def is_duplicate(a: dict, b: dict, key: str = "name") -> bool:
    sim = text_similarity(str(a.get(key) or ""), str(b.get(key) or ""))
    if sim >= SIM_HIGH:
        return True
    xa, xb = _xy(a), _xy(b)
    if not xa or not xb:
        return False
    return haversine_km(*xa, *xb) < DIST_THRESHOLD_KM and sim >= SIM_LOW

def find_duplicate(c: dict, docs: list[dict], key: str = "name"):
    return next((d for d in docs if d.get("layer", c.get("layer")) == c.get("layer") and is_duplicate(c, d, key)), None)

def deduplicate(docs: list[dict]) -> list[dict]:
    out: list[dict] = []
    for d in docs:
        dup = find_duplicate(d, out)
        if dup is None:
            out.append(d)
        else:
            dup["evidence"] = (dup.get("evidence") or []) + (d.get("evidence") or [])
            for k, v in d.items():
                if dup.get(k) in (None, "", []) and v not in (None, "", []):
                    dup[k] = v
    return out
