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
v1 ↔ run comparison and the report live in poe_diff / poe_report.
"""
import asyncio
import time
import uuid

from app.core.events import RunContext, RunRecorder
from app.services import poe_pipeline as poe
from app.services.poe_pipeline import normalize_variant
from app.core.run_rules import attach_rules, bind_rules, get_rule, snapshot_for_run
from app.services.run_fingerprint import (
    build_code_fingerprint, merge_run_params, resume_params,
)

ZONE_TIMEOUT_S = 900  # the v2 pipeline does more work (render, dual geocoding)


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
                      profile: str | None = None) -> dict:
    """Execute (or resume) a full run. `state` is a TaskState (live logs,
    progress, cancel); durable state lives in db.poe_runs.
    variant: v1 | v2 | tinyfish — never writes poe_ports.
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
    if resume:
        done_mrgids = {z["mrgid"] async for z in db.poe_run_zones.find(
            {"run_id": run_id}, {"mrgid": 1})}
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
    log(f"run {run_id}: variant={variant} — {len(todo)} zone(s) à générer from scratch "
        f"(concurrency={concurrency}, timeout {timeout_s}s/zone, "
        f"rules={rules.get('hash')} profile={rules.get('profile')})")

    from app.core.tokenfactory import (calls_taken, plan_zone_calls, restore_calls,
                                       restore_spend, summary as cost_summary)
    if resume:  # the call count and the USD ledger live in memory: a restart would hand
        # out a fresh cap and budget, and the summary would only cover this session
        stored = [c for c in (existing.get("tokenfactory_cost"),
                              (existing.get("summary") or {}).get("tokenfactory"))
                  if isinstance(c, dict)]
        restore_calls(existing.get("calls_taken") or max(
            [int(c.get("calls_taken") or 0) for c in stored], default=0), run_id)
        if stored:
            restore_spend(max(stored, key=lambda c: int(c.get("calls") or 0)), run_id)
    plan_zone_calls(len(todo), run_id)  # each zone gets a fair share of the call cap
    sem = asyncio.Semaphore(max(1, concurrency))
    counter = {"done": len(done_mrgids), "errors": 0}
    t0 = time.time()

    async def _one(z):
        mrgid = z["mrgid"]
        name = z.get("name") or z.get("geoname")
        async with sem:
            if state.cancel:
                return
            # Batch reading stops before the timeout and keeps the batches already read.
            deadline_token = poe.set_zone_deadline(timeout_s)
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
            except Exception as e:
                counter["errors"] += 1
                log(f"[{name}] FAILED: {type(e).__name__}: {e}")
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
                poe.zone_deadline.reset(deadline_token)
                counter["done"] += 1
                state.progress = counter["done"]
                await db.poe_runs.update_one({"_id": run_id}, {"$set": {
                    "zones_done": counter["done"], "last_zone": name,
                    "calls_taken": calls_taken(run_id),
                    "tokenfactory_cost": cost_summary(run_id),
                }})

    await asyncio.gather(*(_one(z) for z in todo))

    cancelled = state.cancel
    by_status: dict[str, int] = {}
    async for z in db.poe_run_zones.find({"run_id": run_id}, {"status": 1}):
        s = z.get("status") or "erreur"
        by_status[s] = by_status.get(s, 0) + 1
    ports_total = await db.poe_run_ports.count_documents({"run_id": run_id})
    zones_partial = await db.poe_run_zones.count_documents(
        {"run_id": run_id, "extraction_partial": True})
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
        "zones_partial": zones_partial,
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
    log(f"run {run_id} terminé: {summary}")
    return summary
