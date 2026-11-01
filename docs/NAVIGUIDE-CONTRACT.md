# NAVIGUIDE contract

NAVIGUIDE (naviguide.fr) calls these Blue Intelligence routes and expects GeoJSON
FeatureCollections with a `features` list (JSON objects for the climatology,
depth, MPA stats and review endpoints). These routes are a contract: on any
branch, a recorded field must not be removed, renamed or change JSON type, and a
recorded status must not change. New fields are allowed.

- **Recorded shape:** `backend/tests/contracts/naviguide.json`, recorded on
  `feat/integrate-poe-pipeline@c811ab3` (the production branch).
- **Harness:** `backend/tests/contracts/naviguide_harness.py`.
- **Tests:** `backend/tests/test_naviguide_contract.py` (part of `pytest backend/tests`;
  needs `backend/requirements-dev.txt`).

The route list follows NAVIGUIDE's code: every call it makes is recorded with the
query parameters it sends. A test fails if one of those calls disappears from the
contract.

## How the shape is recorded

The harness calls `app.main.app` as it is, with every router and the admin gate.
It sets `ADMIN_KEY` to a dummy value, as production does, and uses FastAPI's
TestClient without startup tasks, on seeded offline data:

- **Mongo:** an in-memory `mongomock-motor` database replaces `app.db.db`. It holds
  the repo's `seed/ports_of_entry.geojson` ports and `seed/projects.geojson` projects
  (as `scripts/seed_ci_mongo.py` builds them), the sample zone from
  `eez_world_map.geojson`, and one full-field document for each of marinas,
  anchorages, capitaineries, science, MPA and the depth cache.
- **No network:** every outbound HTTP request raises. The NOAA ENC fetch (`/noaa/aids`
  and the NOAA export) and the ProtectedSeas fetch are stubbed. The MPA bbox query
  (`$geoIntersects`, which mongomock lacks) is done with shapely over the same seeded
  sites. `/depth` is answered from the seeded cache, so no EMODnet call is made.
- **Files:** EEZ map, route and climatology data come from the repo files, as in production.

The recorded shape of each response is:

- **Status:** the HTTP status.
- **Top level:** its keys, and the keys of the objects it contains (one level deep).
- **GeoJSON:** for a FeatureCollection, its geometry types and the union of feature
  property names with their JSON types.

Types are `string`, `number` (int or float), `boolean`, `null`, `object` and `array`.

## What fails a test

- The status changes. For example, `/review/fiche` without `X-Admin-Key` must stay 401
  in production, and 200 with the key.
- A recorded top-level key, nested key or feature property is missing, which includes
  a rename.
- Its type set is no longer the recorded one. For example, a property recorded as
  `string` now also returns `null`.
- New geometry types appear.
- A GeoJSON route that returned features for the seeded data now returns none.
- One of NAVIGUIDE's calls (path plus parameter set) is no longer in the contract.

## Not covered by the first version of this contract

The first recording took routes from a path list, not from NAVIGUIDE's code. It
missed these:

- **Routes:** `/api/anchorages`, `/api/depth`, `/api/noaa/aids`, `/api/review/fiche`
  (with and without `X-Admin-Key`), `/api/export/geojson` (projects; it had been
  excluded on purpose), `/api/climatology/wave/tiles/...` and
  `/api/climatology/current/tiles/...`.
- **Parameter variants:**
  - `/climatology/point` with `dest_lat` + `dest_lon`, with `day`, and with both.
  - `/climatology/crossings` with `day`.
  - `wind`, `current` and `cyclones` `.geojson` at the default spacing; it only had
    `spacing_deg=4`.
  - The `cyclones` fallback with `spacing_deg=4`.
  - `wave.geojson` and wave tiles with `stat=p50`, `stat=p90` and `stat=mean`; it only
    had the default p90 at `spacing_deg=4`.
  - `/climatology/meta` without `month`, as NAVIGUIDE calls it.

`/api/export/route.geojson`, `/api/export/anchorages.geojson` and
`/api/export/noaa-aids.geojson` are not in NAVIGUIDE's call list but stay recorded.

## Notes for NAVIGUIDE

- **`stat=mean` returns no wave features**, on `wave.geojson` and on wave tiles. This is
  also the case in production. The wave snapshots hold Hs P50/P90 only
  (`"stat": "p50_p90"`, "Not a P1M mean"), so `mean` yields an empty FeatureCollection.
  Its top-level shape is recorded, but it has no feature properties to check.
- **`/review/fiche` is admin-gated in production:** without `X-Admin-Key` it answers 401
  `{"detail": "Admin key required"}`.

## Re-recording

Re-record only when production intentionally changes the contract, and tell NAVIGUIDE
first. Run the harness from a checkout of the production branch:

