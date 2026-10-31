"""
poe_seed_enrich — Geocode name_only seeds, judge the others.

Paged search via ``search_named`` (TinyFish, ``serp_filter``, DuckDuckGo if
no key), Fetch of every whitelisted hit (cap 10),
``ask_yes_no`` judge (``judge`` role: Pro → gpt-oss → Muse → Flash) if a
NIM key, else OpenRouter, then Claude Haiku → Sonnet last.
reuse_paid_sources=True: Fetch already-paid judge_sources, 0 Search.
TinyFish Agent only if Fetch returns bot_blocked (1 / seed, lite then
stealth, 2 concurrent, credit cap).

Never writes poe_ports: only poe_run_ports / poe_seed_ports.
Resume: skip already geocoded / already judged ports.
Dual read: catalog parser → sources_bu + remember_seed_urls;
yes/no judge only the residue (name absent from the list).
"""
from __future__ import annotations

import asyncio
import os
from collections import Counter, defaultdict
from functools import lru_cache
from urllib.parse import unquote, urlparse

from shapely.geometry import shape
from shapely.prepared import prep

from app.core.extract import (
    catalog_is_sufficient, complete_with_cascade, extract_structured_ports,
    geocode_query_name, is_geocodeable_name,
)
from app.core.geo import (
    INLAND_FAR_SCORE_KM,
    classify_poe_point, geocode_port_dual, inland_exception_flags,
    select_geocode_candidate,
)
from app.services.poe_gps_registry import accepted_by_key
from app.core.judge import as_bool, as_confidence
from app.core.search import search_named
from app.core.tinyfish import (
    FETCH_URL_CAP, SEARCH_PAGE_CAP, tf_api_key, tf_fetch, tf_poe_agent,
)
from app.services.poe_pipeline import (
    OFFICIAL_TOKENS, build_whitelist, domain_of, list_url_bonus,
    load_exceptions, now_iso, remember_seed_urls, search_polygon_name,
    url_allowed, urls_with_catalog,
)
from app.services.poe_seeds import (
    SEARCH_EXCLUDE_DOMAINS, listing_is_poe, seed_search_query,
    url_is_excluded_search, verdict_for_seed, _match_seed,
)

JUDGE_SYSTEM = (
    "Tu es un juge Ports d'Entrée pour la plaisance. Réponds uniquement en JSON strict : "
    '{"is_poe": true, "confidence": 80, "reason": "", "official_name": null, '
    '"kind": "pleasure"} '
    "confidence = entier 0-100 (pas une fraction 0-1). "
    'kind = pleasure | mixed | cargo | other | unknown. '
    "is_poe=true seulement si une source officielle désigne CE lieu comme "
    "port d'entrée / clearance / puerto habilitado / designated port "
    "POUR la plaisance (yacht, recreational, pleasure craft) OU mixte "
    "(commerce ET plaisance explicites). "
    "false si cargo-only, terminal conteneur, industriel, aéroport, ville, autre pays. "
    "Une marina n'est PAS false automatique : si clearance officielle à CETTE marina, "
    "is_poe=true et kind=pleasure. "
    "null si extraits insuffisants, ou port désigné sans trafic lisible "
    "(plaisance vs cargo). "
    "Juge uniquement le lieu nommé. Ne liste aucun autre port. "
    "Ignore listing communautaire et forums."
)

_KIND_CANON = {
    "pleasure": "pleasure",
    "yacht": "pleasure",
    "yachts": "pleasure",
    "recreational": "pleasure",
    "pleasure_craft": "pleasure",
    "pleasurecraft": "pleasure",
    "plaisance": "pleasure",
    "mixed": "mixed",
    "mixte": "mixed",
    "both": "mixed",
    "cargo": "cargo",
    "commercial": "cargo",
    "freight": "cargo",
    "industrial": "cargo",
    "commerce": "cargo",
    "container": "cargo",
    "cargo_only": "cargo",
    "other": "other",
    "unknown": "unknown",
}


def normalize_judge_kind(raw) -> str:
    """Canonise kind LLM → pleasure | mixed | cargo | other | unknown."""
    if raw is None:
        return "unknown"
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return _KIND_CANON.get(key, "unknown")


DEFAULT_VERIFY_RUN = "20260906-071347-6a9509"
VERIFY_ORDER = ("name_only", "unverified", "probable")
DEFAULT_ENRICH_LIMIT = 200
WHITELIST_DOMAIN_CAP = 15
CATALOG_FETCH_CHARS = 20000
MINE_TASK_ID = "seed-mine"
DEFAULT_MINE_FETCH_CAP = 200
# Path tokens: a port list, not some annex / gazette.
_MINE_PATH_TOKENS = (
    "port-of-entry", "ports-of-entry", "ports-entree", "portos-de-entrada",
    "puertos-habilit", "habilitados", "designated-port", "designated_ports",
    "puertos-y-terminales", "first-arrival", "places-of-first",
    "location-codes-for-ports", "customs-offices-list",
    "border-control-post", "ports-de-plaisance", "points-de-passage",
    "puertos-de-entrada", "list-of-ports", "liste-des-ports",
    "ports-habilit", "pleasure-craft", "small-craft",
    "appendix-b", "puertosymarinamercante", "marina-mercante",
    "ports-using-the-goods", "designated-land-sea",
    "puertos-habilitados", "authorized-ports", "authorised-ports",
)


def parse_judge(data: dict | None) -> dict:
    data = data if isinstance(data, dict) else {}
    flag = as_bool(data.get("is_poe"))
    kind = normalize_judge_kind(data.get("kind"))
    if flag is True:
        status = "accepted"
    elif flag is False:
        status = "rejected"
    else:
        status = "inconclusive"
    # Deterministic net: cargo-only is never a pleasure-craft PoE.
    if kind == "cargo" and status == "accepted":
        status = "rejected"
    conf = as_confidence(data.get("confidence"))
    return {
        "judge_status": status,
        "judge_confidence": conf,
        "judge_reason": str(data.get("reason") or "")[:240],
        "official_name": data.get("official_name"),
        "judge_kind": kind,
    }


def apply_judge_verdict(seed: dict, judge: dict) -> str:
    """Update verify_verdict after a judgment. Does not touch confirmed."""
    current = seed.get("verify_verdict") or verdict_for_seed(seed)
    if current == "confirmed":
        return "confirmed"
    status = judge.get("judge_status")
    has_listing = listing_is_poe(seed)
    has_coords = bool(seed.get("has_coords") or (
        seed.get("lat") is not None and seed.get("lon") is not None))
    if status == "catalog":
        # Beam D: named by the list. Not a pleasure-craft yes (P comes after).
        if current == "name_only" and has_coords:
            return "unverified"
        return current
    if status == "accepted" and has_listing and has_coords:
        from app.services.poe_rules import noonsite_blocks_confirmation
        # Listing signal may still raise confidence. A Noonsite URL cannot confirm.
        if noonsite_blocks_confirmation(seed):
            return "probable"
        return "confirmed"
    if status == "accepted":
        return "probable"
    if status == "rejected" and current == "name_only":
        return "name_only"
    if status == "rejected":
        return "unverified"
    if current == "name_only" and has_coords:
        return "unverified"
    return current


