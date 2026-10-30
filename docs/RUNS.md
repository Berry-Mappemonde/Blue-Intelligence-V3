# Runs

A run is one PoE generation pass over a set of EEZ zones. Mongo stays the
source of truth (`poe_runs`, `poe_run_zones`, `poe_run_ports`,
`poe_run_events`); live collections such as `poe_ports` are never written.
Code: `backend/app/services/unified_runs.py`, `run_artifacts.py`, `poe_runs.py`.

Each run also has a folder `backend/data/runs/<run_id>/` (`BI_RUNS_DIR` overrides the root):

- `trace.jsonl`: append-only, one line per step (`run_start`, `run_resume`,
  `unit_start`, `unit_done`, `unit_error`, `unit_retry_skipped`,
  `unit_missing_in_mongo`, `run_done`).
  Each line is flushed and fsynced. A line torn by a crash is ignored with a
  warning when the trace is read.
- `manifest.json`: rewritten atomically (temp file + rename) at start and at end.

## Start

- API: `POST /api/runs` with `{"layer": "poe", "zones": [...], "limit": 0, "variant": "tinyfish"}`.
  `POST /api/poe/runs` is a wrapper of the same entry; `/api/poe/runs/multi` is deprecated.
- Python: `unified_runs.start_run(db, state, layer="poe", ...)`.

A new run never reuses an existing `run_id`. Only the `poe` layer is driven
here; projects, marinas, harbour offices and MPAs start from their own
endpoints but write the same trace and manifest.

## Resume

- API: `POST /api/runs` with `{"run_id": "...", "resume": true}`.
- CLI: `python backend/run.py --resume <run_id>` (needs `MONGO_URL` / `DB_NAME`
  of the database holding the run; summary JSON on stdout, logs on stderr).

A run_id is launched at most once at a time per API process (a concurrent
start or resume of the same run gets 409).

A resume never changes the run's mode. The mode is recorded at start
(`params.code.sample_mode`); a resume whose `SAMPLE_MODE` differs is refused
(400 on the API). The CLI never switches `SAMPLE_MODE` on by itself for
`--resume`: a live run (or one started before the mode was recorded) needs
`SAMPLE_MODE` off and the Zetaris / Meterless credentials, a sample run needs
`--sample-mode`; otherwise it exits with code 3 and writes nothing.

The stored parameters (zones, limit, variant, rules) are reused. A zone is
skipped only when it has a `poe_run_zones` doc, so completed zones are never
regenerated and their ports never rewritten. Mongo is the source of truth: a
zone with a `unit_done` trace line but no `poe_run_zones` doc is flagged
(`unit_missing_in_mongo`) and regenerated. A zone that was started but not
finished is regenerated.

Errored zones count as done by default. With `retry_errored`
(`"retry_errored": true`, or `--retry-errored` on the CLI) they are redone
once in total, across all resumes: a zone is errored when its last
`unit_done` / `unit_error` trace line is an error (`poe_run_zones.status` for
runs without a trace). A zone already retried (a `unit_start` line with
`retry: true`) or that already has ports stored for the run is not retried.

## Export

- API: `POST /api/runs/<run_id>/export`. CLI: `python backend/run.py --export <run_id>`.

Only `done` or `cancelled` runs can be exported. Output goes to
`backend/data/exports/<run_id>/` (`BI_EXPORT_DIR` overrides the root):
`ports.geojson`, `zones.json`, `trace.jsonl`, `manifest.json`. An existing
export folder is never overwritten, and a second concurrent export of the
same run is refused (409). GeoParquet is not written (no pyarrow /
geopandas dependency).

Paths stored in Mongo (`artifacts.dir`, `exports[].dir`) and returned by the
API are relative to the data dir (`runs/<run_id>`, `exports/<run_id>`).

## Manifest fields

| Field | Content |
|---|---|
| `schema` | `bi-run-manifest/1` |
| `run_id`, `layer`, `state`, `written_at` | identity and state at write time |
| `git` | `sha`, `dirty`, `resume_sha` (code at resume) |
| `config` | label, limit, zones, concurrency, variant, profile, `rules_hash` |
| `models` | provider and model names (no keys) |
| `counts` | zones total / done, `by_status`, `ports_total`, errors (export: score / level coverage) |
| `files` | `path`, `sha256`, `bytes` of each file next to the manifest |
| `sources` | number of source URLs and their domains |
| `tool_versions` | Python and key package versions |
| `cost` | Token Factory usage when available |
| `fields` | `confidence` (V2 score 0-100) and `confidence_level` (C0-C4, nullable): copied as stored, never derived from each other |
| `export` | export only: formats, GeoParquet note, SHA-256 of the run manifest |

No secret is written: keys are stripped as in the run journal.

## Upload

Off by default. The export is uploaded to S3-compatible object storage
(Nebius) only when `BI_OBJECT_STORAGE_UPLOAD=1` and
`BI_OBJECT_STORAGE_BUCKET` are set (`BI_OBJECT_STORAGE_ENDPOINT`,
`_REGION`, `_PREFIX` optional). Keys are `runs/<run_id>/<file>`, manifest
last. Credentials come from boto3's standard chain; the code never reads them.

A failed upload does not fail the export: it is recorded on the run
(`exports[].uploaded: false`, `upload_error`). Retry it without re-exporting
with `POST /api/runs/<run_id>/upload` or `python backend/run.py --upload <run_id>`:
the folder is first checked against its manifest (sizes, SHA-256, no extra
file, manifest hash recorded on the run), and refused (409 / exit 2) if it
does not match. Each attempt is recorded under `poe_runs.uploads`.
