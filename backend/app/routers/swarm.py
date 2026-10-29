"""app.routers.swarm — Discovery-swarm control, KPIs, telemetry,
failed extractions (Force Extract via the same isolated pipeline)."""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import db, get_settings
from app.services import project_runs
from app.state import swarm

router = APIRouter(prefix="/api")

class DeployBody(BaseModel):
    mode: str = "test"
    clear_db: bool = False
    force_rescan: bool = False

@router.post("/swarm/deploy")
async def deploy(body: DeployBody):
    if body.mode not in ("test", "full"):
        raise HTTPException(400, "mode must be test|full")
    if body.clear_db:
        raise HTTPException(400, "clear_db is disabled")
    settings = await get_settings()
    try:
        await swarm.deploy(body.mode, False, settings, body.force_rescan)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {
        "status": "deployed",
        "mode": body.mode,
        "run_id": swarm.run_id,
        "wrote_projects": False,
    }


@router.post("/swarm/stop")
async def stop():
    await swarm.stop()
    return {"status": "stopped"}


@router.get("/swarm/status")
async def status():
    st = swarm.status()
    # Single LLM engine: OpenRouter (dynamic badge on the UI side).
    st["engine"] = "openrouter"
    return st


@router.get("/stats")
async def stats(mode: str = "projects"):
    """KPI counters shown in the Swarm Intelligence Audit view.

    Bug-fix 2026-08-24 — `projects_mapped` used to always return
    `db.projects.count_documents({})` (4463), regardless of the active mode.
    The Audit view now passes `?mode=marinas|formalities|projects|amp` so the
    counter reflects the actual dataset the user is looking at. Extractions
    and success_rate are read from a mode-scoped `dataset` field in the
    telemetry collection when present; legacy rows without that field are
    counted for projects only (backwards-compat).
    """
    m = (mode or "projects").lower()
    if m == "marinas":
        items = await db.marinas.count_documents({})
        tele_filter = {"dataset": "marinas"}
    elif m == "capitaineries":
        items = await db.capitaineries.count_documents({})
        tele_filter = {"dataset": "capitaineries"}
    elif m == "formalities":
        # Refactor 2026-06 — Formalities mode = world map [EEZ -> PoE].
        # ITEMS MAPPED = number of ports of entry extracted.
        items = await db.poe_ports.count_documents({})
        tele_filter = {"dataset": "formalities"}
    elif m == "amp":
        items = await db.amp_sites.count_documents({})
        tele_filter = {"dataset": "amp"}
    elif m == "science":
        items = await db.science_items.count_documents({})
        tele_filter = {"dataset": "science"}
    else:  # projects (default)
        items = await db.projects.count_documents({})
        # Legacy rows have no `dataset` field — count them as projects.
        tele_filter = {"$or": [{"dataset": "projects"}, {"dataset": {"$exists": False}}]}

    total = await db.telemetry.count_documents(tele_filter)
    success = await db.telemetry.count_documents({**tele_filter, "status": {"$in": ["SUCCESS", "MERGED"]}})
    return {
        "mode": m,
        "total_extractions": total,
        "success_rate": round(success / total * 100, 1) if total else 0.0,
        "projects_mapped": items,  # legacy key kept for backwards-compat
        "items_mapped": items,
    }


def _dataset_filter(mode: str) -> dict:
    """Mode-scoped telemetry filter — mirrors /api/stats (legacy rows = projects)."""
    m = (mode or "projects").lower()
    if m == "marinas":
        return {"dataset": "marinas"}
    if m == "capitaineries":
        return {"dataset": "capitaineries"}
    if m == "formalities":
        return {"dataset": "formalities"}
    if m == "amp":
        return {"dataset": "amp"}
    if m == "science":
        return {"dataset": "science"}
    return {"$or": [{"dataset": "projects"}, {"dataset": {"$exists": False}}]}