def should_escalate_sonnet(judge: dict | None, doc: dict) -> bool:
    """Sonnet if Haiku is inconclusive, or if the seed comes from the listing."""
    status = (judge or {}).get("judge_status")
    if status == "inconclusive" or not judge:
        return True
    return "listing" in set(doc.get("seed_sources") or [])


def select_fetch_urls(hits: list[dict], whitelist: list[str],
                      cap: int = FETCH_URL_CAP) -> list[str]:
    """All whitelisted hits, in Search order, soft cap (~10)."""
    out, seen = [], set()
    for h in hits or []:
        u = (h.get("url") or "").strip()
        if not u.startswith("http") or u in seen:
            continue
        if url_is_excluded_search(u):
            continue
        if whitelist and not url_allowed(u, whitelist):
            continue
        if not whitelist:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= cap:
            break
    return out


def _legacy_geocode_cands(dual: dict) -> list[dict]:
    cands = []
    for source in ("nominatim", "geonames"):
        xy = (dual or {}).get(source)
        if not xy:
            continue
        meta = (dual or {}).get(f"{source}_meta") or {}
        cands.append({
            "source": source, "lat": float(xy[0]), "lon": float(xy[1]),
            "label": "",
            "osm_class": meta.get("osm_class") or "",
            "osm_type": meta.get("osm_type") or "",
            "geonames_fcode": meta.get("geonames_fcode") or "",
        })
    return cands


def pick_geocode(dual: dict, port: dict, zone: dict, geom, prepared) -> dict:
    """Score homonyms (EEZ, parentheses, listing), then spatial filter."""
    dual = dual or {}
    pick = dual.get("pick") or {}
    raw_cands = list(dual.get("candidates") or []) or _legacy_geocode_cands(dual)
    sel = pick if pick.get("status") else None
    if raw_cands and (sel is None or not sel.get("ranked")):
        sel = select_geocode_candidate(raw_cands, port, zone, geom, prepared)
    if sel and sel.get("status") == "ambiguous":
        ranked = sel.get("ranked") or []
        alts = ranked[:2]
        return {
            "lat": None, "lon": None, "geocode_source": None,
            "validated": False, "spatial_kind": "ambiguous",
            "geocode_status": "ambiguous",
            "geocode_arbitration": "ambiguous",
            "geocode_agree": dual.get("agree"),
            "has_coords": False,
            "geocode_alt": [
                {"lat": c.get("lat"), "lon": c.get("lon"),
                 "source": c.get("source"), "score": c.get("score"),
                 "label": (c.get("label") or "")[:160]}
                for c in alts
            ],
        }
    chosen_scored = (sel or {}).get("chosen") if sel else None
    cands = []
    if chosen_scored and chosen_scored.get("lat") is not None:
        sources = [chosen_scored]
        for source in ("nominatim", "geonames"):
            xy = dual.get(source)
            if not xy:
                continue
            if (abs(float(xy[0]) - float(chosen_scored["lat"])) < 1e-4
                    and abs(float(xy[1]) - float(chosen_scored["lon"])) < 1e-4):
                continue
            sources.append({"source": source, "lat": xy[0], "lon": xy[1]})
        # One point to validate: the score winner. The others stay
        # available if the winner is spatially rejected.
        ordered = [chosen_scored] + [
            c for c in raw_cands
            if not (abs(float(c["lat"]) - float(chosen_scored["lat"])) < 1e-4
                    and abs(float(c["lon"]) - float(chosen_scored["lon"])) < 1e-4)
        ]
    else:
        ordered = raw_cands
    for item in ordered:
        source = item.get("source") or "nominatim"
        meta = dual.get(f"{source}_meta") or {
            "osm_class": item.get("osm_class") or "",
            "osm_type": item.get("osm_type") or "",
            "geonames_fcode": item.get("geonames_fcode") or "",
        }
        inland = inland_exception_flags(port, zone, meta, official_list=True)
        lat, lon = float(item["lat"]), float(item["lon"])
        row = {"source": source, "lat": lat, "lon": lon,
               "validated": False, "dist_km": None, "kind": "unknown"}
        if geom is not None:
            row.update(classify_poe_point(lat, lon, geom, prepared, inland=inland))
        else:
            row["validated"] = True
            row["kind"] = "unknown"
        cands.append(row)
    if not cands:
        return {"lat": None, "lon": None, "geocode_source": None,
                "validated": False, "spatial_kind": "miss",
                "geocode_agree": dual.get("agree")}
    if dual.get("agree"):
        chosen, arb = cands[0], "agree"
    elif len(cands) == 1:
        chosen, arb = cands[0], "single"
    else:
        valid = [c for c in cands if c.get("validated")]
        chosen, arb = (valid[0] if valid else cands[0]), "eez"
    if not chosen.get("validated") and chosen.get("kind") not in (None, "unknown"):
        other = next((c for c in cands if c is not chosen and c.get("validated")), None)
        if other:
            chosen, arb = other, "spatial_switch"
        else:
            dist = chosen.get("dist_km")
            # Accords Nominatim/GeoNames juste hors sliver 2,2 km (Cassis, Geelong).
            if ((dual or {}).get("agree") and dist is not None
                    and float(dist) <= 4.0):
                return {
                    "lat": chosen["lat"], "lon": chosen["lon"],
                    "geocode_source": chosen["source"],
                    "validated": True, "spatial_kind": chosen.get("kind"),
                    "distance_km": dist,
                    "geocode_agree": True,
                    "geocode_arbitration": "agree_near_eez",
                    "has_coords": True,
                }
            return {
                "lat": None, "lon": None, "geocode_source": None,
                "validated": False, "spatial_kind": chosen.get("kind"),
                "distance_km": dist,
                "geocode_agree": (dual or {}).get("agree"),
                "geocode_arbitration": "spatial_rejected",
                "geocode_rejected_lat": chosen["lat"],
                "geocode_rejected_lon": chosen["lon"],
                "geocode_rejected_source": chosen["source"],
                "has_coords": False,
            }
    return {
        "lat": chosen["lat"], "lon": chosen["lon"],
        "geocode_source": chosen["source"],
        "validated": bool(chosen.get("validated")),
        "spatial_kind": chosen.get("kind"),
        "distance_km": chosen.get("dist_km"),
        "geocode_agree": (dual or {}).get("agree"),
        "geocode_agreement_km": (dual or {}).get("agreement_km"),
        "geocode_arbitration": arb,
        "has_coords": True,
    }


GPS_AUDIT_KEEP = {"ok", "corrected", "dismissed"}
INLAND_FAR_KINDS = {"inland_river", "inland", "other_water"}


def _needs_geocode(doc: dict) -> bool:
    """name_only without GPS, or inland_far / ambiguous. Not confirmed-ok."""
    verdict = doc.get("verify_verdict") or ""
    audit = doc.get("gps_audit_status") or ""
    if verdict == "confirmed" and audit in GPS_AUDIT_KEEP:
        return False
    if (doc.get("geocode_status") or doc.get("geocode_arbitration")) == "ambiguous":
        return True
    kind = doc.get("spatial_kind") or doc.get("spatial_class") or ""
    dist = doc.get("dist_km_to_eez_poly")
    if dist is None:
        dist = doc.get("distance_km")
    try:
        dist_f = float(dist) if dist is not None else None
    except (TypeError, ValueError):
        dist_f = None
    if kind in INLAND_FAR_KINDS and dist_f is not None and dist_f > INLAND_FAR_SCORE_KM:
        return True
    if audit == "flagged":
        reasons = doc.get("gps_audit_reasons") or []
        if any(r in reasons for r in (
                "inland_far", "listing_group_outlier",
                "homonym_paren_mismatch", "nominatim_inland_listing_only")):
            return True
    if doc.get("geocoded_at"):
        return False
    if doc.get("lat") is not None and doc.get("lon") is not None:
        return False
    return verdict == "name_only"


