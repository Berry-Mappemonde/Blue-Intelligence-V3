"""Marina enrichment.

Chain (rising cost, shared with harbormasters via ``run_page_enrich``):
    1. OSM tags already there
    2. Official URL; ``search_named`` only if there is none
    3. Page read (``read_url``)
    4. Phone / VHF-channel regex
    5. NVIDIA NIM `page` chain, then OpenRouter, if a hole remains
    6. TinyFish Agent — official site only (never an engine hit)

Claude is not called here (claude.py contract: not marinas).

The marina schema is not the harbormaster one: visitor berths,
draft, services. An already filled field is not overwritten.

Credit guards:
    - OpenRouter: GET /v1/key before spending. Respect OPENROUTER_MIN_CREDITS
      (default 0.5 USD remaining if a spend limit is set on the key). If limit is null,
      the key is unmetered so we pass through.
    - TinyFish exposes no quota API — auth/quota HTTP errors fall through the chain.

NEVER fabricates values. Every field is null when unknown.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable, Optional

import httpx

from app.core.enrich import (
    PageEnrichSpec,
    field_present,
    fill_empty,
    run_page_enrich,
    url_ok,
)
from app.services.marina_world import official_website, osm_website_from_tags


# TinyFish's schema validator is strict: no union types, no "description" on properties,
# and no "nullable" flag either. Fields are all required-optional and we treat empty/zero
# values as nulls in _normalise_enrichment().
MARINA_ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "canal_vhf": {"type": "string"},
        "places_visiteurs": {"type": "integer"},
        "tirant_eau_max_metres": {"type": "number"},
        "score_protection_meteo": {"type": "integer"},
        "services_disponibles": {"type": "array", "items": {"type": "string"}},
        "telephone_capitainerie": {"type": "string"},
        "resume_avis": {"type": "string"},
    },
}

ENRICH_FIELDS = (
    "canal_vhf",
    "places_visiteurs",
    "tirant_eau_max_metres",
    "score_protection_meteo",
    "services_disponibles",
    "telephone_capitainerie",
    "resume_avis",
)

# Fields that justify paying a model. resume_avis / score are not enough.
HOLE_FIELDS = (
    "canal_vhf",
    "places_visiteurs",
    "tirant_eau_max_metres",
    "services_disponibles",
    "telephone_capitainerie",
)

MAX_FETCH_URLS = 5

MARINA_SITE_PURPOSE = (
    "Official marina website (harbour facilities, VHF, visitor berths, draft). "
    "Prefer the marina's own domain. Ignore Tripadvisor, Booking, Facebook, directories."
)

MARINA_SYSTEM = (
    "You extract factual marina data from the given source text. "
    "Reply ONLY with a single JSON object. Never invent a value. "
    "Do not replace fields that are already known."
)


def marina_enrich_goal(marina: dict) -> str:
    return (
        f"Find and open the official website or authoritative reference page for the marina "
        f"'{marina['name']}' located at latitude {marina['lat']:.4f}, longitude {marina['lon']:.4f}. "
        f"If you land on a directory or aggregator, drill in to the marina's own page. "
        f"From that page extract these fields:\n"
        f"- canal_vhf: the VHF radio channel (e.g. '9' or '9/16')\n"
        f"- places_visiteurs: number of visitor berths (integer)\n"
        f"- tirant_eau_max_metres: maximum draft in meters (number)\n"
        f"- score_protection_meteo: weather protection on a 1-5 scale (integer, 1=exposed, 5=fully sheltered)\n"
        f"- services_disponibles: short French labels (e.g. 'eau','électricité','carburant',"
        f"'douches','wifi','capitainerie','grue','carénage','restaurant')\n"
        f"- telephone_capitainerie: harbour master phone (string, keep as printed)\n"
        f"- resume_avis: two-sentence review summary in French, max 200 characters.\n\n"
        f"Return null for any field not present on the page. Never invent values. "
        f"Do not just repeat OSM tag content — extract from the marina's own site."
    )


# ------------------------------------------------------------------
# Credit guard
# ------------------------------------------------------------------
async def openrouter_check_credit(
    client: httpx.AsyncClient,
    key: str,
    min_usd: float = 0.5,
    logger: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Return True if the OpenRouter key has enough remaining credit (or is unmetered).
    Silent False on any HTTP or parsing error.
    """
    try:
        r = await client.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        if r.status_code != 200:
            if logger:
                logger(f"[credit-guard] OpenRouter GET /v1/key → HTTP {r.status_code}, treating as no credit")
            return False
        data = (r.json().get("data") or {})
        limit = data.get("limit")
        usage = float(data.get("usage") or 0)
        if limit is None:
            if logger:
                logger(f"[credit-guard] OpenRouter limit=null (unmetered), usage=${usage:.6f} — PASS")
            return True
        remaining = float(limit) - usage
        ok = remaining >= min_usd
        if logger:
            logger(
                f"[credit-guard] OpenRouter limit=${limit}, used=${usage:.4f}, "
                f"remaining=${remaining:.4f}, min=${min_usd} → {'PASS' if ok else 'BLOCK'}"
            )
        return ok
    except Exception as e:
        if logger:
            logger(f"[credit-guard] OpenRouter error: {type(e).__name__}: {str(e)[:120]}")
        return False


