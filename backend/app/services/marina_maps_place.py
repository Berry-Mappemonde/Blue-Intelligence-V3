"""“Google place card” signal without the Places API and without scraping Maps.

We do not drive Maps ourselves. TinyFish Search may return a URL
already on /maps/place/. Else TinyFish Fetch opens the deterministic
search link: after JS render (the observed settle time), the card
exposes a /place/ link — or “Impossible de trouver” / can't find.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable
from urllib.parse import unquote, urlparse

from app.services.marina_world import osm_website_from_tags

PLACE_PATH_RE = re.compile(r"/maps/place/", re.I)
SEARCH_PATH_RE = re.compile(r"/maps/search/", re.I)
TOKEN_RE = re.compile(r"[a-z0-9]{3,}", re.I)
COORDS_RE = re.compile(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
DATA_COORDS_RE = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")
PLACE_URL_IN_TEXT_RE = re.compile(r"https?://(?:www\.)?google\.[^/\s\"'<>]+/maps/place/[^\s\"'<>]+", re.I)
NON_MARINA_RE = re.compile(
    r"\b(restaurant|hotel|hôtel|pizzeria|café|cafe|brasserie|nightclub|bar)\b",
    re.I,
)
MARINA_HINT_RE = re.compile(
    r"marina|port de plaisance|port of |yacht\s*(club|harbour|harbor)|harbour|harbor|nautique|ostréicole|ostreicole|capitainerie",
    re.I,
)
PLACE_KIND_BAD_RE = re.compile(
    r"\b(passerelle|parking|pont|hôtel|hotel|restaurant|mairie|église|eglise)\b",
    re.I,
)
PLACE_KIND_OK_RE = re.compile(
    r"\b(marina|port|harbour|harbor|yacht|nautique|plaisance|ostréicole|ostreicole|capitainerie)\b",
    re.I,
)
STOPWORDS = frozenset({
    "marina", "port", "ports", "harbour", "harbor", "yacht", "club",
    "bassin", "plaisance", "the", "and", "des", "les", "une", "aux",
    "sur", "mer", "sea", "bay", "du", "de", "la", "le",
})
# A named basin next to a large marina must not inherit its card.
MAX_PLACE_DISTANCE_M = 8_000

MAPS_PLACE_PURPOSE = (
    "Official Google Maps place page for a named pleasure-craft marina "
    "(maps.google.com/maps/place/...), not a restaurant, hotel or generic pin."
)

SearchFn = Callable[[dict], Awaitable[list[dict]]]
FetchFn = Callable[[dict], Awaitable[dict]]
FetchManyFn = Callable[[list[dict]], Awaitable[dict[Any, dict]]]
FETCH_BATCH = 10
CURSOR_BATCH = 50
# Fields useful for Fetch / OSM tag — not the full GeoJSON.
_PLACE_PROJ = {
    "_id": 1,
    "name": 1,
    "lat": 1,
    "lon": 1,
    "website": 1,
    "tags": 1,
    "maps_place_url": 1,
    "maps_place_status": 1,
}


def is_google_place_url(url: str | None) -> bool:
    raw = (url or "").strip()
    if not raw.startswith("http"):
        return False
    if SEARCH_PATH_RE.search(raw):
        return False
    host = (urlparse(raw).hostname or "").lower()
    if "google." not in host:
        return False
    return bool(PLACE_PATH_RE.search(raw))


def canonicalize_place_url(url: str) -> str:
    """Keep the /place/… path, cut overly long tracking queries."""
    raw = unquote(url.strip())
    parsed = urlparse(raw)
    path = parsed.path or ""
    # /maps/place/Name/@lat,lon,z  → keep up to the name + @coords if present
    cut = path
    if len(cut) > 240:
        cut = cut[:240]
    return f"https://www.google.com{cut}" if cut.startswith("/maps/place/") else raw.split("?")[0][:400]


def name_tokens(name: str | None) -> set[str]:
    raw = (name or "").lower()
    return {t for t in TOKEN_RE.findall(raw) if t not in STOPWORDS}


def _norm_phrase(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", unquote(value or "").lower()).strip()


def hit_blob(hit: dict) -> str:
    parts = [
        hit.get("title"),
        hit.get("url"),
        hit.get("snippet"),
        hit.get("description"),
    ]
    return " ".join(_norm_phrase(str(p)) for p in parts if p)


def place_slug(url: str | None) -> str:
    path = unquote(urlparse(url or "").path or "")
    parts = [p for p in path.split("/") if p]
    try:
        i = next(n for n, p in enumerate(parts) if p.lower() == "place")
        slug = parts[i + 1] if i + 1 < len(parts) else ""
    except StopIteration:
        slug = ""
    if slug.lower().startswith("data="):
        return ""
    return slug.replace("+", " ")


def place_coords(url: str | None) -> tuple[float, float] | None:
    raw = unquote(url or "")
    match = COORDS_RE.search(raw) or DATA_COORDS_RE.search(raw)
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except (TypeError, ValueError):
        return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import radians, sin, cos, sqrt, atan2
    rlat1, rlon1, rlat2, rlon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
    return 2 * 6371000 * atan2(sqrt(a), sqrt(1 - a))


def place_hit_matches(
    name: str,
    hit: dict,
    lat: float | None = None,
    lon: float | None = None,
) -> bool:
    tokens = name_tokens(name)
    if not tokens:
        return False
    blob = hit_blob(hit)
    if not blob:
        return False
    if NON_MARINA_RE.search(blob) and not MARINA_HINT_RE.search(blob):
        return False
    slug = place_slug(hit.get("url"))
    slug_norm = _norm_phrase(slug)
    if slug_norm and PLACE_KIND_BAD_RE.search(slug_norm):
        return False
    if slug_norm and not PLACE_KIND_OK_RE.search(slug_norm):
        # “La Faute-sur-Mer” / “Soubise” = the town, not the port.
        return False
    phrase = _norm_phrase(name)
    name_ok = bool(phrase and phrase in blob)
    if not name_ok:
        overlap = sum(1 for t in tokens if t in blob)
        need = 1 if len(tokens) == 1 else min(2, len(tokens))
        name_ok = overlap >= need
    if not name_ok:
        return False
    coords = place_coords(hit.get("url"))
    if coords is not None and lat is not None and lon is not None:
        from app.core.run_rules import get_rule
        max_m = float(get_rule("marinas.place_match_m", MAX_PLACE_DISTANCE_M))
        if haversine_m(float(lat), float(lon), coords[0], coords[1]) > max_m:
            return False
    return True


def pick_google_place(
    name: str,
    hits: Iterable[dict],
    lat: float | None = None,
    lon: float | None = None,
) -> str | None:
    for hit in hits or []:
        url = (hit.get("url") or "").strip()
        if not is_google_place_url(url):
            continue
        if place_hit_matches(name, hit, lat=lat, lon=lon):
            return canonicalize_place_url(url)
    return None


def place_url_from_doc(doc: dict) -> str | None:
    for raw in (
        doc.get("maps_place_url"),
        doc.get("website"),
        osm_website_from_tags(doc.get("tags") or {}),
        (doc.get("tags") or {}).get("contact:google"),
    ):
        if is_google_place_url(raw):
            return canonicalize_place_url(raw)
    return None


def google_place_query(marina: dict) -> str:
    name = (marina.get("name") or "").strip()
    lat, lon = marina.get("lat"), marina.get("lon")
    if lat is not None and lon is not None:
        return f'"{name}" marina {float(lat):.4f} {float(lon):.4f}'
    return f'"{name}" marina'


async def default_search(marina: dict) -> list[dict]:
    from app.core.tinyfish import tf_api_key, tf_search
    key = tf_api_key()
    if not key or not (marina.get("name") or "").strip():
        return []
    return await tf_search(
        google_place_query(marina),
        key,
        purpose=MAPS_PLACE_PURPOSE,
        log=None,
    )


def _link_url(raw) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        return str(raw.get("url") or raw.get("href") or "").strip()
    return ""


def place_hits_from_fetch(rec: dict | None) -> list[dict]:
    """/maps/place/ links exposed by the Maps page once JS has rendered."""
    rec = rec or {}
    urls: list[str] = []
    seen: set[str] = set()
    candidates = [rec.get("final_url"), *(rec.get("links") or [])]
    for raw in candidates:
        url = _link_url(raw)
        if url and url not in seen and is_google_place_url(url):
            seen.add(url)
            urls.append(url)
    for match in PLACE_URL_IN_TEXT_RE.findall(rec.get("text") or ""):
        if match not in seen and is_google_place_url(match):
            seen.add(match)
            urls.append(match)
    snippet = (rec.get("text") or "")[:400]
    title = rec.get("title") or ""
    return [{"url": u, "title": title, "snippet": snippet} for u in urls]


def _maps_search_url(marina: dict) -> str | None:
    from app.services.marina_world import google_maps_url
    name = (marina.get("name") or "").strip()
    lat, lon = marina.get("lat"), marina.get("lon")
    if not name or lat is None or lon is None:
        return None
    return google_maps_url(name, float(lat), float(lon))


async def default_fetch(marina: dict) -> dict:
    from app.core.tinyfish import tf_api_key, tf_fetch
    key = tf_api_key()
    url = _maps_search_url(marina)
    if not key or not url:
        return {}
    recs = await tf_fetch([url], key, purpose=MAPS_PLACE_PURPOSE, log=None)
    return recs.get(url) or next(iter(recs.values()), {}) or {}


async def default_fetch_many(marinas: list[dict]) -> dict[Any, dict]:
    """One Fetch POST for up to 10 Maps links — 150 URL/min cadence."""
    from app.core.tinyfish import tf_api_key, tf_fetch
    key = tf_api_key()
    if not key:
        return {}
    url_by_id: dict[Any, str] = {}
    urls: list[str] = []
    for marina in marinas:
        url = _maps_search_url(marina)
        if not url:
            continue
        url_by_id[marina["_id"]] = url
        urls.append(url)
    recs = await tf_fetch(urls, key, purpose=MAPS_PLACE_PURPOSE, log=None)
    out: dict[Any, dict] = {}
    for mid, url in url_by_id.items():
        out[mid] = recs.get(url) or {}
    return out


async def resolve_google_place(
    marina: dict,
    *,
    search_fn: SearchFn | None = None,
    fetch_fn: FetchFn | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Return the $set patch. Do not invent a /search/ URL.
    unnamed → skipped. Search/Fetch without /place/ → none.
    """
    checked = now_iso or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    empty = {
        "maps_place_url": None,
        "maps_place_status": "none",
        "maps_place_source": None,
        "maps_place_checked_at": checked,
    }
    name = (marina.get("name") or "").strip()
    if not name:
        return {**empty, "maps_place_status": "skipped_unnamed"}
    tagged = place_url_from_doc({**marina, "maps_place_url": None})
    if tagged:
        return {
            "maps_place_url": tagged,
            "maps_place_status": "found",
            "maps_place_source": "osm_tag",
            "maps_place_checked_at": checked,
        }
    do_search = search_fn if search_fn is not None else default_search
    hits = await do_search(marina)
    picked = pick_google_place(name, hits, lat=marina.get("lat"), lon=marina.get("lon"))
    if picked:
        return {
            "maps_place_url": picked,
            "maps_place_status": "found",
            "maps_place_source": "tinyfish_search",
            "maps_place_checked_at": checked,
        }
    # Tests: search_fn without fetch_fn → no network call.
    if fetch_fn is None and search_fn is not None:
        return empty
    rec = await (fetch_fn or default_fetch)(marina)
    picked = pick_google_place(
        name, place_hits_from_fetch(rec), lat=marina.get("lat"), lon=marina.get("lon"),
    )
    if picked:
        return {
            "maps_place_url": picked,
            "maps_place_status": "found",
            "maps_place_source": "tinyfish_fetch",
            "maps_place_checked_at": checked,
        }
    return empty


