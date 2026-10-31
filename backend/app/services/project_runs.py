"""project_runs — Isolated Project swarm runs (CDC v2, phase B).

A run writes only into:
  - project_runs            : meta (params, progress, summary)
  - project_run_projects    : extracted sites / projects {run_id, …}
  - project_run_events      : micro-steps (RunRecorder)
  - project_run_journal     : full narrative (swarm + agent logs), uncapped

The v1 `projects` collection is NEVER written (wrote_projects: false).
Map promotion = phase D, manual, outside this module.
"""
import time
import uuid

from datetime import datetime, timezone

from app.core.events import RunRecorder
from app.core.run_rules import attach_rules, bind_rules, snapshot_for_run
from app.services.run_fingerprint import build_code_fingerprint, merge_run_params


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]


async def ensure_run_indexes(db):
    try:
        await db.project_run_projects.create_index([("run_id", 1), ("url", 1)], unique=True)
        await db.project_run_projects.create_index("run_id")
        await db.project_run_events.create_index([("run_id", 1), ("seq", 1)])
        await db.project_run_events.create_index([("run_id", 1), ("step", 1)])
        await db.project_run_journal.create_index([("run_id", 1), ("seq", 1)])
        await db.project_run_journal.create_index([("run_id", 1), ("kind", 1)])
        await db.project_runs.create_index("created_at")
    except Exception:
        pass


def empty_counters() -> dict:
    return {
        "sites": 0,
        "unlocated": 0,
        "rejected": 0,
        "failed": 0,
        "merged_v1": 0,
        "merged_run": 0,
        "seen_v1": 0,
    }


COUNTER_FOR_VERDICT = {
    "site": "sites",
    "unlocated": "unlocated",
    "rejected": "rejected",
    "failed": "failed",
    "merged_v1": "merged_v1",
    "merged_run": "merged_run",
    "seen_v1": "seen_v1",
}


async def open_run(db, *, mode: str, label: str = "", settings: dict | None = None,
                   force_rescan: bool = False, run_id: str | None = None,
                   to_file: bool = True, rules_overrides: dict | None = None,
                   profile: str | None = None) -> dict:
    """Create the run document. Do not write `projects`."""
    await ensure_run_indexes(db)
    rid = run_id or new_run_id()
    settings = settings or {}
    fingerprint = build_code_fingerprint(settings)
    rules = snapshot_for_run(mode="projects", settings=settings,
                             overrides=rules_overrides, profile=profile)
    params = attach_rules(merge_run_params({
        "mode": mode,
        "label": label,
        "force_rescan": force_rescan,
        "wrote_projects": False,
        "profile": rules["profile"],
    }, fingerprint), rules)
    bind_rules(rules)
    rec = RunRecorder(rid, db=db, events_coll="project_run_events", to_file=to_file)
    doc = {
        "_id": rid,
        "label": label or f"projects-{mode}",
        "mode": mode,
        "state": "running",
        "wrote_projects": False,
        "params": params,
        "counters": empty_counters(),
        "created_at": now_iso(),
        "started_at": now_iso(),
        "error": None,
    }
    await db.project_runs.update_one(
        {"_id": rid},
        {"$setOnInsert": doc},
        upsert=True,
    )
    await rec.event("run_start", mode=mode, label=doc["label"], wrote_projects=False)
    from app.services.run_artifacts import record_open
    await record_open(rid, "projects", params)
    from app.core.tokenfactory import bind_run
    bind_run(rid)
    return {"run_id": rid, "recorder": rec, "wrote_projects": False}


async def bump_counter(db, run_id: str, key: str, n: int = 1):
    if not run_id or key not in empty_counters():
        return
    await db.project_runs.update_one(
        {"_id": run_id},
        {"$inc": {f"counters.{key}": n}},
    )


