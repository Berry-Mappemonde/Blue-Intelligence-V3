"""
app.routers.runs — Versioned PoE pipeline runs (several in parallel).

POST /api/runs                         unified entry: start or resume (P0-4)
GET  /api/runs/{id}/manifest           data/runs/<id>/manifest.json
POST /api/runs/{id}/export             GeoJSON + manifest export (+ optional upload)
POST /api/runs/{id}/upload             retry the upload of an existing, verified export
POST /api/poe/runs                     start (or resume) a run — wrapper of /api/runs
POST /api/poe/runs/multi               deprecated: v1 + v2 + tinyfish in parallel
GET  /api/poe/runs                     list of runs
GET  /api/poe/runs/searxng             search-engine health
GET  /api/poe/runs/compare             N-run comparison (+ v1)
POST /api/poe/runs/best-of             synthesis into a new run (never v1)
GET  /api/poe/runs/{id}/status
POST /api/poe/runs/{id}/cancel
GET  /api/poe/runs/{id}/ports
GET  /api/poe/runs/{id}/events
GET  /api/poe/runs/{id}/diff
GET  /api/poe/runs/{id}/report
GET  /api/poe/runs/{id}/listing-control   control report vs listing_ref
POST /api/poe/runs/{id}/listing-control   compute + persist the review queue
GET  /api/poe/listing-control/v1
POST /api/poe/listing-control/v1
GET  /api/poe/listing-control/compare
GET  /api/poe/listing-control/review
GET  /api/poe/listing-control/ref
GET  /api/poe/listing-control/canary
GET  /api/poe/seeds/union              bottom-up union v1+runs+OSM+listing (no crawl)
POST /api/poe/seeds/build              rebuild poe_seed_ports (not poe_ports)
GET  /api/poe/seeds                    read poe_seed_ports
GET  /api/poe/seeds/gps-audit          confirmed GPS audit (dry-run, no persist)
GET  /api/poe/seeds/gps-arbitrated     git registry of decided GPS (no persist)
GET  /api/poe/seeds/line               TinyFish query + inventory summary
POST /api/poe/seeds/verify             classify seeds + versioned run (not poe_ports)
POST /api/poe/seeds/enrich             geocode + judge (batches, not poe_ports)
GET  /api/poe/seeds/enrich/status
POST /api/poe/seeds/enrich/cancel
POST /api/poe/seeds/mine-sources       Fetch already-paid judge_sources (0 Search)
GET  /api/poe/seeds/mine-sources/status
POST /api/poe/seeds/mine-sources/cancel
GET  /api/poe/seeds/osm                Taginfo + OSM cache (no Overpass)
POST /api/poe/seeds/osm/refresh        reload Overpass → osm_port_seeds
GET  /api/poe/runs/code-fingerprint
"""
import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.core.tasks import TaskState
from app.db import db as _db
from app.services import poe_pipeline as poe
from app.services import poe_runs
from app.services import unified_runs
from app.services.listing_control import (
    build_listing_control_report, compare_runs_to_listing, persist_review,
    suggest_canary_zones,
)
from app.core.run_rules import snapshot_for_run, snapshot_list_fields
from app.services.run_fingerprint import build_code_fingerprint
from app.services.listing_ref import project_listing
from app.services.poe_bestof import compare_runs, synthesize_best_of
from app.services.poe_diff import diff_run_vs_baseline
from app.services.poe_pipeline import normalize_variant
from app.services.poe_report import build_run_report, report_to_markdown
from app.services.poe_seeds import (
    SEARCH_EXCLUDE_DOMAINS, SEED_LEGEND, build_seed_union, collect_seed_report,
    format_seed_line, match_named_seed, persist_seed_database,
    persist_verify_run, public_seed_view, seed_from_files, seed_search_query,
)
from app.services.poe_confirmed_gps_audit import audit_from_db
from app.services.poe_gps_registry import public_view as gps_registry_view

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

MAX_PARALLEL_RUNS = 4
RUN_STATES: dict[str, TaskState] = {}


def _active_ids() -> list[str]:
    return [rid for rid, st in RUN_STATES.items() if st.running]


class RunBody(BaseModel):
    label: str = ""
    limit: int = 0
    concurrency: int = 2
    zones: list[int] | None = None
    resume_run_id: str | None = None
    variant: str = "tinyfish"           # v1 | v2 | tinyfish
    profile: str | None = None
    rules: dict | None = None


