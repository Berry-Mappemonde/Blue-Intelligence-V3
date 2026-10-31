"""Offline provider orchestration plus opt-in real Nightshift/Nemotron proof."""
import asyncio
import json
import os
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core import generator, llm


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path, request):
    generator._probe_cache.clear()
    generator._probe_locks.clear()
    monkeypatch.setenv('TRACE_LOG_PATH', str(tmp_path / 'trace.jsonl'))
    monkeypatch.setenv('TRACE_STDOUT', '1')
    if request.node.name != 'test_live_nightshift_nemotron':
        for name in ('SAMPLE_MODE', 'METERLESS_MODEL', 'MODEL_NAME', 'MODEL_BASE_URL', 'MODEL_API_KEY',
                     'NEMOTRON_MODEL', 'LLM_PROVIDER', 'NEBIUS_API_KEY', 'TF_RUN_BUDGET_USD',
                     'TF_PRICES_JSON', 'BI_TF_BASE'):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv('METERLESS_BASE_URL', generator.DEFAULT_BASE_URL)
        monkeypatch.setenv('METERLESS_RUNTIME_TOKEN', 'nightshift-test-secret')
        monkeypatch.setenv('NVIDIA_API_KEY', 'nvidia-test-secret')
        monkeypatch.setattr(generator.asyncio, 'sleep', AsyncMock())
        # Tests must explicitly install a transport; otherwise network is forbidden.
        monkeypatch.setattr(httpx.AsyncClient, 'send', AsyncMock(side_effect=AssertionError('Unexpected network')))
    yield tmp_path / 'trace.jsonl'
    generator._probe_cache.clear()
    generator._probe_locks.clear()


@pytest.fixture
def transport(monkeypatch):
    original = httpx.AsyncClient
    # Restore send for MockTransport clients only, bypassing the network guard.
    original_send = _ORIGINAL_SEND
    requests = []

    def install(handler):
        def dispatch(request):
            requests.append(request)
            return handler(request)

        class MockClient(original):
            send = original_send

        def factory(**kwargs):
            assert kwargs['timeout'].read == generator.REQUEST_TIMEOUT_S
            return MockClient(transport=httpx.MockTransport(dispatch), **kwargs)
        monkeypatch.setattr(generator.httpx, 'AsyncClient', factory)
        return requests
    return install


_ORIGINAL_SEND = httpx.AsyncClient.send


def answer(text='{"ports": []}', model='nvidia/nemotron-3.5-lightning-30b-a3b'):
    return httpx.Response(200, json={'model': model, 'choices': [{'message': {'content': text}}]})


def run(**kwargs):
    return asyncio.run(generator.generate('Extract ports from this source.', run_id='test-run', **kwargs))


def test_nightshift_success_headers_actual_model_and_trace(transport, isolated_environment, capsys):
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer nightshift-test-secret'
        assert request.headers['meterless-app'] == 'blue-intelligence'
        assert request.headers['meterless-scope'] == 'user'
        if request.method == 'GET':
            assert request.url.path == '/v1/models'
            return httpx.Response(200, json={'data': [{'id': 'meterless/auto'}]})
        payload = json.loads(request.content)
        assert payload['model'] == 'meterless/auto'
        assert payload['response_format'] == {'type': 'json_object'}
        return answer('```json\n{"ports": [{"name": "Papeete"}]}\n```')
    requests = transport(handler)
    result = run(json_mode=True)
    assert result['provider'] == 'nightshift'
    assert 'nemotron' in result['model']
    assert result['parsed']['ports'][0]['name'] == 'Papeete'
    assert len(requests) == 2
    entry = json.loads(isolated_environment.read_text())
    assert entry['agent_name'] == 'MeterlessGenerator'
    assert entry['requested_model'] == 'meterless/auto'
    assert entry['served_model'] == result['model']
    assert entry['run_id'] == 'test-run' and entry['status'] == 'success'
    assert entry['duration_ms'] >= 0
    assert capsys.readouterr().out == isolated_environment.read_text()


