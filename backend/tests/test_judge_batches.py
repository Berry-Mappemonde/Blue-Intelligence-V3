"""The judge in batches: at most 8 ports per call, each call with its own texts, its calls reserved
from the zone's share, finish_reason stored on every answer (stubbed LLM, offline).

Run 20261105-135558-ed2de4: one call per zone came back cut off, with 3 verdicts of 5, 4 of 13
and none of 47. And #63 stands: a row reaches C2 only through a "poe" on its own row.
"""
import asyncio
import json
import re

import httpx
import pytest

from app.adapters import evidence_store
from app.agents import poe_judge
from app.core import generator, run_rules, tokenfactory
from app.services import poe_pipeline as poe
from app.services import v3_certify as vc

URL = "https://gov.example/decree"
ZONE = {"name": "Testland", "mrgid": 990003}
WHITELIST = ["gov.example"]
NAMES = [f"Port {c}" for c in "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliett".split()]


def _text(names):
    return " ".join(f"Le port de {n} est un port d'entrée désigné pour les navires étrangers."
                    for n in names)


class _Run:
    run_id = "run-judge"


def _doc(name):
    return {"name": name, "mrgid": 990003, "zone_name": "Testland", "source_urls": [URL],
            "extraction_engine": "nightshift", "lat": 1.0, "lon": 1.0, "validated": True,
            "spatial_kind": "in_eez", "confidence": 60}


def _candidates(prompt):
    return [line[2:] for line in prompt.splitlines() if line.startswith("- ")]


@pytest.fixture
def judge(monkeypatch):
    """``answers`` is consumed one call at a time (None: every candidate is "poe" on its own
    sentence; an exception or a dict replaces the reply). Records each prompt."""
    prompts: list[str] = []
    answers: list = []

    async def generate(prompt, system=None, **kw):
        prompts.append(prompt)
        answer = answers.pop(0) if answers else None
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, dict):
            return answer
        ports = [{"name": n, "verdict": "poe", "reason": "Listed.",
                  "quoted_excerpt": f"Le port de {n} est un port d'entrée désigné"}
                 for n in _candidates(prompt)]
        return {"provider": "nightshift", "model": "m", "parsed": {"ports": ports},
                "text": json.dumps({"ports": ports}), "finish_reason": "stop"}

    monkeypatch.setattr(generator, "generate", generate)
    monkeypatch.delenv("SAMPLE_MODE", raising=False)
    monkeypatch.setattr(evidence_store, "documents_from_context",
                        lambda raw, sources: [{"url": f"{URL}/{i}", "text": _text([n])}
                                              for i, n in enumerate(NAMES)])
    return prompts, answers


def _run(docs):
    async def go():
        with poe.zone_extract_budget():
            return await poe._judge_zone_run(ZONE, docs, "raw", [], WHITELIST, _Run(), lambda m: None)
    return asyncio.run(go())


def test_at_most_8_ports_per_call(judge):
    prompts, _ = judge
    docs = [_doc(n) for n in NAMES]
    summary = _run(docs)
    assert [len(_candidates(p)) for p in prompts] == [8, 2]
    assert summary["judged"] == 10 and summary["verdicts"] == {"poe": 10}
    assert [c["ports"] for c in summary["calls"]] == [8, 2]


def test_the_size_is_a_run_rule(judge, monkeypatch):
    prompts, _ = judge
    rule = run_rules.get_rule
    monkeypatch.setattr(run_rules, "get_rule", lambda name, default=None: 3
                        if name == "formalities.judge_ports_per_call" else rule(name, default))
    _run([_doc(n) for n in NAMES])
    assert [len(_candidates(p)) for p in prompts] == [3, 3, 3, 1]


def test_the_rules_are_in_the_catalogue():
    assert run_rules.get_rule("formalities.judge_ports_per_call", None) == 8
    assert run_rules.get_rule("formalities.judge_reserved_calls", None) == 4
    assert run_rules.get_rule("formalities.judge_output_tokens", None) == 4000


def test_each_call_has_its_own_texts_only(judge):
    prompts, _ = judge
    _run([_doc(n) for n in NAMES])
    assert "Port Alpha est" in prompts[0] and "Port India est" not in prompts[0]
    assert "Port India est" in prompts[1] and "Port Alpha est" not in prompts[1]


