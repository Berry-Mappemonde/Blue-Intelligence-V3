"""app.routers.science — Science mode: oceanographic datasets
(Sextant/SISMER, ODATIS, EDMED SeaDataNet) and Argo floats (Coriolis).

Harvest via structured APIs only (GeoNetwork JSON, SPARQL, ERDDAP) —
no LLM, no scraping. Live collection ``science_items`` with non-destructive
upsert; each harvest is recorded in ``science_runs``
(rules snapshot included) for the console Runs tab.
"""
import asyncio
import time
import uuid

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.tasks import BuildState
from app.db import db, get_settings
from app.services.science_build import (
    DISPLAY_SOURCES,
    SCHEMA,
    SLIM_PROJECTION,
    SOURCES,
    build_science,
    to_slim_geojson,
)
from app.services.swarm_pipeline import now_iso
from app.services.geojson_import import empty_import_result, parse_feature_collection

router = APIRouter(prefix="/api")

BUILD_STATE = BuildState()


async def _all_docs(q: dict | None = None) -> list[dict]:
    cur = db.science_items.find(q or {}, SLIM_PROJECTION).sort("name", 1)
    return [doc async for doc in cur]


@router.get("/science")
async def list_science():
    docs = await _all_docs({"lat": {"$ne": None}})
    return to_slim_geojson(docs)


@router.get("/science/count")
async def science_count():
    total = await db.science_items.count_documents({})
    located = await db.science_items.count_documents({"lat": {"$ne": None}})
    by_source = {}
    for source in DISPLAY_SOURCES:
        by_source[source] = await db.science_items.count_documents({"source": source})
    return {
        "total": total,
        "located": located,
        "unlocated": total - located,
        "datasets": await db.science_items.count_documents({"kind": "dataset"}),
        "argo_floats": await db.science_items.count_documents({"kind": "argo_float"}),
        "cruises": await db.science_items.count_documents({"kind": "cruise"}),
        "with_doi": await db.science_items.count_documents({"doi": {"$nin": ["", None]}}),
        "by_source": by_source,
    }


@router.get("/depth")
async def approach_depth(lat: float = Query(...), lon: float = Query(...)):
    """Approach depth (EMODnet DTM) at a point, with Mongo cache."""
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise HTTPException(400, "lat/lon out of range")
    from app.services.depth_sample import sample_depth
    return await sample_depth(db.depth_samples, lat, lon)


@router.get("/export/science.geojson")
async def export_science():
    docs = await _all_docs({"lat": {"$ne": None}})
    fc = to_slim_geojson(docs)
    return JSONResponse(
        fc,
        headers={"Content-Disposition": "attachment; filename=science.geojson"},
    )


