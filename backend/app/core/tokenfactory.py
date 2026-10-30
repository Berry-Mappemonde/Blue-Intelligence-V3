"""Nemotron via Nebius Token Factory (OpenAI-compatible, EU host only).

The key is read from the environment (`NEBIUS_API_KEY`) and is never logged.
Calls go to ``https://api.tokenfactory.nebius.com/v1/chat/completions``.
Any other host, including a US Token Factory host, is refused.

Active when ``LLM_PROVIDER=tokenfactory`` or when ``NEBIUS_API_KEY`` is set.
Without either, callers keep the NVIDIA NIM path.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import weakref
from contextvars import ContextVar
from urllib.parse import urlparse

import httpx

EU_HOST = "api.tokenfactory.nebius.com"
BASE = "https://api.tokenfactory.nebius.com/v1"
CHAT_URL = BASE + "/chat/completions"
DEFAULT_BUDGET_USD = 5.0

TF_LIGHTNING = "nvidia/Nemotron-3_5-Lightning"
TF_SUPER_VL = "nvidia/Nemotron-3.5-Super-VL"
TF_ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
TF_SUPER = "nvidia/nemotron-3-super-120b-a12b"
TF_NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"

ALLOWED = frozenset({TF_LIGHTNING, TF_SUPER_VL, TF_ULTRA, TF_SUPER, TF_NANO})

# Nemotron roles → catalogue ids on the EU host. ``text`` / ``legal`` / ``review``
# stay on their NIM chains: they are not Nemotron roles.
ROLE_MODELS = {
    "filter": TF_LIGHTNING,
    "normalize": TF_LIGHTNING,
    "search_rank": TF_LIGHTNING,
    "page": TF_SUPER_VL,
    "extract": TF_SUPER,
    "json": TF_SUPER,
    "judge": TF_ULTRA,
    "triage": TF_NANO,
}

# NIM catalogue ids (nvidia.py) → Token Factory ids. Super is the same string.
_NIM_IDS = {
    "nvidia/nemotron-3.5-lightning-30b-a3b": TF_LIGHTNING,
    "nvidia/nemotron-3.5-super-vl-120b-a12b": TF_SUPER_VL,
    "nvidia/nemotron-3-ultra-550b-a55b": TF_ULTRA,
    "nvidia/nemotron-3-super-120b-a12b": TF_SUPER,
    "nvidia/nemotron-3-nano-30b-a3b": TF_NANO,
}

# USD per 1M tokens (input, output). Conservative defaults; override with
# TF_PRICES_JSON = {"<model id>": [input, output], ...}.
# Super 120B has no separate figure here and shares the Super-VL band.
PRICES = {
    TF_LIGHTNING: (0.1, 0.4),
    TF_SUPER_VL: (0.3, 1.2),
    TF_ULTRA: (0.6, 2.4),
    TF_NANO: (0.06, 0.24),
    TF_SUPER: (0.3, 1.2),
}
_UNKNOWN_PRICE = (1.0, 3.0)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_END = "</think>"

_current: ContextVar[str | None] = ContextVar("tf_run_id", default=None)
_LEDGER: dict[str, list[dict]] = {}
_CALLS: dict[str, int] = {}  # LLM calls reserved per run (take_calls), any provider
_CALLS_HELD: dict[str, int] = {}  # calls set aside for zones in progress, not used yet
_ZONES_LEFT: dict[str, int] = {}  # zones of the run that have not taken their share yet
_ledger_guard = threading.Lock()
_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class TokenFactoryBudgetExceeded(RuntimeError):
    """TF_RUN_BUDGET_USD reached for this run; the call was not sent."""


class LLMCallCapReached(RuntimeError):
    """The run's cap on LLM calls is used up; the call was not sent."""


def get_key() -> str:
    return (os.environ.get("NEBIUS_API_KEY") or "").strip()


def provider_name(settings: dict | None = None) -> str:
    s = settings or {}
    raw = str(s.get("llm_provider") or os.environ.get("LLM_PROVIDER") or "auto").strip().lower()
    return raw or "auto"


def active(settings: dict | None = None) -> bool:
    """True when Nemotron roles must use Token Factory (never the US NIM host)."""
    if provider_name(settings) == "tokenfactory":
        return True
    return bool(get_key())


def budget_usd() -> float:
    raw = (os.environ.get("TF_RUN_BUDGET_USD") or "").strip()
    if not raw:
        return DEFAULT_BUDGET_USD
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_BUDGET_USD


