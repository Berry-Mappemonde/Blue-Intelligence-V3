"""app.routers.capitaineries — OSM harbour_master dump + SHOM / NOAA overlays
(BUISGL FUNCTN=2), phone / VHF enrichment."""
import asyncio
import os
import time
import uuid

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app.core.from_scratch import resolve_from_scratch
from app.core.llm import get_llm_key
from app.core.tasks import BuildState, TaskState, new_task, prune_tasks, LOGS_TAIL_N
from app.db import db, get_settings
from app.services.capitainerie_enrich import (
    ENRICH_FIELDS,
    enrich_capitainerie,
    rank_enrich_candidates,
)
from app.services.capitainerie_world import (
    SLIM_PROJECTION,
    build_world_capitaineries as run_build,
    to_slim_geojson,
)
from app.services.marina_world import official_website
from app.services.swarm_pipeline import now_iso
from app.services.geojson_import import empty_import_result, parse_feature_collection
from app.services.osm_seeds import TEST_TILE

router = APIRouter(prefix="/api")

BUILD_STATE = BuildState()
ENRICH_BATCH_STATE = TaskState(max_logs=400)
ENRICH_LOCKS: set[str] = set()
ENRICH_TASKS: dict[str, dict] = {}


async def _all_docs(q: dict | None = None, projection: dict | None = None) -> list[dict]:
    cur = db.capitaineries.find(q or {}, projection or SLIM_PROJECTION).sort("name", 1)
    return [doc async for doc in cur]


@router.post("/import/capitaineries.geojson")
async def import_capitaineries_geojson(fc: dict = Body(...)):
    kind, feats = parse_feature_collection(fc)
    if kind == "invalid":
        raise HTTPException(400, "invalid GeoJSON FeatureCollection")
    if kind == "empty":
        return empty_import_result(
            "total_capitaineries", await db.capitaineries.count_documents({}))
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
            shom_id = p.get("shom_id")
            noaa_id = p.get("noaa_id")
            if not name and not osm_id and not shom_id and not noaa_id:
                invalid += 1
                continue
            mid = str(p.get("id") or osm_id or shom_id or noaa_id or "").strip() or str(uuid.uuid4())
            website = p.get("website") or official_website({"tags": p.get("tags") or {}})
            doc = {
                "_id": mid,
                "name": name,
                "lat": lat,
                "lon": lon,
                "source": p.get("source") or "openstreetmap",
                "sources": p.get("sources") or [p.get("source") or "openstreetmap"],
                "tags": p.get("tags") or {},
                "osm_id": osm_id,
                "shom_id": shom_id,
                "noaa_id": noaa_id,
                "website": website,
                "website_status": p.get("website_status") or ("unchecked" if website else None),
                "website_source": p.get("website_source") or ("osm_tag" if website else None),
                "telephone": p.get("telephone"),
                "canal_vhf": p.get("canal_vhf"),
                "enriched": bool(p.get("enriched")),
                "enrichment_source": p.get("enrichment_source"),
                "enriched_at": p.get("enriched_at"),
                "stale": bool(p.get("stale")),
                "fetched_at": p.get("fetched_at") or now_iso(),
            }
            existing = await db.capitaineries.find_one({"_id": mid})
            if existing:
                await db.capitaineries.update_one({"_id": mid}, {"$set": doc})
                updated += 1
            else:
                await db.capitaineries.insert_one(doc)
                imported += 1
        except Exception:
            invalid += 1
    total = await db.capitaineries.count_documents({})
    return {
        "imported": imported, "merged": updated, "skipped_existing": 0,
        "invalid": invalid, "total_capitaineries": total,
    }


@router.get("/capitaineries")
async def list_capitaineries(source: str | None = None, visible: bool = False,
                             review: bool = False):
    q: dict = {}
    if source:
        q["source"] = source
    docs = await _all_docs(q)
    if not docs and not (visible or review):
        last = await db.capitainerie_runs.find_one(
            {"state": "done"},
            sort=[("finished_at", -1), ("created_at", -1)],
        )
        if last:
            cur = db.capitainerie_run_sites.find(
                {"run_id": last["_id"]}, SLIM_PROJECTION or {}
            ).sort("name", 1)
            docs = [d async for d in cur]
    if visible or review:
        from app.services.review_gold import filter_visible
        docs = await filter_visible(
            db, "capitainerie", docs, lambda c: c.get("_id"))
    return to_slim_geojson(docs)


