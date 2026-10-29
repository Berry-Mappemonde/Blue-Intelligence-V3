"""Dispatch Review Suggest by kind.

Batch (scope=all): eez, amp, project.
Single card: marina, harbormaster (OSM batch forbidden).
Never writes Gold or live collections.
"""
from __future__ import annotations

from app.services.poe_zone_fiche import PUBLISHED_RUN
from app.services.review_queue import list_queue

BATCH_KINDS = ("eez", "amp", "project")
FIELD_KINDS = ("marina", "capitainerie")
SUGGEST_KINDS = BATCH_KINDS + FIELD_KINDS


def _sid(value) -> str:
    return "" if value is None else str(value)


async def all_kind_ids(db, kind: str) -> list[str]:
    packed = await list_queue(
        db, kind, PUBLISHED_RUN, offset=0, limit=2000,
        q="", pre_gold=False, stable=False)
    out, seen = [], set()
    for it in packed.get("items") or []:
        eid = _sid(it.get("id"))
        if eid and eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out


async def suggest_one(db, kind: str, entity_id: str, *,
                      settings: dict | None = None,
                      log=None,
                      lessons: list[dict] | None = None) -> dict:
    kind = (kind or "").strip().lower()
    if kind == "eez":
        from app.services.review_doc_picker import suggest_eez_documents
        return await suggest_eez_documents(
            db, entity_id, settings=settings, log=log, lessons=lessons)
    if kind == "amp":
        from app.services.review_amp_picker import suggest_amp_documents
        return await suggest_amp_documents(
            db, entity_id, settings=settings, log=log, lessons=lessons)
    if kind == "project":
        from app.services.review_project_picker import suggest_project_documents
        return await suggest_project_documents(
            db, entity_id, settings=settings, log=log, lessons=lessons)
    if kind in FIELD_KINDS:
        from app.services.review_field_picker import suggest_field_documents
        return await suggest_field_documents(db, kind, entity_id, log=log)
    raise ValueError("suggest is eez|amp|project|marina|capitainerie")


async def run_suggest_batch(db, kind: str, state, *,
                            settings: dict | None = None) -> dict:
    kind = (kind or "").strip().lower()
    if kind not in BATCH_KINDS:
        raise ValueError("batch Proposer is eez|amp|project only")
    log = state.log if hasattr(state, "log") else (lambda m: None)
    ids = await all_kind_ids(db, kind)
    state.total = len(ids)
    state.progress = 0
    if settings is None:
        try:
            from app.db import get_settings
            settings = await get_settings()
        except Exception:
            settings = {}
    from app.services.review_lessons import load_lessons
    lessons = await load_lessons(db, kind=kind)
    ok = fail = 0
    last_id = None
    for eid in ids:
        if getattr(state, "cancel", False):
            log(f"lot Proposer {kind} : annulé")
            break
        try:
            out = await suggest_one(
                db, kind, eid, settings=settings, log=log, lessons=lessons)
            ok += 1
            last_id = eid
            state.results.append({
                "id": eid, "ok": True, "kind": kind,
                "engine": out.get("engine"),
            })
            log(f"lot {kind} {eid}: engine={out.get('engine')}")
        except Exception as e:
            fail += 1
            state.results.append({
                "id": eid, "ok": False, "kind": kind,
                "error": f"{type(e).__name__}",
            })
            log(f"lot {kind} {eid}: {type(e).__name__}: {str(e)[:120]}")
        state.progress = ok + fail
    summary = {
        "ok": ok, "fail": fail, "total": len(ids), "kind": kind,
        "last_id": last_id, "cancelled": bool(getattr(state, "cancel", False)),
    }
    state.summary = summary
    return summary
