"""Tavily Search / Extract. Key from env TAVILY_API_KEY only.

On-disk JSON cache: sha256 of the request (never the key) under
TAVILY_CACHE_DIR, default backend/data/cache/tavily.

Credits actually billed (a cache hit is free):
  basic / fast / ultra-fast search = 1, advanced search = 2,
  extract = 1 per 5 URLs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
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

CREDITS = {"search": 0, "extract": 0}


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


def _read_cache(path: str, body: dict) -> dict | None:
    dest = _cache_path(path, body)
    try:
        if not dest.is_file():
            return None
        data = json.loads(dest.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_cache(path: str, body: dict, payload: dict) -> None:
    directory = cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    dest = _cache_path(path, body)
    tmp = directory / f".{dest.stem}.{os.getpid()}.tmp"
    tmp.write_text(
        json.dumps(_redact(payload), ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, dest)


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
            if last_status == 429 and attempt == 0:
                await _sleep_retry()
                continue
            return None
    return None


async def _sleep_retry() -> None:
    await asyncio.sleep(RETRY_SLEEP_S)


async def _post(path: str, url: str, body: dict, log) -> tuple[dict | None, bool]:
    """Return (payload, from_cache). Failures are (None, False) and are not cached."""
    if not tavily_api_key():
        return None, False
    cached = _read_cache(path, body)
    if cached is not None:
        return cached, True
    data = await _request(url, body, log)
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
    data, from_cache = await _post("search", SEARCH_URL, body, log)
    if data is None:
        return []
    if not from_cache:
        CREDITS["search"] += search_credit_cost(depth)
    hits = _map_search(data)
    log(f"Tavily: {len(hits)} résultat(s)" + (" (cache)" if from_cache else ""))
    return hits


async def tavily_extract(urls: list[str] | None, *, extract_depth: str = "basic",
                         log=None) -> list[dict]:
    """POST /extract, batches of 20. Each row keeps the source ``url``.

    No key or no http(s) URL → []. 1 credit per 5 URLs actually sent.
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
    out: list[dict] = []
    for i in range(0, len(clean), EXTRACT_BATCH):
        batch = clean[i:i + EXTRACT_BATCH]
        body = {"urls": batch, "extract_depth": depth, "format": "markdown"}
        data, from_cache = await _post("extract", EXTRACT_URL, body, log)
        if data is None:
            continue
        if not from_cache:
            CREDITS["extract"] += extract_credit_cost(len(batch))
        out.extend(_map_extract(data))
    return out
