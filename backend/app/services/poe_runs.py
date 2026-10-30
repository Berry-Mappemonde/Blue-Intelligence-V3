"""poe_runs — Versioned “from scratch” PoE pipeline runs.

A run regenerates the selected zones (force, monitoring short-circuited)
in a dedicated space, never touching v1 collections:
  - poe_runs        : run meta (parameters, progress, summary);
  - poe_run_zones   : per-zone result {run_id, mrgid, status, poe_count…};
  - poe_run_ports   : extracted ports {run_id, …};
  - poe_run_events  : structured journal of each micro-step (events_core),
                      also JSONL in backend/data/runs/<run_id>.jsonl.

Resume: an interrupted run (server restart…) continues where it
stopped — zones already in poe_run_zones for this run are skipped.
Mongo is the source of truth: a zone marked `unit_done` in
data/runs/<run_id>/trace.jsonl but missing from poe_run_zones is
flagged (`unit_missing_in_mongo`) and re-run. Errored zones count as
done unless retry_errored=True.
Each run also keeps data/runs/<run_id>/manifest.json (run_artifacts).
v1 ↔ run comparison and the report live in poe_diff / poe_report.

Launch through unified_runs.start_run; calling execute_run directly
still works but is deprecated.
"""
import asyncio
import logging
import time
import uuid

from app.core.events import RunContext, RunRecorder
from app.services import poe_pipeline as poe
from app.services import run_artifacts as artifacts
from app.services.poe_pipeline import normalize_variant
from app.core.run_rules import attach_rules, bind_rules, get_rule, snapshot_for_run
from app.services.run_fingerprint import (
    build_code_fingerprint, merge_run_params, resume_params,
)

logger = logging.getLogger(__name__)

ZONE_TIMEOUT_S = 900  # the v2 pipeline does more work (render, dual geocoding)
LAYER = "poe"


