"""Serper client — Google SERP JSON (organics only).

One query = 1 credit if num <= 10. Beyond that Serper counts 2 credits:
force num=10. knowledgeGraph / answerBox / peopleAlsoAsk are ignored
(they are not port sources).
"""
import asyncio
import os
from urllib.parse import urlparse

import httpx

SEARCH_URL = "https://google.serper.dev/search"
ORGANIC_NUM = 10
SEARCH_RETRY_SLEEP_S = 2.0
TIMEOUT_S = 8.0


def serper_api_key(settings=None) -> str:
    """UI settings first, else SERPER_API_KEY."""
    if settings:
        k = (settings.get("serper_api_key") or "").strip()
        if k:
            return k
    return (os.environ.get("SERPER_API_KEY") or "").strip()


def _domain(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _map_organic(payload: dict) -> list[dict]:
    out = []
    for r in payload.get("organic") or []:
        u = (r.get("link") or r.get("url") or "").strip()
        if not u.startswith("http"):
            continue
        out.append({
            "url": u,
            "domain": _domain(u),
            "title": r.get("title") or "",
            "snippet": r.get("snippet") or "",
            "engine": "serper",
        })
    return out


async def serper_search(query: str, key: str, *, gl: str | None = None,
                        hl: str = "en", log=None) -> list[dict]:
    """POST google.serper.dev/search. 400/401/402/429/timeout → [] (no exception)."""
    log = log or (lambda m: None)
    if not (key or "").strip() or not (query or "").strip():
        return []
    body = {"q": query, "num": ORGANIC_NUM}
    if gl:
        body["gl"] = str(gl).lower()
    if hl:
        body["hl"] = str(hl)
    last_status = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                r = await client.post(
                    SEARCH_URL,
                    headers={
                        "X-API-KEY": key.strip(),
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            last_status = r.status_code
            if r.status_code == 429 and attempt == 0:
                log("Serper: 429 — retry")
                await asyncio.sleep(SEARCH_RETRY_SLEEP_S)
                continue
            if r.status_code in (400, 401, 402, 403, 404, 429, 500, 503):
                snippet = (getattr(r, "text", None) or "")[:120]
                log(f"Serper: HTTP {r.status_code} — ignoré {snippet}".rstrip())
                return []
            r.raise_for_status()
            hits = _map_organic(r.json() if r.content else {})
            log(f"Serper: {len(hits)} organique(s)")
            return hits
        except Exception as e:
            log(f"Serper: échec ({type(e).__name__}: {str(e)[:80]})")
            if last_status == 429 and attempt == 0:
                await asyncio.sleep(SEARCH_RETRY_SLEEP_S)
                continue
            return []
    return []