class MultiRunBody(BaseModel):
    variants: list[str] = Field(default_factory=lambda: ["v1", "v2", "tinyfish"])
    limit: int = 0
    concurrency: int = 2
    zones: list[int] | None = None
    label_prefix: str = "bestof"


class BestOfBody(BaseModel):
    run_ids: list[str]
    include_v1: bool = True
    label: str = ""


class SeedBuildBody(BaseModel):
    run_ids: list[str] = Field(default_factory=list)
    include_v1: bool = True
    include_listing: bool = True
    include_osm: bool = True
    use_default_mondials: bool = True


class SeedVerifyBody(BaseModel):
    run_ids: list[str] = Field(default_factory=list)
    include_v1: bool = True
    include_listing: bool = True
    include_osm: bool = True
    use_default_mondials: bool = True
    persist: bool = True
    label: str = "seed-verify"


class SeedEnrichBody(BaseModel):
    source: str = "seeds"          # seeds = poe_seed_ports ; run = poe_run_ports
    run_id: str = ""
    do_geocode: bool = True
    do_verify: bool = True
    verdicts: list[str] = Field(default_factory=lambda: ["name_only"])
    limit: int = 200
    concurrency: int = 2
    use_agent: bool = True


class SeedMineBody(BaseModel):
    fetch_cap: int = 200
    judge_residue: bool = True
    residue_limit: int = 0
    persist_memory: bool = True
    include_runs: bool = True
    concurrency: int = 2
    use_agent: bool = True


async def _launch_run(*, resume: bool, run_id: str, label: str, limit: int,
                      zones, concurrency: int, variant: str,
                      rules_overrides: dict | None = None,
                      profile: str | None = None,
                      retry_errored: bool = False, force: bool = False) -> str:
    if len(_active_ids()) >= MAX_PARALLEL_RUNS:
        raise HTTPException(409, f"déjà {MAX_PARALLEL_RUNS} runs actifs")
    if run_id in RUN_STATES and RUN_STATES[run_id].running:
        raise HTTPException(409, f"Run {run_id} already running")
    # Reserve the slot before the first await: the check above and this
    # registration must not be split by the Mongo lookup, or two requests
    # both pass the check and the run executes twice.
    previous = RUN_STATES.get(run_id)
    state = TaskState(max_logs=2000)
    state.start()
    RUN_STATES[run_id] = state
    try:
        resolved = await unified_runs.resolve_launch(
            _db, run_id=run_id, resume=resume, label=label, limit=limit,
            zones=zones, concurrency=concurrency, variant=variant,
            retry_errored=retry_errored, force=force)
    except BaseException as e:
        if RUN_STATES.get(run_id) is state:
            if previous is None:
                RUN_STATES.pop(run_id, None)
            else:
                RUN_STATES[run_id] = previous
        if isinstance(e, ValueError):
            busy = isinstance(e, (unified_runs.RunExists, unified_runs.RunBusy))
            raise HTTPException(409 if busy else 400, str(e)) from e
        raise

    async def _runner():
        try:
            state.summary = await unified_runs.start_run(
                _db, state, resolved=resolved,
                rules_overrides=rules_overrides, profile=profile)
        except Exception as e:
            state.error = f"{type(e).__name__}: {e}"
            state.log(f"FATAL: {state.error}")
            await _db.poe_runs.update_one({"_id": run_id}, {"$set": {
                "state": "failed", "error": state.error,
                "finished_at": poe.now_iso()}})
        finally:
            state.finish()

    asyncio.create_task(_runner())
    return run_id


@router.post("/poe/runs", status_code=202)
async def poe_run_start(body: RunBody | None = None):
    body = body or RunBody()
    resume = bool(body.resume_run_id)
    if resume:
        existing = await _db.poe_runs.find_one({"_id": body.resume_run_id})
        if not existing:
            raise HTTPException(404, f"Run {body.resume_run_id} unknown — cannot resume")
        run_id = body.resume_run_id
        params = existing.get("params") or {}
        label = body.label or existing.get("label") or ""
        limit = int(params.get("limit") or 0)
        zones = params.get("only_zones")
        concurrency = int(params.get("concurrency") or body.concurrency)
        variant = params.get("variant") or body.variant
    else:
        run_id = poe_runs.new_run_id()
        label, limit, zones, concurrency = body.label, body.limit, body.zones, body.concurrency
        variant = body.variant
    try:
        variant = normalize_variant(variant)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not label:
        label = f"{variant}-full"
    if not resume and (body.rules or body.profile):
        from app.core.run_rules import RuleError, snapshot_for_run
        from app.db import get_settings
        try:
            snapshot_for_run(mode="formalities", settings=await get_settings(),
                             overrides=body.rules, profile=body.profile)
        except RuleError as e:
            raise HTTPException(400, str(e)) from e
    await _launch_run(resume=resume, run_id=run_id, label=label, limit=limit,
                      zones=zones, concurrency=concurrency, variant=variant,
                      rules_overrides=None if resume else body.rules,
                      profile=None if resume else body.profile)
    return {"status": "started", "run_id": run_id, "resume": resume, "variant": variant}


