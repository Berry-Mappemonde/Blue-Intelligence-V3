"""The judge call gets #34's JSON recovery: repair, one strict "JSON only" retry, and a
failed judge listed in the run summary (runs f3993a / 608d85: the Canaries and
Martinique lost their verdicts to one invalid Nightshift answer).

Offline: Nightshift is a mock transport, so the real generator parse path (and so
llm.repair_json) runs on the broken replies. No keys, no network, no Mongo.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agents import poe_judge
from app.core import generator, run_rules, tokenfactory
from app.services import poe_pipeline as poe

URL = "https://www.martinique.gouv.fr/douane"
WL = ["gouv.fr"]
FDF = "Fort-de-France is designated as a port of entry for yachts arriving from abroad."
MARIN = "Le Marin is a port of entry for customs clearance of pleasure craft."
RAW = f"[SOURCE: {URL}]\n{FDF} {MARIN}\n"
SOURCES = [{"url": URL, "official": True}]
ZONE = {"mrgid": 33178, "name": "Martinique", "iso2": "MQ", "sov_iso2": "FR"}
TEXTS = [{"url": URL, "text": f"{FDF} {MARIN}"}]
PORTS = [{"name": "Fort-de-France"}, {"name": "Le Marin"}]

FDF_VERDICT = {"name": "Fort-de-France", "verdict": "poe", "reason": "designated in the text",
               "quoted_excerpt": FDF}
MARIN_VERDICT = {"name": "Le Marin", "verdict": "poe", "reason": "port of entry for customs",
                 "quoted_excerpt": MARIN}
FULL = json.dumps({"ports": [FDF_VERDICT, MARIN_VERDICT]})
# Cut at max_tokens in the middle of Le Marin's verdict.
TRUNCATED = FULL[:FULL.index('"Le Marin"') + 30]
# A fenced reply with a trailing comma and prose around it.
FENCED = ("Here is the audit:\n```json\n{\"ports\": [" + json.dumps(FDF_VERDICT) + ", "
          + json.dumps(MARIN_VERDICT) + ",]}\n```\nLet me know if you need more {details}.")
GARBAGE = "Fort-de-France and Le Marin both look like ports of entry to me."


@pytest.fixture
def nightshift(monkeypatch, tmp_path):
    """Nightshift through a mock transport; ``replies`` are served in order (last repeats)."""
    for name in ("SAMPLE_MODE", "METERLESS_MODEL", "MODEL_NAME", "MODEL_BASE_URL", "LLM_PROVIDER",
                 "NEBIUS_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("METERLESS_BASE_URL", generator.DEFAULT_BASE_URL)
    monkeypatch.setenv("METERLESS_RUNTIME_TOKEN", "t")
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("TRACE_STDOUT", "0")
    generator._probe_cache.clear()
    generator._probe_locks.clear()
    replies: list[str] = []
    prompts: list[str] = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "meterless/auto"}]})
        prompts.append(json.loads(request.content)["messages"][-1]["content"])
        text = replies[min(len(prompts) - 1, len(replies) - 1)]
        return httpx.Response(200, json={"model": "nvidia/nemotron-3-ultra",
                                         "choices": [{"message": {"content": text}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(generator.httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    tokenfactory.reset_ledger()
    yield replies, prompts
    tokenfactory.reset_ledger()
    generator._probe_cache.clear()
    generator._probe_locks.clear()


def _ask(gate=None):
    return asyncio.run(poe_judge.ask("Martinique", PORTS, TEXTS, "run-judge-json", 60, gate=gate))


# --- poe_judge.ask -------------------------------------------------------------------

def test_truncated_reply_keeps_its_complete_verdicts_and_invents_none(nightshift):
    replies, prompts = nightshift
    replies[:] = [TRUNCATED]
    llm, judged, reason = _ask()
    assert reason is None and len(prompts) == 1
    assert llm["json_repaired"] is True and llm["strict_retry"] is False
    (only,) = judged.values()  # one complete verdict kept
    assert only["verdict"] == "poe" and only["excerpt_verified"] is True
    # the cut verdict for Le Marin is dropped, never completed
    assert not any("marin" in k for k in judged)


def test_fenced_reply_is_repaired(nightshift):
    replies, prompts = nightshift
    replies[:] = [FENCED]
    llm, judged, reason = _ask()
    assert reason is None and len(prompts) == 1 and llm["json_repaired"] is True
    assert sorted(j["verdict"] for j in judged.values()) == ["poe", "poe"]


def test_invalid_reply_gets_one_strict_json_only_retry(nightshift):
    replies, prompts = nightshift
    replies[:] = [GARBAGE, FULL]
    llm, judged, reason = _ask()
    assert reason is None and len(judged) == 2
    assert len(prompts) == 2
    assert poe_judge.JUDGE_STRICT_RETRY not in prompts[0]
    assert poe_judge.JUDGE_STRICT_RETRY in prompts[1]
    assert llm["strict_retry"] is True and llm["attempts"] == 2


def test_still_invalid_after_the_retry_is_a_failure_not_a_skip(nightshift):
    replies, prompts = nightshift
    replies[:] = [GARBAGE]
    llm, judged, reason = _ask()
    assert len(prompts) == 2  # one retry, not more
    assert judged == {} and reason == "invalid JSON after repair and one strict retry"
    assert reason not in poe_judge.DELIBERATE_SKIPS


def test_the_retry_takes_a_call_from_the_gate(nightshift):
    replies, prompts = nightshift
    replies[:] = [GARBAGE, FULL]
    grants = iter([True, False])
    _llm, judged, reason = _ask(gate=lambda: next(grants))
    assert len(prompts) == 1 and judged == {}
    assert reason == f"invalid JSON; strict retry not run ({poe_judge.GATE_REFUSED})"


def test_valid_reply_is_unchanged(nightshift):
    replies, prompts = nightshift
    replies[:] = [FULL]
    llm, judged, reason = _ask()
    assert reason is None and len(prompts) == 1 and len(judged) == 2
    assert llm["json_repaired"] is False and llm["strict_retry"] is False


# --- _judge_zone_run: the run records the failure ------------------------------------

def _docs():
    return [{"name": "Fort-de-France", "source_urls": [URL], "extraction_engine": "llm",
             "confidence": 55},
            {"name": "Le Marin", "source_urls": [URL], "extraction_engine": "llm",
             "confidence": 55}]


def _judge_run(docs, run_id="run-judge-json"):
    async def go():
        token = tokenfactory.bind_run(run_id)
        try:
            tokenfactory.plan_zone_calls(1, run_id)
            with poe.zone_extract_budget():
                return await poe._judge_zone_run(ZONE, docs, RAW, SOURCES, WL,
                                                 SimpleNamespace(run_id=run_id), lambda m: None)
        finally:
            tokenfactory.reset_run(token)
    return asyncio.run(go())


def test_failed_judge_lists_the_zone_and_its_ports(nightshift):
    replies, _prompts = nightshift
    replies[:] = [GARBAGE]
    docs = _docs()
    summary = _judge_run(docs)
    assert summary["status"] == "skipped" and summary["failed"] is True
    assert summary["reason"] == "invalid JSON after repair and one strict retry"
    assert summary["ports_without_verdict"] == ["Fort-de-France", "Le Marin"]
    assert summary["strict_retry"] is True
    assert not any(d.get("judge") for d in docs)


def test_repaired_cut_off_answer_lists_the_port_left_without_verdict(nightshift):
    replies, _prompts = nightshift
    replies[:] = [TRUNCATED]
    docs = _docs()
    summary = _judge_run(docs)
    assert summary["status"] == "ok" and summary["failed"] is False
    assert summary["json_repaired"] is True and summary["judged"] == 1
    assert summary["ports_without_verdict"] == ["Le Marin"]
    assert docs[0]["judge"]["verdict"] == "poe" and "judge" not in docs[1]


def test_deliberate_skip_is_not_a_failure(nightshift):
    docs = _docs()
    token = run_rules.bind_rules({"chosen": {"formalities.judge_in_runs": {"value": False}}})
    try:
        summary = _judge_run(docs)
    finally:
        run_rules.reset_rules(token)
    assert summary["status"] == "skipped" and not summary.get("failed")
    assert not summary.get("ports_without_verdict")


def test_run_summary_lists_failed_judges():
    src = open(poe.__file__.replace("poe_pipeline.py", "poe_runs.py"), encoding="utf-8").read()
    assert '"judge_failed": judge_failed' in src
    assert '"judge.failed": True' in src and "judge.ports_without_verdict.0" in src