async def write_run_project(db, run_id: str, doc: dict) -> str:
    """Upsert a run row. Never write `projects`."""
    if not run_id:
        raise ValueError("run_id required — isolated run only")
    url = doc.get("url")
    if not url:
        raise ValueError("url required")
    rid = doc.get("_id") or str(uuid.uuid4())
    payload = {k: v for k, v in doc.items() if k != "_id"}
    payload["run_id"] = run_id
    payload["wrote_projects"] = False
    payload["url"] = url
    if "updated_at" not in payload:
        payload["updated_at"] = now_iso()
    await db.project_run_projects.update_one(
        {"run_id": run_id, "url": url},
        {"$set": payload, "$setOnInsert": {"_id": rid, "created_at": now_iso()}},
        upsert=True,
    )
    return rid


async def finalize_run(db, run_id: str, *, cancelled: bool = False, error: str | None = None,
                       extra: dict | None = None):
    if not run_id:
        return
    rec = RunRecorder(run_id, db=db, events_coll="project_run_events")
    await rec.resume_seq()
    doc = await db.project_runs.find_one({"_id": run_id}) or {}
    counters = doc.get("counters") or empty_counters()
    n_sites = await db.project_run_projects.count_documents({"run_id": run_id, "verdict": "site"})
    from app.services.run_journal import count_journal_file, load_journal
    try:
        journal_lines = count_journal_file(run_id)
        if journal_lines == 0:
            packed = await load_journal(db, run_id, skip=0, limit=1)
            journal_lines = packed.get("total") or 0
    except Exception:
        journal_lines = 0
    summary = {
        "run_id": run_id,
        "wrote_projects": False,
        "counters": counters,
        "sites_in_run": n_sites,
        "journal_lines": journal_lines,
        "cancelled": cancelled,
        **(extra or {}),
    }
    from app.core.tokenfactory import attach
    attach(summary, run_id)
    from app.core.tavily import attach as tavily_attach
    tavily_attach(summary, run_id)
    state = "cancelled" if cancelled else ("failed" if error else "done")
    await db.project_runs.update_one({"_id": run_id}, {"$set": {
        "state": state,
        "finished_at": now_iso(),
        "error": error,
        "summary": summary,
        "journal_lines": journal_lines,
        "wrote_projects": False,
    }})
    await rec.event("run_done", **summary)
    from app.services.run_artifacts import record_close
    await record_close(run_id, "projects", doc.get("params"), state=state,
                       counts={"sites_in_run": n_sites, **counters},
                       cost=summary.get("tokenfactory"))


async def diff_run_vs_v1(db, run_id: str) -> dict:
    """Compare run URLs to the v1 map. No write."""
    run_docs = await db.project_run_projects.find({"run_id": run_id}).to_list(20000)
    v1 = await db.projects.find({}, {"url": 1, "title": 1}).to_list(30000)
    v1_urls = {p.get("url") for p in v1 if p.get("url")}
    added, known, unlocated = [], [], []
    for d in run_docs:
        url = d.get("url")
        row = {"url": url, "title": d.get("title"), "verdict": d.get("verdict"),
               "lat": d.get("lat"), "lon": d.get("lon")}
        if d.get("verdict") in ("unlocated", "rejected", "failed"):
            unlocated.append(row)
        elif url in v1_urls or d.get("verdict") in ("merged_v1", "seen_v1"):
            known.append(row)
        else:
            added.append(row)
    return {
        "run_id": run_id,
        "wrote_projects": False,
        "summary": {
            "run_total": len(run_docs),
            "added": len(added),
            "already_in_v1": len(known),
            "unlocated_or_rejected": len(unlocated),
        },
        "added": added[:500],
        "already_in_v1": known[:200],
        "unlocated_or_rejected": unlocated[:200],
    }