@router.get("/noaa/aids")
async def noaa_aids(bbox: str = ""):
    """ENC Direct lights / buoys. Bbox outside US waters → empty collection."""
    from app.services import noaa_enc
    parsed = noaa_enc.parse_bbox(bbox)
    if not parsed or not noaa_enc.bbox_intersects_us(parsed):
        return noaa_enc.aids_geojson([])
    import httpx
    async with httpx.AsyncClient() as client:
        docs = await noaa_enc.fetch_aids(client, parsed)
    return noaa_enc.aids_geojson(docs)


@router.get("/export/noaa-aids.geojson")
async def export_noaa_aids(bbox: str = "-80,24,-66,45"):
    from app.core.export_meta import export_response
    from app.services import noaa_enc
    parsed = noaa_enc.parse_bbox(bbox) or (-80.0, 24.0, -66.0, 45.0)
    if not noaa_enc.bbox_intersects_us(parsed):
        fc = noaa_enc.aids_geojson([])
        return export_response(fc, "noaa-aids", "noaa-aids.geojson",
                               license_note=noaa_enc.LICENSE)
    import httpx
    async with httpx.AsyncClient() as client:
        docs = await noaa_enc.fetch_aids(client, parsed)
    fc = noaa_enc.aids_geojson(docs)
    return export_response(fc, "noaa-aids", "noaa-aids.geojson",
                           license_note=noaa_enc.LICENSE)


@router.get("/export/capitaineries.geojson")
async def export_capitaineries():
    from app.core.export_meta import export_response
    docs = await _all_docs({})
    fc = to_slim_geojson(docs)
    return export_response(
        fc, "capitaineries", "capitaineries.geojson",
        license_note=("© OpenStreetMap contributors (ODbL) · SHOM INFORMATIONS_PORTUAIRES "
                      "(Licence Ouverte Etalab) · NOAA ENC Direct to GIS"),
    )


class BuildBody(BaseModel):
    clear_before: bool = False
    resume: bool = True
    from_scratch: bool | None = None
    profile: str | None = None
    rules: dict | None = None
    scope: str | None = None
    tiles: list[list[float]] | None = None
    skip_shom: bool = False
    skip_noaa: bool = False
    label: str | None = None