def test_an_excerpt_is_verified_against_the_call_s_own_texts(judge):
    _, answers = judge
    stray = {"ports": [{"name": "Port Alpha", "verdict": "poe", "reason": "x",
                        "quoted_excerpt": "Le port de Port India est un port d'entrée désigné"}]}
    answers.append({"provider": "nightshift", "model": "m", "parsed": stray, "text": "{}"})
    docs = [_doc(n) for n in NAMES]
    _run(docs)
    assert docs[0]["judge"]["excerpt_verified"] is False
    assert docs[0]["confidence_level"] != "C2"


def test_a_failed_call_leaves_the_other_verdicts(judge):
    _, answers = judge
    answers.append(asyncio.TimeoutError())
    docs = [_doc(n) for n in NAMES]
    summary = _run(docs)
    assert summary["status"] == "ok" and summary["failed"] is False
    assert summary["failed_calls"] == 1 and summary["judged"] == 2
    assert summary["ports_without_verdict"] == NAMES[:8]
    assert docs[9]["judge"]["verdict"] == "poe" and "judge" not in docs[0]


def test_every_call_failing_fails_the_judge(judge):
    _, answers = judge
    answers.extend([asyncio.TimeoutError(), asyncio.TimeoutError()])
    summary = _run([_doc(n) for n in NAMES])
    assert summary["status"] == "skipped" and summary["failed"] is True
    assert summary["failed_calls"] == 2 and len(summary["ports_without_verdict"]) == 10


def test_finish_reason_is_stored_on_every_answer(judge):
    _, answers = judge
    cut = {"ports": [{"name": "Port Alpha", "verdict": "poe", "reason": "x", "quoted_excerpt": ""}]}
    answers.append({"provider": "nightshift", "model": "m", "parsed": cut, "text": "{}",
                    "finish_reason": "length"})
    summary = _run([_doc(n) for n in NAMES])
    assert [c["finish_reason"] for c in summary["calls"]] == ["length", "stop"]