def test_offline_nightshift_nvidia_fallback(transport):
    def handler(request):
        if request.url.host == '127.0.0.1':
            raise httpx.ConnectError('secret body should never leak', request=request)
        assert request.headers['Authorization'] == 'Bearer nvidia-test-secret'
        assert 'meterless-app' not in request.headers
        assert json.loads(request.content)['model'] == generator.DEFAULT_NEMOTRON_MODEL
        return answer('Nemotron processed the port source.')
    requests = transport(handler)
    result = run()
    assert result['provider'] == 'nvidia'
    assert result['fallback_reason'] == 'nightshift: offline'
    assert len(requests) == 2  # one probe attempt, then NVIDIA
    assert 'parsed' not in result


def test_both_down_returns_explicit_sample(transport):
    requests = transport(lambda request: httpx.Response(503, text='private server body'))
    result = run(json_mode=True)
    assert len(requests) == 3  # probe once, NVIDIA request plus one retry
    assert result['provider'] == 'sample' and result['sample']
    assert result['parsed'] == {'ports': []}
    assert result['status'] == 'sample'
    assert result['fallback_reason'] == 'nightshift: http_503; nvidia: http_503'


@pytest.mark.parametrize('status', [401, 403])
def test_unauthorized_is_not_offline_and_does_not_retry(status, transport):
    requests = transport(lambda r: httpx.Response(status) if r.method == 'GET' else answer())
    result = run()
    assert result['provider'] == 'nvidia'
    assert result['fallback_reason'] == 'nightshift: unauthorized'
    assert len(requests) == 2


@pytest.mark.parametrize('failure', ['5xx', 'connection', 'timeout'])
def test_chat_retries_then_success(failure, transport):
    posts = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': []})
        posts.append(request)
        if len(posts) < 2:
            if failure == 'connection':
                raise httpx.ConnectError('offline', request=request)
            if failure == 'timeout':
                raise httpx.ReadTimeout('timeout', request=request)
            return httpx.Response(502)
        return answer()
    transport(handler)
    assert run()['provider'] == 'nightshift'
    assert len(posts) == 2
    assert [call.args[0] for call in generator.asyncio.sleep.call_args_list] == [generator.RETRY_BACKOFF_S]


def test_chat_retries_at_most_once_then_falls_back(transport):
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        if request.method == 'GET':
            return httpx.Response(200, json={'data': []})
        return httpx.Response(502) if request.url.host == '127.0.0.1' else answer()
    transport(handler)
    result = run()
    assert result['provider'] == 'nvidia'
    assert result['fallback_reason'] == 'nightshift: http_502'
    assert hosts == ['127.0.0.1', '127.0.0.1', '127.0.0.1', 'integrate.api.nvidia.com']


@pytest.mark.parametrize('content, expected', [
    ('The user wants a brief. Draft...</think>Confirmed ports: none.', 'Confirmed ports: none.'),
    ('<think>plan the answer</think>\nConfirmed ports: none.', 'Confirmed ports: none.'),
    ('<THINK>a</THINK>x</think>  Final text  ', 'Final text'),
    ('No reasoning here.', 'No reasoning here.'),
])
def test_reasoning_is_stripped_from_content(content, expected, transport):
    transport(lambda r: httpx.Response(200, json={'data': []}) if r.method == 'GET' else answer(content))
    result = run()
    assert result['text'] == expected and result['provider'] == 'nightshift'


def test_reasoning_stripped_before_json_parse(transport):
    transport(lambda r: httpx.Response(200, json={'data': []}) if r.method == 'GET'
              else answer('{"draft": true} thinking</think>{"ports": [{"name": "Papeete"}]}'))
    assert run(json_mode=True)['parsed'] == {'ports': [{'name': 'Papeete'}]}


def test_reasoning_only_answer_is_invalid_and_falls_back(transport):
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': []})
        if request.url.host == '127.0.0.1':
            return answer('only reasoning</think>   ')
        return answer('Final from NVIDIA')
    transport(handler)
    result = run()
    assert result['provider'] == 'nvidia' and result['text'] == 'Final from NVIDIA'
    assert result['fallback_reason'] == 'nightshift: invalid_response'