def chat_url() -> str:
    """EU chat/completions URL. Any other host is an error (no US fallback)."""
    raw = (os.environ.get("BI_TF_BASE") or "").strip()
    if not raw:
        return CHAT_URL
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    if (
        parsed.scheme != "https"
        or host != EU_HOST
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            "tokenfactory host must be https://api.tokenfactory.nebius.com")
    if path in ("", "/"):
        path = "/v1"
    if path == "/v1/chat/completions":
        return f"https://{EU_HOST}{path}"
    if path != "/v1":
        raise RuntimeError(
            "tokenfactory host must be https://api.tokenfactory.nebius.com")
    return f"https://{EU_HOST}{path}/chat/completions"


def _env_override(role: str) -> str | None:
    raw = (os.environ.get(f"TF_MODEL_{role.upper()}") or "").strip()
    if not raw:
        return None
    if raw not in ALLOWED:
        raise RuntimeError("tokenfactory model is not in the EU catalogue")
    return raw


def to_tf(model: str | None) -> str | None:
    """Map a NIM or Token Factory Nemotron id onto the EU catalogue, or None."""
    raw = (model or "").strip()
    if not raw:
        return None
    if raw in ALLOWED:
        return raw
    mapped = _NIM_IDS.get(raw)
    if mapped:
        return mapped
    low = raw.lower()
    for allowed in ALLOWED:
        if allowed.lower() == low:
            return allowed
    if "nemotron" not in low and "lightning" not in low:
        return None
    if "nano" in low:
        return TF_NANO
    if "lightning" in low:
        return TF_LIGHTNING
    if "super-vl" in low or "super_vl" in low:
        return TF_SUPER_VL
    if "ultra" in low:
        return TF_ULTRA
    if "super" in low:
        return TF_SUPER
    return None


def resolve(role: str, model: str | None = None) -> str:
    """Catalogue id for a Nemotron call. Unknown Nemotron ids are refused."""
    if model:
        mapped = to_tf(model)
        if mapped:
            return mapped
        low = model.lower()
        if "nemotron" in low or "lightning" in low:
            raise RuntimeError("tokenfactory model is not in the EU catalogue")
    override = _env_override(role)
    if override:
        return override
    if role in ROLE_MODELS:
        return ROLE_MODELS[role]
    return TF_LIGHTNING


def _looks_nemotron(model: str | None) -> bool:
    low = (model or "").lower()
    return "nemotron" in low or "lightning" in low


def serves(role: str, model: str | None = None, settings: dict | None = None) -> bool:
    """True when this completion must be sent to Token Factory."""
    if not active(settings):
        return False
    if model:
        # Unknown Nemotron ids still stay off the US host: resolve() refuses them.
        return to_tf(model) is not None or _looks_nemotron(model)
    if role in ROLE_MODELS:
        return True
    return _env_override(role) is not None


def routed_model(role: str, preferred: str | None = None,
                 settings: dict | None = None) -> str | None:
    """Single EU model for a Nemotron role, or None to keep the NIM chain."""
    if not active(settings):
        return None
    if preferred and to_tf(preferred) is None and not _looks_nemotron(preferred):
        return None
    if preferred and (to_tf(preferred) or _looks_nemotron(preferred)):
        return resolve(role, preferred)
    if role in ROLE_MODELS or _env_override(role):
        return resolve(role, None)
    return None


def price_table() -> dict[str, tuple[float, float]]:
    table = {key: (float(val[0]), float(val[1])) for key, val in PRICES.items()}
    raw = (os.environ.get("TF_PRICES_JSON") or "").strip()
    if not raw:
        return table
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return table
    if not isinstance(data, dict):
        return table
    for key, val in data.items():
        if isinstance(val, (list, tuple)) and len(val) == 2:
            try:
                table[str(key)] = (float(val[0]), float(val[1]))
            except (TypeError, ValueError):
                continue
    return table


def prices_for(model: str | None) -> tuple[float, float]:
    table = price_table()
    mapped = to_tf(model) or (model or "")
    if mapped in table:
        return table[mapped]
    low = (model or "").lower()
    for key, val in table.items():
        if key.lower() == low:
            return val
    if "nano" in low:
        return table[TF_NANO]
    if "lightning" in low:
        return table[TF_LIGHTNING]
    if "super-vl" in low or "super_vl" in low:
        return table[TF_SUPER_VL]
    if "ultra" in low:
        return table[TF_ULTRA]
    if "super" in low:
        return table[TF_SUPER]
    return _UNKNOWN_PRICE


