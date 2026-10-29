"""Human review queue — one card at a time, persisted comment.

Reads only run / v1 collections. NEVER writes `projects`,
`poe_ports`, `eez_zones`, `marinas`, `capitaineries` or `amp_sites`. Mutated:
`review_comments`, `review_gold`, `review_choices`,
`review_suggest`, `review_lessons`.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

from app.core.dedup import normalize_name
from app.core.identity import same_site
from app.services.marina_world import google_maps_url
from app.services.poe_zone_fiche import (
    PUBLISHED_RUN,
    bu_by_port_name,
    build_zone_fiche,
)
from app.services.poe_zone_label import attach_zone_labels
from app.services import review_gold
from app.services.review_choices import (
    empty_choices,
    ensure_choice_indexes,
    get_choices,
    gold_ready,
    site_ok,
)
from app.services.poe_stable import (
    STABLE_REVIEW_MRGIDS,
    STABLE_REVIEW_ORDER,
    STABLE_REVIEW_SET,
    is_stable_review_mrgid,
    stable_review_stub,
)

_PROJECT_QUEUE_PROJ = {
    "_id": 1, "title": 1, "url": 1, "funders": 1, "funder": 1, "verdict": 1,
    "lat": 1, "lon": 1, "snapped": 1, "snapped_coastal": 1, "geo_source": 1,
}
_POE_QUEUE_PROJ = {
    "_id": 1, "dedup_key": 1, "name": 1, "mrgid": 1, "zone_name": 1,
}
_MARINA_QUEUE_PROJ = {
    "_id": 1, "name": 1, "source": 1, "osm_id": 1, "lat": 1, "lon": 1,
    "source_id": 1,
}
_CAPITAINERIE_QUEUE_PROJ = {
    "_id": 1, "name": 1, "source": 1, "telephone": 1, "canal_vhf": 1,
    "osm_id": 1, "shom_id": 1, "noaa_id": 1, "lat": 1, "lon": 1, "source_id": 1,
}
_EEZ_QUEUE_PROJ = {
    "_id": 0, "mrgid": 1, "name": 1, "geoname": 1, "sovereign": 1,
    "iso2": 1, "sov_iso2": 1, "pol_type": 1, "poe_count": 1, "status": 1,
    "run_id": 1, "sources": 1, "sources_official": 1,
}

KINDS = ("project", "eez", "poe", "marina", "capitainerie", "amp")
_AMP_QUEUE_PROJ = {
    "_id": 1, "site_id": 1, "name": 1, "country": 1, "lfp": 1,
    "manager_url": 1, "visit_url": 1, "other_helpful_links": 1,
    "visit_candidates": 1, "visit_url_status": 1, "source_id": 1,
}
QUEUE_LIMIT_MAX = 2000
QUEUE_LIMIT_DEFAULT = 500


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def comment_key(kind: str, entity_id: str, run_id: str | None = None) -> str:
    """Union key `{mode}:{entity_id}`. `run_id` ignored (legacy debug)."""
    del run_id
    return f"{kind}:{_sid(entity_id)}"


def legacy_comment_key(kind: str, run_id: str, entity_id: str) -> str:
    return f"{kind}:{run_id}:{entity_id}"


def is_published(run_id: str | None) -> bool:
    return not run_id or run_id == PUBLISHED_RUN


def _sid(value) -> str:
    return "" if value is None else str(value)


async def ensure_review_indexes(db) -> None:
    try:
        await db.review_comments.create_index([("kind", 1), ("run_id", 1)])
        await db.review_comments.create_index("entity_id")
        await db.projects.create_index("title")
        await db.project_run_projects.create_index([("run_id", 1), ("title", 1)])
        await db.poe_ports.create_index("name")
        await db.poe_run_ports.create_index([("run_id", 1), ("name", 1)])
        await db.capitaineries.create_index("name")
        await review_gold.ensure_gold_indexes(db)
        await ensure_choice_indexes(db)
        from app.services.review_lessons import ensure_lesson_indexes
        await ensure_lesson_indexes(db)
    except Exception:
        pass


async def _comment_flags(db, kind: str, run_id: str) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    try:
        docs = await db.review_comments.find({"kind": kind}).to_list(20000)
    except Exception:
        return flags
    del run_id
    for d in docs:
        eid = _sid(d.get("entity_id"))
        flags[eid] = bool((d.get("comment") or "").strip())
    return flags


def _queue_item(entity_id: str, title: str, subtitle: str, flags: dict[str, bool],
                extra: dict | None = None) -> dict:
    item = {
        "id": entity_id,
        "title": title or "—",
        "subtitle": subtitle or "",
        "has_comment": bool(flags.get(entity_id)),
    }
    if extra:
        item.update(extra)
    return item


async def _count(coll, q: dict | None = None) -> int:
    if coll is None:
        return 0
    try:
        return await coll.count_documents(q or {})
    except Exception:
        return 0


async def list_runs(db, kind: str) -> dict:
    """Runs available for a card type, plus the published map (v1)."""
    if kind not in KINDS:
        raise ValueError("kind must be project|eez|poe|marina|capitainerie|amp")
    published_count = 0
    if kind == "project":
        published_count = await _count(db.projects)
    elif kind == "eez":
        published_count = await _count(db.eez_zones)
    elif kind == "poe":
        published_count = await _count(db.poe_ports)
    elif kind == "capitainerie":
        published_count = await _count(db.capitaineries)
    elif kind == "amp":
        published_count = await _count(db.amp_sites)
    else:
        published_count = await _count(db.marinas)

    recommended = None
    if kind == "eez":
        recommended = await review_gold.recommended_eez_run_id(db)

    items = [{
        "id": PUBLISHED_RUN,
        "label": "published",
        "state": "published",
        "created_at": None,
        "count": published_count,
        "dataset": kind,
        "recommended": False,
    }]
    if kind == "project":
        docs = await db.project_runs.find({}).to_list(100)
        docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
        for d in docs:
            if review_gold.is_test_run(d):
                continue
            rid = _sid(d.get("_id"))
            n = await _count(db.project_run_projects, {"run_id": rid})
            items.append({
                "id": rid,
                "label": d.get("label") or rid,
                "state": d.get("state"),
                "created_at": d.get("created_at"),
                "count": n,
                "dataset": kind,
                "recommended": False,
            })
    elif kind in ("eez", "poe"):
        docs = await db.poe_runs.find({}).to_list(100)
        docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
        coll = db.poe_run_zones if kind == "eez" else db.poe_run_ports
        for d in docs:
            if review_gold.is_test_run(d):
                continue
            rid = _sid(d.get("_id"))
            n = await _count(coll, {"run_id": rid})
            items.append({
                "id": rid,
                "label": d.get("label") or rid,
                "state": d.get("state"),
                "created_at": d.get("created_at"),
                "count": n,
                "dataset": kind,
                "recommended": bool(kind == "eez" and recommended and rid == recommended),
            })
    elif kind in ("marina", "capitainerie", "amp"):
        from app.services import isolated_runs
        dataset = {"marina": "marinas", "capitainerie": "capitaineries", "amp": "amp"}[kind]
        extra = await isolated_runs.list_meta_runs(db, dataset)
        meta = getattr(db, isolated_runs.spec_for(dataset).meta_coll)
        for row in extra:
            try:
                doc = await meta.find_one({"_id": row["id"]})
            except Exception:
                doc = None
            if review_gold.is_test_run(doc):
                continue
            items.append({
                "id": row["id"],
                "label": row.get("label") or row["id"],
                "state": row.get("state"),
                "created_at": row.get("created_at"),
                "count": row.get("count") or 0,
                "dataset": kind,
                "recommended": False,
                "wrote_live": bool(row.get("wrote_live")),
            })
    return {"kind": kind, "count": len(items), "items": items,
            "recommended_id": recommended if kind == "eez" else None}


async def _label_zones(db, zones: list[dict]) -> list[dict]:
    if not zones:
        return zones
    # Labels: one polygon = one card (France hexagon ≠ Mayotte).
    all_sibs: list[dict] = []
    try:
        all_sibs = await db.eez_zones.find(
            {},
            {"_id": 0, "mrgid": 1, "name": 1, "geoname": 1, "sovereign": 1,
             "iso2": 1, "sov_iso2": 1, "pol_type": 1},
        ).to_list(500)
    except Exception:
        all_sibs = []
    if all_sibs:
        attach_zone_labels(all_sibs)
        by_id = {int(s.get("mrgid") or 0): s for s in all_sibs}
        for z in zones:
            hit = by_id.get(int(z.get("mrgid") or 0))
            if hit:
                for key in ("label", "qualifier", "qualifier_key", "disambiguated"):
                    z[key] = hit.get(key)
    else:
        attach_zone_labels(zones)
    return zones


def _clamp_page(offset: int, limit: int) -> tuple[int, int]:
    off = max(0, int(offset or 0))
    lim = int(limit or QUEUE_LIMIT_DEFAULT)
    lim = max(1, min(lim, QUEUE_LIMIT_MAX))
    return off, lim


def _title_filter(field: str, q: str) -> dict:
    needle = (q or "").strip()
    if not needle:
        return {}
    return {field: {"$regex": re.escape(needle), "$options": "i"}}


def _as_mrgid(value) -> int | None:
    try:
        mid = int(value)
    except (TypeError, ValueError):
        return None
    return mid or None


def _eez_sort_key(zone: dict) -> tuple:
    mid = _as_mrgid(zone.get("mrgid")) or 0
    name = (zone.get("label") or zone.get("name") or "").casefold()
    if mid in STABLE_REVIEW_ORDER:
        return (0, STABLE_REVIEW_ORDER[mid], name)
    return (1, 0, name)


def _eez_subtitle(zone: dict) -> str:
    who = (zone.get("sovereign") or zone.get("iso2") or "").strip()
    try:
        n = int(zone.get("poe_count") or 0)
    except (TypeError, ValueError):
        n = 0
    bit = f"{n} port" if n == 1 else f"{n} ports"
    if who:
        return f"{who} · {bit}"
    return bit


async def _eez_queue_docs(db, rid: str, stable_only: bool) -> list[dict]:
    if is_published(rid):
        docs = await db.eez_zones.find({}, _EEZ_QUEUE_PROJ).to_list(2000)
    else:
        docs = await db.poe_run_zones.find(
            {"run_id": rid}, _EEZ_QUEUE_PROJ).to_list(2000)
    if not stable_only:
        return docs

    have: dict[int, dict] = {}
    for z in docs:
        mid = _as_mrgid(z.get("mrgid"))
        if mid and mid in STABLE_REVIEW_SET:
            have[mid] = z
    best = await review_gold.best_productive_stable_zones(db)
    for mid, z in best.items():
        cur = have.get(mid)
        if cur is None or not review_gold.eez_extraction_is_productive(cur):
            overlay = dict(z)
            src = _sid(z.get("run_id"))
            if src and src != rid:
                overlay["_source_run_id"] = src
            have[mid] = overlay
    missing = [m for m in STABLE_REVIEW_MRGIDS if m not in have]
    if missing:
        try:
            extras = await db.eez_zones.find(
                {"mrgid": {"$in": missing}}, _EEZ_QUEUE_PROJ).to_list(50)
        except Exception:
            extras = []
        for z in extras:
            mid = _as_mrgid(z.get("mrgid"))
            if mid and mid not in have:
                have[mid] = z
    out: list[dict] = []
    for mid in STABLE_REVIEW_MRGIDS:
        out.append(have[mid] if mid in have else stable_review_stub(mid))
    return out


async def list_queue(db, kind: str, run_id: str | None = None,
                     offset: int = 0, limit: int = QUEUE_LIMIT_DEFAULT,
                     q: str = "", pre_gold: bool = False,
                     stable: bool = False) -> dict:
    if kind not in KINDS:
        raise ValueError("kind must be project|eez|poe|marina|capitainerie|amp")
    rid = run_id or PUBLISHED_RUN
    offset, limit = _clamp_page(offset, limit)
    flags = await _comment_flags(db, kind, rid)
    items: list[dict] = []
    needle = (q or "").strip().casefold()
    overrides = await review_gold.overrides_map(db, kind)
    pre_eez = await review_gold.pre_gold_eez_mrgids(db) if kind == "eez" else set()

    if kind == "project":
        filt: dict = {} if is_published(rid) else {"run_id": rid}
        filt.update(_title_filter("title", q))
        coll = db.projects if is_published(rid) else db.project_run_projects
        docs = await (coll.find(filt, _PROJECT_QUEUE_PROJ)
                      .sort("title", 1).to_list(20000))
        for d in docs:
            eid = _sid(d.get("_id") or d.get("url"))
            if not eid:
                continue
            pre = review_gold.is_pre_gold_project(d)
            if pre_gold and not pre:
                continue
            funders = d.get("funders") or ([d.get("funder")] if d.get("funder") else [])
            items.append(_queue_item(
                eid, d.get("title") or "",
                ", ".join(x for x in funders if x) or (d.get("verdict") or ""),
                flags,
                {
                    "verdict": d.get("verdict"),
                    "pre_gold": pre,
                    "gold_on": review_gold.gold_is_on(overrides.get(eid)),
                },
            ))

    elif kind == "eez":
        docs = await _eez_queue_docs(db, rid, bool(stable))
        docs = await _label_zones(db, docs)
        docs.sort(key=_eez_sort_key)
        if needle:
            docs = [z for z in docs if needle in (
                f"{z.get('label') or ''} {z.get('name') or ''} {z.get('sovereign') or ''}"
            ).casefold()]
        for z in docs:
            mid = _as_mrgid(z.get("mrgid"))
            if mid is None:
                continue
            eid = str(mid)
            pre = mid in pre_eez
            if pre_gold and not pre:
                continue
            extra = {
                "mrgid": mid,
                "poe_count": z.get("poe_count") or 0,
                "stable": is_stable_review_mrgid(mid),
                "pre_gold": pre,
                "gold_on": review_gold.eez_is_published(overrides.get(eid)),
            }
            src_run = _sid(z.get("_source_run_id"))
            if src_run:
                extra["source_run_id"] = src_run
            items.append(_queue_item(
                eid,
                z.get("label") or z.get("name") or eid,
                _eez_subtitle(z),
                flags,
                extra,
            ))

    elif kind == "poe":
        filt = {} if is_published(rid) else {"run_id": rid}
        filt.update(_title_filter("name", q))
        coll = db.poe_ports if is_published(rid) else db.poe_run_ports
        docs = await (coll.find(filt, _POE_QUEUE_PROJ)
                      .sort("name", 1).to_list(20000))
        for d in docs:
            eid = _sid(d.get("dedup_key") or d.get("_id"))
            if not eid:
                continue
            items.append(_queue_item(
                eid, d.get("name") or "",
                d.get("zone_name") or str(d.get("mrgid") or ""),
                flags,
                {"mrgid": d.get("mrgid"), "pre_gold": False, "gold_on": False},
            ))

    elif kind == "capitainerie":
        filt = _title_filter("name", q)
        if is_published(rid):
            docs = await (db.capitaineries.find(filt, _CAPITAINERIE_QUEUE_PROJ)
                          .sort("name", 1).to_list(50000))
        else:
            filt = {"run_id": rid, **filt}
            docs = await (db.capitainerie_run_sites.find(filt, _CAPITAINERIE_QUEUE_PROJ)
                          .sort("name", 1).to_list(50000))
        for d in docs:
            eid = _sid(d.get("source_id") or d.get("_id"))
            if not eid:
                continue
            pre = review_gold.is_pre_gold_capitainerie(d)
            if pre_gold and not pre:
                continue
            items.append(_queue_item(
                eid, d.get("name") or "",
                d.get("source") or "",
                flags,
                {
                    "pre_gold": pre,
                    "gold_on": review_gold.gold_is_on(overrides.get(eid)),
                },
            ))

    elif kind == "amp":
        filt = _title_filter("name", q)
        if is_published(rid):
            docs = await (db.amp_sites.find(filt, _AMP_QUEUE_PROJ)
                          .sort("name", 1).to_list(20000))
        else:
            filt = {"run_id": rid, **filt}
            docs = await (db.amp_run_sites.find(filt, _AMP_QUEUE_PROJ)
                          .sort("name", 1).to_list(20000))
        for d in docs:
            eid = _sid(d.get("source_id") or d.get("site_id") or d.get("_id"))
            if not eid:
                continue
            pre = review_gold.is_pre_gold_amp(d)
            if pre_gold and not pre:
                continue
            items.append(_queue_item(
                eid, d.get("name") or "",
                d.get("country") or "",
                flags,
                {
                    "lfp": d.get("lfp"),
                    "manager_url": d.get("manager_url"),
                    "visit_url": d.get("visit_url"),
                    "pre_gold": pre,
                    "gold_on": review_gold.gold_is_on(overrides.get(eid)),
                },
            ))

    else:
        filt = _title_filter("name", q)
        if is_published(rid):
            docs = await (db.marinas.find(filt, _MARINA_QUEUE_PROJ)
                          .sort("name", 1).to_list(50000))
        else:
            filt = {"run_id": rid, **filt}
            docs = await (db.marina_run_marinas.find(filt, _MARINA_QUEUE_PROJ)
                          .sort("name", 1).to_list(50000))
        for d in docs:
            eid = _sid(d.get("source_id") or d.get("_id") or d.get("osm_id"))
            if not eid:
                continue
            pre = review_gold.is_pre_gold_marina(d)
            if pre_gold and not pre:
                continue
            items.append(_queue_item(
                eid, d.get("name") or "",
                d.get("source") or "",
                flags,
                {
                    "pre_gold": pre,
                    "gold_on": review_gold.gold_is_on(overrides.get(eid)),
                },
            ))

    total = len(items)
    items = items[offset:offset + limit]
    commented = sum(1 for it in items if it["has_comment"])
    return {
        "kind": kind,
        "run_id": rid,
        "total": total,
        "offset": offset,
        "limit": limit,
        "pre_gold": bool(pre_gold),
        "stable": bool(stable),
        "commented": commented,
        "items": items,
        "wrote_projects": False,
        "wrote_poe_ports": False,
        "wrote_marinas": False,
        "wrote_capitaineries": False,
        "wrote_amp_sites": False,
    }


def _dedupe_urls(*groups) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for raw in group or []:
            href = raw if isinstance(raw, str) else (raw or {}).get("url")
            href = (href or "").strip()
            if href and href not in seen:
                seen.add(href)
                out.append(href)
    return out


def _project_fiche(doc: dict, extras: list[dict] | None = None) -> dict:
    extras = [e for e in (extras or []) if isinstance(e, dict)]
    funders: list[str] = []
    seen_f: set[str] = set()
    for src in [doc, *extras]:
        for f in (src.get("funders") or ([src.get("funder")] if src.get("funder") else [])):
            name = str(f or "").strip()
            if name and name not in seen_f:
                seen_f.add(name)
                funders.append(name)
    urls = _dedupe_urls(
        [doc.get("url")],
        [e.get("url") for e in extras],
        doc.get("urls") or [],
    )
    sites: list[dict] = []
    seen_sites: set[str] = set()
    for src in [doc, *extras]:
        raw_sites = list(src.get("sites") or [])
        if not raw_sites and src.get("lat") is not None:
            raw_sites = [{
                "site_id": _sid(src.get("_id") or src.get("url") or "main"),
                "name": src.get("location") or src.get("title"),
                "lat": src.get("lat"),
                "lon": src.get("lon"),
                "geo_source": src.get("geo_source"),
                "snapped": bool(src.get("snapped") or src.get("snapped_coastal")),
                "hq_suspect": bool(src.get("hq_suspect")),
                "run_id": src.get("run_id"),
            }]
        for i, s in enumerate(raw_sites):
            if not isinstance(s, dict):
                continue
            row = dict(s)
            sid = _sid(row.get("site_id") or row.get("id") or f"{src.get('_id') or 's'}-{i}")
            if sid in seen_sites:
                continue
            seen_sites.add(sid)
            row["site_id"] = sid
            row["site_ok"] = site_ok(row, title=str(doc.get("title") or ""))
            sites.append(row)
    verdicts = []
    if doc.get("snapped") or doc.get("snapped_coastal"):
        verdicts.append("snapped")
    geo = str(doc.get("geo_source") or "").strip().lower()
    if "fallback" in geo:
        verdicts.append("fallback")
    if doc.get("hq_suspect") or geo in ("hq", "hq_suspect"):
        verdicts.append("hq_suspect")
    if not sites:
        verdicts.append("unlocated")
    return {
        "kind": "project",
        "review_kind": "project",
        "id": _sid(doc.get("_id") or doc.get("url")),
        "title": doc.get("title"),
        "url": urls[0] if urls else doc.get("url"),
        "urls": urls,
        "description": doc.get("description") or "",
        "funders": funders,
        "location": doc.get("location"),
        "lat": doc.get("lat"),
        "lon": doc.get("lon"),
        "s_ocean": doc.get("s_ocean"),
        "category_group": doc.get("category_group"),
        "image": doc.get("image"),
        "verdict": doc.get("verdict"),
        "verdicts": verdicts,
        "geo_source": doc.get("geo_source"),
        "sites": sites,
        "snapped": bool(doc.get("snapped")),
        "fiche_scope": "union" if extras else "v1",
        "wrote_projects": False,
    }


def _marina_fiche(doc: dict) -> dict:
    maps_place = doc.get("maps_place_url") or None
    website = doc.get("website")
    return {
        "kind": "marina",
        "review_kind": "marina",
        "id": _sid(doc.get("_id")),
        "name": doc.get("name"),
        "lat": doc.get("lat"),
        "lon": doc.get("lon"),
        "source": doc.get("source"),
        "priority": doc.get("priority"),
        "osm_id": doc.get("osm_id"),
        "tags": doc.get("tags") or {},
        "nearest_waypoint": doc.get("nearest_waypoint") or {},
        "enriched": bool(doc.get("enriched")),
        "enrichment_source": doc.get("enrichment_source"),
        "enriched_at": doc.get("enriched_at"),
        "canal_vhf": doc.get("canal_vhf"),
        "places_visiteurs": doc.get("places_visiteurs"),
        "tirant_eau_max_metres": doc.get("tirant_eau_max_metres"),
        "score_protection_meteo": doc.get("score_protection_meteo"),
        "services_disponibles": doc.get("services_disponibles"),
        "telephone_capitainerie": doc.get("telephone_capitainerie"),
        "resume_avis": doc.get("resume_avis"),
        "field_sources": doc.get("field_sources") or {},
        "website": website,
        "website_status": doc.get("website_status"),
        "maps_place_url": maps_place,
        "maps_url": (
            google_maps_url(doc.get("name"), doc["lat"], doc["lon"])
            if doc.get("lat") is not None and doc.get("lon") is not None
            else None
        ),
        "urls": _dedupe_urls([website, maps_place]),
        "wrote_marinas": False,
    }


def _capitainerie_fiche(doc: dict) -> dict:
    sources = list(doc.get("sources") or [])
    if doc.get("source") and doc.get("source") not in sources:
        sources = [doc.get("source"), *sources]
    return {
        "kind": "capitainerie",
        "review_kind": "capitainerie",
        "id": _sid(doc.get("_id")),
        "name": doc.get("name"),
        "lat": doc.get("lat"),
        "lon": doc.get("lon"),
        "source": doc.get("source"),
        "sources": sources,
        "osm_id": doc.get("osm_id"),
        "shom_id": doc.get("shom_id"),
        "noaa_id": doc.get("noaa_id"),
        "tags": doc.get("tags") or {},
        "enriched": bool(doc.get("enriched")),
        "enrichment_source": doc.get("enrichment_source"),
        "canal_vhf": doc.get("canal_vhf"),
        "telephone": doc.get("telephone"),
        "field_sources": doc.get("field_sources") or {},
        "website": doc.get("website"),
        "maps_url": (
            google_maps_url(doc.get("name"), doc["lat"], doc["lon"])
            if doc.get("lat") is not None and doc.get("lon") is not None
            else None
        ),
        "urls": _dedupe_urls([doc.get("website")]),
        "wrote_marinas": False,
        "wrote_capitaineries": False,
    }


async def _poe_fiche(db, doc: dict, run_id: str) -> dict:
    mrgid = doc.get("mrgid")
    seeds: list[dict] = []
    extras: list[dict] = []
    if mrgid is not None:
        try:
            seeds = await db.poe_seed_ports.find({"mrgid": int(mrgid)}).to_list(8000)
        except Exception:
            seeds = []
        try:
            if is_published(run_id):
                extras = await db.poe_run_ports.find({"mrgid": int(mrgid)}).to_list(8000)
            else:
                extras = await db.poe_run_ports.find(
                    {"run_id": run_id, "mrgid": int(mrgid)}).to_list(8000)
        except Exception:
            extras = []
    by = bu_by_port_name(list(seeds) + list(extras) + [doc])
    url_bu = by.get(normalize_name(doc.get("name") or "") or "")
    return {
        "kind": "poe",
        "id": _sid(doc.get("dedup_key") or doc.get("_id")),
        "name": doc.get("name"),
        "city": doc.get("city"),
        "lat": doc.get("lat"),
        "lon": doc.get("lon"),
        "mrgid": mrgid,
        "zone_name": doc.get("zone_name"),
        "country_iso2": doc.get("country_iso2"),
        "confidence": doc.get("confidence"),
        "spatial_kind": doc.get("spatial_kind"),
        "validated": bool(doc.get("validated")),
        "geocode_source": doc.get("geocode_source"),
        "osm_confidence": doc.get("osm_confidence"),
        "osm_tags": doc.get("osm_tags"),
        "note": doc.get("note"),
        "url_bu": url_bu,
        "wrote_poe_ports": False,
    }


async def get_comment(db, kind: str, run_id: str, entity_id: str) -> dict:
    eid = _sid(entity_id)
    doc = await db.review_comments.find_one({"_id": comment_key(kind, eid)})
    if not doc and run_id:
        doc = await db.review_comments.find_one({
            "_id": legacy_comment_key(kind, run_id, eid),
        })
    if not doc:
        return {"comment": "", "updated_at": None}
    return {
        "comment": doc.get("comment") or "",
        "updated_at": doc.get("updated_at"),
    }


async def save_comment(db, kind: str, run_id: str, entity_id: str, comment: str) -> dict:
    if kind not in KINDS:
        raise ValueError("kind must be project|eez|poe|marina|capitainerie|amp")
    rid = run_id or PUBLISHED_RUN
    eid = _sid(entity_id)
    cid = comment_key(kind, eid)
    text = comment if comment is not None else ""
    if not isinstance(text, str):
        text = str(text)
    doc = {
        "_id": cid,
        "kind": kind,
        "run_id": rid,
        "entity_id": eid,
        "comment": text,
        "updated_at": now_iso(),
    }
    await db.review_comments.update_one({"_id": cid}, {"$set": doc}, upsert=True)
    return {
        "kind": kind,
        "run_id": rid,
        "id": eid,
        "comment": text,
        "updated_at": doc["updated_at"],
        "wrote_projects": False,
        "wrote_poe_ports": False,
        "wrote_marinas": False,
        "wrote_capitaineries": False,
        "wrote_amp_sites": False,
    }


def _amp_visit_candidates(doc: dict) -> list[dict]:
    from app.services.amp import (
        extract_urls, protectedseas_visit_blobs, urls_equivalent,
    )
    manager = doc.get("manager_url")
    seen: set[str] = set()
    out: list[dict] = []

    def _add(url: str | None, source: str, status: str | None = None):
        href = (url or "").strip()
        if not href or href in seen:
            return
        seen.add(href)
        same = urls_equivalent(href, manager)
        out.append({
            "url": href,
            "source": source,
            "same_as_manager": bool(same),
            "status": status or (
                "rejected_same_as_manager" if same else "candidate"
            ),
        })

    for raw in doc.get("visit_candidates") or []:
        if isinstance(raw, dict):
            _add(raw.get("url"), raw.get("source") or "pipeline", raw.get("status"))
        else:
            _add(str(raw), "pipeline")
    _add(doc.get("visit_url"), doc.get("visit_url_source") or "pipeline",
         doc.get("visit_url_status"))
    for blob in protectedseas_visit_blobs(doc):
        for href in extract_urls(blob):
            _add(href, "protectedseas")
    return out


def _amp_fiche(doc: dict) -> dict:
    from app.services.amp import public_properties
    props = public_properties(doc)
    candidates = _amp_visit_candidates(doc)
    return {
        "kind": "amp",
        "review_kind": "amp",
        "id": props.get("site_id"),
        **props,
        "visit_candidates": candidates,
        "wrote_amp_sites": False,
    }


async def _project_union_extras(db, doc: dict) -> list[dict]:
    """Run occurrences of the same project (url / _id / same_site), no canaries."""
    if not doc:
        return []
    eid = _sid(doc.get("_id") or doc.get("url"))
    url = str(doc.get("url") or "").strip()
    extras: list[dict] = []
    seen: set[str] = set()

    async def _take(rows):
        for row in rows or []:
            rid = _sid(row.get("_id") or row.get("url"))
            if not rid or rid == eid or rid in seen:
                continue
            seen.add(rid)
            extras.append(row)

    try:
        if eid:
            await _take(await db.project_run_projects.find({"_id": eid}).to_list(50))
        if url:
            await _take(await db.project_run_projects.find({"url": url}).to_list(50))
        title = str(doc.get("title") or "").strip()
        if title:
            cand = await db.project_run_projects.find({"title": title}).to_list(80)
            await _take([
                row for row in cand
                if same_site(doc, row, title_key="title")
            ])
    except Exception:
        return extras
    return extras


async def get_fiche(db, kind: str, run_id: str | None, entity_id: str,
                    content_run_id: str | None = None) -> dict | None:
    if kind not in KINDS:
        raise ValueError("kind must be project|eez|poe|marina|capitainerie|amp")
    rid = run_id or PUBLISHED_RUN
    content_rid = content_run_id or rid
    eid = _sid(entity_id)
    fiche = None
    doc = None

    if kind == "project":
        if is_published(rid):
            doc = await db.projects.find_one({"_id": eid})
            if not doc:
                doc = await db.projects.find_one({"url": eid})
        else:
            doc = await db.project_run_projects.find_one({"run_id": rid, "_id": eid})
            if not doc:
                doc = await db.project_run_projects.find_one({"run_id": rid, "url": eid})
        if not doc:
            return None
        extras = await _project_union_extras(db, doc) if is_published(rid) else []
        fiche = _project_fiche(doc, extras)

    elif kind == "eez":
        try:
            mid = int(eid)
        except (TypeError, ValueError):
            return None
        union = is_published(rid)
        fiche_run = rid if union else content_rid
        fiche = await build_zone_fiche(
            db, mid, run_id=fiche_run, union=union)
        if fiche is None:
            return None
        # `kind` stays the pipeline verdict (none / general_list / …).
        fiche["review_kind"] = "eez"
        fiche["id"] = str(mid)

    elif kind == "poe":
        if is_published(rid):
            doc = await db.poe_ports.find_one({"dedup_key": eid})
            if not doc:
                doc = await db.poe_ports.find_one({"_id": eid})
        else:
            doc = await db.poe_run_ports.find_one({"run_id": rid, "dedup_key": eid})
            if not doc:
                doc = await db.poe_run_ports.find_one({"run_id": rid, "_id": eid})
        if not doc:
            return None
        fiche = await _poe_fiche(db, doc, rid)

    elif kind == "capitainerie":
        if is_published(rid):
            doc = await db.capitaineries.find_one({"_id": eid})
            if not doc:
                doc = await db.capitaineries.find_one({"osm_id": eid})
            if not doc:
                doc = await db.capitaineries.find_one({"shom_id": eid})
            if not doc:
                doc = await db.capitaineries.find_one({"noaa_id": eid})
        else:
            from app.services.isolated_runs import find_item
            doc = await find_item(db, "capitaineries", rid, eid)
        if not doc:
            return None
        fiche = _capitainerie_fiche(doc)

    elif kind == "amp":
        if is_published(rid):
            doc = await db.amp_sites.find_one({"_id": eid})
            if not doc:
                doc = await db.amp_sites.find_one({"site_id": eid})
        else:
            from app.services.isolated_runs import find_item
            doc = await find_item(db, "amp", rid, eid)
        if not doc:
            return None
        fiche = _amp_fiche(doc)

    else:
        if is_published(rid):
            doc = await db.marinas.find_one({"_id": eid})
            if not doc:
                doc = await db.marinas.find_one({"osm_id": eid})
        else:
            from app.services.isolated_runs import find_item
            doc = await find_item(db, "marinas", rid, eid)
        if not doc:
            return None
        fiche = _marina_fiche(doc)
        from app.services.control_ref import marina_control_ref
        fiche["control_ref"] = await marina_control_ref(db, doc)

    comment = await get_comment(db, kind, rid, eid)
    source = doc if kind in ("project", "marina", "capitainerie", "amp") else None
    pre = await review_gold.is_pre_gold_entity(db, kind, eid, source)
    override = await review_gold.get_override(db, kind, eid)
    choices = await get_choices(db, kind, eid) if kind in (
        "eez", "project", "marina", "capitainerie", "amp") else empty_choices()
    if kind == "eez":
        gold_on = review_gold.eez_is_published(override)
    else:
        gold_on = review_gold.gold_is_on(override)
    ready = gold_ready(fiche, choices, kind=kind,
                       comment=comment.get("comment") or "")
    return {
        "kind": kind,
        "run_id": rid,
        "id": eid,
        "fiche": fiche,
        "comment": comment.get("comment") or "",
        "comment_updated_at": comment.get("updated_at"),
        "pre_gold": pre,
        "gold_on": gold_on,
        "choices": choices,
        "gold_ready": ready,
        "wrote_projects": False,
        "wrote_poe_ports": False,
        "wrote_marinas": False,
        "wrote_capitaineries": False,
        "wrote_amp_sites": False,
    }
