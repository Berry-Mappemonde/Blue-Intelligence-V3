"""
Anthropic adapter — PoE extraction only.

Strict scope:
  - Direct Messages API (not Claude via OpenRouter);
  - Haiku 4.5, no thinking and no web search;
  - explicit cache_control on the STATIC prefix (≥ 4096 tokens, 1 h TTL);
  - serial lock on all calls (the cache is readable only after the first reply);
  - enabled only if key + $ cap > 0; stop at 90% of the cap;
  - local ledger (DATA_DIR/claude_usage.json) — Anthropic does not expose the balance.

Do not import this module from grounded_search, the swarm or marinas.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
import httpx

from app.config import DATA_DIR

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
CLAUDE_HAIKU_MODEL = "claude-haiku-4-5"
CLAUDE_SONNET_MODEL = "claude-sonnet-4-5"
CLAUDE_MODEL = CLAUDE_HAIKU_MODEL  # extraction catalogue (cache Haiku)
CACHE_TTL = "1h"
MIN_CACHE_TOKENS = 4096
STOP_RATIO = 0.90
MAX_OUTPUT_TOKENS = 2500

# Haiku 4.5 — $ / million de tokens (platform.claude.com/docs pricing)
RATE_INPUT = 1.00
RATE_OUTPUT = 5.00
RATE_CACHE_READ = 0.10
RATE_CACHE_WRITE_5M = 1.25
RATE_CACHE_WRITE_1H = 2.00

# Sonnet 4.5 — ~3× Haiku (input/output/cache)
SONNET_RATE_INPUT = 3.00
SONNET_RATE_OUTPUT = 15.00
SONNET_RATE_CACHE_READ = 0.30
SONNET_RATE_CACHE_WRITE_5M = 3.75
SONNET_RATE_CACHE_WRITE_1H = 6.00

USAGE_FILE = DATA_DIR / "claude_usage.json"
USAGE_LOCK = DATA_DIR / "claude_usage.lock"

_call_lock = asyncio.Lock()


class ClaudeBudgetExhausted(RuntimeError):
    """Local cap reached (90%) or Anthropic billing refusal."""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def estimate_tokens(text: str) -> int:
    """Deliberately underestimate (4 chars/token) to guarantee the Haiku threshold."""
    return max(1, (len(text or "") + 3) // 4)


def get_anthropic_key(settings: dict | None = None) -> str:
    s = settings or {}
    return (str(s.get("anthropic_api_key") or "")
            or _env("ANTHROPIC_API_KEY")
            or _env("CLAUDE_API_KEY")).strip()


def get_claude_budget_usd(settings: dict | None = None) -> float:
    """UI if > 0, else CLAUDE_BUDGET_USD, else 0 (Claude off)."""
    s = settings or {}
    s_val = None
    raw = s.get("claude_budget_usd")
    if raw not in (None, ""):
        try:
            s_val = float(raw)
        except (TypeError, ValueError):
            s_val = None
    if s_val and s_val > 0:
        return s_val
    env_raw = _env("CLAUDE_BUDGET_USD")
    if env_raw:
        try:
            return max(0.0, float(env_raw))
        except ValueError:
            return 0.0
    return 0.0


def claude_enabled(settings: dict | None = None) -> bool:
    return bool(get_anthropic_key(settings)) and get_claude_budget_usd(settings) > 0


def _rates_for_model(model: str | None) -> tuple[float, float, float, float, float]:
    """(input, output, cache_read, cache_write_5m, cache_write_1h) $/MTok."""
    if model and "sonnet" in model:
        return (SONNET_RATE_INPUT, SONNET_RATE_OUTPUT, SONNET_RATE_CACHE_READ,
                SONNET_RATE_CACHE_WRITE_5M, SONNET_RATE_CACHE_WRITE_1H)
    return (RATE_INPUT, RATE_OUTPUT, RATE_CACHE_READ,
            RATE_CACHE_WRITE_5M, RATE_CACHE_WRITE_1H)


def estimate_cost_usd(usage: dict | None, model: str | None = None) -> float:
    """Cost from the Messages API usage block (Haiku by default)."""
    u = usage or {}
    inp_r, out_r, read_r, w5, w1h = _rates_for_model(model)
    read = int(u.get("cache_read_input_tokens") or 0)
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    creation = u.get("cache_creation") or {}
    write_1h = creation.get("ephemeral_1h_input_tokens")
    write_5m = creation.get("ephemeral_5m_input_tokens")
    if write_1h is not None or write_5m is not None:
        write_cost = int(write_1h or 0) * w1h + int(write_5m or 0) * w5
    else:
        write_cost = int(u.get("cache_creation_input_tokens") or 0) * w1h
    return (read * read_r + write_cost + inp * inp_r + out * out_r) / 1_000_000


def _empty_usage() -> dict:
    return {
        "spent_usd": 0.0,
        "calls": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "last_call_at": None,
        "last_usage": {},
    }


def load_usage() -> dict:
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        base = _empty_usage()
        base.update(data if isinstance(data, dict) else {})
        base["spent_usd"] = float(base.get("spent_usd") or 0)
        return base
    except Exception:
        return _empty_usage()


def _write_usage(doc: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = USAGE_LOCK
    lock_path.touch(exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            USAGE_FILE.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def record_usage(api_usage: dict | None, cost_usd: float | None = None) -> dict:
    cost = float(cost_usd if cost_usd is not None else estimate_cost_usd(api_usage))
    u = api_usage or {}
    doc = load_usage()
    doc["spent_usd"] = round(float(doc.get("spent_usd") or 0) + cost, 6)
    doc["calls"] = int(doc.get("calls") or 0) + 1
    doc["cache_read_tokens"] = int(doc.get("cache_read_tokens") or 0) + int(
        u.get("cache_read_input_tokens") or 0)
    doc["cache_write_tokens"] = int(doc.get("cache_write_tokens") or 0) + int(
        u.get("cache_creation_input_tokens") or 0)
    doc["input_tokens"] = int(doc.get("input_tokens") or 0) + int(u.get("input_tokens") or 0)
    doc["output_tokens"] = int(doc.get("output_tokens") or 0) + int(u.get("output_tokens") or 0)
    doc["last_call_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc["last_usage"] = {
        "cache_read_input_tokens": u.get("cache_read_input_tokens") or 0,
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens") or 0,
        "input_tokens": u.get("input_tokens") or 0,
        "output_tokens": u.get("output_tokens") or 0,
        "cost_usd": round(cost, 6),
    }
    _write_usage(doc)
    return doc


def budget_allows_call(settings: dict | None = None) -> bool:
    if not claude_enabled(settings):
        return False
    budget = get_claude_budget_usd(settings)
    spent = float(load_usage().get("spent_usd") or 0)
    from app.core.run_rules import get_rule
    stop = float(get_rule("shared.claude_stop_ratio", STOP_RATIO))
    return spent < budget * stop


def usage_public(settings: dict | None = None) -> dict:
    from app.core.run_rules import get_rule
    doc = load_usage()
    budget = get_claude_budget_usd(settings)
    spent = float(doc.get("spent_usd") or 0)
    return {
        "claude_enabled": claude_enabled(settings),
        "claude_budget_usd": budget,
        "claude_spend_usd": round(spent, 6),
        "claude_calls": int(doc.get("calls") or 0),
        "claude_cache_read_tokens": int(doc.get("cache_read_tokens") or 0),
        "claude_cache_write_tokens": int(doc.get("cache_write_tokens") or 0),
        "claude_stop_ratio": float(get_rule("shared.claude_stop_ratio", STOP_RATIO)),
        "claude_remaining_usd": round(max(0.0, budget - spent), 6) if budget else 0.0,
        "claude_allows_call": budget_allows_call(settings),
        "claude_model": CLAUDE_MODEL,
        "claude_haiku_model": CLAUDE_HAIKU_MODEL,
        "claude_sonnet_model": CLAUDE_SONNET_MODEL,
    }


def _is_billing_error(status: int, body: str) -> bool:
    if status == 402:
        return True
    low = (body or "").lower()
    markers = ("credit", "billing", "quota", "spend limit", "insufficient",
               "payment", "balance")
    return status in (400, 401, 403) and any(m in low for m in markers)


# ---------------------------------------------------------------------------
# Static prefix (few-shots + lexicon) — identical from one EEZ to the next
# ---------------------------------------------------------------------------
_RULES = """Tu es le moteur d'extraction Ports d'Entrée de Blue Intelligence.
Tu réponds UNIQUEMENT avec un unique objet JSON valide, sans prose, sans fences markdown.

