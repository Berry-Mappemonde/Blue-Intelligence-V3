"""Nebius Token Factory (EU) for Nemotron roles. No network."""
import asyncio
import inspect
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core import generator, llm, nvidia, tokenfactory
from app.services.run_fingerprint import build_code_fingerprint

EU = "https://api.tokenfactory.nebius.com/v1/chat/completions"
KEY = "nebius-test-secret"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    tokenfactory.reset_ledger()
    tokenfactory.bind_run(None)
    generator._probe_cache.clear()
    for name in (
        "NEBIUS_API_KEY", "LLM_PROVIDER", "NVIDIA_API_KEY", "NVIDIA_MODEL",
        "TF_RUN_BUDGET_USD", "TF_PRICES_JSON", "BI_TF_BASE",
        "TF_MODEL_FILTER", "TF_MODEL_JUDGE", "TF_MODEL_PAGE", "TF_MODEL_JSON",
        "TF_MODEL_TRIAGE", "TF_MODEL_EXTRACT", "TF_MODEL_NORMALIZE", "TF_MODEL_SEARCH_RANK",
        "NEMOTRON_MODEL", "SAMPLE_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRACE_STDOUT", "0")
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("METERLESS_BASE_URL", generator.DEFAULT_BASE_URL)
    yield
    tokenfactory.reset_ledger()
    tokenfactory.bind_run(None)
    generator._probe_cache.clear()


def _run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _install_client(monkeypatch, responder):
    posts = []

    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            posts.append({"url": url, "headers": headers or {}, "json": json or {}})
            status, payload, text = responder(posts[-1])
            return _Resp(status, payload, text)

    monkeypatch.setattr(tokenfactory.httpx, "AsyncClient", _Client)
    return posts


def _ok(model, content='{"ok": true}', usage=None):
    return {
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
    }


def test_prices_and_default_budget():
    assert tokenfactory.PRICES[tokenfactory.TF_LIGHTNING] == (0.1, 0.4)
    assert tokenfactory.PRICES[tokenfactory.TF_SUPER_VL] == (0.3, 1.2)
    assert tokenfactory.PRICES[tokenfactory.TF_ULTRA] == (0.6, 2.4)
    assert tokenfactory.PRICES[tokenfactory.TF_NANO] == (0.06, 0.24)
    assert tokenfactory.budget_usd() == 5.0
    assert tokenfactory.EU_HOST == "api.tokenfactory.nebius.com"
    assert ".us." not in tokenfactory.CHAT_URL
    assert tokenfactory.chat_url() == EU


def test_absent_env_keeps_nim_ids_and_host(monkeypatch):
    posts = _install_client(monkeypatch, lambda call: (200, _ok(call["json"]["model"], '{"ports": []}'), ""))
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    assert nvidia.llm_provider({}) == "nvidia"
    assert nvidia.models_for("judge")[0] == nvidia.NEMOTRON_ULTRA_MODEL
    assert nvidia.models_for("filter") == (nvidia.NEMOTRON_LIGHTNING_MODEL,)
    data, used = _run(nvidia.complete_json_nvidia_tracked("sys", "user", role="filter"))
    assert data == {"ports": []}
    assert used == nvidia.NEMOTRON_LIGHTNING_MODEL
    assert posts[0]["url"] == nvidia.NVIDIA_URL
    assert tokenfactory.summary()["calls"] == 0


