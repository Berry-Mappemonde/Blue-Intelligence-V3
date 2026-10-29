"""app.routers.marinas — Marinas & anchorages: build (OSM/SHOM), imports/exports,
unit and batch AI enrichment."""
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app.config import ROUTE_FILE
from app.core.from_scratch import resolve_from_scratch
from app.core.llm import get_llm_key
from app.core.tasks import BuildState, TaskState, new_task, prune_tasks, LOGS_TAIL_N
from app.db import db, get_settings
from app.services.anchorage_build import (
    anchorages_to_geojson,
    build_anchorages as run_build_anchorages,
    build_world_anchorages as run_build_world_anchorages,
)
from app.services.marina_enrich import ENRICH_FIELDS, enrich_marina
from app.services.marina_maps_place import is_google_place_url, resolve_maps_places
from app.services.marina_world import (
    SLIM_PROJECTION,
    marinas_to_slim_geojson,
    official_website,
    build_world_marinas as run_build_world_marinas,
)
from app.services.swarm_pipeline import now_iso
from app.services.geojson_import import empty_import_result, parse_feature_collection
from app.services.osm_seeds import TEST_TILE
from app.state import swarm

router = APIRouter(prefix="/api")

# ---------------------------------------------------------------------------
# Import — Marinas GeoJSON (2026-08-24 bug-fix: previously only projects had
# an import endpoint; the sidebar "Import GeoJSON" button silently sent
# marinas/formalities exports to the projects endpoint, discarding them).
# Accepts the `FeatureCollection` produced by `/api/export/marinas.geojson`.
# Each feature.properties.id is used as the Mongo `_id` for idempotent
# upserts, so re-importing the same file is a no-op (updated count grows,
# imported stays at 0).
# ---------------------------------------------------------------------------
@router.post("/import/marinas.geojson")
async def import_marinas_geojson(fc: dict = Body(...)):
    kind, feats = parse_feature_collection(fc)
    if kind == "invalid":
        raise HTTPException(400, "invalid GeoJSON FeatureCollection")
    if kind == "empty":
        return empty_import_result("total_marinas", await db.marinas.count_documents({}))
    imported = updated = invalid = 0
    for f in feats:
        try:
            geom = f.get("geometry") or {}
            if geom.get("type") != "Point":
                invalid += 1
                continue
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                invalid += 1
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                invalid += 1
                continue
            p = f.get("properties") or {}
            name = str(p.get("name") or "").strip()
            osm_id = p.get("osm_id")
            if not name and not osm_id:
                invalid += 1
                continue
            mid = str(p.get("id") or osm_id or "").strip() or str(uuid.uuid4())
            website = p.get("website") or official_website({"tags": p.get("tags") or {}})
            doc = {
                "_id": mid,
                "name": name,
                "lat": lat,
                "lon": lon,
                "source": p.get("source") or "openstreetmap",
                "tags": p.get("tags") or {},
                "osm_id": osm_id,
                "website": website,
                "website_status": p.get("website_status") or ("unchecked" if website else None),
                "website_source": p.get("website_source") or ("osm_tag" if website else None),
                "image": p.get("image"),
                "enriched": bool(p.get("enriched")),
                "enrichment_source": p.get("enrichment_source"),
                "enriched_at": p.get("enriched_at"),
                "stale": bool(p.get("stale")),
                "canal_vhf": p.get("canal_vhf"),
                "places_visiteurs": p.get("places_visiteurs"),
                "tirant_eau_max_metres": p.get("tirant_eau_max_metres"),
                "score_protection_meteo": p.get("score_protection_meteo"),
                "services_disponibles": p.get("services_disponibles"),
                "telephone_capitainerie": p.get("telephone_capitainerie"),
                "resume_avis": p.get("resume_avis"),
                "fetched_at": p.get("fetched_at") or now_iso(),
            }
            if p.get("priority") is not None:
                doc["priority"] = int(p["priority"])
            if p.get("nearest_waypoint"):
                doc["nearest_waypoint"] = p["nearest_waypoint"]
            place = p.get("maps_place_url")
            if is_google_place_url(place):
                doc["maps_place_url"] = place
                doc["maps_place_status"] = "found"
                doc["maps_place_source"] = p.get("maps_place_source") or "import"
            existing = await db.marinas.find_one({"_id": mid})
            if existing:
                await db.marinas.update_one({"_id": mid}, {"$set": doc})
                updated += 1
            else:
                await db.marinas.insert_one(doc)
                imported += 1
        except Exception:
            invalid += 1
    total = await db.marinas.count_documents({})
    if imported or updated:
        mark_marinas_fc_stale()
    swarm.log(f"Marinas GeoJSON import: {imported} imported, {updated} updated, {invalid} invalid", "success")
    return {"imported": imported, "merged": updated, "skipped_existing": 0, "invalid": invalid, "total_marinas": total}

# ---------- Marinas (Phase 2) ----------
MARINA_BUILD_STATE = BuildState()
# Phase 8 — Anchorages (Mouillages)
ANCHORAGE_BUILD_STATE = BuildState()


async def _all_marinas(q: dict | None = None, projection: dict | None = None) -> list[dict]:
    # Sort in Python (0.01 s) rather than Mongo: on Atlas M0 throughput is the
    # limiting factor, and a reduced batch_size bounds each socket
    # read (a default 16 MB batch can exceed socketTimeoutMS).
    cur = db.marinas.find(q or {}, projection or SLIM_PROJECTION).batch_size(4000)
    docs = [doc async for doc in cur]
    docs.sort(key=lambda d: (d.get("name") or ""))
    return docs