async def revalidate_stored_places(coll) -> dict:
    kept = cleared = 0
    checked = 0
    async for marina in _iter_todo(
        coll, {"maps_place_url": {"$regex": "/maps/place/"}}, 200_000,
    ):
        checked += 1
        if stored_place_still_valid(marina):
            kept += 1
            continue
        await coll.update_one({"_id": marina["_id"]}, {"$set": {
            "maps_place_url": None,
            "maps_place_status": "none",
            "maps_place_source": None,
        }})
        cleared += 1
    return {"kept": kept, "cleared": cleared, "checked": checked}


def stored_place_still_valid(marina: dict) -> bool:
    url = marina.get("maps_place_url")
    if not is_google_place_url(url):
        return False
    return place_hit_matches(
        marina.get("name") or "",
        {"url": url, "title": place_slug(url), "snippet": url},
        lat=marina.get("lat"),
        lon=marina.get("lon"),
    )


async def apply_maps_place(coll, marina: dict, patch: dict, *,
                           run_id: str | None = None, dest_db=None) -> None:
    if run_id and dest_db is not None:
        from app.services.isolated_runs import write_item
        await write_item(
            dest_db, "marinas", run_id, {**marina, **patch},
            source_id=marina.get("source_id") or marina.get("osm_id") or marina.get("_id"),
        )
        return
    await coll.update_one({"_id": marina["_id"]}, {"$set": patch})


