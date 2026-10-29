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
    """True only when the excerpt (quotes, case and spacing aside) is inside one evidence text."""
    quote = _normal(excerpt).strip(' "\'')
    return len(quote) >= MIN_EXCERPT_CHARS and any(quote in _normal(t['text']) for t in texts)


def judge_prompt(zone_name: str, ports: list[dict], texts: list[dict]) -> str:
    names = list(dict.fromkeys(p.get('name') for p in ports if p.get('name')))
    lines = [f'Zone: {zone_name}', 'Candidate ports:'] + [f'- {name}' for name in names]
    lines.append('Official texts:')
    lines += [f"[{i}] {t['url']}\n{t['text']}" for i, t in enumerate(texts, 1)]
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


async def _ask(zone_name: str, ports: list[dict], texts: list[dict], run_id: str,
               timeout_s: float) -> tuple[dict, dict, str | None]:
    """(llm provenance, judgments by port_key, skip reason or None)."""
    if (os.environ.get('SAMPLE_MODE') or '').strip().lower() == 'true':
        return {}, {}, 'SAMPLE_MODE=true'
    if not texts:
        return {}, {}, 'no official evidence text for this zone'
    try:
        result = await asyncio.wait_for(generator.generate(
            judge_prompt(zone_name, ports, texts), JUDGE_SYSTEM, json_mode=True, role='judge',
            run_id=run_id, sample_fn=lambda prompt, system: {'ports': []}), timeout=timeout_s)
    except asyncio.TimeoutError:
        return {}, {}, f'no answer within {timeout_s:g}s'
    except Exception as exc:  # noqa: BLE001 - an advisory step never fails the run
        return {}, {}, f'judge failed: {type(exc).__name__}'
    llm = dict(provider=result['provider'], served_model=result['model'])
    if result['provider'] == 'sample':
        return llm, {}, result.get('fallback_reason') or 'no provider answered'
    return llm, parse_judgments(result.get('parsed'), texts), None


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
