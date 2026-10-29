"""
app.routers.formalities — Formalities mode FastAPI endpoints [EEZ -> Ports of Entry].
"""
import asyncio
import os
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.tasks import TaskState
from app.db import db as _db
from app.services import poe_pipeline as poe
from app.services import osm_validate
from app.services.poe_zone_label import attach_zone_labels, zone_sort_key

router = APIRouter(prefix="/api")


REF_STATE = TaskState()
GEN_LOCKS: set[int] = set()

# v1 map: generate / generate-batch used to overwrite poe_ports. 410, not 404.
GENERATE_GONE = (
    "Retiré : Générer / generate-batch n'écrase plus poe_ports. "
    "Utiliser un run isolé (POST /api/poe/runs) ou l'enrichissement des graines."
)


# ---------------------------------------------------------------------------
# Automatic refresh (no manual action)
# - Generated zones re-checked after REFRESH_AFTER_DAYS: sources are
#   re-downloaded and compared by MD5 hash — LLM+geocode re-extraction
#   happens ONLY if the source content changed (handled in poe.generate_zone_poe).
# - Error zones retried (force) after ERROR_RETRY_DAYS.
# ---------------------------------------------------------------------------
REFRESH_AFTER_DAYS = 30
ERROR_RETRY_DAYS = 7
CYCLE_EVERY_H = float(os.getenv("POE_AUTO_REFRESH_EVERY_H", "12"))
MAX_PER_CYCLE = int(os.getenv("POE_AUTO_REFRESH_MAX", "10"))

# 20 sept. 2026 : un cycle a bloqué la boucle asyncio du serveur pendant des
# heures (generate_zone_poe fait du travail synchrone — extraction, PDF,
# TF-IDF — que wait_for ne peut pas interrompre) : le site entier ne répondait
# plus. Désactivé par défaut tant que le pipeline n'est pas rendu non bloquant
# (to_thread / worker séparé). Réactiver : POE_AUTO_REFRESH=1 dans backend/.env.
def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "off", "no")


AUTO_STATE = {
    "enabled": _env_flag("POE_AUTO_REFRESH", False),
    "cycle_running": False,
    "last_cycle_at": None,
    "next_check_at": None,
    "last_summary": None,   # {"checked", "unchanged_md5", "updated", "errors_retried", "failed"}
    "logs": [],
}
_auto_task = None


def _auto_log(msg: str):
    AUTO_STATE["logs"].append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
    if len(AUTO_STATE["logs"]) > 300:
        AUTO_STATE["logs"] = AUTO_STATE["logs"][-300:]