def _patch_from_hits(marina: dict, hits: list[dict], *, source: str, now_iso: str) -> dict:
    name = (marina.get("name") or "").strip()
    picked = pick_google_place(name, hits, lat=marina.get("lat"), lon=marina.get("lon"))
    if picked:
        return {
            "maps_place_url": picked,
            "maps_place_status": "found",
            "maps_place_source": source,
            "maps_place_checked_at": now_iso,
        }
    return {
        "maps_place_url": None,
        "maps_place_status": "none",
        "maps_place_source": None,
        "maps_place_checked_at": now_iso,
    }


async def _apply_result(coll, state, marina: dict, patch: dict, counters: dict,
                       *, run_id: str | None = None, dest_db=None) -> None:
    await apply_maps_place(coll, marina, patch, run_id=run_id, dest_db=dest_db)
    if patch["maps_place_status"] == "found":
        counters["found"] += 1
        state.log(f"✓ {marina.get('name')} → {patch['maps_place_url']}")
    else:
        counters["none"] += 1
        state.log(f"· {marina.get('name')} → pas de /place/")
    state.progress += 1


async def _count_todo(coll, q: dict, fetch_n: int, state) -> int:
    if not hasattr(coll, "count_documents"):
        return 0
    try:
        n = int(await coll.count_documents(q, maxTimeMS=8_000))
    except TypeError:
        n = int(await coll.count_documents(q))
    except Exception as exc:
        state.log(f"Comptage Mongo: {type(exc).__name__} — total à l'avancement")
        return 0
    if fetch_n < 200_000:
        n = min(n, fetch_n)
    return n


