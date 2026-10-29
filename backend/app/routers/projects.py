"""app.routers.projects — Marine projects: list, imports/exports, categories,
on-demand enrichment, community reports."""
import asyncio
import difflib
import html
import ipaddress
import os
import time
import uuid

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from app.core.public_url import URL_MAX_LENGTH, public_url_error
from app.core.report_rate import allow as allow_report
from app.core.tasks import new_task, prune_tasks
from app.db import db, get_settings
from app.services.swarm_pipeline import now_iso
from app.services.geojson_import import empty_import_result, parse_feature_collection
from app.state import swarm
from app.static_data.categories import CATEGORY_GROUPS, normalize_category

router = APIRouter(prefix="/api")

PROJECT_ENRICH_TASKS: dict[str, dict] = {}

def project_to_feature(p: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
        "properties": {
            "id": p["_id"],
            "title": p["title"],
            "url": p["url"],
            "description": p.get("description", ""),
            "funder": ", ".join(p.get("funders") or [p.get("funder", "")]),
            "location": p.get("location"),
            "s_ocean": p.get("s_ocean"),
            "snapped": p.get("snapped", False),
            "image": p.get("image"),
            "category": p.get("category"),
            "category_group": p.get("category_group") or normalize_category(p.get("category")),
        },
    }

@router.get("/projects")
async def get_projects(funder: str | None = None, visible: bool = False):
    q = {}
    if funder and funder != "All":
        q = {"funders": funder}
    docs = await db.projects.find(q).to_list(20000)
    if visible:
        from app.services.review_gold import filter_visible
        docs = await filter_visible(
            db, "project", docs, lambda p: p.get("_id") or p.get("url"))
    return {"type": "FeatureCollection", "features": [project_to_feature(p) for p in docs]}


@router.get("/funders")
async def get_funders(visible: bool = False):
    docs = await db.projects.find({}, {"funders": 1, "title": 1, "lat": 1, "lon": 1,
                                       "snapped": 1, "snapped_coastal": 1,
                                       "geo_source": 1, "url": 1, "_id": 1}).to_list(20000)
    if visible:
        from app.services.review_gold import filter_visible
        docs = await filter_visible(
            db, "project", docs, lambda p: p.get("_id") or p.get("url"))
    counts = {}
    for d in docs:
        for f in d.get("funders") or []:
            counts[f] = counts.get(f, 0) + 1
    return {"total": len(docs), "funders": [{"name": k, "count": v} for k, v in sorted(counts.items())]}


@router.delete("/projects", status_code=410)
async def clear_projects():
    raise HTTPException(410, "purging projects is disabled")


@router.get("/export/geojson")
async def export_geojson():
    from app.core.export_meta import export_response
    docs = await db.projects.find({}).to_list(20000)
    fc = {"type": "FeatureCollection", "features": [project_to_feature(p) for p in docs]}
    return export_response(
        fc, "projects", "blue_intelligence_projects.geojson",
        license_note="Extraction automatique de sources publiques (fondations) — vérifier la source de chaque fiche",
    )

