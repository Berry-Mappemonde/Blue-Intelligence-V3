# NAVIGUIDE contract

NAVIGUIDE (naviguide.fr) reads Blue Intelligence routes and expects GeoJSON
FeatureCollections with a `features` list. These routes are a contract: on any
branch, a recorded field must not be removed, renamed or change JSON type. New
fields are allowed.

- **Recorded shape:** `backend/tests/contracts/naviguide.json`, recorded on
  `feat/integrate-poe-pipeline@c811ab3` (the production branch).
- **Harness:** `backend/tests/contracts/naviguide_harness.py`.
- **Tests:** `backend/tests/test_naviguide_contract.py` (part of `pytest backend/tests`).

## How the shape is recorded

The harness calls each route through FastAPI's TestClient on seeded offline data:

- **Mongo:** an in-memory `mongomock-motor` database replaces `app.db.db`. It holds
  the repo's `seed/ports_of_entry.geojson` ports (as `scripts/seed_ci_mongo.py` builds
  them) plus one full-field document for each of marinas, anchorages, capitaineries,
  science and MPA.
- **No network:** every outbound HTTP request raises. The NOAA ENC fetch and the
  ProtectedSeas fetch are stubbed. The MPA bbox query (`$geoIntersects`, which mongomock
  lacks) is done with shapely over the same seeded sites.
- **Files:** EEZ map, route and climatology data come from the repo files, as in production.

The recorded shape of each response is:

- **Top level:** its keys, and the keys of the objects it contains (one level deep).
- **GeoJSON:** for a FeatureCollection, its geometry types and the union of feature
  property names with their JSON types.

Types are `string`, `number` (int or float), `boolean`, `null`, `object` and `array`.

## What fails a test

- A recorded top-level key, nested key or feature property is missing, which includes
  a rename.
- Its type set is no longer the recorded one. For example, a property recorded as
  `string` now also returns `null`.
- New geometry types appear.
- A route stops returning 200.
- A GeoJSON route returns no features for the seeded data.

## Re-recording

Re-record only when production intentionally changes the contract, and tell NAVIGUIDE
first. Run the harness from a checkout of the production branch:

```bash
cd backend && python tests/contracts/naviguide_harness.py --record "feat/integrate-poe-pipeline@$(git rev-parse --short HEAD)"
```

Copy the resulting `tests/contracts/naviguide.json` into the branch.

## Routes