def marina_site_query(marina: dict) -> str:
    """Marina-mode query — not a port-of-entry query."""
    name = str(marina.get("name") or "").strip()
    bits = [f'"{name}"' if name else "marina", "marina", "harbour", "port"]
    try:
        bits.append(f"{float(marina['lat']):.4f},{float(marina['lon']):.4f}")
    except (TypeError, ValueError, KeyError):
        pass
    return " ".join(bits)


async def duckduckgo_html_search(
    query: str,
    client: httpx.AsyncClient | None = None,
    max_results: int = 3,
) -> list[dict]:
    """Back-compat: the DDG net lives in ``app.core.search``."""
    from app.core.search import duckduckgo_html_search as _ddg
    return await _ddg(query, client=client, max_results=max_results)


def allow_marina_web(doc: dict) -> bool:
    if official_website(doc):
        return True
    if str(doc.get("name") or "").strip():
        return True
    try:
        lat, lon = float(doc["lat"]), float(doc["lon"])
    except (TypeError, ValueError, KeyError):
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


async def discover_marina_urls(
    marina: dict,
    tinyfish_key: Optional[str] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """OSM tag first; else ``search_named`` (TinyFish, DDG if no key)."""
    from app.core.enrich import collect_page_urls
    from app.core.search import search_named

    async def _search() -> list[str]:
        hits = await search_named(
            marina_site_query(marina),
            key=tinyfish_key,
            purpose=MARINA_SITE_PURPOSE,
            max_results=MAX_FETCH_URLS,
            log=logger,
        )
        return [h.get("url") for h in hits if h.get("url")]

    return await collect_page_urls(
        official_website(marina),
        extra=[osm_website_from_tags(marina.get("tags") or {})],
        search=_search,
        max_urls=MAX_FETCH_URLS,
        url_ok_fn=url_ok,
    )


async def resolve_marina_website(
    marina: dict,
    *,
    key: str | None = None,
    logger: Optional[Callable[[str], None]] = None,
) -> str | None:
    """OSM tag first; else ``search_named`` (TinyFish, DDG if no key)."""
    urls = await discover_marina_urls(marina, tinyfish_key=key, logger=logger)
    picked = urls[0] if urls else None
    if picked and logger:
        logger(f"[search] picked {picked}")
    return picked


async def fetch_marina_pages(
    urls: list[str],
    tinyfish_key: Optional[str] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    from app.core.enrich import fetch_pages
    return await fetch_pages(
        urls,
        purpose=MARINA_SITE_PURPOSE,
        tinyfish_key=tinyfish_key,
        logger=logger,
    )


async def fetch_readable(url: str, client: httpx.AsyncClient | None = None,
                         timeout: int = 20) -> tuple[str, str]:
    """Text of a URL via the single door (PDF / HTML / JS cascade).

    ``client`` is ignored: the cascade handles httpx, PDF and the browser.
    Kept for the marina enricher call contract.
    """
    pages = await fetch_marina_pages([url], tinyfish_key=None)
    if not pages:
        return "", ""
    page = pages[0]
    return (page.get("title") or "", (page.get("text") or "")[:6000])


def marina_from_pages(pages: list[dict]) -> dict:
    """Phone / VHF by a simple rule — not berths or draft."""
    from app.core.nav_rule import should_write_vhf
    from app.services.capitainerie_world import contact_from_text
    phone = vhf = None
    for page in pages or []:
        text = page.get("text") or ""
        p, v = contact_from_text(text)
        phone = phone or p
        if v and should_write_vhf(text, v):
            vhf = vhf or v
        if phone and vhf:
            break
    return {"telephone_capitainerie": phone, "canal_vhf": vhf}


def merge_marina_payload(doc: dict, incoming: dict | None) -> dict:
    base = {k: doc.get(k) for k in ENRICH_FIELDS}
    tagged = enrich_from_osm_tags(doc)
    base = fill_empty(base, tagged, ENRICH_FIELDS)
    cleaned = _normalise_enrichment(incoming or {})
    return fill_empty(base, cleaned, ENRICH_FIELDS)


# ------------------------------------------------------------------
# TinyFish path
# ------------------------------------------------------------------
async def enrich_via_tinyfish(
    marina: dict,
    key: str,
    logger: Optional[Callable[[str], None]] = None,
    total_budget_s: float = 210.0,
) -> Optional[dict]:
    """
    Use TinyFish run-async + poll (rather than run-sync) so we can bound the wait budget precisely.
    """
    from app.core.tinyfish import tf_get_run, tf_run_async

    url_hint = official_website(marina)
    if not url_hint or not url_ok(url_hint):
        if logger:
            logger("[tinyfish] no official website — skipping agent")
        return None
    goal = marina_enrich_goal(marina)
    try:
        if logger:
            logger(f"[tinyfish] run-async on {url_hint} (server cap 120s)")
        started = await tf_run_async(url_hint, goal, MARINA_ENRICH_SCHEMA, key, max_duration_s=120)
    except Exception as e:
        # Body/message often carries what TinyFish rejected
        detail = ""
        if hasattr(e, "response") and e.response is not None:  # httpx.HTTPStatusError
            try:
                detail = e.response.text[:200]
            except Exception:
                pass
        if logger:
            logger(f"[tinyfish] run-async error: {type(e).__name__}: {str(e)[:120]} {detail}")
        return None
    run_id = started.get("run_id") or started.get("id")
    if not run_id:
        if logger:
            logger(f"[tinyfish] no run_id in response: {str(started)[:150]}")
        return None
    if logger:
        logger(f"[tinyfish] run_id={run_id}, polling every 4s")

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
            if isinstance(payload, dict) and any(payload.get(k) is not None for k in ENRICH_FIELDS):
                filled = [k for k in ENRICH_FIELDS if payload.get(k) not in (None, "", 0, 0.0, [])]
                if logger:
                    logger(f"[tinyfish] returned useful payload: filled={filled}")
                return _normalise_enrichment(payload)
            if logger:
                logger("[tinyfish] returned empty/unusable payload")
            return None
        if status in ("FAILED", "ERROR", "CANCELLED", "CANCELED"):
            if logger:
                err = str(info.get("error") or info.get("message"))[:150]
                logger(f"[tinyfish] {status}: {err}")
            return None
    if logger:
        logger(f"[tinyfish] TIMEOUT after {total_budget_s:.0f}s (last status={last_status})")
    return None


def _known_and_missing(marina: dict) -> tuple[dict, list[str]]:
    known = {k: marina.get(k) for k in ENRICH_FIELDS if field_present(marina.get(k))}
    missing = [k for k in ENRICH_FIELDS if not field_present(marina.get(k))]
    return known, missing


def _marina_prompt(marina: dict, context: str) -> str:
    tags = marina.get("tags") or {}
    tags_str = json.dumps(
        {k: v for k, v in tags.items() if isinstance(v, str)},
        ensure_ascii=False,
    )
    known, missing = _known_and_missing(marina)
    return (
        "You are extracting factual marina information. Return STRICT JSON matching this schema:\n"
        "{\"canal_vhf\": string|null, \"places_visiteurs\": integer|null, "
        "\"tirant_eau_max_metres\": number|null, \"score_protection_meteo\": integer|null (1-5), "
        "\"services_disponibles\": string[]|null, \"telephone_capitainerie\": string|null, "
        "\"resume_avis\": string|null}\n\n"
        f"Marina: {marina.get('name')}\n"
        f"Coordinates: {marina.get('lat')}, {marina.get('lon')}\n"
        f"OSM tags: {tags_str}\n"
        f"Already known (do not replace): {json.dumps(known, ensure_ascii=False)}\n"
        f"Fill only these missing fields: {missing}\n\n"
        f"SOURCE:\n{context}\n\n"
        "RULES:\n"
        "- Set a field to null if not present in the source text. NEVER fabricate.\n"
        "- Do not change Already known values; omit them or repeat them unchanged.\n"
        "- services_disponibles must be short French labels: eau, électricité, carburant, "
        "douches, wifi, capitainerie, grue, carénage, restaurant, pumpout, déchets.\n"
        "- resume_avis in French, max 200 characters, only if the source text supports it.\n"
        "- score_protection_meteo: only if the source text discusses shelter/weather — else null.\n"
        "Return only the JSON object. No markdown. No commentary."
    )


# ------------------------------------------------------------------
# OpenRouter path
# ------------------------------------------------------------------
async def enrich_via_openrouter(
    marina: dict,
    or_key: str,
    model: str | None = None,
    min_credit_usd: float = 0.5,
    logger: Optional[Callable[[str], None]] = None,
    context: str = "",
) -> Optional[dict]:
    from app.core.llm import openrouter_model, parse_json_flexible
    model = model or openrouter_model()
    if not or_key or not context:
        return None
    async with httpx.AsyncClient() as client:
        if not await openrouter_check_credit(client, or_key, min_usd=min_credit_usd, logger=logger):
            return None
        prompt = _marina_prompt(marina, context)
        try:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {or_key}",
                    "HTTP-Referer": "https://blue-intelligence.local",
                    "X-Title": "BlueIntelligence Marina Enrichment",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": MARINA_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 600,
                    "response_format": {"type": "json_object"},
                },
                timeout=60,
            )
            if r.status_code != 200:
                if logger:
                    logger(f"[openrouter] chat/completions HTTP {r.status_code}: {r.text[:120]}")
                return None
            body = r.json()
            content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            data = parse_json_flexible(content)
            usage = body.get("usage") or {}
            if logger:
                logger(
                    f"[openrouter] OK model={model} "
                    f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')} "
                    f"cost≈${(usage.get('total_cost') or usage.get('cost') or 0):.6f}"
                )
            cleaned = _normalise_enrichment(data if isinstance(data, dict) else {})
            if any(field_present(cleaned.get(k)) for k in ENRICH_FIELDS):
                return cleaned
            if logger:
                logger("[openrouter] empty payload")
            return None
        except Exception as e:
            if logger:
                logger(f"[openrouter] error: {type(e).__name__}: {str(e)[:150]}")
            return None