async def build_run_report(db, run_id: str) -> dict:
    """JSON report of a Projects run (modeled on the PoE report, simpler)."""
    run = await db.project_runs.find_one({"_id": run_id}) or {}
    docs = await db.project_run_projects.find({"run_id": run_id}).to_list(20000)
    events = await db.project_run_events.find(
        {"run_id": run_id}, {"step": 1},
    ).to_list(20000)
    by_verdict: dict[str, int] = {}
    for d in docs:
        v = d.get("verdict") or "?"
        by_verdict[v] = by_verdict.get(v, 0) + 1
    by_step: dict[str, int] = {}
    for e in events:
        s = e.get("step") or "?"
        by_step[s] = by_step.get(s, 0) + 1
    diff = await diff_run_vs_v1(db, run_id)
    return {
        "run_id": run_id,
        "wrote_projects": False,
        "label": run.get("label"),
        "mode": run.get("mode"),
        "state": run.get("state"),
        "counters": run.get("counters") or empty_counters(),
        "by_verdict": by_verdict,
        "events_total": len(events),
        "events_by_step": by_step,
        "journal_lines": run.get("journal_lines") or (run.get("summary") or {}).get("journal_lines") or 0,
        "summary": run.get("summary"),
        "diff": diff.get("summary"),
    }


def report_to_markdown(rep: dict) -> str:
    counters = rep.get("counters") or {}
    verdicts = rep.get("by_verdict") or {}
    diff = rep.get("diff") or {}
    lines = [
        f"# Run Projets {rep.get('run_id')}",
        "",
        f"- état : {rep.get('state')}",
        f"- mode : {rep.get('mode')}",
        f"- wrote_projects : false",
        f"- sites : {counters.get('sites', verdicts.get('site', 0))}",
        f"- unlocated : {counters.get('unlocated', 0)}",
        f"- rejected : {counters.get('rejected', 0)}",
        f"- failed : {counters.get('failed', 0)}",
        f"- déjà en v1 (seen/merged) : {counters.get('seen_v1', 0)} / {counters.get('merged_v1', 0)}",
        f"- événements : {rep.get('events_total', 0)}",
        f"- journal : {rep.get('journal_lines', 0)} lignes",
        f"- diff vs carte : +{diff.get('added', 0)} nouveaux, "
        f"{diff.get('already_in_v1', 0)} déjà connus, "
        f"{diff.get('unlocated_or_rejected', 0)} écartés",
        "",
        "Promotion carte = phase D (manuelle).",
    ]
    return "\n".join(lines) + "\n"


def run_projects_to_geojson(run_id: str, docs: list[dict]) -> dict:
    """FeatureCollection of a run (same properties as the live map)."""
    feats = []
    for d in docs:
        lat, lon = d.get("lat"), d.get("lon")
        if lat is None or lon is None:
            continue
        funders = d.get("funders")
        funder = ", ".join(funders) if funders else (d.get("funder") or "")
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "id": d.get("_id"),
                "run_id": run_id,
                "title": d.get("title"),
                "url": d.get("url"),
                "description": d.get("description", ""),
                "funder": funder,
                "location": d.get("location"),
                "s_ocean": d.get("s_ocean"),
                "snapped": d.get("snapped", False),
                "image": d.get("image"),
                "category": d.get("category"),
                "category_group": d.get("category_group"),
                "verdict": d.get("verdict"),
            },
        })
    return {
        "type": "FeatureCollection",
        "run_id": run_id,
        "wrote_projects": False,
        "features": feats,
    }


def project_to_public(d: dict) -> dict:
    return {
        "id": d.get("_id"),
        "run_id": d.get("run_id"),
        "title": d.get("title"),
        "url": d.get("url"),
        "funder": d.get("funder"),
        "funders": d.get("funders"),
        "location": d.get("location"),
        "lat": d.get("lat"),
        "lon": d.get("lon"),
        "s_ocean": d.get("s_ocean"),
        "verdict": d.get("verdict"),
        "geo_source": d.get("geo_source"),
        "category_group": d.get("category_group"),
        "wrote_projects": False,
    }