def _needs_judge(doc: dict, verdicts: tuple[str, ...]) -> bool:
    if doc.get("judge_status"):
        return False
    return (doc.get("verify_verdict") or "") in verdicts


async def _zone_cache(db, mrgids: set[int]) -> dict[int, dict]:
    if not mrgids:
        return {}
    docs = await db.eez_zones.find(
        {"mrgid": {"$in": list(mrgids)}},
        {"mrgid": 1, "name": 1, "geoname": 1, "iso2": 1, "sov_iso2": 1,
         "sovereign": 1, "geometry": 1, "sources_bu": 1, "catalog_bu": 1},
    ).to_list(500)
    out = {}
    for z in docs:
        mid = z.get("mrgid")
        if mid is None:
            continue
        geom = prepared = None
        try:
            if z.get("geometry"):
                geom = shape(z["geometry"])
                prepared = prep(geom)
        except Exception:
            geom = prepared = None
        rec = {**z, "_geom": geom, "_prep": prepared}
        rec["_bu_catalog"] = catalog_cache_from_zone(rec)
        out[int(mid)] = rec
    return out


@lru_cache(maxsize=1)
def _listing_group_by_key() -> dict[str, str]:
    from app.services.listing_ref import project_listing
    out: dict[str, str] = {}
    try:
        for p in project_listing().get("ports") or []:
            k = p.get("dedup_key")
            g = p.get("group")
            if k and g:
                out[str(k)] = str(g)
    except Exception:
        return out
    return out


def attach_geocode_context(ports: list[dict]) -> None:
    """listing_group + coastal pairs (filter, never a GPS to copy)."""
    groups = _listing_group_by_key()
    for p in ports:
        if not p.get("listing_group"):
            g = groups.get(str(p.get("dedup_key") or ""))
            if g:
                p["listing_group"] = g
    peers_by: dict[tuple, list] = defaultdict(list)
    for p in ports:
        try:
            lat, lon = p.get("lat"), p.get("lon")
            if lat is None or lon is None:
                continue
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        kind = p.get("spatial_kind") or p.get("spatial_class") or ""
        audit = p.get("gps_audit_status") or ""
        inland = kind in INLAND_FAR_KINDS
        if inland and audit not in GPS_AUDIT_KEEP:
            continue
        grp = p.get("listing_group")
        if not grp or p.get("mrgid") is None:
            continue
        try:
            mid = int(p["mrgid"])
        except (TypeError, ValueError):
            continue
        peers_by[(mid, grp)].append(
            {"lat": lat, "lon": lon, "name": p.get("name")})
    for p in ports:
        grp = p.get("listing_group")
        if not grp or p.get("mrgid") is None:
            continue
        try:
            mid = int(p["mrgid"])
        except (TypeError, ValueError):
            continue
        others = [
            x for x in peers_by.get((mid, grp), [])
            if x.get("name") != p.get("name")
        ]
        p["geocode_peers"] = others[:24]


def _registry_geocode(doc: dict, zone: dict, log) -> dict | None:
    """GPS already decided (git registry): no Nominatim, no Claude."""
    hit = accepted_by_key(str(doc.get("dedup_key") or ""))
    if not hit:
        return None
    lat, lon = float(hit["lat"]), float(hit["lon"])
    src = hit.get("geocode_source") or "manual_audit"
    out = {
        "lat": lat, "lon": lon, "has_coords": True,
        "geocode_source": src,
        "geocode_arbitration": "gps_registry",
        "geocoded_at": now_iso(),
        "geocode_query": geocode_query_name(doc.get("name") or ""),
    }
    geom = zone.get("_geom") if zone else None
    prepared = zone.get("_prep") if zone else None
    if geom is not None:
        inland = inland_exception_flags(doc, zone or {}, {}, official_list=True)
        cls = classify_poe_point(lat, lon, geom, prepared, inland=inland)
        out["spatial_kind"] = cls.get("kind")
        out["validated"] = bool(cls.get("validated"))
        out["distance_km"] = cls.get("dist_km")
    if hit.get("action") == "keep":
        out["geocode_kept_previous"] = True
        log(f"géocode registre keep: {hit.get('dedup_key')}")
    else:
        log(f"géocode registre: {hit.get('dedup_key')}")
    return out


async def _llm_fill_mute_port(picked: dict, port: dict, zone: dict,
                              settings: dict | None, log) -> dict:
    """Silent directories → guessed GPS, then VLIZ polygon. Not the Project haven."""
    from app.core.llm import llm_geocode_port
    xy = await llm_geocode_port(port, zone, settings=settings, log=log)
    if not xy:
        return picked
    geom, prepared = zone.get("_geom"), zone.get("_prep")
    inland = inland_exception_flags(port, zone, {}, official_list=True)
    kind, validated, dist = "unknown", geom is None, None
    if geom is not None:
        cls = classify_poe_point(xy[0], xy[1], geom, prepared, inland=inland)
        kind = cls.get("kind") or "unknown"
        validated = bool(cls.get("validated"))
        dist = cls.get("dist_km")
    if validated or kind in (None, "unknown"):
        picked.update({
            "lat": xy[0], "lon": xy[1],
            "geocode_source": "llm",
            "validated": bool(validated),
            "spatial_kind": kind,
            "distance_km": dist,
            "geocode_arbitration": "llm",
            "has_coords": True,
        })
        return picked
    picked.update({
        "geocode_arbitration": "llm_spatial_rejected",
        "geocode_rejected_lat": xy[0],
        "geocode_rejected_lon": xy[1],
        "geocode_rejected_source": "llm",
        "spatial_kind": kind,
        "distance_km": dist,
        "has_coords": False,
    })
    return picked


async def geocode_one(doc: dict, zone: dict, log, settings: dict | None = None) -> dict:
    raw = doc.get("name") or ""
    name = geocode_query_name(raw)
    if name != raw:
        log(f"géocode alias: {raw} → {name}")
    registered = _registry_geocode(doc, zone, log)
    if registered is not None:
        return registered
    if not is_geocodeable_name(name) and not is_geocodeable_name(raw):
        log(f"géocode sauté (non toponyme): {raw}")
        return {"geocoded_at": now_iso(), "spatial_kind": "not_geocodeable",
                "geocode_query": name}
    if not doc.get("listing_group"):
        g = _listing_group_by_key().get(str(doc.get("dedup_key") or ""))
        if g:
            doc["listing_group"] = g
    port = {
        "name": name,
        "city": doc.get("city"),
        "listing_role": "poe" if "listing" in (doc.get("seed_sources") or []) else None,
        "listing_name": doc.get("listing_name") or raw,
        "listing_group": doc.get("listing_group"),
        "geocode_peers": list(doc.get("geocode_peers") or []),
        "dedup_key": doc.get("dedup_key"),
    }
    dual = await geocode_port_dual(port, zone, log)
    picked = pick_geocode(dual, port, zone, zone.get("_geom"), zone.get("_prep"))
    picked["geocoded_at"] = now_iso()
    picked["geocode_query"] = name
    mute = not dual.get("nominatim") and not dual.get("geonames")
    if (not picked.get("has_coords") and mute
            and picked.get("geocode_arbitration") not in (
                "ambiguous", "spatial_rejected")):
        picked = await _llm_fill_mute_port(picked, port, zone, settings, log)
    if (not picked.get("has_coords") and doc.get("lat") is not None
            and doc.get("lon") is not None):
        # Never overwrite an existing GPS with a miss / ambiguous.
        picked.pop("lat", None)
        picked.pop("lon", None)
        picked["has_coords"] = True
        picked["geocode_kept_previous"] = True
    return picked


