"""NVIDIA NIM hosted adapter (OpenAI-compatible).

JSON completions — never web search (:online stays OpenRouter).

The Build catalog (https://build.nvidia.com/models, ~143 cards) mixes
ASR, bio, CFD, image, OCR and LLM. GET /v1/models is a stale OpenAI index
(404 ids: yi-large, dbrx, granite…). Chat IDs: docs.api.nvidia.com/nim/
reference/llm-apis + canonical URL /{publisher}/{title}.

Many cards stay online while the hosted trial returns 410 Gone
(EOL, dated successor: deepseek-v4-flash → flash-0731). Others (Muse,
Laguna, Gemma 4) are live but "Structured Output: Not supported":
json_object hangs them — we omit response_format.

Fallbacks **inside NIM** (HTTP failure / timeout / empty JSON → next).
Single source: `CHAINS`. Outside NIM: OpenRouter, then Claude (price). No NIM web.

  judge   gpt-oss → Muse → Flash           # ask_yes_no PoE; Flash last (529)
  extract gpt-oss → Muse                   # Kimi only if decree (`legal`)
  legal   Kimi → Muse
  json    gpt-oss → Muse                   # ask_yes_no (gatekeeper, AMP), project, geocode
  page    gpt-oss → Muse                   # marina + harbormaster (same chain)
  text    gpt-oss → Muse                   # ask_text, json_object=false
  review  gpt-oss → Muse                   # Review document judge (vision if the model follows)
  Pro (deepseek-v4-pro-0813) is EOL 410: never called.

JSON parameters — hosted contract = infer card (docs.api.nvidia.com/nim/…-infer),
not the playground nor the local card (temp/top_k outside hosted schema):

  Pro    effort none (infer default); kwargs {thinking:false} (Build prototype)
  Flash  effort none (infer default = **high**); kwargs thinking=false
  Muse   no json_object; 0.95 / 1.0; effort low (default high)
  gpt-oss effort low (default medium; enum without none); 0.6 / 0.7
  Kimi   temp 1.0; no top_p (not exposed); effort low (default max)
  Laguna 1 / 0.95; no thinking/effort on the infer API


Key: NVIDIA_API_KEY (env) or settings["nvidia_api_key"] (UI).
Provider: LLM_PROVIDER=nvidia|openrouter|tokenfactory|auto
(auto = Token Factory if NEBIUS_API_KEY and no NVIDIA key, else NVIDIA if a key
is present). Nemotron roles use the EU Token Factory host whenever
NEBIUS_API_KEY is set or the provider is tokenfactory (see tokenfactory.py).
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import httpx

NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
FLASH_MODEL = "deepseek-ai/deepseek-v4-flash-0731"
PRO_MODEL = "deepseek-ai/deepseek-v4-pro-0813"  # EOL 410 — do not call
GPT_OSS_MODEL = "openai/gpt-oss-20b"
PRIMARY_MODEL = GPT_OSS_MODEL
SECONDARY_MODEL = "meta/muse-glimmer-30b"
LEGAL_MODEL = "moonshotai/kimi-k3"

# Nemotron suite — role-specialized NVIDIA NIM chat models (catalogue IDs, 2026-10).
# nemotron-4-340b-reward is a reward model (it scores answers), never a chat model.
NEMOTRON_LIGHTNING_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"  # filter, normalize, search_rank
NEMOTRON_SUPER_VL_MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"  # page (visual PDF reading)
NEMOTRON_SUPER_MODEL = "nvidia/nemotron-3-super-120b-a12b"          # extract, json, reconcile
NEMOTRON_ULTRA_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"          # judge, brief (reasoning)
# Hosted trial gone (410 Gone). _usable_nim rewrites them to gpt-oss.
_EOL_MODELS = frozenset({
    PRO_MODEL,
    "deepseek-ai/deepseek-v4-pro",
})

# Build cards: Capabilities → Structured Output: Not supported.
_NO_JSON_OBJECT = (
    "muse-glimmer",
    "laguna",
    "gemma-4",
    "diffusiongemma",
)

# Docs IDs / EOL aliases → hosted successor still served.
_ALIASES = {
    "deepseek-ai/deepseek-v4-flash": FLASH_MODEL,
    "deepseek-ai/deepseek-v4-pro": PRO_MODEL,
    "poolside/laguna-xs-2-1": "poolside/laguna-xs-2.1",
}

# Order = 2026-09-09 canary (quality then latency, 529/429 queued).
# Muse is no longer 2nd by inheritance: gpt-oss was faster at equal quality.
# Global judge + extraction queue. 6 extractors + 2 judges on the same Pro = 429.
_nvidia_limit = 2
_nvidia_gate: asyncio.Semaphore | None = None


def configure_concurrency(n=None) -> int:
    """1–4 parallel NIM calls (judge and extraction share the queue)."""
    global _nvidia_limit, _nvidia_gate
    raw = n if n is not None else os.environ.get("NVIDIA_MAX_CONCURRENCY", _nvidia_limit)
    try:
        _nvidia_limit = max(1, min(4, int(raw)))
    except (TypeError, ValueError):
        _nvidia_limit = 2
    _nvidia_gate = asyncio.Semaphore(_nvidia_limit)
    return _nvidia_limit


def _nvidia_sem() -> asyncio.Semaphore:
    global _nvidia_gate
    if _nvidia_gate is None:
        configure_concurrency()
    return _nvidia_gate


CHAINS = {
    "judge": (
        NEMOTRON_ULTRA_MODEL,              # Nemotron Ultra for reasoning/legal arbitrage
        GPT_OSS_MODEL,
        SECONDARY_MODEL,
        FLASH_MODEL,
    ),
    "extract": (
        NEMOTRON_SUPER_MODEL,              # Nemotron 3-Super for structured extraction
        GPT_OSS_MODEL,
        SECONDARY_MODEL,
    ),
    "legal": (LEGAL_MODEL, SECONDARY_MODEL),
    "json": (
        NEMOTRON_SUPER_MODEL,              # Nemotron 3-Super for JSON output
        GPT_OSS_MODEL,
        SECONDARY_MODEL,
    ),
    "page": (
        NEMOTRON_SUPER_VL_MODEL,           # Nemotron 3.5-Super-VL for visual PDF reading
        GPT_OSS_MODEL,
        SECONDARY_MODEL,
    ),
    "text": (GPT_OSS_MODEL, SECONDARY_MODEL),
    "review": (GPT_OSS_MODEL, SECONDARY_MODEL),
    "filter": (NEMOTRON_LIGHTNING_MODEL,),  # Nemotron Lightning for triage
    "normalize": (NEMOTRON_LIGHTNING_MODEL,),  # Nemotron Lightning for normalization
    "search_rank": (NEMOTRON_LIGHTNING_MODEL,),  # Nemotron Lightning for ranking
}

_THINKING_RE = re.compile(r"^\s*here's a thinking process", re.I)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)
_LEGAL_RE = re.compile(
    r"\b("
    r"d[ée]cret|arr[êe]t[ée]s?|gazette|journal\s+officiel|"
    r"puertos?\s+habilitados?|boleti[nń]\s+oficial|"
    r"points?\s+de\s+passage\s+frontaliers?|"
    r"designated\s+ports?\s+of\s+entry|"
    r"official\s+list\s+of\s+ports?\s+of\s+entry|"
    r"capitan[ií]as?\s+mar[ií]timas?|"
    r"loi\s+n[°ºo]|boe\.es|douane\.gouv"
    r")\b",
    re.I,
)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def get_nvidia_key(settings: dict | None = None) -> str:
    s = settings or {}
    return (s.get("nvidia_api_key") or _env("NVIDIA_API_KEY")).strip()


def llm_provider(settings: dict | None = None) -> str:
    s = settings or {}
    raw = str(s.get("llm_provider") or _env("LLM_PROVIDER") or "auto").strip().lower()
    if raw in ("nvidia", "openrouter", "tokenfactory"):
        return raw
    if _env("NEBIUS_API_KEY") and not get_nvidia_key(s):
        return "tokenfactory"
    return "nvidia" if get_nvidia_key(s) else "openrouter"


def nvidia_enabled(settings: dict | None = None) -> bool:
    s = settings or {}
    return llm_provider(s) == "nvidia" and bool(get_nvidia_key(s))


def _alias(model: str) -> str:
    """Fix docs / EOL slugs. Muse and Laguna stay callable."""
    raw = (model or "").strip()
    return _ALIASES.get(raw, raw)


def _usable_nim(model: str) -> str:
    """Hosted ID still served. Pro 410 slugs become gpt-oss."""
    used = _alias(model or "")
    if used in _EOL_MODELS or (model or "").strip() in _EOL_MODELS:
        return GPT_OSS_MODEL
    return used


def supports_json_object(model: str | None) -> bool:
    blob = (model or "").lower()
    return not any(token in blob for token in _NO_JSON_OBJECT)


def primary_model() -> str:
    return _usable_nim(_env("NVIDIA_MODEL") or PRIMARY_MODEL)


def secondary_model() -> str:
    return _usable_nim(_env("NVIDIA_MODEL_SECONDARY") or SECONDARY_MODEL)


def legal_model() -> str:
    return _usable_nim(_env("NVIDIA_MODEL_LEGAL") or LEGAL_MODEL)


def _dedupe(models: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in models:
        used = _usable_nim(raw)
        if not used or used in seen:
            continue
        seen.add(used)
        out.append(used)
    return tuple(out)


def models_for(role: str, preferred: str | None = None,
               settings: dict | None = None) -> tuple[str, ...]:
    """NIM order for the role. `CHAINS` is the source; no implicit Muse pin.

    Overrides: `NVIDIA_MODEL_CHAIN_{ROLE}` (full list), else
    `NVIDIA_MODEL` / `NVIDIA_MODEL_LEGAL` at the head only if they are set.
    `preferred`: a one-off call (2nd extract reader, etc.).

    When Token Factory is active, a Nemotron role is a single EU catalogue id
    (no NIM fallback, so the call cannot land on the US host).
    """
    from app.core.tokenfactory import routed_model

    routed = routed_model(role, preferred, settings)
    if routed:
        return (routed,)
    key = role if role in CHAINS else "json"
    extra = _env(f"NVIDIA_MODEL_CHAIN_{key.upper()}")
    if extra:
        defaults = [_usable_nim(p.strip()) for p in extra.split(",") if p.strip()]
    else:
        defaults = []
        for m in CHAINS[key]:
            if m == SECONDARY_MODEL:
                defaults.append(secondary_model())
            elif m == LEGAL_MODEL:
                defaults.append(legal_model())
            else:
                defaults.append(_usable_nim(m))
    if not defaults:
        defaults = [_usable_nim(m) for m in CHAINS[key]]
    head: list[str] = []
    if preferred:
        head.append(preferred)
    elif key == "legal" and _env("NVIDIA_MODEL_LEGAL"):
        head.append(legal_model())
    elif key != "legal" and _env("NVIDIA_MODEL"):
        head.append(primary_model())
    return _dedupe([*head, *defaults])


def engine_label(model: str | None = None) -> str:
    """Persisted label (judge_engine / extraction_engine)."""
    m = (model or primary_model()).lower()
    if "deepseek" in m:
        return "nvidia-deepseek"
    if "muse" in m:
        return "nvidia-muse"
    if "llama-3.2" in m:
        return "nvidia-llama"
    if "kimi" in m:
        return "nvidia-kimi"
    if "laguna" in m:
        return "nvidia-laguna"
    if "gpt-oss" in m:
        return "nvidia-gpt-oss"
    if "gemma" in m:
        return "nvidia-gemma"
    if "minimax" in m:
        return "nvidia-minimax"
    if "nemotron" in m or "lightning" in m:
        return "nvidia-nemotron"
    return "nvidia"


def muse_generation_extras(model: str | None = None) -> dict:
    """Documented NIM parameters (infer cards) for PoE JSON."""
    return generation_extras(model)


def sampling_params(model: str | None = None) -> dict:
    """NVIDIA infer-card sampling, not generic greedy."""
    m = (model or primary_model()).lower()
    if "muse" in m:
        # infer: greedy degrades; recommended pair 0.95 / 1.0.
        # (local card: 1.0 / 0.95 / top_k 64 — top_k absent from hosted schema)
        return {"temperature": 0.95, "top_p": 1.0}
    if "kimi-k3" in m:
        # kimi-k3-infer: "Recommended for Kimi-K3: 1.0"; top_p not exposed.
        return {"temperature": 1.0}
    if "gpt-oss" in m:
        # openai-gpt-oss-20b-infer: defaults 0.6 / 0.7.
        return {"temperature": 0.6, "top_p": 0.7}
    if "laguna" in m:
        # poolside-laguna-xs-2-1-infer: defaults 1 / 0.95.
        return {"temperature": 1.0, "top_p": 0.95}
    # DeepSeek V4 infer: default 1 / 0.95. PoE JSON: temperature 0 only
    # (the card advises against touching temperature and top_p together).
    return {"temperature": 0}


def generation_extras(model: str | None = None, role: str = "json") -> dict:
    """reasoning_effort / chat_template_kwargs per infer card and usage."""
    m = (model or primary_model()).lower()
    # legal: Kimi thinking always on; infer default = max. PoE / page JSON:
    # low so we do not eat max_tokens (trial 429 if max).
    if "muse" in m:
        return {
            "reasoning_effort": "low",
            "chat_template_kwargs": {"reasoning_strength": "low"},
        }
    if "deepseek-v4-flash" in m:
        return {
            "reasoning_effort": "none",
            "chat_template_kwargs": {
                "thinking": False,
                "reasoning_effort": "none",
            },
        }
    if "deepseek-v4" in m:
        return {
            "reasoning_effort": "none",
            "chat_template_kwargs": {"thinking": False},
        }
    if "gpt-oss" in m:
        return {"reasoning_effort": "low"}
    if "kimi-k3" in m:
        effort = "high" if role == "legal" else "low"
        return {"reasoning_effort": effort}
    return {}


def looks_like_legal_text(text: str | None) -> bool:
    """True if the excerpt looks like a decree / gazette (Kimi, not the current judge)."""
    blob = (text or "").strip()
    if len(blob) < 80:
        return False
    return bool(_LEGAL_RE.search(blob))


def second_extract_choice(context: str | None) -> tuple[str, str] | None:
    """Second reader: Kimi on a decree; else the next in the extract chain."""
    prim = primary_model()
    if looks_like_legal_text(context):
        chain = models_for("legal")
    else:
        chain = models_for("extract")
    for model in chain:
        if model != prim:
            return model, engine_label(model)
    return None


def parse_json_strict(txt: str | None):
    """JSON only. Reject chain-of-thought (prompt example included)."""
    if not isinstance(txt, str) or not txt.strip():
        return None
    s = _FENCE_RE.sub("", txt.strip()).strip()
    if not s or _THINKING_RE.search(s):
        return None
    if not s.lstrip()[:1] in "{[":
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _extract_json_object(txt: str | None):
    """First balanced JSON object (net for reasoning_content)."""
    if not isinstance(txt, str):
        return None
    start = txt.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(txt[start:]):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(txt[start:start + i + 1])
                except Exception:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _message_text(msg: dict | None) -> str:
    msg = msg or {}
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "output_text"):
                parts.append(p.get("text") or "")
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts).strip()
    return ""


def _json_from_message(msg: dict | None):
    msg = msg or {}
    raw = _message_text(msg)
    data = parse_json_strict(raw)
    if isinstance(data, dict):
        return data
    reason = msg.get("reasoning_content")
    data = parse_json_strict(reason if isinstance(reason, str) else None)
    if isinstance(data, dict):
        return data
    return _extract_json_object(reason if isinstance(reason, str) else None)


def _retry_wait(response: httpx.Response, fallback: float) -> float:
    raw = (response.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return min(90.0, max(fallback, float(raw)))
    try:
        return min(90.0, max(fallback, float(raw)))
    except (TypeError, ValueError):
        return fallback


def chat_payload(model: str, system: str, user: str, max_tokens: int,
                 *, json_object: bool | None = None, role: str = "json",
                 images: list[bytes] | None = None) -> dict:
    """chat/completions body. json_object=None → per the Build card."""
    from app.core.vision_msg import openai_user_content

    used = _usable_nim(model)
    use_json = supports_json_object(used) if json_object is None else json_object
    body = {
        "model": used,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": openai_user_content(user, images)},
        ],
        "max_tokens": max_tokens,
        "stream": False,
        **sampling_params(used),
        **generation_extras(used, role=role),
    }
    if use_json:
        body["response_format"] = {"type": "json_object"}
    return body


async def _complete_one(key: str, payload: dict, *,
                        max_tokens: int, log=None) -> dict:
    """One model. 410/404 raise immediately; 400 json_object → retry without format."""
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    delays = (5.0, 12.0, 25.0)
    last_err = "nvidia exhausted retries"
    timeout = httpx.Timeout(120.0, connect=20.0)
    used = dict(payload)
    for delay in (*delays, None):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(NVIDIA_URL, headers=headers, json=used)
        except httpx.TimeoutException as e:
            # Not 3×120 s: the next model in the chain must start
            # (Gemma 4 hangs even without json_object).
            raise RuntimeError(f"nvidia timeout: {type(e).__name__}")
        if r.status_code == 400 and used.get("response_format"):
            used = {k: v for k, v in used.items() if k != "response_format"}
            last_err = f"nvidia HTTP 400: {(r.text or '')[:160]}"
            if log:
                log(f"nvidia {used['model']}: json_object rejeté, retry sans format")
            continue
        if r.status_code == 429:
            # Pro quota: 3 retries 5/12/25 s on the same model = a storm.
            # The chain (gpt-oss / Muse) starts immediately.
            raise RuntimeError(f"nvidia HTTP 429: {(r.text or '')[:160]}")
        if r.status_code in (500, 502, 503) and delay is not None:
            wait = _retry_wait(r, delay)
            if log:
                log(f"nvidia {used['model']}: HTTP {r.status_code}, retry in {wait:.0f}s")
            await asyncio.sleep(wait)
            last_err = f"nvidia HTTP {r.status_code}"
            continue
        if r.status_code >= 400:
            raise RuntimeError(
                f"nvidia HTTP {r.status_code}: {(r.text or '')[:160]}")
        body = r.json()
        choices = body.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        data = _json_from_message(msg)
        if isinstance(data, dict):
            return data
        last_err = "nvidia: no JSON in output"
        if delay is None:
            break
        used["max_tokens"] = min(int(used.get("max_tokens") or max_tokens) * 2, 2500)
        if log:
            log(f"nvidia {used['model']}: no strict JSON, retry tokens={used['max_tokens']}")
        await asyncio.sleep(2.0)
        continue
    raise RuntimeError(last_err)


async def complete_json_nvidia_tracked(
        system: str, prompt: str,
        settings: dict | None = None, *,
        model: str | None = None,
        role: str = "json",
        fallback: bool = True,
        max_tokens: int = 800,
        log=None,
        images: list[bytes] | None = None) -> tuple[dict, str]:
    """Like complete_json_nvidia, plus the id actually served."""
    from app.core.tokenfactory import serves as tf_serves, complete_json as tf_complete

    if tf_serves(role, model, settings):
        return await tf_complete(
            system, prompt, role=role, model=model, max_tokens=max_tokens,
            log=log, images=images, settings=settings)
    key = get_nvidia_key(settings)
    if not key:
        raise RuntimeError("NVIDIA_API_KEY missing")
    chain = models_for(role, preferred=model) if fallback else (
        (_usable_nim(model or primary_model()),)
    )
    last_err = "nvidia exhausted chain"
    async with _nvidia_sem():
        for used in chain:
            payload = chat_payload(
                used, system, prompt, max_tokens, role=role, images=images)
            try:
                data = await _complete_one(key, payload, max_tokens=max_tokens, log=log)
                return data, used
            except RuntimeError as e:
                last_err = str(e)
                more = fallback and used != chain[-1]
                if log:
                    nxt = " → suivant" if more else ""
                    log(f"nvidia {used}: {last_err[:120]}{nxt}")
                if not more:
                    break
                continue
    raise RuntimeError(last_err)


async def complete_json_nvidia(system: str, prompt: str,
                               settings: dict | None = None, *,
                               model: str | None = None,
                               role: str = "json",
                               fallback: bool = True,
                               max_tokens: int = 800,
                               log=None,
                               images: list[bytes] | None = None) -> dict:
    """JSON completion. fallback=True: role chain if the model fails."""
    data, _used = await complete_json_nvidia_tracked(
        system, prompt, settings, model=model, role=role,
        fallback=fallback, max_tokens=max_tokens, log=log, images=images)
    return data


async def complete_text_nvidia(system: str, prompt: str,
                               settings: dict | None = None, *,
                               model: str | None = None,
                               role: str = "text",
                               fallback: bool = True,
                               max_tokens: int = 800,
                               log=None) -> str:
    """Text completion (no json_object). `text` chain by default."""
    from app.core.tokenfactory import serves as tf_serves, complete_text as tf_complete_text

    if tf_serves(role, model, settings):
        return await tf_complete_text(
            system, prompt, role=role, model=model,
            max_tokens=max_tokens, log=log)
    key = get_nvidia_key(settings)
    if not key:
        raise RuntimeError("NVIDIA_API_KEY missing")
    chain = models_for(role, preferred=model) if fallback else (
        (_usable_nim(model or primary_model()),)
    )
    last_err = "nvidia exhausted chain"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(120.0, connect=20.0)
    async with _nvidia_sem():
        for used in chain:
            payload = chat_payload(
                used, system, prompt, max_tokens, json_object=False, role=role)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.post(NVIDIA_URL, headers=headers, json=payload)
            except httpx.TimeoutException as e:
                last_err = f"nvidia timeout: {type(e).__name__}"
                if log:
                    log(f"nvidia {used}: {last_err}")
                if not fallback or used == chain[-1]:
                    break
                continue
            if r.status_code >= 400:
                last_err = f"nvidia HTTP {r.status_code}: {(r.text or '')[:160]}"
                if log:
                    log(f"nvidia {used}: {last_err[:120]}")
                if not fallback or used == chain[-1]:
                    break
                continue
            msg = ((r.json().get("choices") or [{}])[0].get("message") or {})
            text = _message_text(msg).strip()
            if text:
                return text
            last_err = "nvidia: empty text"
            if not fallback or used == chain[-1]:
                break
    raise RuntimeError(last_err)


async def extract_ports_nvidia(context: str, zone: dict,
                               settings: dict | None = None, log=None,
                               model: str | None = None,
                               engine: str | None = None,
                               fallback: bool | None = None) -> list[dict]:
    from app.core.llm import POE_EXTRACT_PROMPT, coerce_ports, reader_context

    # Primary reader: extract chain (Pro → gpt-oss → Muse).
    # Kimi is second reader only via second_extract_choice (decree).
    role = "extract"
    do_fb = True if fallback is None and model is None else bool(fallback)
    sent = reader_context(context)
    prompt = POE_EXTRACT_PROMPT.format(
        name=zone.get("name") or zone.get("geoname"),
        sovereign=zone.get("sovereign") or "",
        context=sent,
    )
    data, used = await complete_json_nvidia_tracked(
        "Tu réponds uniquement en JSON strict.", prompt, settings,
        model=model, role=role, fallback=do_fb, max_tokens=2500, log=log)
    tag = engine or engine_label(used)
    ports = coerce_ports(data, context=context)
    for p in ports:
        p["extraction_engine"] = tag
    if log:
        log(f"LLM {tag}: {len(ports)} port(s) extraits sur {len(sent)} chars envoyés")
    return ports
