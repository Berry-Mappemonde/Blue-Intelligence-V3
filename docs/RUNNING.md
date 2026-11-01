# Running the PoE Reconciler

The agent product takes one maritime zone and returns its Ports of Entry, each with
a verdict, a confidence score, reasons and sources, plus a short brief for sailors.
It runs from a fresh clone with **no credentials and no MongoDB**.

```text
ZetarisAdapter (candidates) → ZetarisAdapter (registry check: UN/LOCODE + World Port Index)
  → HMemAgent (recall) → EvaluatorAgent ⇄ ResearchAgent (retry, max 2)
  → EvaluatorAgent (finalise) → BriefingAgent (Meterless) → HMemAgent (remember)
```

## 1. Install (once)

```bash
git clone https://github.com/Berry-Mappemonde/Blue-Intelligence.git
cd Blue-Intelligence
python3 -m venv backend/.venv      # Python 3.12 or newer
# The agent pipeline needs only these seven packages (pinned as in backend/requirements.txt):
backend/.venv/bin/pip install fastapi==0.110.1 httpx==0.28.1 pydantic==2.13.4 \
    python-dotenv==1.2.3 uvicorn==0.25.0 filelock==3.32.3 pycountry==26.2.16
```

`pycountry` resolves a zone's country from its sovereign when the EEZ index has no
code of its own (Alaska, the Azores). Without it those zones get no registry match,
and the sample breakdown below changes.

The full `backend/requirements.txt` is for the wider Blue Intelligence API. It
pins `torch==2.13.0+cpu`, which needs
`--extra-index-url https://download.pytorch.org/whl/cpu` on Linux and has no macOS
wheel. The pipeline never imports torch. For the tests, also `pip install pytest==9.1.1` (the full test set uses `backend/requirements-dev.txt`).

## 2. Sample mode (no credentials)

```bash
backend/.venv/bin/python backend/run.py --input input_examples/01_bermuda.json
```

With no `.env`, `run.py` turns on `SAMPLE_MODE` and says so on stderr. Ports come
from the bundled Zetaris fixture (`backend/data/sample/zetaris/poe_ports.json`,
20 real ports from `seed/ports_of_entry.geojson`). The registry check reads small
real subsets of UN/LOCODE and the World Port Index (`unlocode_ports.json`,
`wpi_ports.json` in the same folder: the sample zones' countries, with the US cut
to Alaska and Portugal to the Azores). The registry check, evaluator, retry loop,
H-MEM memory and briefing logic all run for real on that data; only the
Zetaris and model calls are replaced. The brief is then built deterministically
from the finalised ports (`llm.model = "caller-sample-fn"`).

Run the same input twice: the second run's `memory.recalled` is non-zero, because
H-MEM remembers the first run's verdicts (stored in `data/hmem/`, or `HMEM_DIR`).

The four input examples cover the main paths:

| Input | What it shows |
| --- | --- |
| `input_examples/01_bermuda.json` | `tier1` from stored official evidence (gov.bm) and a registry match; the WPI position (0.67 km) corroborates the geocode, so the port passes at 67 with no retry |
| `input_examples/02_angola.json` | `tier_registered`: registry match, no official page |
| `input_examples/03_azores.json` | `tier_registered`, with the country resolved from the sovereign (Portugal) |
| `input_examples/04_benin.json` | a retry round: EvaluatorAgent critiques the port (`retry`, "find an official source"), ResearchAgent looks for a remembered official source, twice, then the port is finalised as `flag` |

Zones in the sample fixture include Bermuda, Angola, Azores, Alaska, Albania,
Anguilla, Argentina, Australia, Bahamas, Barbados, Belgium and Belize.

## 3. Live mode

```bash
cp .env.example .env    # then fill in the Zetaris and Meterless values
backend/.venv/bin/python backend/run.py --input input_examples/02_angola.json
```

