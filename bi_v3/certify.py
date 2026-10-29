"""Certification levels C0..C4 (BI-V3-PLAN §5). A level never rests on an LLM-only point above C1."""
from __future__ import annotations
import re
GOV_HINTS = (".gouv.fr", ".gov", "gouv.", "douane", "customs", "prefecture", "collectivite", "cacem", "ville-", "mairie")
AGGREGATORS = ("navily", "marinareservation", "noonsite", "superyacht", "tripadvisor", "wikipedia", "facebook", "yachtinsidersguide", "azmartinique", "marineconnect", "boatsetter")

def host(url: str) -> str:
    h = (url or "").split("//", 1)[-1].split("/", 1)[0].lower()
    return h[4:] if h.startswith("www.") else h

def is_official(url: str, name: str = "") -> bool:
    """Official = government domain, or the facility's own domain (host shares a >=4-char token with its name)."""
    h = host(url)
    if any(a in h for a in AGGREGATORS):
        return False
    if any(g in h or g in (url or "").lower() for g in GOV_HINTS):
        return True
    toks = [t for t in re.findall(r"[a-z]{4,}", (name or "").lower()) if t not in ("port", "marina", "harbour", "plaisance", "office", "capitainerie")]
    return any(t in h for t in toks)

def level(entity: dict) -> str:
    ev = entity.get("evidence") or []
    if not ev:
        return "C0"
    lvl = "C0"
    if any(e.get("extracted") and is_official(e.get("url", ""), entity.get("name", "")) for e in ev):
        lvl = "C1"
    if lvl == "C1" and any(e.get("judge") == "supported" for e in ev):
        lvl = "C2"
    g = entity.get("geo") or {}
    if lvl == "C2" and g.get("agree") and g.get("independent_sources", 0) >= 2:
        lvl = "C3"
    if lvl == "C3" and entity.get("human") == "confirmed":
        lvl = "C4"
    return lvl
