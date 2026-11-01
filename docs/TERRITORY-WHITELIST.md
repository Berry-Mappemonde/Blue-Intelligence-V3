# Territory whitelist

Which official domains count for a zone's ports of entry (PoE) when the zone is a territory
rather than its sovereign. Rule (Clément, issue #21): **a territory's whitelist is whoever
actually manages and publishes its ports of entry, researched per territory. It never
inherits the sovereign's list automatically.**

## History

V1/V2 history is in `Berry-Mappemonde/Blue-Intelligence` (this repo starts on 2026-10-18).
Found with `git log -S/-G` on `sov_iso2`, `territories.json`, `MRGID_TO_TERRITORY` and
`td_url_fits_polygon`, plus the commit messages.

| date | commit | what changed |
|---|---|---|
| ≤ 2026-08-25 | V1 `formalities.py` | 13 curated French territories in `territories.json` (regime, `official_domains`, `ports_of_entry`, `ref_url`). The territory record was the source. |
| 2026-08-26 | `49547c1` | V1 fiches deleted. World pipeline over 285 VLIZ EEZs. `build_whitelist(iso2, sov_iso2)` returns the **union** `{iso2, sov_iso2}`: Mayotte gets FR, Niue gets NZ, Sint Maarten gets NL. |
| 2026-08-29 | `16a9d14` "anti gov étranger" | Fallback gatekeeper drops other countries' government sites (`douane.gov.tn` for Cameroon). The zone and sovereign ccTLDs stay allowed. |
| 2026-08-29 | `916d712`, `27a3f58` | Mexico and Niue: seeds and search hints come from official sources. |
| 2026-09-06 | `23593ce`, `ccfe4f1` | CAHIER_DES_CHARGES_POE v1.0/v1.1 written while the union was still in place. |
| 2026-09-07 | `0bc9cfb` | `primary = iso2 or sov_iso2`. A polygon with its own ISO2 no longer gets the sovereign's list (Niue ≠ NZ, Mayotte ≠ FR). Seeds and hints are keyed by polygon ISO2. |
| 2026-09-07 | `f8ae0f7` | Sint Maarten no longer inherits the Netherlands (`government.nl` excluded). |
| 2026-09-07 | `ea258f0` | `territory_ref.py` brings `territories.json` back by MRGID (`MRGID_TO_TERRITORY`), but only to inject curated TD URLs into the Review fiche. |
| 2026-09-14 | `d4a33b5` | `td_url_fits_polygon`: when polygon ≠ sovereign, the Review page keeps only curated domains or the local ccTLD. Review only. |

V3 (`dev/v3`) carries the `iso2 or sov_iso2` code. PR #20/#22 audited the route zones and
put French overseas domains under ISO2 keys of `poe_exceptions.json`. The unmerged branch
`feat/v3-whitelist-inherit-sovereign` (`66958c3`) went back to sovereign inheritance with a
deny-list; issue #21 rejected that approach.

## Spec vs code

- CAHIER §6.1 step 2: whitelist = ISO2 of the polygon **and** of the sovereign (Mayotte `YT` + `FR`). That is the 08-26 union, which the code dropped on 09-07. The spec was never updated.
- CAHIER §24: "France — 23 polygons", one sheet per `mrgid`. The whitelist was the only step not done per polygon: `build_whitelist` had no `mrgid` and never read `territories.json`.
- Consequences: (a) a zone with no ISO2 uses the sovereign's key, so anything added for it also becomes official on the mainland. `taaf.fr` (added for Tromelin) became official for mainland France, and the Canary port authorities for mainland Spain. (b) Overseas domains were keyed by ISO2, so they could not be scoped to a single polygon, and TF covered six TAAF polygons at once. (c) The pipeline and the Review page used two different rules.

## Design

1. **Table**: `backend/data/territories.json`, generic. Each record adds `iso2` (VLIZ polygon ISO2 or `null`), `sovereign_iso2`, `mrgids` (VLIZ v12) and `sovereign_runs_entry` + `sovereign_runs_entry_evidence`. Records exist for France's 13 territories (23 polygons; Clipperton stays unmapped) and the Canary Islands (8364 → ES, sourced from `whitelist_sources.json`). `territory_ref.MRGID_TO_TERRITORY` is now built from `mrgids`.
2. **`build_whitelist(iso2, sov_iso2, exceptions, mrgid=None)`**. For a **mapped mrgid** the whitelist is the record's `official_domains` ∪ `poe_exceptions.json` `whitelist_mrgid[mrgid]` ∪ `auto_mrgid[mrgid]`. The sovereign's national labels (PSL government label + `manual`/`auto` of `sovereign_iso2`) are added **only if** `sovereign_runs_entry` is true. It defaults to false. **Any other zone** keeps today's rule: national labels of `iso2 or sov_iso2`.
3. **`sovereign_runs_entry`**, set from each record's own evidence:
   - **true**: mainland France (the sovereign itself); the DROMs Martinique, Guadeloupe, Guyane, Réunion and Mayotte (art. 73: State customs, PAF and DM run entry); the Canaries (AEAT, Policía Nacional, SASEMAR, Puertos del Estado).
   - **false**: the COMs Saint-Barthélemy, Saint-Martin, Saint-Pierre-et-Miquelon, Polynésie and Wallis-et-Futuna (own statute or customs); New Caledonia (own Code des douanes); the TAAF (préfet's authorisation).
4. **Never attached**: `never_attach_to_sovereign` lists SX, AW, CW, CK, NU, GL and FO. A record for one of them is dropped and never gets the sovereign's labels, so they stay unmapped and keep their own ISO2 list.
5. **Data moves**: the ISO2 buckets of mapped territories (MQ, GP, BL, MF, GF, PM, PF, WF, NC, YT, RE, TF) move to `whitelist_mrgid`, minus what the record already lists. `taaf.fr` leaves `manual.FR` and now comes from the TAAF record (Tromelin, Europa and the other TAAF polygons). The DM sud océan Indien goes to Tromelin's bucket. `palmasport.es`, `puertosdetenerife.org` and `puertoscanarios.es` leave `manual.ES` for the Canaries record. Domains a mapped zone bootstraps go to `auto_mrgid[mrgid]`, not `auto[ISO2]`.
6. **Callers**: the pipeline (`zone_whitelist(zone)` everywhere it built one, plus the referential build), `poe_seed_enrich.judge_one`, and `registry_check`. For a mapped polygon, official evidence must be on that polygon's whitelist: a `douane.gouv.fr` page is no evidence for Nouméa. Unmapped zones keep the naming heuristic. `scripts/route_whitelist_report.py` builds with `mrgid`. The "was missing" note is frozen per audit in `whitelist_sources.json`.
7. **Next**: add Bonaire/Saba/Sint-Eustatius (BQ) once their customs and immigration bodies are sourced. Align `td_url_fits_polygon` (Review) with `sovereign_runs_entry`.
