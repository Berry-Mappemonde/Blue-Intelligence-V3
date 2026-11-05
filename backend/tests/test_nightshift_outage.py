"""One failed Nightshift call is not an outage (stubbed HTTP, offline).

Runs 20261105-002857-f7d809 and 20261105-135558-ed2de4: a single 502 or timeout marked Nightshift
down for 60 s, and every batch behind it failed in milliseconds, retry included, while other
calls still succeeded. Martinique lost the batch holding the douane list both times.
"""
import asyncio
import json
import re

import httpx
import pytest

from app.core import generator, llm, run_rules, tokenfactory
from app.services import poe_pipeline as poe

ZONE = {"name": "Testland", "sovereign": "Testland", "mrgid": 990003}
PROSE = "General information about travel formalities, visas and fees for visitors. "
NAME_RE = re.compile(r"\bPort [A-Z][a-z]+\b")
NAMES = ("Port Alpha", "Port Bravo", "Port Charlie", "Port Delta", "Port Echo", "Port Foxtrot")


def _ok(name="Port Alpha"):
    body = json.dumps({"ports": [{"name": name, "city": None, "note": None, "lat": None,
                                  "lon": None, "geocodeable": True}]})
    return httpx.Response(200, json={"model": "m", "choices": [
        {"message": {"content": body}, "finish_reason": "stop"}]})