# ------------------------------------------------------------------
# OSM-tag fallback
# ------------------------------------------------------------------
def enrich_from_osm_tags(marina: dict) -> dict:
    """Zero-cost: what OSM already knows. Never fabricates."""
    from app.services.capitainerie_world import contact_from_tags
    t = marina.get("tags") or {}
    phone, vhf = contact_from_tags(t)

    def _to_int(v):
        try:
            m = re.search(r"-?\d+", str(v))
            return int(m.group()) if m else None
        except Exception:
            return None

    def _to_float(v):
        try:
            m = re.search(r"[-+]?\d*\.?\d+", str(v))
            return float(m.group()) if m else None
        except Exception:
            return None

    services = []
    for tag, label in [
        ("shower", "douches"),
        ("toilets", "toilettes"),
        ("drinking_water", "eau"),
        ("electricity", "électricité"),
        ("fuel", "carburant"),
        ("pumpout", "pumpout"),
        ("waste_disposal", "déchets"),
        ("wifi", "wifi"),
        ("internet_access", "wifi"),
        ("shop", "commerce"),
        ("restaurant", "restaurant"),
    ]:
        val = t.get(tag)
        if val and str(val).lower() not in ("no", "false", "0"):
            services.append(label)

    return {
        "canal_vhf": vhf,
        "places_visiteurs": _to_int(
            t.get("capacity") or t.get("capacity:persons") or t.get("seamark:harbour:capacity")
        ),
        "tirant_eau_max_metres": _to_float(
            t.get("max_depth") or t.get("depth") or t.get("seamark:harbour:draught")
        ),
        "score_protection_meteo": None,
        "services_disponibles": services or None,
        "telephone_capitainerie": phone,
        "resume_avis": None,
    }


