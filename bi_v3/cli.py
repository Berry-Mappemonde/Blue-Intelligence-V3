"""bi-v3 CLI: run | import-v2 | diff | map | toloka | verify-memory"""
from __future__ import annotations
import argparse, json, os, sys, time
from . import runs, tf, tavily, tracing, mapgen, toloka, certify
from .hmem import HMem
from .diff import diff_runs
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS, SEED, MEM = (os.path.join(ROOT, d) for d in ("runs", "seed", "memory"))
TERR = {"Martinique": "mq,fr", "Guadeloupe": "gp,fr", "Saint-Martin": "mf,sx,fr", "Saint-Barthélemy": "bl,fr",
        "Dominica": "dm", "Saint Lucia": "lc", "Grenada": "gd", "Antigua and Barbuda": "ag"}

def cmd_run(a):
    from .pipeline import run_territory
    rid = runs.new_run_id(ROOT); os.makedirs(os.path.join(RUNS, rid), exist_ok=True)
    tracing.set_sink(os.path.join(RUNS, rid, "trace.jsonl")); t0 = time.time(); ents = []
    for t in a.territories:
        ents += run_territory(t, TERR.get(t, ""), SEED)
    meta = {"kind": "v3-full", "territories": a.territories, "duration_s": int(time.time() - t0),
            "nemotron": tf.LEDGER.summary(), "tavily_credits_estimated": dict(tavily.CREDITS), "models": tf.MODELS}
    d, m = runs.write_run(RUNS, rid, ents, meta)
    mem = HMem(MEM); [mem.remember(rid, e) for e in ents]; mem.save()
    print(json.dumps(m, indent=1, ensure_ascii=False))

def cmd_import_v2(a):
    v2 = json.load(open(os.path.join(SEED, "curated_marinas.json")))["marinas"]
    from .dedup import normalize_name
    ents = [{"layer": "marina", "territory": m.get("escale"), "name": m["name"], "key": normalize_name(m["name"]),
             "lat": m["lat"], "lon": m["lon"], "source_urls": [m["tags"]["website"]] if m.get("tags", {}).get("website") else [],
             "evidence": [], "level": "C0", "origin": "BI V2 curated_marinas.json (historical, read-only)"} for m in v2]
    rid = "00000000T000000-v2-historical"
    d, m = runs.write_run(RUNS, rid, ents, {"kind": "historical-import", "source": "Blue-Intelligence 4ba9c5f backend/data/curated_marinas.json"})
    mem = HMem(MEM); [mem.remember(rid, e) for e in ents]; mem.save(); print(json.dumps(m, indent=1))

def cmd_diff(a):
    d = diff_runs(runs.from_geojson(os.path.join(RUNS, a.a, "entities.geojson")), runs.from_geojson(os.path.join(RUNS, a.b, "entities.geojson")))
    print(json.dumps(d["summary"], indent=1))

def cmd_map(a):
    print(mapgen.build(RUNS, os.path.join(ROOT, "docs", "map.html")), "runs on map")

def cmd_toloka(a):
    ents = runs.from_geojson(os.path.join(RUNS, a.run, "entities.geojson"))
    s = toloka.sample(ents, a.per_layer); print(toloka.export_tsv(s, os.path.join(RUNS, a.run, "toloka_sample.tsv")), "tasks exported (not launched)")

def cmd_verify(a):
    print("ledger ok" if HMem(MEM).verify() else "LEDGER TAMPERED"); 

def main(argv=None):
    p = argparse.ArgumentParser("bi-v3"); sp = p.add_subparsers(required=True)
    r = sp.add_parser("run"); r.add_argument("territories", nargs="+"); r.set_defaults(f=cmd_run)
    sp.add_parser("import-v2").set_defaults(f=cmd_import_v2)
    d = sp.add_parser("diff"); d.add_argument("a"); d.add_argument("b"); d.set_defaults(f=cmd_diff)
    sp.add_parser("map").set_defaults(f=cmd_map)
    t = sp.add_parser("toloka"); t.add_argument("run"); t.add_argument("--per-layer", type=int, default=25); t.set_defaults(f=cmd_toloka)
    sp.add_parser("verify-memory").set_defaults(f=cmd_verify)
    a = p.parse_args(argv); a.f(a)

if __name__ == "__main__":
    main()
