"""An answer with no verdict is a failed call, asked once more (stubbed HTTP, offline).

Run 20261105-183713-b60354: the Canaries' posts and Saint Lucia came back with finish_reason
"stop" and no verdict, and the same prompt then gave 8 verdicts on a second asking. In the
replay the answer was exactly {"ports": []}, valid JSON for candidates that were all named.
"""
import asyncio
import json

import httpx
import pytest

from app.adapters import evidence_store
from app.agents import poe_judge
from app.core import generator
from app.services import poe_pipeline as poe

URL = "https://gov.example/decree"
ZONE = {"name": "Testland", "mrgid": 990003}
WHITELIST = ["gov.example"]
NAMES = [f"Port {c}" for c in "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel".split()]
EMPTY = '{"ports": []}'


class _Run:
    run_id = "run-judge"


def _doc(name):
    return {"name": name, "mrgid": 990003, "zone_name": "Testland", "source_urls": [URL],
            "extraction_engine": "nightshift", "lat": 1.0, "lon": 1.0, "validated": True,
            "spatial_kind": "in_eez", "confidence": 60}


def _candidates(prompt):
    return [line[2:] for line in prompt.splitlines() if line.startswith("- ")]


def _verdicts(prompt):
    return json.dumps({"ports": [
        {"name": n, "verdict": "poe", "reason": "Listed.",
         "quoted_excerpt": f"Le port de {n} est un port d'entrée désigné"}
        for n in _candidates(prompt)]})


@pytest.fixture
def nightshift(monkeypatch, tmp_path):
    """Real generator, mocked HTTP: ``script`` is consumed one completion at a time (a reply
    text, or None for verdicts on every candidate); once empty, every call gets verdicts."""
    for name in ("SAMPLE_MODE", "METERLESS_MODEL", "MODEL_NAME", "MODEL_BASE_URL", "LLM_PROVIDER",
                 "NEBIUS_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("METERLESS_BASE_URL", generator.DEFAULT_BASE_URL)
    monkeypatch.setenv("METERLESS_RUNTIME_TOKEN", "runtime-key")
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("TRACE_STDOUT", "0")
    for store in (generator._probe_cache, generator._outages):
        store.clear()
    generator._probe_locks.clear()
    script: list = []
    asked: list[list[str]] = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "meterless/auto"}]})
        prompt = json.loads(request.content)["messages"][-1]["content"]
        asked.append(_candidates(prompt))
        reply = script.pop(0) if script else None
        return httpx.Response(200, json={"model": "m", "choices": [
            {"message": {"content": reply or _verdicts(prompt)}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(generator.httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(evidence_store, "documents_from_context", lambda raw, sources: [
        {"url": f"{URL}/{i}", "text": f"Le port de {n} est un port d'entrée désigné pour les navires."}
        for i, n in enumerate(NAMES)])
    yield script, asked
    for store in (generator._probe_cache, generator._outages):
        store.clear()
    generator._probe_locks.clear()


def _run(docs):
    async def go():
        with poe.zone_extract_budget():
            return await poe._judge_zone_run(ZONE, docs, "raw", [], WHITELIST, _Run(), lambda m: None)
    return asyncio.run(go())


def test_ask_reads_the_exact_empty_answer_as_a_failed_call(nightshift):
    script, _ = nightshift
    script.append(EMPTY)
    texts = [{"url": URL, "text": "Le port de Port Alpha est un port d'entrée désigné."}]
    llm, got, why = asyncio.run(poe_judge.ask("Testland", [_doc("Port Alpha")], texts, "run", 30.0))
    assert got == {} and why == poe_judge.NO_VERDICT
    assert llm["finish_reason"] == "stop" and llm["raw_answer"] == EMPTY


def test_the_exact_empty_answer_is_asked_again_in_halves(nightshift):
    script, asked = nightshift
    script.append(EMPTY)
    docs = [_doc(n) for n in NAMES]
    summary = _run(docs)
    assert [len(a) for a in asked] == [8, 4, 4]
    assert summary["judged"] == 8 and summary["status"] == "ok"
    assert summary["ports_without_verdict"] == []
    assert [c["retry"] for c in summary["calls"]] == [False, True, True]
    assert summary["calls"][0]["raw_answer"] == EMPTY
    assert summary["calls"][0]["finish_reason"] == "stop"
    assert summary["calls"][0]["reason"] == poe_judge.NO_VERDICT


def test_each_half_has_its_own_texts(nightshift):
    script, _ = nightshift
    script.append(EMPTY)
    summary = _run([_doc(n) for n in NAMES])
    assert [c["texts"] for c in summary["calls"]] == [8, 4, 4]


def test_one_port_is_asked_as_it_was(nightshift):
    script, asked = nightshift
    script.append(EMPTY)
    docs = [_doc("Port Alpha")]
    summary = _run(docs)
    assert asked == [["Port Alpha"], ["Port Alpha"]]
    assert docs[0]["judge"]["verdict"] == "poe" and summary["failed_calls"] == 1


def test_it_is_asked_once_more_only(nightshift):
    script, asked = nightshift
    script.extend([EMPTY] * 3)
    docs = [_doc(n) for n in NAMES]
    summary = _run(docs)
    assert len(asked) == 3
    assert summary["status"] == "skipped" and summary["failed"] is True
    assert summary["reason"] == poe_judge.NO_VERDICT
    assert summary["ports_without_verdict"] == NAMES


def test_a_timed_out_call_is_asked_again(monkeypatch):
    calls = []

    async def ask(name, ports, texts, run_id, timeout_s, gate=None):
        calls.append(len(ports))
        if len(calls) == 1:
            return {}, {}, "no answer within 90s"
        return {"provider": "nightshift"}, {
            poe.registry_check.port_key(d["name"]): {
                "verdict": "unclear", "reason": "", "quoted_excerpt": "", "excerpt_verified": False}
            for d in ports}, None

    monkeypatch.setattr(poe_judge, "ask", ask)
    monkeypatch.setattr(evidence_store, "documents_from_context", lambda raw, sources: [
        {"url": URL, "text": " ".join(f"Le port de {n} est un port d'entrée désigné." for n in NAMES)}])
    summary = _run([_doc(n) for n in NAMES])
    assert calls == [8, 4, 4] and summary["judged"] == 8


def test_a_refused_call_is_not_asked_again(monkeypatch):
    calls = []

    async def ask(name, ports, texts, run_id, timeout_s, gate=None):
        calls.append(len(ports))
        return {}, {}, poe_judge.GATE_REFUSED

    monkeypatch.setattr(poe_judge, "ask", ask)
    monkeypatch.setattr(evidence_store, "documents_from_context", lambda raw, sources: [
        {"url": URL, "text": "Le port de Port Alpha est un port d'entrée désigné."}])
    _run([_doc("Port Alpha")])
    assert calls == [1]
