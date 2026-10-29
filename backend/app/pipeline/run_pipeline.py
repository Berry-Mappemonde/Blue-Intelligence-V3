"""Runnable PoE verification pipeline: one zone in, verified ports and a brief out.

ZetarisAdapter.get_ports -> ZetarisAdapter.registry_check -> HMemAgent.recall
  -> EvaluatorAgent.evaluate -> NemotronJudge.critique
  -> [while route == retry: ResearchAgent -> prepare_retry -> evaluate]
  -> finalise -> BriefingAgent -> HMemAgent.remember

Plain Python: LangGraph is not a dependency of this repository. Every step shares
one run_id and the trace contract of evaluator.py (TRACE_LOG_PATH, TRACE_STDOUT).
Synchronous steps run in daemon threads so RUN_TIMEOUT_S always returns, with a
partial result and an error field, even if a step never finishes.

ResearchAgent makes H-MEM load-bearing: a port routed to retry gets the official
source a previous run verified for that same port, tagged provenance "hmem".
BriefingAgent makes Nightshift load-bearing: the sailor-facing brief is generated
through Meterless (Nemotron when the route serves it), and its sample fallback
builds the same brief deterministically from the finalised port data.
The registry check tiers each candidate with official-source evidence (needs fetched
document text, which seed ports do not carry) and UN/LOCODE / World Port Index matches
held in Zetaris (app/agents/registry_check.py), independent of BI's own seed data.
NemotronJudge (app/agents/poe_judge.py) is an advisory critic: one Nemotron call per zone
on that official evidence text. It records port.judge and may route a flagged port into one
more research round, but never writes a tier, validation, evidence or confidence.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.adapters import evidence_store
from app.adapters.zetaris import ZetarisClient, ZetarisUnavailable
from app.agents import poe_judge, registry_check
from app.agents.evaluator import (
    RETRY_LIMIT, evaluate, finalise, is_official_source, prepare_retry,
    route_after_evaluation,
)
from app.agents.memory import recall_node, remember_node
from app.core import generator, paths

RUN_TIMEOUT_S = 300.0
PIPELINE_AGENT = 'RunPipeline'
RESEARCH_AGENT = 'ResearchAgent'
BRIEFING_AGENT = 'BriefingAgent'
_RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
_URL = re.compile(r'https?://[^\s;,]+')
BRIEF_SYSTEM = (
    'You write short port-clearance briefs for sailors. Use only the port data given. '
    'Never invent ports, sources, fees or rules. Plain text, under 150 words.'
)
DISCLAIMER = 'Indicative only: confirm entry formalities with the official authorities before departure.'


# ---------------------------------------------------------------------------
# Trace (same contract as evaluator.py)
# ---------------------------------------------------------------------------
def trace_path(run_id: str) -> Path:
    return Path(os.environ.get('TRACE_LOG_PATH') or paths.LOGS_DIR / f'trace-{run_id}.jsonl')


def _event(run_id: str, agent: str, action: str, **fields) -> dict:
    return dict(timestamp=datetime.now(timezone.utc).isoformat(), agent_name=agent,
                run_id=run_id, action=action, **fields)


def _write_trace(run_id: str, entries: list[dict]) -> None:
    if not entries:
        return
    payload = ''.join(json.dumps(entry, ensure_ascii=False) + '\n' for entry in entries)
    path = trace_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(payload)
    if os.environ.get('TRACE_STDOUT', '1') != '0':
        print(payload, end='', flush=True)


def validate_input(zone_name, run_id=None) -> tuple[str, str | None]:
    if not isinstance(zone_name, str) or not zone_name.strip() or len(zone_name) > 200:
        raise ValueError('zone_name must be a non-empty string of at most 200 characters')
    if run_id is not None and (not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)):
        raise ValueError('run_id must be 1-64 letters, digits, underscores or hyphens')
    return zone_name.strip(), run_id


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def fetch_ports(zone_name: str, run_id: str) -> tuple[list[dict], str]:
    """ZetarisAdapter step; a live failure falls back to the sample fixture."""
    client = ZetarisClient(run_id=run_id)
    try:
        return client.get_ports(zone_name=zone_name), client.mode
    except ZetarisUnavailable as exc:
        if client.mode != 'live':
            raise
        # Adapter messages are sanitized (no response bodies or credentials).
        _write_trace(run_id, [_event(run_id, 'ZetarisAdapter', 'zetaris_fallback', status='fallback',
                                     input_summary={'zone_name': zone_name}, reason=str(exc)[:200])])
        fallback = ZetarisClient(run_id=run_id)
        fallback.mode = 'sample'
        return fallback.get_ports(zone_name=zone_name), 'fallback'


def check_registries(ports: list[dict], zone_name: str, run_id: str,
                     zetaris_mode: str | None = None) -> tuple[list[dict], dict]:
    """Registry check step: UN/LOCODE + WPI evidence on every candidate port, plus official
    evidence from stored snippets of the zone (evidence_store.lookup; none on failure)."""
    mrgid = next((p.get('mrgid') for p in ports if p.get('mrgid') is not None), None)
    stored = evidence_store.lookup(mrgid, [p.get('name') for p in ports], run_id)
    return registry_check.check_ports(ports, dict(name=zone_name, mrgid=mrgid), run_id,
                                      live_failed=zetaris_mode == 'fallback', stored_records=stored)


def list_zones() -> dict:
    """Zones the current mode can run: live Zetaris, or the sample fixture as fallback."""
    client = ZetarisClient(run_id='zones')
    mode = client.mode
    if mode == 'live':
        try:
            rows = client.query('SELECT DISTINCT zone_name FROM blue_intel.poe_ports', limit=1000)
            return dict(mode='live', zones=sorted({r[0] for r in rows
                                                   if r and isinstance(r[0], str) and r[0] != 'null'}))
        except ZetarisUnavailable:
            mode = 'fallback'
    sample = ZetarisClient(run_id='zones')
    sample.mode = 'sample'
    return dict(mode=mode, zones=sorted({p['zone_name'] for p in sample.get_ports(limit=1000)
                                         if p['zone_name']}))


def load_trace(run_id: str) -> list[dict]:
    """Trace events of the latest run with this run_id (a reused run_id appends)."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError('run_id must be 1-64 letters, digits, underscores or hyphens')
    path = trace_path(run_id)
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('run_id') == run_id:
            if event.get('agent_name') == PIPELINE_AGENT and event.get('action') == 'run_start':
                events = []
            events.append(event)
    return events


