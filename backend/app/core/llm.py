"""
llm_core.py — LLM adapter.

JSON completions: NVIDIA (role chain) → OpenRouter → Claude last.
grounded_search stays OpenRouter (:online) — NIM has no web search.

  - ask_json / ask_text        : completions (strict JSON or free text)
  - gatekeeper_check           : marine filter (local ML then ask_yes_no)
  - extract_project            : structured marine-project extraction
  - extract_ports              : strict Ports of Entry extraction
  - llm_geocode                : intelligent geocoding (Project site)
  - llm_geocode_port           : guessed GPS for a port of entry (silent directories)
  - grounded_search            : grounded web search (:online suffix)

Keys: NVIDIA_API_KEY / OPENROUTER_API_KEY (env) or UI settings.
"""
import asyncio
import json
import os
import re
from contextvars import ContextVar
from urllib.parse import urlparse

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"
JSON_SYSTEM = ("You are the Blue Intelligence maritime OSINT engine. "
               "Reply ONLY with a single valid JSON object, no prose, no markdown fences.")

# Source text a port-extraction reader sends. The pipeline widens it for one zone
# (``llm_context_chars.set(...)``) when it keeps a raw catalog; other callers keep 20,000.
LLM_CONTEXT_CHARS = 20000
llm_context_chars: ContextVar[int] = ContextVar("llm_context_chars", default=LLM_CONTEXT_CHARS)


def reader_context(context: str | None) -> str:
    """The part of ``context`` a port-extraction reader actually sends."""
    return (context or "")[:llm_context_chars.get()]

MARINE_KW = [
    "ocean", "marine", "sea", "coral", "reef", "mangrove", "seagrass", "coastal",
    "fishery", "fisheries", "fishing", "mpa", "marine protected", "kelp", "whale",
    "shark", "turtle", "estuary", "pelagic", "aquaculture", "blue carbon", "coast",
    "seabird", "dolphin", "tide", "tidal", "island", "gulf", "bay", "archipelago", "atoll",
]
LAND_KW = [
    "mountain", "rainforest", "savanna", "desert", "grassland", "freshwater lake",
    "river basin", "alpine", "prairie", "inland forest", "terrestrial",
]


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def openrouter_model() -> str:
    return _env("OPENROUTER_MODEL") or DEFAULT_MODEL


def get_llm_key(settings: dict | None = None) -> str:
    """OpenRouter key: settings (UI) first, otherwise environment variable."""
    s = settings or {}
    return (s.get("openrouter_api_key") or _env("OPENROUTER_API_KEY")).strip()


def has_llm(settings: dict | None = None) -> bool:
    from app.core.nvidia import nvidia_enabled
    from app.core import claude
    if nvidia_enabled(settings) or bool(get_llm_key(settings)):
        return True
    return claude.claude_enabled(settings) and claude.budget_allows_call(settings)