| route | query used | top-level keys | feature properties |
|---|---|---|---|
| `/api/poe/ports` | `mrgid={mrgid}` | `type`, `attribution`, `features` | `city`, `confidence`, `confidence_parts`, `confidence_reasons`, `country_iso2`, `distance_km`, `extracted_at`, `geocode_source`, `id`, `mrgid`, `name`, `note`, `osm_confidence`, `osm_tags`, `reasons_en`, `source_urls`, `spatial_anomaly`, `spatial_kind`, `url_bu`, `validated`, `zone_name` |
| `/api/poe/zones/geojson` |  | `type`, `attribution`, `features` | `geoname`, `iso2`, `mrgid`, `name`, `pol_type`, `sovereign` |
| `/api/export/poe.geojson` |  | `type`, `attribution`, `features`, `metadata` | `city`, `confidence`, `confidence_parts`, `confidence_reasons`, `country_iso2`, `distance_km`, `extracted_at`, `geocode_source`, `id`, `mrgid`, `name`, `note`, `osm_confidence`, `osm_tags`, `reasons_en`, `source_urls`, `spatial_anomaly`, `spatial_kind`, `url_bu`, `validated`, `zone_name` |
| `/api/export/route.geojson` |  | `type`, `name`, `features`, `metadata` | `antimeridian_part`, `from`, `name`, `point_type`, `to`, `type` |
| `/api/export/marinas.geojson` |  | `type`, `features`, `attribution`, `metadata` | `fetched_at`, `has_google_place`, `id`, `image`, `maps_place_url`, `maps_url`, `name`, `osm_id`, `source`, `website`, `website_source`, `website_status` |
| `/api/export/anchorages.geojson` |  | `type`, `features`, `metadata` | `anchorage_type`, `fetched_at`, `id`, `name`, `nearest_waypoint`, `osm_id`, `priority`, `source`, `stale`, `tags` |
| `/api/export/capitaineries.geojson` |  | `type`, `features`, `attribution`, `metadata` | `canal_vhf`, `fetched_at`, `id`, `kind`, `maps_url`, `name`, `noaa_id`, `osm_id`, `shom_id`, `source`, `sources`, `telephone`, `website` |
| `/api/export/noaa-aids.geojson` |  | `type`, `features`, `metadata` | `id`, `kind`, `name`, `service`, `source` |
| `/api/export/amp.geojson` |  | `type`, `features`, `name`, `note`, `metadata` | `category_name`, `country`, `designation`, `enriched_at`, `entry`, `fetched_at`, `gov_level`, `id`, `iucn_cat`, `lat`, `lfp`, `lon`, `manager_url`, `managing_authority`, `name`, `purpose`, `site_id`, `speed`, `state`, `visit_url`, `visit_url_source`, `visit_url_status`, `wdpa_id`, `year_est` |
| `/api/export/science.geojson` |  | `type`, `features`, `attribution` | `abstract`, `bbox`, `cycle`, `date`, `doi`, `end`, `error_m`, `fetched_at`, `id`, `kind`, `lat`, `lon`, `method`, `name`, `ocean`, `profile_date`, `provider`, `ship`, `source`, `start`, `url`, `wmo` |
| `/api/marinas` |  | `type`, `features`, `attribution` | `fetched_at`, `has_google_place`, `id`, `image`, `maps_place_url`, `maps_url`, `name`, `osm_id`, `source`, `website`, `website_source`, `website_status` |
| `/api/capitaineries` |  | `type`, `features`, `attribution` | `canal_vhf`, `fetched_at`, `id`, `kind`, `maps_url`, `name`, `noaa_id`, `osm_id`, `shom_id`, `source`, `sources`, `telephone`, `website` |
| `/api/amp` | `bbox=-61.3,14.3,-60.8,14.9` | `type`, `features`, `hint`, `source`, `truncated`, `fetched`, `error` | `category_name`, `country`, `designation`, `enriched_at`, `entry`, `fetched_at`, `gov_level`, `id`, `iucn_cat`, `lat`, `lfp`, `lon`, `manager_url`, `managing_authority`, `name`, `purpose`, `site_id`, `speed`, `state`, `visit_url`, `visit_url_source`, `visit_url_status`, `wdpa_id`, `year_est` |
| `/api/amp/nogo` |  | `type`, `features`, `name`, `note`, `count` | `category_name`, `country`, `designation`, `enriched_at`, `entry`, `fetched_at`, `gov_level`, `id`, `iucn_cat`, `lat`, `lfp`, `lon`, `manager_url`, `managing_authority`, `name`, `purpose`, `site_id`, `speed`, `state`, `visit_url`, `visit_url_source`, `visit_url_status`, `wdpa_id`, `year_est` |
| `/api/amp/stats` |  | `mode`, `total`, `with_manager_url`, `with_visit_url`, `visit_coverage`, `items_mapped` | (not GeoJSON) |
| `/api/amp/sites/amp-1` |  | `kind`, `id`, `site_id`, `name`, `country`, `state`, `designation`, `category_name`, `iucn_cat`, `purpose`, `lfp`, `entry`, `speed`, `managing_authority`, `gov_level`, `wdpa_id`, `year_est`, `manager_url`, `visit_url`, `visit_url_status`, `visit_url_source`, `lat`, `lon`, `fetched_at`, `enriched_at`, `wrote_amp_sites` | (not GeoJSON) |
| `/api/climatology/meta` | `month=1` | `kind`, `month`, `period`, `periods`, `provenance`, `source_ids`, `doi`, `snapshot`, `attribution`, `disclaimer`, `disclaimer_fr`, `review`, `products` | (not GeoJSON) |
| `/api/climatology/point` | `lat=14.6&lon=-61.0&month=1` | `kind`, `month`, `period`, `provenance`, `periods`, `coordinates`, `snapshot`, `disclaimer`, `disclaimer_fr`, `wind_atlas`, `wave`, `current`, `cyclone` | (not GeoJSON) |
| `/api/climatology/crossings` | `lat1=14.6&lon1=-61.0&lat2=16.2&lon2=-61.5&month=9` | `kind`, `count`, `storms`, `month`, `dayrange`, `radius_nm`, `period`, `source`, `snapshot_present` | (not GeoJSON) |
| `/api/climatology/wind.geojson` | `month=1&spacing_deg=4.0` | `type`, `features`, `attribution`, `metadata` | `calm_pct`, `directions_from`, `gale_pct`, `kind`, `month`, `sample_count`, `stat`, `vector_mean_from_deg`, `vector_mean_knots`, `wind_direction_from_deg`, `wind_speed_knots` |
| `/api/climatology/wave.geojson` | `month=1&spacing_deg=4.0` | `type`, `features`, `attribution`, `metadata` | `dir_deg`, `hs_m`, `kind`, `month`, `period_s`, `stat` |
| `/api/climatology/current.geojson` | `month=1&spacing_deg=4.0` | `type`, `features`, `attribution`, `metadata` | `below_threshold`, `direction_to_deg`, `kind`, `month`, `speed_knots` |
| `/api/climatology/cyclones.geojson` | `month=9` | `type`, `features`, `attribution`, `metadata` | `basin`, `color`, `kind`, `max_wind_kn`, `month`, `name`, `season`, `sid`, `url`, `wind_source` |
| `/api/climatology/wind/tiles/2/1/1.json` | `month=1` | `type`, `features` | `calm_pct`, `dir_from_deg`, `gale_pct`, `speed_knots` |

Not in the contract: write and admin routes (`POST`, `*/build*`, `*/runs*`,
`*/enrich*`, `*/status`), `/api/export/geojson` (the projects export, which is not
`*.geojson`), and the `visible=` / `review=` variants.

The `/api/runs*` endpoints of the unified run (#14) are V3-only. They moved to
`/api/v3/runs` (see `docs/RUNS.md`).