def _judge_prompt(doc: dict, zone: dict, context: str) -> str:
    """Name + zone + excerpts. No listing/OSM/WPI tokens (that would bias)."""
    name = (doc.get("name") or "").strip()
    return (
        f"Candidat : {name}\n"
        f"Zone VLIZ : {search_polygon_name(zone) or zone.get('name') or zone.get('geoname')} "
        f"(mrgid={zone.get('mrgid')}, {zone.get('iso2') or ''})\n"
        f"Juge uniquement CE lieu à partir des extraits. "
        f"Ne liste aucun autre port.\n\n"
        f"EXTRAITS:\n{(context or '')[:8000]}"
    )


async def _judge_llm(doc: dict, zone: dict, context: str, settings: dict, log) -> dict:
    from app.core import claude
    from app.core.judge import YesNo, ask_yes_no

    prompt = _judge_prompt(doc, zone, context)

    def hop_if(yes: YesNo) -> bool:
        return should_escalate_sonnet(parse_judge(yes.raw), doc)

    yes = await ask_yes_no(
        JUDGE_SYSTEM, prompt, settings=settings, log=log,
        role="judge", max_tokens=800,
        openrouter_max_tokens=300, claude_max_tokens=300,
        hop_if=hop_if,
        claude_models=(claude.CLAUDE_HAIKU_MODEL, claude.CLAUDE_SONNET_MODEL),
        on_empty="inconclusive")
    out = parse_judge(yes.raw)
    if yes.engine:
        out["judge_engine"] = yes.engine
    return out


def drop_excluded_hits(hits: list[dict]) -> list[dict]:
    return [h for h in (hits or []) if not url_is_excluded_search(h.get("url") or "")]


async def _search_hits(doc: dict, zone: dict, whitelist: list[str], key: str, log) -> list[dict]:
    iso = (zone.get("iso2") or "").lower() or None
    query = (doc.get("search_query") or "").strip() or seed_search_query({
        **doc,
        "zone_name": doc.get("zone_name") or zone.get("name") or zone.get("geoname"),
    })

    def enough(hs):
        kept = drop_excluded_hits(hs)
        return sum(1 for h in kept if url_allowed(h.get("url") or "", whitelist)) >= FETCH_URL_CAP

    domains = whitelist[:WHITELIST_DOMAIN_CAP] or None
    hits = await search_named(
        query, key=key or "", location=iso, language="en",
        include_domains=domains, exclude_domains=SEARCH_EXCLUDE_DOMAINS,
        max_pages=SEARCH_PAGE_CAP, stop_when=enough, log=log)
    official_n = sum(
        1 for h in drop_excluded_hits(hits)
        if url_allowed(h.get("url") or "", whitelist))
    if official_n == 0:
        extra = await search_named(
            query, key=key or "", location=iso, language="en",
            exclude_domains=SEARCH_EXCLUDE_DOMAINS,
            max_pages=SEARCH_PAGE_CAP, stop_when=enough, log=log)
        seen = {h.get("url") for h in hits}
        for h in extra:
            if h.get("url") not in seen:
                hits.append(h)
    return drop_excluded_hits(hits)


def harvest_bu_catalog(fetched: dict, urls: list[str]) -> dict:
    """Catalog parser on already-fetched state pages. 0 crawl."""
    blocks = []
    for u in urls or []:
        rec = (fetched or {}).get(u) or {}
        if rec.get("blocked") or rec.get("error"):
            continue
        text = rec.get("text") or ""
        if not str(text).strip():
            continue
        blocks.append(f"[SOURCE: {u}]\n{text[:CATALOG_FETCH_CHARS].rstrip()}\n")
    raw = "\n\n".join(blocks)
    ports = extract_structured_ports(raw)
    named = [
        p for p in ports
        if p.get("extraction_engine") == "catalog"
        and "tournure légale" not in (p.get("note") or "")
        and (p.get("name") or "").strip()
    ]
    sufficient = catalog_is_sufficient(ports, raw) or len(named) >= 4
    productive = urls_with_catalog(blocks) if blocks else []
    if sufficient and not productive:
        productive = [
            u for u in (urls or [])
            if ((fetched or {}).get(u) or {}).get("text")
        ]
    return {
        "ports": ports,
        "urls": productive,
        "sufficient": sufficient,
        "raw": raw,
    }


def seed_on_catalog(doc: dict, ports: list[dict] | None) -> dict | None:
    """True if the seed name is already on the official list (dedup)."""
    if not ports:
        return None
    return _match_seed(
        {"name": doc.get("name"), "mrgid": doc.get("mrgid")},
        list(ports),
    )


def _named_on_zone_catalog(doc: dict, zone: dict | None) -> bool:
    if not zone:
        return False
    cache = zone.get("_bu_catalog") or catalog_cache_from_zone(zone)
    if not cache.get("sufficient"):
        return False
    return seed_on_catalog(doc, cache.get("ports")) is not None


def iter_judge_urls(doc: dict):
    for raw in doc.get("judge_sources") or []:
        if isinstance(raw, dict):
            raw = raw.get("url") or ""
        u = str(raw or "").strip()
        if u.startswith("http"):
            yield u


def paid_fetch_urls(doc: dict, cap: int = 4) -> list[str]:
    """Prefer already-paid list-like URLs (MPI, customs, gazette)."""
    urls = list(dict.fromkeys(iter_judge_urls(doc)))

    def _score(u: str) -> float:
        low = (u or "").lower()
        bonus = 0.0
        if "places-of-first-arrival" in low or "first-arrival" in low:
            bonus += 20
        if "mpi.govt.nz" in low or "douane.gov." in low or "customs.govt.nz" in low:
            bonus += 8
        if "gazette.govt.nz/notice/" in low:
            bonus += 6
        if low.endswith(".pdf"):
            bonus -= 2
        return mine_url_score(u) + bonus

    urls.sort(key=_score, reverse=True)
    return urls[: max(1, cap)] if urls else []


def mine_token_hits(url: str) -> int:
    blob = unquote((url or "").lower())
    path = unquote((urlparse(url or "").path or "") + "?" + (urlparse(url or "").query or "")).lower()
    return sum(1 for tok in _MINE_PATH_TOKENS if tok in path or tok in blob)


def mine_url_score(url: str, n: int = 1) -> float:
    return mine_token_hits(url) * 1.0 + min(max(int(n or 0), 0), 20) * 0.15 + list_url_bonus(url)


def is_mine_candidate(url: str, n: int = 1) -> bool:
    """Already-paid official URL that looks like a list — not Search."""
    if not (url or "").startswith("http"):
        return False
    if url_is_excluded_search(url):
        return False
    if not OFFICIAL_TOKENS.search(domain_of(url) or url):
        return False
    hits = mine_token_hits(url)
    bonus = list_url_bonus(url)
    if hits >= 1:
        return True
    if n >= 10 and bonus >= 0:
        return True
    if n >= 5 and bonus >= 0.3:
        return True
    return False


