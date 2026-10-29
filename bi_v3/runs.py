"""Immutable runs: runs/<run_id>/{entities.geojson,manifest.json,trace.jsonl}. A run never overwrites another."""
from __future__ import annotations
import hashlib, json, os, platform, subprocess, time
RULES = {"dedup_km": 0.5, "geo_agree_km": 0.5, "moved_km": 2.0, "levels": "C0-C4 v1"}

def code_fingerprint(root: str) -> str:
    h = hashlib.sha256()
    for dp, _, fs in sorted(os.walk(os.path.join(root, "bi_v3"))):
        for f in sorted(fs):
            if f.endswith(".py"):
                h.update(open(os.path.join(dp, f), "rb").read())
    return h.hexdigest()[:12]

def new_run_id(root: str) -> str:
    rules = hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()[:8]
    return time.strftime("%Y%m%dT%H%M%S") + f"-c{code_fingerprint(root)}-r{rules}"

def to_geojson(entities):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": None if e.get("lat") is None else {"type": "Point", "coordinates": [e["lon"], e["lat"]]},
         "properties": {k: v for k, v in e.items() if k not in ("lat", "lon")}} for e in entities]}

def from_geojson(path):
    out = []
    for f in json.load(open(path))["features"]:
        e = dict(f["properties"]); g = f.get("geometry")
        e["lat"], e["lon"] = (g["coordinates"][1], g["coordinates"][0]) if g else (None, None)
        out.append(e)
    return out

def write_run(runs_dir, run_id, entities, meta):
    d = os.path.join(runs_dir, run_id)
    if os.path.exists(os.path.join(d, "manifest.json")):
        raise FileExistsError("runs are immutable: " + run_id)
    os.makedirs(d, exist_ok=True)
    gj = json.dumps(to_geojson(entities), ensure_ascii=False, indent=1)
    open(os.path.join(d, "entities.geojson"), "w").write(gj)
    counts = {}
    for e in entities:
        counts.setdefault(e["layer"], {}).setdefault(e["level"], 0); counts[e["layer"]][e["level"]] += 1
    manifest = {"run_id": run_id, "rules": RULES, "python": platform.python_version(),
                "entities": len(entities), "counts": counts,
                "sha256": {"entities.geojson": hashlib.sha256(gj.encode()).hexdigest()}, **meta}
    json.dump(manifest, open(os.path.join(d, "manifest.json"), "w"), ensure_ascii=False, indent=1)
    return d, manifest