def test_probe_cache_ttl_configuration_and_concurrent_calls(transport, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(generator.time, 'monotonic', lambda: now[0])
    requests = transport(lambda r: httpx.Response(200, json={'data': []}) if r.method == 'GET' else answer())
    async def calls():
        await asyncio.gather(*(generator.generate('port', run_id='cache') for _ in range(3)))
        assert sum(r.method == 'GET' for r in requests) == 1
        now[0] += 59
        await generator.generate('port')
        assert sum(r.method == 'GET' for r in requests) == 1
        now[0] += 2
        await generator.generate('port')
        assert sum(r.method == 'GET' for r in requests) == 2
        monkeypatch.setenv('METERLESS_RUNTIME_TOKEN', 'new-test-key')
        await generator.generate('port')
        assert sum(r.method == 'GET' for r in requests) == 3
    asyncio.run(calls())


def test_explicit_sample_never_uses_network(monkeypatch):
    monkeypatch.setenv('SAMPLE_MODE', 'true')
    first, second = run(json_mode=True), run(json_mode=True)
    assert first['text'] == second['text'] == '{"ports": []}'
    assert first['fallback_reason'] == 'SAMPLE_MODE=true'
    assert 'Sample mode' in run()['text']


def test_no_keys_offline_skips_nvidia_straight_to_sample(transport, monkeypatch):
    monkeypatch.delenv('METERLESS_RUNTIME_TOKEN')
    monkeypatch.delenv('NVIDIA_API_KEY')
    def handler(request):
        assert request.url.host == '127.0.0.1', 'NVIDIA must not be called without a key'
        assert 'authorization' not in request.headers
        raise httpx.ConnectError('offline')
    requests = transport(handler)
    result = run()
    assert result['provider'] == 'sample'
    assert result['fallback_reason'] == 'nightshift: offline; nvidia: not_configured'
    assert len(requests) == 1


def test_secrets_redacted_preview_truncated_and_stdout_switch(transport, isolated_environment, monkeypatch, capsys):
    transport(lambda r: httpx.Response(401, text='nightshift-test-secret nvidia-test-secret'))
    prompt = 'x' * 190 + 'nightshift-test-secret nvidia-test-secret ' + 'z' * 300
    asyncio.run(generator.generate(prompt, run_id='redact'))
    text = isolated_environment.read_text()
    assert 'nightshift-test-secret' not in text and 'nvidia-test-secret' not in text
    assert len(json.loads(text)['input_summary']['prompt']) <= 200
    assert capsys.readouterr().out == text
    monkeypatch.setenv('TRACE_STDOUT', '0')
    run()
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('response', [answer('not JSON'), httpx.Response(200, json={}),
                                   httpx.Response(200, text='not a response')])
def test_invalid_response_falls_back(response, transport):
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': []})
        return response if request.url.host == '127.0.0.1' else answer()
    transport(handler)
    assert run(json_mode=True)['provider'] == 'nvidia'


def test_default_trace_path_and_safe_run_id(monkeypatch, tmp_path):
    monkeypatch.setenv('SAMPLE_MODE', 'true')
    monkeypatch.delenv('TRACE_LOG_PATH')
    from pathlib import Path
    assert generator.DEFAULT_TRACE_LOG_DIR == Path(__file__).resolve().parents[2] / 'logs'
    monkeypatch.setattr(generator, 'DEFAULT_TRACE_LOG_DIR', tmp_path)
    result = run()
    assert json.loads((tmp_path / 'trace-test-run.jsonl').read_text())['run_id'] == result['run_id']
    with pytest.raises(ValueError, match='run_id'):
        asyncio.run(generator.generate('test', run_id='../outside'))



@pytest.mark.skipif(os.environ.get('NIGHTSHIFT_LIVE') != '1', reason='Set NIGHTSHIFT_LIVE=1 for local Nightshift')
def test_live_nightshift_nemotron(monkeypatch):
    monkeypatch.setenv('SAMPLE_MODE', 'false')
    result = asyncio.run(generator.generate(
        'Extract only the port explicitly named in this source: "Papeete is a port of entry." '
        'Return JSON {"ports": [{"name": "..."}]}.', json_mode=True, run_id='nightshift-live'))
    assert result['provider'] == 'nightshift', result.get('fallback_reason')
    assert 'nemotron' in result['model'].lower(), result['model']
    assert result['parsed']['ports'][0]['name'].lower() == 'papeete'





def test_official_env_names_and_default_model(transport, monkeypatch, isolated_environment):
    monkeypatch.setenv('METERLESS_BASE_URL', 'http://nightshift.test:9000/v1/')
    def handler(request):
        assert request.url.host == 'nightshift.test'
        assert request.headers['Authorization'] == 'Bearer nightshift-test-secret'
        if request.method == 'GET':
            assert request.url.path == '/v1/models'
            return httpx.Response(200, json={'data': []})
        assert json.loads(request.content)['model'] == 'meterless/auto'
        return answer()
    transport(handler)
    assert generator.DEFAULT_MODEL == 'meterless/auto'
    assert run()['provider'] == 'nightshift'
    assert json.loads(isolated_environment.read_text())['requested_model'] == 'meterless/auto'


@pytest.mark.parametrize('official', [True, False])
def test_legacy_env_names_only_when_official_unset(official, transport, monkeypatch, isolated_environment):
    monkeypatch.setenv('MODEL_BASE_URL', 'http://legacy.test/v1')
    monkeypatch.setenv('MODEL_API_KEY', 'legacy-secret')
    monkeypatch.setenv('MODEL_NAME', 'legacy/model')
    if official:
        monkeypatch.setenv('METERLESS_BASE_URL', 'http://official.test/v1')
        monkeypatch.setenv('METERLESS_MODEL', 'meterless/fast')
        host, key, model = 'official.test', 'nightshift-test-secret', 'meterless/fast'
    else:
        monkeypatch.delenv('METERLESS_BASE_URL')
        monkeypatch.delenv('METERLESS_RUNTIME_TOKEN')
        host, key, model = 'legacy.test', 'legacy-secret', 'legacy/model'
    def handler(request):
        assert request.url.host == host
        assert request.headers['Authorization'] == f'Bearer {key}'
        if request.method == 'GET':
            return httpx.Response(200, json={'data': []})
        assert json.loads(request.content)['model'] == model
        return answer()
    transport(handler)
    assert run()['provider'] == 'nightshift'
    assert json.loads(isolated_environment.read_text())['requested_model'] == model


def test_worst_case_latency_budget():
    assert generator.REQUEST_TIMEOUT_S == 90 and generator.MAX_RETRIES == 1
    assert generator.PROBE_TIMEOUT_S == 3 and generator.PROBE_TTL_S == 60
    bounds = generator.worst_case_seconds()
    # Probe plus one 90 s provider budget (request and retry share it) before falling back.
    assert bounds['nightshift'] == generator.PROBE_TIMEOUT_S + generator.REQUEST_TIMEOUT_S == 93
    # The whole call, including optional NVIDIA, is capped well under 3 minutes.
    assert bounds['nightshift'] <= bounds['total'] == generator.GENERATE_BUDGET_S <= 150 < 180


@pytest.mark.parametrize('hang', ['probe', 'chat'])
def test_hanging_providers_respect_budget(hang, transport, monkeypatch):
    # Same algorithm with seconds scaled down: every hanging request must be cut off.
    for name, value in (('PROBE_TIMEOUT_S', 0.05), ('REQUEST_TIMEOUT_S', 0.2), ('GENERATE_BUDGET_S', 0.35),
                        ('MIN_ATTEMPT_S', 0.01), ('RETRY_BACKOFF_S', 0.01)):
        monkeypatch.setattr(generator, name, value)
    posts = []
    async def handler(request):
        if request.method == 'GET' and hang == 'chat':
            return httpx.Response(200, json={'data': []})
        if request.method == 'POST':
            posts.append(request.url.host)
        await asyncio.Event().wait()  # never answers
    transport(handler)
    started = time.perf_counter()
    result = run(json_mode=True)
    elapsed = time.perf_counter() - started
    assert result['provider'] == 'sample' and result['parsed'] == {'ports': []}
    assert result['fallback_reason'] == 'nightshift: timeout; nvidia: timeout'
    # A timed-out request has spent its provider budget, so it is never retried.
    assert posts == (['127.0.0.1', 'integrate.api.nvidia.com'] if hang == 'chat'
                     else ['integrate.api.nvidia.com'])
    assert elapsed < generator.GENERATE_BUDGET_S + 0.2


def _port_list(prompt, system):
    """Deterministic caller logic: parse names from the caller's own input."""
    assert system == 'Extract ports.'
    return {'ports': [{'name': name.strip()} for name in prompt.split(':', 1)[1].split(',')]}


def test_sample_fn_runs_caller_logic_in_sample_mode(monkeypatch, isolated_environment):
    monkeypatch.setenv('SAMPLE_MODE', 'true')
    result = asyncio.run(generator.generate('ports: Papeete, Nuku Hiva', 'Extract ports.', json_mode=True,
                                            run_id='sample-fn', sample_fn=_port_list))
    assert result['parsed'] == {'ports': [{'name': 'Papeete'}, {'name': 'Nuku Hiva'}]}
    assert json.loads(result['text']) == result['parsed']
    assert result['provider'] == 'sample' and result['sample'] and result['status'] == 'sample'
    assert result['model'] == 'caller-sample-fn'
    entry = json.loads(isolated_environment.read_text())
    assert entry['provider'] == 'sample' and entry['served_model'] == 'caller-sample-fn'


def test_sample_fn_used_when_providers_fail(transport, monkeypatch):
    monkeypatch.delenv('NVIDIA_API_KEY')
    transport(lambda request: httpx.Response(503))
    result = asyncio.run(generator.generate('ports: Fort Bay', 'Extract ports.', run_id='down',
                                            sample_fn=lambda p, s: f"caller saw {_port_list(p, s)['ports'][0]['name']}"))
    assert result['text'] == 'caller saw Fort Bay'
    assert result['provider'] == 'sample'
    assert result['fallback_reason'] == 'nightshift: http_503; nvidia: not_configured'


def test_sample_fn_not_called_when_nightshift_answers(transport):
    transport(lambda r: httpx.Response(200, json={'data': []}) if r.method == 'GET' else answer())
    def forbidden(prompt, system):
        raise AssertionError('sample_fn must not run when a provider answers')
    assert run(json_mode=True, sample_fn=forbidden)['provider'] == 'nightshift'


@pytest.mark.parametrize('json_mode, value', [(True, 'not a dict'), (False, {'not': 'a str'})])
def test_sample_fn_wrong_type_is_rejected(json_mode, value, monkeypatch):
    monkeypatch.setenv('SAMPLE_MODE', 'true')
    with pytest.raises(TypeError, match='sample_fn'):
        run(json_mode=json_mode, sample_fn=lambda prompt, system: value)


def served(provider='nightshift', model='nvidia/nemotron-3-super-120b-a12b', **extra):
    return {**dict(text='answer', parsed={'ports': [{'name': 'Papeete'}]}, provider=provider, model=model), **extra}


@pytest.mark.parametrize('role, expected_model', [('json', 'nvidia/nemotron-3-super-120b-a12b'),
                                                   ('judge', 'nvidia/nemotron-3-ultra-550b-a55b'),
                                                   ('filter', 'nvidia/nemotron-3.5-lightning-30b-a3b')])
def test_llm_ask_json_goes_through_nightshift_first(monkeypatch, role, expected_model):
    monkeypatch.delenv('METERLESS_MODEL', raising=False)
    generated = AsyncMock(return_value=served(model=expected_model))
    monkeypatch.setattr(generator, 'generate', generated)
    data, engine = asyncio.run(llm.ask_json_tracked('prompt', 'system', role=role))
    assert data == {'ports': [{'name': 'Papeete'}]}
    assert engine == 'nightshift-' + expected_model.split('/')[-1]
    generated.assert_awaited_once_with('prompt', 'system', json_mode=True, role=role,
                                       run_id=llm.LLM_TRACE_RUN_ID, model=expected_model)


def test_llm_meterless_model_env_overrides_the_role_model(monkeypatch):
    monkeypatch.setenv('METERLESS_MODEL', 'nvidia/nemotron-3.5-lightning-30b-a3b')
    generated = AsyncMock(return_value=served())
    monkeypatch.setattr(generator, 'generate', generated)
    asyncio.run(llm.ask_json('prompt', 'system', role='judge'))
    assert generated.await_args.kwargs['model'] == 'nvidia/nemotron-3.5-lightning-30b-a3b'


def test_llm_sample_generator_result_falls_back_to_nim_then_cascade(monkeypatch):
    from app.core import judge, nvidia
    monkeypatch.setattr(generator, 'generate', AsyncMock(return_value=served(
        provider='sample', model='deterministic-heuristic', fallback_reason='nightshift: offline')))
    # NIM role chain: first model fails, second answers with (data, served_model).
    nim = AsyncMock(side_effect=[RuntimeError('http_503'), ({'ports': []}, 'openai/gpt-oss-20b')])
    monkeypatch.setattr(nvidia, 'get_nvidia_key', lambda settings=None: 'test-key')
    monkeypatch.setattr(nvidia, 'complete_json_nvidia_tracked', nim)
    cascade = AsyncMock(side_effect=AssertionError('NIM answered; cascade not needed'))
    monkeypatch.setattr(judge, 'complete_json_cascade', cascade)
    data, engine = asyncio.run(llm.ask_json_tracked('prompt', 'system', role='json'))
    assert data == {'ports': []} and engine == 'nvidia-nim-gpt-oss-20b'  # a dict, never the tuple


def test_llm_without_nightshift_or_nim_uses_the_old_cascade(monkeypatch):
    from app.core import judge, nvidia
    monkeypatch.setattr(generator, 'generate', AsyncMock(return_value=served(provider='sample')))
    monkeypatch.setattr(nvidia, 'get_nvidia_key', lambda settings=None: '')
    cascade = AsyncMock(return_value=({'ports': []}, 'openrouter'))
    monkeypatch.setattr(judge, 'complete_json_cascade', cascade)
    settings = {'custom': 'setting'}
    assert asyncio.run(llm.ask_json('prompt', 'system', settings, max_tokens=123, role='extract')) == {'ports': []}
    cascade.assert_awaited_once_with('system', 'prompt', settings, role='extract', max_tokens=123, log=None)


def test_llm_text_nightshift_first_then_nvidia(monkeypatch):
    from app.core import nvidia
    monkeypatch.setattr(generator, 'generate', AsyncMock(return_value=served(provider='nightshift')))
    assert asyncio.run(llm.ask_text('prompt', 'system')) == 'answer'
    monkeypatch.setattr(generator, 'generate', AsyncMock(return_value=served(provider='sample')))
    monkeypatch.setattr(nvidia, 'nvidia_enabled', lambda settings: True)
    complete = AsyncMock(return_value='original nvidia response')
    monkeypatch.setattr(nvidia, 'complete_text_nvidia', complete)
    settings = {'custom': 'setting'}
    assert asyncio.run(llm.ask_text('prompt', 'system', settings, max_tokens=55)) == 'original nvidia response'
    complete.assert_awaited_once_with('system', 'prompt', settings, max_tokens=55)


def test_extract_ports_uses_nightshift_and_labels_the_engine(monkeypatch):
    monkeypatch.setattr(generator, 'generate', AsyncMock(return_value=served(
        parsed={'ports': [{'name': 'Papeete', 'city': 'Papeete'}]}, model='nvidia/nemotron-3-super-120b-a12b')))
    ports = asyncio.run(llm.extract_ports('Papeete is the port of entry.', {'name': 'French Polynesia'}, settings={}))
    assert [p['name'] for p in ports] == ['Papeete']
    assert ports[0]['extraction_engine'] == 'nightshift'
    assert ports[0]['extraction_model'] == 'nvidia/nemotron-3-super-120b-a12b'


def test_model_argument_overrides_meterless_model(transport, isolated_environment, monkeypatch):
    monkeypatch.setenv('METERLESS_MODEL', 'meterless/fast')
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': []})
        assert json.loads(request.content)['model'] == 'nvidia/nemotron-3-ultra-550b-a55b'
        return answer(model='nvidia/nemotron-3-ultra-550b-a55b')
    transport(handler)
    result = run(model='nvidia/nemotron-3-ultra-550b-a55b')
    assert result['provider'] == 'nightshift'
    entry = json.loads(isolated_environment.read_text())
    assert entry['requested_model'] == entry['served_model'] == 'nvidia/nemotron-3-ultra-550b-a55b'