@pytest.fixture
def backend(monkeypatch, tmp_path):
    """``replies`` is consumed one completion at a time (a status, or None for a success);
    once empty, every completion succeeds. ``posts`` counts the completions sent."""
    for name in ("SAMPLE_MODE", "METERLESS_MODEL", "MODEL_NAME", "MODEL_BASE_URL", "LLM_PROVIDER",
                 "NEBIUS_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("METERLESS_BASE_URL", generator.DEFAULT_BASE_URL)
    monkeypatch.setenv("METERLESS_RUNTIME_TOKEN", "t")
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("TRACE_STDOUT", "0")
    monkeypatch.setattr(generator, "RETRY_BACKOFF_S", 0.01)
    for store in (generator._probe_cache, generator._outages):
        store.clear()
    generator._probe_locks.clear()
    replies: list = []
    posts: list[str] = []
    other: list[str] = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "meterless/auto"}]})
        if str(request.url).startswith(generator.NVIDIA_URL):
            other.append("nvidia")
            return _ok()
        user = json.loads(request.content)["messages"][-1]["content"]
        name = next(iter(NAME_RE.findall(user.split("EXTRAITS DES SOURCES")[-1])), "Port Alpha")
        posts.append(name)
        status = replies.pop(0) if replies else None
        return httpx.Response(status, text="bad gateway") if status else _ok(name)

    original = httpx.AsyncClient
    monkeypatch.setattr(generator.httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    yield replies, posts, other
    for store in (generator._probe_cache, generator._outages):
        store.clear()
    generator._probe_locks.clear()


def _call(run_id="t"):
    return asyncio.run(generator.generate("q?", json_mode=True, run_id=run_id))


def _fail_once(replies):
    """A failed call is the request and its one retry, both 502."""
    replies.extend([502, 502])


def test_one_failed_call_leaves_nightshift_up(backend):
    replies, posts, _ = backend
    _fail_once(replies)
    assert _call()["provider"] == "sample"
    assert asyncio.run(generator.nightshift_ready()) is True
    assert generator.nightshift_down_for() == 0
    assert _call()["provider"] == "nightshift"
    assert len(posts) == 3


def test_it_is_marked_down_after_n_failures_in_a_row(backend):
    replies, posts, _ = backend
    for _ in range(3):
        _fail_once(replies)
        _call()
    assert asyncio.run(generator.nightshift_ready()) is False
    assert 0 < generator.nightshift_down_for() <= generator.PROBE_TTL_S
    sent = len(posts)
    assert "down_http_502" in _call()["fallback_reason"]
    assert len(posts) == sent


def test_a_success_clears_the_count(backend):
    replies, _, _ = backend
    for _ in range(2):
        _fail_once(replies)
        _call()
    assert _call()["provider"] == "nightshift"
    for _ in range(2):
        _fail_once(replies)
        _call()
    assert asyncio.run(generator.nightshift_ready()) is True


def test_n_is_a_run_rule(backend, monkeypatch):
    replies, _, _ = backend
    rule = run_rules.get_rule
    monkeypatch.setattr(run_rules, "get_rule", lambda name, default=None: 1
                        if name == "formalities.nightshift_down_after" else rule(name, default))
    _fail_once(replies)
    _call()
    assert asyncio.run(generator.nightshift_ready()) is False


def test_the_rule_is_in_the_catalogue():
    assert run_rules.get_rule("formalities.nightshift_down_after", None) == 3


def test_the_first_failure_after_a_back_off_backs_off_again(backend):
    replies, _, _ = backend
    for _ in range(3):
        _fail_once(replies)
        _call()
    generator._probe_cache.clear()  # the 60 s are over
    _fail_once(replies)
    _call()
    assert asyncio.run(generator.nightshift_ready()) is False


def test_a_failed_call_goes_to_the_next_provider_in_the_same_call(backend, monkeypatch):
    replies, _, other = backend
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    _fail_once(replies)
    result = _call()
    assert result["provider"] == "nvidia" and other == ["nvidia"]
    assert "nightshift: http_502" in result["fallback_reason"]


def test_extraction_falls_back_in_order(monkeypatch):
    order = []

    async def nightshift(*a, **k):
        order.append("nightshift")

    async def nim(*a, **k):
        order.append("nim")
        raise RuntimeError("quota")

    async def openrouter(*a, **k):
        order.append("openrouter")
        return [{"name": "Port Alpha"}]

    monkeypatch.setattr(llm, "extract_ports_nightshift", nightshift)
    monkeypatch.setattr(llm, "extract_ports_openrouter", openrouter)
    monkeypatch.setattr("app.core.nvidia.nvidia_enabled", lambda s: True)
    monkeypatch.setattr("app.core.nvidia.extract_ports_nvidia", nim)
    ports = asyncio.run(llm.extract_ports("text", ZONE, settings={}))
    assert ports and order == ["nightshift", "nim", "openrouter"]


# --- batches -----------------------------------------------------------------------------

def _sources() -> str:
    return "\n\n".join(f"[SOURCE: https://gov.example/r{i}]\n" + (PROSE * 200)[:15000] + f" {n}."
                       for i, n in enumerate(NAMES))


def _zone(logs: list):
    async def run():
        with poe.zone_extract_budget():
            raw = _sources()
            ports = await poe._extract_ports_core(raw[:20000], ZONE, logs.append, catalog_text=raw,
                                                  settings={})
            return ports, poe.extraction_gaps()
    return asyncio.run(run())


@pytest.fixture
def batches(backend, monkeypatch):
    import app.core.ml as ml
    monkeypatch.setattr(ml, "extract_entities", lambda text: [])
    monkeypatch.setattr(poe, "extract_structured_ports", lambda raw: [])
    monkeypatch.setattr(poe, "extract_ports", llm.extract_ports)
    tokenfactory.reset_ledger()
    yield backend
    tokenfactory.reset_ledger()


def test_one_failed_call_loses_no_batch(batches):
    replies, posts, _ = batches
    _fail_once(replies)
    ports, gaps = _zone([])
    assert {p["name"] for p in ports} == set(NAMES)
    assert not gaps.get("failed_batches")


def test_a_real_back_off_is_waited_out_before_the_retry(monkeypatch):
    """No HTTP and no timing: the reader fails while the back-off lasts, and the only thing
    that ends it is the wait. Every batch fails its first read whatever the concurrency, so
    the result can't depend on how the calls interleave, on the environment or on the clock."""
    down = [30.0]
    slept: list[float] = []
    sleep = asyncio.sleep

    async def reader(text, zone, settings=None, log=None):
        if down[0]:
            raise RuntimeError("extract_ports: no backend")
        return [{"name": n, "city": None, "note": None, "lat": None, "lon": None,
                 "geocodeable": True, "extraction_engine": "nightshift"}
                for n in NAME_RE.findall(text) if n in NAMES]

    async def fake_sleep(seconds, *a):
        if seconds == down[0] and seconds:
            slept.append(seconds)
            down[0] = 0.0
            seconds = 0
        return await sleep(seconds, *a)

    import app.core.ml as ml
    monkeypatch.setattr(ml, "extract_entities", lambda text: [])
    monkeypatch.setattr(poe, "extract_structured_ports", lambda raw: [])
    monkeypatch.setattr(poe, "extract_ports", reader)
    monkeypatch.setattr(generator, "nightshift_down_for", lambda: down[0])
    monkeypatch.setattr(poe.asyncio, "sleep", fake_sleep)
    tokenfactory.reset_ledger()
    logs: list[str] = []
    try:
        ports, gaps = _zone(logs)
    finally:
        tokenfactory.reset_ledger()
    assert slept == [30.0]
    assert {p["name"] for p in ports} == set(NAMES)
    assert not gaps.get("failed_batches")
    assert any("Nightshift en pause" in m for m in logs)


def test_the_wait_stops_at_the_deadline(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(poe.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(generator, "nightshift_down_for", lambda: 50.0)

    async def go():
        await poe._wait_for_nightshift(lambda m: None, asyncio.get_running_loop().time() + 5)
    asyncio.run(go())
    assert slept and slept[0] <= 5