Schéma obligatoire :
{"ports": [{"name": "...", "city": "... ou null", "note": "... ou null",
            "lat": <decimal ou null>, "lon": <decimal ou null>,
            "geocodeable": true}]}

Règles absolues :
- Extraire tout port, terminal ou harbour que la source officielle désigne comme
  point d'entrée des navires étrangers : ports d'entrée, clearance, puertos
  habilitados, ports of entry, « port of / port de / puerto de X », capitanías
  de puerto, delegaciones, designated ports, gazette, décret, arrêté,
  customs act, liste de ports de plaisance, places of first arrival,
  points de passage contrôlés (annexe I, frontières maritimes).
  Un scan OCR de Gaceta reste une source : extraire les noms malgré les fautes.
  Mexique : ignorer un port sans tag turística / Turística.
  Royaume-Uni : tout port de plaisance nommé est un Port of Entry (sPCR).
  Sint Maarten : extraire tous les exemples de la page douane (Simpsonbay,
  Port de Plaisance, Il de Sol, Cupecoy, Captain Olivers, Greatbay harbor,
  Cruise Terminal). Ignorer aéroport, poste et coastline.
  Égypte : titres de la liste SIS yacht tourism (Hurghada Marina, Marassi,
  Taba Heights, Abu Teeg / El Gouna, Porto Marina North Coast). Ne pas inventer
  d'autres ports (la liste officielle n'est pas exhaustive).
  Albanie : kartelë Dogana accise carburant — uniquement les « Porti detar »
  (Durrës, Shëngjin, Vlorë, Sarandë). Ignorer Dega Doganore Lezhë (ville, pas le port).