def collect_paid_source_urls(docs: list[dict]) -> list[dict]:
    """Dedup already-paid judge_sources (URL → n, mrgids)."""
    by: dict[str, dict] = {}
    for doc in docs or []:
        mid = doc.get("mrgid")
        try:
            mid = int(mid) if mid is not None else None
        except (TypeError, ValueError):
            mid = None
        for u in iter_judge_urls(doc):
            rec = by.setdefault(u, {"url": u, "n": 0, "mrgids": set()})
            rec["n"] += 1
            if mid is not None:
                rec["mrgids"].add(mid)
    out = []
    for rec in by.values():
        rec["score"] = mine_url_score(rec["url"], rec["n"])
        rec["mrgids"] = sorted(rec["mrgids"])
        out.append(rec)
    out.sort(key=lambda r: (r["score"], r["n"]), reverse=True)
    return out


def select_mine_urls(items: list[dict], cap: int = DEFAULT_MINE_FETCH_CAP) -> list[dict]:
    picked = [it for it in items if is_mine_candidate(it.get("url") or "", it.get("n") or 0)]
    if cap and cap > 0:
        picked = picked[:cap]
    return picked


def merge_catalog_cache(cache: dict, harvest: dict) -> dict:
    """Merge a harvest into the EEZ cache (same batch, 0 re-fetch)."""
    if not harvest or not harvest.get("sufficient"):
        return cache
    cache["sufficient"] = True
    seen = {(p.get("name") or "").casefold() for p in (cache.get("ports") or [])}
    ports = list(cache.get("ports") or [])
    for p in harvest.get("ports") or []:
        key = (p.get("name") or "").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        ports.append({
            "name": p.get("name"), "lat": p.get("lat"), "lon": p.get("lon"),
            "city": p.get("city"), "note": p.get("note"),
            "extraction_engine": p.get("extraction_engine") or "catalog",
        })
    cache["ports"] = ports
    cache["urls"] = list(dict.fromkeys(
        [*(cache.get("urls") or []), *(harvest.get("urls") or [])]))
    return cache


def catalog_cache_from_zone(zone: dict | None) -> dict:
    zone = zone or {}
    urls = []
    for u in zone.get("sources_bu") or []:
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u)
        elif isinstance(u, dict) and str(u.get("url") or "").startswith("http"):
            urls.append(u["url"])
    ports = list(zone.get("catalog_bu") or [])
    if urls and ports:
        return {"ports": ports, "urls": urls, "sufficient": True}
    return {"ports": ports, "urls": urls, "sufficient": bool(ports and urls)}


def _attach_bu(out: dict, harvest: dict | None, *, named: bool = False,
               official=None) -> dict:
    if harvest:
        if harvest.get("urls"):
            out["sources_bu"] = harvest["urls"]
        if harvest.get("ports") is not None:
            out["catalog_bu_n"] = len(harvest["ports"])
    out["catalog_named"] = bool(named)
    if official:
        out["official_name"] = official
    return out


def catalog_named_verdict(doc: dict, hit: dict, harvest: dict) -> dict:
    """Name already on the list: no yes/no judge (beam D)."""
    out = {
        "judge_status": "catalog",
        "judge_engine": "catalog-bu",
        "judge_kind": "unknown",
        "judge_confidence": 100,
        "judge_reason": (
            "Nom présent sur la liste officielle (faisceau D). "
            "Juge oui/non sauté — plaisance (P) plus tard."
        )[:240],
        "judge_at": now_iso(),
    }
    return _attach_bu(out, harvest, named=True, official=(hit or {}).get("name"))


async def persist_bu_catalog(db, zone: dict, cache: dict) -> None:
    """Write sources_bu + catalog_bu on the EEZ card. Not poe_ports."""
    if db is None or not zone or zone.get("mrgid") is None:
        return
    if not cache.get("sufficient") or not (cache.get("urls") or cache.get("ports")):
        return
    try:
        await db.eez_zones.update_one(
            {"mrgid": int(zone["mrgid"])},
            {"$addToSet": {"sources_bu": {"$each": list(cache.get("urls") or [])}},
             "$set": {
                 "catalog_bu": list(cache.get("ports") or []),
                 "catalog_bu_at": now_iso(),
             }},
        )
    except Exception:
        return


async def judge_one(doc: dict, zone: dict, settings: dict, log,
                    use_agent: bool = True, db=None, catalog_cache: dict | None = None,
                    persist_memory: bool = True, reuse_paid_sources: bool = False,
                    prefetched: dict | None = None) -> dict:
    name = doc.get("name") or ""
    cache = catalog_cache if catalog_cache is not None else catalog_cache_from_zone(zone)
    cached_hit = seed_on_catalog(doc, cache.get("ports")) if cache.get("sufficient") else None
    if cached_hit:
        log(f"catalogue BU: {name} déjà sur la liste — juge sauté")
        out = catalog_named_verdict(doc, cached_hit, cache)
        out["judge_sources"] = list(cache.get("urls") or [])
        out["judge_agent"] = False
        return out

    exc = load_exceptions()
    whitelist = build_whitelist(zone.get("iso2"), zone.get("sov_iso2"), exc)
    key = tf_api_key(settings) or (os.environ.get("TINYFISH_API_KEY") or "")
    texts = []
    blocked_official = []
    fetched = {}
    if reuse_paid_sources:
        urls = paid_fetch_urls(doc, cap=min(4, FETCH_URL_CAP))
        hits = []
        if prefetched:
            fetched = {u: prefetched[u] for u in urls if u in prefetched}
        missing = [u for u in urls if u not in fetched]
        if key and missing:
            fetched.update(await tf_fetch(missing, key, log=log) or {})
    else:
        hits = await _search_hits(doc, zone, whitelist, key, log)
        urls = select_fetch_urls(hits, whitelist, FETCH_URL_CAP)
        if key and urls:
            fetched = await tf_fetch(urls, key, log=log) or {}
    # PDF / JS: empty Fetch → same cascade as top-down. Maps: unchanged.
    fetched = await complete_with_cascade(urls, fetched, log=log)
    for u in urls:
        rec = fetched.get(u) or {}
        if rec.get("blocked") or rec.get("error") == "bot_blocked":
            blocked_official.append(u)
            continue
        if rec.get("error"):
            continue
        chunk = (rec.get("text") or "")[:4000]
        if chunk:
            texts.append(f"URL {u}\n{chunk}")
    agent_used = False
    if use_agent and key and blocked_official:
        agent_used = True
        zname = zone.get("name") or zone.get("geoname") or ""
        agent = await tf_poe_agent(
            blocked_official[0], name, zname, key,
            iso2=zone.get("iso2"), log=log)
        if agent and not agent.get("_agent_error"):
            texts.append(
                "AGENT TinyFish "
                f"{agent.get('_agent_profile') or ''}\n"
                f"{agent}"
            )
            if not any((fetched.get(u) or {}).get("text") for u in urls):
                judged = parse_judge(agent)
                if judged.get("judge_status") != "inconclusive":
                    judged["judge_engine"] = "tinyfish-agent"
                    judged["judge_sources"] = urls
                    judged["judge_agent_url"] = blocked_official[0]
                    judged["judge_at"] = now_iso()
                    return judged
    harvest = harvest_bu_catalog(fetched, urls)
    if harvest["sufficient"]:
        merge_catalog_cache(cache, harvest)
        zone["sources_bu"] = list(cache.get("urls") or [])
        zone["catalog_bu"] = list(cache.get("ports") or [])
        remembered = remember_seed_urls(
            zone, harvest["urls"], persist=persist_memory)
        if remembered:
            log(f"sources_bu mémorisées ({zone.get('iso2')}): {remembered}")
        log(f"catalogue BU: {len(harvest['ports'])} port(s) · "
            f"{len(harvest['urls'])} url(s)")
        await persist_bu_catalog(db, zone, cache)
        hit = seed_on_catalog(doc, harvest["ports"])
        if hit:
            log(f"catalogue BU: {name} sur la liste — juge sauté")
            out = catalog_named_verdict(doc, hit, harvest)
            out["judge_sources"] = urls
            out["judge_agent"] = agent_used
            if blocked_official:
                out["judge_agent_url"] = blocked_official[0]
            return out
        log(f"catalogue BU: {name} hors liste — juge du résidu")
    if not texts:
        snippets = [
            f"{h.get('title') or ''} — {h.get('snippet') or ''}"
            for h in hits if url_allowed(h.get("url") or "", whitelist)
        ] or [
            f"{h.get('title') or ''} — {h.get('snippet') or ''}" for h in hits[:FETCH_URL_CAP]
        ]
        texts = [s for s in snippets if s.strip(" —")]
    context = "\n\n".join(texts).strip()
    if not context:
        out = parse_judge({"is_poe": None, "reason": "aucune source"})
        out["judge_engine"] = None
        out["judge_sources"] = urls
        out["judge_agent"] = agent_used
        out["judge_at"] = now_iso()
        return _attach_bu(out, harvest if harvest.get("sufficient") else None)
    judged = await _judge_llm(doc, zone, context, settings, log)
    judged["judge_sources"] = urls
    judged["judge_agent"] = agent_used
    if blocked_official:
        judged["judge_agent_url"] = blocked_official[0]
    judged["judge_at"] = now_iso()
    if harvest.get("sufficient"):
        _attach_bu(judged, harvest, named=False)
    return judged


