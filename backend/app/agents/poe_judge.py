"""NemotronJudge: an advisory critic of a zone's verdicts, never a source of evidence.

One Nemotron call per zone, through the Meterless generator (Nightshift first, then the
optional NVIDIA fallback). The judge reads only the official evidence text the run already
holds (the snippets in registry.official_evidence, fetched now or stored) and answers, for
each candidate port, poe / not_poe / unclear with a reason and a quoted excerpt:

    port.judge = {verdict, reason, quoted_excerpt, excerpt_verified, disagrees}

excerpt_verified is true only when the quote appears in that evidence text. The critique
trace event repeats each verdict, reason and excerpt (cut to TRACE_TEXT_CHARS). The judge
never writes tier, validation, official_evidence or confidence, so an excerpt, verified or
not, never counts as evidence. When it calls a flagged port a port of entry, it routes that port
into one more ResearchAgent -> EvaluatorAgent round; the verdict still comes from the same
deterministic scoring.

Skipped, and traced as skipped with the reason, in SAMPLE_MODE, when no port of the zone
has official evidence text, when no provider answers, on timeout or on an unusable answer.
"""
from __future__ import annotations

import asyncio
import os
import unicodedata
from datetime import datetime, timezone

from app.agents import registry_check
from app.agents.evaluator import RETRY_LIMIT, _write_trace
from app.core import generator

AGENT_NAME = 'NemotronJudge'
JUDGE_TIMEOUT_S = 90.0  # leaves the brief its own generator budget inside RUN_TIMEOUT_S
VERDICTS = ('poe', 'not_poe', 'unclear')
MIN_EXCERPT_CHARS = 20
MAX_EXCERPT_CHARS = 400
MAX_TEXTS = 20
TRACE_TEXT_CHARS = 200  # reason / excerpt length in the trace event (port.judge keeps them whole)
GATE_REFUSED = 'formalities.extract_max_llm_calls reached'
# One strict re-ask after a reply that was not valid JSON, even once repaired (#34's
# extraction recovery, applied to the judge: runs f3993a / 608d85 lost the Canaries'
# and Martinique's verdicts to one invalid Nightshift answer).
JUDGE_STRICT_RETRY = (
    '\n\nREMINDER (new attempt, the previous answer was not valid JSON): answer with ONE '
    'JSON object {"ports": [...]} and nothing else: no markdown, no ``` fence, no comment. '
    'Double quotes, no trailing comma, close every bracket. Keep each reason under 20 words '
    'and each quoted_excerpt under 200 characters so the answer fits.')
# Skip reasons that are a decision, not a failed call: the run summary lists the others.
DELIBERATE_SKIPS = ('SAMPLE_MODE=true', 'no official evidence text for this zone', GATE_REFUSED)
RETRY_HINT = 'Nemotron judge: named a port of entry in the official text; re-check its sources'
JUDGE_SYSTEM = (
    'You audit port-of-entry designations for a maritime clearance tool. Use only the official '
    'texts given. A port of entry is a port where foreign vessels may clear customs and '
    'immigration. A port name inside an address or a contact block is not a designation. '
    'For every candidate port give verdict "poe", "not_poe" or "unclear", a reason of at most '
    '30 words, and quoted_excerpt: a sentence copied exactly from one of the texts, or "".'
)
_PUNCTUATION = str.maketrans({'‘': "'", '’': "'", '´': "'", '`': "'", '“': '"', '”': '"',
                              '–': '-', '—': '-'})


def evidence_texts(ports: list[dict]) -> list[dict]:
    """The official evidence snippets the run holds for the zone, [{url, text}], once each."""
    out, seen = [], set()
    for port in ports:
        for e in (port.get('registry') or {}).get('official_evidence') or []:
            if not isinstance(e, dict) or not e.get('snippet'):
                continue
            if (e.get('url'), e['snippet']) not in seen:
                seen.add((e.get('url'), e['snippet']))
                out.append(dict(url=e.get('url'), text=e['snippet']))
    return out[:MAX_TEXTS]


def _normal(text: str | None) -> str:
    text = unicodedata.normalize('NFKC', (text or '').translate(_PUNCTUATION)).casefold()
    return ' '.join(text.split())


