# Blue Intelligence V3

Certified maritime layers for cruising sailors: **ports of entry, marinas, harbour offices**.
V3 is a clean new repository; V1/V2 (`Berry-Mappemonde/Blue-Intelligence`) is untouched (under judging until 6 Nov 2026).

## Pipeline (one full run)
1. **Tavily Search** (several queries per territory and layer) → **Tavily Extract** of the top pages (official domains first).
2. **Nemotron Super** (`nvidia/nemotron-3-super-120b-a12b`, Nebius Token Factory) extracts facilities with a verbatim quote; the quote must be found in the page (`extracted=true`), page-printed coordinates are kept only if the numbers appear in the text.
3. Dedup (V2 spatial-text rules, 500 m / 60–90 %) + Nemotron canonicalisation of aliases.
4. **Nemotron Ultra** (`nvidia/Nemotron-3-Ultra-550b-a55b`) judges each claim: supported / unsupported.
5. Geo: Nominatim + an independent second source (page coordinates, NGA World Port Index, BI V2 curated seeds).
6. Certification **C0–C4**: C1 official source extracted · C2 + Ultra "supported" · C3 + 2 geo sources < 500 m · C4 + human (Toloka). Never an LLM-only point above C1.

## Runs without loss
`runs/<run_id>/` = `entities.geojson` + `manifest.json` (SHA-256, counts per level, rules, models, tokens, cost) + `trace.jsonl`.
`run_id` = timestamp + code fingerprint + rules fingerprint. A run can never overwrite another. V2 seeds are imported as a read-only historical run.
`memory/` is an H-MEM-style memory (inspired by the Meterless H-MEM spec): entity → runs → evidence, best-of with lineage, and a hash-chained append-only ledger (`bi-v3 verify-memory`).

## Commands
```
export NEBIUS_API_KEY=... TAVILY_API_KEY=...   # env only, never in files
python -m bi_v3.cli import-v2
python -m bi_v3.cli run Martinique Guadeloupe
python -m bi_v3.cli diff <run_a> <run_b>
python -m bi_v3.cli map            # docs/map.html: run selector, colour by level, diff mode
python -m bi_v3.cli toloka <run>   # stratified sample TSV for Toloka (never launched automatically)
python -m pytest -q tests
```
Optional tracing to LangSmith: `LANGSMITH_TRACING=true LANGSMITH_API_KEY=...` (otherwise local OTel-shaped JSONL per run).

License: MIT / Apache-2.0 (same as V1/V2).