def remembered_official_sources(memories: list[dict]) -> list[tuple[str, str]]:
    """(url, memory_id) for official sources in recalled verdicts, active ones first."""
    found: list[tuple[str, str]] = []
    for memory in sorted(memories, key=lambda m: m.get('supersededBy') is not None):
        for url in [memory.get('source') or ''] + _URL.findall(memory.get('content') or ''):
            if is_official_source([url]) and url not in (u for u, _ in found):
                found.append((url, memory.get('id')))
    return found


def research(state: dict) -> dict:
    """ResearchAgent: give each retry port the official source H-MEM remembers for it."""
    run_id = state['run_id']
    ports, entries = [], []
    for raw in state.get('ports') or []:
        port = dict(raw)
        if port.get('verdict') == 'retry':
            urls = list(port.get('source_urls') or [])
            remembered = remembered_official_sources(port.get('memory') or [])
            new = [(url, memory_id) for url, memory_id in remembered if url not in urls]
            if new:
                url, memory_id = new[0]
                port['source_urls'] = urls + [url]
                port['source_provenance'] = {**(port.get('source_provenance') or {}), url: 'hmem'}
                outcome = dict(outcome='hmem_official_source', url=url, memory_id=memory_id)
            elif remembered:
                outcome = dict(outcome='remembered official source already applied')
            else:
                outcome = dict(outcome='no prior official source')
            port['research'] = outcome
            entries.append(_event(
                run_id, RESEARCH_AGENT, 'research_port',
                input_summary=dict(port_name=port.get('name'), zone_name=port.get('zone_name'),
                                   retry_hint=port.get('retry_hint'),
                                   memories=len(port.get('memory') or [])),
                **outcome, handoff={'from': RESEARCH_AGENT, 'to': 'EvaluatorAgent'}))
        ports.append(port)
    _write_trace(run_id, entries)
    out = dict(state)
    out['ports'] = ports
    out['needs_retry'] = [p for p in ports if p.get('verdict') == 'retry']
    out['trace'] = list(state.get('trace') or []) + entries
    return out


