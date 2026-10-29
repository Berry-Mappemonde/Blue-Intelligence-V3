"""Review tab API — card queue + comment + Gold switch.

Reads runs / v1. Writes `review_comments`, `review_gold`, `review_choices`,
`review_suggest`. Never writes `projects` / `poe_ports` / `eez_zones` /
`marinas` / `capitaineries` / `amp_sites`.
"""
import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.db import db
from app.services import review_gold, review_queue

from app.services.review_choices import gold_ready, save_choice
from app.services.review_gold import GoldNotReady
from app.services.review_report import REPORT_KINDS, build_report, report_markdown
from app.services.review_suggest import (
    BATCH_KINDS,
    FIELD_KINDS,
    SUGGEST_KINDS,
    run_suggest_batch,
    suggest_one,
)
from app.services.poe_zone_fiche import PUBLISHED_RUN
from app.core.tasks import TaskState

SUGGEST_STATE = TaskState()

router = APIRouter(prefix="/api")


def _kind_or_400(kind: str) -> str:
    if kind not in review_queue.KINDS:
        raise HTTPException(400, "kind must be project|eez|poe|marina|capitainerie|amp")
    return kind


@router.get("/review/runs")
async def review_runs(kind: str):
    kind = _kind_or_400(kind)
    return await review_queue.list_runs(db, kind)


@router.get("/review/queue")
async def review_queue_get(kind: str, run_id: str = PUBLISHED_RUN,
                           offset: int = 0, limit: int = 500, q: str = "",
                           pre_gold: bool = False, stable: bool = False):
    kind = _kind_or_400(kind)
    return await review_queue.list_queue(
        db, kind, run_id, offset=offset, limit=limit, q=q,
        pre_gold=pre_gold, stable=stable)


@router.get("/review/fiche")
async def review_fiche_get(kind: str, id: str, run_id: str = PUBLISHED_RUN,
                           content_run_id: str | None = None):
    kind = _kind_or_400(kind)
    if not id:
        raise HTTPException(400, "id required")
    out = await review_queue.get_fiche(
        db, kind, run_id, id, content_run_id=content_run_id)
    if out is None:
        raise HTTPException(404, "fiche not found")
    return out


@router.get("/review/report")
async def review_report_get(kind: str = "all", format: str = "json"):
    """Rapport de review : commentaires + choix + Gold, lecture seule."""
    if kind != "all" and kind not in REPORT_KINDS:
        raise HTTPException(
            400, "kind must be all|eez|project|marina|capitainerie|amp")
    report = await build_report(db, kind)
    if format == "md":
        return PlainTextResponse(
            report_markdown(report),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition":
                     f"attachment; filename=review_report_{kind}.md"},
        )
    return report


class CommentBody(BaseModel):
    kind: str
    id: str
    run_id: str = PUBLISHED_RUN
    comment: str = Field(default="")


class GoldBody(BaseModel):
    kind: str
    id: str
    run_id: str = PUBLISHED_RUN


class ChoiceBody(BaseModel):
    kind: str
    id: str
    target: str
    action: str
    url: str | None = None
    port_id: str | None = None
    site_id: str | None = None
    field: str | None = None
    lat: float | None = None
    lon: float | None = None


@router.put("/review/comment")
async def review_comment_put(body: CommentBody):
    kind = _kind_or_400(body.kind)
    if not body.id:
        raise HTTPException(400, "id required")
    out = await review_queue.save_comment(db, kind, body.run_id, body.id, body.comment)
    payload = await review_queue.get_fiche(db, kind, body.run_id, body.id)
    if payload:
        out["gold_ready"] = bool(payload.get("gold_ready"))
        out["choices"] = payload.get("choices")
    return out


class ExtractBody(BaseModel):
    kind: str
    id: str


class SuggestBody(BaseModel):
    kind: str
    id: str | None = None
    scope: str = "one"