@router.post("/import/science.geojson")
async def import_science_geojson(fc: dict = Body(...)):
    kind, feats = parse_feature_collection(fc)
    if kind == "invalid":
        raise HTTPException(400, "invalid GeoJSON FeatureCollection")
    if kind == "empty":
        return empty_import_result("total_science", await db.science_items.count_documents({}))
    imported = updated = invalid = 0
    now = now_iso()
    for f in feats:
        try:
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or []
            geom_type = geom.get("type")
            track = None
            if geom_type == "LineString":
                if not isinstance(coords, list) or len(coords) < 2:
                    invalid += 1
                    continue
                track = []
                for pt in coords[:500]:
                    if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                        continue
                    track.append([float(pt[0]), float(pt[1])])
                if len(track) < 2:
                    invalid += 1
                    continue
                mid = track[len(track) // 2]
                lon, lat = float(mid[0]), float(mid[1])
            elif geom_type == "Point" and len(coords) >= 2:
                lon, lat = float(coords[0]), float(coords[1])
            else:
                invalid += 1
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                invalid += 1
                continue
            p = f.get("properties") or {}
            name = str(p.get("name") or "").strip()
            if not name:
                invalid += 1
                continue
            source = p.get("source") if p.get("source") in DISPLAY_SOURCES else None
            mid = str(p.get("id") or "").strip() or (
                f"{source or 'science'}:{uuid.uuid4()}"
            )
            doc = {
                "_id": mid,
                "kind": p.get("kind") or "dataset",
                "source": source or "sextant",
                "error_m": p.get("error_m"),
                "method": p.get("method"),
                "native_id": p.get("native_id") or mid.split(":", 1)[-1],
                "name": name[:240],
                "abstract": str(p.get("abstract") or "")[:600],
                "url": p.get("url"),
                "doi": p.get("doi"),
                "provider": p.get("provider"),
                "date": p.get("date"),
                "profile_date": p.get("profile_date"),
                "cycle": p.get("cycle"),
                "ocean": p.get("ocean"),
                "wmo": p.get("wmo"),
                "ship": p.get("ship"),
                "start": p.get("start"),
                "end": p.get("end"),
                "lat": lat,
                "lon": lon,
                "bbox": p.get("bbox"),
                "track": track or p.get("track"),
                "global": False,
                "fetched_at": p.get("fetched_at") or now,
                "schema": SCHEMA,
            }
            existing = await db.science_items.find_one({"_id": mid})
            if existing:
                await db.science_items.update_one(
                    {"_id": mid}, {"$set": {k: v for k, v in doc.items() if k != "_id"}})
                updated += 1
            else:
                await db.science_items.insert_one(doc)
                imported += 1
        except Exception:
            invalid += 1
    total = await db.science_items.count_documents({})
    return {
        "imported": imported, "merged": updated, "skipped_existing": 0,
        "invalid": invalid, "total_science": total,
    }


class ScienceBuildBody(BaseModel):
    sources: list[str] | None = None
    profile: str | None = None
    rules: dict | None = None
    scope: str | None = None
    max_records: int | None = None


@router.post("/science/build")
async def science_build_start(body: ScienceBuildBody | None = None):
    if BUILD_STATE.running:
        raise HTTPException(409, "A science harvest is already running")
    body = body or ScienceBuildBody()
    sources = [s for s in (body.sources or list(SOURCES)) if s in SOURCES]
    if not sources:
        raise HTTPException(400, f"sources must be a subset of {list(SOURCES)}")

    settings = await get_settings()
    from app.core.run_rules import (
        RuleError, attach_rules, bind_rules, reset_rules, snapshot_for_run,
    )
    try:
        rules = snapshot_for_run(
            mode="science", settings=settings,
            overrides=body.rules, profile=body.profile)
    except RuleError as e:
        raise HTTPException(400, str(e)) from e

    from app.services.isolated_runs import new_run_id
    run_id = new_run_id()
    BUILD_STATE.run_id = run_id
    await db.science_runs.insert_one({
        "_id": run_id,
        "label": "science-harvest",
        "dataset": "science",
        "kind": "catalog_harvest",
        "mode": "science",
        "state": "running",
        "params": attach_rules(
            {"kind": "catalog_harvest", "label": "science-harvest",
             "sources": sources, "profile": rules.get("profile")},
            rules,
        ),
        "created_at": now_iso(),
        "started_at": now_iso(),
        "error": None,
    })

    async def _runner():
        rules_token = bind_rules(rules)
        error = None
        try:
            cap = body.max_records
            if (body.scope or "").lower() == "test":
                cap = cap or 80
            await build_science(
                coll=db.science_items,
                state=BUILD_STATE,
                sources=sources,
                max_records=cap,
                run_id=run_id,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            BUILD_STATE.error = error
            BUILD_STATE.log(f"FATAL: {error}")
            BUILD_STATE.finished_at = time.time()
            BUILD_STATE.running = False
        finally:
            state = "failed" if error else (
                "cancelled" if BUILD_STATE.cancel else "done")
            await db.science_runs.update_one({"_id": run_id}, {"$set": {
                "state": state,
                "finished_at": now_iso(),
                "error": error,
                "summary": BUILD_STATE.summary,
            }})
            reset_rules(rules_token)

    asyncio.create_task(_runner())
    return {
        "started": True, "kind": "catalog_harvest", "sources": sources,
        "run_id": run_id, "profile": rules.get("profile"),
        "rules_hash": rules.get("hash"),
    }


@router.get("/science/build/status")
async def science_build_status():
    s = BUILD_STATE
    return {
        "running": s.running,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "progress": s.progress,
        "total": s.total,
        "summary": s.summary,
        "error": s.error,
        "logs_tail": s.logs[-40:],
        "cancelling": s.cancel and s.running,
        "run_id": s.run_id,
    }


@router.post("/science/build/cancel")
async def science_build_cancel():
    if not BUILD_STATE.running:
        raise HTTPException(409, "No science harvest is running")
    BUILD_STATE.cancel = True
    BUILD_STATE.log("Stop demandé")
    return {"cancelling": True}


def _summary_items(doc: dict) -> int:
    summary = doc.get("summary") or {}
    try:
        return int(summary.get("inserted") or 0) + int(summary.get("updated") or 0)
    except (TypeError, ValueError):
        return 0


@router.get("/science/runs")
async def science_runs_list():
    from app.core.run_rules import snapshot_list_fields
    try:
        docs = await db.science_runs.find({}).to_list(200)
    except Exception:
        docs = []
    docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    items = []
    for d in docs:
        fields = snapshot_list_fields(d.get("params") or {})
        items.append({
            "id": str(d.get("_id")),
            "label": d.get("label") or str(d.get("_id")),
            "state": d.get("state"),
            "created_at": d.get("created_at"),
            "count": _summary_items(d),
            "dataset": "science",
            "kind": d.get("kind") or "",
            "profile": fields["profile"],
            "hash": fields["hash"],
            "hash8": fields["hash8"],
            "counts": fields["counts"],
        })
    return {"count": len(items), "items": items}


@router.get("/science/runs/{run_id}")
async def science_run_detail(run_id: str):
    from app.core.run_rules import snapshot_list_fields
    doc = await db.science_runs.find_one({"_id": run_id})
    if not doc:
        raise HTTPException(404, f"Run {run_id} unknown")
    rules = (doc.get("params") or {}).get("rules") or {}
    if not isinstance(rules, dict):
        rules = {}
    fields = snapshot_list_fields(doc.get("params") or {})
    return {
        "id": str(doc.get("_id")),
        "label": doc.get("label") or str(doc.get("_id")),
        "state": doc.get("state"),
        "created_at": doc.get("created_at"),
        "finished_at": doc.get("finished_at"),
        "kind": doc.get("kind") or "",
        "dataset": "science",
        "error": doc.get("error"),
        "summary": doc.get("summary"),
        "profile": fields["profile"],
        "hash": fields["hash"],
        "hash8": fields["hash8"],
        "counts": fields["counts"],
        "chosen": rules.get("chosen") or {},
        "params": {"rules": rules},
    }
