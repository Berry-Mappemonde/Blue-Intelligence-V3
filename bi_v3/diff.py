"""Run-vs-run diff, generalised from BI V2 services/poe_diff.py to every layer."""
from __future__ import annotations
from .dedup import find_duplicate, normalize_name
from .geo import haversine_km

def _domains(urls):
    out = set()
    for u in urls or []:
        h = str(u).split("//", 1)[-1].split("/", 1)[0].lower()
        out.add(h[4:] if h.startswith("www.") else h)
    return out

def diff_runs(baseline: list[dict], candidate: list[dict], moved_km: float = 2.0) -> dict:
    left = list(baseline); matched, added = [], []
    for c in candidate:
        m = find_duplicate(c, left)
        if m is None:
            added.append(c); continue
        left.remove(m); flags = []; km = None
        if normalize_name(m.get("name")) != normalize_name(c.get("name")):
            flags.append("renamed")
        if None not in (m.get("lat"), m.get("lon"), c.get("lat"), c.get("lon")):
            km = round(haversine_km(m["lat"], m["lon"], c["lat"], c["lon"]), 2)
            if km > moved_km:
                flags.append("moved")
        if _domains(m.get("source_urls")) != _domains(c.get("source_urls")):
            flags.append("resourced")
        if m.get("level") != c.get("level"):
            flags.append("recertified")
        matched.append({"flags": flags, "move_km": km, "before": m, "after": c})
    summary = {"baseline": len(baseline), "candidate": len(candidate), "matched": len(matched),
               "unchanged": sum(1 for m in matched if not m["flags"]), "added": len(added), "removed": len(left)}
    for f in ("renamed", "moved", "resourced", "recertified"):
        summary[f] = sum(1 for m in matched if f in m["flags"])
    return {"summary": summary, "matched": matched, "added": added, "removed": left}