def _page_query(q: dict, last_id: Any) -> dict:
    if last_id is None:
        return q
    return {"$and": [q, {"_id": {"$gt": last_id}}]}


async def _next_page(coll, q: dict, last_id: Any, page: int) -> list[dict]:
    """Short page: the Atlas cursor is closed before TinyFish (avoids CursorNotFound)."""
    qq = _page_query(q, last_id)
    try:
        cur = coll.find(qq, _PLACE_PROJ)
    except TypeError:
        cur = coll.find(qq)
    if hasattr(cur, "sort"):
        try:
            cur = cur.sort("_id", 1)
        except Exception:
            pass
    if hasattr(cur, "limit"):
        cur = cur.limit(page)
    if hasattr(cur, "batch_size"):
        try:
            cur = cur.batch_size(page)
        except Exception:
            pass
    docs: list[dict] = []
    async for doc in cur:
        docs.append(doc)
        if len(docs) >= page:
            break
    return docs


async def _iter_todo(coll, q: dict, fetch_n: int) -> AsyncIterator[dict]:
    """Pages of CURSOR_BATCH docs. Never to_list() the whole collection."""
    last_id: Any = None
    n = 0
    while n < fetch_n:
        page = min(CURSOR_BATCH, fetch_n - n)
        docs = await _next_page(coll, q, last_id, page)
        if not docs:
            return
        for doc in docs:
            last_id = doc["_id"]
            n += 1
            yield doc
            if n >= fetch_n:
                return
        if len(docs) < page:
            return