- La mention « plaisance » n'est PAS exigée si l'État publie une liste officielle.
- Ne JAMAIS inventer un nom absent des extraits. Si aucun port n'est nommé :
  {"ports": []}.
- Ignorer les aéroports, bureaux de poste, passages terrestres, marinas de
  plaisance non désignées (sauf source UK sPCR : tout port de plaisance nommé
  est un Port of Entry), et les ports d'un AUTRE pays cités par comparaison.
- "name" = nom du port tel qu'écrit dans la source (ne pas traduire).
- "city" = ville ou entité fédérative si elle figure dans la source, sinon null.
- "note" en français, max 120 caractères.
- "lat" / "lon" : recopier UNIQUEMENT un nombre déjà écrit dans l'extrait
  (Latitud, Longitude, GPS du décret). Sinon null. Jamais de GPS de mémoire.
- "geocodeable": false si le nom n'est pas un toponyme (phrase, verbe,
  fragment de loi). true pour un vrai nom de port / baie / ville portuaire.
- Conserver l'orthographe officielle (accents, particules, numéros de terminal).
- Un même port répété une seule fois. Plafond 150 ports.
"""

_FEW_SHOTS = """
EXEMPLES D'OR (le préfixe ci-dessous est pédagogique — n'en recopie aucun nom
dans une autre zone) :

--- Exemple A : catalogue numéroté avec coordonnées (Mexique, format SCT) ---
SOURCE:
#### 4.- Ensenada
Entidad federativa: Baja California
Latitud: 31.8522146
Longitud: -116.625788
#### 34.- Manzanillo
Entidad federativa: Colima
Latitud: 19.057546
Longitud: -104.313762
JSON attendu :
{"ports": [
  {"name": "Ensenada", "city": "Baja California", "note": "catalogue officiel",
   "lat": 31.8522146, "lon": -116.625788, "geocodeable": true},
  {"name": "Manzanillo", "city": "Colima", "note": "catalogue officiel",
   "lat": 19.057546, "lon": -104.313762, "geocodeable": true}
]}

--- Exemple A2 : tableau SCT — ne garder que l'activité turística ---
SOURCE:
PUERTOS Y TERMINALES HABILITADOS
1 Bahía Colonet
Baja California
Puerto
07/08/2006
30.96571843
-116.2804389
Tipo de actividad: Comercial
4 Ensenada
Baja California
Puerto
31/05/1974
31.8522146
-116.625788
Tipo de actividad: Comercial Pesquera Turística
[ACTIVIDAD_TURISTICA]
4 Ensenada
JSON attendu :
{"ports": [
  {"name": "Ensenada", "city": "Baja California", "note": "catalogue officiel turística",
   "lat": 31.8522146, "lon": -116.625788, "geocodeable": true}
]}