# ---------------------------------------------------------------------------
# Cache of the world GeoJSON dump (GET /api/marinas without a filter).
# On a remote Mongo (Atlas M0), rebuilding the ~32,000 features takes
# several minutes: serve the last built GeoJSON and rebuild
# in the background (stale-while-revalidate). Exports and snapshots
# always re-read the DB (freshness guaranteed for archives).
# ---------------------------------------------------------------------------
_MARINAS_FC_CACHE: dict = {"fc": None, "built_at": 0.0}
_MARINAS_FC_TTL_S = 300.0
# Local persistence: a process restart serves the last dump
# (15 MB) without waiting for Atlas. Then rebuilds in the background.
_MARINAS_FC_DISK = Path(os.environ.get(
    "MARINAS_FC_CACHE_PATH",
    str(Path(__file__).resolve().parents[2] / "data" / ".marinas_fc_cache.json"),
))
_marinas_fc_task: asyncio.Task | None = None


def _load_marinas_fc_disk() -> dict | None:
    try:
        if not _MARINAS_FC_DISK.is_file():
            return None
        data = json.loads(_MARINAS_FC_DISK.read_text(encoding="utf-8"))
        feats = data.get("features")
        if data.get("type") != "FeatureCollection" or not isinstance(feats, list):
            return None
        return data
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _write_marinas_fc_disk(fc: dict) -> None:
    try:
        _MARINAS_FC_DISK.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MARINAS_FC_DISK.with_name(_MARINAS_FC_DISK.name + ".tmp")
        tmp.write_text(json.dumps(fc, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_MARINAS_FC_DISK)
    except OSError as exc:
        print(f"[marinas] écriture cache disque échouée : {exc}")


async def _rebuild_marinas_fc() -> dict:
    docs = await _all_marinas({})
    fc = await asyncio.to_thread(marinas_to_slim_geojson, docs)
    _MARINAS_FC_CACHE["fc"] = fc
    _MARINAS_FC_CACHE["built_at"] = time.monotonic()
    await asyncio.to_thread(_write_marinas_fc_disk, fc)
    return fc


def _log_marinas_fc_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        print(f"[marinas] rafraîchissement du cache GeoJSON échoué : {exc}")


def _schedule_marinas_fc_refresh() -> asyncio.Task:
    """One rebuild at a time; return the task in progress."""
    global _marinas_fc_task
    if _marinas_fc_task is None or _marinas_fc_task.done():
        # Check the loop BEFORE creating the coroutine, otherwise an out-of-
        # asyncio leaves a never-awaited coroutine (RuntimeWarning).
        asyncio.get_running_loop()
        _marinas_fc_task = asyncio.create_task(_rebuild_marinas_fc())
        _marinas_fc_task.add_done_callback(_log_marinas_fc_result)
    return _marinas_fc_task


def mark_marinas_fc_stale() -> None:
    """Call after any write to db.marinas: the old cache stays
    served while a rebuild starts in the background."""
    _MARINAS_FC_CACHE["built_at"] = 0.0
    try:
        _schedule_marinas_fc_refresh()
    except RuntimeError:
        pass  # no asyncio loop (test context)


def start_marinas_fc_warmup() -> None:
    """Warm-up at server start (called by app.main).

    First load the last dump written to disk (immediate response),
    then re-read Mongo in the background.
    """
    if _MARINAS_FC_CACHE["fc"] is None:
        disk = _load_marinas_fc_disk()
        if disk is not None:
            _MARINAS_FC_CACHE["fc"] = disk
            # 0 = considered stale: a Mongo rebuild starts immediately.
            _MARINAS_FC_CACHE["built_at"] = 0.0
            n = len(disk.get("features") or [])
            print(f"[marinas] cache disque chargé ({n} fiches)")
    _schedule_marinas_fc_refresh()


@router.get("/marinas")
async def list_marinas(
    priority: int | None = None,
    source: str | None = None,
    visible: bool = False,
):
    q: dict = {}
    if priority is not None:
        q["priority"] = int(priority)
    if source:
        q["source"] = source
    if MARINA_BUILD_STATE.running:
        raise HTTPException(
            503,
            "marina dump running — skip full GeoJSON until the tile job finishes",
        )
    if priority is None and not source and not visible:
        fc = _MARINAS_FC_CACHE["fc"]
        if fc is not None:
            if time.monotonic() - _MARINAS_FC_CACHE["built_at"] > _MARINAS_FC_TTL_S:
                _schedule_marinas_fc_refresh()
            return fc
        # Cold cache: all clients wait for the same rebuild.
        return await _schedule_marinas_fc_refresh()
    docs = await _all_marinas(q)
    if visible:
        from app.services.review_gold import filter_visible
        docs = await filter_visible(db, "marina", docs, lambda m: m.get("_id"))
    return marinas_to_slim_geojson(docs)


@router.get("/export/marinas.geojson")
async def export_marinas():
    from app.core.export_meta import export_response
    docs = await _all_marinas({})
    fc = marinas_to_slim_geojson(docs)
    return export_response(fc, "marinas", "marinas.geojson",
                           license_note="© OpenStreetMap contributors (ODbL)")

class MarinasBuildBody(BaseModel):
    clear_before: bool = False
    resume: bool = True
    from_scratch: bool | None = None
    profile: str | None = None
    rules: dict | None = None
    # Kept so old clients / the Anchorages Audit map are not broken.
    radius_nm: float | None = None
    include_corridor: bool | None = None
    corridor_step_nm: float | None = None
    corridor_radius_nm: float | None = None
    maps_place_after: bool = False
    scope: str | None = None
    tiles: list[list[float]] | None = None
    label: str | None = None


async def _marina_rules_and_radii(body, settings: dict, extra_overrides: dict | None = None):
    from app.core.run_rules import RuleError, bind_rules, snapshot_for_run
    overrides = dict(body.rules or {})
    if extra_overrides:
        overrides.update({k: v for k, v in extra_overrides.items() if v is not None})
    try:
        rules = snapshot_for_run(mode="marinas", settings=settings,
                                 overrides=overrides or None, profile=body.profile)
    except RuleError as e:
        raise HTTPException(400, str(e)) from e
    bind_rules(rules)
    chosen = rules.get("chosen") or {}
    radius_nm = float(
        body.radius_nm
        if body.radius_nm is not None
        else chosen.get("marinas.waypoint_radius_nm", {}).get("value")
        or settings.get("marina_search_radius_nm")
        or 10.0
    )
    step = float(
        body.corridor_step_nm
        if body.corridor_step_nm is not None
        else chosen.get("marinas.corridor_step_nm", {}).get("value")
        or 25.0
    )
    rad = float(
        body.corridor_radius_nm
        if body.corridor_radius_nm is not None
        else chosen.get("marinas.corridor_radius_nm", {}).get("value")
        or 25.0
    )
    return rules, radius_nm, step, rad


async def _persist_marina_run(kind: str, rules: dict, extra: dict, *,
                              resume: bool = False, settings: dict | None = None,
                              rules_overrides: dict | None = None,
                              label: str | None = None) -> dict:
    from app.services import isolated_runs
    opened = await isolated_runs.open_run(
        db, "marinas", kind=kind, label=label or f"marinas-{kind}",
        settings=settings or {}, extra_params=extra,
        profile=rules.get("profile"),
        rules_overrides=rules_overrides,
        resume=resume,
    )
    return opened


async def _chain_marinas_enrich_and_anchorages(
        scope: str, *, from_scratch: bool = False) -> None:
    """After the dump: priority enrichment then anchorages.

    Test: 10 enriched cards + the Bay of Biscay tile.
    Full from scratch: everything (including already enriched) + cursor-free anchorages.
    Anchorages write the public map (`db.anchorages`, upsert).
    """
    try:
        if not ENRICH_BATCH_STATE.running:
            MARINA_BUILD_STATE.log(
                "Chaînage — enrichissement par priorité"
                + (" (from scratch)" if from_scratch else ""))
            await marina_enrich_batch(
                MarinaEnrichBatchBody(
                    limit=10 if scope == "test" else 0,
                    include_enriched=bool(from_scratch and scope == "full"),
                ))
    except HTTPException:
        pass
    except Exception as exc:
        MARINA_BUILD_STATE.log(
            f"enrich chaîné: {type(exc).__name__}: {str(exc)[:60]}")

    if ANCHORAGE_BUILD_STATE.running:
        return
    try:
        MARINA_BUILD_STATE.log(
            "Chaînage — dump mouillages "
            + ("(tuile test)" if scope == "test" else "monde")
            + (" from scratch" if from_scratch else ""))
        if from_scratch and scope == "full":
            try:
                await db.anchorages.delete_many({})
            except Exception:
                pass
        await run_build_world_anchorages(
            anchorages_coll=db.anchorages,
            cursor_coll=(
                db.anchorage_test_cursors if scope == "test"
                else db.anchorage_world_cursors),
            state=ANCHORAGE_BUILD_STATE,
            route_path=ROUTE_FILE,
            resume=(scope != "test") and not from_scratch,
            tiles=(TEST_TILE,) if scope == "test" else None,
        )
    except Exception as exc:
        MARINA_BUILD_STATE.log(
            f"mouillages chaînés: {type(exc).__name__}: {str(exc)[:60]}")


@router.post("/marinas/build")
async def marinas_build_start(body: MarinasBuildBody | None = None):
    if MARINA_BUILD_STATE.running:
        raise HTTPException(409, "A marinas build is already running")
    body = body or MarinasBuildBody()
    settings = await get_settings()
    rules, _radius_nm, _step, _rad = await _marina_rules_and_radii(body, settings)

    if body.clear_before:
        raise HTTPException(400, "clear_before is forbidden (shared.no_purge)")

    from app.services import isolated_runs
    try:
        tiles = isolated_runs.parse_osm_tiles(body.tiles)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    scope = (body.scope or "full").lower()
    scratch = resolve_from_scratch(body.from_scratch, scope=scope)
    resume = bool(body.resume) and not scratch and scope != "test"

    opened = await _persist_marina_run("marinas_world", rules, {
        "resume": resume,
        "from_scratch": scratch,
        "profile": rules.get("profile"),
        "kind": "world_leisure_marina",
        "tiles": [list(t) for t in tiles] if tiles else None,
    }, resume=resume, settings=settings, rules_overrides=body.rules,
        label=body.label)
    run_id = opened["run_id"]
    recorder = opened["recorder"]
    MARINA_BUILD_STATE.run_id = run_id
    from app.services.isolated_runs import reset_run as _reset_iso
    from app.core.run_rules import bind_rules, reset_rules
    _reset_iso(opened["token"])
    test_tiles = (TEST_TILE,) if scope == "test" else None
    maps_after = bool(body.maps_place_after or scratch)

    async def _runner():
        from app.services import isolated_runs
        rules_token = bind_rules(rules)
        try:
            await run_build_world_marinas(
                marinas_coll=db.marina_run_marinas,
                cursor_coll=db.marina_run_cursors,
                state=MARINA_BUILD_STATE,
                resume=resume,
                tiles=test_tiles or tiles,
                run_id=run_id,
                recorder=recorder,
            )
            extra = dict(MARINA_BUILD_STATE.summary or {})
            extra["from_scratch"] = scratch
            if (maps_after and not MAPS_PLACE_STATE.running
                    and not MARINA_BUILD_STATE.cancel):
                MARINA_BUILD_STATE.log(
                    "Dump terminé — résolution des fiches Google /place/"
                    + (" (force)" if scratch else ""))
                await resolve_maps_places(
                    marinas_coll=db.marina_run_marinas,
                    state=MAPS_PLACE_STATE,
                    skip_search=True,
                    force=scratch,
                    extra_filter={"run_id": run_id},
                    run_id=run_id,
                    dest_db=db,
                )
            if scope == "full" and not getattr(MARINA_BUILD_STATE, "cancel", False):
                extra["promoted"] = await isolated_runs.promote_run_to_live(
                    db, "marinas", run_id, replace=scratch)
            await isolated_runs.finalize_run(
                db, "marinas", run_id, extra=extra,
                cancelled=bool(MARINA_BUILD_STATE.cancel),
            )
            if not getattr(MARINA_BUILD_STATE, "cancel", False):
                await _chain_marinas_enrich_and_anchorages(
                    scope, from_scratch=scratch)
        except Exception as e:
            await isolated_runs.finalize_run(
                db, "marinas", run_id, error=str(e)[:200])
        finally:
            reset_rules(rules_token)

    asyncio.create_task(_runner())
    return {
        "started": True,
        "run_id": run_id,
        "kind": "world_leisure_marina",
        "resume": resume,
        "from_scratch": scratch,
        "maps_place_after": maps_after,
        "profile": rules.get("profile"),
        "rules_hash": rules.get("hash"),
        "wrote_marinas": False,
        "tiles": [list(t) for t in tiles] if tiles else None,
        "label": body.label,
    }


@router.get("/marinas/build/status")
async def marinas_build_status():
    s = MARINA_BUILD_STATE
    return {
        "running": s.running,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "progress": s.progress,
        "total": s.total,
        "summary": s.summary,
        "error": s.error,
        "logs_tail": s.logs[-LOGS_TAIL_N:],
        "run_id": s.run_id,
        "wrote_marinas": False,
        "cancelling": bool(s.cancel and s.running),
    }


@router.post("/marinas/build/cancel")
async def marinas_build_cancel():
    if not MARINA_BUILD_STATE.running:
        raise HTTPException(409, "No marinas build is running")
    MARINA_BUILD_STATE.cancel = True
    MARINA_BUILD_STATE.log("Stop demandé")
    return {"cancelling": True}


@router.get("/marinas/count")
async def marinas_count():
    async def _count(q: dict):
        try:
            return await db.marinas.count_documents(q, maxTimeMS=4000)
        except Exception:
            return None

    async def _est():
        try:
            return await db.marinas.estimated_document_count()
        except Exception:
            return None

    total, named, with_website, osm, shom, curated, enriched, place = await asyncio.gather(
        _est(),
        _count({"name": {"$nin": ["", None]}}),
        _count({"website": {"$nin": ["", None]}}),
        _count({"source": "openstreetmap"}),
        _count({"source": "shom"}),
        _count({"source": "curated"}),
        _count({"enriched": True}),
        _count({"maps_place_status": "found"}),
    )
    return {
        "total": total,
        "named": named,
        "with_website": with_website,
        "by_source": {
            "openstreetmap": osm,
            "shom": shom,
            "curated": curated,
        },
        "enriched": enriched,
        "with_google_place": place,
    }


# ---------- Anchorages (Phase 8 — Mouillages) ----------

class AnchoragesBuildBody(BaseModel):
    radius_nm: float | None = None
    clear_before: bool = False
    include_corridor: bool = False
    corridor_step_nm: float | None = None
    corridor_radius_nm: float | None = None
    profile: str | None = None
    rules: dict | None = None
    # "full" (default) = tiled world dump · "test" = Biscay tile ·
    # "corridor" = legacy waypoints + route band (isolated run).
    scope: str | None = None


@router.get("/anchorages")
async def list_anchorages(
    priority: int | None = None,
    anchorage_type: str | None = None,
):
    q: dict = {}
    if priority is not None:
        q["priority"] = int(priority)
    if anchorage_type:
        q["anchorage_type"] = anchorage_type
    docs = await db.anchorages.find(q).sort([("priority", 1), ("name", 1)]).to_list(20000)
    return anchorages_to_geojson(docs)


@router.get("/export/anchorages.geojson")
async def export_anchorages():
    from app.core.export_meta import export_response
    docs = await db.anchorages.find({}).sort([("priority", 1), ("name", 1)]).to_list(20000)
    fc = anchorages_to_geojson(docs)
    return export_response(fc, "anchorages", "anchorages.geojson",
                           license_note="© OpenStreetMap contributors (ODbL)")


@router.post("/anchorages/build")
async def anchorages_build_start(body: AnchoragesBuildBody | None = None):
    if ANCHORAGE_BUILD_STATE.running:
        raise HTTPException(409, "An anchorages build is already running")
    body = body or AnchoragesBuildBody()
    scope = (body.scope or "full").lower()
    if scope != "corridor":
        # 2026-09 — tiled world dump: no more 25 NM corridor.
        # Write the public map (db.anchorages), upsert by dedup_key.
        if body.clear_before:
            raise HTTPException(400, "clear_before is forbidden (shared.no_purge)")
        ANCHORAGE_BUILD_STATE.run_id = None

        async def _world_runner():
            try:
                await run_build_world_anchorages(
                    anchorages_coll=db.anchorages,
                    cursor_coll=(
                        db.anchorage_test_cursors if scope == "test"
                        else db.anchorage_world_cursors),
                    state=ANCHORAGE_BUILD_STATE,
                    route_path=ROUTE_FILE,
                    resume=scope != "test",
                    tiles=(TEST_TILE,) if scope == "test" else None,
                )
            except Exception:
                pass

        asyncio.create_task(_world_runner())
        return {
            "started": True,
            "scope": scope,
            "world": True,
            "wrote_marinas": False,
            "wrote_anchorages": True,
        }
    settings = await get_settings()
    extra = {}
    if body.corridor_step_nm is not None:
        extra["marinas.corridor_step_nm"] = body.corridor_step_nm
    if body.corridor_radius_nm is not None:
        extra["marinas.corridor_radius_nm"] = body.corridor_radius_nm
    if body.radius_nm is not None:
        extra["marinas.waypoint_radius_nm"] = body.radius_nm
    rules, radius_nm, step, rad = await _marina_rules_and_radii(body, settings, extra)

    if body.clear_before:
        raise HTTPException(400, "clear_before is forbidden (shared.no_purge)")

    opened = await _persist_marina_run("anchorages", rules, {
        "radius_nm": radius_nm,
        "include_corridor": body.include_corridor,
        "corridor_step_nm": step,
        "corridor_radius_nm": rad,
        "profile": rules.get("profile"),
    }, settings=settings, rules_overrides=body.rules)
    run_id = opened["run_id"]
    ANCHORAGE_BUILD_STATE.run_id = run_id
    from app.services.isolated_runs import reset_run as _reset_iso
    from app.core.run_rules import bind_rules, reset_rules
    _reset_iso(opened["token"])

    async def _runner():
        from app.services import isolated_runs
        rules_token = bind_rules(rules)
        try:
            await run_build_anchorages(
                anchorages_coll=db.marina_run_anchorages,
                route_path=ROUTE_FILE,
                radius_nm=radius_nm,
                state=ANCHORAGE_BUILD_STATE,
                include_corridor=body.include_corridor,
                corridor_step_nm=step,
                corridor_radius_nm=rad,
                run_id=run_id,
            )
            await isolated_runs.finalize_run(
                db, "marinas", run_id, extra=ANCHORAGE_BUILD_STATE.summary)
        except Exception as e:
            await isolated_runs.finalize_run(
                db, "marinas", run_id, error=str(e)[:200])
        finally:
            reset_rules(rules_token)

    asyncio.create_task(_runner())
    return {
        "started": True,
        "run_id": run_id,
        "radius_nm": radius_nm,
        "include_corridor": body.include_corridor,
        "corridor_step_nm": step,
        "corridor_radius_nm": rad,
        "corridor_band_nm": rad * 2,
        "profile": rules.get("profile"),
        "rules_hash": rules.get("hash"),
        "wrote_marinas": False,
        "wrote_anchorages": False,
    }


@router.post("/anchorages/build/cancel")
async def anchorages_build_cancel():
    if not ANCHORAGE_BUILD_STATE.running:
        raise HTTPException(409, "No anchorages build is running")
    ANCHORAGE_BUILD_STATE.cancel = True
    ANCHORAGE_BUILD_STATE.log("Stop demandé")
    return {"cancelling": True}


@router.get("/anchorages/build/status")
async def anchorages_build_status():
    s = ANCHORAGE_BUILD_STATE
    return {
        "running": s.running,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "progress": s.progress,
        "total": s.total,
        "summary": s.summary,
        "error": s.error,
        "logs_tail": s.logs[-LOGS_TAIL_N:],
        "run_id": s.run_id,
        "wrote_marinas": False,
    }


@router.get("/anchorages/count")
async def anchorages_count():
    return {
        "total": await db.anchorages.count_documents({}),
        "by_priority": {
            "1": await db.anchorages.count_documents({"priority": 1}),
            "2": await db.anchorages.count_documents({"priority": 2}),
            "3": await db.anchorages.count_documents({"priority": 3}),
        },
        "by_type": {
            "anchorage": await db.anchorages.count_documents({"anchorage_type": "anchorage"}),
            "anchor_berth": await db.anchorages.count_documents({"anchorage_type": "anchor_berth"}),
            "bay": await db.anchorages.count_documents({"anchorage_type": "bay"}),
        },
    }


# ---------- Marina enrichment (Phase 3) ----------
# Per-id lock so the SAME marina can't be enriched concurrently.
ENRICH_LOCKS: set[str] = set()
# Per-id state registry for on-demand tasks (marinas + projects). Kept in-memory,
# TTL-cleaned when a new task starts.
MARINA_ENRICH_TASKS: dict[str, dict] = {}




class MarinaEnrichBatchBody(BaseModel):
    limit: int = 10   # 0 = all remaining marinas ("Chain all" mode)
    priority: int | None = None
    include_enriched: bool = False   # if True, re-enrich already-enriched ones
    stale_only: bool = False   # if True, filter to stale-only (needs enriched_at)
    profile: str | None = None
    rules: dict | None = None


ENRICH_BATCH_STATE = TaskState(max_logs=400)
MAPS_PLACE_STATE = TaskState(max_logs=400)


class MapsPlaceBody(BaseModel):
    limit: int = 0
    force: bool = False
    skip_search: bool = True
    profile: str | None = None
    rules: dict | None = None


_MARINA_ENGINE_LABELS = {
    "tinyfish": "TinyFish Agent",
    "fetch": "Fetch / regex",
    "nvidia": "NVIDIA NIM",
    "nvidia-deepseek": "NVIDIA DeepSeek",
    "nvidia-muse": "NVIDIA Muse",
    "nvidia-gpt-oss": "NVIDIA gpt-oss",
    "openrouter": "OpenRouter",
    "tags": "OSM tags",
    "fallback": "OSM Fallback",
}


async def _marina_telemetry(marina: dict, status: str, duration_ms: float, engine: str, results: int, detail: str):
    """Telemetry row tagged dataset=marinas so the Audit table tracks each enrichment."""
    tags = marina.get("tags") or {}
    target = tags.get("website") or tags.get("contact:website") or tags.get("url") or f"marina:{marina.get('name')}"
    await db.telemetry.insert_one({
        "_id": str(uuid.uuid4()), "url": target, "engine": engine, "status": status,
        "duration_ms": int(duration_ms), "results": results, "detail": str(detail)[:500],
        "ts": now_iso(), "dataset": "marinas",
    })


async def _run_marina_enrich_one(marina: dict, min_credit_usd: float, log_fn,
                                skip_tinyfish: bool = False, run_id: str | None = None) -> dict:
    """Run the enrichment chain. `run_id` → marina_run_marinas ; sinon carte live (fiche unitaire)."""
    settings = await get_settings()
    tf_key = (settings.get("tinyfish_api_key") or os.environ.get("TINYFISH_API_KEY") or "").strip() or None
    or_key = get_llm_key(settings) or None
    # Credit saving: after a TinyFish failure, do not pay again for this marina.
    skip_tf = skip_tinyfish or bool(marina.get("tinyfish_failed"))
    t0 = time.time()
    try:
        result = await enrich_marina(
            marina,
            tinyfish_key=tf_key,
            openrouter_key=or_key,
            min_credit_usd=min_credit_usd,
            logger=log_fn,
            skip_tinyfish=skip_tf,
            settings=settings,
        )
    except Exception as e:
        await _marina_telemetry(marina, "FAILED", (time.time() - t0) * 1000, "Enrichment", 0,
                                f"{marina.get('name')} — {type(e).__name__}: {e}")
        raise
    tf_attempted = result.pop("_tinyfish_attempted", False)
    update = {**result, "stale": False,
              "enrich_attempts": int(marina.get("enrich_attempts") or 0) + 1}
    if tf_attempted and result.get("enrichment_source") != "tinyfish":
        update["tinyfish_failed"] = True
    if run_id:
        from app.services.isolated_runs import write_item
        await write_item(
            db, "marinas", run_id, {**marina, **update},
            source_id=marina.get("source_id") or marina.get("osm_id") or marina.get("_id"),
        )
    else:
        await db.marinas.update_one({"_id": marina["_id"]}, {"$set": update})
        mark_marinas_fc_stale()
    src = result.get("enrichment_source") or ""
    filled = [k for k in ENRICH_FIELDS if result.get(k) is not None]
    await _marina_telemetry(
        marina,
        "SUCCESS" if result.get("enriched") else "FAILED",
        (time.time() - t0) * 1000,
        _MARINA_ENGINE_LABELS.get(src, src or "?"),
        len(filled),
        f"{marina.get('name')} — champs: {', '.join(filled) or 'aucun'}",
    )
    return result


@router.post("/marinas/{marina_id}/enrich", status_code=202)
async def marina_enrich_one(marina_id: str):
    """
    Start marina enrichment as a background task and return 202 immediately.
    Poll GET /api/marinas/{marina_id}/enrich/status for progress/result.
    """
    if marina_id in ENRICH_LOCKS:
        raise HTTPException(409, "Enrichment already in progress for this marina")
    marina = await db.marinas.find_one({"_id": marina_id})
    if not marina:
        raise HTTPException(404, "marina not found")
    settings = await get_settings()
    min_credit = float(settings.get("openrouter_min_credits_usd") or 0.5)

    prune_tasks(MARINA_ENRICH_TASKS)
    ENRICH_LOCKS.add(marina_id)
    MARINA_ENRICH_TASKS[marina_id] = new_task()

    async def _runner():
        task = MARINA_ENRICH_TASKS[marina_id]

        def log_fn(msg: str):
            task["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(task["logs"]) > 200:
                task["logs"] = task["logs"][-200:]

        try:
            await _run_marina_enrich_one(marina, min_credit, log_fn)
            fresh = await db.marinas.find_one({"_id": marina_id})
            task["result"] = {
                k: fresh.get(k) for k in (
                    "_id", "name", "lat", "lon", "source", "priority",
                    "nearest_waypoint", "tags", "osm_id", "enriched",
                    "enrichment_source", "enriched_at", "stale",
                    *ENRICH_FIELDS,
                )
            }
            task["state"] = "done"
        except Exception as e:
            task["error"] = f"{type(e).__name__}: {e}"
            task["state"] = "error"
        finally:
            task["finished_at"] = time.time()
            ENRICH_LOCKS.discard(marina_id)

    asyncio.create_task(_runner())
    return {"status": "started", "marina_id": marina_id}


@router.get("/marinas/{marina_id}/enrich/status")
async def marina_enrich_status(marina_id: str):
    """Poll the state of an on-demand marina enrichment. Returns 'idle' if no task."""
    task = MARINA_ENRICH_TASKS.get(marina_id)
    if not task:
        return {"state": "idle", "marina_id": marina_id}
    return {
        "state": task["state"],
        "marina_id": marina_id,
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
        "result": task["result"],
        "error": task["error"],
        "logs_tail": task["logs"][-30:],
    }


@router.post("/marinas/enrich-batch")
async def marina_enrich_batch(body: MarinaEnrichBatchBody | None = None):
    if ENRICH_BATCH_STATE.running:
        raise HTTPException(409, "A marinas enrichment batch is already running")
    body = body or MarinaEnrichBatchBody()
    settings = await get_settings()
    concurrency = int(settings.get("marina_batch_concurrency") or 2)
    min_credit = float(settings.get("openrouter_min_credits_usd") or 0.5)
    stale_days = int(settings.get("enrich_stale_days") or 365)

    # Selection query
    q: dict = {}
    if body.priority is not None:
        q["priority"] = int(body.priority)
    if body.stale_only:
        # cutoff timestamp
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - stale_days * 86400),
        )
        q["$or"] = [{"enriched_at": {"$lt": cutoff}}, {"enriched": {"$ne": True}}]
    elif not body.include_enriched:
        q["$or"] = [{"enriched": {"$ne": True}}, {"enriched": False}]
        # Credit saving: after 2 failed attempts, the marina leaves automatic
        # batches (still re-runnable one-off via its Enrich button).
        q["enrich_attempts"] = {"$not": {"$gte": 2}}

    limit = int(body.limit or 0)
    candidates = await db.marinas.find(q).sort([("priority", 1), ("name", 1)]).to_list(limit if limit > 0 else None)

    from app.core.run_rules import RuleError, snapshot_for_run
    try:
        rules = snapshot_for_run(
            mode="marinas", settings=settings,
            overrides=body.rules, profile=body.profile)
    except RuleError as e:
        raise HTTPException(400, str(e)) from e
    opened = await _persist_marina_run(
        "enrich", rules, {"limit": body.limit, "priority": body.priority},
        settings=settings, rules_overrides=body.rules)
    run_id = opened["run_id"]
    ENRICH_BATCH_STATE.run_id = run_id
    from app.services.isolated_runs import reset_run as _reset_iso
    _reset_iso(opened["token"])

    ENRICH_BATCH_STATE.running = True
    ENRICH_BATCH_STATE.started_at = time.time()
    ENRICH_BATCH_STATE.finished_at = None
    ENRICH_BATCH_STATE.progress = 0
    ENRICH_BATCH_STATE.total = len(candidates)
    ENRICH_BATCH_STATE.results = []
    ENRICH_BATCH_STATE.logs = []
    ENRICH_BATCH_STATE.error = None
    ENRICH_BATCH_STATE.cancel = False

    async def _runner():
        from app.core.run_rules import bind_rules
        rules_token = bind_rules(rules)
        try:
            ENRICH_BATCH_STATE.log(
                f"Selected {len(candidates)} marinas (concurrency={concurrency}, "
                f"limit={body.limit}, priority={body.priority}, min_credit=${min_credit})"
            )
            sem = asyncio.Semaphore(max(1, concurrency))
            counter = {"i": 0}

            async def _one(m):
                async with sem:
                    if ENRICH_BATCH_STATE.cancel:
                        ENRICH_BATCH_STATE.log(f"SKIP {m['name']}: batch annulé")
                        return
                    if m["_id"] in ENRICH_LOCKS:
                        ENRICH_BATCH_STATE.log(f"SKIP {m['name']}: already locked")
                        return
                    ENRICH_LOCKS.add(m["_id"])
                    ENRICH_BATCH_STATE.log(f"→ {m['name']} (P{m.get('priority')})")
                    per_logs: list[str] = []
                    try:
                        result = await _run_marina_enrich_one(
                            m, min_credit,
                            lambda s: (per_logs.append(s), ENRICH_BATCH_STATE.log(f"  {s}")),
                            run_id=run_id,
                        )
                        ENRICH_BATCH_STATE.results.append({
                            "id": m["_id"], "name": m["name"],
                            "source": result.get("enrichment_source"),
                            "enriched": result.get("enriched"),
                            "fields_filled": [k for k in ENRICH_FIELDS if result.get(k) is not None],
                        })
                    except Exception as e:
                        ENRICH_BATCH_STATE.log(f"  FAILED: {type(e).__name__}: {e}")
                        ENRICH_BATCH_STATE.results.append({
                            "id": m["_id"], "name": m["name"], "error": f"{type(e).__name__}: {e}",
                        })
                    finally:
                        ENRICH_LOCKS.discard(m["_id"])
                        counter["i"] += 1
                        ENRICH_BATCH_STATE.progress = counter["i"]

            await asyncio.gather(*(_one(m) for m in candidates))
            by_src = {"tinyfish": 0, "openrouter": 0, "fallback": 0}
            for r in ENRICH_BATCH_STATE.results:
                s = r.get("source")
                if s in by_src:
                    by_src[s] += 1
            ENRICH_BATCH_STATE.log(f"Done. Sources: {by_src}")
        except Exception as e:
            ENRICH_BATCH_STATE.error = f"{type(e).__name__}: {e}"
            ENRICH_BATCH_STATE.log(f"FATAL {ENRICH_BATCH_STATE.error}")
        finally:
            from app.services import isolated_runs
            from app.core.run_rules import reset_rules as _reset_rules
            await isolated_runs.finalize_run(
                db, "marinas", run_id,
                cancelled=ENRICH_BATCH_STATE.cancel,
                error=ENRICH_BATCH_STATE.error,
                extra={"results": len(ENRICH_BATCH_STATE.results)},
            )
            ENRICH_BATCH_STATE.finished_at = time.time()
            ENRICH_BATCH_STATE.running = False
            _reset_rules(rules_token)

    asyncio.create_task(_runner())
    return {
        "started": True, "selected": len(candidates), "concurrency": concurrency,
        "run_id": run_id, "wrote_marinas": False,
        "profile": rules.get("profile"), "rules_hash": rules.get("hash"),
    }