Live mode needs `ZETARIS_API_KEY`, `ZETARIS_ORG_ID` and `METERLESS_RUNTIME_TOKEN`
(the Zetaris PoE view is registered once by `backend/scripts/zetaris_setup.py --apply`).
`NVIDIA_API_KEY` is optional. The registry views need a one-off setup too (see
"Independent registries" below). Every external call is bounded and falls back:

| Call | Bound | Fallback |
| --- | --- | --- |
| Zetaris SQL | 15 s × 3 attempts | bundled sample data, trace `status: "fallback"`; once a run has fallen back, its registry check reads the fixtures directly |
| Nightshift probe | 3 s, cached 60 s | NVIDIA if configured, else sample brief |
| Nightshift / NVIDIA request | 90 s per provider (one retry inside it) | next provider, then sample brief |
| Whole run | 5 minutes | partial result with an `error` field |

The PoE search step uses a self-hosted SearXNG (`infra/searxng/`) at `SEARXNG_URL`,
default `http://127.0.0.1:8890`. It is not 8888 because the local Zetaris API
listens on 8888-8889. Public SearXNG instances remain as a fallback.

`llm.provider` and `llm.model` in the output show which model actually wrote the brief
(the served model, as reported by the runtime).

**Recorded live run:** `output_examples/01_bermuda.live_nemotron.json` and its trace
`output_examples/01_bermuda.live_nemotron.trace.jsonl` were recorded on 25 October 2026
against a local Zetaris instance and Meterless Nightshift, with
`METERLESS_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b`. Nightshift served
`nvidia/nemotron-3.5-super-vl-120b-a12b` to NemotronJudge and
`nvidia/nemotron-3-ultra-550b-a55b` to the brief (`provider: "nightshift"`). The
trace records both the requested and the served model, because the runtime may
route a request to a different model. Zetaris returned two Bermuda ports, both
`tier1` and `pass` at 67 with WPI-corroborated positions. The judge agreed with
both, quoting the gov.bm Customs page (`excerpt_verified: true`). H-MEM started
empty. The official evidence came from the stored snippets in Mongo
(`evidence_lookup` event: `status: "ok"`, `source: "mongo"`, three records). That
collection had been filled from the official-page snapshots through the generation
path (PR #49). It is a recording for reference: it cannot be reproduced
without credentials, and no code reads it.

### Evidence tiers and independent registries (why "official" is not circular)

The candidates in `blue_intel.poe_ports` are Blue Intelligence's own extraction, so
they cannot confirm themselves. The registry check (`ZetarisAdapter.registry_check`,
[`backend/app/agents/registry_check.py`](../backend/app/agents/registry_check.py))
weighs two independent kinds of evidence. The first is two third-party registries held in Zetaris:

| View | Source | Licence | Rows kept |
| --- | --- | --- | --- |
| `blue_intel.unlocode_ports` | [UN/LOCODE](https://unece.org/trade/cefact/unlocode-code-list-country-and-territory), UNECE ([download](https://unlocode.unece.org/downloads/unlocode-latest.zip)) | Published by UNECE for free use; no formal licence is stated | Function position 1 = "1" (port): 17,642 |
| `blue_intel.wpi_ports` | [World Port Index](https://msi.nga.mil/Publications/WPI), NGA Pub 150 ([CSV](https://msi.nga.mil/api/publications/download?type=view&key=16920959/SFH00000/UpdatedPub150.csv)) | US Government work, public domain | All entries: 3,807 |

- **Registry match:** same country code, the same name, and within 25 km when both
  sides have coordinates. Names are casefolded, without accents or punctuation;
  "Port of", "Porto de", "Harbour" and similar words are removed; St becomes Saint;
  "A / B" and "A (B)" are split; WPI alternate names and UN/LOCODE names without
  diacritics are included. A name and country match beyond 25 km is recorded as
  `coord_mismatch` with its distance. It is not a match, and the port is kept.
- **Official source evidence:** a URL that passes `is_official_source()` **and** whose
  text names the port (or a matched registry name) within ~200 characters of an
  entry term (`registry_check.py`, with French, Spanish, Portuguese, Italian and Greek
  equivalents). A *strong* term is enough on its own: port(s) of entry, entry port,
  clearing/clear customs, clear in, clearance port, designated port, designated
  marina, port of call for clearing. A *weak* term (customs, immigration, quarantine,
  harbour master, point(s) of entry, which also covers airports and land borders)
  counts only with a port, harbour, marina or quay word in the same
  window and a match on the full name, not on the town left after stripping "Port
  of" (Hamilton's customs "Long Room" does not make the Port of Hamilton an entry
  point). A mention inside an address or contact listing ("Cabinet Building,
  Hamilton", a street number, a postcode, an e-mail) does not count, and neither does
  a bare mention. The text is either
  fetched now (`evidence_source: "fetched_now"`) or a stored snippet of an earlier
  fetch (`"stored_snippet"`); it is never model output. Without text, a URL proves
  nothing.

| Tier (`registry.tier`) | Label | Evidence | `zetaris_validation` |
| --- | --- | --- | --- |
| `tier1` | Official Commercial Port of Entry | official source + registry match | `official` |
| `tier2` | Official Yachting Port of Entry | official source, no registry match | `official` |
| `tier_registered` | Registered port, entry status unverified | registry match only | `registered - entry status unverified` |
| `tier3` | Unconfirmed - review needed | neither | `unconfirmed - review needed` |

A registry listing never makes a port of entry on its own: UN/LOCODE and the WPI
list ports, not clearance status. WPI "First Port of Entry" is recorded as
supporting evidence only. Each port carries `registry` (`tier`, `label`,
`validation`, `matches`, `official_evidence`, `coord_mismatch`,
`first_port_of_entry`, `reasons`). Each check is a `ZetarisAdapter`
`registry_check` trace event that states the tier and why.

**Stored evidence.** Generation (`generate_zone_poe`) builds evidence records from
the official pages it fetched, deterministically: `{mrgid, port_key, url, domain,
matched_name, snippet, content_sha256, fetched_at, extract_level}`, where `snippet`
is a verbatim slice of at most 400 characters around a mention that passes the
check above and `content_sha256` hashes the full fetched text. At most three per
port go to the Mongo collection `poe_evidence` (unique on `mrgid` + `port_key` +
`url`). `run.py`, `POST /run` and `/api/poe/verify` read ports from Zetaris, so
their registry step looks the records up by zone (`evidence_store.lookup`) and runs
the same check on each snippet before it counts. In `SAMPLE_MODE`, or when Mongo is
unreachable, the lookup reads small verbatim snapshots of real official pages in
[`backend/data/sample/official_pages/`](../backend/data/sample/official_pages/)
and builds the records the same way. A failed lookup gives no evidence, never an
official tier. Each lookup is a `ZetarisAdapter` `evidence_lookup` trace event.

**Fill evidence on a deployment without regenerating.** A live deployment whose Mongo
has no `poe_evidence` yet finds no stored evidence, and its ports stay at their
registry tier. `backend/scripts/seed_evidence_from_snapshots.py` fills the collection
from the committed snapshots through the generation path: each page's text and fetch
metadata go through `documents_from_context`, `build_records` and `store`, exactly as
after a fetch. The port names come from the zone's ports in Zetaris (live when
configured, otherwise the sample fixture). No record is written by hand. Re-running
writes the same records (upsert on `mrgid` + `port_key` + `url`).

```bash
cd backend
.venv/bin/python scripts/seed_evidence_from_snapshots.py                          # dry run: prints each record, new / update / unchanged
.venv/bin/python scripts/seed_evidence_from_snapshots.py --zone Bermuda --apply   # writes to MONGO_URL / DB_NAME (or --mongo-url, --db)
```

After `--apply`, a live run's `evidence_lookup` event shows `source: "mongo"`.

In the sample run, 2 of the 20 sample ports are `tier1` (Port of St. George's,
Bermuda: gov.bm Customs and a Bermuda planning report; Anchorage Small Boat Harbor,
Alaska: CBP's designated marina), 2 are `tier2` (Deep water Harbor, Antigua:
customs.gov.ag; Conch Inn Marina, Bahamas: Bahamas Customs), 11 are
`tier_registered` and 5 are `tier3`. Before stored evidence, the same run gave 13
`tier_registered` and 7 `tier3`.

UN/LOCODE coordinates are `DDMMN DDDMMW` and are converted to decimal degrees.
The zone's country comes from the EEZ index (the sovereign's code for zones such as
Alaska or the Azores). If the zone's country is unknown, the check queries a box
around the candidates and matches only ports that carry their own `country_iso2`.

Setup (administrators, once; the full datasets are downloaded to the gitignored
`backend/data/registries/`, never committed):

```bash
cd backend
.venv/bin/python scripts/zetaris_setup.py --part registries            # dry run: plan + SQL
.venv/bin/python scripts/zetaris_setup.py --part registries --apply    # download, stage, register, verify counts
.venv/bin/python scripts/zetaris_setup.py --part registries --prepare-only --fixtures  # rebuild the sample fixtures
```

`--apply` downloads both files, writes `unlocode_ports.csv` and `wpi_ports.csv`,
copies them into the Zetaris container (`docker cp` into
`/home/zetaris/customer-data/shared/blue_intel`; `--stage none` if you copy them
yourself), registers them as read-only file-store tables plus the two views, and
compares each view's `COUNT(*)` with the local CSV. `--unlocode-zip` and `--wpi-csv`
use files you have already downloaded. The agents only ever read the views through
the adapter's read-only `query()`.

## 4. CLI and HTTP

```bash
# CLI: a zone name, or a JSON input file; JSON result on stdout (and optionally a file)
backend/.venv/bin/python backend/run.py --zone "Azores" --sample-mode
backend/.venv/bin/python backend/run.py --input input_examples/03_azores.json --output out.json

# HTTP server on PORT (default 8000, host 127.0.0.1; set HOST=0.0.0.0 in a container)
backend/.venv/bin/python backend/run.py --serve
# in a second terminal:
curl -s localhost:8000/healthz
curl -s localhost:8000/zones
curl -s -X POST localhost:8000/run -H 'Content-Type: application/json' \
     -d '{"zone_name": "Bermuda", "run_id": "demo-1"}'
curl -s localhost:8000/runs/demo-1/trace
```

**Demo page (verification console):** with the server running, open
<http://localhost:8000> in a browser, pick a zone and press **Run verification**.
The page streams the run from `GET /run/stream` and shows:

- a top bar with the mode and the Zetaris, H-MEM and LLM status, plus a
  "Nemotron via Nightshift" badge when the served model is a Nemotron model;
- counts for confirmed and flagged ports, retry rounds, memories recalled and duration;
- a chart of the zone with port pins coloured by verdict (click a pin, or
  **Locate** on a card, to link the two);
- the agent log, filled in step by step as each trace event arrives: agent,
  action, handoffs, retry rounds and durations;
- port cards (verdict, confidence, coordinates, reasons split into
  "Supporting" and "Concerns", sources tagged "from memory" when they came from
  H-MEM) and the brief as a clearance card, captioned with the model that wrote it.

It is one file, [`backend/app/pipeline/demo_page.html`](../backend/app/pipeline/demo_page.html).
The chart uses Leaflet 1.9.4 from unpkg (pinned, with Subresource Integrity) and
OpenStreetMap tiles. Without network access, or if the tiles fail, it draws an
offline plot of the port coordinates instead, so sample mode works with no
network. Open <http://localhost:8000/?chart=plot> to use the offline plot directly.

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Demo page |
| `POST /run` | Run the pipeline for one zone (JSON in, JSON out) |
| `GET /run/stream?zone=…` | The same run as Server-Sent Events: one `trace` event per agent step as it happens, then a `result` event with the `POST /run` output |
| `GET /zones` | Zones available now: live Zetaris, or the sample fixture (`mode`: `live`, `sample` or `fallback`) |
| `GET /runs/{run_id}/trace` | Trace events of the latest run with that `run_id`, as JSON |
| `GET /healthz` | Health and whether `SAMPLE_MODE` is on |

If port 8000 is already in use, the server exits with "address already in use", and
`curl` would reach whichever program holds the port. Start it with another port,
for example `PORT=8001 backend/.venv/bin/python backend/run.py --serve`, and use
`localhost:8001` in the `curl` commands and the browser.

CLI options: `--zone NAME` or `--input FILE` (one is required), `--run-id ID`,
`--sample-mode` (force offline sample data), `--timeout SECONDS` (default 300),
`--trace PATH` (trace file, like `TRACE_LOG_PATH`), `--output FILE`, and
`--no-dotenv` (ignore `.env` files, to simulate a fresh clone). The result JSON goes
to stdout after the trace events; a readable summary goes to stderr. Exit code 0 on
success, 1 if the result has an `error`, 2 for invalid input.

Input: `{"zone_name": str, "run_id": optional str}` (`run_id`: letters, digits, `_`, `-`).
Output: `run_id`, `zone`, `mode` (`live` or `sample`), `ports` (name, verdict,
confidence, lat, lon, reasons_en, sources with provenance `zetaris` or `hmem`,
memories_recalled, research, registry `{tier, label, validation, matches,
official_evidence, coord_mismatch, first_port_of_entry, reasons}`, judge
`{verdict, reason, quoted_excerpt, excerpt_verified, disagrees}` or null), `summary`, `brief`,
`llm`, `components`, `registry` (`mode`, `country_iso2`, `registry_entries`,
`official_count`, `tiers`, `coord_mismatch_count`), `judge` (`status` `ok` or `skipped`,
`reason`, `provider`, `served_model`, `retry_requested`), `retries`, `memory`, `trace_file`, `timings_ms`, `error`. Examples: `input_examples/` and
`output_examples/` (generated by running the commands above in sample mode).

## 5. Traces

Every agent writes JSONL events with the same `run_id` to stdout and to
`logs/trace-<run_id>.jsonl` (override with `TRACE_LOG_PATH`; `TRACE_STDOUT=0`
silences stdout). Agents: `RunPipeline`, `ZetarisAdapter`, `HMemAgent`,
`EvaluatorAgent`, `NemotronJudge`, `ResearchAgent`, `BriefingAgent`, `MeterlessGenerator`.
Handoffs are recorded in `handoff`; retries appear as `prepare_retry` events and
ResearchAgent `research_port` outcomes (see `output_examples/04_benin.trace.jsonl`).
`NemotronJudge` writes one `critique` event per run: an advisory Nemotron reading of the
zone's official evidence text, or `status: skipped` with the reason (SAMPLE_MODE, no
official text, no provider answer, timeout). It records `port.judge` and may route a
flagged port it calls a port of entry into one more research round; it never writes a
tier, validation, official evidence or confidence (`backend/app/agents/poe_judge.py`).
`output_examples/*.trace.jsonl` are copies of real traces from the example runs.

## 6. Tests

```bash
cd backend
MONGO_URL=mongodb://127.0.0.1:27017 DB_NAME=bi_test POE_AUTO_REFRESH=0 \
  .venv/bin/python -m pytest tests/test_run_pipeline.py tests/test_zetaris_registries.py -v
```

`test_run_pipeline.py` includes fresh-clone simulations (`run.py --input` and
`run.py --zone`, in a clean environment). Live tests run only with `ZETARIS_LIVE=1` or `NIGHTSHIFT_LIVE=1`
and real credentials in the environment.

H-MEM serializes access with a file lock: `filelock` (installed above; Windows,
Linux and macOS). Without it, the lock falls back to `fcntl` (Linux and macOS), and
then to a thread-only lock that does not protect against other processes.