async def resolve_maps_places(
    *,
    marinas_coll,
    state,
    limit: int = 0,
    force: bool = False,
    skip_search: bool = True,
    search_fn: SearchFn | None = None,
    fetch_fn: FetchFn | None = None,
    fetch_many_fn: FetchManyFn | None = None,
    batch_size: int = FETCH_BATCH,
    extra_filter: dict | None = None,
    run_id: str | None = None,
    dest_db=None,
) -> dict:
    """Resumable (ignore already-statused cards). World: skip Search
    (does not index /place/) and Fetch in batches of 10 (150 URL/min quota).
    """
    state.running = True
    state.started_at = time.time()
    state.finished_at = None
    state.error = None
    state.logs = []
    state.summary = None
    state.progress = 0
    state.total = 0
    state.cancel = False
    state.run_id = run_id
    state.log("Job /place/ démarré — curseur Mongo par lots (sans to_list)")

    q: dict[str, Any] = {"name": {"$nin": ["", None]}}
    if extra_filter:
        q.update(extra_filter)
    if not force:
        q["$or"] = [
            {"maps_place_status": {"$exists": False}},
            {"maps_place_status": None},
        ]

    fetch_n = int(limit) if limit and int(limit) > 0 else 200_000
    sequential = search_fn is not None or fetch_fn is not None
    size = max(1, min(FETCH_BATCH, int(batch_size or FETCH_BATCH)))
    mode = "séquentiel" if sequential else f"Fetch lots de {size}"
    state.total = await _count_todo(marinas_coll, q, fetch_n, state)
    state.log(
        f"Fiches Google : {state.total or '?'} marina(s) nommée(s) "
        f"(force={force}, skip_search={skip_search}, {mode})"
    )

    counters = {"found": 0, "none": 0, "errors": 0, "osm_tag": 0}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    selected = 0
    try:
        if sequential:
            async for marina in _iter_todo(marinas_coll, q, fetch_n):
                if getattr(state, "cancel", False):
                    state.log("Stop demandé")
                    break
                selected += 1
                if state.total < selected:
                    state.total = selected
                try:
                    patch = await resolve_google_place(
                        marina, search_fn=search_fn, fetch_fn=fetch_fn, now_iso=now,
                    )
                    await _apply_result(marinas_coll, state, marina, patch, counters,
                                       run_id=run_id, dest_db=dest_db)
                    if patch.get("maps_place_source") == "osm_tag":
                        counters["osm_tag"] += 1
                except Exception as exc:
                    counters["errors"] += 1
                    state.log(f"✗ {marina.get('name')}: {type(exc).__name__}: {str(exc)[:80]}")
                    state.progress += 1
        else:
            pending: list[dict] = []
            fetch_many = fetch_many_fn or default_fetch_many
            lot = 0

            async def flush_pending() -> None:
                nonlocal pending, lot
                if not pending:
                    return
                if getattr(state, "cancel", False):
                    pending = []
                    return
                lot += 1
                chunk = pending
                pending = []
                if lot == 1 or lot % 5 == 0:
                    state.log(
                        f"Lot Fetch {lot} ({len(chunk)} URL) — "
                        f"{state.progress}/{state.total or '?'}"
                    )
                try:
                    recs = await fetch_many(chunk)
                except Exception as exc:
                    state.log(f"Lot Fetch {lot}: {type(exc).__name__}: {str(exc)[:80]}")
                    recs = {}
                for marina in chunk:
                    try:
                        rec = recs.get(marina["_id"]) or {}
                        patch = _patch_from_hits(
                            marina, place_hits_from_fetch(rec),
                            source="tinyfish_fetch", now_iso=now,
                        )
                        await _apply_result(marinas_coll, state, marina, patch, counters,
                                       run_id=run_id, dest_db=dest_db)
                    except Exception as exc:
                        counters["errors"] += 1
                        state.log(
                            f"✗ {marina.get('name')}: {type(exc).__name__}: {str(exc)[:80]}"
                        )
                        state.progress += 1
                await asyncio.sleep(0)

            async for marina in _iter_todo(marinas_coll, q, fetch_n):
                if getattr(state, "cancel", False):
                    state.log("Stop demandé")
                    break
                selected += 1
                if state.total < selected:
                    state.total = selected
                tagged = place_url_from_doc({**marina, "maps_place_url": None})
                if tagged:
                    patch = {
                        "maps_place_url": tagged,
                        "maps_place_status": "found",
                        "maps_place_source": "osm_tag",
                        "maps_place_checked_at": now,
                    }
                    await _apply_result(marinas_coll, state, marina, patch, counters,
                                       run_id=run_id, dest_db=dest_db)
                    counters["osm_tag"] += 1
                    continue
                pending.append(marina)
                if len(pending) >= size:
                    await flush_pending()
            await flush_pending()

        summary = {
            "selected": selected,
            "found": counters["found"],
            "none": counters["none"],
            "errors": counters["errors"],
            "osm_tag": counters["osm_tag"],
            "force": force,
            "skip_search": skip_search and not sequential,
        }
        state.summary = summary
        state.log(f"Fiches Google terminé: {summary}")
        return summary
    except Exception as exc:
        state.error = f"{type(exc).__name__}: {exc}"
        state.log(f"FATAL: {state.error}")
        raise
    finally:
        state.finished_at = time.time()
        state.running = False