@router.post("/poe/runs/multi", status_code=202)
async def poe_run_multi(body: MultiRunBody | None = None):
    """Deprecated (P0-4): launch the requested variants in parallel.

    Still works; each run goes through the unified runner. Use POST /api/runs.
    """
    logger.warning("POST /api/poe/runs/multi is deprecated — use POST /api/runs")
    body = body or MultiRunBody()
    started = []
    errors = []
    for raw in body.variants:
        try:
            variant = normalize_variant(raw)
        except ValueError as e:
            errors.append({"variant": raw, "error": str(e)})
            continue
        try:
            run_id = poe_runs.new_run_id()
            label = f"{body.label_prefix}-{variant}"
            await _launch_run(
                resume=False, run_id=run_id, label=label,
                limit=body.limit, zones=body.zones,
                concurrency=body.concurrency, variant=variant)
            started.append({"run_id": run_id, "variant": variant, "label": label})
            await asyncio.sleep(0.05)
        except HTTPException as e:
            errors.append({"variant": variant, "error": e.detail})
    if not started:
        raise HTTPException(409, {"detail": "aucun run démarré", "errors": errors})
    return {"status": "started", "runs": started, "errors": errors,
            "active_run_ids": _active_ids(),
            "deprecated": "use POST /api/runs"}


@router.get("/poe/runs/code-fingerprint")
async def poe_runs_code_fingerprint():
    """Fingerprint that would be written in params.code on the next POST /runs."""
    from app.db import get_settings
    settings = await get_settings()
    rules = snapshot_for_run(mode="formalities", settings=settings)
    timeout = int((rules.get("chosen") or {}).get(
        "formalities.zone_timeout_s", {}).get("value") or poe_runs.ZONE_TIMEOUT_S)
    fp = build_code_fingerprint(settings, zone_timeout_s=timeout)
    fp["rules"] = {"hash": rules["hash"], "profile": rules["profile"],
                   "counts": rules["counts"]}
    return fp


@router.get("/poe/runs/searxng")
async def poe_searxng_health():
    instances = poe.searx_instances()
    own = poe.configured_searxng_url()
    results = []
    for inst in instances[:6]:
        t0 = time.time()
        ok = False
        n = 0
        err = None
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(f"{inst}/search",
                                     params={"q": "ports of entry france customs",
                                             "format": "json"})
                if r.status_code == 200:
                    n = len((r.json().get("results") or []))
                    ok = n > 0
                else:
                    err = f"HTTP {r.status_code}"
        except Exception as e:
            err = f"{type(e).__name__}"
        results.append({"instance": inst, "ok": ok, "n": n, "error": err,
                        "ms": int((time.time() - t0) * 1000),
                        "local": inst.rstrip("/") == own})
    return {"searxng_url": own, "instances": results,
            "ok": any(x["ok"] for x in results)}


@router.get("/poe/runs")
async def poe_runs_list():
    docs = await _db.poe_runs.find({}).sort("created_at", -1).to_list(100)
    items = []
    for d in docs:
        row = dict(d)
        row["id"] = str(d.get("_id"))
        row.update(snapshot_list_fields(d.get("params") or {}))
        items.append(row)
    return {"count": len(items), "active_run_ids": _active_ids(),
            "active_run_id": (_active_ids() or [None])[0], "items": items}


@router.get("/poe/listing-control/ref")
async def listing_control_ref():
    """slug → mrgid projection (not the detailed ports)."""
    proj = project_listing()
    return {
        "listing_ref_id": proj["listing_ref_id"],
        "generated_at": proj.get("generated_at"),
        "disclaimer": proj.get("disclaimer"),
        "stats": proj["stats"],
        "unresolved": proj.get("unresolved") or [],
        "resolutions": [
            {k: r[k] for k in ("slug", "name", "mrgids", "iso2", "method")}
            for r in proj.get("resolutions") or []
        ],
    }