async def _artifact(fn, *args, **kwargs):
    """File artifacts must never break a run: Mongo stays the source of truth."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        logger.warning("run artifact %s failed: %s", getattr(fn, "__name__", fn), exc)
        return None


async def _run_sources(db, run_id: str) -> dict:
    urls = []
    try:
        async for z in db.poe_run_zones.find({"run_id": run_id}, {"sources": 1}):
            for s in z.get("sources") or []:
                urls.append(s.get("url") if isinstance(s, dict) else s)
    except Exception:
        pass
    return artifacts.sources_summary(urls)


async def write_manifest(db, run_id: str, state: str, params: dict,
                         counts: dict, cost: dict | None = None) -> str | None:
    manifest = artifacts.build_manifest(
        run_id=run_id, layer=LAYER, state=state, params=params, counts=counts,
        sources=await _run_sources(db, run_id), cost=cost)
    sha = await _artifact(artifacts.write_run_manifest, run_id, manifest)
    if sha:
        await db.poe_runs.update_one({"_id": run_id}, {"$set": {
            "artifacts": {
                "dir": artifacts.data_relative(artifacts.run_dir(run_id),
                                               artifacts.runs_root()),
                "trace": artifacts.TRACE_NAME,
                "manifest": artifacts.MANIFEST_NAME,
            },
            "manifest_sha256": sha,
        }})
    return sha


def _trace_terminal_units(run_id: str) -> set:
    return {r.get("unit") for r in artifacts.read_trace(run_id)
            if r.get("event") in ("unit_done", "unit_error")}


def _retried_units(run_id: str) -> set:
    """Zones already retried once: unit_start lines with retry=True."""
    return {r.get("unit") for r in artifacts.read_trace(run_id)
            if r.get("event") == "unit_start" and r.get("retry")}


async def _retryable_zones(db, run_id: str, selected: set, zone_status: dict,
                           log) -> set[int]:
    """Errored zones to redo once on resume (retry_errored=True).

    Errored = the last unit_done / unit_error line of the trace is an error;
    zones without any trace line fall back to their poe_run_zones status
    (runs started before the trace existed). A zone that already has ports
    stored for this run is not retried, so stored ports are never replaced.
    A zone is retried at most once across all resumes: each retry is recorded
    as a unit_start line with retry=True, and such zones are not retried again.
    """
    errored = {int(m) for m in (await _artifact(artifacts.errored_units, run_id) or set())
               if isinstance(m, int)}
    terminal = await _artifact(_trace_terminal_units, run_id) or set()
    retried = await _artifact(_retried_units, run_id) or set()
    errored |= {m for m, s in zone_status.items()
                if m not in terminal and artifacts.is_error_status(s)}
    retry: set[int] = set()
    for mrgid in sorted(errored & selected):
        if mrgid in retried:
            log(f"zone {mrgid} en erreur mais déjà relancée une fois — non relancée")
            await _artifact(artifacts.append_trace, run_id, "unit_retry_skipped",
                            unit=mrgid, reason="already retried once")
            continue
        if await db.poe_run_ports.count_documents({"run_id": run_id, "mrgid": mrgid}):
            log(f"zone {mrgid} en erreur mais a déjà des ports stockés — non relancée")
            await _artifact(artifacts.append_trace, run_id, "unit_retry_skipped",
                            unit=mrgid, reason="ports already stored")
            continue
        retry.add(mrgid)
    if retry:
        log(f"reprise du run {run_id}: {len(retry)} zone(s) en erreur relancée(s)")
    return retry


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]


async def ensure_run_indexes(db):
    try:
        await db.poe_run_ports.create_index([("run_id", 1), ("mrgid", 1)])
        await db.poe_run_zones.create_index([("run_id", 1), ("mrgid", 1)], unique=True)
        await db.poe_run_events.create_index([("run_id", 1), ("seq", 1)])
        await db.poe_run_events.create_index([("run_id", 1), ("step", 1)])
    except Exception:
        pass


async def _select_zones(db, limit: int, only_zones: list[int] | None) -> list[dict]:
    q: dict = {}
    if only_zones:
        q["mrgid"] = {"$in": [int(m) for m in only_zones]}
    docs = await db.eez_zones.find(q, {"geometry": 0}).to_list(500)
    docs.sort(key=lambda d: (d.get("name") or "").lower())
    if limit and limit > 0:
        docs = docs[:limit]
    return docs


async def execute_run(db, state, run_id: str, label: str = "",
                      limit: int = 0, only_zones: list[int] | None = None,
                      concurrency: int = 2, resume: bool = False,
                      variant: str = "tinyfish",
                      rules_overrides: dict | None = None,
                      profile: str | None = None,
                      retry_errored: bool = False) -> dict:
    """Execute (or resume) a full run. `state` is a TaskState (live logs,
    progress, cancel); durable state lives in db.poe_runs.
    variant: v1 | v2 | tinyfish — never writes poe_ports.
    retry_errored: on resume, errored zones are redone once in total (see
    _retryable_zones); completed zones are never re-run.
    """
    await ensure_run_indexes(db)
    from app.core.tokenfactory import bind_run
    bind_run(run_id)
    variant = normalize_variant(variant)
    recorder = RunRecorder(run_id, db=db)
    run_ctx = RunContext(run_id=run_id, recorder=recorder, variant=variant)
    log = state.log

    zones = await _select_zones(db, limit, only_zones)
    if not zones:
        raise ValueError("aucune ZEE candidate — construire le référentiel d'abord")

    done_mrgids: set[int] = set()
    retry_mrgids: set[int] = set()
    if resume:
        zone_status = {z["mrgid"]: z.get("status") async for z in db.poe_run_zones.find(
            {"run_id": run_id}, {"mrgid": 1, "status": 1})}
        done_mrgids = set(zone_status)
        traced = await _artifact(artifacts.completed_units, run_id) or set()
        selected = {z["mrgid"] for z in zones}
        for mrgid in sorted(m for m in traced if isinstance(m, int)
                            and m in selected and m not in done_mrgids):
            log(f"zone {mrgid} terminée dans la trace mais absente de Mongo — relancée")
            await _artifact(artifacts.append_trace, run_id, "unit_missing_in_mongo",
                            unit=mrgid)
        if retry_errored:
            retry_mrgids = await _retryable_zones(db, run_id, selected, zone_status, log)
            done_mrgids -= retry_mrgids
        if done_mrgids:
            log(f"reprise du run {run_id}: {len(done_mrgids)} zone(s) déjà générées, sautées")
    todo = [z for z in zones if z["mrgid"] not in done_mrgids]

    state.total = len(zones)
    state.progress = len(done_mrgids)
    settings = {}
    try:
        from app.db import get_settings
        settings = await get_settings()
    except Exception:
        pass
    timeout_s = int(get_rule("formalities.zone_timeout_s", ZONE_TIMEOUT_S))
    if resume:
        existing = await db.poe_runs.find_one({"_id": run_id}) or {}
        existing_params = existing.get("params") or {}
        rules = existing_params.get("rules") or snapshot_for_run(
            mode="formalities", settings=settings,
            overrides=rules_overrides, profile=profile)
        timeout_s = int((rules.get("chosen") or {}).get(
            "formalities.zone_timeout_s", {}).get("value") or timeout_s)
        fingerprint = build_code_fingerprint(settings, zone_timeout_s=timeout_s)
        base = {"label": label, "limit": limit, "concurrency": concurrency,
                "only_zones": only_zones, "force": True, "variant": variant,
                "profile": rules.get("profile")}
        params = resume_params(existing_params or base, fingerprint)
        params.update({"label": label, "limit": limit, "concurrency": concurrency,
                       "only_zones": only_zones, "force": True, "variant": variant})
        if not isinstance(params.get("rules"), dict):
            params = attach_rules(params, rules)
    else:
        rules = snapshot_for_run(mode="formalities", settings=settings,
                                 overrides=rules_overrides, profile=profile)
        timeout_s = int((rules.get("chosen") or {}).get(
            "formalities.zone_timeout_s", {}).get("value") or timeout_s)
        fingerprint = build_code_fingerprint(settings, zone_timeout_s=timeout_s)
        base = {"label": label, "limit": limit, "concurrency": concurrency,
                "only_zones": only_zones, "force": True, "variant": variant,
                "profile": rules["profile"]}
        params = attach_rules(merge_run_params(base, fingerprint), rules)
    bind_rules(rules)
    await db.poe_runs.update_one({"_id": run_id}, {"$set": {
        "label": label, "params": params, "state": "running",
        "started_at": poe.now_iso() if not resume else None,
        "resumed": resume, "zones_total": len(zones),
        "zones_done": len(done_mrgids), "error": None,
    }, "$setOnInsert": {"created_at": poe.now_iso()}}, upsert=True)
    if not resume:
        await recorder.event("run_start", params=params, zones_total=len(zones))
    else:
        await recorder.event("run_resume", params=params, zones_total=len(zones),
                             already_done=len(done_mrgids))
    await _artifact(artifacts.append_trace, run_id,
                    "run_resume" if resume else "run_start", layer=LAYER,
                    zones_total=len(zones), already_done=sorted(done_mrgids),
                    retry=sorted(retry_mrgids), variant=variant)
    await write_manifest(db, run_id, "running", params, {
        "zones_total": len(zones), "zones_done": len(done_mrgids)})
    log(f"run {run_id}: variant={variant} — {len(todo)} zone(s) à générer from scratch "
        f"(concurrency={concurrency}, timeout {timeout_s}s/zone, "
        f"rules={rules.get('hash')} profile={rules.get('profile')})")

    sem = asyncio.Semaphore(max(1, concurrency))
    counter = {"done": len(done_mrgids), "errors": 0}
    t0 = time.time()

    async def _one(z):
        mrgid = z["mrgid"]
        name = z.get("name") or z.get("geoname")
        async with sem:
            if state.cancel:
                return
            await _artifact(artifacts.append_trace, run_id, "unit_start",
                            unit=mrgid, zone=name, retry=mrgid in retry_mrgids)
            try:
                doc = await asyncio.wait_for(
                    poe.generate_zone_poe(
                        db, mrgid,
                        logger=lambda m: log(f"[{name}] {m}"),
                        force=True, run=run_ctx),
                    timeout=timeout_s)
                state.results.append({"mrgid": mrgid, "name": name,
                                      "status": (doc or {}).get("status"),
                                      "poe_count": (doc or {}).get("poe_count", 0)})
                await _artifact(artifacts.append_trace, run_id, "unit_done",
                                unit=mrgid, zone=name,
                                status=(doc or {}).get("status"),
                                poe_count=(doc or {}).get("poe_count", 0))
            except Exception as e:
                counter["errors"] += 1
                log(f"[{name}] FAILED: {type(e).__name__}: {e}")
                await _artifact(artifacts.append_trace, run_id, "unit_error",
                                unit=mrgid, zone=name,
                                error=f"{type(e).__name__}: {str(e)[:200]}")
                await recorder.event("zone_error", mrgid=mrgid, zone=name,
                                     error=f"{type(e).__name__}: {str(e)[:200]}")
                await db.poe_run_zones.update_one(
                    {"run_id": run_id, "mrgid": mrgid},
                    {"$set": {"run_id": run_id, "mrgid": mrgid, "name": name,
                              "status": "erreur", "poe_count": 0,
                              "last_error": f"{type(e).__name__}: {str(e)[:200]}",
                              "generated_at": poe.now_iso()}},
                    upsert=True)
                state.results.append({"mrgid": mrgid, "name": name, "error": str(e)[:120]})
            finally:
                counter["done"] += 1
                state.progress = counter["done"]
                await db.poe_runs.update_one({"_id": run_id}, {"$set": {
                    "zones_done": counter["done"], "last_zone": name,
                }})

    await asyncio.gather(*(_one(z) for z in todo))

    cancelled = state.cancel
    by_status: dict[str, int] = {}
    async for z in db.poe_run_zones.find({"run_id": run_id}, {"status": 1}):
        s = z.get("status") or "erreur"
        by_status[s] = by_status.get(s, 0) + 1
    ports_total = await db.poe_run_ports.count_documents({"run_id": run_id})
    catalog_skipped = 0
    try:
        catalog_skipped = await db.poe_run_events.count_documents({
            "run_id": run_id, "step": "extraction_compare",
            "payload.skipped_llm": True,
        })
    except Exception:
        pass
    summary = {
        "run_id": run_id,
        "zones_total": len(zones),
        "zones_done": counter["done"],
        "by_status": by_status,
        "ports_total": ports_total,
        "errors": counter["errors"],
        "cancelled": cancelled,
        "duration_s": round(time.time() - t0, 1),
        "catalog_skipped_zones": catalog_skipped,
        "git_sha": (params.get("code") or {}).get("git_sha"),
    }
    from app.core.tokenfactory import attach
    attach(summary, run_id)
    await db.poe_runs.update_one({"_id": run_id}, {"$set": {
        "state": "cancelled" if cancelled else "done",
        "finished_at": poe.now_iso(),
        "summary": summary,
        "by_status": by_status,
        "ports_total": ports_total,
    }})
    await recorder.event("run_done", **summary)
    final_state = "cancelled" if cancelled else "done"
    await _artifact(artifacts.append_trace, run_id, "run_done", state=final_state,
                    zones_done=counter["done"], ports_total=ports_total,
                    errors=counter["errors"])
    await write_manifest(db, run_id, final_state, params, {
        "zones_total": len(zones), "zones_done": counter["done"],
        "by_status": by_status, "ports_total": ports_total,
        "errors": counter["errors"],
    }, cost=summary.get("tokenfactory"))
    log(f"run {run_id} terminé: {summary}")
    return summary
