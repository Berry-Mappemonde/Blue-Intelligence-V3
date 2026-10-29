"""Isolated Projects runs API — never writes `projects`."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.core.tasks import TaskState
from app.core.run_rules import RuleError, snapshot_list_fields
from app.core.from_scratch import resolve_from_scratch
from app.db import db, get_settings
from app.services import project_runs
from app.state import swarm

router = APIRouter(prefix="/api")

RUN_STATES: dict[str, TaskState] = {}


class ProjectRunBody(BaseModel):
    mode: str = "test"
    label: str = ""
    force_rescan: bool | None = None
    from_scratch: bool | None = None
    profile: str | None = None
    rules: dict | None = None


@router.post("/projects/runs", status_code=202)
async def project_run_start(body: ProjectRunBody | None = None):
    body = body or ProjectRunBody()
    if body.mode not in ("test", "full"):
        raise HTTPException(400, "mode must be test|full")
    if swarm.running:
        raise HTTPException(409, "swarm already running")
    scratch = resolve_from_scratch(
        body.from_scratch if body.from_scratch is not None else body.force_rescan,
        mode=body.mode)
    force_rescan = scratch
    settings = await get_settings()
    try:
        opened = await project_runs.open_run(
            db, mode=body.mode, label=body.label, settings=settings,
            force_rescan=force_rescan, rules_overrides=body.rules,
            profile=body.profile)
    except RuleError as e:
        raise HTTPException(400, str(e)) from e
    run_id = opened["run_id"]
    state = TaskState(max_logs=2000)
    state.start()
    RUN_STATES[run_id] = state
    try:
        await swarm.deploy(
            body.mode, False, settings, force_rescan,
            run_id=run_id, recorder=opened["recorder"])
    except ValueError as e:
        await project_runs.finalize_run(db, run_id, error=str(e))
        raise HTTPException(409, str(e)) from e
    return {
        "status": "started",
        "run_id": run_id,
        "mode": body.mode,
        "force_rescan": force_rescan,
        "from_scratch": scratch,
        "wrote_projects": False,
    }


@router.get("/projects/runs")
async def project_runs_list():
    docs = await db.project_runs.find({}).sort("created_at", -1).to_list(100)
    items = []
    for d in docs:
        row = dict(d)
        row["id"] = str(d.get("_id"))
        row.update(snapshot_list_fields(d.get("params") or {}))
        items.append(row)
    return {
        "count": len(items),
        "active_run_id": swarm.run_id if swarm.running else None,
        "wrote_projects": False,
        "items": items,
    }


@router.get("/projects/runs/{run_id}")
async def project_run_detail(run_id: str):
    doc = await db.project_runs.find_one({"_id": run_id})
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
        "kind": doc.get("mode") or "projects",
        "error": doc.get("error"),
        "summary": doc.get("counters"),
        "journal_lines": doc.get("journal_lines") or (doc.get("summary") or {}).get("journal_lines") or 0,
        "wrote_projects": False,
        "chosen": rules.get("chosen") or {},
        "params": {"rules": rules},
        **fields,
    }


@router.get("/projects/runs/{run_id}/status")
async def project_run_status(run_id: str):
    doc = await db.project_runs.find_one({"_id": run_id})
    if not doc:
        raise HTTPException(404, f"Run {run_id} unknown")
    from app.core.tokenfactory import public_cost
    persisted = (doc.get("summary") or {}).get("tokenfactory") if isinstance(doc.get("summary"), dict) else None
    out = {
        "run": doc,
        "wrote_projects": False,
        "live": swarm.status() if swarm.run_id == run_id else None,
        "tokenfactory": public_cost(run_id, persisted),
    }
    st = RUN_STATES.get(run_id)
    if st is not None and st.running:
        out["task"] = st.status()
    return out


@router.post("/projects/runs/{run_id}/cancel")
async def project_run_cancel(run_id: str):
    if swarm.run_id != run_id or not swarm.running:
        raise HTTPException(409, f"Run {run_id} is not running")
    await swarm.stop()
    return {"cancelling": True, "run_id": run_id, "wrote_projects": False}


@router.get("/projects/runs/{run_id}/projects")
async def project_run_projects(run_id: str, verdict: str | None = None, limit: int = 500):
    if not await db.project_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    q: dict = {"run_id": run_id}
    if verdict:
        q["verdict"] = verdict
    total = await db.project_run_projects.count_documents(q)
    docs = await db.project_run_projects.find(q).to_list(min(int(limit or 500), 2000))
    return {
        "total": total,
        "wrote_projects": False,
        "items": [project_runs.project_to_public(d) for d in docs],
    }


@router.get("/projects/runs/{run_id}/geojson")
async def project_run_geojson(run_id: str):
    """Run FeatureCollection — for the map display (run selector)."""
    if not await db.project_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    docs = await db.project_run_projects.find({"run_id": run_id}).to_list(20000)
    return project_runs.run_projects_to_geojson(run_id, docs)


@router.get("/projects/runs/{run_id}/events")
async def project_run_events(run_id: str, step: str | None = None,
                             skip: int = 0, limit: int = 500):
    q: dict = {"run_id": run_id}
    if step:
        q["step"] = step
    total = await db.project_run_events.count_documents(q)
    docs = await db.project_run_events.find(q).sort("seq", 1).skip(skip).to_list(min(limit, 2000))
    return {"total": total, "skip": skip, "count": len(docs), "items": docs,
            "wrote_projects": False}


@router.get("/projects/runs/{run_id}/journal")
async def project_run_journal(run_id: str, skip: int = 0, limit: int = 2000,
                              kind: str | None = None, tail: bool = False,
                              format: str = "json"):
    """Full run journal (swarm narrative + agents). Not the live map."""
    from app.services.run_journal import (
        JOURNAL_DOWNLOAD_MAX,
        JOURNAL_LIMIT_DEFAULT,
        JOURNAL_LIMIT_MAX,
        enrich_journal_payload,
        iter_journal_file,
        journal_to_jsonl,
        journal_to_text,
        load_journal,
        params_from_journal,
    )
    try:
        packed = await load_journal(
            db, run_id, skip=skip, limit=limit, kind=kind, tail=tail)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    doc = await db.project_runs.find_one({"_id": run_id}, {"params": 1})
    if not doc and packed["total"] == 0:
        raise HTTPException(404, f"Run {run_id} unknown")
    packed["limit"] = max(1, min(JOURNAL_LIMIT_MAX, int(limit or JOURNAL_LIMIT_DEFAULT)))
    params = (doc or {}).get("params") if isinstance((doc or {}).get("params"), dict) else None
    if not params:
        params = params_from_journal(run_id)
    enrich_journal_payload(packed, params)
    fmt = (format or "json").lower()
    if fmt in ("txt", "text", "jsonl"):
        items = list(iter_journal_file(run_id, kind=kind))
        if not items:
            dumped = await load_journal(
                db, run_id, skip=0, limit=JOURNAL_DOWNLOAD_MAX, kind=kind)
            items = dumped["items"]
        filename = f"run-{run_id}-journal.{'txt' if fmt != 'jsonl' else 'jsonl'}"
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        if fmt == "jsonl":
            return PlainTextResponse(
                journal_to_jsonl(items, params=params),
                media_type="application/x-ndjson; charset=utf-8",
                headers=headers)
        return PlainTextResponse(
            journal_to_text(run_id, items, params=params),
            media_type="text/plain; charset=utf-8",
            headers=headers)
    return packed


@router.get("/projects/runs/{run_id}/diff")
async def project_run_diff(run_id: str):
    if not await db.project_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    return await project_runs.diff_run_vs_v1(db, run_id)


@router.get("/projects/runs/{run_id}/report")
async def project_run_report(run_id: str, format: str = "json"):
    if not await db.project_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    rep = await project_runs.build_run_report(db, run_id)
    if format == "markdown":
        return PlainTextResponse(
            project_runs.report_to_markdown(rep),
            media_type="text/markdown; charset=utf-8")
    return rep


@router.post("/projects/runs/{run_id}/promote")
async def project_run_promote(run_id: str):
    if not await db.project_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    raise HTTPException(501, "promotion manuelle — phase D du CDC Projets")