@router.post("/capitaineries/build")
async def capitaineries_build_start(body: BuildBody | None = None):
    if BUILD_STATE.running:
        raise HTTPException(409, "A capitaineries build is already running")
    body = body or BuildBody()
    if body.clear_before:
        raise HTTPException(400, "clear_before is forbidden (shared.no_purge)")

    settings = await get_settings()
    from app.core.run_rules import RuleError, bind_rules, reset_rules, snapshot_for_run
    from app.services import isolated_runs
    try:
        rules = snapshot_for_run(
            mode="capitaineries", settings=settings,
            overrides=body.rules, profile=body.profile)
    except RuleError as e:
        raise HTTPException(400, str(e)) from e
    try:
        tiles = isolated_runs.parse_osm_tiles(body.tiles)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    scope = (body.scope or "full").lower()
    scratch = resolve_from_scratch(body.from_scratch, scope=scope)
    resume = bool(body.resume) and not scratch and scope != "test"
    opened = await isolated_runs.open_run(
        db, "capitaineries", kind="world_harbour_master",
        label=body.label or "capitaineries-world", settings=settings,
        extra_params={
            "resume": resume, "from_scratch": scratch,
            "profile": rules.get("profile"),
            "skip_shom": body.skip_shom, "skip_noaa": body.skip_noaa,
            "tiles": body.tiles,
        },
        rules_overrides=body.rules, profile=body.profile,
        resume=resume,
    )
    run_id = opened["run_id"]
    recorder = opened["recorder"]
    BUILD_STATE.run_id = run_id
    isolated_runs.reset_run(opened["token"])
    test_tiles = (TEST_TILE,) if scope == "test" else None

    async def _runner():
        rules_token = bind_rules(rules)
        try:
            await run_build(
                coll=db.capitainerie_run_sites,
                cursor_coll=db.capitainerie_run_cursors,
                state=BUILD_STATE,
                resume=resume,
                tiles=test_tiles or tiles,
                skip_shom=scope == "test" or body.skip_shom,
                skip_noaa=scope == "test" or body.skip_noaa,
                run_id=run_id,
                recorder=recorder,
            )
            extra = dict(BUILD_STATE.summary or {})
            extra["from_scratch"] = scratch
            if scope == "full" and not BUILD_STATE.cancel:
                extra["promoted"] = await isolated_runs.promote_run_to_live(
                    db, "capitaineries", run_id, replace=scratch)
            await isolated_runs.finalize_run(
                db, "capitaineries", run_id, extra=extra,
                cancelled=bool(BUILD_STATE.cancel))
            # Full: "world dump, then enrichment" — by priority,
            # stoppable, keeps OpenRouter credits in the endpoint.
            if scope == "full" and not BUILD_STATE.cancel and not ENRICH_BATCH_STATE.running:
                try:
                    BUILD_STATE.log(
                        "Chaînage — enrichissement par priorité"
                        + (" (from scratch)" if scratch else ""))
                    await enrich_batch(EnrichBatchBody(
                        limit=0, include_enriched=scratch))
                except HTTPException:
                    pass
                except Exception as chain_exc:
                    BUILD_STATE.log(
                        f"enrich chaîné: {type(chain_exc).__name__}: {str(chain_exc)[:60]}")
        except Exception as exc:
            BUILD_STATE.log(f"build crashed: {type(exc).__name__}: {exc}")
            await isolated_runs.finalize_run(
                db, "capitaineries", run_id, error=str(exc)[:200])
        finally:
            reset_rules(rules_token)

    asyncio.create_task(_runner())
    return {
        "started": True, "kind": "world_harbour_master", "resume": resume,
        "from_scratch": scratch,
        "run_id": run_id, "wrote_capitaineries": False,
        "profile": rules.get("profile"), "rules_hash": rules.get("hash"),
        "skip_shom": body.skip_shom, "skip_noaa": body.skip_noaa,
        "tiles": [list(t) for t in tiles] if tiles else None,
        "label": body.label,
    }


@router.get("/capitaineries/build/status")
async def capitaineries_build_status():
    s = BUILD_STATE
    return {
        "running": s.running,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "progress": s.progress,
        "total": s.total,
        "summary": s.summary,
        "error": s.error,
        "logs_tail": s.logs[-LOGS_TAIL_N:],
        "cancelling": s.cancel and s.running,
        "run_id": s.run_id,
        "wrote_capitaineries": False,
    }


@router.post("/capitaineries/build/cancel")
async def capitaineries_build_cancel():
    if not BUILD_STATE.running:
        raise HTTPException(409, "No capitaineries build is running")
    BUILD_STATE.cancel = True
    BUILD_STATE.log("Stop demandé")
    return {"cancelling": True}


@router.get("/capitaineries/count")
async def capitaineries_count():
    return {
        "total": await db.capitaineries.count_documents({}),
        "named": await db.capitaineries.count_documents({"name": {"$nin": ["", None]}}),
        "with_phone": await db.capitaineries.count_documents({"telephone": {"$nin": ["", None]}}),
        "with_vhf": await db.capitaineries.count_documents({"canal_vhf": {"$nin": ["", None]}}),
        "with_website": await db.capitaineries.count_documents({"website": {"$nin": ["", None]}}),
        "by_source": {
            "openstreetmap": await db.capitaineries.count_documents({"source": "openstreetmap"}),
            "shom": await db.capitaineries.count_documents({"source": "shom"}),
            "osm+shom": await db.capitaineries.count_documents({"source": "osm+shom"}),
            "noaa": await db.capitaineries.count_documents({"source": "noaa"}),
            "osm+noaa": await db.capitaineries.count_documents({"source": "osm+noaa"}),
            "osm+shom+noaa": await db.capitaineries.count_documents({"source": "osm+shom+noaa"}),
        },
        "enriched": await db.capitaineries.count_documents({"enriched": True}),
    }