@router.get("/poe/listing-control/v1")
async def listing_control_v1(restrict_to_run_zones: bool = False):
    return await build_listing_control_report(
        _db, "v1", restrict_to_run_zones=restrict_to_run_zones)


@router.post("/poe/listing-control/v1")
async def listing_control_v1_persist(restrict_to_run_zones: bool = False):
    report = await build_listing_control_report(
        _db, "v1", restrict_to_run_zones=restrict_to_run_zones)
    persisted = await persist_review(_db, report)
    return {"report": report, "persisted": persisted}


@router.get("/poe/listing-control/compare")
async def listing_control_compare(run_ids: str = "", include_v1: bool = True,
                                  persist: bool = False):
    ids = [x.strip() for x in run_ids.split(",") if x.strip()]
    if not include_v1 and not ids:
        raise HTTPException(400, "run_ids requis si include_v1=false")
    for rid in ids:
        if not await _db.poe_runs.find_one({"_id": rid}):
            raise HTTPException(404, f"Run {rid} unknown")
    return await compare_runs_to_listing(
        _db, ids, include_v1=include_v1, persist=persist)


@router.get("/poe/listing-control/canary")
async def listing_control_canary(run_ids: str = "", include_v1: bool = True,
                                 limit: int = 25):
    """EEZs to run as canary (listing_only ∪ error) — no crawl."""
    ids = [x.strip() for x in run_ids.split(",") if x.strip()]
    return await suggest_canary_zones(
        _db, ids, include_v1=include_v1, limit=limit)


@router.get("/poe/seeds/osm")
async def poe_seeds_osm():
    """Worldwide Taginfo counts + osm_port_seeds cache state. No Overpass."""
    from app.services.osm_seeds import osm_inventory
    return await osm_inventory(_db)


@router.post("/poe/seeds/osm/refresh")
async def poe_seeds_osm_refresh():
    """Reload Overpass (slow, 2–10 min). Does not write poe_ports."""
    from app.services.osm_seeds import refresh_osm_cache
    return await refresh_osm_cache(_db)


@router.post("/poe/seeds/build")
async def poe_seeds_build(body: SeedBuildBody | None = None):
    """Rebuild poe_seed_ports (runs ∪ listing ∪ OSM). No crawl, not poe_ports."""
    body = body or SeedBuildBody()
    ids = [x.strip() for x in body.run_ids if str(x).strip()]
    report = await collect_seed_report(
        _db, ids, include_v1=body.include_v1,
        include_listing=body.include_listing,
        include_osm=body.include_osm,
        use_default_mondials=body.use_default_mondials)
    persisted = await persist_seed_database(_db, report)
    out = public_seed_view(report, mode="seed-db")
    out["persisted"] = persisted
    return out


@router.get("/poe/seeds")
async def poe_seeds_list(mrgid: int | None = None, verdict: str | None = None,
                         limit: int = 50):
    """Read poe_seed_ports. No poe_ports write."""
    q: dict = {}
    if mrgid is not None:
        q["mrgid"] = int(mrgid)
    if verdict:
        q["verify_verdict"] = verdict
    total = await _db.poe_seed_ports.count_documents(q)
    cap = max(0, min(int(limit or 50), 200))
    docs = await _db.poe_seed_ports.find(q).to_list(cap)
    return {
        "total": total,
        "limit": cap,
        "seeds": docs,
        "wrote_poe_ports": False,
    }


@router.get("/poe/seeds/gps-audit")
async def poe_seeds_gps_audit():
    """GPS audit of `confirmed` (dry-run). No persist, not poe_ports, no build."""
    return await audit_from_db(_db)


@router.get("/poe/seeds/gps-arbitrated")
async def poe_seeds_gps_arbitrated():
    """Git registry of decided GPS. No persist, no poe_ports, no build."""
    return gps_registry_view()