def test_key_or_provider_maps_nemotron_roles_only(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    monkeypatch.setenv("NVIDIA_MODEL", "openai/gpt-oss-20b")
    assert nvidia.llm_provider({}) == "nvidia"
    assert nvidia.models_for("judge") == (tokenfactory.TF_ULTRA,)
    assert nvidia.models_for("page") == (tokenfactory.TF_SUPER_VL,)
    assert nvidia.models_for("extract") == (tokenfactory.TF_SUPER,)
    assert nvidia.models_for("json") == (tokenfactory.TF_SUPER,)
    assert nvidia.models_for("filter") == (tokenfactory.TF_LIGHTNING,)
    assert nvidia.models_for("normalize") == (tokenfactory.TF_LIGHTNING,)
    assert nvidia.models_for("search_rank") == (tokenfactory.TF_LIGHTNING,)
    assert nvidia.models_for("triage") == (tokenfactory.TF_NANO,)
    assert nvidia.models_for("text")[0] == nvidia.GPT_OSS_MODEL
    assert nvidia.models_for("legal")[0] == nvidia.LEGAL_MODEL
    assert nvidia.models_for("review")[0] == nvidia.GPT_OSS_MODEL
    mapped = tokenfactory.to_tf(nvidia.NEMOTRON_LIGHTNING_MODEL)
    assert mapped == tokenfactory.TF_LIGHTNING
    assert tokenfactory.to_tf(nvidia.NEMOTRON_ULTRA_MODEL) == tokenfactory.TF_ULTRA
    assert tokenfactory.to_tf(nvidia.NEMOTRON_SUPER_VL_MODEL) == tokenfactory.TF_SUPER_VL

    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "tokenfactory")
    assert nvidia.llm_provider({}) == "tokenfactory"
    assert nvidia.models_for("page") == (tokenfactory.TF_SUPER_VL,)
    assert nvidia.models_for("page", settings={"llm_provider": "openrouter"})[0] == (
        nvidia.NEMOTRON_SUPER_VL_MODEL)


def test_completion_stays_on_eu_host_and_tracks_cost(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    monkeypatch.setenv("TF_RUN_BUDGET_USD", "5")

    def responder(call):
        assert call["url"] == EU
        assert call["headers"]["Authorization"] == f"Bearer {KEY}"
        assert "integrate.api.nvidia.com" not in call["url"]
        model = call["json"]["model"]
        return 200, _ok(
            model,
            '<think>hidden</think>{"ok": true}',
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        ), ""

    posts = _install_client(monkeypatch, responder)
    token = tokenfactory.bind_run("run-eu")
    try:
        data, used = _run(nvidia.complete_json_nvidia_tracked(
            "sys", "user", role="judge"))
    finally:
        tokenfactory.reset_run(token)
    assert data == {"ok": True}
    assert used == tokenfactory.TF_ULTRA
    assert len(posts) == 1
    assert posts[0]["json"]["model"] == tokenfactory.TF_ULTRA
    cost = tokenfactory.summary("run-eu")
    assert cost["calls"] == 1
    assert cost["tokens_in"] == 1_000_000
    assert cost["tokens_out"] == 1_000_000
    assert cost["usd_estimated"] == pytest.approx(3.0)
    assert cost["budget_usd"] == 5
    assert cost["budget_reached"] is False
    assert cost["host"] == "api.tokenfactory.nebius.com"
    assert cost["entries"][0]["model"] == tokenfactory.TF_ULTRA
    assert KEY not in json.dumps(cost)


@pytest.mark.parametrize("role, model, tokens_in, tokens_out, usd", [
    ("filter", tokenfactory.TF_LIGHTNING, 1_000_000, 1_000_000, 0.5),
    ("page", tokenfactory.TF_SUPER_VL, 1_000_000, 0, 0.3),
    ("extract", tokenfactory.TF_SUPER, 0, 1_000_000, 1.2),
    ("triage", tokenfactory.TF_NANO, 1_000_000, 1_000_000, 0.3),
])
def test_role_prices(monkeypatch, role, model, tokens_in, tokens_out, usd):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)

    def responder(call):
        assert call["url"] == EU
        assert call["json"]["model"] == model
        return 200, _ok(model, '{"ok": true}', {
            "prompt_tokens": tokens_in, "completion_tokens": tokens_out,
        }), ""

    _install_client(monkeypatch, responder)
    _run(nvidia.complete_json_nvidia_tracked("sys", "user", role=role))
    snap = tokenfactory.summary()
    assert snap["usd_estimated"] == pytest.approx(usd)
    assert snap["by_model"][model]["calls"] == 1