def excerpt_verified(excerpt: str | None, texts: list[dict]) -> bool:
    """True only when the excerpt (quotes, case and spacing aside) is inside one evidence text.
    A list row quoted whole counts however short: "(34) Vigo" is all the row says."""
    quote = _normal(excerpt).strip(' "\'')
    if quote and any(t.get('list_row') and quote == _normal(t['text']).strip(' "\'') for t in texts):
        return True
    return len(quote) >= MIN_EXCERPT_CHARS and any(quote in _normal(t['text']) for t in texts)


def judge_prompt(zone_name: str, ports: list[dict], texts: list[dict]) -> str:
    names = list(dict.fromkeys(p.get('name') for p in ports if p.get('name')))
    lines = [f'Zone: {zone_name}', 'Candidate ports:'] + [f'- {name}' for name in names]
    lines.append('Official texts:')
    lines += [f"[{i}] {t['url']}" + (f" ({t['list_row']})" if t.get('list_row') else '')
              + f"\n{t['text']}" for i, t in enumerate(texts, 1)]
    lines.append('Return JSON: {"ports": [{"name": "<candidate name as given>", '
                 '"verdict": "poe|not_poe|unclear", "reason": "...", "quoted_excerpt": "..."}]}, '
                 'one entry per candidate port.')
    return '\n'.join(lines)


def parse_judgments(parsed, texts: list[dict]) -> dict[str, dict]:
    """port_key -> {verdict, reason, quoted_excerpt, excerpt_verified}; invalid entries dropped."""
    items = parsed.get('ports') if isinstance(parsed, dict) else None
    out = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            continue
        verdict = str(item.get('verdict') or '').strip().lower()
        if verdict not in VERDICTS:
            continue
        excerpt = str(item.get('quoted_excerpt') or '').strip()[:MAX_EXCERPT_CHARS]
        out[registry_check.port_key(item['name'])] = dict(
            verdict=verdict, reason=str(item.get('reason') or '').strip()[:300],
            quoted_excerpt=excerpt, excerpt_verified=excerpt_verified(excerpt, texts))
    return out


def _clip(text: str) -> str:
    return text if len(text) <= TRACE_TEXT_CHARS else text[:TRACE_TEXT_CHARS - 1] + '…'


def _disagrees(judge_verdict: str, port_verdict: str | None) -> bool:
    return ((judge_verdict == 'poe' and port_verdict != 'pass')
            or (judge_verdict == 'not_poe' and port_verdict == 'pass'))


async def ask(zone_name: str, ports: list[dict], texts: list[dict], run_id: str,
              timeout_s: float = JUDGE_TIMEOUT_S, *,
              gate=None) -> tuple[dict, dict, str | None]:
    """(llm provenance, judgments by port_key, skip reason or None).

    ``gate`` (optional) is called once, right before the single generator call; when it
    returns False the call is not made and the zone is skipped with ``GATE_REFUSED``.
    The poe_pipeline runs pass the zone's share of the run call cap here.
    """
    if (os.environ.get('SAMPLE_MODE') or '').strip().lower() == 'true':
        return {}, {}, 'SAMPLE_MODE=true'
    if not texts:
        return {}, {}, 'no official evidence text for this zone'
    if gate is not None and not gate():
        return {}, {}, GATE_REFUSED
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    prompt = judge_prompt(zone_name, ports, texts)
    result, att, err = await _generate_once(prompt, run_id, timeout_s)
    recovery = dict(json_repaired=bool(att.get('repaired')), strict_retry=False, attempts=1)
    if _invalid_json(result, att):
        # Repair already ran inside the call (llm.repair_json): still invalid, so one strict
        # "JSON only" re-ask, paid from the same call share as the first one.
        left = deadline - loop.time()
        if left <= 1 or (gate is not None and not gate()):
            why = 'no time left' if left <= 1 else GATE_REFUSED
            return _llm(result, recovery), {}, f'invalid JSON; strict retry not run ({why})'
        recovery.update(strict_retry=True, attempts=2)
        result, att, err = await _generate_once(prompt + JUDGE_STRICT_RETRY, run_id, left)
        recovery['json_repaired'] = recovery['json_repaired'] or bool(att.get('repaired'))
        if _invalid_json(result, att):
            return _llm(result, recovery), {}, 'invalid JSON after repair and one strict retry'
    if err:
        return dict(recovery), {}, err
    llm = _llm(result, recovery)
    if result['provider'] == 'sample':
        return llm, {}, result.get('fallback_reason') or 'no provider answered'
    return llm, parse_judgments(result.get('parsed'), texts), None


