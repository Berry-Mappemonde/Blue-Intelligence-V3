"""Nightshift-first generation, with optional NVIDIA Nemotron and offline fallbacks.

Environment (no credentials are required for SAMPLE_MODE). Official Meterless
names win; the legacy MODEL_* names are read only when the official one is unset:
  METERLESS_BASE_URL (or MODEL_BASE_URL): Nightshift OpenAI base,
    default http://127.0.0.1:8088/v1
  METERLESS_RUNTIME_TOKEN (or MODEL_API_KEY): Nightshift bearer token; blank
    permits an unauthenticated probe
  METERLESS_MODEL (or MODEL_NAME): Nightshift route/model, default meterless/auto.
    response.model is recorded as served, without guessing (Nemotron proof).
  NVIDIA_API_KEY: optional direct NVIDIA fallback; unset skips straight to sample
  NEMOTRON_MODEL: default nvidia/nemotron-3.5-lightning-30b-a3b
  NEBIUS_API_KEY: when set, Nemotron skips Nightshift and NVIDIA NIM and uses
    Nebius Token Factory (EU host https://api.tokenfactory.nebius.com/v1 only)
  LLM_PROVIDER=tokenfactory: same EU hop, even before a key check
  TF_RUN_BUDGET_USD: hard stop for Token Factory spend on a run (default 5)
  SAMPLE_MODE: true bypasses all providers
  LLM_PROVIDER: meterless opts existing ask_json/ask_text into this generator
  TRACE_LOG_PATH: override repo logs/trace-<run_id>.jsonl
  TRACE_STDOUT: 0 disables stdout traces (default 1)
  NIGHTSHIFT_LIVE: 1 enables the opt-in live Nemotron integration test

Latency is bounded: the /models probe gets PROBE_TIMEOUT_S (cached PROBE_TTL_S);
each provider gets REQUEST_TIMEOUT_S of wall clock, shared by the request and its
single retry; the whole call never exceeds GENERATE_BUDGET_S before sample.

Sample output comes from the caller's sample_fn(prompt, system) when given, so
real logic runs on the caller's own inputs; otherwise a generic empty structure.
JSON mode requires an object, matching ask_json's contract. Provider failures are
returned as sanitized fallback reasons, never raw HTTP bodies or exception messages.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from app.core.llm import parse_json_flexible

DEFAULT_BASE_URL = 'http://127.0.0.1:8088/v1'
DEFAULT_MODEL = 'meterless/auto'
DEFAULT_NEMOTRON_MODEL = 'nvidia/nemotron-3.5-lightning-30b-a3b'
NVIDIA_URL = 'https://integrate.api.nvidia.com/v1/chat/completions'
DEFAULT_TRACE_LOG_DIR = Path(__file__).resolve().parents[3] / 'logs'
REQUEST_TIMEOUT_S = 90.0   # per provider: one request plus its retry share this
MAX_RETRIES = 1
MAX_OUTPUT_TOKENS = 2000   # every role but "extract" (formalities.extract_output_tokens)
RETRY_BACKOFF_S = 1.0
PROBE_TIMEOUT_S = 3.0
PROBE_TTL_S = 60
GENERATE_BUDGET_S = 150.0  # hard cap for the whole call before sample
MIN_ATTEMPT_S = 5.0        # do not start a provider with less time than this
SECRET_ENV = ('METERLESS_RUNTIME_TOKEN', 'MODEL_API_KEY', 'NVIDIA_API_KEY', 'NEBIUS_API_KEY')
_THINK_BLOCK = re.compile(r'<think>.*?</think>', re.S | re.I)
_THINK_END = '</think>'
_probe_cache = {}  # One configuration at a time; includes a credential fingerprint.
_probe_locks = weakref.WeakKeyDictionary()


def _env(name, default=''):
    return os.environ.get(name, '').strip() or default


def _setting(official, legacy, default=''):
    """Official Meterless name first; the legacy name only when it is unset."""
    return _env(official) or _env(legacy, default)


def worst_case_seconds() -> dict:
    """Upper bounds, in seconds, before each fallback (asserted by tests)."""
    return dict(nightshift=PROBE_TIMEOUT_S + REQUEST_TIMEOUT_S, total=GENERATE_BUDGET_S)


def _strip_reasoning(text: str) -> str:
    """Drop reasoning a model returns inside content (Nemotron ends it with </think>)."""
    text = _THINK_BLOCK.sub('', text)
    end = text.lower().rfind(_THINK_END)
    if end != -1:
        text = text[end + len(_THINK_END):]
    return text.strip()


class _ProviderFailure(Exception):
    """Carries a safe reason code, never a provider response body."""


async def _request(client, method, url, *, budget, retries=MAX_RETRIES, **kwargs):
    """At most 1 + retries attempts, all inside one wall-clock budget in seconds."""
    deadline = time.monotonic() + budget
    for attempt in range(retries + 1):
        try:
            response = await asyncio.wait_for(client.request(method, url, **kwargs),
                                              timeout=deadline - time.monotonic())
        except (httpx.TransportError, asyncio.TimeoutError) as exc:
            offline = isinstance(exc, httpx.TransportError) and not isinstance(exc, httpx.TimeoutException)
            reason = 'offline' if offline else 'timeout'
        else:
            if response.status_code in (401, 403):
                raise _ProviderFailure('unauthorized')
            if 200 <= response.status_code < 300:
                return response
            if response.status_code < 500:
                raise _ProviderFailure(f'http_{response.status_code}')
            reason = f'http_{response.status_code}'
        # A timed-out first attempt has used the whole budget, so it is not retried.
        if attempt == retries or deadline - time.monotonic() <= RETRY_BACKOFF_S:
            raise _ProviderFailure(reason)
        await asyncio.sleep(RETRY_BACKOFF_S)
    raise AssertionError('unreachable')


async def _probe(client, base, key, headers):
    identity = (base, hashlib.sha256(key.encode()).hexdigest())
    loop = asyncio.get_running_loop()
    lock = _probe_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        cached = _probe_cache.get(identity)
        if cached and time.monotonic() - cached[0] < PROBE_TTL_S:
            return cached[1]
        try:
            await _request(client, 'GET', base + '/models', budget=PROBE_TIMEOUT_S,
                           retries=0, headers=headers)
            status = 'ready'
        except _ProviderFailure as exc:
            status = str(exc)
        _probe_cache.clear()
        _probe_cache[identity] = (time.monotonic(), status)
        return status


def _output_tokens(role):
    """max_tokens of one completion; the port-extraction role reads its run rule."""
    if role != 'extract':
        return MAX_OUTPUT_TOKENS
    from app.core.llm import extract_output_tokens
    return extract_output_tokens()


async def _complete(client, url, headers, model, prompt, system, json_mode, budget,
                    max_tokens=None):
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    if json_mode:
        messages.append({'role': 'system', 'content': 'Return a single valid JSON object.'})
    messages.append({'role': 'user', 'content': prompt})
    payload = dict(model=model, messages=messages, temperature=0, stream=False,
                   max_tokens=max_tokens or MAX_OUTPUT_TOKENS)
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    response = await _request(client, 'POST', url, budget=budget, headers=headers, json=payload)
    try:
        body = response.json()
        text = body['choices'][0]['message']['content']
        served_model = body['model']
        usage = body.get('usage') if isinstance(body.get('usage'), dict) else None
        if isinstance(text, str):
            text = _strip_reasoning(text)
        if not isinstance(text, str) or not text or not isinstance(served_model, str) or not served_model:
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError):
        raise _ProviderFailure('invalid_response') from None
    result = dict(text=text, model=served_model)
    if usage:
        result['usage'] = usage
    if json_mode:
        parsed = parse_json_flexible(text)
        if not isinstance(parsed, dict):
            raise _ProviderFailure('invalid_json')
        result['parsed'] = parsed
    return result


def _sample(prompt, system, json_mode, sample_fn):
    if sample_fn is not None:
        # The caller's deterministic logic, computed from its own inputs.
        value = sample_fn(prompt, system)
        if json_mode:
            if not isinstance(value, dict):
                raise TypeError('sample_fn must return a dict in JSON mode')
            return dict(text=json.dumps(value, ensure_ascii=False), parsed=value, model='caller-sample-fn')
        if not isinstance(value, str):
            raise TypeError('sample_fn must return a str in text mode')
        return dict(text=value, model='caller-sample-fn')
    if json_mode:
        return dict(text='{"ports": []}', parsed={'ports': []}, model='deterministic-heuristic')
    maritime = bool(re.search(r'\b(port|ports|customs|maritime|clearance)\b', prompt, re.I))
    subject = 'port verification' if maritime else 'this request'
    return dict(text=f'Sample mode: no AI provider used. No verified information is available for {subject}.',
                model='deterministic-heuristic')


def _trace(result, prompt, role, requested_model):
    entry = dict(timestamp=datetime.now(timezone.utc).isoformat(), agent_name='MeterlessGenerator',
                 action='generate', run_id=result['run_id'], provider=result['provider'],
                 requested_model=requested_model, served_model=result['model'],
                 duration_ms=result['duration_ms'], status=result['status'],
                 input_summary={'prompt': prompt[:200], 'role': role},
                 fallback_reason=result.get('fallback_reason'))
    # Redact before truncating too, so even a key straddling the 200-char limit
    # cannot appear as a prefix in a prompt preview.
    preview = prompt
    for name in SECRET_ENV:
        secret = _env(name)
        if secret:
            preview = preview.replace(secret, '[REDACTED]')
    entry['input_summary']['prompt'] = preview[:200]
    payload = json.dumps(entry, ensure_ascii=False)
    for name in SECRET_ENV:
        secret = _env(name)
        if secret:
            payload = payload.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]')
    path = Path(_env('TRACE_LOG_PATH') or DEFAULT_TRACE_LOG_DIR / f"trace-{result['run_id']}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(payload + '\n')
    if _env('TRACE_STDOUT', '1') != '0':
        print(payload, flush=True)


async def generate(prompt, system=None, *, json_mode=False, role='json', run_id=None,
                   sample_fn=None, model=None) -> dict:
    """Generate via Nightshift first, or Token Factory when that provider is active.

    model, if given, is the Nightshift model to request instead of METERLESS_MODEL.

    sample_fn(prompt, system), if given, supplies the sample result (a dict in
    JSON mode, a str otherwise) when SAMPLE_MODE is on or every provider fails.
    duration_ms includes probes, retries and fallbacks. run_id is generated if
    omitted and returned to the caller. Sample output has provider='sample',
    status='sample' and sample=True.
    """
    run_id = str(uuid4()) if run_id is None else run_id
    if not isinstance(run_id, str) or re.fullmatch(r'[A-Za-z0-9_-]+', run_id) is None:
        raise ValueError('run_id must contain only letters, digits, underscores or hyphens')
    started = time.monotonic()
    deadline = started + GENERATE_BUDGET_S
    reasons, result, provider = [], None, 'sample'
    requested_model = model or _setting('METERLESS_MODEL', 'MODEL_NAME', DEFAULT_MODEL)
    out_tokens = _output_tokens(role)
    if _env('SAMPLE_MODE').lower() == 'true':
        reasons.append('SAMPLE_MODE=true')
    else:
        from app.core import tokenfactory

        # Key set or LLM_PROVIDER=tokenfactory: Nemotron goes to the EU host only.
        use_tf = tokenfactory.active()
        base = _setting('METERLESS_BASE_URL', 'MODEL_BASE_URL', DEFAULT_BASE_URL).rstrip('/')
        key = _setting('METERLESS_RUNTIME_TOKEN', 'MODEL_API_KEY')
        headers = {'meterless-app': 'blue-intelligence', 'meterless-scope': 'user'}
        if key:
            headers['Authorization'] = f'Bearer {key}'
        async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=5.0)) as client:
            if not use_tf:
                status = await _probe(client, base, key, headers)
                if status == 'ready':
                    try:
                        result = await _complete(client, base + '/chat/completions', headers,
                                                 requested_model, prompt, system, json_mode,
                                                 min(REQUEST_TIMEOUT_S, deadline - time.monotonic()),
                                                 max_tokens=out_tokens)
                        provider = 'nightshift'
                    except _ProviderFailure as exc:
                        reasons.append(f'nightshift: {exc}')
                else:
                    reasons.append(f'nightshift: {status}')
            if result is None and use_tf:
                tf_key = tokenfactory.get_key()
                if not tf_key:
                    reasons.append('tokenfactory: not_configured')
                elif deadline - time.monotonic() < MIN_ATTEMPT_S:
                    reasons.append('tokenfactory: no_time_left')
                else:
                    explicit = _env('NEMOTRON_MODEL') or None
                    try:
                        requested_model = tokenfactory.resolve(role, explicit)
                        tf_url = tokenfactory.chat_url()
                    except RuntimeError as exc:
                        reasons.append('tokenfactory: host' if 'host' in str(exc) else 'tokenfactory: model')
                    else:
                        rid = tokenfactory.account_run(run_id)
                        try:
                            async with tokenfactory.budget_lock():
                                tokenfactory.ensure_budget(rid)
                                result = await _complete(
                                    client, tf_url, {'Authorization': f'Bearer {tf_key}'},
                                    requested_model, prompt, system, json_mode,
                                    min(REQUEST_TIMEOUT_S, deadline - time.monotonic()),
                                    max_tokens=out_tokens)
                                tokenfactory.record(
                                    result['model'], result.get('usage'), run_id=rid)
                            provider = 'tokenfactory'
                        except tokenfactory.TokenFactoryBudgetExceeded:
                            reasons.append('tokenfactory: budget')
                        except _ProviderFailure as exc:
                            reasons.append(f'tokenfactory: {exc}')
            elif result is None:
                nvidia_key = _env('NVIDIA_API_KEY')
                if not nvidia_key:
                    # Optional: the app needs only Zetaris and Nightshift settings.
                    reasons.append('nvidia: not_configured')
                elif deadline - time.monotonic() < MIN_ATTEMPT_S:
                    reasons.append('nvidia: no_time_left')
                else:
                    requested_model = _env('NEMOTRON_MODEL', DEFAULT_NEMOTRON_MODEL)
                    try:
                        result = await _complete(client, NVIDIA_URL,
                            {'Authorization': f'Bearer {nvidia_key}'}, requested_model, prompt, system,
                            json_mode, min(REQUEST_TIMEOUT_S, deadline - time.monotonic()),
                            max_tokens=out_tokens)
                        provider = 'nvidia'
                    except _ProviderFailure as exc:
                        reasons.append(f'nvidia: {exc}')
    if result is None:
        result = _sample(prompt, system, json_mode, sample_fn)
    result.update(provider=provider, run_id=run_id, sample=provider == 'sample',
                  status='sample' if provider == 'sample' else 'success',
                  duration_ms=round((time.monotonic() - started) * 1000, 3))
    if reasons:
        result['fallback_reason'] = '; '.join(reasons)
    _trace(result, prompt, role, requested_model)
    return result
