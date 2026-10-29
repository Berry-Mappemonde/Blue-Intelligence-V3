"""app.routers.amp — 4th mode: Marine Protected Areas (ProtectedSeas)."""
import asyncio
import json
import os
import time
from collections import OrderedDict

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.from_scratch import resolve_from_scratch
from app.core.run_rules import catalog_default
from app.core.tasks import TaskState, LOGS_TAIL_N
from app.db import db
from app.services import amp as amp_svc
from app.services import amp_visit

router = APIRouter(prefix="/api")
VISIT_DISCOVER_STATE = TaskState(max_logs=400)


class VisitUrlBody(BaseModel):
    url: str | None = None


class RefreshBody(BaseModel):
    bbox: str
    force: bool = True


class DiscoverBody(BaseModel):
    limit: int = 200
    skip_search: bool = False
    profile: str | None = None
    rules: dict | None = None
    bbox: str | None = None
    label: str | None = None
    resume: bool = False
    from_scratch: bool | None = None
    harvest_polygons: bool | None = None
    scope: str | None = None


def _parse_bbox(raw: str, **caps):
    try:
        return amp_svc.parse_bbox(raw, **caps)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# Vue large : la FeatureCollection est construite et encodée hors de la
# boucle, puis servie déjà rendue. Le cache mémoire évite de recommencer
# pour une bbox voisine. Les vues ≤ amp.bbox_max_deg ne passent pas ici.
# Une heure : la première lecture Mongo reste la plus chère, les suivantes
# resservent le JSON déjà allégé. Quatre entrées au plus, donc la durée
# ne grossit pas le pic mémoire.
_WIDE_CACHE_TTL_S = 3600.0
_WIDE_CACHE_MAX_ENTRIES = 4
_WIDE_CACHE_MAX_BYTES = 16_000_000
_WIDE_WARMUP_ENV = "AMP_WIDE_VIEW_WARMUP"
_wide_cache: OrderedDict[tuple, tuple[float, bytes]] = OrderedDict()
_wide_key_locks: dict[tuple, asyncio.Lock] = {}
_wide_key_locks_loop: asyncio.AbstractEventLoop | None = None
_wide_warmup_task: asyncio.Task | None = None
_WIDE_KEY_LOCKS_MAX = 128


def clear_wide_amp_response_cache() -> None:
    _wide_cache.clear()


def _lock_for_wide_key(key: tuple) -> asyncio.Lock:
    """Un verrou par bbox quantifiée : deux vues distinctes avancent ensemble."""
    global _wide_key_locks, _wide_key_locks_loop
    loop = asyncio.get_running_loop()
    if _wide_key_locks_loop is not loop:
        _wide_key_locks = {}
        _wide_key_locks_loop = loop
    lock = _wide_key_locks.get(key)
    if lock is not None:
        return lock
    if len(_wide_key_locks) >= _WIDE_KEY_LOCKS_MAX:
        _wide_key_locks = {k: v for k, v in _wide_key_locks.items() if v.locked()}
    lock = asyncio.Lock()
    _wide_key_locks[key] = lock
    return lock