@router.get("/poe/seeds/line")
async def poe_seeds_line(mrgid: int, name: str):
    """One judge line for a candidate (collection, else listing + priors)."""
    docs = await _db.poe_seed_ports.find({"mrgid": int(mrgid)}).to_list(8000)
    hit = match_named_seed(docs, mrgid, name)
    source = "poe_seed_ports"
    if hit is None:
        hit = seed_from_files(int(mrgid), name)
        source = "files"
    if hit is None:
        raise HTTPException(404, "seed unknown")
    return {
        "name": hit.get("name"),
        "search_query": hit.get("search_query") or seed_search_query(hit),
        "search_exclude_domains": list(
            hit.get("search_exclude_domains") or SEARCH_EXCLUDE_DOMAINS),
        "line": hit.get("seed_line") or format_seed_line(hit),
        "legend": SEED_LEGEND,
        "source": source,
        "seed": hit,
        "wrote_poe_ports": False,
    }


@router.get("/poe/seeds/union")
async def poe_seeds_union(run_ids: str = "", include_v1: bool = True,
                          include_listing: bool = True,
                          include_osm: bool = True,
                          use_default_mondials: bool = False):
    """Bottom-up seed union (v1, runs, OSM, listing). No crawl, no poe_ports write.

    `use_default_mondials=true` adds the 5 already-in-DB 285-EEZ runs if
    `run_ids` is empty. OSM comes from the `osm_port_seeds` cache (no Overpass here).
    Residue = listing names to geocode, not to re-scrape.
    """
    ids = [x.strip() for x in run_ids.split(",") if x.strip()]
    return await build_seed_union(
        _db, ids, include_v1=include_v1, include_listing=include_listing,
        include_osm=include_osm, use_default_mondials=use_default_mondials)


@router.post("/poe/seeds/verify")
async def poe_seeds_verify(body: SeedVerifyBody | None = None):
    """Inventory + PoE verdict. Optionally persisted as a `verify` run.

    No SERP crawl. No write to poe_ports. The crawl (geocode /
    OSM / Claude judge) only targets `name_only` and `unverified`, later.
    """
    body = body or SeedVerifyBody()
    ids = [x.strip() for x in body.run_ids if str(x).strip()]
    report = await collect_seed_report(
        _db, ids, include_v1=body.include_v1,
        include_listing=body.include_listing,
        include_osm=body.include_osm,
        use_default_mondials=body.use_default_mondials)
    out = public_seed_view(report, mode="seed-verify")
    if body.persist:
        persisted = await persist_verify_run(_db, report, label=body.label)
        out["persisted"] = persisted
        out["run_id"] = persisted["run_id"]
    return out


@router.post("/poe/seeds/enrich", status_code=202)
async def poe_seeds_enrich(body: SeedEnrichBody | None = None):
    """Geocode / judge a batch. Does not write poe_ports.

    Default: `poe_seed_ports` collection (source=seeds). TinyFish searches
    the seed name; noonsite.com is excluded. Claude judges the excerpts.
    `source=run` + `run_id` re-reads a versioned verify run.
    """
    from app.services.poe_seed_enrich import execute_enrich

    body = body or SeedEnrichBody()
    source = (body.source or "seeds").strip()
    if source not in ("seeds", "run"):
        raise HTTPException(400, "source doit être seeds ou run")
    run_id = (body.run_id or "").strip()
    task_id = "seed-enrich" if source == "seeds" else run_id
    if source == "run":
        if not run_id:
            raise HTTPException(400, "run_id requis si source=run")
        existing = await _db.poe_runs.find_one({"_id": run_id})
        if not existing:
            raise HTTPException(404, f"Run {run_id} unknown — lancer POST /api/poe/seeds/verify")
    else:
        n = await _db.poe_seed_ports.count_documents({})
        if not n:
            raise HTTPException(404, "poe_seed_ports vide — lancer POST /api/poe/seeds/build")
    if len(_active_ids()) >= MAX_PARALLEL_RUNS:
        raise HTTPException(409, f"déjà {MAX_PARALLEL_RUNS} runs actifs")
    if task_id in RUN_STATES and RUN_STATES[task_id].running:
        raise HTTPException(409, f"Enrich {task_id} already running")
    verdicts = [v for v in (body.verdicts or ["name_only"]) if v]
    state = TaskState(max_logs=2000)
    state.start()
    RUN_STATES[task_id] = state

    async def _runner():
        try:
            state.summary = await execute_enrich(
                _db, state, run_id=run_id, source=source,
                do_geocode=body.do_geocode, do_verify=body.do_verify,
                verdicts=verdicts, limit=int(body.limit or 0),
                concurrency=max(1, min(2, int(body.concurrency or 2))),
                use_agent=bool(body.use_agent))
        except Exception as e:
            state.error = f"{type(e).__name__}: {e}"
            state.log(f"FATAL: {state.error}")
            await _db.poe_runs.update_one({"_id": task_id}, {"$set": {
                "enrich_error": state.error,
                "enrich_finished_at": poe.now_iso()}}, upsert=True)
        finally:
            state.finish()

    asyncio.create_task(_runner())
    return {
        "status": "started",
        "source": source,
        "run_id": task_id,
        "limit": body.limit,
        "verdicts": verdicts,
        "do_geocode": body.do_geocode,
        "do_verify": body.do_verify,
        "use_agent": body.use_agent,
        "wrote_poe_ports": False,
    }