class EnrichBatchBody(BaseModel):
    limit: int = 10
    include_enriched: bool = False
    skip_tinyfish: bool = False
    profile: str | None = None
    rules: dict | None = None


_ENGINE_LABELS = {
    "tinyfish": "TinyFish Agent",
    "fetch": "TinyFish Fetch",
    "nvidia": "NVIDIA NIM",
    "nvidia-deepseek": "NVIDIA DeepSeek",
    "nvidia-muse": "NVIDIA Muse",
    "nvidia-gpt-oss": "NVIDIA gpt-oss",
    "openrouter": "OpenRouter",
    "tags": "OSM/SHOM/NOAA tags",
}


async def _telemetry(doc: dict, status: str, duration_ms: float, engine: str, results: int, detail: str):
    await db.telemetry.insert_one({
        "_id": str(uuid.uuid4()),
        "url": official_website(doc) or f"capitainerie:{doc.get('name')}",
        "engine": engine, "status": status,
        "duration_ms": int(duration_ms), "results": results,
        "detail": str(detail)[:500], "ts": now_iso(), "dataset": "capitaineries",
    })


async def _run_enrich_one(doc: dict, min_credit_usd: float, log_fn,
                         skip_tinyfish: bool = False, run_id: str | None = None) -> dict:
    settings = await get_settings()
    tf_key = (settings.get("tinyfish_api_key") or os.environ.get("TINYFISH_API_KEY") or "").strip() or None
    or_key = get_llm_key(settings) or None
    skip_tf = skip_tinyfish or bool(doc.get("tinyfish_failed"))
    t0 = time.time()
    try:
        result = await enrich_capitainerie(
            doc,
            tinyfish_key=tf_key,
            openrouter_key=or_key,
            min_credit_usd=min_credit_usd,
            logger=log_fn,
            skip_tinyfish=skip_tf,
            settings=settings,
        )
    except Exception as e:
        await _telemetry(doc, "FAILED", (time.time() - t0) * 1000, "Enrichment", 0,
                         f"{doc.get('name')} — {type(e).__name__}: {e}")
        raise
    tf_attempted = result.pop("_tinyfish_attempted", False)
    update = {**result, "stale": False,
              "enrich_attempts": int(doc.get("enrich_attempts") or 0) + 1}
    if tf_attempted and result.get("enrichment_source") != "tinyfish":
        update["tinyfish_failed"] = True
    if run_id:
        from app.services.isolated_runs import write_item
        await write_item(
            db, "capitaineries", run_id, {**doc, **update},
            source_id=doc.get("source_id") or doc.get("osm_id") or doc.get("_id"),
        )
    else:
        await db.capitaineries.update_one({"_id": doc["_id"]}, {"$set": update})
    src = result.get("enrichment_source") or ""
    filled = [k for k in ENRICH_FIELDS if result.get(k) is not None]
    await _telemetry(
        doc,
        "SUCCESS" if result.get("enriched") else "FAILED",
        (time.time() - t0) * 1000,
        _ENGINE_LABELS.get(src, src or "?"),
        len(filled),
        f"{doc.get('name')} — champs: {', '.join(filled) or 'aucun'}",
    )
    return result


