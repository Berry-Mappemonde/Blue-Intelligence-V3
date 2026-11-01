# Specification — Recreational Ports of Entry

Scoping document for the **Formalities** mode of Blue Intelligence.
It rereads the code, the PRD, the architecture, and the decisions of previous agents.
It is written in plain language: it is the contract of what we are looking for, and what we refuse.

Version 1.4 — 7 September 2026. **Complete** document (object, strategies, rules, tools, code, data, interface, acceptance tests, risks, annexes).

**1.2** freezes the missing contract of the 3rd Word pass (#3 / #20): Bottom-Up **list detector**, not only a yes/no on a name; both arms **in parallel**; stop at the first genuine list; judge only the **residue**. It also freezes the map rule: we **remember** each mapping step to compare them; we **publish** only a map that is confident enough (Noonsite listing coverage + official sheet per EEZ for rereading).

**1.3** freezes the **VLIZ grain**: one EEZ = **one polygon** (`mrgid`), never a country. France has 23 polygons. We search, attach and produce a sheet **by polygon**.

**1.4** freezes the **review sheet**: one sheet per VLIZ polygon, **one** Top-Down URL (the State page or PDF that lists the PoE), the **PoE list**, **one** Bottom-Up URL **per port**. No Generate button. A future world TD∥BU run would be **versioned** (`poe_run_*`, one pass per `mrgid`), without erasing v1 or runs already done — **not to be launched** until the NVIDIA models are wired.

**Contents**

1. In one sentence
2. Why this work exists
3. What we want to obtain
4. What we do not want
5. Vocabulary
6. The two strategies
7. Rules
8. Sources and tools
9. The code — where each piece lives
10. Confidence score
11. Hard constraints
12. Acceptance criteria
13. Current state and gaps
14. Recommended work order
15. Documents and conversations this specification inherits from
16. Who does what
17. Lifecycle of a port / an EEZ
18. The second deliverable: official sources per EEZ
19. Algorithm of the two bundles (D and P)
20. Data model
21. What the user sees
22. OpenStreetMap inventory
23. World Port Index and UN/LOCODE
24. Concrete examples
25. Acceptance tests
26. Risks
27. Out of scope
28. Annexes

---

## 1. In one sentence

For every Exclusive Economic Zone (EEZ) in the world, find **all official Ports of Entry usable by a foreign recreational vessel**, and **the State pages that list them**.

---

## 2. Why this work exists

There is no single, official, up-to-date world list of the ports where a foreign yacht can complete its formalities (customs, immigration, clearance).

Each State designates its own points of entry, in its language, on its own media: customs site, gazette, decree, PDF, order, maritime portal. These lists change. Some speak of recreational craft. Others list all designated ports, including commercial terminals.

Blue Intelligence maps these lists for the Berry-Mappemonde expedition and, more broadly, for any skipper who crosses EEZs. The information remains **indicative**: one always checks with the authorities before departure.

---

## 3. What we want to obtain

Two deliverables, inseparable, **per EEZ** (VLIZ identifier `mrgid`, one polygon — not a country):

1. **The list of official Ports of Entry (PoE) for recreational craft.**
   For each port: official name, city if known, coordinates, belonging EEZ, source URLs, confidence score, and where the signal comes from (extraction, OSM, listing, run, judge).

2. **The official sources that list these ports.**
   For each EEZ: the State page or pages that are actually useful (decree, gazette, customs list, SCT catalogue, etc.), not a forum, not a blog, not Noonsite.

An EEZ without a physical port is not a failure: we qualify it in law (UNCLOS: uninhabited island, overlapping claim, joint regime, entry via the sovereign State, Antarctica).

---

## 4. What we do not want

- **Commercial or industrial ports** presented as recreational PoE (container terminals, tankers, ore terminals, industrial fishing), unless the State designates them **also** for foreign recreational vessels.
- All the world’s **OSM marinas**. A marina is a PoE only if an official source (or an equivalent bundle) says one can do clearance there.
- **Airports**, post offices, land border posts, ports of another country cited by comparison.
- A map that **overwrites** the current database (`poe_ports`) by an automatic crawl, or that **erases** a prior step (rebuild `seeds/build` after enrich).
- Republishing Noonsite, or treating it as official truth.
- Inventing a port name absent from the extracts.

---

## 5. Vocabulary

| Word | Meaning here |
|------|--------------|
| **EEZ** | Maritime polygon of a State or territory, Marine Regions / VLIZ v12 reference (~285 zones). Key: `mrgid`. A State may have **several** polygons (hexagone, overseas, joint regime, claim): **one sheet per polygon**, never a country aggregate. |
| **PoE** | Port of Entry: **designated** place where a foreign **recreational** vessel can complete its formalities. |
| **Recreational harbour** | Infrastructure that hosts yachts (marina, mixed port, haven with recreational formalities). This is not necessarily a PoE. |
| **Commercial / industrial port** | Freight, industrial fishing, military infrastructure. Useful as a **counter-list**, not as a Formalities map deliverable. |
| **Official source** | Page or PDF of a State domain (customs, gazette, port authority, ministry). Each PoE must cite at least one `ref_url` / `source_urls`. |
| **Seed** | Already-known candidate (name ± coordinates), **not yet** promoted onto a published map. |
| **Run** | Versioned generation in a separate space (`poe_run_*`), without touching the v1 map. |
| **Step** | Comparable snapshot of a PoE mapping (v1, a run, the union, an enrich batch, a listing-control). We **keep** them all. |
| **v1 map** | Currently displayable `poe_ports` collection. Training treasure. No purge. **Not** a definitive publication until §12 is reached. |
| **Published map** | The one we assume as public Formalities, only after §12. |
| **`sources_td` / `sources_bu`** | State URLs of an EEZ according to the arm (country vs known port). Presence in both = gold source. |
| **Noonsite listing** | Community inventory (harvest 2026-09-05): signal and **confidence threshold** to dare publish, not Gold Dataset. |
| **Gold Dataset** | Human review + official source. It does not exist yet. |

---

## 6. The two strategies

We do not choose one or the other. We **make them work together**.

Top-Down answers: *“This VLIZ polygon (`mrgid`), which official list does it publish?”*

Bottom-Up answers **two things**: *“This already-known place, is it really a recreational PoE?”* and, if the State page opened for this place is a **catalogue**, *“which other ports does this page designate?”*

### 6.1 Top-Down — from the EEZ to the list

We start from the **VLIZ polygon**, not from the aggregated country, and not from a port name.

1. Take **this** VLIZ polygon (territory name, polygon ISO2, sovereign, geometry, `mrgid`).
2. Build the **whitelist** of State domains of **this** polygon: for a polygon curated in `territories.json`, whoever manages and publishes its ports of entry (`official_domains` + `whitelist_mrgid`), plus the sovereign's national labels only when the record sets `sovereign_runs_entry` (Mayotte yes, New Caledonia no); otherwise the ISO2 of the polygon, else the sovereign's. No automatic inheritance (`docs/TERRITORY-WHITELIST.md`).
3. **Search** with the name of the **polygon** (`search_polygon_name`: `France hexagone` ≠ `Mayotte` ≠ `Saba`), not only the sovereign. Multilingual queries, SearXNG and/or TinyFish Search (`location` = polygon ISO2), official net, grounded search last.
4. **Download** the pages (HTTP, Readability, Chromium render, out-of-process PDF). Discard anti-bot interstitials.
5. **Extract** the names (table/decree parser first; otherwise LLM ∥ NER; Claude as second reader if the budget is open). A **national** decree may list several polygons: we read the entire list.
6. **Geocode** (Nominatim ∥ GeoNames), check that the point is in **this** polygon (or land edge, framed river exception). A GPS in **another** polygon of the same sovereign (Dzaoudzi in Mayotte while we are treating the hexagone) is **not written** on this sheet.
7. **Store** under the `mrgid` of this polygon, without destroying the existing, with the URLs used.

Business question: *which ports does this VLIZ polygon designate for the entry of foreign recreational vessels?*
Example: `France (hexagone)` (5677) and `France (Mayotte)` (48944) are **two** sheets, **two** searches, **two** lists. Valid for every sovereign with several zones (UK, NL, US, Spain…).

Top-Down remains the way to **discover an official list without any seed**. Bottom-Up can **also** discover a list, by opening the State page of an already-known port. The 12-EEZ canaries (September 2026) showed too many law fragments, too little listing overlap, and EEZs with zero ports while the v1 map had some. Hence the pivot: both arms **in parallel**, stop at the first genuine list.

### 6.2 Bottom-Up — from the already-known place to the proof

We start from the **candidate ports already found**. For each we ask: *is THIS place a recreational Port of Entry?* and *is the State page opened for this place a list to harvest?*

Seeds come from the union (deduplicated, not an exclusive intersection):

- the **v1 map** (`poe_ports`);
- **world runs** already done (`poe_run_ports`);
- the **Noonsite listing** (names, often without GPS);
- **OpenStreetMap** (havens, customs, sometimes marinas near a control);
- later the **World Port Index** (commercial / industrial ports, to contrast).

A seed seen only in OSM, only in Noonsite, or only in v1 **stays** in the union. VLIZ serves to know **which EEZ** the point belongs to, no longer to launch a crawl of the whole zone.

An already-known name is **SERP bait**, not only a file to stamp. Search “Fort Bay, Saba, clearance” and land on a State PDF that lists **all** designated ports: we do not answer only yes/no for Fort Bay.

Then, for each seed still without an official list for its EEZ:

1. geocode the name if needed;
2. TinyFish Search on **this name** + country whitelist (`noonsite.com` excluded);
3. Fetch of the State pages found;
4. **double reading of the page** (same URL):
   - catalogue parser / `looks_like_port_catalog`: if it is a list, extract **the entire** list (same tools as Top-Down) and put the URL in `sources_bu` of **this** EEZ;
   - otherwise (or in addition, for the bait name): a **judge** (Claude Haiku, Sonnet if the listing or the doubt requires it, otherwise OpenRouter) answers yes / no / insufficient **for this place**;
5. TinyFish Agent only if the official page is blocked.

The judge does not receive the Noonsite badge or the OSM tags: we avoid confirmation bias. Noonsite and OSM serve **afterwards**, for the score. It sees only the name + the State extracts.

Today `judge_one` / `execute_enrich` **throw away the rest of the page**. `remember_seed_urls` exists in Top-Down, **not** yet in Bottom-Up enrichment. That is the code gap of this contract.

### 6.3 How the two overlap

| | Top-Down | Bottom-Up |
|---|---|---|
| Starting point | a VLIZ polygon (`mrgid`) | a candidate place **of this polygon** |
| Question | which official list does **this** EEZ publish? | is this State page, for this place, a **list** — and is this place a recreational PoE? |
| Main product | State URLs + extracted names → `sources_td` | URL + entire list if catalogue → `sources_bu`; otherwise verdict per seed |
| Current weakness | noise, badly read catalogues, canaries at 10% | the yes/no judge **throws away the rest of the page** (Fort Bay / Saba) |
| Strength | finds a decree without any seed | capitalises already-paid stock; a known port often opens the catalogue |

We do not choose an arm. **Top-Down seeks the list via the VLIZ polygon; Bottom-Up seeks the list via an already-known port of this polygon; both write into the same `mrgid` sheet; the first genuine official list wins; the other arm only does the remainder.** A national catalogue (e.g. `douane.gouv.fr`) does **not** authorise merging hexagone and overseas: we attach each GPS to the polygon that contains it.

#### Parallel pipeline per EEZ (contract)

Both arms **start at the same time**. They share a bus:

| Bus | Content |
|-----|---------|
| `sources_td` | State URLs found by searching **this polygon** |
| `sources_bu` | State URLs found by searching an already-known port **of this polygon** |
| `ports` | extracted names attached to **this** `mrgid` (a Mayotte GPS does not enter the hexagone sheet) |

Bus rules:

- a URL seen by **both** arms is a **gold source** (same page, two paths);
- we put the URL on the EEZ sheet as soon as it lists ports, even if the judge has not yet decided each name;
- `remember_seed_urls` applies to **both** arms (today: Top-Down only).

**Stop at the first genuine list.** A list is “genuine” / usable when the parser says it is a catalogue, not a law fragment — same criterion as `catalog_is_sufficient` / `looks_like_port_catalog`:

- ≥ 3 ports with lat/lon **written in the text**, **or**
- `looks_like_port_catalog` + at least one coordinated port, **or**
- decree / PDF / table that designates several ports of entry (catalogue parser, not a count of “port of X” turns of phrase).

As soon as one arm has this list: we **stop** seeking other lists for this EEZ. We do not stop the work: we move to the **remainder**.

**Judge only on the residue.** The residue = seeds of this EEZ whose name is **not** in the official list (normalised spelling / `dedup_key`). For those only: yes / no / insufficient. We do not relaunch a list crawl, we do not ask the judge again for ports already named by the decree.

Bundles D and P (section 19) apply **afterwards**: the official list feeds D; P filters recreational use. A D port without a recreational clue does **not** go as-is onto the Formalities map. A P port without a State page remains a seed.

Queries: SearXNG **and** TinyFish; **language of the polygon + French + English**; TinyFish `location` = polygon ISO2.

---

## 7. Rules

### 7.1 A PoE of this product is a designated recreational port

Golden rule: **we publish on the Formalities map only places where a foreign recreational vessel can, according to a State source, complete its entry.**

Consequences:

- Container terminal, tanker, ore terminal, yard, military port: **no**, unless explicit recreational / yacht / recreo / turística mention.
- OSM marina without regulatory text: **no**.
- Mixed port (commerce + recreational, capitanía, yacht clearance): **yes**.
- State list “recreational only” (e.g. French customs PDF of attached recreational harbours): **yes**, this is the cleanest case.
- General State list with an activity field (e.g. Mexico SCT): keep only **Turística** (and mixed that includes tourism). Commerce / fishing / *sin actividad*: off the Formalities map.
- Single State list, without distinction: we **extract** (the word “recreational” is not required to *read* the list), then we **filter** with recreational OSM, Noonsite listing, absence of commerce-only WPI, and the judge.

### 7.2 Sources

- Extract **only** from official pages. Each entry cites its source.
- `noonsite.com` is **blacklisted** for the crawl and never enters `source_urls`.
- A Noonsite match **increases** confidence. Absence of Noonsite **refutes nothing**.
- OSM, WPI, GeoNames, Nominatim **prove a place**, not the legal status “recreational port of entry”.
- Never invent a name outside the extracts. Ceiling 150 ports per zone. Official spelling kept.

### 7.3 Geography

The point must be in **this** EEZ, or on its edge:

- in the polygon, or at ≤ 2.2 km (quay / sliver);
- on land, ≤ 15 km from the coastline of this EEZ;
- river exception: up to 400 km, **in this country** (ISO2 of the zone, not the sovereign), and only if it is a **real port** (not an inland city).
- At sea outside the EEZ, too far, or GPS hallucination: rejected.
- We do **not** use `snap_to_ocean` for PoE (Projects-mode patch).

### 7.4 Map, runs and memory of steps

- `poe_ports`: **no purge**, non-destructive upsert. Already-computed OSM / anomaly fields are kept.
- A from-scratch run writes into `poe_run_*`, never into the map.
- The seeds workshop writes into `poe_seed_ports` (or `poe_run_ports` of the union run). **Forbidden**: `POST /api/poe/seeds/build` after an enrichment — delete+insert, it erases already-paid Claude judgments.
- Map promotion = **manual**, port by port or EEZ by EEZ, after review. We do **not** publish until we are confident enough (§12 criteria).
- **It does not matter which map is displayed in the meantime.** The current display is not a product decision. We will publish a Formalities map when it is confident enough.
- **What matters is to keep in memory the results of each step** of PoE mapping, under a stable identifier, to **compare** them (listing-control, diffs, confirmed / listing_only / run_only counts). We do not overwrite one step to make another.
- Forbidden automatically: `generate-batch` without a limit, `force` on the map, `/api/deploy clear_db=true`.

### 7.5 EEZ without PoE

Qualify, do not invent: `sovereign_entry`, `uninhabited`, `overlapping_claim`, `joint_regime`, `antarctic`. Clear the qualification as soon as the zone has ports.

---

## 8. Sources and tools

### 8.1 What feeds the lists (evidence)

| Tool | Role | What it is not |
|------|------|----------------|
| **State sites** (customs, gazette, decree, port authority) | Only **evidence** of PoE status | — |
| **SearXNG** | Find the URLs. Local instance first, public as backup | Not an extractor |
| **TinyFish Search** | Targeted search (EEZ or seed name), `include_domains` net | Not a truth |
| **TinyFish Fetch** | Page mirror (often in parallel with Jina / HTTP) | Not for navigating a JS site |
| **TinyFish Agent** | 1 official URL **already known** if anti-bot, 2 in parallel, credit cap | Not a world crawl |
| **OpenRouter** | Grounded search `:online`, extraction, fallback judge | Does not invent a port |
| **Claude** (Haiku, sometimes Sonnet) | Second reader (Top-Down); **yes/no judge** on the Bottom-Up **residue**; **not** a list extractor once a catalogue is read by the parser. Local budget, stop at 90% | Not a search engine. Off if `CLAUDE_BUDGET_USD` = 0. Sees neither Noonsite badge nor OSM tags |
| **Playwright / Chromium** | Local render of JS pages, free | Skipped on a hard challenge |
| **PyMuPDF** (subprocess) | Text of official PDFs, timeout, SHA-256 cache | Crashed the worker if it stayed in-process |
| **pdfminer.six** (in process, `read_pdf`) | Fallback text when the PyMuPDF worker fails; caps 30 MB / 180 pages; a PDF with no text layer is logged and flagged `pdf_scanned` (Nemotron VL candidate) | Pure Python, no native crash |

### 8.2 What feeds the seeds and the control (signals)

| Tool | Role | Caution |
|------|------|---------|
| **VLIZ Marine Regions v12** | EEZ frame, key `mrgid` | Do not make it a blind crawl engine |
| **Previous runs** | Already-paid versioned stock (v1, v2, tinyfish, best-of) | A lot of noise: to verify, not to republish as-is |
| **v1 map** | ~1,170–1,280 geocoded PoE, training set | Never overwrite it |
| **Noonsite listing** | 196 countries, **1,193** PoE, **1,092** other ports (harvest 2026-09-05). Roles `poe` / `other`. Control and name seed | Not Gold. No Noonsite URL as source. Unmatched `other` (marinas) do not go into “listing only” review |
| **OpenStreetMap** | Place, marina, haven, customs, `port_of_entry`. Overpass + Taginfo | ~32,000 `leisure=marina`: too many to take all. A marina + nearby customs = **candidate**, not a PoE |
| **World Port Index (WPI)** | NGA inventory of **commercial and industrial ports** (~3,800, US public domain) | **Wired**: ingest `wpi_ports.json` + `wpi_commercial` token. Never evidence of yacht clearance. WPI “First Port of Entry” is ignored. |
| **UN/LOCODE** | Place code + port function | Same logic as WPI: existence, not recreational status |
| **Nominatim** | OSM geocoding, ~1 req/s, Mongo cache | May point to a city, not the quay |
| **GeoNames** | Second geocoder, hourly quota | Agreement < 2 km = good signal |
| **spaCy NER** | Port names in the text, in parallel with the LLM | Does not read the law |
| **SERP classifier** | Sorts URLs before download | A State domain is never discarded for a low score |

### 8.3 What is deliberately excluded from extraction

Blacklist `backend/data/territories.json`: Noonsite, skipper forums, Wikipedia, TripAdvisor, sailing magazines, etc. We can **compare names** to Noonsite; we do not **download** Noonsite in the pipeline.

---

## 9. The code — where each piece lives

The backend is in `backend/app/`. `backend/server.py` only loads the application.

### 9.1 Top-Down (pipeline per EEZ)

File: `backend/app/services/poe_pipeline.py`

| Function | Role |
|----------|------|
| `build_referential` | Loads the ~285 VLIZ EEZ (WFS), simplifies, writes the map |
| `build_whitelist` / `url_allowed` | State domains of the country |
| `localized_query` / `search_hint_queries` / `search_polygon_name` | Queries in the language of the VLIZ polygon (Mayotte ≠ France hexagone) |
| `search_searxng` / `search_grounded` | URL search |
| `rank_candidates_ml` | SERP sort before fetch |
| `_find_sources` | SearXNG ∥ TinyFish, official net, customs retry |
| `_collect_texts` / `fetch_and_parse` | N1/N2/N3 cascade |
| `extract_ports_llm` | Local catalogue, then LLM ∥ NER, Claude second reader |
| `_extract_and_geocode` | Names → GPS → EEZ filter |
| `_persist_zone` | Non-destructive upsert into `poe_ports` |
| `_persist_zone_run` | Write into a run’s space |
| `generate_zone_poe` | Orchestrator of one EEZ |
| `qualify_unclos` | Legal status of EEZs without a port |

Search variants (`normalize_variant`):

- `v1`: English SearXNG, then local language if empty;
- `v2`: English SearXNG **and** local **in parallel**;
- `tinyfish` (default): SearXNG ∥ TinyFish Search union.

### 9.2 Versioned runs

| File | Key functions |
|------|---------------|
| `services/poe_runs.py` | `execute_run`, `new_run_id` — from scratch, resume, timeout 900 s/zone |
| `services/poe_diff.py` | `diff_run_vs_baseline` — v1 ↔ run |
| `services/poe_report.py` | `build_run_report`, `report_to_markdown` |
| `services/poe_bestof.py` | `synthesize_best_of`, `is_legal_fragment` — inter-run synthesis, **never** written into v1 |
| `services/run_fingerprint.py` | Fingerprint of the code + run parameters |
| `routers/runs.py` | API `/api/poe/runs*` |

### 9.3 Bottom-Up (seeds, judge, OSM)

| File | Key functions |
|------|---------------|
| `services/poe_seeds.py` | `union_extracted`, `attach_listing_seeds`, `attach_osm_seeds`, `attach_wpi_commercial`, `verdict_for_seed`, `build_seed_union`, `persist_verify_run` |
| `services/poe_zone_fiche.py` | EEZ review sheet (`sources_td` / `sources_bu` + PoE), read-only |
| `services/poe_seed_enrich.py` | `geocode_one`, `judge_one`, `execute_enrich`, `apply_judge_verdict`. **Gap**: `judge_one` does not yet launch the catalogue parser nor `remember_seed_urls` |
| `services/osm_seeds.py` | `is_marina_only`, `is_seed_candidate`, `refresh_osm_cache`, `osm_inventory` |
| `services/wpi_ports.py` | ingest Pub 150, `match_wpi_port`, `wpi_commercial` (counter-list, not evidence) |
| `services/osm_validate.py` | `overpass_around`, `score_confidence`, `validate_ports` — osm_confidence **without** changing name/GPS |
| `services/listing_ref.py` | `project_listing` — Noonsite slug → mrgid |
| `services/listing_control.py` | `compare_to_listing`, `persist_review` — review queue |
| `services/poe_confidence.py` | `score_port` (0–100: source, reading, map, external) |

Seed verdicts (`verdict_for_seed`):

- `confirmed` — listing ∩ (v1 or run or OSM) + coordinates;
- `probable` — strong OSM, or ≥ 2 extracted sources, or listing ∩ extract without GPS;
- `unverified` — a single extracted source: to judge;
- `name_only` — listing without a point: geocode first.

### 9.4 Shared core

| File | Functions useful to PoE |
|------|-------------------------|
| `core/extract.py` | `serp_filter`, `extract_cascade`, `looks_blocked`, `extract_structured_ports`, `catalog_is_sufficient` |
| `core/geo.py` | `geocode_port_dual`, `classify_poe_point`, `harbour_evidence`, `inland_exception_flags` |
| `core/llm.py` | `extract_ports`, `grounded_search`, `ask_json` |
| `core/claude.py` | JSON extraction (rules + few-shots), prefix cache, budget |
| `core/tinyfish.py` | `tf_search_pages`, `tf_fetch`, `tf_poe_agent` |
| `core/dedup.py` | `merge_docs`, `find_duplicate_in_list`, `normalize_name` |
| `core/rag.py` | `select_list_context`, `content_changed` |
| `core/ml.py` | `predict_serp`, `extract_entities`, `scan_poe_anomalies` |
| `core/pdf_worker.py` | Isolated PDF parse (P0) |

### 9.5 Embedded data

| File | Content |
|------|---------|
| `data/listing_control/all_countries.json` | Community PoE / other listing |
| `data/listing_control/slug_overrides.json` | Slug → mrgid join |
| `data/poe_exceptions.json` | Memorised State domains and URLs |
| `data/territories.json` | Curated France / overseas reference + blacklist |
| `data/curated_marinas.json` | **Route** marinas (Marinas mode, another subject) |

### 9.6 Formalities API (reminder)

- `GET /api/poe/zones/{mrgid}` — review sheet (PoE + `sources_td` + `sources_bu`, read-only)
- `POST /api/poe/zones/{mrgid}/generate` · `POST /api/poe/generate-batch` — **410** (withdrawn, no longer overwrite `poe_ports`)
- `POST /api/poe/runs` · `/multi` · `/best-of` — isolated runs
- `GET /api/poe/seeds/union` · `POST /api/poe/seeds/verify` · `POST /api/poe/seeds/enrich`
- `GET /api/poe/seeds/osm` · `POST /api/poe/seeds/osm/refresh`
- `POST /api/poe/validate-osm` · `POST /api/poe/qualify-unclos`
- `GET /api/poe/runs/{id}/listing-control`

The **Marinas** mode (`marina_world.py`, world dump `leisure=marina`) is **not** the Formalities mode. The two cross only as a signal: an OSM marina near a customs may become a **seed**.

---

## 10. Confidence score

This is not official truth. It is a bundle, 0–100:

| Piece | Max points | Idea |
|-------|------------|------|
| Source | 30 | State domain / gazette vs web synthesis |
| Reading | 25 | Catalogue, LLM ∩ NER agreement, Claude second reader |
| Map | 25 | In the EEZ, geocoders agree |
| External | 20 | Noonsite listing `poe`, OSM, several runs |

A port seen only in a web synthesis stays low. A decree + two agreeing GPS + OSM + listing rises.

Listing control (outside extraction):

- listing PoE ∩ run → `confident` (signal);
- listing `other` ∩ run PoE → `contradiction` (review);
- run only / listing PoE only / ambiguous name → review.

---

## 11. Hard constraints

1. **The data in the database are a treasure.** 4,463 projects + 1,171+ PoE **and each PoE mapping step**: no purge, no destructive rebuild after enrichment.
2. **Official sources only** for legal status.
3. **PoE = designated recreational**, not “any World Port Index port”.
4. **Claude is a scalpel**, not the engine: budget, cache, stop at 90%, OpenRouter fallback.
5. **P0 “survive”**: out-of-process PDF, Mongo geocode cache, lock on `poe_exceptions.json`. Money is no longer the main risk; crashes and hangs were.
6. **No Generate / generate-batch** on the map or in Audit: these routes answer **410**. Isolated runs and seed enrichment only.
7. Map legal notice: data are **indicative**.

---

## 12. Acceptance criteria

We consider the work successful for an EEZ when:

1. We have identified **the official source or sources** that list the points of entry (stable URL, State domain, in `sources_td` and/or `sources_bu`), **or** we have justified the absence (UNCLOS).
2. All **recreational** PoE of this list are in the current mapping step (name + GPS in the right EEZ + `source_urls`).
3. **Commerce-only** ports (WPI / commercial activity without recreational) are not there.
4. Marinas without formalities are not there.
5. A skipper (or a reviewer) can open **one EEZ sheet**: PoE list + clickable TD URLs + clickable BU URLs + score.
6. The v1 map has not been overwritten by a batch. Prior steps are still comparable.

### Formalities map “confident enough” for publication

We **display** what we want in the meantime. We **publish** (replace or overlay `poe_ports` as the public Formalities map) only when **both** hold:

1. **Listing coverage** — the candidate map includes **the overwhelming majority** of Noonsite listing PoE (`listing-control` control: high `coverage`, residual and assumed `listing_only` queue). Absence of Noonsite does not force a hole: the listing is not Gold; it is the confidence threshold chosen to dare publish.
2. **Manual reread** — the UI allows rereading **one official list per EEZ** (source sheet: decree / gazette / catalogue, not only points). Without this sheet, we do not publish: we cannot judge a map.

Until these two criteria are reached: we compare the steps (v1, runs, union, confirmed, future batches); we do not “choose” a displayed map as truth.

---

## 13. Current state and gaps

### Already in place

- Complete Top-Down pipeline (search, whitelist, cascade, extraction, geocoding, score, UNCLOS).
- Isolated runs, comparison, best-of, code fingerprint, P0 anti-crash.
- Noonsite listing-control (passive judge).
- Bottom-Up seed union (v1 + 5 world runs + OSM + listing).
- Batch enrichment (geocode `name_only`, judge `unverified`) **without** writing `poe_ports`. **Without** yet extracting the catalogue from the judge’s page.
- A posteriori OSM validation of v1 PoE.

World runs already in the database (to reuse, not to redo as a crawl):

- `20260905-201122-91f6da` (best-of)
- `20260905-084036-fe1e08` (v2)
- `20260905-084036-5ecfa7` (tinyfish)
- `20260904-073236-8748b2`
- `20260829-003645-d7ab2e`

Seeds run (0 crawl): `20260906-071347-6a9509`.

Top-Down 12-EEZ canaries: quality **NO-GO** (noise, listing ~10%, Venezuela at 0). Do not relaunch a world Top-Down “to see”.

### Gaps versus this specification

| Gap | Detail |
|-----|--------|
| **Bottom-Up = yes/no only** | `judge_one` answers for **one** name and **throws away the rest** of the State page. Word comments #3 / #20: BU **can** discover an unknown list. Contract §6.3 not yet in the code. |
| **`remember_seed_urls` TD only** | Productive BU URLs are not memorised for the other EEZs of the same country. |
| **WPI absent from the code** | **Addressed (ingest + token, no rebuild).** Snapshot `backend/data/wpi_ports.json` (~3,805 ports). Name match + ≤ 1 km → `wpi_commercial`. 0 new seed, 0 `seed_sources`, 0 judge/score/verdict evidence. **Do not** `POST /api/poe/seeds/build` (4034 / 1280 / 781 frozen). |
| **OSM marinas excluded from seeds** | **Addressed (code + tests, no rebuild).** `leisure=marina` / CATHAF marina* = P seed if customs / `border_control` / `port_of_entry` at ≤ 800 m (`around.ctrl`, `osm_role=marina_pleasure`). Far from a control: still excluded. **Do not** `POST /api/poe/seeds/build` nor Overpass refresh until a rebuild is decided (4034 / 1280 / 781 frozen). |
| **Judge too “designated port”** | **Addressed (prompt + parse, not WPI as evidence).** `JUDGE_SYSTEM` and TinyFish require **recreational or mixed** for `is_poe=true`; `kind=cargo` → `rejected`. A marina with official clearance is no longer an automatic false. WPI counter-list: token only. |
| **Top-Down still noisy** | Useful to discover **official URLs per EEZ** (2nd deliverable), not to fill the map in one go. |
| **EEZ sheet absent from the UI** | **Addressed (read-only, no rebuild).** `GET /api/poe/zones/{mrgid}` + Formalities banner + popup: **1 TD URL** (State page/PDF listing the PoE), **PoE list**, **1 BU URL per port**. One sheet = one VLIZ polygon (`France (hexagone)` ≠ `France (Mayotte)`). No Generate button (`POST …/generate` = 410). Noonsite off the sheet. **Do not** `POST /api/poe/seeds/build`. **Do not** launch a world run before the NVIDIA models. |
| **SERP search too “country”** | **Addressed (code, no crawl).** `_find_sources` / hints / TinyFish `location` target the **polygon** (`search_polygon_name`, ISO2 `YT` for Mayotte). A port geocoded outside this polygon is no longer written on this sheet. **This does not relaunch a world Top-Down.** |
| **Manual promotion** | No D/P → map review UI. The EEZ sheet does not write `poe_ports`. |
| **Gold Dataset** | Does not exist. The listing is not one. |

---

## 14. Recommended work order

1. **Freeze this contract** (this document). Then wire the double reading into `judge_one` / `execute_enrich` (`looks_like_port_catalog` + parser + `sources_bu` + `remember_seed_urls`).
2. Keep the seed union and already-paid judgments; **do not** relaunch a world Top-Down crawl **now**; **do not** `POST /api/poe/seeds/build`. France’s 23 polygons (and the 47 sovereigns with several EEZs) are treated **one `mrgid` at a time**. A **future** world TD∥BU run, if decided after the NVIDIA models, is a **new `run_id`** in `poe_run_*`: one pass per polygon, without touching `poe_ports` or runs already in the database. If the stock has the wrong `mrgid`, **re-zone** by VLIZ point-in-polygon (0 crawl) before any crawl.
3. Make TD and BU work **in parallel per EEZ**; stop list search at the first genuine list; judge the residue only.
4. **Remember** each step (v1, runs, union, confirmed, listing-control, future batches) to compare them. Map display is not a publication.
5. Finish Bottom-Up enrichment by batches (names without GPS, then unverified) **with** catalogue harvest.
6. Wire **WPI** (and possibly UN/LOCODE) as a “commerce/industrial” signal.
7. Treat **OSM marinas near a customs** as P seeds, not as PoE.
8. Recalibrate the judge and the SCT-like filter (bundles D and P) **on the residue**.
9. For each EEZ, **remember the official URL** as soon as it is proven (`sources_td` / `sources_bu`, `poe_exceptions.json`).
10. EEZ sheet UI (list + clickable TD/BU sources) + human review. **Publish** `poe_ports` only when listing coverage and this sheet hold (§12).

---

## 15. Documents and conversations this specification inherits from

- `docs/archives/PRD.md` — original problem, non-destructiveness constraint, 2026-08 history.
- `docs/ARCHITECTURE.md` — code layout after refactor.
- `README.md` — the three modes (Projects, Marinas, Formalities).
- Earlier design decisions: Top-Down pipeline, Claude strategy, Noonsite harvest, P0, listing-control, NO-GO canaries, Bottom-Up pivot, seed union, OSM Taginfo inventory, Word comments on the specification (#3 / #20: BU also discovers lists).

This specification **prevails** over implementation details as soon as there is a conflict (e.g. “all marinas” vs “marinas = seeds only”; “State cargo list” vs “recreational map”; “yes/no judge only” vs “BU list detector”).

---

## 16. Who does what

| Actor | What they do | What they do not |
|-------|--------------|------------------|
| **Skipper** (public map) | Consults EEZs, PoE, sources, score. Always checks with the authorities before leaving. | Does not launch a generation. Does not vote yet (crowdsourcing = backlog). |
| **Operator** (Console) | Builds the EEZ reference, launches an isolated run, relaunches seed enrichment, consults diffs and listing-control. | Does not overwrite `poe_ports` (`generate-batch` / Generate = 410). |
| **Automatic judge** (Claude / OpenRouter) | On the **residue**: says whether **this place** is a recreational PoE according to official extracts. Does not read the catalogue in place of the parser. | Does not invent a name. Does not see the Noonsite badge or OSM tags (anti-bias). Does not throw away an official list after a yes/no. |
| **Human reviewer** | Decides D/P mismatches, listing `contradiction`, `unverified`. Promotes a port to the map. | Does not “goldise” Noonsite. |
| **Pipeline** | Searches **in parallel** (TD ∥ BU), downloads, extracts catalogues, geocodes, unions, records each step. | Never purges the v1 map. Does not overwrite a prior step. |

---

## 17. Lifecycle of a port / an EEZ

A place is not born a PoE. It crosses states. **The EEZ also has a cycle**: we seek a list there, not only points.

### 17.1 Cycle of an EEZ (both arms)

```
EEZ
 ├── TD arm (country) ──► sources_td ──┐
 │                                     ├── shared bus
 └── BU arm (bait seed) ──► sources_bu ──┘
         │
         ├─ page = catalogue  → extract the ENTIRE list, remember the URL
         └─ page ≠ catalogue  → judge yes / no / insufficient for THIS name
                    │
                    ▼
         as soon as a genuine list exists: STOP list search
                    │
                    ▼
         judge / D×P only on the RESIDUE (seeds off the list)
                    │
                    ▼
         versioned mapping step (comparable, never overwritten)
                    │
                    ▼
         map publication only if §12 (listing + EEZ sheet)
```

Saba example: the Fort Bay seed opens the Main Ports / decree PDF. We **keep** Fort Bay **and** the other designated ports of the page. We do not stamp Fort Bay to throw away Cove Bay or the rest.

### 17.2 Cycle of a seed

```
candidate (seed)
    → geocoded in the right EEZ
        → State page opened for this name
            → if catalogue: the seed joins the extracted list (no longer an isolated verdict)
            → otherwise: judged on official extracts (yes / no / insufficient)
                → cross-checked (listing / OSM / WPI / multi-run)
                    → reviewed if doubt
                        → promoted manually **only** onto a map confident enough
                            → OSM revalidated (osm_confidence)
                                → refreshed only if the State page has changed (MD5, 30 days)
```

| State | Meaning | Where it lives |
|-------|---------|----------------|
| `name_only` | A name (often Noonsite), no GPS | `poe_seed_ports` / `poe_run_ports` |
| `unverified` | A point, a single extracted source | same |
| `probable` | Several signals, not yet State evidence + listing | same |
| `confirmed` | Listing ∩ extract + GPS — **strong signal, not yet the published map** | same |
| `accepted` / `rejected` / `inconclusive` | Judge verdict on **this** name (residue only) | `judge_*` fields |
| **step** | Comparable snapshot (v1, a run, the union, an enrich batch, a listing-control) | `poe_runs` / `poe_seed_ports` / listing files |
| **on the published map** | Promoted into `poe_ports` **after** §12 | Formalities mode |
| `stale` | Source not reviewed for 180 days (display); auto-refresh at 30 days | `eez_zones` |

A judge `rejected` **stays `unverified`**. We do not promote it, we do not destroy it.

Deduplication: same EEZ + close name (fuzzy) or points less than 500 m apart. We merge, we do not duplicate “Port of X” and “X”.

The `confirmed` already obtained (workshop 2026-09-06/07) are a **step** to keep and to compare to v1 and to the listing. That is not, in itself, the map to publish.

---

## 18. The second deliverable: official sources per EEZ

The skipper must not only see points. They must see **the State page** of this polygon. This is also the UI publication criterion (§12): **one official list per EEZ** for the manual reread.

### Review sheet (UI contract)

For each of the ~285 VLIZ polygons, one sheet, **without** a Generate button:

| Element | Meaning |
|---------|---------|
| **1 Top-Down URL** | The State page or PDF **supposed to list the PoE** of **this** polygon (decree, gazette, customs catalogue). Projects gesture: a domain name + external link. |
| **PoE list** | Ports attached to this `mrgid` (not the country aggregate). |
| **1 Bottom-Up URL per PoE** | The State page opened by searching **this** port name. Not a pile of BU URLs at zone level. |

If several TD pages exist in the database, the review **displays only one** (PDF / “list” page first). The others stay stored. Noonsite, wiki, forums: off the sheet. `douane.gouv.fr` home without a list: not enough. Uninhabited / claimed EEZ without a source: `kind = none` + UNCLOS code.

Fields kept backstage (`sources_td` / `sources_bu` / `from_arm`): a URL seen by both arms = **gold source**. Top-Down **fills the sheet’s TD URL**. Bottom-Up **fills each port’s URL**. Once a genuine list is placed, this polygon’s `include_domains` net starts from this sheet.

---

## 19. Algorithm of the two bundles (D and P)

We do not choose between “any State list” and “recreational only”. We compute both, then we decide.

### Bundle D — designated by the State

Input: pages of the EEZ source sheet.

Output: every place the text names as port of entry / clearance / puerto habilitado / designated port, **including** a commercial terminal if the State writes it.

Tools: table parser, LLM + NER, Claude second reader. The word “recreational” is **not** required to *read*.

### Bundle P — recreational

Input: the same pages **plus** the recreational seeds (Noonsite listing `poe`, OSM marinas near a customs, “attached recreational harbours” PDF, SCT field *Turística*).

Output: places where a foreign yacht can do clearance.

Bundle D pages may arrive **by any arm** (`sources_td` or `sources_bu`). The word “recreational” is not required to *read*; it is required to *publish* (P).

### Decision

| D | P | Decision |
|---|---|----------|
| yes | yes | **Map PoE** — the clean case (mixed port or recreational list) |
| yes | no | **not on the Formalities map** — commercial/industrial port (WPI helps confirm it). We can keep it backstage `designated_other` |
| no | yes | **seed**, not official PoE — marina or listing without a decree. The BU arm **continues seeking an official list** (SERP bait); if the page is a catalogue, the port may move to D∩P |
| no | no | ignored |

Mexico case: the SCT catalogue has an activity field. D = all “port designated” rows. P = *Turística* rows (and mixed that contains tourism). Only P ∩ D goes on the map.

Metropolitan France case: the customs PDF of attached recreational harbours **is already P**. No need for WPI. This PDF, if it also names overseas, does **not** go into the hexagone sheet: Dzaoudzi / Mamoudzou belong to `France (Mayotte)` (48944).

---

## 20. Data model

MongoDB. We do not invent a sixth collection for each idea: we reuse.

### Map (v1) — do not overwrite

| Collection | One line = |
|------------|------------|
| `eez_zones` | one EEZ (polygon, iso2, status, `poe_count`, `sources` / target: `sources_td` + `sources_bu`, `unclos`, `confidence_avg`) |
| `poe_ports` | one PoE of the **displayable v1 map** (name, lat/lon, `mrgid`, `source_urls`, `confidence`, `osm_*`, `spatial_kind`). Become **published map** only after §12 |

Business key of a port: `dedup_key = "{mrgid}:{normalised name}"`.

Useful fields of a `poe_ports`:

- identity: `name`, `city`, `note`, `mrgid`, `zone_name`, `country_iso2`
- map: `lat`, `lon`, `validated`, `spatial_kind` (`in_eez` / `coastal_land` / `inland_river` / rejected)
- evidence: `source_urls`, `extraction_engine`, `extraction_agreement`, `claude_agreement`, `from_synthesis`
- score: `confidence`, `confidence_parts`, `confidence_reasons`, `listing_role`
- OSM: `osm_confidence`, `osm_tags`, `osm_id`, `osm_checked_at`
- anomalies: `spatial_anomaly` (IsolationForest / DBSCAN, additive flag)

### Workspace (runs / seeds / steps)

Each PoE mapping step is a **comparable object**. We keep them all.

| Step (examples) | Identifier | Role |
|-----------------|------------|------|
| v1 map | collection `poe_ports` (~1,280) | Training set, possible current display — **not** a definitive publication |
| SERP world | `20260905-201122-91f6da` (best-of), `…fe1e08` (v2), `…5ecfa7` (tinyfish), `20260904-073236-8748b2`, `20260829-003645-d7ab2e` | Noisy stock, to compare, not to republish as-is |
| 12-EEZ canaries | `20260906-041309-8fb2d2`, `20260906-063439-2ccadf` | Top-down NO-GO proof |
| Seed union | `20260906-071347-6a9509` | 0 crawl, inventory verdicts |
| Seeds workshop | `poe_seed_ports` (build 2026-09-06, review 2026-09-07) | ~4,034 seeds, confirmed / probable / … — **do not rebuild** |
| Noonsite listing | snapshot `2026-09-05T10:39:03Z` | Coverage control, not Gold |
| Future TD∥BU batches | new `run_id` | Catalogue harvest + residue judge |

Compare = listing-control (`coverage`, `noise`, `confident`, `listing_only`, `run_only`) + name/`dedup_key` diff between two steps. Not an overwrite.

| Collection | One line = |
|------------|------------|
| `poe_runs` | one run (label, variant, code fingerprint, progress) |
| `poe_run_zones` | result of **one** EEZ in **one** run |
| `poe_run_ports` | one extracted port or one enriched seed, tied to `run_id` |
| `poe_seed_ports` | one persisted seed of the workshop (union + judgments). `wrote_poe_ports: false` |
| `poe_run_events` | journal of a micro-step (search, fetch, judge, BU catalogue…) |
| `poe_listing_review` | review queue (contradiction, run only, listing only, ambiguous) |
| `osm_port_seeds` | named OSM object, attached to an EEZ |
| `jobs` | resume of long tasks (OSM validation, enrich) |
| `geocode_cache` | Nominatim / GeoNames, TTL 180 d / 14 d |

The seeds run `20260906-071347-6a9509` lives in `poe_run_ports`. The later workshop lives in `poe_seed_ports`. Both are steps. **Do not** `POST /api/poe/seeds/build` afterwards.

### Files beside the database

- `backend/data/listing_control/all_countries.json` — 196 countries, 1,193 PoE, 1,092 other
- `backend/data/listing_control/slug_overrides.json` — slug → mrgid
- `backend/data/listing_control/eez_index.json` — 285 EEZ for the join
- `backend/data/poe_exceptions.json` — State domains outside the `gov` pattern + productive URLs
- `backend/data/territories.json` — curated France / overseas + blacklist
- `backend/data/eez_world_map.geojson` — simplified polygons for Leaflet
- `backend/data/wpi_ports.json` — slim World Port Index (~3,805 ports, `wpi_commercial` counter-list)

---

## 21. What the user sees

### Skipper — Formalities mode

- World map of EEZs coloured by status (not yet generated / generated / without official source / error).
- Amber points = published PoE. Click: name, EEZ, score, OSM badge, spatial anomaly, up to 3 source URLs, “indicative” notice.
- Left banner: the ~285 VLIZ polygons, search, status filter; **sheet** of the selected polygon: **1 TD URL** (official list) + **PoE list** + **1 BU URL per port**.
- Polygon popup: same sheet. **No** Generate button (`POST …/generate` = 410).
- Fixed notice: *check with the authorities before departure.*
- Until §12 is reached, the operator may **change the displayed step** (v1, a run, confirmed…) without that counting as publication.

### Operator — Console

- Build the EEZ reference (VLIZ), once.
- Launch an **isolated run** (`/api/poe/runs`), not a map batch.
- Seed union, batch enrichment, OSM refresh.
- Run ↔ v1 diff, markdown report, listing-control, review queue.
- Auto-refresh: every 12 h, max 60 EEZ, re-downloads sources older than 30 days, re-extracts only if the MD5 has changed; retries errors after 7 days.

What the UI **lacks** (gap of this specification): D/P + judge + listing review screen. The Review tab and the **Gold** button are scoped in `docs/CAHIER_DES_CHARGES_REVIEW.md` v1.1: **one** sheet per polygon (ports on it), **all** TD/BU URLs of all runs deduplicated and clickable, Gold = accepted sheet **on the map**. The sentence “display only one TD” applies to the map banner, not to Review.

---

## 22. OpenStreetMap inventory

OSM has no “recreational Port of Entry” object. We assemble tags. Taginfo counts of 5 September 2026.

### Useful signals (seeds or validation)

| Tag | Order of magnitude | Usage |
|-----|-------------------|--------|
| `port_of_entry=yes` | 59 | Only tag that **says** PoE. Very rare. Strong seed. |
| `port_of_entry=no` | 411 | Negative signal, not absolute truth. |
| `government=customs` / `amenity=customs` / `office=customs` | thousands, often inland | Customs. An office ≠ a port. Useful **near** a haven / a marina. |
| `barrier=border_control` | same | Often road or airport. Filter: in or ≤ 15 km from the EEZ. |
| `industrial=port` / `landuse=harbour` / `harbour=yes` | a few thousand | Infrastructure. D seed, not recreational evidence. |
| `leisure=marina` | ~31,792 | **Recreational.** Too many to take all. P seed **only** if customs / border / `port_of_entry` at ≤ 800 m, or already in listing/v1. |
| CATHAF `marina` / `marina_no_facilities` | ~21,800 | OpenSeaMap recreational. Same rule as `leisure=marina`. |

Today the code **accepts** a marina as a P seed if a control is at ≤ 800 m (`osm_near_control`); otherwise it stays excluded. It is **not** a PoE by itself. The Atlas union is not rebuilt until a rebuild is decided.

### Not to take as PoE

- `amenity=ferry_terminal` alone (OSM score bonus +0.1, not a seed).
- Airports (filter on the name).
- Objects more than 15 km from the EEZ (Rhine, Danube, Great Lakes, dry ports).
- Mass `seamark:type=harbour` (25,070, of which 21,344 also marina).

A posteriori validation (`osm_validate.py`): around a PoE **already on the map**, 3 km radius, we note `osm_confidence` without moving the GPS. Already done on the 1,171 v1 PoE (678 ≥ 0.5; 154 without a tag).

---

## 23. World Port Index and UN/LOCODE

### WPI (NGA, US public domain, ~3,805 ports)

List of **commercial and industrial ports**: name, country, coordinates, traffic type (cargo, tanker, fishing…). Snapshot: `backend/data/wpi_ports.json`. Reload: `python scripts/ingest_wpi.py UpdatedPub150.csv`.

Usage:

1. load the WPI (slim file; VLIZ attachment is done via already-zoned seeds);
2. match by name + proximity (≤ 1 km) to seeds — **no** orphan WPI seed;
3. place a `wpi_commercial` token (inventory line, not sent to the judge);
4. if D yes and P no and WPI yes → confirm the gap **off the Formalities map** (review, not auto);
5. if P yes and WPI yes → **mixed** port, stay on the map if the State source allows it.

The WPI **never proves** that a yacht can clear customs. The *First Port of Entry* field of the NGA CSV is **ignored**. The token adds no confidence point and does not change `verify_verdict`.

### UN/LOCODE (UNECE)

Place code + function `1` = maritime port. Same role: existence / spelling / country, not recreational legal status.

---

## 24. Concrete examples

### Metropolitan France — dedicated recreational list

Source: customs PDF *“Liste des ports de plaisance rattachés au dispositif”* (`douane.gouv.fr`).  
La Rochelle, Tino Rossi, Charles Ornano, etc. This is already bundle P. We do not need WPI.  
Curated reference: `territories.json`. Arrival outside Schengen only.

### Mexico — general catalogue + activity field

Source: SCT list (numbered table, lat/lon, federal entity, activity).  
Ensenada, Manzanillo… D = all designated rows. P = *Turística* (possibly mixed).  
The catalogue parser copies the coordinates **written in the PDF**, never a GPS from memory.

### Niue — one sentence of law

“ …through the port of Alofi, the Hanan International Airport, or the Post Office. ”  
We keep **Alofi**. We discard the airport and the post office. No OSM marina required: it is the only named maritime point of entry.

### Marina without formalities

A `leisure=marina` in Croatia, without customs at 800 m, absent from the `poe` listing and from a decree: **weak seed or nothing**. It is not a PoE.

### France — 23 polygons, not a single EEZ

VLIZ v12 splits France into 23 polygons (hexagone 5677, Mayotte 48944, Réunion 8338, Guadeloupe 33177, joint regimes, uninhabited islands…). The Noonsite listing too: slug `france-2` → 5677 only; slug `mayotte` → 48944. We **never** produce a “France” sheet that mixes Marseille and Mamoudzou.

This observation does **not** authorise a world Top-Down crawl. The 12-EEZ canaries remain NO-GO. Counter-measure: `mrgid` grain in the search and the sheet; PIP re-zoning of existing stock if needed.

### Venezuela in the Top-Down canary

The 12-EEZ crawl returned **0** ports. The v1 map has 12. That is why we do not relaunch a world Top-Down: it would **overwrite** already-useful stock. We start from the 12 seeds and seek **their** decree — and if that decree is a catalogue, we take **the entire** list.

### Saba — the judge that threw away the catalogue

Searching “Fort Bay, Saba, clearance” can open a State page (or the Main Ports panel) that lists **several** ports. The contract: extract **the entire** list, put the URL in `sources_bu`, judge only what is not on it. Current code (`judge_one`) stops at the Fort Bay yes/no: that is the bug to break.

---

## 25. Acceptance tests

We do not “feel” that an EEZ is good. We tick.

### For an EEZ (unit acceptance)

1. The source sheet has at least one State URL that lists ports (`sources_td` and/or `sources_bu`), **or** a UNCLOS code.
2. If a page opened by a seed is a catalogue, **all** designated ports of the page are extracted, not only the bait name.
3. Each PoE of the step has: name, GPS in the EEZ (or framed edge / river), at least one official `source_urls`.
4. No airport, no inland city at 100 km, no port of another country.
5. No commerce-only WPI terminal without a recreational mention.
6. Popup / sheet: clickable TD and BU sources + score.
7. `poe_ports` of this EEZ has not lost a port that v1 had, except a **written** rejection (review). Prior steps remain readable.

### For a seeds run (world acceptance)

1. `wrote_poe_ports: false`.
2. Union ≥ v1 + listing + OSM cache (not an empty crawl).
3. `confirmed` / `probable` / `unverified` / `name_only` counters logged **and kept**.
4. BU catalogue: a list URL → `sources_bu` + extracted ports. Judge: `is_poe=true` only on the **residue**, **official** extracts + **recreational or mixed**.
5. No `POST /api/poe/seeds/build` after enrichment.
6. Automatic tests green: `test_poe_seeds`, `test_poe_seed_enrich`, `test_listing_control`, `test_osm_seeds`, `test_poe_v2_pipeline`, `test_p0_survive`, `test_ner_unclos_osm`.

### Forbidden during acceptance

Click Generate / `generate-batch` / `force` on the map (routes **410**). Relaunch a world Top-Down “to compare”. Rebuild the seeds. Publish `poe_ports` before §12.

Local command:

```bash
cd backend && python3 -m pytest tests/test_poe_seeds.py tests/test_listing_control.py tests/test_osm_seeds.py tests/test_p0_survive.py -q
```

---

## 26. Risks

| Risk | Effect | Counter-measure already there / to do |
|------|--------|---------------------------------------|
| Noisy Top-Down crawl | Law fragments, 0 port on a populated EEZ, listing at 10% | No more world Top-Down; Bottom-Up by seed **and** catalogue harvest |
| Country aggregate (France+Mayotte) | A “France” sheet / crawl swallows overseas | One sheet and one search **per VLIZ polygon**; PIP re-zoning, not a world run |
| BU judge that throws away the list | Fort Bay yes, rest of the page lost | Double reading: catalogue parser + residue judge (§6.3) |
| Too much commerce on the map | Skipper arrives in a container terminal | Bundle P + WPI + recalibrated judge |
| Too many marinas | 32,000 useless points | Marina = seed only near a customs / listing |
| Overwrite of v1 **or of a step** | Loss of the training set or of Claude judgments | Isolated runs, `poe_seed_ports` kept, no `seeds/build` after enrich, promotion only after §12 |
| Rebuild `seeds/build` | Delete+insert, ~$7 of judgments lost | Forbidden as long as `judge_*` verdicts exist |
| Anti-bot (Akamai, etc.) | Empty pages, false extracts (`unblock.federalregister.gov`) | `looks_blocked`, targeted TinyFish Agent, never ingest the interstitial |
| Nominatim / Overpass / TinyFish quota | 900 s hang, slow race | Mongo cache, out-of-process PDF, batches of 200 |
| Judge bias | It says yes because Noonsite or OSM said so | The judge receives only name + official extracts |
| Noonsite taken for Gold | We copy skipper errors | Listing = signal; absence ≠ refutation |
| Incomplete whitelist | Empty TinyFish net, EEZ `ia_sans_source` | `poe_exceptions.json` + PSL patterns, only countries **with** an EEZ |
| Claude off | Budget 0 despite the key | OpenRouter fallback; the run continues |
| Law / Noonsite ToS | Grey zone of the 2026-09-05 harvest | JSON already ingested; no more crawl; no URL in the sources |

---

## 27. Out of scope

This specification **does not cover**:

- the **Projects** mode (foundations swarm, 4,463 projects) — see `docs/CAHIER_DES_CHARGES_PROJETS.md`;
- the **Marinas** mode: world dump `leisure=marina` (identity `osm_id`, outside Formalities) — other product, other map layer;
- OSM **anchorages** along the route (±25 NM);
- skipper crowdsourcing with a law link (PRD P1 backlog);
- Noonsite premium subscription (refused);
- opus-mt automatic translation of queries (16-language matrix enough for now);
- automatic promotion to `poe_ports` (including “display the confirmed as if it were the map”);
- **aviation** or **land border** formalities.

The crossing “a PoE near a conservation project” is a P2 idea, not a deliverable of this document.

---

## 28. Annexes

### A. Displayed UNCLOS codes

| Code | Skipper text (idea) |
|------|---------------------|
| `sovereign_entry` | No PoE listed — clearance via the ports of the sovereign State; transit in innocent passage. |
| `uninhabited` | Uninhabited territory — no local customs; written authorisation of the sovereign. |
| `overlapping_claim` | Disputed waters — check **all** claimant States. |
| `joint_regime` | Joint regime — shared formalities. |
| `antarctic` | South of 60°S — no EEZ regime; permit via Antarctic programmes. |

Function: `qualify_unclos`. The block disappears as soon as the zone has ports.

### B. Collections and API (short reminder)

Map write: `POST /api/poe/zones/{mrgid}/generate` and `POST /api/poe/generate-batch` — **410**, no more `poe_ports` upsert by a click.  
Run write: `POST /api/poe/runs` — safe.  
Seed read: `GET /api/poe/seeds/union`.  
Judge: `POST /api/poe/seeds/enrich` — geocode + residue judge; **target**: catalogue harvest → `sources_bu`.  
OSM: `POST /api/poe/seeds/osm/refresh`, `POST /api/poe/validate-osm`.  
Control: `GET /api/poe/runs/{id}/listing-control`.  
**Do not** call `POST /api/poe/seeds/build` again after an enrichment.

### C. Runs to reuse (do not recrawl)

World: `20260905-201122-91f6da`, `20260905-084036-fe1e08`, `20260905-084036-5ecfa7`, `20260904-073236-8748b2`, `20260829-003645-d7ab2e`.  
Seeds: `20260906-071347-6a9509`.  
Workshop `poe_seed_ports`: build 2026-09-06, name_only review 2026-09-07.  
12-EEZ canaries (NO-GO): `20260906-041309-8fb2d2`, `20260906-063439-2ccadf`.  
Listing: snapshot `2026-09-05T10:39:03Z` (1,193 PoE).

### D. Relevant automatic tests

`backend/tests/test_poe_seeds.py`, `test_poe_seed_enrich.py`, `test_listing_control.py`, `test_osm_seeds.py`, `test_poe_v2_pipeline.py`, `test_poe_api.py`, `test_p0_survive.py`, `test_ner_unclos_osm.py`, `test_poe_audit_improvements.py`, `test_run_fingerprint.py`, `test_claude_cache.py`.

### E. Attribution

EEZ: Flanders Marine Institute — Marine Regions, Maritime Boundaries v12 (CC-BY 4.0).  
Marinas / geocoding: OpenStreetMap contributors (ODbL), Nominatim, Overpass, GeoNames.  
WPI: National Geospatial-Intelligence Agency, US public domain (when wired).

---

The **numbers** (2.2 km, 15 km, 400 km, 800 m, ≥ 3 ports, 90% Claude, listing coverage…) are catalogued in `backend/data/run_rules.json` with a principle and an interval — see `docs/REGLES_PARAMETRES.md`. Each run freezes the snapshot in `params.rules`.

*End of the specification. Any rule evolution (recreational, WPI, marina-seeds, TD∥BU arms, map promotion) is done first here, then in the catalogue / the code.*