@router.get("/poe/seeds/enrich/status")
async def poe_seeds_enrich_status(run_id: str = "seed-enrich"):
    doc = await _db.poe_runs.find_one({"_id": run_id})
    if not doc:
        raise HTTPException(404, f"Run {run_id} unknown")
    out = {"run_id": run_id, "enrich": doc.get("enrich"),
           "enriched_at": doc.get("enriched_at"),
           "wrote_poe_ports": False}
    st = RUN_STATES.get(run_id)
    if st is not None:
        out["live"] = st.status()
    return out


@router.post("/poe/seeds/enrich/cancel")
async def poe_seeds_enrich_cancel(run_id: str = "seed-enrich"):
    st = RUN_STATES.get(run_id)
    if st is None or not st.running:
        raise HTTPException(409, f"Enrich {run_id} is not running")
    st.cancel = True
    st.log("Annulation enrich demandée — les graines restantes ne démarreront pas")
    return {"cancelling": True, "run_id": run_id}


@router.post("/poe/seeds/mine-sources", status_code=202)
async def poe_seeds_mine_sources(body: SeedMineBody | None = None):
    """Fetch already-paid judge_sources. 0 Search. No poe_ports."""
    from app.services.poe_seed_enrich import MINE_TASK_ID, mine_paid_sources

    body = body or SeedMineBody()
    n = await _db.poe_seed_ports.count_documents({})
    if not n:
        raise HTTPException(404, "poe_seed_ports vide — lancer POST /api/poe/seeds/build")
    if len(_active_ids()) >= MAX_PARALLEL_RUNS:
        raise HTTPException(409, f"déjà {MAX_PARALLEL_RUNS} runs actifs")
    if MINE_TASK_ID in RUN_STATES and RUN_STATES[MINE_TASK_ID].running:
        raise HTTPException(409, f"Mine {MINE_TASK_ID} already running")
    state = TaskState(max_logs=2000)
    state.start()
    RUN_STATES[MINE_TASK_ID] = state

    async def _runner():
        try:
            state.summary = await mine_paid_sources(
                _db, state,
                fetch_cap=max(1, min(800, int(body.fetch_cap or 200))),
                persist_memory=bool(body.persist_memory),
                mark_named=True,
                judge_residue=bool(body.judge_residue),
                residue_limit=max(0, int(body.residue_limit or 0)),
                include_runs=bool(body.include_runs),
                concurrency=max(1, min(2, int(body.concurrency or 2))),
                use_agent=bool(body.use_agent))
        except Exception as e:
            state.error = f"{type(e).__name__}: {e}"
            state.log(f"FATAL: {state.error}")
            await _db.poe_runs.update_one({"_id": MINE_TASK_ID}, {"$set": {
                "mine_error": state.error,
                "mine_finished_at": poe.now_iso()}}, upsert=True)
        finally:
            state.finish()

    asyncio.create_task(_runner())
    return {
        "status": "started",
        "run_id": MINE_TASK_ID,
        "fetch_cap": body.fetch_cap,
        "judge_residue": body.judge_residue,
        "residue_limit": body.residue_limit,
        "search": False,
        "wrote_poe_ports": False,
    }


@router.get("/poe/seeds/mine-sources/status")
async def poe_seeds_mine_status():
    from app.services.poe_seed_enrich import MINE_TASK_ID

    doc = await _db.poe_runs.find_one({"_id": MINE_TASK_ID})
    out = {"run_id": MINE_TASK_ID, "mine": (doc or {}).get("mine"),
           "mined_at": (doc or {}).get("mined_at"),
           "wrote_poe_ports": False, "search": False}
    st = RUN_STATES.get(MINE_TASK_ID)
    if st is not None:
        out["live"] = st.status()
    return out