async def _extract_gold_safe(entity_id: str) -> None:
    try:
        from app.services.review_extract import extract_gold_ports
        await extract_gold_ports(db, entity_id)
    except Exception:
        pass


@router.put("/review/choice")
async def review_choice_put(body: ChoiceBody):
    kind = _kind_or_400(body.kind)
    if kind not in ("eez", "project", "marina", "capitainerie", "amp"):
        raise HTTPException(400, "choice is not for this kind")
    if not body.id:
        raise HTTPException(400, "id required")
    try:
        out = await save_choice(
            db, kind, body.id, body.target, body.action,
            url=body.url, port_id=body.port_id, site_id=body.site_id,
            field=body.field, lat=body.lat, lon=body.lon)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    payload = await review_queue.get_fiche(db, kind, PUBLISHED_RUN, body.id)
    fiche = (payload or {}).get("fiche") if payload else None
    out["gold_ready"] = gold_ready(
        fiche, out.get("choices"), kind=kind,
        comment=(payload or {}).get("comment") or "")
    out["gold_on"] = bool(payload and payload.get("gold_on")) if payload else False
    return out


@router.put("/review/gold")
async def review_gold_put(body: GoldBody):
    if body.kind not in review_gold.GOLD_KINDS:
        raise HTTPException(400, "kind must be project|eez|marina|capitainerie|amp")
    if not body.id:
        raise HTTPException(400, "id required")
    payload = await review_queue.get_fiche(db, body.kind, body.run_id, body.id)
    if payload is None:
        raise HTTPException(404, "fiche not found")
    fiche = payload.get("fiche")
    comment = payload.get("comment") or ""
    choices = payload.get("choices")
    try:
        out = await review_gold.toggle_gold(
            db, body.kind, body.id, run_id=body.run_id,
            fiche=fiche, comment=comment, choices=choices)
    except GoldNotReady as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.kind == "eez" and out.get("gold_on"):
        asyncio.create_task(_extract_gold_safe(body.id))
    return out


@router.post("/review/extract")
async def review_extract_post(body: ExtractBody):
    if body.kind != "eez":
        raise HTTPException(400, "extract is only for eez")
    if not body.id:
        raise HTTPException(400, "id required")
    from app.services.review_extract import extract_gold_ports
    try:
        return await extract_gold_ports(db, body.id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/review/suggest/status")
async def review_suggest_status():
    return SUGGEST_STATE.status()


def _suggest_value_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    code = 404 if "not found" in msg.lower() else 400
    return HTTPException(code, msg)


@router.post("/review/suggest")
async def review_suggest_post(body: SuggestBody):
    kind = (body.kind or "").strip().lower()
    if kind not in SUGGEST_KINDS:
        raise HTTPException(
            400, "suggest is eez|amp|project|marina|capitainerie")
    scope = (body.scope or "one").strip().lower()
    if kind in FIELD_KINDS:
        if scope == "all" or not body.id:
            raise HTTPException(
                400, "marina/capitainerie: une fiche à la fois (pas de lot OSM)")
        try:
            return await suggest_one(db, kind, body.id)
        except ValueError as e:
            raise _suggest_value_error(e) from e
    if scope == "all" or not body.id:
        if kind not in BATCH_KINDS:
            raise HTTPException(400, "batch Proposer is eez|amp|project only")
        if SUGGEST_STATE.running:
            raise HTTPException(409, "suggest batch already running")
        SUGGEST_STATE.start()

        async def _runner():
            try:
                await run_suggest_batch(db, kind, SUGGEST_STATE)
            except Exception as e:
                SUGGEST_STATE.error = f"{type(e).__name__}: {e}"
                SUGGEST_STATE.log(f"FATAL: {SUGGEST_STATE.error}")
            finally:
                SUGGEST_STATE.finish()

        asyncio.create_task(_runner())
        return {"status": "started", "scope": "all", "kind": kind}
    try:
        return await suggest_one(db, kind, body.id)
    except ValueError as e:
        raise _suggest_value_error(e) from e
