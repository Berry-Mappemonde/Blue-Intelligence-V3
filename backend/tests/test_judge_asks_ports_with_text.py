"""The judge is asked only about ports the zone holds official text for (stubbed LLM, offline).

Run 20261105-002857-f7d809: asked about every candidate, it answered "not_poe" on 20 Canary ports
with no text at all, and "not mentioned" reads as a contradiction.
"""
import asyncio
import json

import pytest

from app.adapters import evidence_store
from app.agents import poe_judge
from app.core import generator
from app.services import poe_pipeline as poe
from app.services import v3_certify as vc

URL = "https://gov.example/decree"
TEXT = "Le port de Port Alpha est un port d'entrée désigné pour les navires de plaisance étrangers."
ZONE = {"name": "Testland", "mrgid": 990003}
WHITELIST = ["gov.example"]


class _Run:
    run_id = "run-judge"


def _doc(name):
    return {"name": name, "mrgid": 990003, "zone_name": "Testland", "source_urls": [URL],
            "extraction_engine": "nightshift", "lat": 1.0, "lon": 1.0, "validated": True,
            "spatial_kind": "in_eez", "confidence": 60}


@pytest.fixture
def judge(monkeypatch):
    prompts: list[str] = []

    async def generate(prompt, system=None, **kw):
        prompts.append(prompt)
        ports = [{"name": line[2:], "verdict": "not_poe", "reason": "Not mentioned",
                  "quoted_excerpt": ""} for line in prompt.splitlines() if line.startswith("- ")]
        return {"provider": "nightshift", "model": "m", "parsed": {"ports": ports},
                "text": json.dumps({"ports": ports})}

    monkeypatch.setattr(generator, "generate", generate)
    monkeypatch.setattr(evidence_store, "documents_from_context",
                        lambda raw, sources: [{"url": URL, "text": TEXT}])
    monkeypatch.delenv("SAMPLE_MODE", raising=False)
    return prompts


def _run(docs):
    async def go():
        with poe.zone_extract_budget():
            return await poe._judge_zone_run(ZONE, docs, "raw", [], WHITELIST, _Run(), lambda m: None)
    return asyncio.run(go())


def test_a_port_with_no_text_gets_no_verdict(judge):
    docs = [_doc("Port Alpha"), _doc("Port Bravo")]
    summary = _run(docs)
    assert "- Port Alpha" in judge[0] and "Port Bravo" not in judge[0]
    assert docs[0]["judge"]["verdict"] == "not_poe"
    assert "judge" not in docs[1]
    assert summary["ports_without_text"] == ["Port Bravo"]


def test_no_verdict_is_not_a_contradiction(judge):
    docs = [_doc("Port Alpha"), _doc("Port Bravo")]
    _run(docs)
    assert vc.judge_outcome(docs[1]) is None


def test_a_port_with_no_text_is_not_a_missing_verdict(judge):
    summary = _run([_doc("Port Alpha"), _doc("Port Bravo")])
    assert summary["ports_without_verdict"] == []
    assert summary["status"] == "ok" and summary["failed"] is False


def test_a_zone_with_no_text_for_any_port_makes_no_call(judge, monkeypatch):
    monkeypatch.setattr(evidence_store, "documents_from_context", lambda raw, sources: [])
    summary = _run([_doc("Port Bravo")])
    assert judge == [] and summary["reason"] == "no official evidence text for this zone"