async def execute_enrich(db, state, *, run_id: str = "",
                         source: str = "run",
                         do_geocode: bool = True, do_verify: bool = True,
                         verdicts: list[str] | None = None,
                         limit: int = 0, concurrency: int = 2,
                         use_agent: bool = True,
                         persist_memory: bool = True,
                         only_mrgids=None,
                         residue_only: bool = False) -> dict:
    """Geocode then judge. Resume. No poe_ports.

    source=seeds reads/writes poe_seed_ports. source=run uses poe_run_ports.
    """
    from app.db import get_settings

    wanted = tuple(v for v in (verdicts or list(VERIFY_ORDER)) if v in VERIFY_ORDER)
    settings = {}
    try:
        settings = await get_settings()
    except Exception:
        pass
    from_seeds = source == "seeds"
    if from_seeds:
        coll = db.poe_seed_ports
        ports = await coll.find({}).to_list(20000)
        if not ports:
            raise ValueError("poe_seed_ports vide — lancer POST /api/poe/seeds/build")
        task_id = "seed-enrich"
    else:
        coll = db.poe_run_ports
        q = {"run_id": run_id}
        ports = await coll.find(q).to_list(20000)
        if not ports:
            raise ValueError(f"run {run_id} sans ports — lancer POST /api/poe/seeds/verify")
        task_id = run_id
    if only_mrgids:
        wanted_m = {int(x) for x in only_mrgids}
        ports = [p for p in ports
                 if p.get("mrgid") is not None and int(p["mrgid"]) in wanted_m]
    attach_geocode_context(ports)
    geo_todo = [p for p in ports if do_geocode and _needs_geocode(p)]
    judge_pool = [p for p in ports if do_verify and _needs_judge(p, wanted)]
    mrgids = {int(p["mrgid"]) for p in geo_todo + judge_pool if p.get("mrgid") is not None}
    zones = await _zone_cache(db, mrgids)
    if residue_only:
        judge_pool = [
            p for p in judge_pool
            if not _named_on_zone_catalog(
                p, zones.get(int(p["mrgid"])) if p.get("mrgid") is not None else None)
        ]
    if limit and limit > 0:
        geo_todo = geo_todo[:limit]
        if geo_todo:
            # name_only batch: judge what we geocode, not a 2nd unverified batch.
            judge_pool = []
        else:
            judge_pool = judge_pool[:limit]
    else:
        # Full run: geocode first, do not judge the same seed twice.
        judge_pool = [p for p in judge_pool if not _needs_geocode(p)]
    state.total = len(geo_todo) + len(judge_pool)
    state.progress = 0
    log = state.log
    log(f"enrich {task_id}: géocode {len(geo_todo)} · juge {len(judge_pool)} "
        f"(concurrency={concurrency} agent={use_agent} source={'seeds' if from_seeds else 'run'})")
    counts = Counter()
    just_geocoded = []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _geo_one(doc):
        async with sem:
            if state.cancel:
                return
            zone = zones.get(int(doc["mrgid"])) if doc.get("mrgid") is not None else None
            if not zone:
                log(f"pas de ZEE pour {doc.get('name')} mrgid={doc.get('mrgid')}")
                counts["geo_no_zone"] += 1
                return
            try:
                upd = await geocode_one(
                    doc, zone, lambda m: log(f"[{doc.get('name')}] {m}"),
                    settings)
            except Exception as e:
                counts["geo_error"] += 1
                log(f"géocode FAIL {doc.get('name')}: {type(e).__name__}: {e}")
                return
            if upd.get("has_coords"):
                counts["geo_ok"] += 1
                doc.update(upd)
                if doc.get("verify_verdict") == "name_only":
                    doc["verify_verdict"] = "unverified"
                    upd["verify_verdict"] = "unverified"
                just_geocoded.append(doc)
            else:
                counts["geo_miss"] += 1
            await coll.update_one(
                {"_id": doc["_id"]}, {"$set": upd})
            state.progress += 1

    async def _judge_one(doc):
        async with sem:
            if state.cancel:
                return
            zone = zones.get(int(doc["mrgid"])) if doc.get("mrgid") is not None else None
            if not zone:
                counts["judge_no_zone"] += 1
                return
            try:
                cache = zone.setdefault("_bu_catalog", catalog_cache_from_zone(zone))
                judged = await judge_one(
                    doc, zone, settings, lambda m: log(f"[{doc.get('name')}] {m}"),
                    use_agent=use_agent, db=db, catalog_cache=cache,
                    persist_memory=persist_memory)
            except Exception as e:
                counts["judge_error"] += 1
                log(f"juge FAIL {doc.get('name')}: {type(e).__name__}: {e}")
                return
            doc.update(judged)
            if judged.get("has_coords") or doc.get("lat") is not None:
                doc["has_coords"] = True
            new_v = apply_judge_verdict(doc, judged)
            judged["verify_verdict"] = new_v
            counts[f"judge_{judged.get('judge_status')}"] += 1
            await coll.update_one(
                {"_id": doc["_id"]}, {"$set": judged})
            state.progress += 1

    if geo_todo:
        await asyncio.gather(*[_geo_one(p) for p in geo_todo])
        if do_verify:
            extra_mrgids = {
                int(p["mrgid"]) for p in just_geocoded if p.get("mrgid") is not None
            } - set(zones)
            if extra_mrgids:
                zones.update(await _zone_cache(db, extra_mrgids))
            for p in just_geocoded:
                if not p.get("judge_status"):
                    judge_pool.append(p)
            state.total = len(geo_todo) + len(judge_pool)

    if judge_pool and not state.cancel:
        await asyncio.gather(*[_judge_one(p) for p in judge_pool])

    summary = {
        "run_id": task_id,
        "source": "seeds" if from_seeds else "run",
        "geocode_todo": len(geo_todo),
        "judge_todo": len(judge_pool),
        "counts": dict(counts),
        "cancelled": bool(state.cancel),
        "wrote_poe_ports": False,
        "use_agent": use_agent,
        "finished_at": now_iso(),
    }
    await db.poe_runs.update_one(
        {"_id": task_id},
        {"$set": {"enrich": summary, "enriched_at": now_iso()}},
        upsert=from_seeds)
    state.summary = summary
    log(f"enrich terminé: {summary['counts']}")
    return summary