@router.post("/poe/seeds/mine-sources/cancel")
async def poe_seeds_mine_cancel():
    from app.services.poe_seed_enrich import MINE_TASK_ID

    st = RUN_STATES.get(MINE_TASK_ID)
    if st is None or not st.running:
        raise HTTPException(409, f"Mine {MINE_TASK_ID} is not running")
    st.cancel = True
    st.log("Annulation mine demandée")
    return {"cancelling": True, "run_id": MINE_TASK_ID}


@router.get("/poe/listing-control/review")
async def listing_control_review(run_id: str | None = None, reason: str | None = None,
                                 skip: int = 0, limit: int = 500):
    q: dict = {}
    if run_id:
        q["run_id"] = run_id
    if reason:
        q["reason"] = reason
    total = await _db.poe_listing_review.count_documents(q)
    docs = await _db.poe_listing_review.find(q).skip(skip).to_list(min(limit, 2000))
    return {"total": total, "skip": skip, "count": len(docs), "items": docs}


@router.get("/poe/runs/compare")
async def poe_runs_compare(run_ids: str, include_v1: bool = True):
    ids = [x.strip() for x in run_ids.split(",") if x.strip()]
    if not ids:
        raise HTTPException(400, "run_ids requis (csv)")
    for rid in ids:
        if not await _db.poe_runs.find_one({"_id": rid}):
            raise HTTPException(404, f"Run {rid} unknown")
    return await compare_runs(_db, ids, include_v1=include_v1)


@router.post("/poe/runs/best-of")
async def poe_runs_best_of(body: BestOfBody):
    if not body.run_ids:
        raise HTTPException(400, "run_ids requis")
    for rid in body.run_ids:
        if not await _db.poe_runs.find_one({"_id": rid}):
            raise HTTPException(404, f"Run {rid} unknown")
    summary = await synthesize_best_of(
        _db, body.run_ids, include_v1=body.include_v1, label=body.label)
    return {"status": "done", **summary}


@router.get("/poe/runs/{run_id}/status")
async def poe_run_status(run_id: str):
    doc = await _db.poe_runs.find_one({"_id": run_id})
    if not doc:
        raise HTTPException(404, f"Run {run_id} unknown")
    from app.core.tokenfactory import public_cost
    persisted = (doc.get("summary") or {}).get("tokenfactory") if isinstance(doc.get("summary"), dict) else None
    out = {"run": doc, "tokenfactory": public_cost(run_id, persisted)}
    st = RUN_STATES.get(run_id)
    if st is not None and st.running:
        out["live"] = st.status()
    return out


@router.get("/poe/runs/{run_id}")
async def poe_run_detail(run_id: str):
    doc = await _db.poe_runs.find_one({"_id": run_id})
    if not doc:
        raise HTTPException(404, f"Run {run_id} unknown")
    rules = (doc.get("params") or {}).get("rules") or {}
    fields = snapshot_list_fields(doc.get("params") or {})
    return {
        "id": str(doc.get("_id")),
        "label": doc.get("label"),
        "state": doc.get("state"),
        "created_at": doc.get("created_at"),
        "finished_at": doc.get("finished_at"),
        "kind": (doc.get("params") or {}).get("variant") or "poe",
        "error": doc.get("error"),
        "summary": doc.get("summary"),
        "chosen": rules.get("chosen") or {},
        "params": {"rules": rules},
        **fields,
    }


@router.post("/poe/runs/{run_id}/cancel")
async def poe_run_cancel(run_id: str):
    st = RUN_STATES.get(run_id)
    if st is None or not st.running:
        raise HTTPException(409, f"Run {run_id} is not running")
    st.cancel = True
    st.log("Annulation demandée — les zones restantes ne démarreront pas")
    return {"cancelling": True, "run_id": run_id}


@router.get("/poe/runs/{run_id}/ports")
async def poe_run_ports(run_id: str, mrgid: int | None = None):
    q: dict = {"run_id": run_id}
    if mrgid is not None:
        q["mrgid"] = mrgid
    docs = await _db.poe_run_ports.find(q).to_list(20000)
    return poe.ports_to_geojson(docs)


@router.get("/poe/runs/{run_id}/events")
async def poe_run_events(run_id: str, step: str | None = None, mrgid: int | None = None,
                         skip: int = 0, limit: int = 500):
    q: dict = {"run_id": run_id}
    if step:
        q["step"] = step
    if mrgid is not None:
        q["mrgid"] = mrgid
    total = await _db.poe_run_events.count_documents(q)
    docs = await _db.poe_run_events.find(q).sort("seq", 1).skip(skip).to_list(min(limit, 2000))
    return {"total": total, "skip": skip, "count": len(docs), "items": docs}