def test_prices_json_overrides_the_table(monkeypatch):
    monkeypatch.setenv("TF_PRICES_JSON", json.dumps({
        tokenfactory.TF_LIGHTNING: [2.0, 4.0],
    }))
    row = tokenfactory.record(
        tokenfactory.TF_LIGHTNING,
        {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        run_id="priced",
    )
    assert row["usd"] == pytest.approx(6.0)
    monkeypatch.setenv("TF_PRICES_JSON", "{not json")
    assert tokenfactory.prices_for(tokenfactory.TF_NANO) == (0.06, 0.24)


def test_budget_stops_the_next_call(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    monkeypatch.setenv("TF_RUN_BUDGET_USD", "0.49")

    def responder(call):
        return 200, _ok(call["json"]["model"], '{"ok": true}', {
            "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000,
        }), ""

    posts = _install_client(monkeypatch, responder)
    token = tokenfactory.bind_run("budget-run")
    try:
        _run(nvidia.complete_json_nvidia_tracked("sys", "user", role="filter"))
        with pytest.raises(tokenfactory.TokenFactoryBudgetExceeded):
            _run(nvidia.complete_json_nvidia_tracked("sys", "user", role="filter"))
    finally:
        tokenfactory.reset_run(token)
    assert len(posts) == 1
    assert tokenfactory.summary("budget-run")["budget_reached"] is True
    assert tokenfactory.summary("budget-run")["usd_estimated"] == pytest.approx(0.5)
    assert KEY not in str(tokenfactory.summary("budget-run"))


def test_zero_budget_sends_nothing(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    monkeypatch.setenv("TF_RUN_BUDGET_USD", "0")
    posts = _install_client(monkeypatch, lambda call: (_ for _ in ()).throw(AssertionError("posted")))
    with pytest.raises(tokenfactory.TokenFactoryBudgetExceeded):
        _run(nvidia.complete_json_nvidia_tracked("sys", "user", role="json"))
    assert posts == []


def test_us_host_is_refused_before_any_post(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    monkeypatch.setenv("BI_TF_BASE", "https://api.tokenfactory.us.nebius.com/v1")
    posts = _install_client(monkeypatch, lambda call: (_ for _ in ()).throw(AssertionError(call["url"])))
    with pytest.raises(RuntimeError, match="api.tokenfactory.nebius.com"):
        _run(nvidia.complete_json_nvidia_tracked("sys", "user", role="judge"))
    assert posts == []
    monkeypatch.setenv("BI_TF_BASE", "https://integrate.api.nvidia.com/v1")
    with pytest.raises(RuntimeError, match="api.tokenfactory.nebius.com"):
        tokenfactory.chat_url()


def test_missing_key_does_not_call_nvidia(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "tokenfactory")
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test")
    posts = _install_client(monkeypatch, lambda call: (_ for _ in ()).throw(AssertionError(call["url"])))
    with pytest.raises(RuntimeError, match="NEBIUS_API_KEY missing"):
        _run(nvidia.complete_json_nvidia_tracked("sys", "user", role="extract"))
    assert posts == []
    assert KEY not in "NEBIUS_API_KEY missing"


def test_unknown_nemotron_id_is_not_sent_to_the_us_host(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    posts = _install_client(monkeypatch, lambda call: (_ for _ in ()).throw(AssertionError(call["url"])))
    with pytest.raises(RuntimeError, match="catalogue"):
        _run(nvidia.complete_json_nvidia_tracked(
            "sys", "user", role="judge", model="nvidia/nemotron-not-in-catalogue"))
    assert posts == []


def test_http_error_redacts_the_key(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)

    def responder(call):
        return 500, {}, f"echo {KEY} failed"

    _install_client(monkeypatch, responder)
    with pytest.raises(RuntimeError) as caught:
        _run(nvidia.complete_json_nvidia_tracked("sys", "user", role="json"))
    assert KEY not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_json_object_400_retries_without_format(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    seen = {"n": 0}

    def responder(call):
        seen["n"] += 1
        if seen["n"] == 1:
            assert call["json"]["response_format"] == {"type": "json_object"}
            return 400, {}, "format rejected"
        assert "response_format" not in call["json"]
        return 200, _ok(call["json"]["model"], '{"ok": true}', {
            "prompt_tokens": 10, "completion_tokens": 5,
        }), ""

    posts = _install_client(monkeypatch, responder)
    data, used = _run(nvidia.complete_json_nvidia_tracked("sys", "user", role="json"))
    assert data == {"ok": True}
    assert used == tokenfactory.TF_SUPER
    assert len(posts) == 2
    assert tokenfactory.summary()["calls"] == 1


def test_images_are_forwarded_for_super_vl(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)

    def responder(call):
        content = call["json"]["messages"][1]["content"]
        assert isinstance(content, list)
        assert any(part.get("type") == "image_url" for part in content)
        assert call["json"]["model"] == tokenfactory.TF_SUPER_VL
        return 200, _ok(tokenfactory.TF_SUPER_VL, '{"ports": []}'), ""

    _install_client(monkeypatch, responder)
    data, used = _run(nvidia.complete_json_nvidia_tracked(
        "sys", "page", role="page", images=[b"\xff\xd8\xff"]))
    assert used == tokenfactory.TF_SUPER_VL
    assert data == {"ports": []}


def test_model_override_must_be_in_the_catalogue(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    monkeypatch.setenv("TF_MODEL_FILTER", tokenfactory.TF_NANO)
    assert nvidia.models_for("filter") == (tokenfactory.TF_NANO,)
    monkeypatch.setenv("TF_MODEL_JUDGE", "openai/gpt-oss-20b")
    with pytest.raises(RuntimeError, match="catalogue"):
        nvidia.models_for("judge")


def test_run_accumulator_is_readable_by_runs():
    tokenfactory.record(tokenfactory.TF_NANO, {
        "prompt_tokens": 1_000_000, "completion_tokens": 0,
    }, run_id="run-1")
    summary = {"run_id": "run-1"}
    tokenfactory.attach(summary, "run-1")
    assert summary["tokenfactory"]["usd_estimated"] == pytest.approx(0.06)
    assert summary["tokenfactory"]["tokens_in"] == 1_000_000
    assert tokenfactory.public_cost("other", summary["tokenfactory"])["calls"] == 1
    tokenfactory.record(tokenfactory.TF_NANO, {"prompt_tokens": 10}, run_id="other")
    live = tokenfactory.public_cost("other", summary["tokenfactory"])
    assert live["calls"] == 1
    assert live["tokens_in"] == 10
    for fn in (
        __import__("app.services.poe_runs", fromlist=["execute_run"]).execute_run,
        __import__("app.services.project_runs", fromlist=["finalize_run"]).finalize_run,
        __import__("app.services.isolated_runs", fromlist=["finalize_run"]).finalize_run,
    ):
        assert "attach" in inspect.getsource(fn)
    from app.routers import project_runs, runs
    assert "public_cost" in inspect.getsource(runs.poe_run_status)
    assert "public_cost" in inspect.getsource(project_runs.project_run_status)


def test_isolated_run_id_buckets_the_ledger():
    from app.services import isolated_runs
    token = isolated_runs.bind_run("iso-9")
    try:
        tokenfactory.record(tokenfactory.TF_LIGHTNING, {
            "prompt_tokens": 1_000_000, "completion_tokens": 0,
        })
        assert tokenfactory.summary("iso-9")["usd_estimated"] == pytest.approx(0.1)
        assert tokenfactory.summary("iso-8")["calls"] == 0
    finally:
        isolated_runs.reset_run(token)


def test_fingerprint_has_no_secret(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    monkeypatch.setenv("TF_RUN_BUDGET_USD", "1.25")
    fp = build_code_fingerprint({})
    blob = json.dumps(fp)
    assert KEY not in blob
    assert fp["tokenfactory_configured"] is True
    assert fp["tokenfactory_enabled"] is True
    assert fp["tokenfactory_budget_usd"] == 1.25
    assert fp["tokenfactory_judge_model"] == tokenfactory.TF_ULTRA
    assert fp["tokenfactory_page_model"] == tokenfactory.TF_SUPER_VL
    assert fp["features"]["tokenfactory_adapter"] is True
    assert "integrate.api.nvidia.com" not in blob


def _generator_transport(monkeypatch, handler):
    requests = []
    original = httpx.AsyncClient
    real_send = original.send

    def dispatch(request):
        requests.append(request)
        return handler(request)

    class MockClient(original):
        send = real_send

    def factory(**kwargs):
        return MockClient(transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(generator.httpx, "AsyncClient", factory)
    monkeypatch.setattr(generator.asyncio, "sleep", AsyncMock())
    return requests


def _answer(model, usage=None):
    body = {
        "model": model,
        "choices": [{"message": {"content": "Nemotron ok"}}],
    }
    if usage:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def test_generator_nemotron_fallback_uses_eu_host_not_nvidia(monkeypatch, tmp_path):
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test-secret")

    def handler(request):
        assert request.url.host != "127.0.0.1"
        assert request.url.scheme == "https"
        assert request.url.host == "api.tokenfactory.nebius.com"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        payload = json.loads(request.content)
        assert payload["model"] == tokenfactory.TF_ULTRA
        return _answer(tokenfactory.TF_ULTRA, {"prompt_tokens": 1000, "completion_tokens": 1000})

    requests = _generator_transport(monkeypatch, handler)
    result = _run(generator.generate(
        "Extract ports.", role="judge", run_id="tf-run"))
    assert result["provider"] == "tokenfactory"
    assert result["model"] == tokenfactory.TF_ULTRA
    assert "fallback_reason" not in result
    hosts = {request.url.host for request in requests}
    assert hosts == {"api.tokenfactory.nebius.com"}
    assert len(requests) == 1
    snap = tokenfactory.summary("tf-run")
    assert snap["tokens_in"] == 1000
    assert snap["tokens_out"] == 1000
    assert snap["usd_estimated"] == pytest.approx(0.003)
    trace = (tmp_path / "trace.jsonl").read_text()
    assert KEY not in trace
    assert "nv-test-secret" not in trace


def test_generator_without_nebius_still_uses_nvidia(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test-secret")

    def handler(request):
        if request.url.host == "127.0.0.1":
            raise httpx.ConnectError("offline", request=request)
        assert request.url.host == "integrate.api.nvidia.com"
        payload = json.loads(request.content)
        assert payload["model"] == generator.DEFAULT_NEMOTRON_MODEL
        return _answer(generator.DEFAULT_NEMOTRON_MODEL)

    _generator_transport(monkeypatch, handler)
    result = _run(generator.generate("Extract ports.", run_id="nim-run"))
    assert result["provider"] == "nvidia"
    assert tokenfactory.summary("nim-run")["calls"] == 0


def test_forced_provider_skips_nightshift_and_nvidia(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "tokenfactory")
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test-secret")

    def handler(request):
        assert request.url.host == "api.tokenfactory.nebius.com"
        return _answer(tokenfactory.TF_SUPER, {"prompt_tokens": 0, "completion_tokens": 0})

    requests = _generator_transport(monkeypatch, handler)
    result = _run(generator.generate("Extract ports.", role="json", run_id="forced"))
    assert result["provider"] == "tokenfactory"
    assert len(requests) == 1
    assert json.loads(requests[0].content)["model"] == tokenfactory.TF_SUPER
    assert "fallback_reason" not in result


def test_forced_provider_without_key_does_not_call_nvidia(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "tokenfactory")
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test-secret")

    def handler(request):
        raise AssertionError(request.url.host)

    _generator_transport(monkeypatch, handler)
    result = _run(generator.generate("Extract ports.", run_id="no-key"))
    assert result["provider"] == "sample"
    assert result["fallback_reason"] == "tokenfactory: not_configured"


def test_generator_budget_does_not_fall_through_to_nvidia(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "tokenfactory")
    monkeypatch.setenv("NEBIUS_API_KEY", KEY)
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test-secret")
    monkeypatch.setenv("TF_RUN_BUDGET_USD", "0")

    def handler(request):
        raise AssertionError(request.url.host)

    _generator_transport(monkeypatch, handler)
    result = _run(generator.generate("Extract ports.", run_id="capped"))
    assert result["provider"] == "sample"
    assert result["fallback_reason"] == "tokenfactory: budget"


def test_ask_json_accepts_a_tokenfactory_generator_result(monkeypatch):
    monkeypatch.setattr(generator, "generate", AsyncMock(return_value={
        "provider": "tokenfactory",
        "parsed": {"ok": True},
        "text": "{}",
        "model": tokenfactory.TF_ULTRA,
    }))
    data, engine = _run(llm.ask_json_tracked("prompt", "system", role="judge"))
    assert data == {"ok": True}
    assert engine == "tokenfactory-" + tokenfactory.TF_ULTRA.split("/")[-1]
    assert llm._public_engine(engine, "Extractor") == "TokenFactory Extractor"


def test_take_calls_is_per_run_and_partial():
    assert tokenfactory.take_calls(3, 5, "run-a") == 3
    assert tokenfactory.take_calls(3, 5, "run-a") == 2
    assert tokenfactory.take_calls(1, 5, "run-a") == 0
    assert tokenfactory.take_calls(4, 5, "run-b") == 4
    assert tokenfactory.calls_taken("run-a") == 5
    tokenfactory.reset_ledger("run-a")
    assert tokenfactory.calls_taken("run-a") == 0 and tokenfactory.calls_taken("run-b") == 4