@router.get("/telemetry")
async def telemetry(mode: str = "projects"):
    docs = await db.telemetry.find(_dataset_filter(mode)).sort("ts", -1).to_list(200)
    for d in docs:
        d["id"] = d.pop("_id")
    return docs


@router.get("/failed")
async def failed(mode: str = "projects"):
    docs = await db.failed.find(_dataset_filter(mode)).sort("ts", -1).to_list(200)
    for d in docs:
        d["id"] = d.pop("_id")
    return docs


@router.delete("/audit")
async def clear_audit():
    t = await db.telemetry.delete_many({})
    f = await db.failed.delete_many({})
    return {"telemetry_deleted": t.deleted_count, "failed_deleted": f.deleted_count}


async def _force_extract_one(row: dict, settings: dict, *, finalize_own: bool = True):
    """Same `_process_url` / gatekeeper, run write only."""
    if settings:
        swarm.settings = settings
    if not swarm.run_id:
        opened = await project_runs.open_run(
            db, mode="enrich", label="force-extract", settings=settings)
        swarm.run_id = opened["run_id"]
        swarm.recorder = opened["recorder"]
    try:
        result = await swarm._process_url({
            "url": row["url"],
            "funder": row.get("funder", ""),
            "source": row.get("source", ""),
            "force": True,
        })
        if result and result.get("status") == "site":
            await db.failed.delete_one({"_id": row["_id"]})
            swarm.log(f"Force extract OK (run {swarm.run_id}): {row['url'][:60]}", "success")
        return result
    except Exception as e:
        swarm.log(f"Force extract failed for {row['url'][:60]}: {str(e)[:100]}", "error")
        return {"status": "failed", "url": row.get("url"), "error": str(e)[:200]}
    finally:
        if finalize_own and not swarm.running:
            rid = swarm.run_id
            await project_runs.finalize_run(db, rid)
            if swarm.run_id == rid and not swarm.running:
                swarm.run_id = None
                swarm.recorder = None


@router.post("/failed/{fid}/force")
async def force_one(fid: str):
    settings = await get_settings()
    row = await db.failed.find_one({"_id": fid})
    if not row:
        raise HTTPException(404, "failed entry not found")
    live = bool(swarm.running and swarm.run_id)
    if not live:
        opened = await project_runs.open_run(
            db, mode="enrich", label="force-extract", settings=settings)
        swarm.run_id = opened["run_id"]
        swarm.recorder = opened["recorder"]
        swarm.settings = settings
    rid = swarm.run_id
    asyncio.create_task(_force_extract_one(row, settings, finalize_own=not live))
    return {
        "status": "started",
        "url": row["url"],
        "run_id": rid,
        "wrote_projects": False,
    }


@router.post("/failed/force-all")
async def force_all():
    settings = await get_settings()
    rows = await db.failed.find({
        "$or": [{"dataset": "projects"}, {"dataset": {"$exists": False}}],
    }).to_list(100)
    live = bool(swarm.running and swarm.run_id)
    opened_here = False
    if not live:
        opened = await project_runs.open_run(
            db, mode="enrich", label="force-extract-all", settings=settings)
        swarm.run_id = opened["run_id"]
        swarm.recorder = opened["recorder"]
        swarm.settings = settings
        opened_here = True
    rid = swarm.run_id

    async def run_all():
        try:
            sem = asyncio.Semaphore(3)

            async def one(r):
                async with sem:
                    await _force_extract_one(r, settings, finalize_own=False)
            await asyncio.gather(*[one(r) for r in rows])
        finally:
            if opened_here and not swarm.running:
                await project_runs.finalize_run(db, rid)
                if swarm.run_id == rid and not swarm.running:
                    swarm.run_id = None
                    swarm.recorder = None

    asyncio.create_task(run_all())
    return {
        "status": "started",
        "count": len(rows),
        "run_id": rid,
        "wrote_projects": False,
    }