async def mine_paid_sources(db, state, *, fetch_cap: int = DEFAULT_MINE_FETCH_CAP,
                            persist_memory: bool = True,
                            mark_named: bool = True,
                            judge_residue: bool = True,
                            residue_limit: int = 0,
                            include_runs: bool = True,
                            concurrency: int = 2,
                            use_agent: bool = True) -> dict:
    """Re-read already-paid judge_sources (Fetch, 0 Search).

    If a page is a catalog: sources_bu + catalog_bu on the EEZ,
    remember_seed_urls, judge_status=catalog for names already on the list.
    The yes/no judge only runs on the residue of THESE EEZs.
    Never writes poe_ports.
    """
    from app.db import get_settings

    settings = {}
    try:
        settings = await get_settings()
    except Exception:
        pass
    log = state.log
    seeds = await db.poe_seed_ports.find({}).to_list(20000)
    if not seeds:
        raise ValueError("poe_seed_ports vide — lancer POST /api/poe/seeds/build")
    docs = list(seeds)
    if include_runs:
        try:
            extra = await db.poe_run_ports.find(
                {"judge_sources.0": {"$exists": True}},
                {"judge_sources": 1, "mrgid": 1},
            ).to_list(20000)
            docs.extend(extra or [])
        except Exception:
            pass
    collected = collect_paid_source_urls(docs)
    selected = select_mine_urls(collected, cap=fetch_cap)
    key = tf_api_key(settings) or (os.environ.get("TINYFISH_API_KEY") or "")
    urls = [it["url"] for it in selected]
    url_mrgids = {it["url"]: set(it["mrgids"]) for it in selected}
    log(f"mine: {len(collected)} URL payées · {len(urls)} à Fetch (0 Search)")
    state.total = max(1, len(urls) + (1 if mark_named or judge_residue else 0))
    state.progress = 0
    fetched = {}
    if key and urls:
        batches = (len(urls) + 9) // 10
        for i in range(0, len(urls), 10):
            if state.cancel:
                log("mine: annulation pendant Fetch")
                break
            batch = urls[i:i + 10]
            log(f"mine Fetch {i // 10 + 1}/{batches} ({len(batch)} URL)")
            try:
                part = await asyncio.wait_for(tf_fetch(batch, key, log=log), timeout=180)
            except TimeoutError:
                log(f"mine Fetch timeout lot {i // 10 + 1}")
                part = {}
            part = await complete_with_cascade(batch, part or {}, log=log)
            fetched.update(part or {})
            ok = sum(1 for r in (part or {}).values() if str(r.get("text") or "").strip())
            log(f"mine Fetch lot {i // 10 + 1}: {ok}/{len(batch)} textes")
            state.progress = min(state.total, i + len(batch))
    elif urls:
        log("mine: pas de clé TinyFish — cascade locale (PDF / HTML)")
        fetched = await complete_with_cascade(urls, {}, log=log)
    state.progress = min(state.total, len(urls) or 1)

    caches: dict[int, dict] = {}
    catalog_urls = []
    for u in urls:
        rec = fetched.get(u) or {}
        harvest = harvest_bu_catalog({u: rec}, [u])
        if not harvest.get("sufficient"):
            continue
        catalog_urls.append(u)
        for mid in url_mrgids.get(u) or []:
            cache = caches.setdefault(mid, {"ports": [], "urls": [], "sufficient": False})
            merge_catalog_cache(cache, harvest)

    zones = await _zone_cache(db, set(caches)) if caches else {}
    remembered = []
    for mid, cache in caches.items():
        zone = zones.get(mid) or {"mrgid": mid}
        zone["sources_bu"] = list(cache.get("urls") or [])
        zone["catalog_bu"] = list(cache.get("ports") or [])
        zone["_bu_catalog"] = cache
        zones[mid] = zone
        added = remember_seed_urls(zone, cache.get("urls") or [], persist=persist_memory)
        remembered.extend(added)
        await persist_bu_catalog(db, zone, cache)
        log(f"mine ZEE {mid}: {len(cache.get('ports') or [])} ports · "
            f"{len(cache.get('urls') or [])} url(s)")

    counts = Counter()
    counts["urls_paid"] = len(collected)
    counts["urls_fetched"] = len(urls)
    counts["urls_catalog"] = len(catalog_urls)
    counts["eez_catalog"] = len(caches)
    counts["remembered"] = len(remembered)

    if mark_named and caches:
        for doc in seeds:
            if state.cancel:
                break
            mid = doc.get("mrgid")
            try:
                mid = int(mid) if mid is not None else None
            except (TypeError, ValueError):
                continue
            cache = caches.get(mid)
            if not cache:
                continue
            hit = seed_on_catalog(doc, cache.get("ports"))
            if not hit:
                continue
            current = doc.get("judge_status")
            if current and current not in ("inconclusive",):
                counts["named_kept"] += 1
                continue
            judged = catalog_named_verdict(doc, hit, cache)
            judged["judge_sources"] = list(
                dict.fromkeys([*(cache.get("urls") or []), *iter_judge_urls(doc)]))
            judged["verify_verdict"] = apply_judge_verdict(doc, judged)
            await db.poe_seed_ports.update_one({"_id": doc["_id"]}, {"$set": judged})
            counts["named_catalog"] += 1

    residue = None
    if judge_residue and caches and not state.cancel:
        log(f"mine: juge du résidu sur {len(caches)} ZEE catalogue")
        residue = await execute_enrich(
            db, state, source="seeds", do_geocode=False, do_verify=True,
            verdicts=list(VERIFY_ORDER), limit=residue_limit,
            concurrency=concurrency, use_agent=use_agent,
            persist_memory=persist_memory,
            only_mrgids=set(caches), residue_only=True)
        counts["residue_todo"] = residue.get("judge_todo") or 0
        for k, v in (residue.get("counts") or {}).items():
            counts[k] += v

    summary = {
        "run_id": MINE_TASK_ID,
        "urls_paid": len(collected),
        "urls_selected": urls,
        "urls_catalog": catalog_urls,
        "eez_catalog": sorted(caches),
        "counts": dict(counts),
        "residue": ({k: residue[k] for k in ("judge_todo", "counts", "run_id")}
                    if residue else None),
        "cancelled": bool(state.cancel),
        "wrote_poe_ports": False,
        "search": False,
        "finished_at": now_iso(),
    }
    await db.poe_runs.update_one(
        {"_id": MINE_TASK_ID},
        {"$set": {"mine": summary, "mined_at": now_iso()}},
        upsert=True)
    state.summary = summary
    state.progress = state.total
    log(f"mine terminé: {summary['counts']}")
    return summary