@router.post("/import/geojson")
async def import_geojson(fc: dict = Body(...)):
    kind, feats = parse_feature_collection(fc)
    if kind == "invalid":
        raise HTTPException(400, "invalid GeoJSON FeatureCollection")
    if kind == "empty":
        return empty_import_result("total_projects", await db.projects.count_documents({}))
    existing = await db.projects.find({}, {"url": 1, "title": 1, "lat": 1, "lon": 1, "funders": 1}).to_list(50000)
    seen_urls = {e.get("url") for e in existing}
    grid = {}
    for e in existing:
        grid.setdefault((round(e["lat"], 1), round(e["lon"], 1)), []).append(e)
    imported = merged = invalid = skipped = 0
    docs = []
    for f in feats:
        try:
            geom = f.get("geometry") or {}
            if geom.get("type") != "Point":
                invalid += 1
                continue
            lon, lat = float(geom["coordinates"][0]), float(geom["coordinates"][1])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                invalid += 1
                continue
            p = f.get("properties") or {}
            title = str(p.get("title") or "").strip()
            url = str(p.get("url") or "").strip()
            if not title or not url:
                invalid += 1
                continue
            if url in seen_urls:
                # URL already imported — silent backfill of category/category_group if missing.
                incoming_cat = p.get("category")
                incoming_grp = p.get("category_group") or normalize_category(incoming_cat)
                if incoming_cat or incoming_grp:
                    update_set = {}
                    if incoming_cat:
                        update_set["category"] = incoming_cat
                    if incoming_grp:
                        update_set["category_group"] = incoming_grp
                    if update_set:
                        await db.projects.update_one(
                            {"url": url, "$or": [
                                {"category_group": {"$exists": False}},
                                {"category_group": None},
                                {"category_group": ""},
                                {"category_group": "Other"},
                            ]},
                            {"$set": update_set},
                        )
                skipped += 1
                continue
            s = p.get("s_ocean", p.get("s_ocean_score", p.get("relevance_score", 0.5)))
            try:
                s = float(s)
                s = round(s / 100, 3) if s > 1 else round(s, 3)
            except (TypeError, ValueError):
                s = 0.5
            funder = str(p.get("funder") or "Imported").strip() or "Imported"
            cell = (round(lat, 1), round(lon, 1))
            dup = None
            for e in grid.get(cell, []):
                if difflib.SequenceMatcher(None, title.lower(), e["title"].lower()).ratio() > 0.9:
                    dup = e
                    break
            if dup:
                funders = list(set((dup.get("funders") or []) + [funder]))
                update_set = {"funders": funders}
                # Backfill category & category_group on already-imported docs (fixes historical import loss)
                incoming_cat = p.get("category")
                incoming_grp = p.get("category_group") or normalize_category(incoming_cat)
                if incoming_cat and not dup.get("category"):
                    update_set["category"] = incoming_cat
                if incoming_grp and not dup.get("category_group"):
                    update_set["category_group"] = incoming_grp
                await db.projects.update_one({"_id": dup["_id"]}, {"$set": update_set})
                merged += 1
                seen_urls.add(url)
                continue
            doc = {
                "_id": str(uuid.uuid4()), "title": title[:200], "url": url,
                "description": str(p.get("description") or "")[:250],
                "funder": funder, "funders": [funder],
                "location": p.get("location"), "lat": lat, "lon": lon,
                "s_ocean": s, "snapped": bool(p.get("snapped") or p.get("snapped_coastal") or False),
                "geo_source": "import", "image": p.get("image") or p.get("image_url"),
                "category": p.get("category"),
                "category_group": p.get("category_group") or normalize_category(p.get("category")),
                "engine": "GeoJSON Import", "created_at": now_iso(),
            }
            docs.append(doc)
            seen_urls.add(url)
            grid.setdefault(cell, []).append({"_id": doc["_id"], "title": title, "lat": lat, "lon": lon, "funders": [funder]})
            imported += 1
        except Exception:
            invalid += 1
    if docs:
        await db.projects.insert_many(docs)
    total = await db.projects.count_documents({})
    swarm.log(f"GeoJSON import: {imported} imported, {merged} merged, {skipped} already known, {invalid} invalid", "success")
    return {"imported": imported, "merged": merged, "skipped_existing": skipped, "invalid": invalid, "total_projects": total}

@router.get("/categories")
async def get_categories():
    rows = await db.projects.aggregate([
        {"$group": {"_id": "$category_group", "n": {"$sum": 1}}},
    ]).to_list(50)
    counts = {r["_id"] or "Other": r["n"] for r in rows}
    return {"groups": [{"name": g, "color": c, "count": counts.get(g, 0)} for g, c in CATEGORY_GROUPS.items()]}

