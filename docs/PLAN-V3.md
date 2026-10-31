# Blue Intelligence V3 — Plan (draft for review)

Status: draft v5, 29 Oct 2026. **Clément's instructions take precedence; where BI-V3-PLAN (28 Oct) and BI-V3-CONSIGNES (29 Oct, 10:28) differ, CONSIGNES wins** (see `divergences-plan-consignes.md`). They come from the plan and the instruction register kept by the "Soumission (Nebius x NVIDIA)" agent (`plan-soumission-v3.md`: BI-V3-PLAN of 28 Oct and BI-V3-CONSIGNES of 29 Oct, 10:28 Toronto time). V2 business specifications from Clément's Mac (`consignes-mac/`) apply where they do not conflict. Items marked **[to confirm]** are not settled by any source.

## 1. Goals (Clément, 28 Oct)
a. Launch a new **full run from scratch** for the **projects, ports of entry, marinas and harbour offices** layers, **without losing any data** from earlier runs held in the databases.
b. Show the new runs **on the maps**.
c. Use the **V1 and V2 tools, plus Nemotron, plus Meterless (Nightshift, H-MEM) and Tavily**.
d. Get **more points on the maps, better certified and better georeferenced**.
e. Submit a **sample** of the new runs' data to **Toloka**.
f. All of this becomes Blue Intelligence V3. Then (g) assess LangSmith and Tandem, for BI, NAVIGUIDE or both.