REMEMBERED_TASK_ID = "seed-remembered"


async def apply_remembered_catalogs(db, state, *, persist_memory: bool = False,
                                    mark_named: bool = True,
                                    judge_residue: bool = True,
                                    residue_limit: int = 25,
                                    concurrency: int = 2,
                                    use_agent: bool = False) -> dict:
    """Fetch already-remembered seed_urls (0 Search) on 200 NM EEZs
    still without a catalog, then judge the probable/unverified residue.

    No joint regime. Never writes poe_ports. No seeds/build.
    """
    from app.db import get_settings

    settings = {}
    try:
        settings = await get_settings()
    except Exception:
        pass
    log = state.log
    exc = load_exceptions()
    seed_map = {
        str(cc).upper(): [u for u in (urls or []) if str(u).startswith("http")]
        for cc, urls in (exc.get("seed_urls") or {}).items()
        if urls
    }
    seeds = await db.poe_seed_ports.find(
        {},
        {"name": 1, "mrgid": 1, "judge_status": 1, "judge_sources": 1,
         "verify_verdict": 1, "has_coords": 1, "lat": 1, "lon": 1},
    ).to_list(20000)
    if not seeds:
        raise ValueError("poe_seed_ports vide — lancer POST /api/poe/seeds/build")
    zones = await db.eez_zones.find(
        {},
        {"mrgid": 1, "name": 1, "geoname": 1, "iso2": 1, "sov_iso2": 1,
         "pol_type": 1, "catalog_bu": 1, "sources_bu": 1},
    ).to_list(500)
    targets = []
    for z in zones:
        cc = (z.get("iso2") or "").upper()
        if cc not in seed_map:
            continue
        pol = (z.get("pol_type") or "").lower()
        if "joint" in pol:
            continue
        if catalog_cache_from_zone(z).get("sufficient"):
            continue
        if z.get("mrgid") is None:
            continue
        targets.append(z)
    url_mrgids: dict[str, set[int]] = defaultdict(set)
    for z in targets:
        mid = int(z["mrgid"])
        for u in seed_map[(z.get("iso2") or "").upper()]:
            url_mrgids[u].add(mid)
    urls = list(url_mrgids)
    key = tf_api_key(settings) or (os.environ.get("TINYFISH_API_KEY") or "")
    log(f"remembered: {len(targets)} ZEE · {len(urls)} URL (0 Search)")
    state.total = max(1, len(urls) + 1)
    state.progress = 0
    fetched = {}
    if key and urls:
        batches = (len(urls) + 9) // 10
        for i in range(0, len(urls), 10):
            if state.cancel:
                break
            batch = urls[i:i + 10]
            log(f"remembered Fetch {i // 10 + 1}/{batches}")
            try:
                part = await asyncio.wait_for(tf_fetch(batch, key, log=log), timeout=180)
            except TimeoutError:
                log(f"remembered Fetch timeout lot {i // 10 + 1}")
                part = {}
            part = await complete_with_cascade(batch, part or {}, log=log)
            fetched.update(part or {})
            state.progress = min(state.total, i + len(batch))
    elif urls:
        log("remembered: pas de clé TinyFish — cascade locale")
        fetched = await complete_with_cascade(urls, {}, log=log)

    caches: dict[int, dict] = {}
    catalog_urls = []
    for u in urls:
        harvest = harvest_bu_catalog({u: fetched.get(u) or {}}, [u])
        if not harvest.get("sufficient"):
            continue
        catalog_urls.append(u)
        for mid in url_mrgids.get(u) or []:
            cache = caches.setdefault(mid, {"ports": [], "urls": [], "sufficient": False})
            merge_catalog_cache(cache, harvest)

    zone_by = {int(z["mrgid"]): z for z in zones if z.get("mrgid") is not None}
    remembered = []
    for mid, cache in caches.items():
        zone = zone_by.get(mid) or {"mrgid": mid}
        zone["sources_bu"] = list(cache.get("urls") or [])
        zone["catalog_bu"] = list(cache.get("ports") or [])
        zone["_bu_catalog"] = cache
        zone_by[mid] = zone
        added = remember_seed_urls(zone, cache.get("urls") or [], persist=persist_memory)
        remembered.extend(added)
        await persist_bu_catalog(db, zone, cache)
        log(f"remembered ZEE {mid}: {len(cache.get('ports') or [])} ports · "
            f"{len(cache.get('urls') or [])} url(s)")

    counts = Counter()
    counts["eez_targets"] = len(targets)
    counts["urls_fetched"] = len(urls)
    counts["urls_catalog"] = len(catalog_urls)
    counts["eez_catalog"] = len(caches)
    counts["remembered"] = len(remembered)

    if mark_named and caches:
        for doc in seeds:
            if state.cancel:
                break
            mid = doc.get("mrgid")
            try:
                mid = int(mid) if mid is not None else None
            except (TypeError, ValueError):
                continue
            cache = caches.get(mid)
            if not cache:
                continue
            hit = seed_on_catalog(doc, cache.get("ports"))
            if not hit:
                continue
            current = doc.get("judge_status")
            if current and current not in ("inconclusive",):
                counts["named_kept"] += 1
                continue
            judged = catalog_named_verdict(doc, hit, cache)
            judged["judge_sources"] = list(
                dict.fromkeys([*(cache.get("urls") or []), *iter_judge_urls(doc)]))
            judged["verify_verdict"] = apply_judge_verdict(doc, judged)
            await db.poe_seed_ports.update_one({"_id": doc["_id"]}, {"$set": judged})
            counts["named_catalog"] += 1

    residue = None
    if judge_residue and caches and not state.cancel:
        log(f"remembered: juge du résidu probable/unverified sur {len(caches)} ZEE")
        residue = await execute_enrich(
            db, state, source="seeds", do_geocode=False, do_verify=True,
            verdicts=["probable", "unverified"], limit=residue_limit,
            concurrency=concurrency, use_agent=use_agent,
            persist_memory=persist_memory,
            only_mrgids=set(caches), residue_only=True)
        counts["residue_todo"] = residue.get("judge_todo") or 0
        for k, v in (residue.get("counts") or {}).items():
            counts[k] += v

    summary = {
        "run_id": REMEMBERED_TASK_ID,
        "eez_catalog": sorted(caches),
        "urls_catalog": catalog_urls,
        "counts": dict(counts),
        "residue": ({k: residue[k] for k in ("judge_todo", "counts", "run_id")}
                    if residue else None),
        "cancelled": bool(state.cancel),
        "wrote_poe_ports": False,
        "search": False,
        "finished_at": now_iso(),
    }
    await db.poe_runs.update_one(
        {"_id": REMEMBERED_TASK_ID},
        {"$set": {"remembered": summary, "remembered_at": now_iso()}},
        upsert=True)
    state.summary = summary
    state.progress = state.total
    log(f"remembered terminé: {summary['counts']}")
    return summary