def _brief_sources(port: dict) -> list[str]:
    """The port's own source URLs, then the official pages that designate it (registry
    official evidence: fetched now or a stored snippet), as "domain: url"."""
    provenance = port.get('source_provenance') or {}
    urls = [u for u in port.get('source_urls') or [] if u]
    out = [u + (' (from verification memory)' if provenance.get(u) == 'hmem' else '') for u in urls]
    for e in (port.get('registry') or {}).get('official_evidence') or []:
        url = e.get('url') if isinstance(e, dict) else None
        if url and url not in urls:
            urls.append(url)
            out.append(f"{e.get('domain') or url}: {url} (official evidence)")
    return out


def _brief_rows(ports: list[dict]) -> list[dict]:
    rows = []
    for port in ports:
        why = list(port.get('check_reasons') or []) or list(port.get('reasons_en') or [])[:3]
        if port.get('source_urls') and not is_official_source(port['source_urls']):
            why.append('no government source')
        rows.append(dict(
            name=port.get('name'), verdict=port.get('verdict'), confidence=port.get('confidence'), why=why,
            tier=_tier_text(port), sources=_brief_sources(port)))
    return rows


def _tier_text(port: dict) -> str | None:
    """The registry check's first reason: the tier and why (None before the check ran)."""
    reasons = (port.get('registry') or {}).get('reasons') or []
    return reasons[0] if reasons else None


def sample_brief(zone_name: str, ports: list[dict]) -> str:
    """The brief, built deterministically from the finalised port data."""
    rows = _brief_rows(ports)
    confirmed = [r for r in rows if r['verdict'] == 'pass']
    flagged = [r for r in rows if r['verdict'] != 'pass']
    lines = [f'Clearance brief: {zone_name}']
    lines.append('Confirmed ports of entry:' + ('' if confirmed else ' none confirmed in this run.'))
    for r in confirmed:
        tier = f" {r['tier']}." if r['tier'] else ''
        lines.append(f"- {r['name']} (confidence {r['confidence']}/100).{tier} "
                     f"Sources: {', '.join(r['sources']) or 'No source'}")
    if flagged:
        lines.append('Flagged, verify before relying on them:')
    for r in flagged:
        sources = ', '.join(r['sources']) or 'No source'
        tier = f" {r['tier']}." if r['tier'] else ''
        lines.append(f"- {r['name']} (confidence {r['confidence']}/100): {'; '.join(r['why'])}.{tier} "
                     f"Sources: {sources}")
    lines.append(DISCLAIMER)
    return '\n'.join(lines)


def brief_prompt(zone_name: str, ports: list[dict]) -> str:
    return (f'Zone: {zone_name}\nFinalised ports (JSON):\n'
            f'{json.dumps(_brief_rows(ports), ensure_ascii=False)}\n'
            'Write the brief: which ports are confirmed (verdict "pass"), which are flagged and why, '
            'each with its sources and its evidence tier (state the tier as given; a registry listing '
            'alone is not port-of-entry status). '
            f'End with: "{DISCLAIMER}"')