```bash
cd backend && python tests/contracts/naviguide_harness.py --record "feat/integrate-poe-pipeline@$(git rev-parse --short HEAD)"
```

Copy the resulting `tests/contracts/naviguide.json` into the branch.

## Routes

| route | query used | status | top-level keys | feature properties |
|---|---|---|---|---|
| `/api/poe/ports` | `mrgid={mrgid}` | 200 | `type`, `attribution`, `features` | `city`, `confidence`, `confidence_parts`, `confidence_reasons`, `country_iso2`, `distance_km`, `extracted_at`, `geocode_source`, `id`, `mrgid`, `name`, `note`, `osm_confidence`, `osm_tags`, `reasons_en`, `source_urls`, `spatial_anomaly`, `spatial_kind`, `url_bu`, `validated`, `zone_name` |
| `/api/poe/zones/geojson` |  | 200 | `type`, `attribution`, `features` | `geoname`, `iso2`, `mrgid`, `name`, `pol_type`, `sovereign` |
| `/api/export/poe.geojson` |  | 200 | `type`, `attribution`, `features`, `metadata` | `city`, `confidence`, `confidence_parts`, `confidence_reasons`, `country_iso2`, `distance_km`, `extracted_at`, `geocode_source`, `id`, `mrgid`, `name`, `note`, `osm_confidence`, `osm_tags`, `reasons_en`, `source_urls`, `spatial_anomaly`, `spatial_kind`, `url_bu`, `validated`, `zone_name` |
| `/api/export/geojson` |  | 200 | `type`, `features`, `metadata` | `category`, `category_group`, `description`, `funder`, `id`, `image`, `location`, `s_ocean`, `snapped`, `title`, `url` |
| `/api/export/science.geojson` |  | 200 | `type`, `features`, `attribution` | `abstract`, `bbox`, `cycle`, `date`, `doi`, `end`, `error_m`, `fetched_at`, `id`, `kind`, `lat`, `lon`, `method`, `name`, `ocean`, `profile_date`, `provider`, `ship`, `source`, `start`, `url`, `wmo` |
| `/api/export/marinas.geojson` |  | 200 | `type`, `features`, `attribution`, `metadata` | `fetched_at`, `has_google_place`, `id`, `image`, `maps_place_url`, `maps_url`, `name`, `osm_id`, `source`, `website`, `website_source`, `website_status` |
| `/api/export/capitaineries.geojson` |  | 200 | `type`, `features`, `attribution`, `metadata` | `canal_vhf`, `fetched_at`, `id`, `kind`, `maps_url`, `name`, `noaa_id`, `osm_id`, `shom_id`, `source`, `sources`, `telephone`, `website` |
| `/api/export/amp.geojson` |  | 200 | `type`, `features`, `name`, `note`, `metadata` | `category_name`, `country`, `designation`, `enriched_at`, `entry`, `fetched_at`, `gov_level`, `id`, `iucn_cat`, `lat`, `lfp`, `lon`, `manager_url`, `managing_authority`, `name`, `purpose`, `site_id`, `speed`, `state`, `visit_url`, `visit_url_source`, `visit_url_status`, `wdpa_id`, `year_est` |
| `/api/export/route.geojson` |  | 200 | `type`, `name`, `features`, `metadata` | `antimeridian_part`, `from`, `name`, `point_type`, `to`, `type` |
| `/api/export/anchorages.geojson` |  | 200 | `type`, `features`, `metadata` | `anchorage_type`, `fetched_at`, `id`, `name`, `nearest_waypoint`, `osm_id`, `priority`, `source`, `stale`, `tags` |
| `/api/export/noaa-aids.geojson` |  | 200 | `type`, `features`, `metadata` | `id`, `kind`, `name`, `service`, `source` |
| `/api/marinas` |  | 200 | `type`, `features`, `attribution` | `fetched_at`, `has_google_place`, `id`, `image`, `maps_place_url`, `maps_url`, `name`, `osm_id`, `source`, `website`, `website_source`, `website_status` |
| `/api/capitaineries` |  | 200 | `type`, `features`, `attribution` | `canal_vhf`, `fetched_at`, `id`, `kind`, `maps_url`, `name`, `noaa_id`, `osm_id`, `shom_id`, `source`, `sources`, `telephone`, `website` |
| `/api/anchorages` |  | 200 | `type`, `features` | `anchorage_type`, `fetched_at`, `id`, `name`, `nearest_waypoint`, `osm_id`, `priority`, `source`, `stale`, `tags` |
| `/api/noaa/aids` | `bbox=-74.3,40.3,-73.6,40.9` | 200 | `type`, `features`, `metadata` | `id`, `kind`, `name`, `service`, `source` |
| `/api/depth` | `lat=14.6&lon=-61.07` | 200 | `lat`, `lon`, `depth_m`, `on_land`, `source`, `cached` | (not GeoJSON) |
| `/api/amp` | `bbox=-61.3,14.3,-60.8,14.9` | 200 | `type`, `features`, `hint`, `source`, `truncated`, `fetched`, `error` | `category_name`, `country`, `designation`, `enriched_at`, `entry`, `fetched_at`, `gov_level`, `id`, `iucn_cat`, `lat`, `lfp`, `lon`, `manager_url`, `managing_authority`, `name`, `purpose`, `site_id`, `speed`, `state`, `visit_url`, `visit_url_source`, `visit_url_status`, `wdpa_id`, `year_est` |
| `/api/amp/stats` |  | 200 | `mode`, `total`, `with_manager_url`, `with_visit_url`, `visit_coverage`, `items_mapped` | (not GeoJSON) |
| `/api/amp/nogo` |  | 200 | `type`, `features`, `name`, `note`, `count` | `category_name`, `country`, `designation`, `enriched_at`, `entry`, `fetched_at`, `gov_level`, `id`, `iucn_cat`, `lat`, `lfp`, `lon`, `manager_url`, `managing_authority`, `name`, `purpose`, `site_id`, `speed`, `state`, `visit_url`, `visit_url_source`, `visit_url_status`, `wdpa_id`, `year_est` |
| `/api/amp/sites/amp-1` |  | 200 | `kind`, `id`, `site_id`, `name`, `country`, `state`, `designation`, `category_name`, `iucn_cat`, `purpose`, `lfp`, `entry`, `speed`, `managing_authority`, `gov_level`, `wdpa_id`, `year_est`, `manager_url`, `visit_url`, `visit_url_status`, `visit_url_source`, `lat`, `lon`, `fetched_at`, `enriched_at`, `wrote_amp_sites` | (not GeoJSON) |
| `/api/review/fiche` | `kind=eez&id={mrgid}` + `X-Admin-Key` | 200 | `kind`, `run_id`, `id`, `fiche`, `comment`, `comment_updated_at`, `pre_gold`, `gold_on`, `choices`, `gold_ready`, `wrote_projects`, `wrote_poe_ports`, `wrote_marinas`, `wrote_capitaineries`, `wrote_amp_sites` | (not GeoJSON) |
| `/api/review/fiche` | `kind=eez&id={mrgid}` | 401 | `detail` | (not GeoJSON) |
| `/api/review/fiche` | `kind=amp&id=amp-1` + `X-Admin-Key` | 200 | `kind`, `run_id`, `id`, `fiche`, `comment`, `comment_updated_at`, `pre_gold`, `gold_on`, `choices`, `gold_ready`, `wrote_projects`, `wrote_poe_ports`, `wrote_marinas`, `wrote_capitaineries`, `wrote_amp_sites` | (not GeoJSON) |
| `/api/review/fiche` | `kind=amp&id=amp-1` | 401 | `detail` | (not GeoJSON) |
| `/api/climatology/meta` |  | 200 | `kind`, `month`, `period`, `periods`, `provenance`, `source_ids`, `doi`, `snapshot`, `attribution`, `disclaimer`, `disclaimer_fr`, `review`, `products` | (not GeoJSON) |
| `/api/climatology/point` | `lat=14.6&lon=-61.0&month=1` | 200 | `kind`, `month`, `period`, `provenance`, `periods`, `coordinates`, `snapshot`, `disclaimer`, `disclaimer_fr`, `wind_atlas`, `wave`, `current`, `cyclone` | (not GeoJSON) |
| `/api/climatology/point` | `lat=14.6&lon=-61.0&month=1&dest_lat=16.2&dest_lon=-61.5` | 200 | `kind`, `month`, `period`, `provenance`, `periods`, `coordinates`, `snapshot`, `disclaimer`, `disclaimer_fr`, `wind_atlas`, `wave`, `current`, `cyclone` | (not GeoJSON) |
| `/api/climatology/point` | `lat=14.6&lon=-61.0&month=9&day=15` | 200 | `kind`, `month`, `period`, `provenance`, `periods`, `coordinates`, `snapshot`, `disclaimer`, `disclaimer_fr`, `wind_atlas`, `wave`, `current`, `cyclone` | (not GeoJSON) |
| `/api/climatology/point` | `lat=14.6&lon=-61.0&month=9&day=15&dest_lat=16.2&dest_lon=-61.5` | 200 | `kind`, `month`, `period`, `provenance`, `periods`, `coordinates`, `snapshot`, `disclaimer`, `disclaimer_fr`, `wind_atlas`, `wave`, `current`, `cyclone` | (not GeoJSON) |
| `/api/climatology/crossings` | `lat1=14.6&lon1=-61.0&lat2=16.2&lon2=-61.5&month=9` | 200 | `kind`, `count`, `storms`, `month`, `dayrange`, `radius_nm`, `period`, `source`, `snapshot_present` | (not GeoJSON) |
| `/api/climatology/crossings` | `lat1=14.6&lon1=-61.0&lat2=16.2&lon2=-61.5&month=9&day=15` | 200 | `kind`, `count`, `storms`, `month`, `dayrange`, `radius_nm`, `period`, `source`, `snapshot_present` | (not GeoJSON) |
| `/api/climatology/wind.geojson` | `month=1` | 200 | `type`, `features`, `attribution`, `metadata` | `calm_pct`, `directions_from`, `gale_pct`, `kind`, `month`, `sample_count`, `stat`, `vector_mean_from_deg`, `vector_mean_knots`, `wind_direction_from_deg`, `wind_speed_knots` |
| `/api/climatology/wind.geojson` | `month=1&spacing_deg=4` | 200 | `type`, `features`, `attribution`, `metadata` | `calm_pct`, `directions_from`, `gale_pct`, `kind`, `month`, `sample_count`, `stat`, `vector_mean_from_deg`, `vector_mean_knots`, `wind_direction_from_deg`, `wind_speed_knots` |
| `/api/climatology/current.geojson` | `month=1` | 200 | `type`, `features`, `attribution`, `metadata` | `below_threshold`, `direction_to_deg`, `kind`, `month`, `speed_knots` |
| `/api/climatology/current.geojson` | `month=1&spacing_deg=4` | 200 | `type`, `features`, `attribution`, `metadata` | `below_threshold`, `direction_to_deg`, `kind`, `month`, `speed_knots` |
| `/api/climatology/cyclones.geojson` | `month=1` | 200 | `type`, `features`, `attribution`, `metadata` | `basin`, `color`, `kind`, `max_wind_kn`, `month`, `name`, `season`, `sid`, `url`, `wind_source` |
| `/api/climatology/cyclones.geojson` | `month=1&spacing_deg=4` | 200 | `type`, `features`, `attribution`, `metadata` | `basin`, `color`, `kind`, `max_wind_kn`, `month`, `name`, `season`, `sid`, `url`, `wind_source` |
| `/api/climatology/wave.geojson` | `month=1&stat=p50` | 200 | `type`, `features`, `attribution`, `metadata` | `dir_deg`, `hs_m`, `kind`, `month`, `period_s`, `stat` |
| `/api/climatology/wave.geojson` | `month=1&stat=p50&spacing_deg=4` | 200 | `type`, `features`, `attribution`, `metadata` | `dir_deg`, `hs_m`, `kind`, `month`, `period_s`, `stat` |
| `/api/climatology/wave.geojson` | `month=1&stat=p90` | 200 | `type`, `features`, `attribution`, `metadata` | `dir_deg`, `hs_m`, `kind`, `month`, `period_s`, `stat` |
| `/api/climatology/wave.geojson` | `month=1&stat=p90&spacing_deg=4` | 200 | `type`, `features`, `attribution`, `metadata` | `dir_deg`, `hs_m`, `kind`, `month`, `period_s`, `stat` |
| `/api/climatology/wave.geojson` | `month=1&stat=mean` | 200 | `type`, `features`, `attribution`, `metadata` | (no features: see note) |
| `/api/climatology/wave.geojson` | `month=1&stat=mean&spacing_deg=4` | 200 | `type`, `features`, `attribution`, `metadata` | (no features: see note) |
| `/api/climatology/wind/tiles/2/1/1.json` | `month=1` | 200 | `type`, `features` | `calm_pct`, `dir_from_deg`, `gale_pct`, `speed_knots` |
| `/api/climatology/current/tiles/2/1/1.json` | `month=1` | 200 | `type`, `features` | `direction_to_deg`, `speed_knots` |
| `/api/climatology/wave/tiles/2/1/1.json` | `month=1&stat=p50` | 200 | `type`, `features` | `dir`, `hs_m`, `stat` |
| `/api/climatology/wave/tiles/2/1/1.json` | `month=1&stat=p90` | 200 | `type`, `features` | `dir`, `hs_m`, `stat` |
| `/api/climatology/wave/tiles/2/1/1.json` | `month=1&stat=mean` | 200 | `type`, `features` | (no features: see note) |

Not in the contract: write and admin routes (`POST`, `*/build*`, `*/runs*`,
`*/enrich*`, `*/status`) and the `visible=` / `review=` variants.

The `/api/runs*` endpoints of the unified run (#14) are V3-only. They moved to
`/api/v3/runs` (see `docs/RUNS.md`).