@router.post("/marinas/enrich-batch/cancel")
async def marina_enrich_batch_cancel():
    """Stop the running enrichment batch — remaining marinas are not launched."""
    if not ENRICH_BATCH_STATE.running:
        raise HTTPException(409, "No enrichment batch is running")
    ENRICH_BATCH_STATE.cancel = True
    ENRICH_BATCH_STATE.log("⛔ Stop demandé — les marinas restantes ne seront pas lancées")
    return {"cancelling": True}


@router.get("/marinas/enrich-batch/status")
async def marina_enrich_batch_status():
    s = ENRICH_BATCH_STATE
    return {
        "running": s.running,
        "cancelling": s.cancel,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "progress": s.progress,
        "total": s.total,
        "results": s.results,
        "logs_tail": s.logs[-60:],
        "error": s.error,
        "run_id": s.run_id,
        "wrote_marinas": False,
    }


@router.post("/marinas/maps-place")
async def marinas_maps_place_start(body: MapsPlaceBody | None = None):
    if MAPS_PLACE_STATE.running:
        raise HTTPException(409, "A Google-place resolve is already running")
    body = body or MapsPlaceBody()
    settings = await get_settings()
    from app.core.run_rules import RuleError, snapshot_for_run
    try:
        rules = snapshot_for_run(
            mode="marinas", settings=settings,
            overrides=body.rules, profile=body.profile)
    except RuleError as e:
        raise HTTPException(400, str(e)) from e
    opened = await _persist_marina_run(
        "maps_place", rules,
        {"limit": body.limit, "force": body.force, "skip_search": body.skip_search},
        settings=settings, rules_overrides=body.rules)
    run_id = opened["run_id"]
    MAPS_PLACE_STATE.run_id = run_id
    from app.services.isolated_runs import reset_run as _reset_iso
    _reset_iso(opened["token"])

    async def _runner():
        from app.services import isolated_runs
        from app.core.run_rules import bind_rules, reset_rules
        rules_token = bind_rules(rules)
        try:
            await resolve_maps_places(
                marinas_coll=db.marinas,
                state=MAPS_PLACE_STATE,
                limit=int(body.limit or 0),
                force=bool(body.force),
                skip_search=bool(body.skip_search),
                run_id=run_id,
                dest_db=db,
            )
            mark_marinas_fc_stale()
            await isolated_runs.finalize_run(
                db, "marinas", run_id, extra=MAPS_PLACE_STATE.summary)
        except Exception as exc:
            MAPS_PLACE_STATE.error = f"{type(exc).__name__}: {exc}"
            await isolated_runs.finalize_run(
                db, "marinas", run_id, error=MAPS_PLACE_STATE.error)
        finally:
            reset_rules(rules_token)

    asyncio.create_task(_runner())
    return {
        "started": True,
        "limit": body.limit,
        "force": body.force,
        "skip_search": body.skip_search,
        "run_id": run_id,
        "wrote_marinas": False,
        "profile": rules.get("profile"),
        "rules_hash": rules.get("hash"),
    }