async def write_brief(state: dict, zone_name: str) -> dict:
    """BriefingAgent: Meterless generation, with a deterministic data-built fallback."""
    run_id, ports = state['run_id'], list(state.get('ports') or [])
    result = await generator.generate(
        brief_prompt(zone_name, ports), BRIEF_SYSTEM, role='brief', run_id=run_id,
        sample_fn=lambda prompt, system: sample_brief(zone_name, ports))
    llm = dict(provider=result['provider'], model=result['model'],
               fallback_reason=result.get('fallback_reason'))
    _write_trace(run_id, [_event(
        run_id, BRIEFING_AGENT, 'brief',
        input_summary=dict(zone_name=zone_name, ports=len(ports)),
        provider=llm['provider'], served_model=llm['model'], fallback_reason=llm['fallback_reason'],
        handoff={'from': BRIEFING_AGENT, 'to': 'HMemAgent'})])
    return dict(text=result['text'], **llm)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def _in_thread(fn, *args):
    """Run fn in a daemon thread: a step that never returns cannot block shutdown."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def settle(value, error):
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)

    def work():
        try:
            value, error = fn(*args), None
        except BaseException as exc:  # noqa: BLE001 — re-raised in the event loop
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(settle, value, error)
        except RuntimeError:
            pass  # loop already closed after a timeout

    threading.Thread(target=work, daemon=True, name=f'pipeline-{fn.__name__}').start()
    return await future


async def _step(ctx: dict, name: str, fn, *args):
    ctx['step'] = name
    started = time.monotonic()
    try:
        return await _in_thread(fn, *args)
    finally:
        elapsed = round((time.monotonic() - started) * 1000, 1)
        ctx['timings'][name] = round(ctx['timings'].get(name, 0) + elapsed, 1)


async def _timed(ctx: dict, name: str, awaitable):
    """Await an async step (the event loop's own timeout applies) and record its duration."""
    ctx['step'] = name
    started = time.monotonic()
    try:
        return await awaitable
    finally:
        elapsed = round((time.monotonic() - started) * 1000, 1)
        ctx['timings'][name] = round(ctx['timings'].get(name, 0) + elapsed, 1)


async def _run(ctx: dict) -> None:
    zone_name, run_id = ctx['zone'], ctx['run_id']
    ports, ctx['zetaris_mode'] = await _step(ctx, 'ZetarisAdapter.get_ports', fetch_ports, zone_name, run_id)
    state = ctx['state'] = {'run_id': run_id, 'ports': ports}
    if not ports:
        ctx['error'] = f'no ports of entry found for zone {zone_name!r}'
        return
    ports, ctx['registry'] = await _step(ctx, 'ZetarisAdapter.registry_check', check_registries,
                                         ports, zone_name, run_id, ctx['zetaris_mode'])
    state = ctx['state'] = {'run_id': run_id, 'ports': ports}
    state = ctx['state'] = await _step(ctx, 'HMemAgent.recall', recall_node, state)
    state = ctx['state'] = await _step(ctx, 'EvaluatorAgent.evaluate', evaluate, state)
    state = ctx['state'] = await _timed(ctx, 'NemotronJudge.critique', poe_judge.judge_zone(state, zone_name))
    for _ in range(RETRY_LIMIT):  # route_after_evaluation also enforces the cap
        if route_after_evaluation(state) != 'retry':
            break
        state = ctx['state'] = await _step(ctx, 'ResearchAgent.research', research, state)
        state = ctx['state'] = await _step(ctx, 'EvaluatorAgent.prepare_retry', prepare_retry, state)
        state = ctx['state'] = await _step(ctx, 'EvaluatorAgent.evaluate', evaluate, state)
    state = ctx['state'] = await _step(ctx, 'EvaluatorAgent.finalise', finalise, state)
    ctx['brief'] = await _timed(ctx, 'BriefingAgent.brief', write_brief(state, zone_name))
    ctx['state'] = await _step(ctx, 'HMemAgent.remember', remember_node, state)
    ctx['remembered'] = True


def _port_out(port: dict) -> dict:
    provenance = port.get('source_provenance') or {}
    return dict(name=port.get('name'), verdict=port.get('verdict'), confidence=port.get('confidence'),
                lat=port.get('lat'), lon=port.get('lon'),
                reasons_en=list(port.get('reasons_en') or []),
                sources=[dict(url=u, provenance=provenance.get(u, 'zetaris'))
                         for u in port.get('source_urls') or []],
                memories_recalled=len(port.get('memory') or []), research=port.get('research'),
                registry=port.get('registry'), judge=port.get('judge'))


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(paths.REPO_ROOT))
    except ValueError:
        return str(path)


