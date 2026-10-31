"""Tavily Search / Extract. Key from env TAVILY_API_KEY only.

On-disk JSON cache: sha256 of the request (never the key) under
TAVILY_CACHE_DIR, default backend/data/cache/tavily. Only answers with
results are cached; Extract entries keep only the URLs that were read.
An entry older than the run rule ``tavily.cache_ttl_days`` (default 7)
is a miss. Cache hits and misses are counted per run (``summary``).

Credits actually billed (a cache hit is free):
  basic / fast / ultra-fast search = 1, advanced search = 2,
  extract = 1 per 5 URLs read.

Budget, per run (same run key as the Token Factory ledger): a call is sent
only if its credits fit under the run rule ``tavily.max_credits``; credits
are reserved before the call and given back if it fails. At most
``MAX_CONCURRENT`` calls are in flight per event loop. ``summary(run_id)``
is stored with the run (``tavily_cost`` on PoE runs, ``summary.tavily``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import weakref
from pathlib import Path
from urllib.parse import urlparse

import httpx

SEARCH_URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"
TIMEOUT_S = 90.0
CONNECT_TIMEOUT_S = 10.0
RETRY_SLEEP_S = 2.0
EXTRACT_BATCH = 20
_DEPTHS = {"ultra-fast", "fast", "basic", "advanced"}
_SECRET_FIELDS = {"api_key", "apikey", "authorization", "token"}

# backend/app/core/tavily.py -> parents[2] = backend/
_DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "data" / "cache" / "tavily"

DEFAULT_CACHE_TTL_DAYS = 7
DEFAULT_MAX_CREDITS = 1000
MAX_CONCURRENT = 4
_CACHED_AT = "_cached_at"

CREDITS = {"search": 0, "extract": 0}  # process total; per run: summary()

_STATS: dict[str, dict] = {}
_stats_guard = threading.Lock()
_sems: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _sem() -> asyncio.Semaphore:
    """One semaphore per event loop (tests and threads run their own loops)."""
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT)
        _sems[loop] = sem
    return sem


def max_credits() -> int:
    """Run rule ``tavily.max_credits``. 0 = no paid Tavily call (cache still read)."""
    try:
        from app.core.run_rules import get_rule
        raw = get_rule("tavily.max_credits", DEFAULT_MAX_CREDITS)
    except Exception:
        raw = DEFAULT_MAX_CREDITS
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CREDITS


def _run_key(run_id: str | None = None) -> str:
    """Same per-run key as the Token Factory ledger."""
    from app.core.tokenfactory import ledger_key
    return ledger_key(run_id)


def _slot(rid: str) -> dict:
    return _STATS.setdefault(rid, {
        "cache_hits": 0, "cache_misses": 0,
        "credits_search": 0, "credits_extract": 0, "capped": 0,
    })


def _credits_used(slot: dict) -> int:
    return int(slot.get("credits_search") or 0) + int(slot.get("credits_extract") or 0)


def reserve_credits(kind: str, cost: int, run_id: str | None = None) -> bool:
    """Take ``cost`` credits for this run if they fit under ``tavily.max_credits``."""
    cost = max(0, int(cost))
    cap = max_credits()
    rid = _run_key(run_id)
    with _stats_guard:
        slot = _slot(rid)
        if _credits_used(slot) + cost > cap:
            slot["capped"] = int(slot.get("capped") or 0) + 1
            return False
        slot[f"credits_{kind}"] = int(slot.get(f"credits_{kind}") or 0) + cost
    return True


def refund_credits(kind: str, cost: int, run_id: str | None = None) -> None:
    """Give back credits reserved for a call that failed or billed less."""
    cost = max(0, int(cost))
    if not cost:
        return
    rid = _run_key(run_id)
    with _stats_guard:
        slot = _slot(rid)
        slot[f"credits_{kind}"] = max(0, int(slot.get(f"credits_{kind}") or 0) - cost)


def credits_used(run_id: str | None = None) -> int:
    with _stats_guard:
        return _credits_used(_STATS.get(_run_key(run_id)) or {})


def restore(cost: dict | None, run_id: str | None = None) -> None:
    """Resume after a restart: credits and cache counts from the stored ``summary()``,
    so the cap covers the whole run. Does nothing if this process already counted."""
    cost = cost if isinstance(cost, dict) else {}
    credits = cost.get("credits") if isinstance(cost.get("credits"), dict) else {}
    rid = _run_key(run_id)
    with _stats_guard:
        slot = _slot(rid)
        if _credits_used(slot) or slot.get("cache_hits") or slot.get("cache_misses"):
            return
        slot["credits_search"] = int(credits.get("search") or 0)
        slot["credits_extract"] = int(credits.get("extract") or 0)
        slot["cache_hits"] = int(cost.get("cache_hits") or 0)
        slot["cache_misses"] = int(cost.get("cache_misses") or 0)
        slot["capped"] = int(cost.get("calls_capped") or 0)


def _count(field: str, n: int = 1, run_id: str | None = None) -> None:
    rid = _run_key(run_id)
    with _stats_guard:
        slot = _slot(rid)
        slot[field] = slot.get(field, 0) + n


def reset_stats(run_id: str | None = None) -> None:
    with _stats_guard:
        if run_id is None:
            _STATS.clear()
        else:
            _STATS.pop(run_id, None)


def summary(run_id: str | None = None) -> dict:
    """Per-run Tavily record: cache hits / misses and hit ratio.

    One lookup = one search query or one Extract URL."""
    rid = _run_key(run_id)
    with _stats_guard:
        slot = dict(_STATS.get(rid) or {})
    hits = int(slot.get("cache_hits") or 0)
    misses = int(slot.get("cache_misses") or 0)
    total = hits + misses
    c_search = int(slot.get("credits_search") or 0)
    c_extract = int(slot.get("credits_extract") or 0)
    cap = max_credits()
    used = c_search + c_extract
    return {
        "run_id": rid,
        "provider": "tavily",
        "requests": total,
        "cache_hits": hits,
        "cache_misses": misses,
        "cache_hit_ratio": round(hits / total, 4) if total else None,
        "cache_ttl_days": cache_ttl_days(),
        "credits": {"search": c_search, "extract": c_extract, "total": used},
        "max_credits": cap,
        "budget_reached": used >= cap or int(slot.get("capped") or 0) > 0,
        "calls_capped": int(slot.get("capped") or 0),
    }


def attach(summary_doc: dict, run_id: str) -> dict:
    summary_doc["tavily"] = summary(run_id)
    return summary_doc


def cache_ttl_days() -> float:
    """Run rule ``tavily.cache_ttl_days``. 0 = cache neither read nor trusted."""
    try:
        from app.core.run_rules import get_rule
        raw = get_rule("tavily.cache_ttl_days", DEFAULT_CACHE_TTL_DAYS)
    except Exception:
        raw = DEFAULT_CACHE_TTL_DAYS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_CACHE_TTL_DAYS)


def tavily_api_key() -> str:
    """TAVILY_API_KEY from the environment. Empty when unset."""
    return (os.environ.get("TAVILY_API_KEY") or "").strip()


def cache_dir() -> Path:
    raw = (os.environ.get("TAVILY_CACHE_DIR") or "").strip()
    if raw:
        return Path(raw)
    return _DEFAULT_CACHE


def search_credit_cost(search_depth: str | None) -> int:
    return 2 if (search_depth or "").strip().lower() == "advanced" else 1


def extract_credit_cost(n_urls: int) -> int:
    n = max(0, int(n_urls or 0))
    if n <= 0:
        return 0
    return (n + 4) // 5


def request_cache_id(path: str, body: dict) -> str:
    """sha256 of the public request. The API key is not part of the hash."""
    payload = {"path": path, "body": _public_body(body)}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S)


def _public_body(body: dict) -> dict:
    out = {}
    for key, val in (body or {}).items():
        if str(key).lower() in _SECRET_FIELDS or val is None:
            continue
        if isinstance(val, (list, tuple)):
            if key in ("include_domains", "exclude_domains"):
                items = sorted({str(x).strip() for x in val if str(x).strip()})
            else:
                items = []
                for x in val:
                    s = str(x).strip()
                    if s and s not in items:
                        items.append(s)
            if items:
                out[key] = items
            continue
        out[key] = val
    return out


def _redact(obj):
    if isinstance(obj, dict):
        return {
            k: _redact(v) for k, v in obj.items()
            if str(k).lower() not in _SECRET_FIELDS
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _domain(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _safe_err(exc: Exception) -> str:
    name = type(exc).__name__
    msg = str(exc).replace("\n", " ")[:80]
    low = msg.lower()
    if "bearer " in low or "tvly-" in low or "api_key" in low or "authorization" in low:
        return name
    return f"{name}: {msg}" if msg else name


def _cache_path(path: str, body: dict) -> Path:
    return cache_dir() / f"{request_cache_id(path, body)}.json"


def _cacheable(path: str, payload: dict | None) -> dict | None:
    """What may be cached: a search with results, an extract with read URLs only.

    An empty answer or a failed URL can be transient; caching it would freeze it
    for every later run. ``failed_results`` is dropped, and so is any row with
    no text.
    """
    if not isinstance(payload, dict):
        return None
    rows = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
    if path == "extract":
        rows = [r for r in rows
                if (r.get("url") or "").strip()
                and (r.get("raw_content") or r.get("content"))]
        if not rows:
            return None
        out = {k: v for k, v in payload.items() if k != "failed_results"}
        out["results"] = rows
        return out
    if not rows:
        return None
    return payload


def _read_cache(path: str, body: dict) -> dict | None:
    ttl_days = cache_ttl_days()
    if ttl_days <= 0:
        return None
    dest = _cache_path(path, body)
    try:
        if not dest.is_file():
            return None
        data = json.loads(dest.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        cached_at = data.pop(_CACHED_AT, None)
        if not isinstance(cached_at, (int, float)):
            cached_at = dest.stat().st_mtime
    except Exception:
        return None
    if time.time() - float(cached_at) > ttl_days * 86400:
        return None
    # Entries written before empty answers were refused are not trusted either.
    return _cacheable(path, data)


def _write_cache(path: str, body: dict, payload: dict) -> bool:
    keep = _cacheable(path, payload)
    if keep is None:
        return False
    directory = cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = _cache_path(path, body)
    tmp = directory / f".{dest.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
    blob = dict(_redact(keep))
    blob[_CACHED_AT] = time.time()
    tmp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, dest)
    return True


def _as_domains(values) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        parts = values.split(",")
    else:
        parts = list(values)
    out = []
    for part in parts:
        s = str(part).strip().lstrip(".")
        if s and s not in out:
            out.append(s)
    return out


def _clamp_results(n, default: int = 10) -> int:
    try:
        n = int(default if n is None else n)
    except (TypeError, ValueError):
        n = default
    return max(1, min(20, n))


def _search_body(query: str, max_results: int, search_depth: str,
                 include_domains, exclude_domains) -> tuple[dict, str]:
    depth = (search_depth or "basic").strip().lower()
    if depth not in _DEPTHS:
        depth = "basic"
    body: dict = {
        "query": query,
        "max_results": _clamp_results(max_results),
        "search_depth": depth,
    }
    inc = _as_domains(include_domains)
    exc = _as_domains(exclude_domains)
    if inc:
        body["include_domains"] = inc
    if exc:
        body["exclude_domains"] = exc
    return body, depth


def _map_search(payload: dict) -> list[dict]:
    out = []
    for row in payload.get("results") or []:
        if not isinstance(row, dict):
            continue
        url = (row.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        snippet = row.get("content") if isinstance(row.get("content"), str) else ""
        if not snippet:
            snippet = row.get("snippet") if isinstance(row.get("snippet"), str) else ""
        hit = {
            "url": url,
            "domain": _domain(url),
            "title": row.get("title") or "",
            "snippet": snippet or "",
            "engine": "tavily",
        }
        score = row.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            hit["score"] = score
        out.append(hit)
    return out


def _map_extract(payload: dict) -> list[dict]:
    out = []
    for row in payload.get("results") or []:
        if not isinstance(row, dict):
            continue
        url = (row.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        text = row.get("raw_content")
        if text is None:
            text = row.get("content") or ""
        if isinstance(text, list):
            text = "\n".join(str(x) for x in text if x)
        elif not isinstance(text, str):
            text = str(text or "")
        out.append({
            "url": url,
            "raw_content": text,
            "title": row.get("title") or "",
        })
    return out


async def _request(url: str, body: dict, log) -> dict | None:
    key = tavily_api_key()
    if not key:
        return None
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    sent = _public_body(body)
    last_status = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.post(url, headers=headers, json=sent)
            last_status = resp.status_code
            if resp.status_code == 429 and attempt == 0:
                log("Tavily: 429 — retry")
                await _sleep_retry()
                continue
            if resp.status_code >= 400:
                log(f"Tavily: HTTP {resp.status_code} — ignoré")
                return None
            try:
                data = resp.json() if resp.content else {}
            except Exception:
                log("Tavily: JSON invalide")
                return None
            if not isinstance(data, dict):
                log("Tavily: réponse inattendue")
                return None
            return _redact(data)
        except Exception as exc:
            log(f"Tavily: échec ({_safe_err(exc)})")
            # One retry after a 429, or after a network error (timeout, reset).
            transient = isinstance(exc, httpx.TransportError) or last_status == 429
            if transient and attempt == 0:
                await _sleep_retry()
                continue
            return None
    return None


async def _sleep_retry() -> None:
    await asyncio.sleep(RETRY_SLEEP_S)


async def _paid_request(kind: str, url: str, body: dict, cost: int, log) -> dict | None:
    """Reserve ``cost`` credits under tavily.max_credits, then send under the
    semaphore. A failed call gives its credits back. Over the cap → None."""
    if not reserve_credits(kind, cost):
        log(f"Tavily: tavily.max_credits atteint ({credits_used()}/{max_credits()}) — appel non envoyé")
        return None
    try:
        async with _sem():
            data = await _request(url, body, log)
    except BaseException:
        refund_credits(kind, cost)
        raise
    if data is None:
        refund_credits(kind, cost)
        return None
    CREDITS[kind] += cost
    return data


async def _post(path: str, url: str, body: dict, log, cost: int) -> tuple[dict | None, bool]:
    """Return (payload, from_cache). Failures and empty answers are not cached."""
    if not tavily_api_key():
        return None, False
    cached = _read_cache(path, body)
    if cached is not None:
        _count("cache_hits")
        return cached, True
    _count("cache_misses")
    data = await _paid_request(path, url, body, cost, log)
    if data is None:
        return None, False
    try:
        _write_cache(path, body, data)
    except Exception as exc:
        log(f"Tavily: cache ignoré ({_safe_err(exc)})")
    return data, False


async def tavily_search(query: str, *, max_results: int = 10,
                        search_depth: str = "basic", include_domains=None,
                        exclude_domains=None, log=None) -> list[dict]:
    """POST /search. No key, empty query, or HTTP error → [] (no exception).

    Each hit keeps its source URL (``url``) and ``engine="tavily"``.
    """
    log = log or (lambda _m: None)
    q = (query or "").strip()
    if not q or not tavily_api_key():
        return []
    body, depth = _search_body(
        q, max_results, search_depth, include_domains, exclude_domains)
    data, from_cache = await _post(
        "search", SEARCH_URL, body, log, search_credit_cost(depth))
    if data is None:
        return []
    hits = _map_search(data)
    log(f"Tavily: {len(hits)} résultat(s)" + (" (cache)" if from_cache else ""))
    return hits


def _extract_body(url: str, depth: str) -> dict:
    """Cache key of one URL. Extract is cached per URL, so a failed URL in a
    batch is sent again next time while the URLs already read stay cached."""
    return {"urls": [url], "extract_depth": depth, "format": "markdown"}


async def tavily_extract(urls: list[str] | None, *, extract_depth: str = "basic",
                         log=None) -> list[dict]:
    """POST /extract, batches of 20. Each row keeps the source ``url``.

    No key or no http(s) URL → []. 1 credit per 5 URLs actually sent.
    Cache per URL: only the URLs not cached (or expired) are sent.
    """
    log = log or (lambda _m: None)
    if not tavily_api_key():
        return []
    clean: list[str] = []
    for raw in urls or []:
        if not isinstance(raw, str):
            continue
        url = raw.strip()
        if url.startswith("http") and url not in clean:
            clean.append(url)
    if not clean:
        return []
    depth = "advanced" if (extract_depth or "").strip().lower() == "advanced" else "basic"
    by_url: dict[str, dict] = {}
    todo: list[str] = []
    for url in clean:
        cached = _read_cache("extract", _extract_body(url, depth))
        if cached is not None:
            _count("cache_hits")
            for row in _map_extract(cached):
                by_url.setdefault(row["url"], row)
        else:
            todo.append(url)
    for i in range(0, len(todo), EXTRACT_BATCH):
        batch = todo[i:i + EXTRACT_BATCH]
        body = {"urls": batch, "extract_depth": depth, "format": "markdown"}
        _count("cache_misses", len(batch))
        reserved = extract_credit_cost(len(batch))
        data = await _paid_request("extract", EXTRACT_URL, body, reserved, log)
        if data is None:
            continue
        rows = [r for r in (data.get("results") or []) if isinstance(r, dict)]
        # Billed on URLs read, not URLs sent: give back the rest of the reservation.
        billed = extract_credit_cost(len([r for r in rows if (r.get("url") or "").strip()]))
        if billed < reserved:
            refund_credits("extract", reserved - billed)
            CREDITS["extract"] -= reserved - billed
        for row in rows:
            url = (row.get("url") or "").strip()
            if url not in batch:
                continue
            try:
                _write_cache("extract", _extract_body(url, depth), {"results": [row]})
            except Exception as exc:
                log(f"Tavily: cache ignoré ({_safe_err(exc)})")
        for row in _map_extract({"results": rows}):
            by_url.setdefault(row["url"], row)
    ordered = [by_url[u] for u in clean if u in by_url]
    return ordered + [row for u, row in by_url.items() if u not in clean]
