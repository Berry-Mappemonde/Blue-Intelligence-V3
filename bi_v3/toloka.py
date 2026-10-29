"""Stratified human-check sample for Toloka (BI-V3-PLAN §6). Exports a TSV for upload; never launches/pays."""
from __future__ import annotations
import csv, random

def sample(entities, per_layer=25, seed=42):
    rnd = random.Random(seed); out = []
    for layer in sorted({e["layer"] for e in entities}):
        pool = [e for e in entities if e["layer"] == layer and e["level"] in ("C2", "C3") and e.get("source_urls")]
        out += rnd.sample(pool, min(per_layer, len(pool)))
    return out

def export_tsv(entities, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["INPUT:entity_id", "INPUT:question", "INPUT:url", "INPUT:lat", "INPUT:lon"])
        for e in entities:
            w.writerow([e["key"], f'Does this official page confirm that "{e["name"]}" ({e["layer"]}) exists at this place? Yes / No / Cannot tell',
                        e["source_urls"][0], e.get("lat"), e.get("lon")])
    return len(entities)