# ---------------------------------------------------------------------------
# Robust JSON parsing
# ---------------------------------------------------------------------------
def parse_json_flexible(txt: str):
    txt = re.sub(r"^```(json)?|```$", "", (txt or "").strip(), flags=re.M).strip()
    try:
        return json.loads(txt)
    except Exception:
        pass
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    m = re.search(r"\[.*\]", txt, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Single backend: OpenRouter
# ---------------------------------------------------------------------------
async def _call_openrouter(prompt: str, system: str, key: str, model: str | None = None,
                           json_mode: bool = True, max_tokens: int = 2000,
                           retries: int = 2,
                           images: list[bytes] | None = None) -> str:
    from app.core.vision_msg import openai_user_content

    payload = {
        "model": model or openrouter_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": openai_user_content(prompt, images)},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://blueintelligence.online",
        "X-Title": "Blue Intelligence",
    }
    for attempt in range(retries):
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(OPENROUTER_URL, headers=headers, json=payload)
            if r.status_code in (429, 500, 502, 503) and attempt < retries - 1:
                await asyncio.sleep(8 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    raise RuntimeError("openrouter exhausted retries")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _public_engine(engine: str, kind: str) -> str:
    """Persisted label (gatekeeper / project extractor)."""
    if (engine or "").startswith("nightshift"):
        return f"Nightshift {kind}"
    if (engine or "").startswith("nvidia"):
        return f"NVIDIA {kind}"
    if (engine or "").startswith("tokenfactory"):
        return f"TokenFactory {kind}"
    if (engine or "").startswith("claude"):
        return f"Claude {kind}"
    return f"OpenRouter {kind}"


# Nemotron through Meterless Nightshift is the first hop of every completion.
# tokenfactory: Nebius EU host served the call (generator fallback or forced provider).
GENERATOR_PROVIDERS = ("nightshift", "nvidia", "tokenfactory")  # "sample" means no model answered
LLM_TRACE_RUN_ID = "llm-calls"  # MeterlessGenerator events: logs/trace-llm-calls.jsonl


def nightshift_model(role: str) -> str | None:
    """METERLESS_MODEL when set, else the role's specialised Nemotron (first in its chain)."""
    if _env("METERLESS_MODEL"):
        return _env("METERLESS_MODEL")
    from app.core.nvidia import CHAINS
    return next((m for m in CHAINS.get(role, CHAINS["json"]) if "nemotron" in m), None)


async def _nightshift(prompt: str, system: str, *, json_mode: bool, role: str, log=None) -> dict | None:
    """Generator result when Nightshift (or its NVIDIA fallback) served it; None otherwise."""
    from app.core.generator import generate
    try:
        result = await generate(prompt, system, json_mode=json_mode, role=role,
                                run_id=LLM_TRACE_RUN_ID, model=nightshift_model(role))
    except Exception as e:
        if log:
            log(f"Nightshift: {type(e).__name__}: {str(e)[:80]}")
        return None
    if result.get("provider") in GENERATOR_PROVIDERS:
        return result
    if log:
        log(f"Nightshift unavailable ({result.get('fallback_reason')}); NIM fallback")
    return None


def _generator_engine(result: dict) -> str:
    return f"{result['provider']}-{(result.get('model') or 'unknown').split('/')[-1]}"


async def ask_json_tracked(prompt: str, system: str = JSON_SYSTEM,
                           settings: dict | None = None,
                           max_tokens: int = 2000, log=None,
                           role: str = "json") -> tuple[dict, str]:
    """Strict JSON: Nemotron via Nightshift (generator.generate) first → NVIDIA NIM (role chain) → OpenRouter → Claude last.

    The generator result is used only when Nightshift (or its NVIDIA fallback)
    actually served it; a sample result means no model answered, so the NIM role
    chain and then the old cascade take over. The served model is in the trace.
    """
    served = await _nightshift(prompt, system, json_mode=True, role=role, log=log)
    if served is not None:
        return served["parsed"], _generator_engine(served)

    # FALLBACK: NVIDIA NIM role chain (direct NIM calls, not via Nightshift)
    from app.core.nvidia import get_nvidia_key, complete_json_nvidia_tracked, models_for
    from app.core.tokenfactory import active as tf_active, to_tf
    if tf_active(settings) or get_nvidia_key(settings):
        for model in models_for(role or "json", settings=settings):
            try:
                data, served_model = await complete_json_nvidia_tracked(
                    system, prompt, settings, model=model, role=role,
                    fallback=False, max_tokens=max_tokens)
                served = served_model or model
                if tf_active(settings) and to_tf(served):
                    return data, f"tokenfactory-{served.split('/')[-1]}"
                return data, f"nvidia-nim-{served.split('/')[-1]}"
            except Exception as e:
                if log:
                    hop = "Token Factory" if tf_active(settings) and to_tf(model) else "NVIDIA NIM"
                    log(f"{hop} {model}: {type(e).__name__}: {str(e)[:80]}")
                continue

    from app.core.judge import complete_json_cascade
    data, engine = await complete_json_cascade(
        system, prompt, settings, role=role, max_tokens=max_tokens, log=log)
    if (engine or "").startswith("claude"):
        engine = "claude"
    return data, engine


async def ask_json(prompt: str, system: str = JSON_SYSTEM, settings: dict | None = None,
                   max_tokens: int = 2000, log=None, role: str = "json") -> dict:
    """JSON strict : NVIDIA → OpenRouter → Claude en dernier."""
    data, _engine = await ask_json_tracked(
        prompt, system, settings, max_tokens=max_tokens, log=log, role=role)
    return data


async def ask_text(prompt: str, system: str = "You are a helpful assistant.",
                   settings: dict | None = None, max_tokens: int = 2000) -> str:
    """Text: Nemotron via Nightshift (generator.generate) first → NVIDIA NIM → OpenRouter."""
    served = await _nightshift(prompt, system, json_mode=False, role="text")
    if served is not None:
        return served["text"]

    # FALLBACK: NVIDIA NIM text chain
    from app.core import nvidia
    if nvidia.nvidia_enabled(settings):
        try:
            return await nvidia.complete_text_nvidia(
                system, prompt, settings, max_tokens=max_tokens)
        except Exception:
            pass
    key = get_llm_key(settings)
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY missing")
    return await _call_openrouter(prompt, system, key, json_mode=False, max_tokens=max_tokens)


# ---------------------------------------------------------------------------
# Marine gatekeeper + local ML pre-filter (weak supervision)
# ---------------------------------------------------------------------------
def heuristic_gatekeeper(text: str, settings: dict) -> dict:
    low = text.lower()
    marine = sum(low.count(k) for k in MARINE_KW)
    land = sum(low.count(k) for k in LAND_KW)
    score = marine / (marine + land * 1.5 + 1e-9) if marine else 0.0
    score = round(min(1.0, score), 3)
    accepted = marine >= 3 and s_ocean_meets_min(score, settings)
    return {"accepted": accepted, "score": score,
            "reason": f"heuristic: {marine} marine / {land} terrestrial keyword hits",
            "engine": "Heuristic Gatekeeper"}


def s_ocean_meets_min(s_ocean, settings: dict | None = None) -> bool:
    """CDC §10: the threshold cuts the S_ocean beam, after extract too."""
    try:
        score = float(s_ocean)
    except (TypeError, ValueError):
        return False
    floor = float((settings or {}).get("min_marine_score", 0.5))
    return score >= floor


async def gatekeeper_check(title: str, text: str, settings: dict) -> dict:
    # 1. Local classifier (bootstrapped on existing projects): if very
    # confident, decide without an LLM call (save credits).
    try:
        from app.core.ml import predict_relevance
        ml = predict_relevance(f"{title} {text[:2500]}")
    except Exception:
        ml = None
    if ml is not None:
        accept_at = float(settings.get("gatekeeper_accept", 0.85))
        reject_at = float(settings.get("gatekeeper_reject", 0.12))
        if ml["score"] >= accept_at:
            return {"accepted": True, "score": round(ml["score"], 3),
                    "reason": "ML gatekeeper: high-confidence marine (local model, no LLM call)",
                    "engine": "ML Gatekeeper (local)"}
        if ml["score"] <= reject_at:
            return {"accepted": False, "score": round(ml["score"], 3),
                    "reason": "ML gatekeeper: high-confidence non-marine (local model, no LLM call)",
                    "engine": "ML Gatekeeper (local)"}
    # 2. LLM (NIM → OpenRouter → Claude) or heuristic
    if not has_llm(settings):
        return heuristic_gatekeeper(f"{title} {text}", settings)
    prompt = f"""Gatekeeper Protocol: decide if this project is a MARINE/OCEAN/COASTAL conservation, restoration or protection project.
REJECT purely terrestrial (mountains, inland forests) or freshwater (lakes, rivers) projects, UNLESS it is an estuary with direct coastal impact.
Project title: {title}
Page content (truncated):
{text[:3000]}

Return JSON: {{"marine": true/false, "score": 0.0-1.0, "reason": "<short reason>"}}"""
    try:
        from app.core.judge import ask_yes_no
        yes = await ask_yes_no(
            JSON_SYSTEM, prompt, settings=settings, role="json", max_tokens=400)
        score = float((yes.raw or {}).get("score") or 0)
        accepted = bool(yes.accepted) and s_ocean_meets_min(score, settings)
        return {"accepted": accepted, "score": round(score, 3),
                "reason": (yes.reason or "")[:300],
                "engine": _public_engine(yes.engine, "Gatekeeper")}
    except Exception as e:
        res = heuristic_gatekeeper(f"{title} {text}", settings)
        res["reason"] = f"llm failed ({str(e)[:80]}), {res['reason']}"
        return res


# ---------------------------------------------------------------------------
# Structured extraction of a marine project (ex ai.extract_project)
# ---------------------------------------------------------------------------
def heuristic_extract(title: str, text: str, meta_desc: str, settings: dict) -> dict:
    desc = (meta_desc or text[:400]).strip().replace("\n", " ")
    desc = re.sub(r"\s+", " ", desc)[:250]
    gk = heuristic_gatekeeper(f"{title} {text}", settings)
    return {"title": title[:200], "description": desc, "location": None,
            "latitude": None, "longitude": None, "s_ocean": gk["score"],
            "partners": [],
            "engine": "Heuristic Extractor"}


async def extract_project(title: str, text: str, meta_desc: str, url: str, funder: str,
                          settings: dict, ext_links=None) -> dict:
    if not has_llm(settings):
        return heuristic_extract(title, text, meta_desc, settings)
    links_block = ""
    if ext_links:
        links_block = "\nExternal organization links found on the page:\n" + \
            "\n".join(f"- {l['name']}: {l['url']}" for l in ext_links[:15])
    prompt = f"""Extract structured data from this marine conservation project page.
URL: {url}
Funder: {funder}
Page title: {title}
Content (truncated):
{text[:5000]}{links_block}

Return JSON:
{{"title": "<official project name>",
 "description": "<ecological impact synthesis, WHAT and WHY, max 250 chars, do not repeat title>",
 "location": "<most specific geographic place name, e.g. 'Banc d'Arguin, Mauritania', or null if global>",
 "latitude": <decimal or null>,
 "longitude": <decimal or null>,
 "s_ocean": <0.0-1.0 relevance score: technicality + source reliability + oceanic localization>,
 "category": "<exactly one of: MPA, Conservation, Research, Fisheries, Policy & Advocacy, Pollution, Coastal & Habitat, Education, Other>",
 "partners": [<up to 3 partner/grantee MARINE conservation organizations explicitly mentioned, each {{"name": "...", "url": "<their website from the links list, or null>"}}. Empty array if none>]}}"""
    try:
        out, engine = await ask_json_tracked(prompt, settings=settings)
        return {
            "title": str(out.get("title") or title)[:200],
            "description": str(out.get("description") or "")[:250],
            "location": out.get("location"),
            "latitude": out.get("latitude"),
            "longitude": out.get("longitude"),
            "s_ocean": round(float(out.get("s_ocean") or 0.5), 3),
            "category": str(out.get("category") or "Other"),
            "partners": [p for p in (out.get("partners") or []) if isinstance(p, dict) and p.get("name")][:3],
            "engine": _public_engine(engine, "Extractor"),
        }
    except Exception:
        return heuristic_extract(title, text, meta_desc, settings)


# ---------------------------------------------------------------------------
# Strict Ports of Entry extraction
# ---------------------------------------------------------------------------
POE_EXTRACT_PROMPT = """Tu extrais les ports officiellement désignés pour l'entrée des navires étrangers dans : {name} ({sovereign}).

Réponds UNIQUEMENT avec un JSON strict de la forme:
{{"ports": [{{"name": "...", "city": "... ou null", "note": "... ou null", "lat": <decimal ou null>, "lon": <decimal ou null>, "geocodeable": true}}]}}

Règles absolues:
- Prendre tout port / terminal / harbour que la source officielle désigne comme point d'entrée :
  ports d'entrée, clearance, puertos habilitados (décret), ports of entry,
  « port of / port de / puerto de X », capitanías de puerto, delegaciones,
  designated ports, gazette, décret, liste de ports de plaisance, places of first arrival.
  Un règlement scanné (OCR) reste une source : extraire les noms malgré les fautes OCR.
  Un tableau SCT « N° Nom État Puerto/Terminal date lat lon » (sans le mot Latitud)
  est un catalogue : ne garder QUE les ports tagués turística / Turística.
  Annexe I Mayotte : uniquement les sites « Frontières maritimes » (pas l'aéroport).
  Royaume-Uni : si une liste de ports de plaisance est publiée, tous sont Ports of Entry.
  Sint Maarten : la phrase « Some examples include » de Customs.aspx est une liste
  de PoE (marinas + Greatbay harbor + Cruise Terminal) — ignorer aéroport et poste.
  Égypte : titres « X Marina: » de la page SIS yacht-tourism — ne pas inventer
  Alexandria / Port Said / Sharm s'ils ne figurent pas en titre.
  Albanie : « Porti detar X » de la kartelë akcizë / anijet e peshkimit
  (Durrës, Shëngjin, Vlorë, Sarandë) — pas Lezhë (dega doganore seulement).
  Tableaux MPI « | Nom | / Approved vessels » = places of first arrival.
  La mention « plaisance » n'est PAS exigée si l'État publie une liste officielle.
- Ne JAMAIS inventer un nom absent des extraits. Si aucun port n'est nommé : {{"ports": []}}.
- Ignorer les aéroports (sauf s'ils sont le seul point d'entrée maritime nommé — ne pas les extraire).
- "name" = nom du port tel qu'écrit. "note" en français, max 120 caractères.
- "lat"/"lon" : recopier UNIQUEMENT un nombre présent dans l'extrait. Sinon null.
  Ne jamais estimer un GPS de mémoire.
- "geocodeable": false si le nom n'est pas un toponyme (phrase, verbe, fragment).

EXTRAITS DES SOURCES OFFICIELLES:
{context}"""


_COORD_TOKEN_RE = re.compile(r"[+-]?\d+\.\d+|[+-]\d{2,}")


def _parse_coord(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number_in_text(value: float, text: str) -> bool:
    """The number (or a coherent rounding) appears in the source excerpt."""
    if not text:
        return False
    raw = f"{value:.10f}".rstrip("0").rstrip(".")
    if raw and raw in text:
        return True
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    for m in _COORD_TOKEN_RE.finditer(text):
        try:
            n = float(m.group(0))
        except ValueError:
            continue
        if decimals >= 2:
            if abs(round(n, decimals) - round(value, decimals)) <= 10 ** (-decimals):
                return True
        elif decimals == 1:
            if abs(n - value) <= 0.05:
                return True
        elif abs(n - value) < 1e-6:
            return True
    return False


def coords_appear_in_text(lat: float, lon: float, text: str) -> bool:
    """True only if lat and lon are copied from the excerpt (not invented)."""
    return _number_in_text(lat, text) and _number_in_text(lon, text)


def _as_bool_or_none(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return None


def coerce_ports(data, context: str | None = None) -> list[dict]:
    ports = data.get("ports") if isinstance(data, dict) else None
    out = []
    for p in ports or []:
        if not (isinstance(p, dict) and (p.get("name") or "").strip()):
            continue
        row = {
            "name": str(p["name"]).strip()[:120],
            "city": (str(p["city"]).strip()[:80] if p.get("city") else None),
            "note": (str(p["note"]).strip()[:200] if p.get("note") else None),
        }
        geo_flag = _as_bool_or_none(p.get("geocodeable"))
        if geo_flag is not None:
            row["geocodeable"] = geo_flag
        lat = _parse_coord(p.get("lat") if p.get("lat") is not None else p.get("latitude"))
        lon = _parse_coord(p.get("lon") if p.get("lon") is not None else p.get("longitude"))
        if lat is not None and lon is not None:
            if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                lat = lon = None
            elif context is not None and not coords_appear_in_text(lat, lon, context):
                lat = lon = None
        if lat is not None and lon is not None:
            row["lat"] = lat
            row["lon"] = lon
        out.append(row)
    return out[:150]  # high safety ceiling — no per-country cap (large maritime states)


async def _json_openrouter(prompt: str, system: str, settings: dict | None,
                           max_tokens: int,
                           images: list[bytes] | None = None) -> dict:
    key = get_llm_key(settings)
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY missing")
    raw = await _call_openrouter(
        prompt, system, key, json_mode=True, max_tokens=max_tokens,
        images=images)
    data = parse_json_flexible(raw)
    if isinstance(data, dict):
        return data
    raise RuntimeError("openrouter: no JSON in output")


async def extract_ports_openrouter(context: str, zone: dict,
                                   settings: dict | None = None, log=None) -> list[dict]:
    """OpenRouter reader only (no NIM loop-back)."""
    from app.services.poe_zone_label import search_polygon_name
    sent = reader_context(context)
    prompt = POE_EXTRACT_PROMPT.format(
        name=search_polygon_name(zone) or (zone.get("name") or zone.get("geoname") or ""),
        sovereign=zone.get("sovereign") or "",
        context=sent,
    )
    data = await _json_openrouter(
        prompt, "Tu réponds uniquement en JSON strict.", settings, 2500)
    ports = coerce_ports(data, context=context)
    for p in ports:
        p["extraction_engine"] = "openrouter"
    if log:
        log(f"LLM OpenRouter: {len(ports)} port(s) extraits sur {len(sent)} chars envoyés")
    return ports


async def extract_ports_nightshift(context: str, zone: dict, log=None) -> list[dict] | None:
    """Nemotron through Nightshift with the same prompt as OpenRouter; None if unavailable."""
    from app.services.poe_zone_label import search_polygon_name
    sent = reader_context(context)
    prompt = POE_EXTRACT_PROMPT.format(
        name=search_polygon_name(zone) or (zone.get("name") or zone.get("geoname") or ""),
        sovereign=zone.get("sovereign") or "",
        context=sent,
    )
    served = await _nightshift(prompt, "Tu réponds uniquement en JSON strict.",
                               json_mode=True, role="extract", log=log)
    if served is None:
        return None
    ports = coerce_ports(served["parsed"], context=context)
    for p in ports:
        p["extraction_engine"] = "nightshift"
        p["extraction_model"] = served.get("model")
    if log:
        log(f"LLM Nightshift ({served.get('model')}): {len(ports)} port(s) extraits "
            f"sur {len(sent)} chars envoyés")
    return ports


async def extract_ports(context: str, zone: dict, settings: dict | None = None, log=None) -> list[dict]:
    """Primary reader: Nemotron via Nightshift, then NIM, OpenRouter, Claude. The second
    reader (gpt-oss / Kimi) is orchestrated in poe_pipeline.extract_ports_llm."""
    s = settings
    if s is None:
        try:
            from app.db import get_settings
            s = await get_settings()
        except Exception:
            s = {}
    ports = await extract_ports_nightshift(context, zone, log=log)
    if ports is not None:
        return ports
    from app.core.nvidia import extract_ports_nvidia, nvidia_enabled
    from app.core import claude
    if nvidia_enabled(s):
        try:
            return await extract_ports_nvidia(context, zone, settings=s, log=log)
        except Exception as e:
            if log:
                log(f"NVIDIA extract épuisé: {type(e).__name__}: {str(e)[:100]}")
    try:
        return await extract_ports_openrouter(context, zone, settings=s, log=log)
    except Exception as e:
        if log:
            log(f"OpenRouter extract: {type(e).__name__}: {str(e)[:100]}")
    if claude.claude_enabled(s) and claude.budget_allows_call(s):
        return await claude.extract_ports_claude(context, zone, settings=s, log=log)
    raise RuntimeError("extract_ports: no backend")


# ---------------------------------------------------------------------------
# Intelligent LLM geocoding
# ---------------------------------------------------------------------------
async def llm_geocode(location: str, title: str, settings: dict):
    """Guessed GPS of a *conservation site* (reef, bay, MPA). Not a port of entry."""
    if not has_llm(settings):
        return None
    prompt = f"""You are a maritime geocoding expert. Give the best-estimate GPS coordinates for this marine conservation project site.
Project: {title}
Location description: {location or 'unknown'}

Rules: prefer the actual project site (reef, bay, MPA, coastal zone) over any city or HQ. If the location is a coastal region, return a point in the adjacent waters.
Return JSON: {{"latitude": <decimal>, "longitude": <decimal>, "confidence": <0.0-1.0>}}. If you truly cannot estimate, use confidence 0."""
    return await _parse_llm_gps(prompt, settings)


async def llm_geocode_port(port: dict, zone: dict | None = None,
                           settings: dict | None = None, log=None):
    """Guessed GPS of a *port of entry*, if Nominatim and GeoNames are silent.

    This is not ``llm_geocode`` (Project site). The caller must still require
    the point to fall in THIS VLIZ polygon — not "in France".
    """
    log = log or (lambda m: None)
    if not has_llm(settings):
        return None
    zone = zone or {}
    name = (port.get("name") or "").strip()
    if not name:
        return None
    city = (port.get("city") or "").strip()
    label = zone.get("name") or zone.get("geoname") or ""
    prompt = f"""You estimate GPS for a yacht port of entry (customs / clearance harbour), not a conservation project.

Port: {name}
City: {city or "unknown"}
THIS exclusive economic zone polygon: {label}
VLIZ mrgid: {zone.get("mrgid")}
iso2 of this polygon: {zone.get("iso2") or ""}
sovereign: {zone.get("sovereign") or ""}

Place the harbour or quay in THIS polygon only (or its coastal land, or a river port of this country).
Do not place an overseas territory on a mainland fiche, or the reverse.
If you cannot place this port in THIS polygon, use confidence 0.

Return JSON: {{"latitude": <decimal>, "longitude": <decimal>, "confidence": <0.0-1.0>}}."""
    hit = await _parse_llm_gps(prompt, settings)
    if hit:
        log(f"géocode LLM port: {name} → {hit[0]:.4f}, {hit[1]:.4f}")
    return hit


async def _parse_llm_gps(prompt: str, settings: dict | None) -> tuple[float, float] | None:
    try:
        out = await ask_json(prompt, settings=settings, max_tokens=300)
        lat, lon = float(out.get("latitude")), float(out.get("longitude"))
        from app.core.run_rules import get_rule
        min_conf = float(get_rule("shared.llm_geocode_min_confidence", 0.4))
        if float(out.get("confidence", 0)) >= min_conf and -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0):
            return lat, lon
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Grounded search: OpenRouter web search (:online suffix)
# ---------------------------------------------------------------------------
def _fallback_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


async def grounded_search(prompt: str, log=None, domain_fn=None) -> tuple[list[dict], str | None]:
    """Return (candidates [{url, domain}], text synthesis) via OpenRouter :online."""
    log = log or (lambda m: None)
    dom = domain_fn or _fallback_domain
    key = _env("OPENROUTER_API_KEY")
    if not key:
        log("grounded_search: OPENROUTER_API_KEY absente")
        return [], None
    payload = {
        "model": f"{openrouter_model()}:online",
        "messages": [{"role": "user", "content": prompt + "\n\nCite the official source URLs you used."}],
        "temperature": 0,
        "max_tokens": 1800,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(OPENROUTER_URL,
                                  headers={"Authorization": f"Bearer {key}"}, json=payload)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
    except Exception as e:
        log(f"OpenRouter :online: échec ({type(e).__name__}: {str(e)[:80]})")
        return [], None
    synthesis = msg.get("content") or ""
    urls = []
    for a in msg.get("annotations") or []:
        cit = a.get("url_citation") or {}
        u = cit.get("url") or a.get("url")
        if u and u.startswith("http"):
            urls.append(u)
    # URLs cited inline in the synthesis as a complement
    urls += re.findall(r"https?://[^\s\)\]\"']+", synthesis)
    seen, out = set(), []
    for u in urls:
        u = u.rstrip(".,;")
        d = dom(u)
        if d and d not in seen:
            seen.add(d)
            out.append({"url": u, "domain": d})
    log(f"OpenRouter :online: {len(out)} sources candidates, synthèse {len(synthesis)} chars")
    return out[:10], synthesis or None


# ---------------------------------------------------------------------------
# Nominatim vs GeoNames tie-break (one JSON call per zone / place)
# ---------------------------------------------------------------------------
def _tiebreak_user(zone: dict, items: list[dict]) -> str:
    from app.core.geo import tiebreak_user_prompt
    return tiebreak_user_prompt(zone, items)


def _tiebreak_system() -> str:
    from app.core.geo import TIEBREAK_SYSTEM
    return TIEBREAK_SYSTEM


async def arbitrate_geocode(zone: dict, items: list[dict],
                            settings: dict | None = None,
                            log=None) -> dict:
    """NVIDIA → OpenRouter → Claude. Empty dict = EEZ fallback on the pipeline side."""
    from app.core import claude, nvidia
    if not items:
        return {}
    prompt = _tiebreak_user(zone, items)
    system = _tiebreak_system()
    if nvidia.nvidia_enabled(settings):
        try:
            data = await nvidia.complete_json_nvidia(
                system, prompt, settings, role="json",
                max_tokens=400, log=log)
            picks = claude._parse_tiebreak(data, items)
            if picks:
                if log:
                    log(f"NVIDIA tiebreak: {len(items)} port(s)")
                return picks
        except Exception as e:
            if log:
                log(f"NVIDIA tiebreak: {type(e).__name__}: {str(e)[:80]}")
    if get_llm_key(settings):
        try:
            data = await _json_openrouter(
                prompt, system, settings, 400)
            picks = claude._parse_tiebreak(data, items)
            if picks:
                if log:
                    log(f"OpenRouter tiebreak: {len(items)} port(s)")
                return picks
        except Exception as e:
            if log:
                log(f"OpenRouter tiebreak: {type(e).__name__}: {str(e)[:80]}")
    return await claude.arbitrate_geocode_claude(
        zone, items, settings=settings, log=log)