@router.get("/poe/runs/{run_id}/diff")
async def poe_run_diff(run_id: str, moved_km: float = 2.0, full: bool = False):
    if not await _db.poe_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    diff = await diff_run_vs_baseline(_db, run_id, moved_km=moved_km)
    if not full:
        diff["matched"] = [m for m in diff["matched"] if m["flags"]][:200]
        diff["added"] = diff["added"][:200]
        diff["removed"] = diff["removed"][:200]
    return diff


@router.get("/poe/runs/{run_id}/report")
async def poe_run_report(run_id: str, format: str = "json"):
    if not await _db.poe_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    t0 = time.time()
    rep = await build_run_report(_db, run_id)
    rep["built_in_s"] = round(time.time() - t0, 2)
    if format == "markdown":
        return PlainTextResponse(report_to_markdown(rep), media_type="text/markdown; charset=utf-8")
    return rep


@router.get("/poe/runs/{run_id}/listing-control")
async def poe_run_listing_control(run_id: str, restrict_to_run_zones: bool = True):
    if not await _db.poe_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    return await build_listing_control_report(
        _db, run_id, restrict_to_run_zones=restrict_to_run_zones)


@router.post("/poe/runs/{run_id}/listing-control")
async def poe_run_listing_control_persist(run_id: str, restrict_to_run_zones: bool = True):
    if not await _db.poe_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    report = await build_listing_control_report(
        _db, run_id, restrict_to_run_zones=restrict_to_run_zones)
    persisted = await persist_review(_db, report)
    return {"report": report, "persisted": persisted}


class UnifiedRunBody(BaseModel):
    layer: str = "poe"
    run_id: str | None = None
    resume: bool = False
    retry_errored: bool = False         # resume only: redo errored zones once
    force: bool = False                 # resume only: take over a run marked running
    label: str = ""
    limit: int = 0
    concurrency: int = 2
    zones: list[int] | None = None
    variant: str = "tinyfish"
    profile: str | None = None
    rules: dict | None = None


@router.post("/runs", status_code=202)
async def unified_run_start(body: UnifiedRunBody | None = None):
    """Single entry point (P0-4): new run, or resume of an existing run_id."""
    body = body or UnifiedRunBody()
    try:
        unified_runs.normalize_layer(body.layer)
        variant = normalize_variant(body.variant)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.resume and not body.run_id:
        raise HTTPException(400, "run_id required to resume")
    run_id = body.run_id or poe_runs.new_run_id()
    label = body.label or ("" if body.resume else f"{variant}-full")
    if not body.resume and (body.rules or body.profile):
        from app.core.run_rules import RuleError
        from app.db import get_settings
        try:
            snapshot_for_run(mode="formalities", settings=await get_settings(),
                             overrides=body.rules, profile=body.profile)
        except RuleError as e:
            raise HTTPException(400, str(e)) from e
    await _launch_run(resume=body.resume, run_id=run_id, label=label,
                      limit=body.limit, zones=body.zones,
                      concurrency=body.concurrency, variant=variant,
                      rules_overrides=None if body.resume else body.rules,
                      profile=None if body.resume else body.profile,
                      retry_errored=body.retry_errored,
                      force=body.resume and body.force)
    return {"status": "started", "run_id": run_id, "resume": body.resume,
            "layer": "poe", "variant": variant}


@router.get("/runs/{run_id}/manifest")
async def unified_run_manifest(run_id: str):
    from app.services import run_artifacts
    try:
        path = run_artifacts.manifest_path(run_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    doc = run_artifacts.read_manifest(path)
    if doc is None:
        raise HTTPException(404, f"no manifest for run {run_id}")
    return doc


@router.post("/runs/{run_id}/export")
async def unified_run_export(run_id: str):
    """Export a finished run (GeoJSON + manifest). Upload only if enabled."""
    try:
        return await unified_runs.export_run(_db, run_id)
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/runs/{run_id}/upload")
async def unified_run_upload(run_id: str):
    """Upload an existing export again (after a failed upload); never re-exports.
    The export is checked against its manifest hashes first."""
    try:
        return await unified_runs.upload_existing_export(_db, run_id)
    except unified_runs.ExportIncomplete as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