## 2. Repository (state on 29 Oct, verified by the Soumission agent through the GitHub API)
**Non-negotiable rules**
- **V3 starts from an as-is copy of the existing Blue Intelligence repository, full history included. It is never a rewrite** (this supersedes the 28 Oct plan's "clean tree copy without history").
- **`Blue-Intelligence-Map` is not integrated into V3.**
- **The current repository `Berry-Mappemonde/Blue-Intelligence` is never modified** (it is under judging).
- **No merge into `main` without Clément's visual review** of the application.
- **Toloka is prepared only**: nothing is sent or paid until Clément approves, after satisfactory runs.

**State**
- `Berry-Mappemonde/Blue-Intelligence-V3` (private) is an **as-is mirror copy** of `Berry-Mappemonde/Blue-Intelligence`: all branches and tags; default branch `feat/integrate-poe-pipeline`, identical to the source.
- **All work happens on `dev/v3`**, through branches and pull requests. **Never merge into `main`**, and never merge `feat/integrate-poe-pipeline` into `main`. Nothing goes into `main` before Clément's full visual review of the application.
- **GitHub Actions are disabled** so the copied deploy workflows cannot run; re-enable only with Clément's approval.
- The full history was scanned for secrets: none found (only `.env.example` files with dummy values). Keys live in environment variables only.
- An earlier prototype (commit 058bd3c, made by mistake as a clean new repo) is preserved on branch `prototype-058bd3c`.
- `Blue-Intelligence-Map` is **not part of V3** (Clément's decision of 29 Oct).
- The original repositories are read-only; V2 stays untouched while it is under judging (Open Agent Hackathon, until 6 Nov). The batch infrastructure and the "hackathon machine" are never copied.
- Coding goes through batches of small numbered lots (Cursor CLI), each on its own branch, opened as a PR to `dev/v3` only when its tests pass.

## 3. What the copy already contains (reuse, don't rewrite)
@HamidZ11 — please check this inventory of the run mechanism against the code you know.
- **Runs without loss:** isolated versioned runs (`poe_runs`, `project_runs`, `isolated_runs`, `run_journal`, `run_fingerprint`), run IDs, a rules snapshot and a code fingerprint per run; live collections are never written by a run; run diff (`poe_diff`); full run script; `RunSelector` in the frontend.
- **Maps:** `MapView`, layer sidebars, zone sheet, MPA / marina / harbour-office / science panels (MapLibre and Leaflet).
- **V1 tools:** archived under `Archives/Blue-Intelligence/` (not wired in).
- **V2 tools:** OSM seeds and validation, land mask, World Port Index, NOAA ENC, GeoNames / Nominatim, review queue, Zetaris adapters, evidence store, H-MEM engine.
- **Nemotron:** `core/nvidia.py`, `extract.py` (Super VL), PoE judge agent. **Meterless Nightshift:** "Nightshift-first" generator.
- **Missing:** Tavily (docs only, no code), Toloka export, LangSmith / Tandem. Whether Nemotron goes through Token Factory is not confirmed.

## 4. Work plan by priority (from BI-V3-CONSIGNES §3)
**P0**
1. Add **Tavily** (Search + Extract) to the research cascade of the runs (ports of entry, projects, marinas, harbour offices); official domains via `include_domains`, then the V2 cascade (trafilatura, Readability, Jina, TinyFish last).
2. Route Nemotron through **Nebius Token Factory (EU host)**, keeping the copy's models.
3. **Risk: one Nemotron call per whole EEZ.** Today Nemotron has to process an entire EEZ in each call, which will very likely fail or degrade for EEZs that contain many ports (context size, truncated or incomplete answers). Option to study: split each EEZ into batches of ports and/or sources (for example one batch per official list page or per group of candidate ports), process the batches separately, then merge the results under the same `mrgid` sheet with the existing dedup rules. Batch size and limits must be measured on real EEZs, not set in advance. @HamidZ11 — review requested on the batching approach and on where the per-EEZ call happens in the copy's pipeline.
4. **Unified run mechanism, designed before the full run.** V2 and V3 runs work very differently: the copy (V2) has isolated versioned runs in Mongo (`*_run_*` collections, `run_id`, rules snapshot, code fingerprint, run journal, `poe_diff`, live collections never written by a run), while prototype 058bd3c writes immutable file runs (`runs/<run_id>/` with `entities.geojson`, a SHA-256 `manifest.json` and `trace.jsonl`, `run_id` = timestamp + code fingerprint + rules fingerprint) and certifies points C0–C4. Design one mechanism that combines them sensibly so that **a single run** produces everything (all four layers, V2 tools + Tavily + Nemotron via Token Factory, C0–C4 levels, manifest, diff against V2), with no parallel or duplicate run systems and no loss of earlier runs. Write the design and get it validated before step 5. @HamidZ11 — review requested on the unified run design (§3, §8).
5. **Run the full run** from `dev/v3` with this unified mechanism, without losing earlier runs (V2 database read-only), with a **quantified diff against V2** runs.
6. Keep Actions disabled, never push to `main`; everything through PRs to `dev/v3`.

**P1**
7. Salvage from prototype 058bd3c, as lots on `dev/v3`: the C0–C4 ladder, the verified verbatim quote, and the SHA-256 run manifest (as part of item 4).
8. Prepare the Toloka export (§6), without sending anything.
9. Nightshift: compare with Token Factory and write a note (use Nightshift and H-MEM only if the Nebius hackathon rules allow them and Nebius cannot do the same or better).
10. Business rules: United Kingdom (every UK port is a port of entry; entry is via an official app, so no designated port is not a gap), the PoE golden rule, Noonsite never Gold. Check what the copy already implements.
11. LangSmith: real test once a key is provided; Tandem: assessment.

**P2**
12. A visual review page for Clément (made as simple as possible); FR / EN manual and help pages.
13. Decide when (and whether) to re-enable Actions and make the repository public.

Indicative sequence (from BI-V3-PLAN §7, repository steps removed since the copy exists): inventory of existing runs (read-only) → Token Factory + Tavily → unified run design validated (P0-4) → full V3 run on a pilot EEZ → diff vs V2 → map → world run → Toloka export prepared (no send) → LangSmith / Tandem. Connecting NAVIGUIDE to V3 data only after 15 Dec is a proposal, **[to confirm]**.

## 5. Certification and georeferencing
Levels (a point moves up, and goes down only if evidence contradicts it):
- **C0** candidate (one source);
- **C1** official source extracted (Tavily Extract, government or port-authority domain);
- **C2** C1 + Nemotron Ultra judge "supported" on the excerpt;
- **C3** C2 + agreeing geo: two independent geocoders < 500 m, at sea or at the quay (land mask), OSM `osm_confidence`;
- **C4** C3 + human validation (internal review or Toloka).
Display the precision (m) and the method (OSM, WPI, geocoding, LLM). **An LLM-only point never goes above C1.** Numeric targets (share of C2+ and C3+ per layer) are set after the first run, not invented here.

Models: CONSIGNES says keep the copy's Nemotron models and route them through Token Factory. The 28 Oct plan's split (Nano / Lightning triage, Super extraction, Ultra judge, Qwen3 embeddings) is an option only. Nano and Super 120B are retired on 9 Nov **[to confirm: target models]**.

## 6. Toloka sample (preparation only)
**Preparation only: nothing is sent or paid until Clément approves, after satisfactory runs.** Budget $50 maximum, association account (RV1, 28 Oct). Export on `dev/v3`, from a validated V3 run: stratified by layer and confidence level, batches of 23 items, overlap of 3; 3 "yes" = confirmed, 2 against 1 = split (CONSIGNES; supersedes the plan's 100 points / 25 per layer). Existing material: `/workspace/toloka/`. Suggested task: "Does this official page confirm that [port / marina / harbour office] exists at this location? Yes / No / Can't tell, + link". Languages FR, EN, ES. Reviewers judge the evidence, never the extraction. Public data only, no personal names. Results calibrate C2 / C3.

## 7. Business contract inherited from V2 (Clément's specifications)
- **PoE goal:** for every EEZ, all official Ports of Entry usable by a foreign recreational vessel, and the State pages that list them. Information is indicative ("Not for navigation").
- **Grain:** one EEZ = one VLIZ v12 polygon (`mrgid`), never a country aggregate (France has 23). One sheet per polygon: one Top-Down URL, the PoE list, one Bottom-Up URL per port; a URL seen by both arms is a Gold source.
- **PoE = designated recreational.** Excluded: commerce-only or industrial ports, marinas without clearance, airports, land posts, ports of another country, any name absent from the extracts.
- **Official sources only** for legal status. Noonsite is a signal for name comparison, never downloaded, cited, or Gold.
- **Data are a treasure:** no purge, no destructive rebuild, every mapping step stays comparable.
- **Numbers are not opinions:** each parameter is Law, Geometry, Score, or Budget, with a default and an interval, recorded per run. Nominatim at most 1 request / 1.1 s.
- **Dedup:** 500 m + 60 % / 90 % name similarity (PoE / projects); harbour offices use a separate 250 m building overlay.
- **V2 confidence score (0–100):** Source 30 · Reading 25 · Map 25 · External 20. Its relation to C0–C4 is **[to confirm]**. @HamidZ11 — review requested on how the V2 score and the C0–C4 levels should relate.

## 8. Run storage
@HamidZ11 — review requested (see P0-4).
Each run is exported (GeoParquet / GeoJSON + `manifest.json` with counts, SHA-256, sources, tool versions, cost) to **Nebius Object Storage** (RV1 choice), never into git; git keeps only manifests.

## 9. Open questions
1. Pilot EEZ for the first full run (Martinique was suggested), expressed as `mrgid` values. **[to confirm]**
2. Access to a V2 Mongo export (mongodump by Clément, or the published exports). **[to confirm]**
3. LangSmith and Tandem keys (Tandem = Frumu's, after 15 Dec per the decisions). **[to confirm]**
4. Whether to use a single repository for NAVIGUIDE + BI V3. **[to confirm]**
5. When (and whether) to re-enable Actions and make the repository public (the plan's "private, then public at launch" is not decided). **[to confirm]**
6. Milestones and dates for V3. **[not in sources]**
7. Hamid's role and the areas he should review. **[to confirm]**
8. Mapping between the V2 0–100 score and C0–C4. **[to confirm]** @HamidZ11

9. **EEZs with many ports:** how to split the per-EEZ Nemotron call into batches of ports / sources, and how to merge them back into one `mrgid` sheet (P0-3). **[to confirm]** @HamidZ11
10. **Unified run:** which parts of the V2 run mechanism and of the prototype's file runs to keep, so that only one run is launched (P0-4). **[to confirm]** @HamidZ11

## Sources
- `plan-soumission-v3.md` (copy of `/workspace/submissions/devpost-naviguide/pilotage/BI-V3-PLAN.md` and `BI-V3-CONSIGNES.md`), **takes precedence**
- RV1 decisions of 28 Oct (Soumission agent attachment): V3 private; Toloka $50 max, association account; Nebius Object Storage
- `consignes-mac/README.md`, `consignes-mac/docs/*.md` (V2 specifications)
- `/workspace/bi-v3/README.md` (prototype 058bd3c, historical)
- GitHub metadata for `Berry-Mappemonde/Blue-Intelligence-V3` (read-only)
