"""Harbormaster enrichment: tags, Search/Fetch, regex, NIM, OpenRouter.

Chain (rising cost, shared with marinas via ``run_page_enrich``):
  1. OSM/SHOM/NOAA tags
  2. Official URL; ``search_named`` only if there is none
  3. Page read (``read_url``) — stop if phone and VHF
  4. Phone / VHF-channel regex
  5. NVIDIA NIM `page` chain, then OpenRouter, if a hole remains
  6. TinyFish Agent (official site only, last resort)

The schema is not the marina one: phone and VHF only.
Never invented. An already filled field is not overwritten.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from app.core.enrich import (
    PageEnrichSpec,
    fetch_pages,
    run_page_enrich,
    url_ok,
)
from app.core.search import search_named
from app.services.capitainerie_world import (
    GENERIC_OFFICE_NAMES,
    contact_from_tags,
    contact_from_text,
    fill_contact,
)
from app.services.marina_enrich import (
    openrouter_check_credit,
)
from app.services.marina_world import official_website

ENRICH_FIELDS = ("telephone", "canal_vhf")
MAX_FETCH_URLS = 8

CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "telephone": {"type": "string"},
        "canal_vhf": {"type": "string"},
    },
}

CAPITAINERIE_PURPOSE = (
    "Official harbour master / capitainerie office contact page "
    "(telephone and VHF channel). Prefer government and port-authority domains. "
    "Ignore restaurants, hotels, Instagram, Tripadvisor."
)

CONTACT_SYSTEM = (
    "You extract harbour-master contact from the given source text. "
    "Reply ONLY with a single JSON object. Never invent a phone number or VHF channel."
)


def contact_enrich_goal(doc: dict) -> str:
    name = doc.get("name") or "harbour master's office"
    return (
        f"Find the official harbour master / capitainerie page for '{name}' "
        f"at {doc.get('lat'):.4f},{doc.get('lon'):.4f}. "
        f"Extract only: telephone (harbour master phone as printed) and "
        f"canal_vhf (VHF channel, e.g. '9' or '9/16' or '9/68'). "
        f"Return null if a field is not on the official page. Never invent values."
    )


def _normalise_contact(payload: dict | None) -> dict:
    out = {"telephone": None, "canal_vhf": None}
    if not isinstance(payload, dict):
        return out
    from app.services.capitainerie_world import _clean_phone, _clean_vhf
    tel = payload.get("telephone") or payload.get("telephone_capitainerie") or payload.get("phone")
    vhf = payload.get("canal_vhf") or payload.get("vhf") or payload.get("vhf_channel")
    out["telephone"] = _clean_phone(tel)
    out["canal_vhf"] = _clean_vhf(vhf)
    return out


def merge_contact_payload(doc: dict, incoming: dict | None) -> dict:
    """Tags / site: fill empty fields only."""
    base = {
        "telephone": doc.get("telephone"),
        "canal_vhf": doc.get("canal_vhf"),
    }
    tags_phone, tags_vhf = contact_from_tags(doc.get("tags") or {})
    fill_contact(base, tags_phone, tags_vhf)
    cleaned = _normalise_contact(incoming)
    fill_contact(base, cleaned.get("telephone"), cleaned.get("canal_vhf"))
    return base


def needs_website_enrich(doc: dict) -> bool:
    phone, vhf = doc.get("telephone"), doc.get("canal_vhf")
    if not phone or not vhf:
        t_phone, t_vhf = contact_from_tags(doc.get("tags") or {})
        phone = phone or t_phone
        vhf = vhf or t_vhf
    return not (phone and vhf)


def _has_distinct_name(doc: dict) -> bool:
    name = str(doc.get("name") or "").strip().lower()
    return bool(name) and name not in GENERIC_OFFICE_NAMES


def _has_coords(doc: dict) -> bool:
    try:
        lat, lon = float(doc["lat"]), float(doc["lon"])
    except (TypeError, ValueError, KeyError):
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def allow_web_lookup(doc: dict) -> bool:
    """Official site, distinct name, or GPS (generic "Capitainerie" label + coords)."""
    if official_website(doc):
        return True
    if (doc.get("tags") or {}).get("website"):
        return True
    if _has_distinct_name(doc):
        return True
    return _has_coords(doc)


def rank_enrich_candidates(docs: list[dict]) -> list[dict]:
    """Official site first, then distinct name, then generic with GPS."""
    def key(d: dict):
        site = 1 if official_website(d) else 0
        named = 1 if _has_distinct_name(d) else 0
        gps = 1 if _has_coords(d) else 0
        return (-site, -named, -gps, str(d.get("name") or "").lower())
    return sorted(docs, key=key)


def _coords_fragment(doc: dict) -> str:
    try:
        return f"{float(doc['lat']):.4f},{float(doc['lon']):.4f}"
    except (TypeError, ValueError, KeyError):
        return ""


def contact_search_query(doc: dict) -> str:
    """Search query: distinct name, else "capitainerie" + GPS."""
    name = str(doc.get("name") or "").strip()
    coords = _coords_fragment(doc)
    if name and name.lower() not in GENERIC_OFFICE_NAMES:
        bits = [f'"{name}"', "harbour master", "capitainerie"]
        if coords:
            bits.append(coords)
        bits.extend(["VHF", "telephone"])
        return " ".join(bits)
    bits = ["capitainerie", "harbour master"]
    if coords:
        bits.append(coords)
    bits.extend(["VHF", "telephone"])
    return " ".join(bits)


def _url_ok(url: str) -> bool:
    """Same SERP cut as top-down / AMP."""
    return url_ok(url)


def _url_rank(url: str, official: str | None) -> tuple:
    u = (url or "").lower()
    official_hit = 0
    if official:
        off = official.rstrip("/").lower()
        official_hit = 1 if off in u.rstrip("/") or u.rstrip("/") in off else 0
    host = ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    gov = 1 if any(
        token in host or token in u
        for token in (
            ".gouv.", ".gov", ".gc.ca", "port", "harbour", "harbor",
            "marina", "capitainerie", "harbourmaster",
        )
    ) else 0
    return (-official_hit, -gov)


def _contact_prompt(doc: dict, context: str) -> str:
    tags = doc.get("tags") or {}
    tags_str = json.dumps(
        {k: v for k, v in tags.items() if isinstance(v, str)},
        ensure_ascii=False,
    )
    known = {
        k: doc.get(k) for k in ENRICH_FIELDS
        if doc.get(k)
    }
    missing = [k for k in ENRICH_FIELDS if not doc.get(k)]
    return (
        "Extract harbour master / capitainerie contact. Return STRICT JSON:\n"
        "{\"telephone\": string|null, \"canal_vhf\": string|null}\n\n"
        f"Name: {doc.get('name')}\n"
        f"Coordinates: {doc.get('lat')}, {doc.get('lon')}\n"
        f"OSM/SHOM/NOAA tags: {tags_str}\n"
        f"Already known (do not replace): {json.dumps(known, ensure_ascii=False)}\n"
        f"Fill only these missing fields: {missing}\n\n"
        f"SOURCE:\n{context}\n\n"
        "RULES: null if not in the source. NEVER fabricate. "
        "Do not change Already known values. "
        "canal_vhf like '9' or '9/16' or '9/68'. telephone as printed. JSON only."
    )


async def discover_contact_urls(
    doc: dict,
    tinyfish_key: Optional[str] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """Official URL first; ``search_named`` only if there is none."""
    from app.core.enrich import collect_page_urls

    official = official_website(doc)

    async def _search() -> list[str]:
        hits = await search_named(
            contact_search_query(doc),
            key=tinyfish_key,
            purpose=CAPITAINERIE_PURPOSE,
            max_results=MAX_FETCH_URLS,
            log=logger,
        )
        ranked = sorted(
            [h for h in hits if _url_ok((h or {}).get("url") or "")],
            key=lambda h: _url_rank(h.get("url") or "", official),
        )
        return [h.get("url") for h in ranked if h.get("url")]

    return await collect_page_urls(
        official,
        extra=[(doc.get("tags") or {}).get("website")],
        search=_search,
        max_urls=MAX_FETCH_URLS,
        url_ok_fn=_url_ok,
    )


async def fetch_contact_pages(
    urls: list[str],
    tinyfish_key: Optional[str] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """Fetch first if a key; cascade (PDF / JS / HTML) as soon as text is missing."""
    return await fetch_pages(
        urls,
        purpose=CAPITAINERIE_PURPOSE,
        tinyfish_key=tinyfish_key,
        logger=logger,
    )


def contact_from_pages(pages: list[dict]) -> dict:
    phone = vhf = None
    for page in pages:
        p, v = contact_from_text(page.get("text") or "")
        phone = phone or p
        vhf = vhf or v
        if phone and vhf:
            break
    return {"telephone": phone, "canal_vhf": vhf}


async def enrich_via_nvidia(
    doc: dict,
    context: str,
    settings: dict | None,
    logger: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """`page` chain: Pro → gpt-oss → Muse. `_engine` = model actually served."""
    from app.core import nvidia
    if not context or not nvidia.nvidia_enabled(settings):
        return None
    try:
        if logger:
            logger("[nvidia] NIM page chain on harbour-master text")
        data, used = await nvidia.complete_json_nvidia_tracked(
            CONTACT_SYSTEM, _contact_prompt(doc, context), settings,
            role="page", max_tokens=200, log=logger,
        )
        cleaned = _normalise_contact(data if isinstance(data, dict) else {})
        if any(cleaned.values()):
            cleaned["_engine"] = nvidia.engine_label(used)
            if logger:
                logger(f"[nvidia] {cleaned['_engine']} "
                       f"filled={[k for k, v in cleaned.items() if v and k != '_engine']}")
            return cleaned
        if logger:
            logger("[nvidia] empty payload")
        return None
    except Exception as e:
        if logger:
            logger(f"[nvidia] {type(e).__name__}: {str(e)[:120]}")
        return None


async def enrich_via_nvidia_muse(*args, **kwargs):
    """Alias: the Muse pin was removed (`page` chain, Pro first)."""
    return await enrich_via_nvidia(*args, **kwargs)


async def enrich_via_openrouter(
    doc: dict,
    or_key: str,
    min_credit_usd: float = 0.5,
    logger: Optional[Callable[[str], None]] = None,
    context: str = "",
) -> Optional[dict]:
    from app.core.llm import openrouter_model, parse_json_flexible
    if not or_key or not context:
        return None
    async with httpx.AsyncClient() as client:
        if not await openrouter_check_credit(client, or_key, min_usd=min_credit_usd, logger=logger):
            return None
        prompt = _contact_prompt(doc, context)
        try:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {or_key}",
                    "HTTP-Referer": "https://blue-intelligence.local",
                    "X-Title": "BlueIntelligence Capitainerie Enrichment",
                },
                json={
                    "model": openrouter_model(),
                    "messages": [
                        {"role": "system", "content": CONTACT_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 200,
                    "response_format": {"type": "json_object"},
                },
                timeout=60,
            )
            if r.status_code != 200:
                if logger:
                    logger(f"[openrouter] HTTP {r.status_code}: {r.text[:120]}")
                return None
            content = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            payload = parse_json_flexible(content)
            cleaned = _normalise_contact(payload if isinstance(payload, dict) else {})
            if any(cleaned.values()):
                if logger:
                    logger(f"[openrouter] filled={[k for k, v in cleaned.items() if v]}")
                return cleaned
            if logger:
                logger("[openrouter] empty payload")
            return None
        except Exception as e:
            if logger:
                logger(f"[openrouter] {type(e).__name__}: {str(e)[:120]}")
            return None


async def enrich_via_tinyfish(
    doc: dict,
    key: str,
    logger: Optional[Callable[[str], None]] = None,
    total_budget_s: float = 180.0,
) -> Optional[dict]:
    import asyncio
    from app.core.tinyfish import tf_get_run, tf_run_async

    url_hint = official_website(doc)
    if not url_hint or not _url_ok(url_hint):
        if logger:
            logger("[tinyfish] no official website — skipping agent")
        return None
    goal = contact_enrich_goal(doc)
    try:
        if logger:
            logger(f"[tinyfish] run-async on {url_hint}")
        started = await tf_run_async(url_hint, goal, CONTACT_SCHEMA, key, max_duration_s=120)
    except Exception as e:
        if logger:
            logger(f"[tinyfish] run-async error: {type(e).__name__}: {str(e)[:120]}")
        return None
    run_id = started.get("run_id") or started.get("id")
    if not run_id:
        return None
    deadline = asyncio.get_event_loop().time() + total_budget_s
    last_status = None
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(4)
        try:
            info = await tf_get_run(run_id, key)
        except Exception as e:
            if logger:
                logger(f"[tinyfish] poll error: {type(e).__name__}: {str(e)[:80]}")
            continue
        status = info.get("status")
        if status != last_status and logger:
            logger(f"[tinyfish] status={status}")
            last_status = status
        if status in ("COMPLETED", "SUCCESS", "SUCCEEDED"):
            payload: Any = info.get("result") or info.get("output") or info
            for pk in ("result", "output", "extraction", "data"):
                if isinstance(payload, dict) and pk in payload and isinstance(payload[pk], dict):
                    payload = payload[pk]
            cleaned = _normalise_contact(payload if isinstance(payload, dict) else {})
            if any(cleaned.values()):
                return cleaned
            if logger:
                logger("[tinyfish] empty payload")
            return None
        if status in ("FAILED", "ERROR", "CANCELLED", "CANCELED"):
            if logger:
                logger(f"[tinyfish] {status}")
            return None
    if logger:
        logger(f"[tinyfish] TIMEOUT after {total_budget_s:.0f}s")
    return None


def _capitainerie_from_tags(doc: dict) -> dict:
    return merge_contact_payload(doc, None)


def _capitainerie_spec() -> PageEnrichSpec:
    return PageEnrichSpec(
        fields=ENRICH_FIELDS,
        hole_fields=ENRICH_FIELDS,
        from_tags=_capitainerie_from_tags,
        official_url=official_website,
        allow_web=allow_web_lookup,
        discover_urls=discover_contact_urls,
        fetch_pages=fetch_contact_pages,
        from_pages=contact_from_pages,
        nvidia=enrich_via_nvidia,
        openrouter=enrich_via_openrouter,
        agent=enrich_via_tinyfish,
        merge=merge_contact_payload,
        url_ok=_url_ok,
    )


async def enrich_capitainerie(
    doc: dict,
    tinyfish_key: Optional[str] = None,
    openrouter_key: Optional[str] = None,
    min_credit_usd: float = 0.5,
    logger: Optional[Callable[[str], None]] = None,
    skip_tinyfish: bool = False,
    settings: Optional[dict] = None,
) -> dict:
    return await run_page_enrich(
        doc,
        _capitainerie_spec(),
        tinyfish_key=tinyfish_key,
        openrouter_key=openrouter_key,
        min_credit_usd=min_credit_usd,
        logger=logger,
        skip_tinyfish=skip_tinyfish,
        settings=settings,
    )