def test_the_generator_reads_finish_reason_and_traces_it(monkeypatch, tmp_path):
    for name in ("SAMPLE_MODE", "METERLESS_MODEL", "MODEL_NAME", "MODEL_BASE_URL", "LLM_PROVIDER",
                 "NEBIUS_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("METERLESS_BASE_URL", generator.DEFAULT_BASE_URL)
    monkeypatch.setenv("METERLESS_RUNTIME_TOKEN", "runtime-key")
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("TRACE_LOG_PATH", str(trace))
    monkeypatch.setenv("TRACE_STDOUT", "0")
    generator._probe_cache.clear()
    generator._probe_locks.clear()
    sent = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "meterless/auto"}]})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "m", "choices": [
            {"message": {"content": '{"ports": []}'}, "finish_reason": "length"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(generator.httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    result = asyncio.run(generator.generate("q", json_mode=True, role="judge", run_id="t"))
    generator._probe_cache.clear()
    assert result["finish_reason"] == "length"
    assert json.loads(trace.read_text())["finish_reason"] == "length"
    assert sent[0]["max_tokens"] == 4000


# --- the calls come from the zone's share ------------------------------------------------

def _in_zone(body, share=20):
    async def go():
        with poe.zone_extract_budget() as state:
            state.update(allowance=share, run_id=None)
            return await body(state)
    return asyncio.run(go())


def test_the_judge_s_calls_are_reserved_and_the_rest_given_back(judge, monkeypatch):
    monkeypatch.setattr(poe, "has_official_text", lambda *a, **k: True)
    docs = [_doc(n) for n in NAMES]

    async def body(state):
        assert poe._reserve_judge_call("raw", [], lambda m: None, WHITELIST)
        assert state["judge_reserved"] == 4 and state["used"] == 4
        summary = await poe._judge_zone_run(ZONE, docs, "raw", [], WHITELIST, _Run(), lambda m: None)
        assert len(summary["calls"]) == 2
        assert "judge_reserved" not in state and state["used"] == 2

    _in_zone(body)


def test_past_the_reservation_it_takes_what_is_left_of_the_share(judge, monkeypatch):
    prompts, _ = judge
    rule = run_rules.get_rule
    monkeypatch.setattr(run_rules, "get_rule", lambda name, default=None: 1
                        if name == "formalities.judge_reserved_calls" else rule(name, default))
    monkeypatch.setattr(poe, "has_official_text", lambda *a, **k: True)

    async def body(state):
        assert poe._reserve_judge_call("raw", [], lambda m: None, WHITELIST)
        await poe._judge_zone_run(ZONE, [_doc(n) for n in NAMES], "raw", [], WHITELIST, _Run(),
                                  lambda m: None)
        assert state["used"] == 2

    _in_zone(body)
    assert len(prompts) == 2


@pytest.mark.parametrize("share,reserved", [(2, 1), (8, 2), (20, 4), (200, 4)])
def test_the_reservation_leaves_the_readers_most_of_the_share(monkeypatch, share, reserved):
    monkeypatch.setattr(poe, "has_official_text", lambda *a, **k: True)
    monkeypatch.delenv("SAMPLE_MODE", raising=False)

    async def body(state):
        assert poe._reserve_judge_call("raw", [], lambda m: None, WHITELIST)
        assert state["judge_reserved"] == reserved and not state.get("capped")

    _in_zone(body, share=share)


def test_the_judge_waits_out_a_back_off(judge, monkeypatch):
    prompts, _ = judge
    left = [30.0]
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        left[0] = 0.0

    monkeypatch.setattr(poe.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(generator, "nightshift_down_for", lambda: left[0])
    _run([_doc("Port Alpha")])
    assert slept == [30.0] and len(prompts) == 1


# --- #63 stands -----------------------------------------------------------------------------

def _flat_list():
    sources = json.loads((__import__("pathlib").Path(__file__).resolve().parent / "fixtures"
                          / "martinique_51697e_sources.json").read_text(encoding="utf-8"))
    return next(s for s in sources["sources"] if "liste-points-clearance" in s["url"])


REGIE = "Régie Port de Plaisance Marina de la pointe du bout"
BRIGADE = "Trinité Brigade de surveillance des douanes Cité des douanes Quartier Raisinier"
MARTINIQUE = {"name": "Martinique", "geoname": "Martinique", "mrgid": 33178}


def _row_record():
    source = _flat_list()
    return {"name": REGIE, "city": "Les Trois-Îlets", "mrgid": 33178, "zone_name": "Martinique",
            "source_urls": [source["url"], "https://www.martinique.gouv.fr/x"],
            "extraction_engine": "nightshift", "lat": 14.5, "lon": -61.0, "validated": True,
            "spatial_kind": "in_eez", "confidence": 60}, source


@pytest.mark.parametrize("excerpt,level", [
    (BRIGADE, "C1"),
    ("Régie Port de Plaisance Marina de la pointe du bout", "C2"),
])
def test_a_poe_quoting_a_neighbouring_row_gives_no_c2(monkeypatch, excerpt, level):
    record, source = _row_record()
    whitelist = ["douane.gouv.fr", "martinique.gouv.fr"]
    monkeypatch.delenv("SAMPLE_MODE", raising=False)
    monkeypatch.setattr(evidence_store, "documents_from_context",
                        lambda raw, sources: [{"url": source["url"], "text": source["text"]}])

    async def generate(prompt, system=None, **kw):
        ports = [{"name": REGIE, "verdict": "poe", "reason": "Listed.", "quoted_excerpt": excerpt}]
        return {"provider": "nightshift", "model": "m", "parsed": {"ports": ports}, "text": "{}",
                "finish_reason": "stop"}

    monkeypatch.setattr(generator, "generate", generate)

    async def go():
        with poe.zone_extract_budget():
            await poe._judge_zone_run(MARTINIQUE, [record], "raw", [], whitelist, _Run(),
                                      lambda m: None)
    asyncio.run(go())
    assert record["judge"]["verdict"] == "poe" and record["judge"]["excerpt_verified"] is True
    out = vc.assess_port(record, whitelist=whitelist, texts=[source], zone=MARTINIQUE)
    assert out["confidence_level"] == level