def marina_from_tags(marina: dict) -> dict:
    """Doc fields already there, then OSM tags into the empties."""
    base = {k: marina.get(k) for k in ENRICH_FIELDS}
    return fill_empty(base, enrich_from_osm_tags(marina), ENRICH_FIELDS)


def needs_marina_enrich(marina: dict) -> bool:
    tagged = marina_from_tags(marina)
    return any(not field_present(tagged.get(k)) for k in HOLE_FIELDS)


# ------------------------------------------------------------------
# Normaliser
# ------------------------------------------------------------------
def _normalise_enrichment(payload: dict) -> dict:
    """Coerce types + trim strings; keep nulls. Empty strings and zeros are treated as nulls
    (schemas that can't express null often use these placeholders — e.g. TinyFish)."""
    out: dict[str, Any] = {}
    for k in ENRICH_FIELDS:
        v = payload.get(k) if isinstance(payload, dict) else None
        if v in ("", "null", "None", 0, 0.0):
            v = None
        if k == "canal_vhf" and v is not None:
            v = str(v).strip()[:20] or None
        elif k == "places_visiteurs" and v is not None:
            try:
                v = int(float(v))
                if v <= 0:
                    v = None
            except Exception:
                v = None
        elif k == "tirant_eau_max_metres" and v is not None:
            try:
                v = float(v)
                if v <= 0:
                    v = None
            except Exception:
                v = None
        elif k == "score_protection_meteo" and v is not None:
            try:
                v = int(v)
                if v < 1 or v > 5:
                    v = None
            except Exception:
                v = None
        elif k == "services_disponibles":
            if isinstance(v, list):
                v = [str(x).strip()[:40] for x in v if x and str(x).strip()]
                v = v or None
            elif isinstance(v, str):
                v = [x.strip() for x in re.split(r"[,;/]", v) if x.strip()] or None
            else:
                v = None
        elif k in ("telephone_capitainerie", "resume_avis") and v is not None:
            v = str(v).strip()
            if k == "resume_avis":
                v = v[:220]
            v = v or None
        out[k] = v
    return out