@router.post("/capitaineries/{item_id}/enrich", status_code=202)
async def enrich_one(item_id: str):
    if item_id in ENRICH_LOCKS:
        raise HTTPException(409, "Enrichment already in progress")
    doc = await db.capitaineries.find_one({"_id": item_id})
    if not doc:
        raise HTTPException(404, "capitainerie not found")
    settings = await get_settings()
    min_credit = float(settings.get("openrouter_min_credits_usd") or 0.5)
    prune_tasks(ENRICH_TASKS)
    ENRICH_LOCKS.add(item_id)
    ENRICH_TASKS[item_id] = new_task()

    async def _runner():
        task = ENRICH_TASKS[item_id]

        def log_fn(msg: str):
            task["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(task["logs"]) > 200:
                task["logs"] = task["logs"][-200:]

        try:
            await _run_enrich_one(doc, min_credit, log_fn)
            fresh = await db.capitaineries.find_one({"_id": item_id})
            task["result"] = {
                k: fresh.get(k) for k in (
                    "_id", "name", "lat", "lon", "source", "osm_id", "shom_id",
                    "noaa_id", "enriched", "enrichment_source", "enriched_at", *ENRICH_FIELDS,
                )
            }
            task["state"] = "done"
        except Exception as e:
            task["error"] = f"{type(e).__name__}: {e}"
            task["state"] = "error"
        finally:
            task["finished_at"] = time.time()
            ENRICH_LOCKS.discard(item_id)

    asyncio.create_task(_runner())
    return {"status": "started", "id": item_id}


@router.get("/capitaineries/{item_id}/enrich/status")
async def enrich_status(item_id: str):
    task = ENRICH_TASKS.get(item_id)
    if not task:
        return {"state": "idle", "id": item_id}
    return {
        "state": task["state"], "id": item_id,
        "started_at": task["started_at"], "finished_at": task["finished_at"],
        "result": task["result"], "error": task["error"],
        "logs_tail": task["logs"][-30:],
    }


@router.post("/capitaineries/enrich-batch")
async def enrich_batch(body: EnrichBatchBody | None = None):
    if ENRICH_BATCH_STATE.running:
        raise HTTPException(409, "A capitaineries enrichment batch is already running")
    body = body or EnrichBatchBody()
    settings = await get_settings()
    concurrency = int(settings.get("marina_batch_concurrency") or 2)
    min_credit = float(settings.get("openrouter_min_credits_usd") or 0.5)

    q: dict = {}
    if not body.include_enriched:
        q["$or"] = [{"enriched": {"$ne": True}}, {"enriched": False}]
        q["enrich_attempts"] = {"$not": {"$gte": 2}}

    pool = await db.capitaineries.find(q).to_list(None)
    ranked = rank_enrich_candidates(pool)
    limit = int(body.limit or 0)
    candidates = ranked[:limit] if limit > 0 else ranked

    from app.core.run_rules import RuleError, bind_rules, reset_rules, snapshot_for_run
    from app.services import isolated_runs
    try:
        rules = snapshot_for_run(
            mode="capitaineries", settings=settings,
            overrides=body.rules, profile=body.profile)
    except RuleError as e:
        raise HTTPException(400, str(e)) from e
    opened = await isolated_runs.open_run(
        db, "capitaineries", kind="enrich",
        label="capitaineries-enrich", settings=settings,
        extra_params={"limit": body.limit, "profile": rules.get("profile")},
        rules_overrides=body.rules, profile=body.profile,
    )
    run_id = opened["run_id"]
    ENRICH_BATCH_STATE.run_id = run_id
    isolated_runs.reset_run(opened["token"])

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
        rules_token = bind_rules(rules)
        try:
            ENRICH_BATCH_STATE.log(
                f"Selected {len(candidates)} capitaineries (concurrency={concurrency})"
            )
            sem = asyncio.Semaphore(max(1, concurrency))
            counter = {"i": 0}

            async def _one(m):
                async with sem:
                    if ENRICH_BATCH_STATE.cancel:
                        ENRICH_BATCH_STATE.log(f"SKIP {m.get('name')}: batch annulé")
                        return
                    if m["_id"] in ENRICH_LOCKS:
                        ENRICH_BATCH_STATE.log(f"SKIP {m.get('name')}: already locked")
                        return
                    ENRICH_LOCKS.add(m["_id"])
                    ENRICH_BATCH_STATE.log(f"→ {m.get('name')}")
                    try:
                        result = await _run_enrich_one(
                            m, min_credit,
                            lambda s: ENRICH_BATCH_STATE.log(f"  {s}"),
                            skip_tinyfish=body.skip_tinyfish,
                            run_id=run_id,
                        )
                        ENRICH_BATCH_STATE.results.append({
                            "id": m["_id"], "name": m.get("name"),
                            "source": result.get("enrichment_source"),
                            "enriched": result.get("enriched"),
                            "fields_filled": [k for k in ENRICH_FIELDS if result.get(k) is not None],
                        })
                    except Exception as e:
                        ENRICH_BATCH_STATE.log(f"  FAILED: {type(e).__name__}: {e}")
                        ENRICH_BATCH_STATE.results.append({
                            "id": m["_id"], "name": m.get("name"),
                            "error": f"{type(e).__name__}: {e}",
                        })
                    finally:
                        ENRICH_LOCKS.discard(m["_id"])
                        counter["i"] += 1
                        ENRICH_BATCH_STATE.progress = counter["i"]

            await asyncio.gather(*(_one(m) for m in candidates))
            ENRICH_BATCH_STATE.log("Done.")
        except Exception as e:
            ENRICH_BATCH_STATE.error = f"{type(e).__name__}: {e}"
            ENRICH_BATCH_STATE.log(f"FATAL {ENRICH_BATCH_STATE.error}")
        finally:
            from app.services import isolated_runs
            await isolated_runs.finalize_run(
                db, "capitaineries", run_id,
                cancelled=ENRICH_BATCH_STATE.cancel,
                error=ENRICH_BATCH_STATE.error,
                extra={"results": len(ENRICH_BATCH_STATE.results)},
            )
            ENRICH_BATCH_STATE.finished_at = time.time()
            ENRICH_BATCH_STATE.running = False
            reset_rules(rules_token)

    asyncio.create_task(_runner())
    return {
        "started": True, "selected": len(candidates), "concurrency": concurrency,
        "run_id": run_id, "wrote_capitaineries": False,
        "profile": rules.get("profile"), "rules_hash": rules.get("hash"),
    }


@router.post("/capitaineries/enrich-batch/cancel")
async def enrich_batch_cancel():
    if not ENRICH_BATCH_STATE.running:
        raise HTTPException(409, "No enrichment batch is running")
    ENRICH_BATCH_STATE.cancel = True
    ENRICH_BATCH_STATE.log("Stop demandé")
    return {"cancelling": True}


@router.get("/capitaineries/enrich-batch/status")
async def enrich_batch_status():
    s = ENRICH_BATCH_STATE
    return {
        "running": s.running, "cancelling": s.cancel,
        "started_at": s.started_at, "finished_at": s.finished_at,
        "progress": s.progress, "total": s.total,
        "results": s.results, "logs_tail": s.logs[-60:], "error": s.error,
        "run_id": s.run_id, "wrote_capitaineries": False,
    }


@router.get("/capitaineries/runs")
async def capitaineries_runs_list():
    from app.services import isolated_runs
    items = await isolated_runs.list_meta_runs(db, "capitaineries")
    return {
        "count": len(items),
        "wrote_capitaineries": False,
        "items": items,
    }


@router.get("/capitaineries/runs/{run_id}")
async def capitaineries_run_detail(run_id: str):
    from app.services import isolated_runs
    doc = await isolated_runs.get_meta_run(db, "capitaineries", run_id)
    if not doc:
        raise HTTPException(404, f"Run {run_id} unknown")
    return {**doc, "wrote_capitaineries": False}


@router.get("/capitaineries/runs/{run_id}/events")
async def capitaineries_run_events(run_id: str, step: str | None = None,
                                   skip: int = 0, limit: int = 500):
    from app.services import isolated_runs
    if not await db.capitainerie_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    return await isolated_runs.list_run_events(
        db, "capitaineries", run_id, step=step, skip=skip, limit=limit)


@router.get("/capitaineries/runs/{run_id}/geojson")
async def capitaineries_run_geojson(run_id: str):
    """Run FeatureCollection — map run selector.

    Historical live dumps (`wrote_capitaineries=true`) have no
    isolated items: then serve the live collection they wrote.
    """
    meta = await db.capitainerie_runs.find_one({"_id": run_id})
    if not meta:
        raise HTTPException(404, f"Run {run_id} unknown")
    docs = await db.capitainerie_run_sites.find(
        {"run_id": run_id}, SLIM_PROJECTION).to_list(50000)
    live_fallback = False
    if not docs and meta.get("wrote_capitaineries"):
        docs = await _all_docs({})
        live_fallback = True
    fc = to_slim_geojson(docs)
    fc["run_id"] = run_id
    fc["live_fallback"] = live_fallback
    return fc