--- Exemple B : tournure légale isolée (ne pas extraire l'aéroport) ---
SOURCE:
No plant material may be imported into Niue except through the port of Alofi,
the Hanan International Airport, or the Post Office.
JSON attendu :
{"ports": [{"name": "Alofi", "city": null, "note": "port of Alofi (loi)",
            "lat": null, "lon": null, "geocodeable": true}]}

--- Exemple C : décret français, plusieurs ports, ignorer le hors-sujet ---
SOURCE:
L'entrée des navires étrangers s'effectue uniquement par les ports de Papeete,
Uturoa et Taiohae. Le terminal aérien de Faa'a n'est pas un point d'entrée
maritime. La marina de Punaiia n'est pas désignée.
JSON attendu :
{"ports": [
  {"name": "Papeete", "city": null, "note": null},
  {"name": "Uturoa", "city": null, "note": null},
  {"name": "Taiohae", "city": null, "note": null}
]}

--- Exemple D : source qui cite un pays tiers (à ignorer) ---
SOURCE:
Unlike the list published by U.S. CBP (San Diego, Los Angeles), the designated
ports of entry for this territory are Apia and Asau only.
JSON attendu :
{"ports": [
  {"name": "Apia", "city": null, "note": null},
  {"name": "Asau", "city": null, "note": null}
]}

--- Exemple E : aucun port nommé ---
SOURCE:
The Customs Act 2001 empowers the Minister to designate ports by notice in the
Gazette. No notice is reproduced on this page.
JSON attendu :
{"ports": []}

--- Exemple F : table anglaise ---
SOURCE:
Designated port | Town
Port of Spain | Port of Spain
Scarborough | Tobago
JSON attendu :
{"ports": [
  {"name": "Port of Spain", "city": "Port of Spain", "note": null},
  {"name": "Scarborough", "city": "Tobago", "note": null}
]}
"""

_GLOSSARY_ROWS = (
    ("fr", "ports d'entrée, ports désignés, décret, arrêté, douane, gazette, "
     "capitainerie, formalités d'entrée, clearance plaisance"),
    ("en", "ports of entry, designated ports, customs act, gazette, clearance, "
     "harbour, port authority, pleasure craft, foreign vessel"),
    ("es", "puertos habilitados, puertos de entrada, decreto, aduana, gaceta, "
     "capitanía, despacho, embarcaciones de recreo"),
    ("pt", "portos de entrada, portos designados, decreto, alfândega, diário "
     "oficial, capitania, despacho aduaneiro"),
    ("ar", "موانئ الدخول, مرسوم, جمارك, قائمة رسمية, سلطة الموانئ"),
    ("id", "pelabuhan masuk, pelabuhan resmi, bea cukai, keputusan, gazette"),
    ("it", "porti di ingresso, porti designati, decreto, dogana, gazzetta"),
    ("el", "λιμάνια εισόδου, τελωνείο, διάταγμα, επίσημη εφημερίδα"),
    ("tr", "giriş limanları, gümrük, kararname, resmi gazete"),
    ("ru", "порты въезда, таможня, указ, официальный список"),
    ("zh", "入境港口, 海关, 法令, 官方名单"),
    ("ja", "入国港, 税関, 政令, 官報"),
    ("de", "Eingangshäfen, Zoll, Verordnung, Bundesanzeiger"),
    ("nl", "havens van binnenkomst, douane, besluit, staatsblad"),
    ("th", "ท่าเรือเข้าเมือง, ศุลกากร, ราชกิจจานุเบกษา"),
    ("vi", "cảng nhập cảnh, hải quan, nghị định, công báo"),
    ("ko", "입국 항구, 세관, 법령, 관보"),
)


def _glossary() -> str:
    lines = [
        "LEXIQUE OFFICIEL STABLE (aide à la reconnaissance, ne jamais inventer "
        "un port à partir de ce lexique) :"
    ]
    for lang, terms in _GLOSSARY_ROWS:
        lines.append(f"- {lang}: {terms}")
    # Repeat stable details to reach the Haiku cache threshold
    # (4096 tokens) with useful content, not random padding.
    extras = [
        "Un terminal pétrolier ou minéralier n'est un PoE que s'il est nommé "
        "dans la liste officielle d'entrée des navires étrangers.",
        "« Port » suivi d'un numéro TCP/IP ou d'un protocole n'est jamais un PoE.",
        "Une marina OSM sans mention réglementaire n'est pas un PoE.",
        "Les coordonnées du décret, si présentes dans l'extrait, se recopient "
        "dans lat/lon ; un GPS absent de l'extrait reste null.",
        "Si la source mélange aéroports et ports maritimes, ne retenir que les ports maritimes.",
        "Les homonymes (Portsmouth, Victoria, Georgetown) se distinguent par la ville "
        "ou le territoire nommé dans l'extrait, jamais par connaissance mondiale.",
        "Un PDF de loi qui renvoie à un « schedule » sans le reproduire → ports [].",
        "Les îles inhabitables / bases militaires sans liste → ports [].",
        "Les noms en caractères non latins se recopient tels quels.",
        "Ne pas normaliser « Harbour » en « Harbor » ni l'inverse.",
    ]
    lines.append("PRÉCISIONS STABLES :")
    for i, extra in enumerate(extras, 1):
        lines.append(f"{i}. {extra}")
    # Long but deterministic anchor: URL / gazette patterns to recognize
    # (not a list of ports). Repeated to guarantee ≥ 4096 Haiku tokens.
    url_tokens = (
        "port-of-entry", "ports-of-entry", "puertos-habilitados",
        "puertos-de-entrada", "portos-de-entrada", "ports-entree",
        "designated-ports", "customs-act", "decreto", "gazette",
        "douane", "aduana", "alfandega", "zoll", "bea-cukai",
        "capitanias", "port-authority", "marine-notice", "official-list",
    )
    lines.append("JETONS D'URL DE LISTE OFFICIELLE (reconnaissance, pas d'extraction) :")
    for tok in url_tokens:
        lines.append(
            f"- « {tok} » dans une URL .gov / .gouv / .gob / .go indique souvent "
            f"une liste ou un PDF de désignation ; extraire uniquement les noms "
            f"présents dans l'extrait fourni, jamais depuis ce jeton."
        )
    return "\n".join(lines)


def _build_static_prefix() -> str:
    text = f"{_RULES}\n{_FEW_SHOTS}\n{_glossary()}\n"
    # Net: if a refactor shortens the prefix under the Haiku threshold,
    # pad with details already stated (stable content).
    pad_n = 0
    while estimate_tokens(text) < MIN_CACHE_TOKENS + 80:
        pad_n += 1
        text += (
            f"\nRAPPEL STABLE #{pad_n} : n'invente aucun port, ignore les aéroports, "
            f"JSON strict {{\"ports\": [...]}}, name tel que dans la source, "
            f"note en français ≤ 120 caractères, city null si absente.\n"
        )
        if pad_n > 40:
            break
    return text


STATIC_PREFIX = _build_static_prefix()


def build_extract_payload(zone: dict, context: str) -> dict:
    """Messages payload: breakpoint on the static prefix, never on the user."""
    from app.core.llm import reader_context
    from app.services.poe_zone_label import search_polygon_name
    name = search_polygon_name(zone) or (zone.get("name") or zone.get("geoname") or "")
    sovereign = zone.get("sovereign") or ""
    user_text = (
        f"Zone à extraire : {name} ({sovereign}, polygone VLIZ mrgid={zone.get('mrgid')}).\n\n"
        f"EXTRAITS DES SOURCES OFFICIELLES:\n{reader_context(context)}"
    )
    return {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0,
        "system": [
            {
                "type": "text",
                "text": STATIC_PREFIX,
                "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": user_text}],
            }
        ],
    }


def _content_text(message: dict) -> str:
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


async def extract_ports_claude(context: str, zone: dict,
                               settings: dict | None = None, log=None) -> list[dict]:
    """One serialized Haiku call. Raises ClaudeBudgetExhausted or RuntimeError."""
    if not claude_enabled(settings):
        raise RuntimeError("Claude disabled (missing key or budget is 0)")
    if not budget_allows_call(settings):
        raise ClaudeBudgetExhausted("local budget ≥ 90%")

    key = get_anthropic_key(settings)
    payload = build_extract_payload(zone, context)
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    async with _call_lock:
        if not budget_allows_call(settings):
            raise ClaudeBudgetExhausted("local budget ≥ 90% (lock)")
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=payload)
        body = r.text or ""
        if _is_billing_error(r.status_code, body):
            raise ClaudeBudgetExhausted(f"anthropic HTTP {r.status_code}")
        if r.status_code >= 400:
            raise RuntimeError(f"anthropic HTTP {r.status_code}: {body[:180]}")
        data = r.json()
        usage = data.get("usage") or {}
        cost = estimate_cost_usd(usage)
        record_usage(usage, cost)
        if log:
            cr = usage.get("cache_read_input_tokens") or 0
            cw = usage.get("cache_creation_input_tokens") or 0
            log(f"Claude {CLAUDE_MODEL}: cache_read={cr} cache_write={cw} "
                f"cost=${cost:.4f} spent=${load_usage()['spent_usd']:.4f}")

    from app.core.llm import coerce_ports, parse_json_flexible
    raw = _content_text({"content": data.get("content") or []})
    parsed = parse_json_flexible(raw)
    if parsed is None:
        raise RuntimeError("claude: no JSON in output")
    ports = coerce_ports(parsed, context=context)
    for p in ports:
        p["extraction_engine"] = "claude"
    return ports


async def complete_json_claude(system: str, user: str,
                               settings: dict | None = None, *,
                               model: str | None = None,
                               max_tokens: int = 250, log=None,
                               images: list[bytes] | None = None) -> dict:
    """JSON Messages call (judge). Haiku or Sonnet. Raises on budget / HTTP."""
    from app.core.vision_msg import claude_user_content

    if not claude_enabled(settings):
        raise RuntimeError("Claude disabled (missing key or budget is 0)")
    if not budget_allows_call(settings):
        raise ClaudeBudgetExhausted("local budget ≥ 90%")
    model = model or CLAUDE_HAIKU_MODEL
    key = get_anthropic_key(settings)
    payload = {
        "model": model,
        "max_tokens": int(max_tokens),
        "temperature": 0,
        "system": system,
        "messages": [{"role": "user", "content": claude_user_content(user, images)}],
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    async with _call_lock:
        if not budget_allows_call(settings):
            raise ClaudeBudgetExhausted("local budget ≥ 90% (lock)")
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=payload)
        body = r.text or ""
        if _is_billing_error(r.status_code, body):
            raise ClaudeBudgetExhausted(f"anthropic HTTP {r.status_code}")
        if r.status_code >= 400:
            raise RuntimeError(f"anthropic HTTP {r.status_code}: {body[:180]}")
        data = r.json()
        usage = data.get("usage") or {}
        cost = estimate_cost_usd(usage, model=model)
        record_usage(usage, cost)
        if log:
            log(f"Claude {model}: cost=${cost:.4f} "
                f"spent=${load_usage()['spent_usd']:.4f}")
    from app.core.llm import parse_json_flexible
    raw = _content_text({"content": data.get("content") or []})
    parsed = parse_json_flexible(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("claude: no JSON in output")
    return parsed


def _parse_tiebreak(parsed, items: list[dict]) -> dict:
    """Map name → choice. Original keys + casefold."""
    allowed = {"nominatim", "geonames", "none"}
    names = {(it.get("name") or "").strip() for it in items if it.get("name")}
    out: dict[str, str] = {}
    for pick in (parsed or {}).get("picks") or []:
        if not isinstance(pick, dict):
            continue
        n = (pick.get("name") or "").strip()
        c = (pick.get("choice") or "").strip().lower()
        if c not in allowed:
            continue
        if n in names or n.casefold() in {x.casefold() for x in names}:
            out[n] = c
            out[n.casefold()] = c
    return out


async def arbitrate_geocode_claude(zone: dict, items: list[dict],
                                   settings: dict | None = None,
                                   log=None) -> dict:
    """One Haiku call per zone: Nominatim vs GeoNames, no 3rd GPS.

    items = [{name, nominatim: [lat, lon], geonames: [lat, lon]}, ...]
    Return {name: 'nominatim'|'geonames'|'none'} (also casefold).
    Empty dict if Claude is off, budget, or parse fails (EEZ fallback on the pipeline).
    """
    from app.core.geo import TIEBREAK_SYSTEM, tiebreak_user_prompt

    if not items:
        return {}
    if not claude_enabled(settings) or not budget_allows_call(settings):
        return {}

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 400,
        "temperature": 0,
        "system": TIEBREAK_SYSTEM,
        "messages": [{"role": "user", "content": tiebreak_user_prompt(zone, items)}],
    }
    key = get_anthropic_key(settings)
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    async with _call_lock:
        if not budget_allows_call(settings):
            return {}
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=payload)
        body = r.text or ""
        if _is_billing_error(r.status_code, body):
            raise ClaudeBudgetExhausted(f"anthropic HTTP {r.status_code}")
        if r.status_code >= 400:
            raise RuntimeError(f"anthropic HTTP {r.status_code}: {body[:180]}")
        data = r.json()
        usage = data.get("usage") or {}
        cost = estimate_cost_usd(usage)
        record_usage(usage, cost)
        if log:
            log(f"Claude tiebreak {CLAUDE_MODEL}: {len(items)} port(s) "
                f"cost=${cost:.4f}")

    from app.core.llm import parse_json_flexible
    raw = _content_text({"content": data.get("content") or []})
    parsed = parse_json_flexible(raw)
    if not isinstance(parsed, dict):
        return {}
    return _parse_tiebreak(parsed, items)
