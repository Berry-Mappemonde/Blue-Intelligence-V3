# Blue Intelligence

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE-MIT)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE-APACHE)
[![NVIDIA Nemotron](https://img.shields.io/badge/NVIDIA-Nemotron-76B900.svg)](https://www.nvidia.com/en-us/ai-data-science/foundation-models/nemotron/)

A [Berry-Mappemonde](https://berrymappemonde.org) project. Code is **MIT or Apache-2.0**.

> [!WARNING]
> **Not for navigation.**
> Maps and figures come from public sources and computations.
> This is not an official nautical chart. Always check government
> publications before making any decision at sea.

**Blue Intelligence** ([blueintelligence.online](https://blueintelligence.online))
is the memory of maritime sources: Ports of Entry per EEZ, marinas,
harbour offices (capitaineries), marine protected areas, ocean science and
marine projects. Each record keeps its official source. An unknown field
stays empty; no figure is invented.

The live voyage application **NAVIGUIDE** is a separate repository:
[github.com/Berry-Mappemonde/NAVIGUIDE](https://github.com/Berry-Mappemonde/NAVIGUIDE)
([naviguide.fr](https://naviguide.fr)). It reads Blue Intelligence over its
public API, `https://blueintelligence.online/api`.

## PoE Reconciler (Open Agent Hackathon 2026)

**What it does.** Give it one maritime zone (an EEZ, such as Bermuda or the Azores)
and it returns that zone's Ports of Entry, the places where a sailor may legally
clear customs and immigration on arrival. Each port comes with a verdict, a 0–100
confidence score with reasons, its sources, and an **evidence tier** that says what
backs it: an official government page that designates it, an independent port
registry (UN/LOCODE, NGA World Port Index), both, or neither. A short brief for
sailors closes each run. The answer is built from scattered public sources, and a
registry listing alone is never presented as port-of-entry status.

- **Tier = legal-status evidence:** what says this port is a port of entry (an official page, a registry, both or neither).
- **Verdict = data-quality review:** whether the record is reliable enough to use as is (`pass`) or needs a human look (`flag`), from its sources, reading and coordinates.

### Architecture

```text
RunPipeline ─► ZetarisAdapter  candidates from Zetaris (blue_intel.poe_ports)
            ─► ZetarisAdapter  registry check (UN/LOCODE + WPI views in Zetaris)
                               + stored official evidence → evidence tier
            ─► HMemAgent       recall earlier verdicts (Meterless H-MEM engine)
            ─► EvaluatorAgent  score each port: pass, flag or retry
            ─► NemotronJudge   advisory critique of the official text (Nemotron)
            ─► EvaluatorAgent ⇄ ResearchAgent   retry with memory (max 2)
            ─► EvaluatorAgent  finalise: pass or flag, never dropped
            ─► BriefingAgent   brief via Meterless Nightshift → NVIDIA Nemotron
            ─► HMemAgent       remember verdicts (created / reinforced / superseded)
```

- **Agents:** seven named agents share one `run_id` and one JSONL trace, with
  handoffs, critique and retry events: `RunPipeline` (the orchestrator),
  `ZetarisAdapter`, `HMemAgent`, `EvaluatorAgent`, `NemotronJudge`,
  `ResearchAgent` and `BriefingAgent`. Each model call is also traced as
  `MeterlessGenerator`, recording the requested and the served model.
- **NemotronJudge (advisory critic):** one Nemotron call per zone, through
  Nightshift, on the official evidence text the run already holds. It records a
  verdict (`poe`, `not_poe`, `unclear`), a reason and a quoted excerpt per port,
  and whether that excerpt is really in the evidence text. It never overrides
  evidence: no tier, validation, official evidence or score comes from it. When it
  calls a flagged port a port of entry, it triggers one more research round. It is
  skipped, and the trace says why, without credentials (`SAMPLE_MODE`) or without
  an answer. Code: [`poe_judge.py`](backend/app/agents/poe_judge.py); tests:
  [`test_poe_judge.py`](backend/tests/test_poe_judge.py); live recording:
  [`01_bermuda.live_nemotron.json`](output_examples/01_bermuda.live_nemotron.json)
  and [its trace](output_examples/01_bermuda.live_nemotron.trace.jsonl).
- **Zetaris (data layer):** ports, and the two independent registries, are read
  through Zetaris SQL with a read-only guard, a timeout and a fallback to bundled
  real fixtures.
- **Evidence tiers:**

  | Tier | Meaning |
  | --- | --- |
  | `tier1` Official Commercial Port of Entry | official page designates it, and a registry lists it |
  | `tier2` Official Yachting Port of Entry | official page designates it, and no registry lists it (typical for marinas) |
  | `tier_registered` | registry match only; entry status unverified |
  | `tier3` | neither: review needed |

  An official page counts only if its URL is a government domain and its text
  names the port near an entry term. A strong term ("port of entry", "clear
  customs", "designated marina") is enough on its own. A weak term ("customs",
  "immigration") also needs a port word nearby and the port's full name. Pages
  fetched during generation are stored as verbatim snippets, never model output.
  Runs then look the snippets up by zone.
- **Meterless:** the H-MEM engine (a Python port that passes the 13 official
  conformance vectors) makes memory load-bearing. Nightshift is the primary model
  provider and serves Nemotron.

### Quickstart: fresh clone, no credentials, no database

```bash
git clone https://github.com/Berry-Mappemonde/Blue-Intelligence.git
cd Blue-Intelligence
python3 -m venv backend/.venv   # Python 3.12+ (macOS's /usr/bin/python3 is 3.9: too old)
backend/.venv/bin/pip install fastapi==0.110.1 httpx==0.28.1 pydantic==2.13.4 \
    python-dotenv==1.2.3 uvicorn==0.25.0 filelock==3.32.3 pycountry==26.2.16
backend/.venv/bin/python backend/run.py --zone Bermuda
backend/.venv/bin/python backend/run.py --input input_examples/01_bermuda.json --output bermuda.json
```

With no `.env`, `run.py` turns on `SAMPLE_MODE` and logs it on stderr. In this
mode the agents run for real on bundled data, and only the Zetaris and model
calls are replaced. Expected output:

- **stderr:** a summary block (abridged):

  ```text
  WARNING run: SAMPLE_MODE enabled automatically; missing: ZETARIS_API_KEY, ZETARIS_ORG_ID, METERLESS_RUNTIME_TOKEN or MODEL_API_KEY
  Pipeline Result
  Zone:     Bermuda
  Mode:     sample
  Ports:    1
  Summary:  {'pass': 1}
  LLM:      sample / caller-sample-fn
  Memory:   0 recalled, remembered
  ```

- **stdout:** each trace event as one JSON line, then the result JSON, with keys
  `run_id`, `zone`, `mode`, `ports`, `summary`, `brief`, `llm`, `components`,
  `registry`, `judge`, `retries`, `memory`, `trace_file`, `timings_ms` and `error`.
  `--output` also writes the result to a file.
- **Trace file:** `logs/trace-<run_id>.jsonl`. For the input example that's
  `logs/trace-example-bermuda.jsonl`.
- **Memory:** run the same zone again and `memory.recalled` is no longer 0, because
  H-MEM stored the first run in `data/hmem/`.

The 20 sample zones include Bermuda, Angola, Azores, Alaska, Bahamas and Antigua
and Barbuda. `input_examples/04_benin.json` shows a retry round: EvaluatorAgent
critiques the port, ResearchAgent looks for a remembered official source, twice. Across them the sample run gives 2 `tier1`, 2 `tier2`, 11
`tier_registered` and 5 `tier3`. Official evidence comes from small verbatim
snapshots of real government pages in
[`backend/data/sample/official_pages/`](backend/data/sample/official_pages/).

The same pipeline runs over HTTP:

```bash
backend/.venv/bin/python backend/run.py --serve   # http://127.0.0.1:8000; PORT=… if taken
curl -s -X POST http://127.0.0.1:8000/run -H 'Content-Type: application/json' \
     -d '{"zone_name": "Angola"}'
```

It also serves `GET /healthz` and a demo page at `/`.

### Live mode

```bash
cp .env.example .env     # fill in the values below
backend/.venv/bin/python backend/run.py --input input_examples/02_angola.json
```

- **Zetaris:** `ZETARIS_REST_URL`, `ZETARIS_ORG_ID` and `ZETARIS_API_KEY`. The
  PoE view and the registry views are registered once by
  `backend/scripts/zetaris_setup.py`.
- **Meterless Nightshift:** `METERLESS_BASE_URL` (default
  `http://127.0.0.1:8088/v1`), `METERLESS_RUNTIME_TOKEN` and `METERLESS_MODEL`
  (default `meterless/auto`).
- **Optional:** `NVIDIA_API_KEY`, for a direct Nemotron fallback when Nightshift
  is down.

Every external call has a timeout and a fallback, in this order: Nightshift, then
NVIDIA, then the sample brief. A Zetaris failure falls back to the bundled
fixtures. `llm.provider` and `llm.model` report the model that actually wrote the
brief; Nightshift may route to a different Nemotron than the one requested.

### Sponsor technology and evidence

[`docs/sponsors/SPONSOR_TECH.md`](docs/sponsors/SPONSOR_TECH.md) (merges with
PR #31) links every claim about Zetaris, Meterless H-MEM, Nightshift and NVIDIA
Nemotron to the code and tests that back it. Setup details, bounds and tests are
in [`docs/RUNNING.md`](docs/RUNNING.md).

### Limits, stated plainly

- **Sample data is small:** 20 real ports, one per zone, and small real
  subsets of the registries. Official evidence exists only for zones whose
  official pages have been fetched: 4 zones in the sample.
- **Tier and verdict can differ** (definitions above). Only a port with official
  evidence can have its position corroborated by a registry (≤ 2 km), which `pass`
  needs: St. George's passes (`tier1`, 67), Road Bay stays `flag` (`tier_registered`).
- **Grounding is a heuristic:** it checks the official domain, the entry term
  and the distance. A port name with "Port of" stripped can match the town.
  The committed snapshots were reviewed by hand.
- **Recorded output:** the recorded live run in `output_examples/` is for
  reference. Nothing reads it, and judges cannot reproduce it without
  credentials.
- **Not for navigation:** always check official publications before arriving.

## Dependencies

| Part | Runtime | Dependencies |
|---|---|---|
| API (`backend/`) | Python **3.12** | `backend/requirements.txt` (FastAPI, Motor/PyMongo, httpx, trafilatura, PyMuPDF, shapely…) |
| Database | **MongoDB** (local or Atlas) | database name from `DB_NAME` |
| Map (`frontend/`) | Node.js (version in `.nvmrc`) | `frontend/package.json` (React, Create React App, MapLibre/Leaflet, Tailwind) |
| Optional AI and search | — | NVIDIA NIM (Nemotron), OpenRouter, Anthropic, TinyFish, Serper, SearXNG, GeoNames. Without keys the API still runs; AI steps fall back to rules or are skipped. |

## Installation (macOS / Linux)

```bash
git clone https://github.com/Berry-Mappemonde/Blue-Intelligence.git
cd Blue-Intelligence

# API
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt   # pins a Linux-only torch +cpu wheel: on macOS use the Quickstart pins above, or install requirements.txt without the torch line
cat > .env <<'EOF'
MONGO_URL=mongodb://127.0.0.1:27017
DB_NAME=blueintel_db
CORS_ORIGINS=*
EOF
cd ..

# Map
cd frontend
npm ci
cd ..
```

Keys go in `backend/.env` only, never in git: `NVIDIA_API_KEY`,
`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `TINYFISH_API_KEY`,
`SERPER_API_KEY`, `SEARXNG_URL`, `GEONAMES_USERNAME`, `ADMIN_KEY`.
`LLM_PROVIDER=nvidia|openrouter|auto` selects the model provider.

Optional sample data (the same seed the CI uses):

```bash
cd backend
MONGO_URL=mongodb://127.0.0.1:27017 DB_NAME=blueintel_db .venv/bin/python scripts/seed_ci_mongo.py
```

## Usage

```bash
# API on http://127.0.0.1:8001/api/
cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001

# Map on http://localhost:3000 (another terminal)
cd frontend && REACT_APP_BACKEND_URL=http://127.0.0.1:8001 npm start
```

Tests need the test dependencies in `backend/requirements-dev.txt` (it includes
`requirements.txt` and adds pytest, its CI plugins and the in-memory Mongo used by the
offline contract tests). Production installs `requirements.txt` only, never the dev file.

```bash
cd backend && .venv/bin/pip install -r requirements-dev.txt
cd backend && .venv/bin/python -m pytest tests -q      # needs MongoDB and the API running
cd frontend && npm test                                # Jest
```

Production runs on an OVH VPS (`infra/vps/README.md`): every merge on
`main` passes CI (`.github/workflows/ci.yml`) and deploys
blueintelligence.online (`.github/workflows/deploy.yml`).

Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Documentation index: [docs/README.md](docs/README.md).

## Licence

Code is **MIT or Apache License 2.0** (`LICENSE-MIT`, `LICENSE-APACHE`).
Data (OpenStreetMap, Marine Regions, EMODnet…) keeps its original licence.
Nemotron weights remain under the NVIDIA licence.

---

## Français (résumé)

**Blue Intelligence** est la mémoire des sources maritimes (ports d'entrée
par ZEE, marinas, capitaineries, aires protégées, science, projets), en
production sur [blueintelligence.online](https://blueintelligence.online).
Le voyage en direct **NAVIGUIDE** est dans son propre dépôt :
[github.com/Berry-Mappemonde/NAVIGUIDE](https://github.com/Berry-Mappemonde/NAVIGUIDE).
Installation et usage : voir les sections ci-dessus (Python 3.12, MongoDB,
Node.js ; `backend/.env` pour `MONGO_URL`, `DB_NAME` et les clés).

### Licence

Le code est sous **MIT ou Apache License 2.0** (`LICENSE-MIT`,
`LICENSE-APACHE`). Les données (OpenStreetMap, Marine Regions, EMODnet…)
gardent leur licence d’origine. Les poids Nemotron restent sous licence
NVIDIA.

Index des plans : [docs/README.md](docs/README.md).