def _llm(result: dict | None, recovery: dict) -> dict:
    result = result or {}
    return dict(provider=result.get('provider'), served_model=result.get('model'), **recovery)


def _invalid_json(result: dict | None, att: dict) -> bool:
    """The generator found no usable JSON: a provider said invalid_json and none answered."""
    if not result or result.get('provider') != 'sample':
        return False
    return bool(att.get('invalid_json')) or 'invalid_json' in (result.get('fallback_reason') or '')


async def _generate_once(prompt: str, run_id: str, timeout_s: float):
    """One judge call with #34's repair on (llm.extract_attempt scope): a fenced reply, a
    trailing comma or a verdict list cut at max_tokens is parsed by llm.repair_json, which
    keeps only complete verdicts and never completes a cut one. -> (result, attempt, error)."""
    from app.core import llm
    att = {'invalid_json': [], 'repaired': []}
    token = llm.extract_attempt.set(att)
    try:
        result = await asyncio.wait_for(generator.generate(
            prompt, JUDGE_SYSTEM, json_mode=True, role='judge',
            run_id=run_id, sample_fn=lambda prompt, system: {'ports': []}), timeout=timeout_s)
    except asyncio.TimeoutError:
        return None, att, f'no answer within {timeout_s:g}s'
    except Exception as exc:  # noqa: BLE001 - an advisory step never fails the run
        return None, att, f'judge failed: {type(exc).__name__}'
    finally:
        llm.extract_attempt.reset(token)
    return result, att, None


_ask = ask  # judge_zone (run_pipeline) and existing callers


async def judge_zone(state: dict, zone_name: str, *, timeout_s: float = JUDGE_TIMEOUT_S) -> dict:
    """Run the judge on evaluated ports; return the state with port.judge and any retry routing."""
    run_id, ports = state['run_id'], list(state.get('ports') or [])
    texts = evidence_texts(ports)
    llm, judged, reason = await _ask(zone_name, ports, texts, run_id, timeout_s)
    retry_allowed = int(state.get('retry_count') or 0) < RETRY_LIMIT
    out_ports, rows, retry = [], [], []
    for raw in ports:
        port = dict(raw)
        judge = None if reason else judged.get(registry_check.port_key(port.get('name')))
        if judge:
            port['judge'] = dict(judge, disagrees=_disagrees(judge['verdict'], port.get('verdict')))
            rows.append(dict(port_name=port.get('name'), verdict=judge['verdict'],
                             port_verdict=port.get('verdict'), excerpt_verified=judge['excerpt_verified'],
                             disagrees=port['judge']['disagrees'], reason=_clip(judge['reason']),
                             quoted_excerpt=_clip(judge['quoted_excerpt'])))
            # Routing only: evaluate() recomputes the verdict from the same scoring.
            if judge['verdict'] == 'poe' and port.get('verdict') == 'flag' and retry_allowed:
                port['verdict'], port['retry_hint'] = 'retry', RETRY_HINT
                retry.append(port.get('name'))
        out_ports.append(port)
    if not reason and not rows:
        reason = 'no usable answer'
    summary = dict(status='skipped' if reason else 'ok', reason=reason, provider=llm.get('provider'),
                   served_model=llm.get('served_model'), retry_requested=retry)
    event = dict(timestamp=datetime.now(timezone.utc).isoformat(), agent_name=AGENT_NAME, run_id=run_id,
                 action='critique', input_summary=dict(zone_name=zone_name, ports=len(ports),
                                                       evidence_texts=len(texts)),
                 **summary, judgments=rows, handoff={'from': AGENT_NAME, 'to': 'EvaluatorAgent'})
    _write_trace([event], run_id)
    out = dict(state)
    out['ports'] = out_ports
    out['needs_retry'] = [p for p in out_ports if p.get('verdict') == 'retry']
    out['judge'] = summary
    out['trace'] = list(state.get('trace') or []) + [event]
    return out