# ---------- On-demand project enrich (Phase 3, async as of Phase 3.1) ----------
async def _run_project_enrich(project_id: str, task: dict):
    """Actual enrichment work — runs in background."""

    def log_fn(msg: str):
        task["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(task["logs"]) > 200:
            task["logs"] = task["logs"][-200:]

    proj = await db.projects.find_one({"_id": project_id})
    if not proj:
        task["state"] = "error"
        task["error"] = "project not found"
        task["finished_at"] = time.time()
        return
    url = proj.get("url")
    if not url:
        task["state"] = "error"
        task["error"] = "project has no URL"
        task["finished_at"] = time.time()
        return
    log_fn(f"Refreshing {url}")

    from bs4 import BeautifulSoup as _BS
    from app.core.extract import read_url as _read_url
    from app.core.llm import extract_project as _extract, gatekeeper_check as _gk
    from app.services.swarm_pipeline import pick_image as _pick_image

    try:
        settings = await get_settings()
        page = await _read_url(url, min_chars=200, prefer_fetch=False, log=log_fn)
        text = page.get("text") or ""
        html = page.get("html") or ""
        page_title = (page.get("title") or "").strip() or proj.get("title") or url
        meta_desc = page.get("meta_desc") or ""
        image = page.get("image")
        if not image and html:
            full = _BS(html, "html.parser")
            image = _pick_image(full, full, url)
        log_fn(f"Fetched via {page.get('level')}: {len(text)} chars")
        gk = await _gk(page_title, text, settings)
        if not gk["accepted"]:
            log_fn(f"Gatekeeper REJECTED: {gk['reason'][:80]}")
            task["state"] = "error"
            task["error"] = f"gatekeeper: {gk['reason']}"
            task["finished_at"] = time.time()
            return
        funder = proj.get("funder") or (proj.get("funders") or ["Unknown"])[0]
        extracted = await _extract(page_title, text, meta_desc, url, funder, settings)
        log_fn(f"Extraction engine={extracted.get('engine')}, title='{(extracted.get('title') or '')[:60]}'")

        update: dict = {}
        for field, key in [
            ("title", "title"),
            ("description", "description"),
            ("location", "location"),
            ("category", "category"),
        ]:
            new_v = extracted.get(key)
            if new_v and str(new_v).strip() and str(new_v) != str(proj.get(field) or ""):
                update[field] = str(new_v).strip()[:250 if field == "description" else 200]
        if extracted.get("category"):
            update["category_group"] = normalize_category(extracted["category"])
        if image and (not proj.get("image") or image != proj.get("image")):
            update["image"] = image
        try:
            new_s = extracted.get("s_ocean")
            if new_s is not None:
                update["s_ocean"] = round(float(new_s), 3)
        except (TypeError, ValueError):
            pass
        update["enriched"] = True
        update["enriched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        update["enrichment_source"] = extracted.get("engine", "unknown")
        await db.projects.update_one({"_id": project_id}, {"$set": update})
        log_fn(f"Updated fields: {list(update.keys())}")
        fresh = await db.projects.find_one({"_id": project_id})
        task["result"] = {
            "project": project_to_feature(fresh),
            "updates": list(update.keys()),
        }
        task["state"] = "done"
    except Exception as e:
        log_fn(f"FAILED: {type(e).__name__}: {str(e)[:200]}")
        task["state"] = "error"
        task["error"] = f"{type(e).__name__}: {e}"
    finally:
        task["finished_at"] = time.time()


@router.post("/projects/{project_id}/enrich", status_code=202)
async def project_enrich_one(project_id: str):
    """
    Kick off project re-extraction as a background task.
    Returns 202 immediately; poll GET /api/projects/{project_id}/enrich/status.
    """
    proj = await db.projects.find_one({"_id": project_id})
    if not proj:
        raise HTTPException(404, "project not found")
    if not proj.get("url"):
        raise HTTPException(400, "project has no URL")

    existing = PROJECT_ENRICH_TASKS.get(project_id)
    if existing and existing.get("state") == "running":
        raise HTTPException(409, "Enrichment already in progress for this project")

    prune_tasks(PROJECT_ENRICH_TASKS)
    PROJECT_ENRICH_TASKS[project_id] = new_task()
    asyncio.create_task(_run_project_enrich(project_id, PROJECT_ENRICH_TASKS[project_id]))
    return {"status": "started", "project_id": project_id}


@router.get("/projects/{project_id}/enrich/status")
async def project_enrich_status(project_id: str):
    task = PROJECT_ENRICH_TASKS.get(project_id)
    if not task:
        return {"state": "idle", "project_id": project_id}
    return {
        "state": task["state"],
        "project_id": project_id,
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
        "result": task["result"],
        "error": task["error"],
        "logs_tail": task["logs"][-30:],
    }

class ReportBody(BaseModel):
    name: str
    url: str
    description: str = ""


_NAME_MAX = 200
_DESCRIPTION_MAX = 5000


def report_email_html(name: str, url: str, description: str) -> str:
    """Notification body. Every user field is escaped before it enters HTML."""
    safe_name = html.escape(name, quote=True)
    safe_url = html.escape(url, quote=True)
    safe_desc = html.escape((description or "—")[:1000], quote=True)
    return f"""<table style="font-family:Arial,sans-serif;max-width:560px;"><tr><td>
<h2 style="color:#0284c7;">🌊 Nouveau projet signalé sur Blue Intelligence</h2>
<p><b>Nom du projet :</b> {safe_name}</p>
<p><b>URL :</b> <a href="{safe_url}">{safe_url}</a></p>
<p><b>Description :</b><br/>{safe_desc}</p>
<p style="color:#64748b;font-size:12px;">Ce projet a été ajouté automatiquement à la file d'attente du Swarm et sera analysé lors du prochain run.</p>
</td></tr></table>"""


def _report_client_addr(request: Request) -> str:
    """Socket peer, or the proxy's X-Real-IP when the peer is the local proxy."""
    peer = (request.client.host if request.client else "") or ""
    trusted = False
    if peer in {"localhost", "testclient"}:
        trusted = True
    else:
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            ip = None
        if ip is not None and (ip.is_loopback or ip.is_private or ip.is_link_local):
            trusted = True
    if trusted:
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            return real[:80]
    return peer or "unknown"


@router.post("/report-project")
async def report_project(body: ReportBody, request: Request):
    name, url = body.name.strip(), body.url.strip()
    description = body.description or ""
    if not name:
        raise HTTPException(400, "name required and url must start with http(s)")
    if len(name) > _NAME_MAX:
        raise HTTPException(422, "name is too long")
    if len(url) > URL_MAX_LENGTH:
        raise HTTPException(422, "url is too long")
    if len(description) > _DESCRIPTION_MAX:
        raise HTTPException(422, "description is too long")
    if not url.lower().startswith("http"):
        # "not-a-url" stays 400 for existing clients. An explicit other scheme is 422.
        if ":" in url:
            raise HTTPException(422, "URL scheme must be http or https")
        raise HTTPException(400, "name required and url must start with http(s)")
    reason = public_url_error(url)
    if reason:
        raise HTTPException(422, reason)
    if not allow_report(_report_client_addr(request)):
        raise HTTPException(429, "Too many reports from this address. Try again later.")
    rid = str(uuid.uuid4())
    email_status = "skipped"
    resend_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if resend_key:
        try:
            import resend
            resend.api_key = resend_key
            subject_name = name.replace("\r", " ").replace("\n", " ")[:80]
            params = {
                "from": os.environ.get("SENDER_EMAIL", "onboarding@resend.dev"),
                "to": [os.environ.get("REPORT_RECIPIENT", "clementfilisetti@berrymappemonde.org")],
                "subject": f"[Blue Intelligence] Projet signalé : {subject_name}",
                "html": report_email_html(name, url, description),
            }
            result = await asyncio.to_thread(resend.Emails.send, params)
            email_status = "sent" if result.get("id") else "failed"
        except Exception as e:
            email_status = f"failed: {str(e)[:120]}"
    await db.reported_projects.insert_one({
        "_id": rid, "name": name, "url": url, "description": description[:1000],
        "status": "queued", "email_status": email_status, "ts": now_iso(),
    })
    await db.deeplink_pages.update_one(
        {"url": url},
        {"$set": {"url": url, "funder": "Community Report", "source": "user-report", "ts": now_iso()},
         "$setOnInsert": {"_id": str(uuid.uuid4())}},
        upsert=True,
    )
    queued_now = False
    if swarm.running and swarm.queue is not None:
        swarm.queued_count += 1
        await swarm.queue.put({"url": url, "funder": "Community Report", "source": "user-report", "depth": 1})
        queued_now = True
    swarm.log(f"Community report: '{name[:50]}' → {'queue (live)' if queued_now else 'DeepLinkCache (next run)'}", "success")
    return {"id": rid, "status": "queued_now" if queued_now else "queued_next_run", "email_status": email_status}


@router.get("/reports")
async def get_reports():
    docs = await db.reported_projects.find({}).sort("ts", -1).to_list(100)
    for d in docs:
        d["id"] = d.pop("_id")
    return docs

ARCGIS_MPA_URL = ""  # Removed in Phase 5 — MPA layer decommissioned.
MPA_FIELDS: list[str] = []  # Deprecated in Phase 5: MPA layer removed entirely.


# ---------- MPA endpoint removed in Phase 5 (Protected Areas layer decommissioned). ----------
# The /api/mpa endpoint returned MPA polygons from ArcGIS ProtectedSeas Navigator.
# Kept as HTTP 410 for backward compatibility with older frontends that may still ping it.
@router.get("/mpa", status_code=410, include_in_schema=False)
async def mpa_deprecated(bbox: str = ""):
    raise HTTPException(410, "MPA layer removed in Phase 5.")