async def enrich_via_nvidia(
    marina: dict,
    context: str = "",
    settings: dict | None = None,
    logger: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """`page` chain: Pro → gpt-oss → Muse. `_engine` = model actually served."""
    from app.core import nvidia
    if not context or not nvidia.nvidia_enabled(settings):
        return None
    try:
        if logger:
            logger("[nvidia] NIM page chain on marina text")
        data, used = await nvidia.complete_json_nvidia_tracked(
            MARINA_SYSTEM, _marina_prompt(marina, context), settings,
            role="page", max_tokens=600, log=logger,
        )
        cleaned = _normalise_enrichment(data if isinstance(data, dict) else {})
        if any(field_present(cleaned.get(k)) for k in ENRICH_FIELDS):
            cleaned["_engine"] = nvidia.engine_label(used)
            if logger:
                logger(f"[nvidia] {cleaned['_engine']} "
                       f"filled={[k for k, v in cleaned.items() if field_present(v) and k != '_engine']}")
            return cleaned
        if logger:
            logger("[nvidia] empty payload")
        return None
    except Exception as e:
        if logger:
            logger(f"[nvidia] {type(e).__name__}: {str(e)[:120]}")
        return None


def _marina_spec() -> PageEnrichSpec:
    return PageEnrichSpec(
        fields=ENRICH_FIELDS,
        hole_fields=HOLE_FIELDS,
        from_tags=marina_from_tags,
        official_url=official_website,
        allow_web=allow_marina_web,
        discover_urls=discover_marina_urls,
        fetch_pages=fetch_marina_pages,
        from_pages=marina_from_pages,
        nvidia=enrich_via_nvidia,
        openrouter=enrich_via_openrouter,
        agent=enrich_via_tinyfish,
        merge=merge_marina_payload,
        url_ok=url_ok,
    )


# ------------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------------
async def enrich_marina(
    marina: dict,
    tinyfish_key: Optional[str] = None,
    openrouter_key: Optional[str] = None,
    min_credit_usd: float = 0.5,
    logger: Optional[Callable[[str], None]] = None,
    skip_tinyfish: bool = False,
    settings: Optional[dict] = None,
) -> dict:
    """
    Ordre commun (jumeau n°3) :
        tags → page / regex → NVIDIA → OpenRouter → Agent (site officiel).
    """
    return await run_page_enrich(
        marina,
        _marina_spec(),
        tinyfish_key=tinyfish_key,
        openrouter_key=openrouter_key,
        min_credit_usd=min_credit_usd,
        logger=logger,
        skip_tinyfish=skip_tinyfish,
        settings=settings,
    )


def is_stale(marina: dict, max_age_days: int = 365) -> bool:
    ea = marina.get("enriched_at")
    if not ea:
        return False
    try:
        t = time.strptime(ea, "%Y-%m-%dT%H:%M:%SZ")
        return (time.time() - time.mktime(t)) > max_age_days * 86400
    except (ValueError, TypeError):
        return False
