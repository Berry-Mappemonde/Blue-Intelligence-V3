"""Gold Dataset — certified Review run.

Live databases (`projects`, `poe_ports`, `eez_zones`, `marinas`,
`capitaineries`, `amp_sites`) are never written. Only `review_gold`
holds the overrides (on / off + snapshot).

Gold = explicit click. Map shows the certified run only if
“Show review” is checked. No silent pre-Gold.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone

from app.core.geo import haversine_km, ocean_fallback_coords
from app.services.poe_stable import STABLE_REVIEW_MRGIDS
from app.services.review_choices import (
    apply_snapshot_to_doc,
    build_gold_snapshot,
    empty_choices,
    finalize_choices,
    get_choices,
    gold_incomplete_message,
    gold_ready,
    save_choices_doc,
    snapshot_port_docs,
)


class GoldNotReady(ValueError):
    """Formalities Gold clicked too early — no official list kept."""

GOLD_KINDS = ("project", "eez", "marina", "capitainerie", "amp")
FALLBACK_SOURCES = frozenset({
    "ocean-region-fallback", "ocean_fallback", "ocean-fallback",
    "fallback", "ocean_region_fallback",
})
OSM_SOURCES = frozenset({"openstreetmap", "osm"})
FALLBACK_KM = 50.0
_TEST_LABEL = re.compile(
    r"^(canary|smoke|seed-enrich|test|debug)(?:[-_].*)?$",
    re.I,
)

_eez_pre_gold_cache: tuple[float, frozenset[int]] | None = None
_EEZ_CACHE_TTL_S = 300.0
_eez_locks: dict[int, asyncio.Lock] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sid(value) -> str:
    return "" if value is None else str(value)


def gold_key(kind: str, entity_id: str) -> str:
    return f"{kind}:{_sid(entity_id)}"


def is_test_run(doc: dict | None) -> bool:
    """Canaries, smokes, seed-enrich — not world prod runs."""
    if not doc:
        return False
    if str(doc.get("purpose") or "").strip().lower() == "test":
        return True
    for raw in (doc.get("label"), doc.get("_id")):
        text = str(raw or "").strip()
        if text and _TEST_LABEL.match(text):
            return True
    return False


def is_pre_gold_project(doc: dict | None) -> bool:
    """Neither snap_to_ocean nor a hashed ocean point (`ocean_fallback_coords`)."""
    if not doc:
        return False
    if doc.get("snapped") or doc.get("snapped_coastal"):
        return False
    geo = str(doc.get("geo_source") or "").strip().lower()
    if geo in FALLBACK_SOURCES:
        return False
    title = str(doc.get("title") or "")
    try:
        lat = float(doc["lat"])
        lon = float(doc["lon"])
    except (KeyError, TypeError, ValueError):
        return True
    if not title:
        return True
    flat, flon = ocean_fallback_coords(title)
    try:
        if haversine_km(lat, lon, flat, flon) < FALLBACK_KM:
            return False
    except Exception:
        return True
    return True


def is_pre_gold_marina(doc: dict | None) -> bool:
    """Queue: OSM identity + GPS present. Not a silent Gold."""
    if not doc:
        return False
    src = str(doc.get("source") or "").strip().lower()
    if src not in OSM_SOURCES and not doc.get("osm_id"):
        return False
    return _has_xy(doc)


def is_pre_gold_capitainerie(doc: dict | None) -> bool:
    """Queue: a building with GPS. Gold stays a click."""
    if not doc:
        return False
    if not (doc.get("osm_id") or doc.get("shom_id") or doc.get("noaa_id") or doc.get("name")):
        return False
    return _has_xy(doc)


def is_pre_gold_amp(doc: dict | None) -> bool:
    """Queue: something to judge (manager or visit candidates)."""
    if not doc:
        return False
    if doc.get("manager_url") or doc.get("visit_url") or doc.get("other_helpful_links"):
        return True
    return bool(doc.get("visit_candidates"))


def _has_xy(doc: dict | None) -> bool:
    if not doc:
        return False
    try:
        lat, lon = float(doc["lat"]), float(doc["lon"])
    except (KeyError, TypeError, ValueError):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def gold_is_on(override: dict | None) -> bool:
    """Gold = override on. Never a pre-Gold default."""
    return bool(override and override.get("on"))


def gold_pressed(is_pre_gold: bool, override: dict | None) -> bool:
    """Compat: pre-Gold no longer flips the switch."""
    del is_pre_gold
    return gold_is_on(override)


def eez_is_published(override: dict | None) -> bool:
    """Formalities Gold = override on AND card snapshot."""
    return bool(override and override.get("on") and override.get("snapshot"))


def eez_on_map(override: dict | None) -> bool:
    """Formalities “Show review” layer = published Gold card."""
    return eez_is_published(override)


def reset_eez_pre_gold_cache() -> None:
    global _eez_pre_gold_cache
    _eez_pre_gold_cache = None


async def production_poe_runs(db) -> list[dict]:
    try:
        docs = await db.poe_runs.find({}).to_list(200)
    except Exception:
        return []
    return [d for d in docs if not is_test_run(d)]


def _eez_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _eez_locks.get(id(loop))
    if lock is None:
        lock = asyncio.Lock()
        _eez_locks[id(loop)] = lock
    return lock


def _mrgid_set(docs: list[dict]) -> set[int]:
    out: set[int] = set()
    for d in docs or []:
        try:
            mid = int(d.get("mrgid") or 0)
        except (TypeError, ValueError):
            continue
        if mid:
            out.add(mid)
    return out


def eez_extraction_is_productive(doc: dict | None) -> bool:
    """At least one port and one official source — v1 alone is not enough."""
    if not doc:
        return False
    try:
        n = int(doc.get("poe_count") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return False
    if doc.get("sources_official"):
        return True
    for src in doc.get("sources") or []:
        if not isinstance(src, dict):
            continue
        if src.get("official"):
            return True
        if str(src.get("kind") or "").strip().lower() == "official":
            return True
    return False


async def pre_gold_eez_mrgids(db) -> set[int]:
    """mrgids extracted productively on a prod Formalities run.

    Productive = poe_count > 0 and at least one official source.
    v1 and canaries / smokes do not count: an EEZ only
    present on the published map (e.g. Mayotte without a run) is not pre-Gold.
    Read zone cards only — not a port scan.
    """
    global _eez_pre_gold_cache
    now = time.time()
    if _eez_pre_gold_cache and now - _eez_pre_gold_cache[0] < _EEZ_CACHE_TTL_S:
        return set(_eez_pre_gold_cache[1])

    async with _eez_lock():
        now = time.time()
        if _eez_pre_gold_cache and now - _eez_pre_gold_cache[0] < _EEZ_CACHE_TTL_S:
            return set(_eez_pre_gold_cache[1])

        hits: set[int] = set()
        prod_runs = await production_poe_runs(db)
        rids = [_sid(r.get("_id")) for r in prod_runs if _sid(r.get("_id"))]
        if rids:
            try:
                zones = await db.poe_run_zones.find(
                    {"run_id": {"$in": rids}},
                    {"mrgid": 1, "run_id": 1, "poe_count": 1,
                     "sources": 1, "sources_official": 1},
                ).to_list(8000)
            except Exception:
                zones = []
            for z in zones:
                if eez_extraction_is_productive(z):
                    hits |= _mrgid_set([z])

        _eez_pre_gold_cache = (time.time(), frozenset(hits))
        return hits


async def recommended_eez_run_id(db) -> str | None:
    """Prod run that covers the most of the 11 productively, then the most recent.

    A France rerun (1 zone) must not hide the run of the 11 (FR+NZ+EG…).
    """
    prod = await production_poe_runs(db)
    by_id = {_sid(d.get("_id")): d for d in prod if _sid(d.get("_id"))}
    if not by_id:
        return None
    try:
        zones = await db.poe_run_zones.find(
            {
                "run_id": {"$in": list(by_id)},
                "mrgid": {"$in": list(STABLE_REVIEW_MRGIDS)},
            },
            {"mrgid": 1, "run_id": 1, "poe_count": 1,
             "sources": 1, "sources_official": 1},
        ).to_list(400)
    except Exception:
        return None
    scores: dict[str, int] = {}
    for z in zones:
        if not eez_extraction_is_productive(z):
            continue
        rid = _sid(z.get("run_id"))
        if rid in by_id:
            scores[rid] = scores.get(rid, 0) + 1
    best: str | None = None
    best_score = 0
    best_at = ""
    for rid, n in scores.items():
        created = str((by_id.get(rid) or {}).get("created_at") or "")
        if best is None or n > best_score or (n == best_score and created > best_at):
            best = rid
            best_score = n
            best_at = created
    return best


async def best_productive_stable_zones(db) -> dict[int, dict]:
    """Best productive card of each of the 11 polygons (max ports, then recent run)."""
    prod = await production_poe_runs(db)
    by_id = {_sid(d.get("_id")): d for d in prod if _sid(d.get("_id"))}
    if not by_id:
        return {}
    try:
        zones = await db.poe_run_zones.find(
            {
                "run_id": {"$in": list(by_id)},
                "mrgid": {"$in": list(STABLE_REVIEW_MRGIDS)},
            },
            {"mrgid": 1, "run_id": 1, "name": 1, "geoname": 1, "sovereign": 1,
             "iso2": 1, "sov_iso2": 1, "pol_type": 1, "poe_count": 1,
             "status": 1, "sources": 1, "sources_official": 1},
        ).to_list(400)
    except Exception:
        return {}
    best: dict[int, dict] = {}
    best_key: dict[int, tuple] = {}
    for z in zones:
        if not eez_extraction_is_productive(z):
            continue
        try:
            mid = int(z.get("mrgid") or 0)
        except (TypeError, ValueError):
            continue
        if not mid:
            continue
        rid = _sid(z.get("run_id"))
        try:
            n = int(z.get("poe_count") or 0)
        except (TypeError, ValueError):
            n = 0
        created = str((by_id.get(rid) or {}).get("created_at") or "")
        key = (n, created)
        if mid not in best or key > best_key[mid]:
            best[mid] = z
            best_key[mid] = key
    return best


async def overrides_map(db, kind: str) -> dict[str, dict]:
    try:
        docs = await db.review_gold.find({"kind": kind}).to_list(50000)
    except Exception:
        return {}
    return {_sid(d.get("entity_id")): d for d in docs}


async def get_override(db, kind: str, entity_id: str) -> dict | None:
    try:
        return await db.review_gold.find_one({"_id": gold_key(kind, entity_id)})
    except Exception:
        return None


async def is_pre_gold_entity(db, kind: str, entity_id: str, doc: dict | None = None) -> bool:
    if kind == "project":
        return is_pre_gold_project(doc)
    if kind == "marina":
        return is_pre_gold_marina(doc)
    if kind == "capitainerie":
        return is_pre_gold_capitainerie(doc)
    if kind == "amp":
        return is_pre_gold_amp(doc)
    if kind == "eez":
        try:
            mid = int(entity_id)
        except (TypeError, ValueError):
            return False
        return mid in await pre_gold_eez_mrgids(db)
    return False


async def visible_eez_mrgids(db) -> set[int]:
    """mrgids of the certified Formalities run (“Show review” layer)."""
    return set(await published_snapshots(db))


async def published_snapshots(db) -> dict[int, dict]:
    overrides = await overrides_map(db, "eez")
    out: dict[int, dict] = {}
    for eid, ov in overrides.items():
        if not eez_is_published(ov):
            continue
        try:
            mid = int(eid)
        except (TypeError, ValueError):
            continue
        snap = ov.get("snapshot")
        if isinstance(snap, dict):
            out[mid] = snap
    return out


async def visible_poe_port_docs(db, mrgid: int | None = None,
                                  country: str | None = None) -> list[dict]:
    """Ports of the certified Formalities run — never live `poe_ports`."""
    snaps = await published_snapshots(db)
    if mrgid is not None:
        try:
            mid = int(mrgid)
        except (TypeError, ValueError):
            return []
        if mid not in snaps:
            return []
        snaps = {mid: snaps[mid]}
    iso = (country or "").strip().upper()
    out: list[dict] = []
    for mid, snap in snaps.items():
        for doc in snapshot_port_docs(mid, snap):
            if iso and str(doc.get("country_iso2") or "").upper() != iso:
                continue
            out.append(doc)
    return out


async def filter_visible(db, kind: str, docs: list[dict], id_fn) -> list[dict]:
    """Certified run only: explicit Gold, never a silent pre-Gold."""
    overrides = await overrides_map(db, kind)
    out: list[dict] = []
    for d in docs:
        eid = _sid(id_fn(d))
        if not eid:
            continue
        ov = overrides.get(eid)
        if kind == "eez":
            try:
                mid = int(d.get("mrgid") or eid)
            except (TypeError, ValueError):
                continue
            ov = overrides.get(str(mid)) or ov
            if eez_on_map(ov):
                out.append(d)
            continue
        if gold_is_on(ov):
            out.append(apply_snapshot_to_doc(kind, d, ov.get("snapshot")))
    return out


async def _load_fiche_for_gold(db, kind: str, eid: str, run_id: str | None,
                               comment: str, choices: dict) -> tuple:
    from app.services.review_queue import get_fiche
    packed = await get_fiche(db, kind, run_id, eid)
    if not packed:
        return None, comment, choices
    fiche = packed.get("fiche")
    if not comment:
        comment = packed.get("comment") or ""
    ch = choices if choices else (packed.get("choices") or empty_choices())
    return fiche, comment, ch


async def toggle_gold(db, kind: str, entity_id: str, *,
                      run_id: str | None = None,
                      snapshot: dict | None = None,
                      fiche: dict | None = None,
                      comment: str = "",
                      choices: dict | None = None) -> dict:
    if kind not in GOLD_KINDS:
        raise ValueError("kind must be project|eez|marina|capitainerie|amp")
    eid = _sid(entity_id)
    if not eid:
        raise ValueError("id required")
    if kind == "eez":
        return await _toggle_eez_gold(
            db, eid, run_id=run_id, fiche=fiche, comment=comment, choices=choices)

    override = await get_override(db, kind, eid)
    currently_on = gold_is_on(override)
    cid = gold_key(kind, eid)
    ch = choices if choices is not None else await get_choices(db, kind, eid)
    if currently_on:
        payload = {
            "_id": cid, "kind": kind, "entity_id": eid,
            "on": False, "run_id": run_id, "updated_at": now_iso(),
        }
        if override.get("snapshot"):
            payload["snapshot"] = override["snapshot"]
        await db.review_gold.update_one({"_id": cid}, {"$set": payload}, upsert=True)
        pressed = False
        snap = payload.get("snapshot")
        pre = await is_pre_gold_entity(db, kind, eid, fiche)
    else:
        if fiche is None:
            fiche, comment, ch = await _load_fiche_for_gold(
                db, kind, eid, run_id, comment, ch)
        pre = await is_pre_gold_entity(db, kind, eid, fiche)
        if fiche is None or not gold_ready(fiche, ch, kind=kind, comment=comment):
            raise GoldNotReady(gold_incomplete_message(kind))
        snap = snapshot
        if snap is None:
            snap = build_gold_snapshot(fiche, ch, comment, kind=kind)
        payload = {
            "_id": cid, "kind": kind, "entity_id": eid,
            "on": True, "run_id": run_id, "updated_at": now_iso(),
        }
        if snap:
            payload["snapshot"] = snap
        await db.review_gold.update_one({"_id": cid}, {"$set": payload}, upsert=True)
        pressed = True
        try:
            from app.services.review_lessons import record_gold_lesson
            await record_gold_lesson(db, fiche, ch, comment, kind=kind)
        except Exception:
            pass
    return {
        "kind": kind,
        "id": eid,
        "gold_on": pressed,
        "pre_gold": pre,
        "choices": ch,
        "gold_ready": gold_ready(fiche, ch, kind=kind, comment=comment) if fiche is not None else True,
        "snapshot": snap if pressed else None,
        "wrote_projects": False,
        "wrote_poe_ports": False,
        "wrote_marinas": False,
        "wrote_capitaineries": False,
        "wrote_amp_sites": False,
    }


async def _toggle_eez_gold(db, eid: str, *, run_id: str | None,
                           fiche: dict | None, comment: str,
                           choices: dict | None) -> dict:
    pre = await is_pre_gold_entity(db, "eez", eid, None)
    override = await get_override(db, "eez", eid)
    cid = gold_key("eez", eid)
    if eez_is_published(override):
        payload = {
            "_id": cid, "kind": "eez", "entity_id": eid,
            "on": False, "run_id": run_id, "updated_at": now_iso(),
        }
        if override.get("snapshot"):
            payload["snapshot"] = override["snapshot"]
        await db.review_gold.update_one({"_id": cid}, {"$set": payload}, upsert=True)
        ch = choices if choices is not None else await get_choices(db, "eez", eid)
        return {
            "kind": "eez",
            "id": eid,
            "gold_on": False,
            "pre_gold": pre,
            "choices": ch,
            "gold_ready": gold_ready(fiche, ch, kind="eez", comment=comment) if fiche is not None else True,
            "wrote_projects": False,
            "wrote_poe_ports": False,
            "wrote_marinas": False,
            "wrote_capitaineries": False,
            "wrote_amp_sites": False,
        }

    if fiche is None:
        from app.services.poe_zone_fiche import build_zone_fiche
        try:
            mid = int(eid)
        except (TypeError, ValueError) as e:
            raise ValueError("id required") from e
        fiche = await build_zone_fiche(db, mid, union=True)
    if not fiche:
        raise ValueError("fiche not found")
    ch = choices if choices is not None else await get_choices(db, "eez", eid)
    if not gold_ready(fiche, ch, kind="eez", comment=comment):
        raise GoldNotReady(gold_incomplete_message("eez"))
    ch = finalize_choices(fiche, ch)
    await save_choices_doc(db, "eez", eid, ch)
    snap = build_gold_snapshot(fiche, ch, comment)
    try:
        from app.services.poe_pipeline import remember_review_pins
        remember_review_pins(
            snap.get("mrgid") or eid,
            [rec.get("url") for rec in (snap.get("sources_td") or []) if rec.get("url")],
            [rec.get("url") for rec in (snap.get("dropped_td") or []) if rec.get("url")],
        )
    except Exception:
        pass
    try:
        from app.services.review_lessons import record_gold_lesson
        await record_gold_lesson(db, fiche, ch, comment)
    except Exception:
        pass
    payload = {
        "_id": cid, "kind": "eez", "entity_id": eid,
        "on": True, "run_id": run_id, "updated_at": now_iso(),
        "snapshot": snap,
    }
    await db.review_gold.update_one({"_id": cid}, {"$set": payload}, upsert=True)
    return {
        "kind": "eez",
        "id": eid,
        "gold_on": True,
        "pre_gold": pre,
        "choices": ch,
        "gold_ready": True,
        "wrote_projects": False,
        "wrote_poe_ports": False,
        "wrote_marinas": False,
        "wrote_capitaineries": False,
        "wrote_amp_sites": False,
    }


async def ensure_gold_indexes(db) -> None:
    try:
        await db.review_gold.create_index("kind")
        await db.review_gold.create_index("entity_id")
    except Exception:
        pass