@router.post("/marinas/maps-place/cancel")
async def marinas_maps_place_cancel():
    if not MAPS_PLACE_STATE.running:
        raise HTTPException(409, "No Google-place resolve is running")
    MAPS_PLACE_STATE.cancel = True
    MAPS_PLACE_STATE.log("Stop demandé")
    return {"cancelling": True}


@router.get("/marinas/maps-place/status")
async def marinas_maps_place_status():
    s = MAPS_PLACE_STATE
    return {
        "running": s.running,
        "cancelling": s.cancel,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "progress": s.progress,
        "total": s.total,
        "summary": s.summary,
        "error": s.error,
        "logs_tail": s.logs[-LOGS_TAIL_N:],
        "run_id": s.run_id,
        "wrote_marinas": False,
    }


@router.get("/marinas/runs")
async def marinas_runs_list():
    from app.services import isolated_runs
    items = await isolated_runs.list_meta_runs(db, "marinas")
    return {
        "count": len(items),
        "wrote_marinas": False,
        "items": items,
    }


@router.get("/marinas/runs/{run_id}")
async def marinas_run_detail(run_id: str):
    from app.services import isolated_runs
    doc = await isolated_runs.get_meta_run(db, "marinas", run_id)
    if not doc:
        raise HTTPException(404, f"Run {run_id} unknown")
    return {**doc, "wrote_marinas": False}


@router.get("/marinas/runs/{run_id}/events")
async def marinas_run_events(run_id: str, step: str | None = None,
                             skip: int = 0, limit: int = 500):
    from app.services import isolated_runs
    if not await db.marina_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    return await isolated_runs.list_run_events(
        db, "marinas", run_id, step=step, skip=skip, limit=limit)


@router.get("/marinas/runs/{run_id}/geojson")
async def marinas_run_geojson(run_id: str):
    """Run FeatureCollection — map run selector.

    Historical live dumps (`wrote_marinas=true`) have no isolated
    items: then serve the live collection they wrote.
    """
    meta = await db.marina_runs.find_one({"_id": run_id})
    if not meta:
        raise HTTPException(404, f"Run {run_id} unknown")
    docs = await db.marina_run_marinas.find(
        {"run_id": run_id}, SLIM_PROJECTION).to_list(50000)
    live_fallback = False
    if not docs and meta.get("wrote_marinas"):
        docs = await _all_marinas()
        live_fallback = True
    fc = marinas_to_slim_geojson(docs)
    fc["run_id"] = run_id
    fc["live_fallback"] = live_fallback
    return fc