def bind_run(run_id: str | None):
    return _current.set(run_id)


def reset_run(token) -> None:
    _current.reset(token)


def current_run_id() -> str | None:
    own = _current.get()
    if own:
        return own
    try:
        from app.services.isolated_runs import current_run_id as isolated_run
        return isolated_run()
    except Exception:
        return None


def account_run(run_id: str | None = None) -> str:
    if run_id and run_id != "_unscoped":
        bound = current_run_id()
        if bound:
            return bound
        return run_id
    return current_run_id() or run_id or "_unscoped"


def ledger_key(run_id: str | None = None) -> str:
    if run_id:
        return run_id
    return current_run_id() or "_unscoped"


def _rows(run_id: str | None = None) -> list[dict]:
    with _ledger_guard:
        return list(_LEDGER.get(ledger_key(run_id), ()))


def reset_ledger(run_id: str | None = None) -> None:
    with _ledger_guard:
        if run_id is None:
            _LEDGER.clear()
            _CALLS.clear()
            _CALLS_HELD.clear()
            _ZONES_LEFT.clear()
        else:
            _LEDGER.pop(run_id, None)
            _CALLS.pop(run_id, None)
            _CALLS_HELD.pop(run_id, None)
            _ZONES_LEFT.pop(run_id, None)


def take_calls(wanted: int, cap: int, run_id: str | None = None) -> int:
    """Reserve up to ``wanted`` LLM calls against the run's ``cap``; return how many were granted.

    Counts calls whatever the provider, on the same per-run key as the USD ledger. One
    call is one provider step started: a primary-reader batch, each fallback step of
    ``llm.extract_ports`` (NIM, OpenRouter, Claude), the second reader, the Claude last
    resort. Retries inside one step are not counted separately.
    """
    rid = ledger_key(run_id)
    with _ledger_guard:
        used = _CALLS.get(rid, 0)
        granted = max(0, min(int(wanted), int(cap) - used))
        _CALLS[rid] = used + granted
    return granted


def calls_taken(run_id: str | None = None) -> int:
    with _ledger_guard:
        return _CALLS.get(ledger_key(run_id), 0)


def restore_calls(n: int, run_id: str | None = None) -> None:
    """Resume after a restart: the run's calls already taken, from the run document."""
    rid = ledger_key(run_id)
    with _ledger_guard:
        _CALLS[rid] = max(_CALLS.get(rid, 0), int(n or 0))


def restore_spend(cost: dict | None, run_id: str | None = None) -> None:
    """Resume after a restart: the run's ledger rows from its stored cost summary
    (``summary()``), so the USD budget and the summary cover the whole run. Does nothing
    if this process already holds rows for the run."""
    cost = cost if isinstance(cost, dict) else {}
    rows = [{"model": e.get("model"), "in": int(e.get("tokens_in") or 0),
             "out": int(e.get("tokens_out") or 0), "usd": float(e.get("usd") or 0.0)}
            for e in cost.get("entries") or () if isinstance(e, dict)]
    if not rows and float(cost.get("usd_estimated") or 0.0) > 0:
        rows = [{"model": "restored", "in": int(cost.get("tokens_in") or 0),
                 "out": int(cost.get("tokens_out") or 0),
                 "usd": float(cost["usd_estimated"])}]
    rid = ledger_key(run_id)
    with _ledger_guard:
        if rows and not _LEDGER.get(rid):
            _LEDGER[rid] = rows


def plan_zone_calls(zones: int, run_id: str | None = None) -> None:
    """Number of zones still to run; each takes a fair share of the cap (``reserve_zone_calls``)."""
    with _ledger_guard:
        _ZONES_LEFT[ledger_key(run_id)] = max(0, int(zones))


