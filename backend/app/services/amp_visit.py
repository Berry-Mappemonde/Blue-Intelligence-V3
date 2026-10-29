"""AMP visit-URL discovery — Harbormaster / PoE cascade.

1. Refresh ProtectedSeas attributes (ArcGIS, no geometry) — free extras.
2. Extras / Website-label heuristic.
3. TinyFish Fetch on ``manager_url``; local cascade (PDF / JS) if Fetch is empty.
4. ``search_named`` (``site:`` then open web; DuckDuckGo if no key).
5. ``ask_yes_no`` judge (``json`` chain: Pro → gpt-oss → Muse), OpenRouter / Claude net.

``visit_url`` is never the manager homepage.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Awaitable, Callable
from app.core.events import HeartbeatWatch, emit
from app.core.search import search_named
from app.core.tinyfish import AMP_VISIT_PURPOSE, FETCH_URL_CAP, tf_api_key
from app.db import get_settings
from app.services import amp as amp_svc

JudgeFn = Callable[[dict, list[dict]], Awaitable[str | None]]
AttrsFetchFn = Callable[[list[str]], Awaitable[dict[str, dict]]]

VISIT_JUDGE_SYSTEM = (
    "Tu juges des URL de visite d'aire marine protégée. "
    "Réponds uniquement en JSON strict : "
    '{"url": "https://example.com/visite", "accept": true, "reason": ""} '
    "url doit être l'une des candidates, ou null. "
    "accept=true seulement si la page donne des procédures de visite, d'entrée, "
    "de permis, de mouillage ou de plaisance POUR LE SITE NOMMÉ. "
    "accept=false pour une homepage institutionnelle, un blog mouillage générique "
    "d'un pays, un article académique, un charter, une page tourisme sans règles, "
    "un autre parc ou un autre pays. N'invente aucune URL."
)
JUDGE_CANDIDATE_CAP = 8

VISIT_HINT_RE = amp_svc.VISIT_HINT_RE
BAD_URL_RE = re.compile(
    r"facebook\.|twitter\.|instagram\.|tiktok\.|linkedin\.|"
    r"tripadvisor\.|booking\.|airbnb\.|youtube\.|"
    r"researchgate\.|atraques\.|yachtmate\.|noonsite\.|"
    r"safetyanchoralarm\.|leportvauban\.|"
    r"/contact|/about|/news|/presse|/donate|/privacy|/legal|"
    r"/login|/cart|/shop",
    re.I,
)
FETCH_BATCH = FETCH_URL_CAP

FetchManyFn = Callable[[list[str]], Awaitable[dict[str, dict]]]
SearchFn = Callable[..., Awaitable[list[dict]]]


def manager_host(url: str | None) -> str | None:
    return amp_svc.url_host(url)


def score_visit_candidate(
    url: str | None,
    manager_url: str | None,
    *,
    title: str = "",
    snippet: str = "",
    curated: bool = False,
    name: str = "",
    require_name: bool = False,
) -> int:
    """Score > 0 = candidat plausible. 0 = rejeter (homepage, pub, hors sujet)."""
    if BAD_URL_RE.search(url or ""):
        return 0
    return amp_svc.rank_visit_candidate(
        url, manager_url, curated=curated, title=title, snippet=snippet,
        name=name, require_name=require_name)


def pick_visit_from_urls(
    manager_url: str | None,
    urls: list[str],
    *,
    titles: dict[str, str] | None = None,
    name: str = "",
    require_name: bool = False,
) -> str | None:
    titles = titles or {}
    best_url, best_score = None, 0
    for raw in urls:
        sc = score_visit_candidate(
            raw, manager_url, title=titles.get(raw, ""),
            name=name, require_name=require_name)
        if sc > best_score:
            best_url, best_score = raw, sc
    if not best_url:
        return None
    chosen, status = amp_svc.pick_visit_url(manager_url, discovered=best_url)
    return chosen if status == "found" else None


def urls_from_fetch_record(rec: dict | None) -> list[str]:
    if not rec:
        return []
    out: list[str] = []
    for link in rec.get("links") or []:
        if isinstance(link, str):
            out.append(link)
        elif isinstance(link, dict):
            href = link.get("url") or link.get("href") or ""
            if href:
                out.append(href)
    out.extend(amp_svc.extract_urls(rec.get("text") or ""))
    return out


def search_query(doc: dict) -> tuple[str, str | None]:
    """Open query: the visit page is not always on the manager host."""
    name = (doc.get("name") or "").strip()
    country = (doc.get("country") or "").strip()
    q = f'"{name}" {country} visit permit anchoring mooring plaisance réglementation'.strip()
    return q, manager_host(doc.get("manager_url"))


def visit_judge_prompt(doc: dict, candidates: list[dict]) -> str:
    lines = []
    for i, cand in enumerate(candidates[:JUDGE_CANDIDATE_CAP], 1):
        title = (cand.get("title") or "").strip()
        lines.append(f"{i}. {cand.get('url')} | {title}")
    return (
        f"Site : {(doc.get('name') or '').strip()}\n"
        f"Pays : {(doc.get('country') or '').strip()}\n"
        f"URL gestionnaire (interdite comme visit_url) : {doc.get('manager_url')}\n"
        f"Candidats :\n" + "\n".join(lines)
    )


def parse_visit_judge(
    data: dict | None,
    candidates: list[dict],
    manager_url: str | None,
) -> str | None:
    """Keep an already proposed URL. Refuse the manager and URLs not in the list."""
    from app.core.judge import parse_yes_no
    yes = parse_yes_no(
        data,
        allowed_urls=candidates,
        forbidden_urls=[manager_url] if manager_url else None,
        url_key=amp_svc.normalize_url,
    )
    return yes.url if yes.accepted else None


async def llm_judge_visit(
    doc: dict,
    candidates: list[dict],
    *,
    settings: dict | None = None,
    log=None,
) -> str | None:
    """NVIDIA (json chain) → OpenRouter → Claude. URL among the candidates."""
    if not candidates:
        return None
    from app.core.judge import ask_yes_no

    yes = await ask_yes_no(
        VISIT_JUDGE_SYSTEM,
        visit_judge_prompt(doc, candidates),
        settings=settings,
        log=log,
        role="json",
        max_tokens=400,
        allowed_urls=candidates,
        forbidden_urls=[doc.get("manager_url")] if doc.get("manager_url") else None,
        url_key=amp_svc.normalize_url,
        on_empty="inconclusive",
    )
    if yes.accepted and yes.url:
        doc["visit_url_judge"] = yes.engine
        return yes.url
    return None


def search_candidates(
    doc: dict,
    hits: list[dict],
    *,
    require_name: bool = True,
) -> list[dict]:
    """SERP prefilter: score > 0, never the manager homepage."""
    out: list[dict] = []
    seen: set[str] = set()
    manager = doc.get("manager_url")
    name = doc.get("name") or ""
    for hit in hits:
        url = hit.get("url") if isinstance(hit, dict) else None
        if not url or amp_svc.urls_equivalent(url, manager):
            continue
        key = amp_svc.normalize_url(url)
        if not key or key in seen:
            continue
        title = (hit.get("title") or "") if isinstance(hit, dict) else ""
        if score_visit_candidate(
            url, manager, title=title, name=name, require_name=require_name,
        ) <= 0:
            continue
        seen.add(key)
        out.append({"url": url, "title": title})
        if len(out) >= JUDGE_CANDIDATE_CAP:
            break
    return out


def search_queries(doc: dict) -> list[tuple[str, str | None]]:
    """site:host first (bonus), then open search if needed."""
    name = (doc.get("name") or "").strip()
    country = (doc.get("country") or "").strip()
    host = manager_host(doc.get("manager_url"))
    open_q, _ = search_query(doc)
    out: list[tuple[str, str | None]] = []
    if host:
        scoped = (
            f"site:{host} (visite OR visit OR plaisance OR mouillage OR permit "
            f"OR réglementation OR anchoring) \"{name}\""
        ).strip()
        out.append((scoped, host))
    out.append((open_q, None))
    return out


def _is_manual_visit(doc: dict) -> bool:
    return (doc.get("visit_url_source") or "") == "manual" and bool(doc.get("visit_url"))


def _needs_visit(doc: dict, *, from_scratch: bool = False) -> bool:
    if _is_manual_visit(doc):
        return False
    if from_scratch:
        return True
    if doc.get("visit_url") and not amp_svc.urls_equivalent(
            doc.get("visit_url"), doc.get("manager_url")):
        return False
    return True


async def _write_visit(db, doc: dict) -> None:
    from app.services.isolated_runs import current_run_id, write_item
    rid = current_run_id()
    fields = {
        "visit_url": doc.get("visit_url"),
        "visit_url_status": doc.get("visit_url_status"),
        "visit_url_source": doc.get("visit_url_source"),
        "visit_url_judge": doc.get("visit_url_judge"),
        "enriched_at": doc.get("enriched_at"),
        "manager_url": doc.get("manager_url"),
        "ps_website_raw": doc.get("ps_website_raw"),
        "other_helpful_links": doc.get("other_helpful_links"),
    }
    if rid:
        payload = {**doc, **fields}
        await write_item(
            db, "amp", rid, payload,
            source_id=doc.get("site_id") or doc.get("_id"),
        )
        return
    await db.amp_sites.update_one({"_id": doc["_id"]}, {"$set": fields})


async def _commit_discovered(db, doc: dict, url: str | None, source: str,
                             recorder=None) -> str:
    site_id = doc.get("site_id") or doc.get("_id")
    await emit(
        recorder, "visit_try",
        site_id=site_id, name=doc.get("name"), source=source, url=url,
    )
    if not url:
        return "unchanged"
    amp_svc.apply_visit_choice(doc, discovered=url, source=source)
    after = doc.get("visit_url")
    if after and not amp_svc.urls_equivalent(after, doc.get("manager_url")):
        await _write_visit(db, doc)
        await emit(
            recorder, "visit_found",
            site_id=site_id, name=doc.get("name"), source=source, url=after,
        )
        return "found"
    if doc.get("visit_url_status") == "rejected_same_as_manager":
        await _write_visit(db, doc)
        await emit(
            recorder, "visit_rejected",
            site_id=site_id, name=doc.get("name"), source=source,
            url=url, reason="rejected_same_as_manager",
        )
        return "rejected"
    await emit(
        recorder, "visit_rejected",
        site_id=site_id, name=doc.get("name"), source=source,
        url=url, reason=doc.get("visit_url_status") or "unchanged",
    )
    return "unchanged"


def _doc_in_bbox(doc: dict, bbox: tuple[float, float, float, float]) -> bool:
    minx, miny, maxx, maxy = bbox
    lat = doc.get("lat")
    lon = doc.get("lon") if doc.get("lon") is not None else doc.get("lng")
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return miny <= lat_f <= maxy and minx <= lon_f <= maxx


async def pending_sites(db, limit: int, *, bbox=None, run_id=None,
                        from_scratch: bool = False) -> list[dict]:
    cap = int(limit) if limit and int(limit) > 0 else 50_000
    if from_scratch:
        docs = await db.amp_sites.find({}).to_list(cap * 4 if bbox else cap)
        docs = [d for d in docs if _needs_visit(d, from_scratch=True)]
    else:
        q = {
            "$or": [
                {"visit_url": {"$in": [None, ""]}},
                {"visit_url_status": {"$in": ["none", "not_found", None]}},
            ],
        }
        docs = await db.amp_sites.find(q).to_list(cap * 4 if bbox else cap)
        docs = [d for d in docs if _needs_visit(d)]
    if bbox:
        docs = [d for d in docs if _doc_in_bbox(d, bbox)]
    if run_id:
        done_ids: set[str] = set()
        try:
            already = await db.amp_run_sites.find({"run_id": run_id}).to_list(5000)
        except Exception:
            already = []
        for row in already:
            status = row.get("visit_url_status")
            if row.get("visit_url") or status in (
                "found", "rejected_same_as_manager", "not_found",
            ):
                sid = str(row.get("site_id") or row.get("source_id") or "")
                if sid:
                    done_ids.add(sid)
        docs = [
            d for d in docs
            if str(d.get("site_id") or d.get("_id") or "") not in done_ids
        ]
    if from_scratch:
        blanked = []
        for doc in docs:
            out = dict(doc)
            out["visit_url"] = None
            out["visit_url_status"] = "none"
            out["visit_url_source"] = None
            blanked.append(out)
        docs = blanked
    return docs[:cap]


async def copy_visit_to_live(db, run_id: str) -> int:
    """Copy visit_urls found in the run onto ``amp_sites``.

    Do not overwrite a live URL with an empty one: if from-scratch found
    nothing, keep the old one.
    """
    if not run_id:
        return 0
    n = 0
    try:
        rows = await db.amp_run_sites.find({"run_id": run_id}).to_list(50_000)
    except Exception:
        return 0
    for row in rows:
        url = row.get("visit_url")
        if not url:
            continue
        sid = str(row.get("site_id") or row.get("source_id") or "")
        if not sid:
            continue
        fields = {
            "visit_url": url,
            "visit_url_status": row.get("visit_url_status") or "found",
            "visit_url_source": row.get("visit_url_source"),
            "visit_url_judge": row.get("visit_url_judge"),
            "enriched_at": row.get("enriched_at"),
        }
        try:
            await db.amp_sites.update_one(
                {"_id": sid}, {"$set": fields})
            await db.amp_sites.update_one(
                {"site_id": sid}, {"$set": fields})
            n += 1
        except Exception:
            continue
    return n


async def default_fetch_many(urls: list[str], *, key: str, log=None) -> dict[str, dict]:
    """TinyFish Fetch, then local cascade if there is no text and no links."""
    from app.core.extract import as_fetch_record, read_urls
    pages = await read_urls(
        urls, min_chars=80, prefer_fetch=True, fetch_purpose=AMP_VISIT_PURPOSE,
        fetch_key=key, keep_if_links=True, log=log)
    return {u: as_fetch_record(p, u) for u, p in pages.items()}


async def default_search(query: str, *, key: str, include_domains=None, log=None) -> list[dict]:
    return await search_named(
        query, key=key or "", include_domains=include_domains,
        purpose=AMP_VISIT_PURPOSE, log=log)


async def _pick_from_search(
    doc: dict,
    hits: list[dict],
    *,
    judge: JudgeFn | None,
) -> str | None:
    cands = search_candidates(doc, hits, require_name=True)
    if judge:
        return await judge(doc, cands)
    titles = {c["url"]: c.get("title") or "" for c in cands}
    return pick_visit_from_urls(
        doc.get("manager_url"),
        [c["url"] for c in cands],
        titles=titles,
        name=doc.get("name") or "",
        require_name=True,
    )


async def discover_visit_urls(
    db,
    *,
    state,
    limit: int = 200,
    skip_search: bool = False,
    fetch_many_fn: FetchManyFn | None = None,
    search_fn: SearchFn | None = None,
    tf_key: str | None = None,
    refresh_attrs: bool = True,
    attrs_fetch_fn: AttrsFetchFn | None = None,
    use_llm_judge: bool = True,
    judge_fn: JudgeFn | None = None,
    run_id: str | None = None,
    recorder=None,
    bbox: tuple[float, float, float, float] | None = None,
    from_scratch: bool = False,
    harvest_polygons: bool = False,
) -> dict:
    """Refresh PS → extras → Fetch → Search → Muse / OpenRouter judge."""
    from app.services.isolated_runs import bind_run, reset_run, write_item

    token = bind_run(run_id) if run_id else None
    state.running = True
    state.started_at = time.time()
    state.finished_at = None
    state.error = None
    state.logs = []
    state.summary = None
    state.progress = 0
    state.cancel = False
    state.run_id = run_id

    hb = HeartbeatWatch(recorder).start(lambda: {
        "progress": state.progress,
        "total": state.total,
        "last_item": getattr(state, "last_item", None),
    })
    harvest_summary = None
    if harvest_polygons and not bbox:
        harvest_summary = await amp_svc.harvest_world_polygons(
            db, state=state, force=True)
        await emit(recorder, "amp_harvest", **(harvest_summary or {}))
    if bbox:
        await emit(recorder, "bbox", bbox=list(bbox),
                   minx=bbox[0], miny=bbox[1], maxx=bbox[2], maxy=bbox[3])
        last = getattr(amp_svc, "LAST_REFRESH", None) or {}
        src = last.get("source")
        if src == "arcgis":
            await emit(recorder, "arcgis_fetch", bbox=list(bbox),
                       count=last.get("count"), fetched=last.get("fetched"))
        else:
            await emit(recorder, "cache_hit", bbox=list(bbox),
                       count=last.get("count"), source=src or "cache")

    docs = await pending_sites(
        db, limit, bbox=bbox, run_id=run_id, from_scratch=from_scratch)
    if bbox and run_id:
        map_docs = []
        try:
            map_docs = await amp_svc.query_cache(db, bbox, limit=int(limit))
        except Exception:
            map_docs = []
        if not map_docs:
            try:
                raw = await db.amp_sites.find({}).to_list(4000)
            except Exception:
                raw = []
            map_docs = [d for d in raw if _doc_in_bbox(d, bbox)][:int(limit)]
        for doc in map_docs:
            try:
                await write_item(
                    db, "amp", run_id, dict(doc),
                    source_id=doc.get("site_id") or doc.get("_id"),
                )
            except Exception:
                pass
    state.total = len(docs)
    counters = {
        "selected": len(docs),
        "attrs_refreshed": 0,
        "from_links": 0,
        "from_fetch": 0,
        "from_search": 0,
        "rejected_same_as_manager": 0,
        "unchanged": 0,
        "errors": 0,
        "no_tinyfish_key": False,
        "judge_engine": None,
    }
    state.log(
        f"AMP visite : {len(docs)} site(s)"
        + (" (from scratch)" if from_scratch else " sans URL de visite")
    )

    remaining: list[dict] = []
    try:
        if refresh_attrs and docs:
            refresh_docs = list(docs)
            seen_ids = {d.get("_id") for d in refresh_docs}
            try:
                dirty = await db.amp_sites.find(
                    {"manager_url": {"$regex": r"\|"}}).to_list(2000)
            except Exception:
                dirty = []
            for extra in dirty:
                if extra.get("_id") not in seen_ids:
                    refresh_docs.append(extra)
                    seen_ids.add(extra.get("_id"))
            counters["attrs_refreshed"] = await amp_svc.refresh_protectedseas_attrs(
                db, refresh_docs, fetch_fn=attrs_fetch_fn, log=state.log)

        settings = {}
        if use_llm_judge or tf_key is None:
            try:
                settings = await get_settings()
            except Exception:
                settings = {}
        judge = None if not use_llm_judge else (
            judge_fn or (lambda doc, cands: llm_judge_visit(
                doc, cands, settings=settings, log=state.log)))

        for doc in docs:
            if getattr(state, "cancel", False):
                state.log("Stop demandé")
                break
            site_id = doc.get("site_id") or doc.get("_id")
            state.last_item = site_id
            hb.update(progress=state.progress, last_item=site_id)
            await emit(
                recorder, "site",
                site_id=site_id, name=doc.get("name"),
                has_visit=bool(doc.get("visit_url")),
            )
            before = doc.get("visit_url")
            await emit(
                recorder, "visit_try",
                site_id=site_id, name=doc.get("name"),
                source="other_helpful_links",
            )
            amp_svc.apply_visit_choice(doc, source="other_helpful_links")
            after = doc.get("visit_url")
            if after and not amp_svc.urls_equivalent(after, doc.get("manager_url")):
                await _write_visit(db, doc)
                counters["from_links"] += 1
                state.log(f"✓ {doc.get('name')} ← other_helpful_links")
                await emit(
                    recorder, "visit_found",
                    site_id=site_id, name=doc.get("name"),
                    source="other_helpful_links", url=after,
                )
            else:
                if doc.get("visit_url_status") == "rejected_same_as_manager":
                    counters["rejected_same_as_manager"] += 1
                    await emit(
                        recorder, "visit_rejected",
                        site_id=site_id, name=doc.get("name"),
                        source="other_helpful_links",
                        url=after, reason="rejected_same_as_manager",
                    )
                remaining.append(doc)
                if after != before or doc.get("ps_website_raw"):
                    await _write_visit(db, doc)
            state.progress += 1

        key = (tf_key if tf_key is not None else tf_api_key(settings)).strip()
        if not key:
            counters["no_tinyfish_key"] = True
            state.log("Pas de clé TinyFish — Search via DuckDuckGo, Fetch via cascade")

        fetch_many = fetch_many_fn or (
            lambda urls: default_fetch_many(urls, key=key, log=state.log))
        search = search_fn or (
            lambda q, include_domains=None: default_search(
                q, key=key, include_domains=include_domains, log=state.log))

        after_fetch: list[dict] = []
        by_manager: dict[str, list[dict]] = defaultdict(list)
        for doc in remaining:
            url = doc.get("manager_url") or ""
            if url:
                by_manager[url].append(doc)
            else:
                after_fetch.append(doc)
        unique_urls = list(by_manager)
        state.log(
            f"Fetch : {len(unique_urls)} URL gestionnaire(s) "
            f"pour {len(remaining)} site(s)")
        for i in range(0, len(unique_urls), FETCH_BATCH):
            if getattr(state, "cancel", False):
                state.log("Stop demandé")
                break
            batch_urls = unique_urls[i:i + FETCH_BATCH]
            try:
                recs = await fetch_many(batch_urls)
            except Exception as exc:
                state.log(f"Lot Fetch : {type(exc).__name__}: {str(exc)[:80]}")
                recs = {}
            for url in batch_urls:
                rec = recs.get(url) or {}
                links = urls_from_fetch_record(rec)
                for doc in by_manager[url]:
                    picked = pick_visit_from_urls(
                        doc.get("manager_url"), links,
                        name=doc.get("name") or "")
                    verdict = await _commit_discovered(
                        db, doc, picked, "tinyfish_fetch", recorder=recorder)
                    if verdict == "found":
                        counters["from_fetch"] += 1
                        state.log(f"✓ {doc.get('name')} ← tinyfish_fetch")
                    else:
                        if verdict == "rejected":
                            counters["rejected_same_as_manager"] += 1
                        after_fetch.append(doc)

        if not skip_search:
            state.log(f"Search named + juge : {len(after_fetch)} site(s)")
            for doc in after_fetch:
                if getattr(state, "cancel", False):
                    state.log("Stop demandé")
                    break
                if not _needs_visit(doc):
                    continue
                picked = None
                try:
                    for query, host in search_queries(doc):
                        hits = await search(query, include_domains=host)
                        picked = await _pick_from_search(doc, hits, judge=judge)
                        if picked:
                            if doc.get("visit_url_judge"):
                                counters["judge_engine"] = doc.get("visit_url_judge")
                            break
                except Exception as exc:
                    counters["errors"] += 1
                    state.log(f"✗ Search {doc.get('name')}: {type(exc).__name__}")
                    counters["unchanged"] += 1
                    continue
                verdict = await _commit_discovered(
                    db, doc, picked, "tinyfish_search", recorder=recorder)
                if verdict == "found":
                    counters["from_search"] += 1
                    state.log(f"✓ {doc.get('name')} ← tinyfish_search")
                elif verdict == "rejected":
                    counters["rejected_same_as_manager"] += 1
                else:
                    counters["unchanged"] += 1
        else:
            counters["unchanged"] += len(after_fetch)

        summary = {
            **counters,
            "found": counters["from_links"] + counters["from_fetch"] + counters["from_search"],
            "skip_search": skip_search,
            "from_scratch": from_scratch,
            "harvest": harvest_summary,
        }
        state.summary = summary
        state.log(f"AMP visite terminé: {summary}")
        return summary
    except Exception as exc:
        state.error = f"{type(exc).__name__}: {exc}"
        state.log(f"FATAL: {state.error}")
        raise
    finally:
        await hb.aclose()
        state.finished_at = time.time()
        state.running = False
        if token is not None:
            reset_run(token)