def wide_view_warmup_enabled() -> bool:
    return os.environ.get(_WIDE_WARMUP_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _wide_cache_key(box, span: float, *, visible: bool, review: bool) -> tuple:
    flags = (bool(visible), bool(review))
    if span >= 60.0:
        return ("world", flags)
    rounded = tuple(round(float(v), 1) for v in box)
    return ("bbox", rounded, flags)


def _wide_cache_get(key: tuple) -> bytes | None:
    item = _wide_cache.get(key)
    if item is None:
        return None
    stored_at, payload = item
    if time.monotonic() - stored_at > _WIDE_CACHE_TTL_S:
        _wide_cache.pop(key, None)
        return None
    _wide_cache.move_to_end(key)
    return payload


def _wide_cache_put(key: tuple, payload: bytes) -> None:
    if len(payload) > _WIDE_CACHE_MAX_BYTES:
        return
    _wide_cache[key] = (time.monotonic(), payload)
    _wide_cache.move_to_end(key)
    while len(_wide_cache) > _WIDE_CACHE_MAX_ENTRIES:
        _wide_cache.popitem(last=False)


def _json_response(payload: bytes) -> Response:
    return Response(content=payload, media_type="application/json")


def _render_wide_amp_json(docs: list, extra: dict) -> bytes:
    """Sync. Détache la géométrie source pour relâcher les polygones bruts."""
    rendered = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        geom = doc.pop("geometry", None)
        item = dict(doc)
        item["geometry"] = amp_svc.lighten_geometry_for_wide_view(geom)
        rendered.append(item)
    fc = amp_svc.to_feature_collection(rendered, extra=extra)
    encoded = jsonable_encoder(fc)
    return json.dumps(
        encoded,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


async def _wide_amp_response(box, span: float, *, visible: bool, review: bool) -> Response:
    key = _wide_cache_key(box, span, visible=visible, review=review)
    cached = _wide_cache_get(key)
    if cached is not None:
        return _json_response(cached)
    async with _lock_for_wide_key(key):
        cached = _wide_cache_get(key)
        if cached is not None:
            return _json_response(cached)
        # Vue éloignée : cache local seulement, pas d'appel ProtectedSeas.
        # Les aires arrivent par lots (projection + batch_size).
        docs = await amp_svc.collect_light_areas_for_wide_view(
            db, box, span, limit=8000,
        )
        if visible or review:
            from app.services.review_gold import filter_visible
            docs = await filter_visible(
                db, "amp", docs,
                lambda d: d.get("site_id") or d.get("_id"))
        extra = {
            "hint": "zoom" if not docs else None,
            "source": "cache",
            "truncated": len(docs) >= 8000,
            "fetched": 0,
        }
        payload = await asyncio.to_thread(_render_wide_amp_json, docs, extra)
        _wide_cache_put(key, payload)
        return _json_response(payload)


async def _warm_wide_amp_cache() -> None:
    """Préchauffe la vue mondiale. Lecture seule : même chemin que GET /api/amp."""
    try:
        await _wide_amp_response(
            (-180.0, -90.0, 180.0, 90.0), 360.0,
            visible=False, review=False,
        )
    except Exception as exc:
        print(f"[amp] warm-up vue large ignoré : {exc}")


def start_wide_amp_warmup():
    """Au démarrage, seulement si AMP_WIDE_VIEW_WARMUP est activé (sinon rien)."""
    global _wide_warmup_task
    if not wide_view_warmup_enabled():
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = _wide_warmup_task
    if task is not None and not task.done():
        return task
    _wide_warmup_task = asyncio.create_task(_warm_wide_amp_cache())
    return _wide_warmup_task


@router.get("/amp")
async def list_amp(bbox: str = "", force: bool = False, visible: bool = False,
                   review: bool = False):
    """MPA polygons in the bbox (minx,miny,maxx,maxy, WGS84)."""
    if not bbox:
        raise HTTPException(400, "bbox required (minx,miny,maxx,maxy)")
    # Worldwide caps: the zoomed-out view is served from the local cache;
    # only the ProtectedSeas refresh keeps the strict limit.
    box = _parse_bbox(bbox, max_w=360.0, max_h=180.0)
    max_span = float(catalog_default("amp.bbox_max_deg", 8))
    span = amp_svc.bbox_span_deg(box)
    if span > max_span:
        return await _wide_amp_response(box, span, visible=visible, review=review)
    docs, meta = await amp_svc.sites_in_bbox(db, box, force=force)
    if visible or review:
        from app.services.review_gold import filter_visible
        docs = await filter_visible(
            db, "amp", docs,
            lambda d: d.get("site_id") or d.get("_id"))
    return amp_svc.to_feature_collection(docs, extra={
        "hint": None,
        "source": meta.get("source"),
        "truncated": bool(meta.get("truncated")),
        "fetched": meta.get("fetched") or 0,
        "error": meta.get("error"),
    })


@router.get("/amp/nogo")
async def amp_nogo():
    """AMP interdites à la plaisance (entry=1 ou speed=1), polygones allégés."""
    docs = await db.amp_sites.find(
        {"$or": [{"entry": 1}, {"speed": 1}]},
    ).to_list(500)
    kept = []
    for doc in docs:
        if not amp_svc.is_forbidden_pleasure(doc):
            continue
        geom = amp_svc.repair_geometry_for_mongo(doc.get("geometry"), max_bytes=80_000)
        if not amp_svc.is_area_geom(geom):
            continue
        slim = dict(doc)
        slim["geometry"] = geom
        kept.append(slim)
    return amp_svc.to_feature_collection(kept, extra={
        "name": "amp_nogo",
        "note": "entry=1 or speed=1 — no-transit polygons for routing",
        "count": len(kept),
    })


@router.get("/amp/stats")
async def amp_stats():
    return await amp_svc.amp_stats(db)


@router.get("/amp/sites/{site_id}")
async def amp_site(site_id: str):
    doc = await db.amp_sites.find_one({"_id": site_id})
    if not doc:
        doc = await db.amp_sites.find_one({"site_id": site_id})
    if not doc:
        raise HTTPException(404, "AMP not found")
    return {
        "kind": "amp",
        "id": doc.get("site_id") or doc.get("_id"),
        **amp_svc.public_properties(doc),
        "wrote_amp_sites": False,
    }


@router.post("/amp/refresh")
async def amp_refresh(body: RefreshBody):
    box = _parse_bbox(body.bbox)
    docs, meta = await amp_svc.sites_in_bbox(db, box, force=body.force)
    return {
        "status": "ok",
        "count": len(docs),
        **meta,
    }


@router.post("/amp/sites/{site_id}/visit-url")
async def amp_set_visit_url(site_id: str, body: VisitUrlBody):
    doc = await amp_svc.set_visit_url(db, site_id, body.url)
    if not doc:
        raise HTTPException(404, "AMP not found")
    if body.url and amp_svc.urls_equivalent(body.url, doc.get("manager_url")):
        raise HTTPException(
            409,
            "visit_url must differ from ProtectedSeas manager_url",
        )
    return {
        "id": doc.get("site_id") or doc.get("_id"),
        "manager_url": doc.get("manager_url"),
        "visit_url": doc.get("visit_url"),
        "visit_url_status": doc.get("visit_url_status"),
        "visit_url_source": doc.get("visit_url_source"),
    }


@router.post("/amp/resolve-visit-urls")
async def amp_resolve_visit_urls(limit: int = 500):
    """Synchronous heuristic: other_helpful_links only."""
    return await amp_svc.resolve_visit_urls(db, limit=min(max(limit, 1), 2000))


@router.post("/amp/discover-visit-urls")
async def amp_discover_visit_start(body: DiscoverBody | None = None):
    """Background job: ProtectedSeas extras → Fetch → Search → NIM judge (json chain)."""
    if VISIT_DISCOVER_STATE.running:
        raise HTTPException(409, "A visit-URL discover is already running")
    body = body or DiscoverBody()
    scope = (body.scope or "full").lower()
    scratch = resolve_from_scratch(body.from_scratch, scope=scope)
    harvest = (
        bool(body.harvest_polygons) if body.harvest_polygons is not None
        else scratch)
    if scratch and (body.limit is None or int(body.limit or 0) <= 0):
        limit = 50_000
    elif scratch:
        limit = min(max(int(body.limit), 1), 50_000)
    else:
        limit = min(max(int(body.limit or 200), 1), 2000)
    bbox = None
    if body.bbox:
        bbox = _parse_bbox(body.bbox, max_w=360.0, max_h=180.0)
    from app.core.run_rules import RuleError, bind_rules, reset_rules, snapshot_for_run
    from app.db import get_settings
    from app.services import isolated_runs
    settings = await get_settings()
    try:
        rules = snapshot_for_run(
            mode="amp", settings=settings,
            overrides=body.rules, profile=body.profile)
    except RuleError as e:
        raise HTTPException(400, str(e)) from e
    opened = await isolated_runs.open_run(
        db, "amp", kind="discover_visit",
        label=body.label or "amp-visit", settings=settings,
        extra_params={"limit": limit, "skip_search": bool(body.skip_search),
                      "profile": rules.get("profile"),
                      "bbox": body.bbox, "from_scratch": scratch,
                      "harvest_polygons": harvest},
        rules_overrides=body.rules, profile=body.profile,
        resume=body.resume and not scratch,
    )
    run_id = opened["run_id"]
    recorder = opened["recorder"]
    VISIT_DISCOVER_STATE.run_id = run_id
    isolated_runs.reset_run(opened["token"])

    async def _runner():
        rules_token = bind_rules(rules)
        try:
            await amp_visit.discover_visit_urls(
                db,
                state=VISIT_DISCOVER_STATE,
                limit=limit,
                skip_search=bool(body.skip_search),
                run_id=run_id,
                recorder=recorder,
                bbox=bbox,
                from_scratch=scratch,
                harvest_polygons=harvest,
            )
            copied = 0
            if scratch:
                copied = await amp_visit.copy_visit_to_live(db, run_id)
            extra = dict(VISIT_DISCOVER_STATE.summary or {})
            extra["copied_to_live"] = copied
            await isolated_runs.finalize_run(
                db, "amp", run_id, extra=extra,
                cancelled=bool(VISIT_DISCOVER_STATE.cancel))
        except Exception as exc:
            VISIT_DISCOVER_STATE.error = f"{type(exc).__name__}: {exc}"
            await isolated_runs.finalize_run(
                db, "amp", run_id, error=VISIT_DISCOVER_STATE.error)
        finally:
            reset_rules(rules_token)

    asyncio.create_task(_runner())
    return {
        "started": True, "limit": limit, "skip_search": bool(body.skip_search),
        "from_scratch": scratch, "harvest_polygons": harvest,
        "run_id": run_id, "wrote_amp_sites": False,
        "profile": rules.get("profile"), "rules_hash": rules.get("hash"),
        "bbox": body.bbox, "label": body.label,
    }


@router.post("/amp/discover-visit-urls/cancel")
async def amp_discover_visit_cancel():
    if not VISIT_DISCOVER_STATE.running:
        raise HTTPException(409, "No visit-URL discover is running")
    VISIT_DISCOVER_STATE.cancel = True
    VISIT_DISCOVER_STATE.log("Stop demandé")
    return {"cancelling": True}


@router.get("/amp/discover-visit-urls/status")
async def amp_discover_visit_status():
    s = VISIT_DISCOVER_STATE
    return {
        "running": s.running,
        "cancelling": s.cancel,
        "started_at": s.started_at,
        "finished_at": s.finished_at,
        "progress": s.progress,
        "total": s.total,
        "summary": s.summary,
        "error": s.error,
        "logs_tail": s.logs[-LOGS_TAIL_N:],
        "run_id": s.run_id,
        "wrote_amp_sites": False,
    }


@router.get("/amp/runs")
async def amp_runs_list():
    from app.services import isolated_runs
    items = await isolated_runs.list_meta_runs(db, "amp")
    return {
        "count": len(items),
        "wrote_amp_sites": False,
        "items": items,
    }


@router.get("/amp/runs/{run_id}")
async def amp_run_detail(run_id: str):
    from app.services import isolated_runs
    doc = await isolated_runs.get_meta_run(db, "amp", run_id)
    if not doc:
        raise HTTPException(404, f"Run {run_id} unknown")
    return {**doc, "wrote_amp_sites": False}


@router.get("/amp/runs/{run_id}/events")
async def amp_run_events(run_id: str, step: str | None = None,
                         skip: int = 0, limit: int = 500):
    from app.services import isolated_runs
    if not await db.amp_runs.find_one({"_id": run_id}):
        raise HTTPException(404, f"Run {run_id} unknown")
    return await isolated_runs.list_run_events(
        db, "amp", run_id, step=step, skip=skip, limit=limit)


@router.get("/amp/runs/{run_id}/geojson")
async def amp_run_geojson(run_id: str):
    """Run FeatureCollection (polygons) — map run selector."""
    meta = await db.amp_runs.find_one({"_id": run_id})
    if not meta:
        raise HTTPException(404, f"Run {run_id} unknown")
    docs = await db.amp_run_sites.find({"run_id": run_id}).to_list(20000)
    live_fallback = False
    if not docs and meta.get("wrote_amp_sites"):
        docs = await db.amp_sites.find({}).to_list(20000)
        live_fallback = True
    return amp_svc.to_feature_collection(docs, extra={
        "run_id": run_id,
        "live_fallback": live_fallback,
        "hint": None,
        "wrote_amp_sites": False,
    })


@router.get("/export/amp.geojson")
async def export_amp_geojson():
    from app.core.export_meta import export_response
    docs = await db.amp_sites.find({}, amp_svc.SLIM_PROJECTION).to_list(20000)
    fc = amp_svc.to_feature_collection(docs, geometry=False, extra={
        "name": "amp_sites",
        "note": "centroids + manager_url / visit_url — not official boundaries",
    })
    return export_response(
        fc, "amp", "amp.geojson",
        license_note="ProtectedSeas Navigator — centroïdes et métadonnées, pas les limites officielles",
    )
