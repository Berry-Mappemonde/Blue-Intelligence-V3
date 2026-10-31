"""Runnable product: fresh-clone CLI, POST /run, H-MEM research, fallbacks, bounds."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters import zetaris
from app.agents.evaluator import RETRY_LIMIT
from app.pipeline import run_pipeline as pipeline
from app.pipeline.api import app

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRST_INPUT = sorted((REPO_ROOT / 'input_examples').glob('*.json'))[0]
AGENTS = {'ZetarisAdapter', 'HMemAgent', 'EvaluatorAgent', 'NemotronJudge', 'ResearchAgent', 'BriefingAgent',
          'MeterlessGenerator'}
OUTPUT_KEYS = {'run_id', 'zone', 'mode', 'ports', 'summary', 'brief', 'llm', 'components', 'retries',
               'memory', 'trace_file', 'timings_ms', 'error', 'registry', 'judge'}
PORT_KEYS = {'name', 'verdict', 'confidence', 'lat', 'lon', 'reasons_en', 'sources', 'memories_recalled', 'research',
             'registry', 'judge'}
_CREDENTIAL_ENV = ('ZETARIS_API_KEY', 'ZETARIS_ORG_ID', 'ZETARIS_REST_URL', 'METERLESS_BASE_URL',
                   'METERLESS_RUNTIME_TOKEN', 'METERLESS_MODEL', 'MODEL_BASE_URL', 'MODEL_API_KEY',
                   'MODEL_NAME', 'NVIDIA_API_KEY')


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path, request):
    monkeypatch.setenv('HMEM_DIR', str(tmp_path / 'hmem'))
    monkeypatch.setenv('TRACE_LOG_PATH', str(tmp_path / 'trace.jsonl'))
    monkeypatch.setenv('TRACE_STDOUT', '0')
    if not request.node.name.startswith('test_live_'):
        monkeypatch.setenv('SAMPLE_MODE', 'true')
        for name in _CREDENTIAL_ENV:
            monkeypatch.delenv(name, raising=False)
    return tmp_path


def trace_rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]


def run(zone, run_id=None, **kwargs):
    return asyncio.run(pipeline.run_pipeline(zone, run_id, **kwargs))


def assert_schema(result):
    assert set(result) == OUTPUT_KEYS
    assert result['ports'] and all(set(p) == PORT_KEYS for p in result['ports'])
    assert isinstance(result['brief'], str) and result['brief']
    assert result['llm']['provider'] and result['llm']['model']
    assert result['timings_ms']['total'] >= 0


def run_cli(tmp_path, name, hmem_dir):
    """`python backend/run.py` from the repo root with no MONGO_URL, keys or .env."""
    out, trace = tmp_path / f'{name}.json', tmp_path / f'{name}.trace.jsonl'
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path), 'HMEM_DIR': str(hmem_dir),
           'TRACE_LOG_PATH': str(trace)}
    proc = subprocess.run(
        [sys.executable, 'backend/run.py', '--no-dotenv', '--input', str(FIRST_INPUT.relative_to(REPO_ROOT)),
         '--output', str(out)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc, json.loads(out.read_text(encoding='utf-8')), trace_rows(trace)


@pytest.fixture(scope='module')
def cli_runs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('cli')
    first = run_cli(tmp, 'first', tmp / 'hmem')
    second = run_cli(tmp, 'second', tmp / 'hmem')
    return first, second


def test_fresh_clone_cli_succeeds_in_sample_mode(cli_runs):
    (proc, result, _), _ = cli_runs
    assert 'SAMPLE_MODE enabled automatically' in proc.stderr
    assert_schema(result)
    assert result['error'] is None and result['mode'] == 'sample'
    assert result['components'] == {'zetaris': 'sample', 'registry': 'sample', 'llm': 'sample', 'judge': 'skipped'}
    assert result['judge']['reason'] == 'SAMPLE_MODE=true'
    assert result['llm']['model'] == 'caller-sample-fn'
    # stdout: JSONL trace events, then the same result as pretty JSON.
    lines = proc.stdout.splitlines()
    start = lines.index('{')
    assert all(json.loads(line)['run_id'] == result['run_id'] for line in lines[:start])
    assert json.loads('\n'.join(lines[start:])) == result


@pytest.mark.parametrize('cwd, script', [('repo', 'backend/run.py'), ('backend', 'run.py')])
def test_fresh_clone_cli_zone_interface(cwd, script, tmp_path):
    """--zone / --run-id / --sample-mode / --timeout / --trace, from a clean env, from either directory."""
    trace = tmp_path / 'zone.trace.jsonl'
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path), 'HMEM_DIR': str(tmp_path / 'hmem')}
    proc = subprocess.run(
        [sys.executable, script, '--no-dotenv', '--zone', 'Angola', '--run-id', 'cli-zone', '--sample-mode',
         '--timeout', '60', '--trace', str(trace)],
        cwd=REPO_ROOT if cwd == 'repo' else REPO_ROOT / 'backend', env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    result = json.loads('\n'.join(lines[lines.index('{'):]))
    assert_schema(result)
    assert result['run_id'] == 'cli-zone' and result['zone'] == 'Angola' and result['mode'] == 'sample'
    assert 'Pipeline Result' in proc.stderr and 'Run ID:   cli-zone' in proc.stderr  # summary on stderr
    assert {r['run_id'] for r in trace_rows(trace)} == {'cli-zone'}


def test_cli_rejects_bad_zone_input(tmp_path):
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path)}
    proc = subprocess.run([sys.executable, 'backend/run.py', '--no-dotenv', '--zone', 'Angola', '--run-id', '../x'],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and 'Invalid input' in proc.stderr


def test_cli_trace_has_one_run_id_and_handoffs(cli_runs):
    (_, result, rows), _ = cli_runs
    assert AGENTS - {'ResearchAgent'} <= {r['agent_name'] for r in rows}
    assert {r['run_id'] for r in rows} == {result['run_id']}
    handoffs = {(r['handoff']['from'], r['handoff']['to']) for r in rows if r.get('handoff')}
    assert {('HMemAgent', 'EvaluatorAgent'), ('BriefingAgent', 'HMemAgent')} <= handoffs
    assert [r['agent_name'] for r in rows][0] == 'RunPipeline' and rows[-1]['action'] == 'run_end'
    # Bermuda is tier 1 from stored official evidence: no retry for a missing official source.
    assert not any(r['action'] == 'prepare_retry' for r in rows)


def test_trace_has_every_agent_one_run_id_and_retry(isolated):
    # Benin's sample port has a registry match only (tier_registered) and no source URL,
    # so it goes through the retry round.
    result = run('Benin', 'retry-round')
    rows = trace_rows(isolated / 'trace.jsonl')
    assert AGENTS <= {r['agent_name'] for r in rows}
    assert {r['run_id'] for r in rows} == {'retry-round'} == {result['run_id']}
    assert rows[0]['agent_name'] == 'RunPipeline' and rows[-1]['action'] == 'run_end'
    assert any(r['action'] == 'prepare_retry' for r in rows) and result['retries'] == RETRY_LIMIT
    handoffs = {(r['handoff']['from'], r['handoff']['to']) for r in rows if r.get('handoff')}
    assert {('HMemAgent', 'EvaluatorAgent'), ('ResearchAgent', 'EvaluatorAgent'),
            ('BriefingAgent', 'HMemAgent')} <= handoffs


def test_second_run_recalls_first_run_memories(cli_runs):
    (_, first, _), (_, second, rows) = cli_runs
    assert first['memory']['recalled'] == 0
    assert second['memory']['recalled'] >= 1
    recall = [r for r in rows if r['agent_name'] == 'HMemAgent' and r['action'] == 'recall']
    assert recall and all(r['memories'] for r in recall)
    assert recall[0]['memories'][0]['provenance']['refs'] == [first['run_id']]


def test_research_uses_remembered_official_source(monkeypatch, isolated):
    official = 'https://www.customs.gov.bm/yachts'
    port = {'name': 'Port of St. George’s', 'lat': 32.38, 'lon': -64.67, 'source_urls': [official],
            'validated': True, 'geocode_source': 'nominatim', 'zone_name': 'Bermuda', 'mrgid': 8493, 'id': 'p1'}
    current = [port]
    monkeypatch.setattr(zetaris.ZetarisClient, 'get_ports', lambda self, **kw: [dict(p) for p in current])
    first = run('Bermuda', 'research-first')
    assert first['ports'][0]['research'] is None  # official source present: no retry
    current = [dict(port, source_urls=[])]  # this run's data lost the source
    second = run('Bermuda', 'research-second')
    out = second['ports'][0]
    assert out['research'] == {'outcome': 'hmem_official_source', 'url': official,
                               'memory_id': out['research']['memory_id']}
    assert out['sources'] == [{'url': official, 'provenance': 'hmem'}]
    assert out['verdict'] == 'flag' and out['confidence'] > 40 and second['retries'] == 1
    rows = [r for r in trace_rows(isolated / 'trace.jsonl') if r['run_id'] == 'research-second']
    research = [r for r in rows if r['agent_name'] == 'ResearchAgent']
    assert research[0]['outcome'] == 'hmem_official_source' and research[0]['url'] == official
    assert '(from verification memory)' in second['brief']


def test_post_run_returns_same_schema():
    client = TestClient(app)
    assert client.get('/healthz').json() == {'status': 'ok', 'sample_mode': True}
    response = client.post('/run', json={'zone_name': 'Angola', 'run_id': 'api-angola'})
    assert response.status_code == 200
    result = response.json()
    assert_schema(result)
    assert result['run_id'] == 'api-angola' and result['zone'] == 'Angola'
    assert client.post('/run', json={'zone_name': 'Angola', 'run_id': '../escape'}).status_code == 422
    assert client.post('/run', json={}).status_code == 422


def test_zetaris_live_failure_falls_back_to_sample(monkeypatch, isolated):
    monkeypatch.delenv('SAMPLE_MODE')
    monkeypatch.setenv('ZETARIS_API_KEY', 'test-key')
    monkeypatch.setenv('ZETARIS_ORG_ID', '1')
    monkeypatch.setenv('ZETARIS_REST_URL', 'http://127.0.0.1:9/api/v1.0')  # nothing listens
    monkeypatch.setenv('METERLESS_BASE_URL', 'http://127.0.0.1:9/v1')
    monkeypatch.setattr(zetaris.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(pipeline.evidence_store, '_mongo_records', lambda mrgid: [])  # no stored evidence
    result = run('Angola', 'zetaris-down')
    assert result['error'] is None and result['ports']
    assert result['components']['zetaris'] == 'fallback' and result['mode'] == 'sample'
    assert result['llm']['provider'] == 'sample'
    assert result['components']['registry'] == 'fallback'
    rows = trace_rows(isolated / 'trace.jsonl')
    events = [(r['action'], r.get('status')) for r in rows if r['agent_name'] == 'ZetarisAdapter']
    # One wait on the dead server: the registry step reuses the outage and reads the fixtures.
    assert events == [('zetaris_query', 'error'), ('zetaris_fallback', 'fallback'), ('zetaris_query', 'ok'),
                      ('evidence_lookup', 'ok'),
                      ('zetaris_fallback', 'fallback'), ('zetaris_query', 'ok'), ('zetaris_query', 'ok'),
                      ('registry_check', 'tier_registered')]
    assert 'test-key' not in (isolated / 'trace.jsonl').read_text()


def test_timeout_returns_partial_result(monkeypatch):
    monkeypatch.setattr(pipeline, 'recall_node', lambda state: time.sleep(30))
    started = time.monotonic()
    result = run('Angola', 'slow-run', timeout_s=0.5)
    assert time.monotonic() - started < 5
    assert 'timed out after 0.5s during HMemAgent.recall' in result['error']
    assert result['ports'][0]['verdict'] is None and result['summary'] == {'unevaluated': 1}
    assert result['memory']['remembered'] is False


def test_retries_are_capped_and_every_port_is_finalised():
    result = run('Benin')
    assert result['retries'] == RETRY_LIMIT
    assert {p['verdict'] for p in result['ports']} <= {'pass', 'flag'}


def test_unknown_zone_and_invalid_input():
    result = run('Atlantis', 'no-zone')
    assert result['ports'] == [] and "no ports of entry found for zone 'Atlantis'" in result['error']
    for zone, run_id in (('', None), ('Bermuda', '../x'), (None, None)):
        with pytest.raises(ValueError):
            run(zone, run_id)


def test_sample_brief_is_built_from_port_data():
    brief = pipeline.sample_brief('Angola', [{'name': 'Porto de Luanda', 'verdict': 'flag', 'confidence': 60,
                                             'check_reasons': ['single geocoder, not cross-checked'],
                                             'source_urls': ['https://amn.gov.ao/x']}])
    assert brief.splitlines() == [
        'Clearance brief: Angola',
        'Confirmed ports of entry: none confirmed in this run.',
        'Flagged, verify before relying on them:',
        '- Porto de Luanda (confidence 60/100): single geocoder, not cross-checked. Sources: https://amn.gov.ao/x',
        pipeline.DISCLAIMER,
    ]


GOV_BM_EVIDENCE = [
    {'url': 'https://www.gov.bm/department/customs', 'domain': 'gov.bm', 'evidence_source': 'stored_snippet'},
    {'url': 'https://planning.gov.bm/report.pdf', 'domain': 'planning.gov.bm', 'evidence_source': 'stored_snippet'},
]


def test_brief_lists_official_evidence_as_sources_and_never_an_empty_sources_line():
    ports = [
        {'name': 'Port of St. George’s', 'verdict': 'pass', 'confidence': 67, 'source_urls': [],
         'registry': {'official_evidence': GOV_BM_EVIDENCE}},
        {'name': 'Nowhere Quay', 'verdict': 'pass', 'confidence': 61, 'source_urls': []},
        {'name': 'Other Quay', 'verdict': 'flag', 'confidence': 30, 'source_urls': [], 'check_reasons': ['not geocoded']},
    ]
    lines = pipeline.sample_brief('Bermuda', ports).splitlines()
    assert lines[2] == ('- Port of St. George’s (confidence 67/100). Sources: '
                        'gov.bm: https://www.gov.bm/department/customs (official evidence), '
                        'planning.gov.bm: https://planning.gov.bm/report.pdf (official evidence)')
    assert lines[3].endswith('Sources: No source') and lines[5].endswith('Sources: No source')
    assert not any(line.rstrip().endswith('Sources:') for line in lines)


def test_evidence_url_already_in_the_sources_is_not_repeated():
    port = {'name': 'P', 'source_urls': ['https://www.gov.bm/department/customs'],
            'registry': {'official_evidence': GOV_BM_EVIDENCE}}
    assert pipeline._brief_sources(port) == [
        'https://www.gov.bm/department/customs', 'planning.gov.bm: https://planning.gov.bm/report.pdf (official evidence)']


def test_nemotron_brief_prompt_carries_the_official_evidence_sources():
    port = {'name': 'Port of St. George’s', 'verdict': 'pass', 'confidence': 67, 'source_urls': [],
            'registry': {'official_evidence': GOV_BM_EVIDENCE}}
    prompt = pipeline.brief_prompt('Bermuda', [port])
    rows = json.loads(prompt.split('Finalised ports (JSON):\n', 1)[1].split('\n', 1)[0])
    assert rows[0]['sources'][0] == 'gov.bm: https://www.gov.bm/department/customs (official evidence)'


def test_sample_run_brief_names_the_official_pages():
    brief = run('Bermuda', 'brief-sources')['brief']
    assert 'gov.bm: https://www.gov.bm/department/customs (official evidence)' in brief
    assert not any(line.rstrip().endswith('Sources:') for line in brief.splitlines())


@pytest.mark.skipif(os.environ.get('ZETARIS_LIVE') != '1', reason='Set ZETARIS_LIVE=1 for local Zetaris')
def test_live_zone_through_real_zetaris(monkeypatch):
    monkeypatch.setenv('SAMPLE_MODE', 'false')
    result = run('Bermuda', 'live-zetaris')
    assert result['components']['zetaris'] == 'live', result['error']
    assert result['mode'] == 'live' and result['ports']


@pytest.mark.skipif(os.environ.get('NIGHTSHIFT_LIVE') != '1', reason='Set NIGHTSHIFT_LIVE=1 for local Nightshift')
def test_live_brief_served_by_nemotron(monkeypatch):
    monkeypatch.setenv('SAMPLE_MODE', 'false')
    result = run('Angola', 'live-nightshift')
    assert result['llm']['provider'] == 'nightshift', result['llm']['fallback_reason']
    assert 'nemotron' in result['llm']['model'].lower(), result['llm']['model']
    assert 'Luanda' in result['brief']


def test_demo_page_serves_with_pinned_map_library_and_offline_fallback():
    import re
    response = TestClient(app).get('/')
    assert response.status_code == 200 and response.headers['content-type'].startswith('text/html')
    page = response.text
    assert '<title>PoE Reconciler</title>' in page
    assert "api('/zones')" in page and "new EventSource('/run/stream?zone='" in page
    # Only the pinned Leaflet build (with SRI) and OSM tiles are remote; no static <script src> or stylesheet.
    remote = set(re.findall(r"https://[^'\"\s)]+", page)) - {'https://www.openstreetmap.org/copyright'}
    assert remote == {'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
                      'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
                      'https://tile.openstreetmap.org/{z}/{x}/{y}.png'}
    assert page.count('sha256-') == 2
    assert '<script src=' not in page and '<link rel="stylesheet"' not in page
    assert 'function svgPlot' in page  # offline chart when Leaflet or tiles are unavailable


def _sse(response):
    """Parse a text/event-stream body into [(event, data), ...]."""
    events, kind, data = [], None, []
    for line in response.iter_lines():
        if line.startswith('event: '):
            kind = line[7:]
        elif line.startswith('data: '):
            data.append(line[6:])
        elif line == '' and kind:
            events.append((kind, json.loads('\n'.join(data))))
            kind, data = None, []
    return events


def test_run_stream_emits_trace_in_order_then_result(isolated):
    with TestClient(app).stream('GET', '/run/stream', params={'zone': 'Benin', 'run_id': 'sse-1'}) as response:
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/event-stream')
        events = _sse(response)
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == 'result' and set(kinds[:-1]) == {'trace'}
    trace = [data for kind, data in events if kind == 'trace']
    assert {e['run_id'] for e in trace} == {'sse-1'}
    assert trace[0]['action'] == 'run_start' and trace[-1]['action'] == 'run_end'
    assert trace == pipeline.load_trace('sse-1')  # same events, same order as the trace file
    assert [e['timestamp'] for e in trace] == sorted(e['timestamp'] for e in trace)
    assert AGENTS <= {e['agent_name'] for e in trace}
    result = events[-1][1]
    assert_schema(result)
    assert result['run_id'] == 'sse-1' and result['error'] is None


def test_run_stream_generates_run_id_and_filters_shared_trace_file(isolated):
    client = TestClient(app)
    client.post('/run', json={'zone_name': 'Angola', 'run_id': 'other-run'})  # same TRACE_LOG_PATH
    with client.stream('GET', '/run/stream', params={'zone': 'Angola'}) as response:
        events = _sse(response)
    run_id = events[-1][1]['run_id']
    assert run_id and {d['run_id'] for k, d in events if k == 'trace'} == {run_id}


def test_run_stream_rejects_bad_input():
    client = TestClient(app)
    assert client.get('/run/stream').status_code == 422
    assert client.get('/run/stream', params={'zone': ''}).status_code == 422
    assert client.get('/run/stream', params={'zone': 'Bermuda', 'run_id': '../x'}).status_code == 422


def test_run_stream_unknown_zone_ends_with_error_result():
    with TestClient(app).stream('GET', '/run/stream', params={'zone': 'Atlantis'}) as response:
        events = _sse(response)
    assert events[-1][0] == 'result' and 'no ports of entry' in events[-1][1]['error']
    assert [d['action'] for k, d in events if k == 'trace'][-1] == 'run_end'


def test_zones_in_sample_mode():
    data = TestClient(app).get('/zones').json()
    assert data['mode'] == 'sample'
    assert data['zones'] == sorted(data['zones']) and len(data['zones']) == 20
    assert {'Bermuda', 'Angola', 'Azores'} <= set(data['zones'])


def test_zones_fall_back_when_zetaris_is_down(monkeypatch):
    monkeypatch.delenv('SAMPLE_MODE')
    monkeypatch.setenv('ZETARIS_API_KEY', 'test-key')
    monkeypatch.setenv('ZETARIS_ORG_ID', '1')
    monkeypatch.setenv('ZETARIS_REST_URL', 'http://127.0.0.1:9/api/v1.0')
    monkeypatch.setattr(zetaris.time, 'sleep', lambda seconds: None)
    data = TestClient(app).get('/zones').json()
    assert data['mode'] == 'fallback' and 'Bermuda' in data['zones']


def test_trace_route_returns_the_runs_events():
    client = TestClient(app)
    result = client.post('/run', json={'zone_name': 'Benin', 'run_id': 'trace-api'}).json()
    response = client.get('/runs/trace-api/trace')
    assert response.status_code == 200
    events = response.json()['events']
    assert {e['run_id'] for e in events} == {'trace-api'} == {result['run_id']}
    assert AGENTS <= {e['agent_name'] for e in events}
    assert events[0]['action'] == 'run_start' and events[-1]['action'] == 'run_end'
    # A reused run_id appends to the trace; the route returns only the latest run.
    client.post('/run', json={'zone_name': 'Benin', 'run_id': 'trace-api'})
    again = client.get('/runs/trace-api/trace').json()['events']
    assert [e['action'] for e in again].count('run_start') == 1
    assert client.get('/runs/never-ran/trace').status_code == 404


@pytest.mark.parametrize('bad', ['%2E%2E', '..%2F..%2Fetc%2Fpasswd', 'bad.id', 'a%20b', 'x' * 65])
def test_trace_route_rejects_bad_run_ids(bad):
    response = TestClient(app).get(f'/runs/{bad}/trace')
    assert response.status_code in (404, 422), response.text
    assert 'events' not in response.text


def test_trace_route_rejects_traversal_with_422():
    assert TestClient(app).get('/runs/%2E%2E/trace').status_code == 422
    with pytest.raises(ValueError):
        pipeline.load_trace('../../etc/passwd')