def reserve_zone_calls(cap: int, floor: int, run_id: str | None = None) -> int:
    """Set aside one zone's share of the run cap: free calls / zones left, at least
    ``floor``, never more than what is free. Free = cap - used - held by other zones."""
    rid = ledger_key(run_id)
    with _ledger_guard:
        free = max(0, int(cap) - _CALLS.get(rid, 0) - _CALLS_HELD.get(rid, 0))
        left = max(1, _ZONES_LEFT.get(rid, 1))
        share = min(free, max(int(floor), free // left))
        _CALLS_HELD[rid] = _CALLS_HELD.get(rid, 0) + share
        if rid in _ZONES_LEFT:
            _ZONES_LEFT[rid] = max(0, _ZONES_LEFT[rid] - 1)
    return share


def use_zone_calls(n: int, run_id: str | None = None) -> None:
    """Move ``n`` calls of a zone's share from held to used."""
    rid = ledger_key(run_id)
    with _ledger_guard:
        _CALLS[rid] = _CALLS.get(rid, 0) + int(n)
        _CALLS_HELD[rid] = max(0, _CALLS_HELD.get(rid, 0) - int(n))


def return_zone_calls(n: int, run_id: str | None = None) -> None:
    """Move ``n`` used calls of a zone's share back to held (calls never started)."""
    rid = ledger_key(run_id)
    with _ledger_guard:
        n = min(int(n), _CALLS.get(rid, 0))
        _CALLS[rid] = _CALLS.get(rid, 0) - n
        _CALLS_HELD[rid] = _CALLS_HELD.get(rid, 0) + n


def close_zone_calls(unused: int | None, run_id: str | None = None) -> None:
    """Zone finished: give back its unused share. ``None``: it never took a share, so it
    leaves the zones still to run."""
    rid = ledger_key(run_id)
    with _ledger_guard:
        if unused is None:
            if rid in _ZONES_LEFT:
                _ZONES_LEFT[rid] = max(0, _ZONES_LEFT[rid] - 1)
            return
        _CALLS_HELD[rid] = max(0, _CALLS_HELD.get(rid, 0) - int(unused))


def spent_usd(run_id: str | None = None) -> float:
    return sum(float(row["usd"]) for row in _rows(run_id))


def ensure_budget(run_id: str | None = None) -> None:
    spent = spent_usd(run_id)
    budget = budget_usd()
    if spent >= budget:
        raise TokenFactoryBudgetExceeded(
            f"tokenfactory budget reached ({spent:.6f} >= {budget:.6f} USD)")


def record(model: str, usage: dict | None = None, run_id: str | None = None) -> dict:
    usage = usage or {}
    tokens_in = int(usage.get("prompt_tokens") or usage.get("input_tokens") or usage.get("in") or 0)
    tokens_out = int(
        usage.get("completion_tokens") or usage.get("output_tokens") or usage.get("out") or 0)
    price_in, price_out = prices_for(model)
    usd = tokens_in / 1_000_000 * price_in + tokens_out / 1_000_000 * price_out
    row = {"model": model, "in": tokens_in, "out": tokens_out, "usd": usd}
    rid = ledger_key(run_id)
    with _ledger_guard:
        _LEDGER.setdefault(rid, []).append(row)
    return row


def summary(run_id: str | None = None) -> dict:
    rid = ledger_key(run_id)
    rows = _rows(rid)
    by_model: dict[str, dict] = {}
    tokens_in = tokens_out = 0
    usd = 0.0
    entries = []
    for row in rows:
        tokens_in += int(row["in"])
        tokens_out += int(row["out"])
        usd += float(row["usd"])
        slot = by_model.setdefault(row["model"], {
            "calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0,
        })
        slot["calls"] += 1
        slot["tokens_in"] += int(row["in"])
        slot["tokens_out"] += int(row["out"])
        slot["usd"] = round(slot["usd"] + float(row["usd"]), 6)
        entries.append({
            "model": row["model"],
            "tokens_in": int(row["in"]),
            "tokens_out": int(row["out"]),
            "usd": round(float(row["usd"]), 6),
        })
    spent = round(usd, 6)
    budget = budget_usd()
    return {
        "run_id": rid,
        "provider": "tokenfactory",
        "host": EU_HOST,
        "calls": len(rows),
        # Calls counted against formalities.extract_max_llm_calls (any provider), not
        # only the Token Factory requests in "calls".
        "calls_taken": calls_taken(rid),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usd_estimated": spent,
        "budget_usd": budget,
        "budget_reached": spent >= budget,
        "by_model": by_model,
        "entries": entries,
    }


def public_cost(run_id: str, persisted: dict | None = None) -> dict:
    """Live accumulator, or the cost stored on the run document if this process has none."""
    live = summary(run_id)
    if live["calls"] == 0 and isinstance(persisted, dict) and int(persisted.get("calls") or 0) > 0:
        out = dict(persisted)
        out["calls_taken"] = max(int(persisted.get("calls_taken") or 0), live["calls_taken"])
        return out
    return live


def attach(summary_doc: dict, run_id: str) -> dict:
    summary_doc["tokenfactory"] = public_cost(run_id)
    return summary_doc


def _lock_for_loop() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _locks[loop] = lock
    return lock


def budget_lock():
    return _lock_for_loop()


def _redact(text: str) -> str:
    out = text or ""
    key = get_key()
    if key:
        out = out.replace(key, "[REDACTED]")
    return out[:160]


def _strip_think(text: str) -> str:
    text = _THINK_BLOCK.sub("", text or "")
    end = text.lower().rfind(_THINK_END)
    if end != -1:
        text = text[end + len(_THINK_END):]
    return text.strip()


def _payload(model: str, system: str, user: str, max_tokens: int,
             images: list[bytes] | None, *, json_mode: bool) -> dict:
    from app.core.vision_msg import openai_user_content

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": openai_user_content(user, images)},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


async def _post(key: str, url: str, payload: dict, *, log=None, model: str) -> dict:
    if urlparse(url).hostname != EU_HOST:
        raise RuntimeError(
            "tokenfactory host must be https://api.tokenfactory.nebius.com")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(120.0, connect=20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError:
        raise RuntimeError("tokenfactory transport error") from None
    if response.status_code >= 400:
        if log:
            log(f"tokenfactory {model}: HTTP {response.status_code}")
        raise RuntimeError(
            f"tokenfactory HTTP {response.status_code}: {_redact(response.text)}")
    try:
        body = response.json()
    except Exception:
        raise RuntimeError("tokenfactory: invalid JSON response") from None
    if not isinstance(body, dict):
        raise RuntimeError("tokenfactory: invalid JSON response")
    return body


def _message(body: dict) -> dict:
    choices = body.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    if not isinstance(msg, dict):
        return {}
    content = msg.get("content")
    reason = msg.get("reasoning_content")
    cleaned = dict(msg)
    if isinstance(content, str):
        cleaned["content"] = _strip_think(content)
    if isinstance(reason, str):
        cleaned["reasoning_content"] = _strip_think(reason)
    return cleaned


def _parse_json(body: dict) -> dict | None:
    from app.core.nvidia import _extract_json_object, _json_from_message

    msg = _message(body)
    data = _json_from_message(msg)
    if isinstance(data, dict):
        return data
    content = msg.get("content")
    if isinstance(content, str):
        data = _extract_json_object(content)
        if isinstance(data, dict):
            return data
    return None


def _served_model(body: dict, requested: str) -> str:
    raw = body.get("model")
    if isinstance(raw, str):
        mapped = to_tf(raw)
        if mapped:
            return mapped
    return requested


async def _call(role: str, model: str | None, system: str, user: str,
                max_tokens: int, images: list[bytes] | None, *,
                json_mode: bool, log=None) -> tuple[dict, str]:
    key = get_key()
    if not key:
        raise RuntimeError("NEBIUS_API_KEY missing")
    used = resolve(role, model)
    url = chat_url()
    rid = account_run(None)
    async with budget_lock():
        ensure_budget(rid)
        payload = _payload(used, system, user, max_tokens, images, json_mode=json_mode)
        try:
            body = await _post(key, url, payload, log=log, model=used)
        except RuntimeError as exc:
            if json_mode and "HTTP 400" in str(exc) and payload.get("response_format"):
                payload.pop("response_format", None)
                body = await _post(key, url, payload, log=log, model=used)
            else:
                raise
        served = _served_model(body, used)
        record(served, body.get("usage") if isinstance(body.get("usage"), dict) else None, run_id=rid)
    return body, served


async def complete_json(system: str, prompt: str, *,
                        role: str = "json",
                        model: str | None = None,
                        max_tokens: int = 800,
                        log=None,
                        images: list[bytes] | None = None,
                        settings: dict | None = None) -> tuple[dict, str]:
    """JSON completion on the EU host. ``settings`` is accepted for call-site parity."""
    del settings
    body, served = await _call(
        role, model, system, prompt, max_tokens, images,
        json_mode=True, log=log)
    data = _parse_json(body)
    if not isinstance(data, dict):
        raise RuntimeError("tokenfactory: no JSON in output")
    return data, served


async def complete_text(system: str, prompt: str, *,
                        role: str = "text",
                        model: str | None = None,
                        max_tokens: int = 800,
                        log=None) -> str:
    body, _served = await _call(
        role, model, system, prompt, max_tokens, None,
        json_mode=False, log=log)
    msg = _message(body)
    text = msg.get("content") if isinstance(msg.get("content"), str) else ""
    text = text.strip()
    if not text:
        raise RuntimeError("tokenfactory: empty text")
    return text