def _memory_out(state: dict, ports: list[dict], remembered: bool) -> dict:
    """H-MEM activity of the run: memories recalled, and what remember did per port."""
    outcomes = [e.get('outcome') for e in state.get('trace') or []
                if e.get('agent_name') == 'HMemAgent' and e.get('action') == 'remember']
    return dict(recalled=sum(p['memories_recalled'] for p in ports), remembered=remembered,
                created=outcomes.count('created'), reinforced=outcomes.count('reinforced'),
                superseded=outcomes.count('superseded'))


def _result(ctx: dict, total_ms: float) -> dict:
    state = ctx['state']
    ports = [_port_out(p) for p in state.get('ports') or []]
    brief = ctx.get('brief') or {}
    zetaris_mode = ctx.get('zetaris_mode')
    summary = {}
    for port in ports:
        summary[port['verdict'] or 'unevaluated'] = summary.get(port['verdict'] or 'unevaluated', 0) + 1
    return dict(
        run_id=ctx['run_id'], zone=ctx['zone'],
        mode='live' if zetaris_mode == 'live' else 'sample',
        ports=ports, summary=summary, brief=brief.get('text'),
        llm=dict(provider=brief.get('provider'), model=brief.get('model'),
                 fallback_reason=brief.get('fallback_reason')),
        components=dict(zetaris=zetaris_mode, registry=(ctx.get('registry') or {}).get('mode'),
                        llm=brief.get('provider'), judge=(state.get('judge') or {}).get('status')),
        registry=ctx.get('registry'), judge=state.get('judge'),
        retries=int(state.get('retry_count') or 0),
        memory=_memory_out(state, ports, bool(ctx.get('remembered'))),
        trace_file=_display_path(trace_path(ctx['run_id'])),
        timings_ms=dict(ctx['timings'], total=total_ms),
        error=ctx.get('error'),
    )


async def run_pipeline(zone_name: str, run_id: str | None = None, *,
                       timeout_s: float = RUN_TIMEOUT_S) -> dict:
    """Verify one zone's Ports of Entry; always returns within timeout_s (plus bookkeeping)."""
    zone_name, run_id = validate_input(zone_name, run_id)
    if run_id is None:
        run_id = str(uuid4())
    ctx = dict(run_id=run_id, zone=zone_name, state={'run_id': run_id, 'ports': []},
               step=None, timings={}, error=None)
    _write_trace(run_id, [_event(run_id, PIPELINE_AGENT, 'run_start',
                                 input_summary=dict(zone_name=zone_name, timeout_s=timeout_s))])
    started = time.monotonic()
    try:
        await asyncio.wait_for(_run(ctx), timeout=timeout_s)
    except asyncio.TimeoutError:
        ctx['error'] = f"run timed out after {timeout_s:g}s during {ctx['step']}; partial result"
    except Exception as exc:  # noqa: BLE001 — reported in the result, never a hang
        ctx['error'] = f"{ctx['step']} failed: {type(exc).__name__}: {str(exc)[:200]}"
    result = _result(ctx, round((time.monotonic() - started) * 1000, 1))
    _write_trace(run_id, [_event(run_id, PIPELINE_AGENT, 'run_end',
                                 status='error' if result['error'] else 'ok', error=result['error'],
                                 summary=result['summary'], timings_ms=result['timings_ms'])])
    return result


async def stream_run(zone_name: str, run_id: str, *, poll_s: float = 0.05):
    """Run the pipeline and yield ('trace', event) as each agent writes it, then ('result', result).

    Agents append to the run's trace file; this tails it from the current end,
    keeps only this run_id (TRACE_LOG_PATH may be shared) and reads once more
    after the run finishes, so no event is lost.
    """
    path = trace_path(run_id)
    offset = path.stat().st_size if path.is_file() else 0
    task = asyncio.create_task(run_pipeline(zone_name, run_id))
    pending = b''
    while True:
        finished = task.done()
        if path.is_file():
            with path.open('rb') as stream:
                stream.seek(offset)
                chunk = stream.read()
            offset += len(chunk)
            *lines, pending = (pending + chunk).split(b'\n')
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get('run_id') == run_id:
                    yield 'trace', event
        if finished:
            break
        await asyncio.sleep(poll_s)
    yield 'result', task.result()