def _ts_of(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


async def _auto_refresh_cycle():
    now = time.time()
    zones = await _db.eez_zones.find({}, {"geometry": 0}).to_list(500)
    stale, errored = [], []
    for z in zones:
        ref = _ts_of(z.get("checked_at")) or _ts_of(z.get("generated_at"))
        if ref is None:
            continue
        st = z.get("status")
        if st in ("ia", "ia_sans_source") and now - ref > REFRESH_AFTER_DAYS * 86400:
            stale.append(z)
        elif st == "erreur" and now - ref > ERROR_RETRY_DAYS * 86400:
            errored.append(z)
    stale.sort(key=lambda z: z.get("generated_at") or "")
    errored.sort(key=lambda z: z.get("generated_at") or "")
    todo = [(z, False) for z in stale] + [(z, True) for z in errored]
    todo = todo[:MAX_PER_CYCLE]
    if not todo:
        _auto_log("cycle: aucune zone à re-vérifier")
        AUTO_STATE["last_summary"] = {"checked": 0, "unchanged_md5": 0, "updated": 0, "errors_retried": 0, "failed": 0}
        return

    _auto_log(f"cycle: {len(stale)} zone(s) périmées (> {REFRESH_AFTER_DAYS} j) + "
              f"{len(errored)} erreur(s) (> {ERROR_RETRY_DAYS} j) — {len(todo)} traitées (cap {MAX_PER_CYCLE})")
    summary = {"checked": 0, "unchanged_md5": 0, "updated": 0, "errors_retried": 0, "failed": 0}
    for z, force in todo:
        mrgid = z["mrgid"]
        if mrgid in GEN_LOCKS or REF_STATE.running:
            _auto_log(f"[{z.get('name')}] SKIP (verrou ou référentiel en cours)")
            continue
        GEN_LOCKS.add(mrgid)
        prev_gen = z.get("generated_at")
        try:
            doc = await asyncio.wait_for(
                poe.generate_zone_poe(_db, mrgid,
                                      logger=lambda m, n=z.get("name"): _auto_log(f"[{n}] {m}"),
                                      force=force, refresh=not force),
                timeout=360,
            )
            summary["checked"] += 1
            if force:
                summary["errors_retried"] += 1
            if doc and doc.get("generated_at") == prev_gen and doc.get("status") == z.get("status"):
                summary["unchanged_md5"] += 1
            else:
                summary["updated"] += 1
        except Exception as e:
            summary["failed"] += 1
            _auto_log(f"[{z.get('name')}] FAILED: {type(e).__name__}: {e}")
        finally:
            GEN_LOCKS.discard(mrgid)
        await asyncio.sleep(2)
    AUTO_STATE["last_summary"] = summary
    _auto_log(f"cycle terminé: {summary}")


async def _auto_refresh_loop():
    await asyncio.sleep(90)  # let the app start
    # Pas de cycle au démarrage : le premier vient après CYCLE_EVERY_H, sinon
    # chaque redémarrage relance aussitôt le travail qui a bloqué le serveur.
    if AUTO_STATE["last_cycle_at"] is None:
        AUTO_STATE["last_cycle_at"] = time.time()
    while True:
        due = (AUTO_STATE["last_cycle_at"] or 0) + CYCLE_EVERY_H * 3600 <= time.time()
        busy = REF_STATE.running
        if AUTO_STATE["enabled"] and due and not busy:
            AUTO_STATE["cycle_running"] = True
            try:
                await _auto_refresh_cycle()
            except Exception as e:
                _auto_log(f"cycle FATAL: {type(e).__name__}: {e}")
            finally:
                AUTO_STATE["cycle_running"] = False
                AUTO_STATE["last_cycle_at"] = time.time()
        AUTO_STATE["next_check_at"] = time.time() + 1800
        await asyncio.sleep(1800)


def start_auto_refresh():
    global _auto_task
    if _auto_task is None or _auto_task.done():
        _auto_task = asyncio.create_task(_auto_refresh_loop())


@router.get("/poe/auto-refresh/status")
async def poe_auto_refresh_status():
    return {
        "enabled": AUTO_STATE["enabled"],
        "cycle_running": AUTO_STATE["cycle_running"],
        "last_cycle_at": AUTO_STATE["last_cycle_at"],
        "next_check_at": AUTO_STATE["next_check_at"],
        "last_summary": AUTO_STATE["last_summary"],
        "config": {
            "refresh_after_days": REFRESH_AFTER_DAYS,
            "error_retry_days": ERROR_RETRY_DAYS,
            "cycle_every_hours": CYCLE_EVERY_H,
            "max_per_cycle": MAX_PER_CYCLE,
        },
        "logs_tail": AUTO_STATE["logs"][-30:],
    }


# ---------------------------------------------------------------------------
# EEZ referential
# ---------------------------------------------------------------------------
@router.post("/poe/referential/build", status_code=202)
async def poe_referential_build():
    if REF_STATE.running:
        raise HTTPException(409, "EEZ referential build already running")
    REF_STATE.reset()
    REF_STATE.running = True
    REF_STATE.started_at = time.time()

    async def _runner():
        try:
            REF_STATE.summary = await poe.build_referential(_db, REF_STATE)
        except Exception as e:
            REF_STATE.error = f"{type(e).__name__}: {e}"
            REF_STATE.log(f"FATAL: {REF_STATE.error}")
        finally:
            REF_STATE.finished_at = time.time()
            REF_STATE.running = False

    asyncio.create_task(_runner())
    return {"status": "started"}


@router.get("/poe/referential/status")
async def poe_referential_status():
    return REF_STATE.status()


@router.get("/poe/zones")
async def poe_zones(visible: bool = False):
    docs = await _db.eez_zones.find({}, {"geometry": 0}).to_list(500)
    if visible:
        from app.services.review_gold import filter_visible
        docs = await filter_visible(
            _db, "eez", docs, lambda z: str(int(z.get("mrgid") or 0)))
    attach_zone_labels(docs)
    items = sorted((poe.zone_to_item(d) for d in docs), key=zone_sort_key)
    by_status: dict[str, int] = {}
    for z in items:
        by_status[z["status"]] = by_status.get(z["status"], 0) + 1
    if visible:
        from app.services.review_gold import published_snapshots
        snaps = await published_snapshots(_db)
        total_ports = 0
        for z in items:
            snap = snaps.get(int(z["mrgid"]))
            if not snap:
                continue
            n = len(snap.get("ports") or [])
            z["poe_count"] = n
            z["gold_published"] = True
            total_ports += n
    else:
        total_ports = await _db.poe_ports.count_documents({})
    return {
        "count": len(items),
        "summary": {"by_status": by_status, "total_ports": total_ports},
        "attribution": poe.EEZ_ATTRIBUTION,
        "items": items,
    }


_EEZ_FC_CACHE: dict | None = None


def _eez_feature_collection() -> dict:
    global _EEZ_FC_CACHE
    if _EEZ_FC_CACHE is None:
        import json
        _EEZ_FC_CACHE = json.loads(poe.MAP_FILE.read_text(encoding="utf-8"))
    return _EEZ_FC_CACHE


@router.get("/poe/zones/geojson")
async def poe_zones_geojson(visible: bool = False):
    if not poe.MAP_FILE.exists():
        raise HTTPException(404, "EEZ map not built yet — POST /api/poe/referential/build first")
    if not visible:
        return FileResponse(
            poe.MAP_FILE, media_type="application/geo+json",
            headers={"Cache-Control": "public, max-age=3600", "X-EEZ-Source": "Marine Regions (VLIZ) v12 CC-BY 4.0"},
        )
    from app.services.review_gold import visible_eez_mrgids
    raw = _eez_feature_collection()
    allowed = await visible_eez_mrgids(_db)
    feats = []
    for feat in raw.get("features") or []:
        try:
            mid = int((feat.get("properties") or {}).get("mrgid") or 0)
        except (TypeError, ValueError):
            continue
        if mid in allowed:
            feats.append(feat)
    return {
        "type": "FeatureCollection",
        "features": feats,
        "attribution": poe.EEZ_ATTRIBUTION,
    }


@router.get("/poe/zones/{mrgid}")
async def poe_zone_fiche(mrgid: int, visible: bool = False, review: bool = False):
    """Map card of an EEZ: unique run by default; Gold if Show review."""
    from app.services.poe_zone_fiche import build_map_zone_fiche, build_zone_fiche

    if visible or review:
        fiche = await build_map_zone_fiche(_db, mrgid)
        if fiche is None:
            raise HTTPException(404, f"ZEE {mrgid} inconnue")
        return fiche
    fiche = await build_zone_fiche(_db, mrgid, union=True)
    if fiche is None:
        raise HTTPException(404, f"ZEE {mrgid} inconnue")
    return fiche


# ---------------------------------------------------------------------------
# Map PoE generation — removed (do not upsert poe_ports anymore)
# ---------------------------------------------------------------------------
@router.post("/poe/zones/{mrgid}/generate")
async def poe_generate(mrgid: int, force: bool = False):
    raise HTTPException(410, GENERATE_GONE)


@router.get("/poe/zones/{mrgid}/generate/status")
async def poe_generate_status(mrgid: int):
    raise HTTPException(410, GENERATE_GONE)


@router.post("/poe/generate-batch")
async def poe_generate_batch():
    raise HTTPException(410, GENERATE_GONE)


@router.get("/poe/generate-batch/status")
async def poe_generate_batch_status():
    raise HTTPException(410, GENERATE_GONE)


@router.post("/poe/generate-batch/cancel")
async def poe_generate_batch_cancel():
    raise HTTPException(410, GENERATE_GONE)


@router.get("/poe/claude-usage")
async def poe_claude_usage():
    from app.core.claude import usage_public
    from app.db import get_settings
    return usage_public(await get_settings())


# ---------------------------------------------------------------------------
# Bottom-Up validation of existing PoEs via OSM Overpass (background task)
# — enriches osm_confidence / osm_tags WITHOUT touching name, coordinates or text.
# ---------------------------------------------------------------------------
OSM_STATE = TaskState()


class OsmValidateBody(BaseModel):
    only_unchecked: bool = True
    limit: int = 0          # 0 = all
    radius_m: int = 3000


def _start_osm_task(only_unchecked: bool, limit: int, radius_m: int, resumed: bool = False):
    """Start Overpass validation and persist the job state in db.jobs
    (automatic resume after server kill/reload)."""
    OSM_STATE.reset()
    OSM_STATE.running = True
    OSM_STATE.started_at = time.time()
    if resumed:
        OSM_STATE.log("Reprise automatique — job interrompu par un redémarrage du serveur")

    async def _runner():
        try:
            await _db.jobs.update_one(
                {"_id": "osm_validation"},
                {"$set": {"desired": True, "resumed": resumed, "started_at": poe.now_iso(),
                          "params": {"only_unchecked": only_unchecked, "limit": limit,
                                     "radius_m": radius_m}}},
                upsert=True)
            OSM_STATE.summary = await osm_validate.validate_ports(
                _db, OSM_STATE, only_unchecked=only_unchecked, limit=limit, radius_m=radius_m)
            await _db.jobs.update_one({"_id": "osm_validation"}, {"$set": {
                "desired": False, "finished_at": poe.now_iso(),
                "cancelled": OSM_STATE.cancel, "summary": OSM_STATE.summary}})
        except Exception as e:
            # desired stays True in the DB → new attempt on next startup
            OSM_STATE.error = f"{type(e).__name__}: {e}"
            OSM_STATE.log(f"FATAL: {OSM_STATE.error} — reprise auto au prochain démarrage")
        finally:
            OSM_STATE.finished_at = time.time()
            OSM_STATE.running = False

    asyncio.create_task(_runner())


def schedule_job_resume(delay_s: float = 20.0):
    """Automatic resume of interrupted background jobs (called at startup).
    The delay avoids restarting during a hot-reload burst."""
    async def _resume():
        await asyncio.sleep(delay_s)
        try:
            job = await _db.jobs.find_one({"_id": "osm_validation"})
            if not job or not job.get("desired") or OSM_STATE.running:
                return
            params = job.get("params") or {}
            only_unchecked = bool(params.get("only_unchecked", True))
            q = {"lat": {"$ne": None}}
            if only_unchecked:
                q["osm_checked_at"] = {"$exists": False}
            remaining = await _db.poe_ports.count_documents(q)
            if remaining == 0:
                await _db.jobs.update_one({"_id": "osm_validation"}, {"$set": {
                    "desired": False, "finished_at": poe.now_iso(),
                    "note": "reprise inutile: tous les PoE déjà vérifiés"}})
                return
            print(f"[resume] validation OSM interrompue détectée — reprise auto ({remaining} PoE restants)")
            _start_osm_task(only_unchecked, int(params.get("limit") or 0),
                            int(params.get("radius_m") or 3000), resumed=True)
        except Exception as e:
            print(f"[resume] échec de la reprise automatique: {e}")

    asyncio.create_task(_resume())


@router.post("/poe/validate-osm", status_code=202)
async def poe_validate_osm(body: OsmValidateBody | None = None):
    if OSM_STATE.running:
        raise HTTPException(409, "OSM validation already running")
    body = body or OsmValidateBody()
    _start_osm_task(body.only_unchecked, body.limit, body.radius_m)
    return {"status": "started", "only_unchecked": body.only_unchecked,
            "limit": body.limit, "radius_m": body.radius_m}


@router.get("/poe/validate-osm/status")
async def poe_validate_osm_status():
    st = OSM_STATE.status()
    if _db is not None:
        st["osm_checked_total"] = await _db.poe_ports.count_documents({"osm_checked_at": {"$exists": True}})
        st["high_confidence_total"] = await _db.poe_ports.count_documents({"osm_confidence": {"$gte": 0.5}})
    return st


@router.post("/poe/validate-osm/cancel")
async def poe_validate_osm_cancel():
    if not OSM_STATE.running:
        raise HTTPException(409, "No OSM validation running")
    OSM_STATE.cancel = True
    OSM_STATE.log("Annulation demandée — arrêt propre après le PoE en cours")
    return {"cancelling": True}


# ---------------------------------------------------------------------------
# UNCLOS legal qualification of EEZs without a PoE (business logic, instant)
# ---------------------------------------------------------------------------
@router.post("/poe/qualify-unclos")
async def poe_qualify_unclos():
    zones = await _db.eez_zones.find({}, {"mrgid": 1, "name": 1, "geoname": 1, "pol_type": 1,
                                          "anchor": 1, "poe_count": 1, "unclos": 1}).to_list(1000)
    counts: dict = {}
    qualified = cleared = 0
    for z in zones:
        q = poe.qualify_unclos(z)
        if q:
            q["qualified_at"] = poe.now_iso()
            await _db.eez_zones.update_one({"_id": z["_id"]}, {"$set": {"unclos": q}})
            counts[q["code"]] = counts.get(q["code"], 0) + 1
            qualified += 1
        elif z.get("unclos"):
            await _db.eez_zones.update_one({"_id": z["_id"]}, {"$unset": {"unclos": ""}})
            cleared += 1
    return {"zones_scanned": len(zones), "qualified": qualified, "cleared": cleared, "by_code": counts}


# ---------------------------------------------------------------------------
# Ports & exports
# ---------------------------------------------------------------------------
@router.get("/poe/ports")
async def poe_ports(mrgid: int | None = None, country: str | None = None,
                    visible: bool = False):
    if visible:
        from app.services.review_gold import visible_poe_port_docs
        docs = await visible_poe_port_docs(_db, mrgid=mrgid, country=country)
    else:
        q: dict = {}
        if mrgid is not None:
            q["mrgid"] = mrgid
        if country:
            q["country_iso2"] = country.upper()
        docs = await _db.poe_ports.find(q).to_list(10000)
    return await asyncio.to_thread(poe.ports_to_geojson, docs)


@router.get("/export/poe.geojson")
async def export_poe_geojson():
    from app.core.export_meta import export_response
    docs = await _db.poe_ports.find({}).to_list(10000)
    return export_response(
        poe.ports_to_geojson(docs), "poe", "ports_of_entry.geojson",
        license_note=("Extraction Blue Intelligence de sources gouvernementales — "
                      "chaque port cite sa source ; statuts à vérifier avant escale"),
    )


@router.post("/poe/verify")
async def poe_verify(payload: dict):
    """Verify PoE for a zone: run pipeline and return Nemotron status + registry validation + H-MEM activity.

    Validated ports are tiers 1-2 of the run's registry check (official-source evidence);
    a UN/LOCODE / World Port Index match alone is tier_registered, never validated. Each
    port carries registry {tier, label, validation, matches, official_evidence,
    coord_mismatch, first_port_of_entry, reasons}.

    Input: { "zone_name": str, "mrgid": int | None }
    Output: { zone_name, mrgid, run_id, nemotron_status, validated_ports, non_validated_ports,
              ports_by_tier, zetaris_mode, registry, memory, brief, error, trace_name }
    memory is {recalled, remembered, created, reinforced, superseded}. trace_name is the
    trace file's name (logs/trace-<run_id>.jsonl on the server), never a filesystem path.
    """
    from pathlib import PurePath
    from app.pipeline.run_pipeline import run_pipeline, validate_input

    try:
        zone_name, _ = validate_input(payload.get("zone_name"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    mrgid = payload.get("mrgid")

    # Run the pipeline
    result = await run_pipeline(zone_name)

    # Registry evidence recorded by the run's ZetarisAdapter.registry_check step
    ports = result.get("ports") or []
    validated_ports = [p for p in ports if (p.get("registry") or {}).get("validation") == "official"]
    non_validated_ports = [p for p in ports if (p.get("registry") or {}).get("validation") != "official"]
    ports_by_tier: dict[str, list[str]] = {}
    for p in ports:
        ports_by_tier.setdefault((p.get("registry") or {}).get("tier") or "unchecked", []).append(p.get("name"))

    # Which model actually wrote the brief (served model, as reported by the runtime)
    llm = result.get("llm") or {}
    nemotron_status = {
        "provider": llm.get("provider"),
        "model": llm.get("model"),
        "nemotron": "nemotron" in (llm.get("model") or "").lower(),
        "fallback_reason": llm.get("fallback_reason"),
    }

    return {
        "zone_name": zone_name,
        "mrgid": mrgid,
        "run_id": result["run_id"],
        "nemotron_status": nemotron_status,
        "validated_ports": validated_ports,
        "non_validated_ports": non_validated_ports,
        "zetaris_mode": (result.get("components") or {}).get("zetaris"),
        "ports_by_tier": ports_by_tier,
        "registry": result.get("registry"),
        "memory": result.get("memory"),
        "brief": result.get("brief"),
        "error": result.get("error"),
        "trace_name": PurePath(result["trace_file"]).name,
    }
