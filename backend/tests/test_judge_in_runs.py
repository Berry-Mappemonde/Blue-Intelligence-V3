"""NemotronJudge inside poe_pipeline runs: verdict -> C-level, call cap, off-switch.

Offline: the judge's provider call is stubbed (generator.generate, or the Token Factory
HTTP step for the ledger test). No network, no keys.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents import poe_judge  # noqa: E402
from app.core import run_rules, tokenfactory  # noqa: E402
from app.services import poe_pipeline as poe  # noqa: E402

URL = "https://www.martinique.gouv.fr/douane"
WL = ["gouv.fr"]
FDF = "Fort-de-France is designated as a port of entry for yachts arriving from abroad."
MARIN = "Le Marin is a port of entry for customs clearance of pleasure craft."
RAW = f"[SOURCE: {URL}]\n{FDF} {MARIN}\n"
SOURCES = [{"url": URL, "official": True}]
ZONE = {"mrgid": 1, "name": "Martinique", "iso2": "MQ", "sov_iso2": "FR"}
GEO_OK = {"geocode_agree": True, "geocode_agreement_km": 0.2, "spatial_kind": "in_eez",
          "lat": 14.6, "lon": -61.07}


def _docs():
    # Fort-de-France: one LLM reader -> C1, lifted to C2 by a verified "poe" (geocoders
    # 1.5 km apart, so it stops at C2).
    # Le Marin: LLM∩NER + agreeing geo -> C3, capped to C1 by "not_poe".
    return [
        {"name": "Fort-de-France", "source_urls": [URL], "extraction_engine": "llm",
         "confidence": 55, **GEO_OK, "geocode_agreement_km": 1.5},  # geo short of C3
        {"name": "Le Marin", "source_urls": [URL], "extraction_engine": "llm",
         "extraction_agreement": True,
         "confidence": 80, **GEO_OK},
    ]


ANSWER = {"ports": [
    {"name": "Fort-de-France", "verdict": "poe", "reason": "designated in the text",
     "quoted_excerpt": FDF},
    {"name": "Le Marin", "verdict": "not_poe", "reason": "customs office address only",
     "quoted_excerpt": ""},
]}


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.delenv("SAMPLE_MODE", raising=False)
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "trace.jsonl"))
    tokenfactory.reset_ledger()
    yield
    tokenfactory.reset_ledger()


@pytest.fixture
def judge_calls(monkeypatch):
    """Stub the judge's generator call; record each call."""
    calls = []

    async def fake_generate(prompt, system=None, *, json_mode=False, role="json",
                            run_id=None, sample_fn=None, **kw):
        calls.append({"role": role, "run_id": run_id, "prompt": prompt})
        return {"provider": "tokenfactory", "model": tokenfactory.TF_ULTRA, "parsed": ANSWER}

    monkeypatch.setattr(poe_judge.generator, "generate", fake_generate)
    return calls


def _rules(**values):
    """A run snapshot carrying only the given rule values."""
    return run_rules.bind_rules({"chosen": {k: {"value": v} for k, v in values.items()}})


def _judge(docs, run_id="run-judge", **rules):
    """_judge_zone_run inside a zone budget scope bound to a run, as generate_zone_poe does."""
    async def go():
        rtoken = _rules(**rules)
        ttoken = tokenfactory.bind_run(run_id)
        try:
            tokenfactory.plan_zone_calls(1, run_id)
            with poe.zone_extract_budget() as state:
                summary = await poe._judge_zone_run(
                    ZONE, docs, RAW, SOURCES, WL, SimpleNamespace(run_id=run_id), lambda m: None)
                return summary, dict(state), poe.extraction_gaps()
        finally:
            tokenfactory.reset_run(ttoken)
            run_rules.reset_rules(rtoken)
    return asyncio.run(go())


def test_verdicts_reach_the_level(judge_calls):
    docs = _docs()
    for d in docs:
        poe.attach_confidence_level(d, whitelist=WL)
    assert [d["confidence_level"] for d in docs] == ["C1", "C3"]

    summary, state, gaps = _judge(docs)
    assert len(judge_calls) == 1 and judge_calls[0]["role"] == "judge"
    assert judge_calls[0]["run_id"] == "run-judge"
    fdf, marin = docs
    # port.judge keeps the parse_judgments shape.
    assert set(fdf["judge"]) == {"verdict", "reason", "quoted_excerpt", "excerpt_verified"}
    assert fdf["judge"]["verdict"] == "poe" and fdf["judge"]["excerpt_verified"] is True
    assert fdf["confidence_level"] == "C2"
    assert marin["judge"]["verdict"] == "not_poe"
    assert marin["confidence_level"] == "C1"
    assert marin["confidence_downgraded_from"] == "C3"
    assert marin["judge_contradicted"] is True
    # The V2 score is untouched.
    assert [d["confidence"] for d in docs] == [55, 80]
    assert summary["status"] == "ok" and summary["judged"] == 2
    assert summary["verdicts"] == {"poe": 1, "not_poe": 1}
    assert state["judge"] == summary
    assert gaps == {}
    # The judge call is counted on the run's call cap.
    assert tokenfactory.calls_taken("run-judge") == 1


def test_unverified_excerpt_does_not_lift(monkeypatch):
    async def fake_generate(prompt, system=None, **kw):
        return {"provider": "tokenfactory", "model": "m", "parsed": {"ports": [
            {"name": "Fort-de-France", "verdict": "poe", "reason": "r",
             "quoted_excerpt": "this sentence is not in any official text at all"}]}}

    monkeypatch.setattr(poe_judge.generator, "generate", fake_generate)
    docs = _docs()[:1]
    _judge(docs)
    assert docs[0]["judge"]["excerpt_verified"] is False
    assert docs[0]["confidence_level"] == "C1"


def test_cap_reached_skips_the_judge_and_records_why(judge_calls):
    docs = _docs()

    async def go():
        rtoken = _rules(**{"formalities.extract_max_llm_calls": 1,
                           "formalities.extract_min_zone_calls": 1})
        ttoken = tokenfactory.bind_run("run-cap")
        try:
            tokenfactory.plan_zone_calls(1, "run-cap")
            with poe.zone_extract_budget() as state:
                assert poe._take_extract_calls(1) == 1  # the extraction used the share
                summary = await poe._judge_zone_run(
                    ZONE, docs, RAW, SOURCES, WL, SimpleNamespace(run_id="run-cap"),
                    lambda m: None)
                return summary, dict(state), poe.extraction_gaps()
        finally:
            tokenfactory.reset_run(ttoken)
            run_rules.reset_rules(rtoken)

    summary, state, gaps = asyncio.run(go())
    assert judge_calls == []
    assert summary["status"] == "skipped"
    assert summary["reason"] == poe_judge.GATE_REFUSED == "formalities.extract_max_llm_calls reached"
    assert state["judge"] == summary
    assert tokenfactory.calls_taken("run-cap") == 1
    # The extraction read all it was given: a refused judge call does not mark it capped.
    assert "capped" not in gaps
    assert all("judge" not in d for d in docs)
    assert [d["confidence_level"] for d in docs] == ["C1", "C3"]


def test_off_switch(judge_calls):
    docs = _docs()
    summary, state, _ = _judge(docs, **{"formalities.judge_in_runs": False})
    assert judge_calls == []
    assert summary == {**summary, "status": "skipped", "reason": "formalities.judge_in_runs=false"}
    assert state["judge"]["reason"] == "formalities.judge_in_runs=false"
    assert tokenfactory.calls_taken("run-judge") == 0
    assert all("judge" not in d and "confidence_level" not in d for d in docs)


def test_switch_is_a_catalogue_rule_on_by_default():
    rule = run_rules.get_rule_def("formalities.judge_in_runs")
    assert rule["value"] is True and rule["kind"] == "budget" and rule["vary"] is True
    assert run_rules.get_rule("formalities.judge_in_runs") is True
    snap = run_rules.resolve_rules(mode="formalities",
                                   overrides={"formalities.judge_in_runs": "false"})
    assert snap["chosen"]["formalities.judge_in_runs"]["value"] is False


def test_no_official_text_skips_without_taking_a_call(judge_calls):
    docs = _docs()

    async def go():
        ttoken = tokenfactory.bind_run("run-empty")
        try:
            with poe.zone_extract_budget() as state:
                summary = await poe._judge_zone_run(
                    ZONE, docs, "[SOURCE: https://navily.com/x]\n" + FDF, [], WL,
                    SimpleNamespace(run_id="run-empty"), lambda m: None)
                return summary, dict(state)
        finally:
            tokenfactory.reset_run(ttoken)

    summary, state = asyncio.run(go())
    assert judge_calls == []
    assert summary["reason"] == "no official evidence text for this zone"
    assert tokenfactory.calls_taken("run-empty") == 0


def test_judge_spend_lands_on_the_run_ledger(monkeypatch):
    """Through the real generator Token Factory branch, with the HTTP step stubbed."""
    from app.core import generator

    monkeypatch.setenv("LLM_PROVIDER", "tokenfactory")
    monkeypatch.setenv("NEBIUS_API_KEY", "test-not-a-key")
    seen = []

    async def fake_complete(client, url, headers, model, prompt, system, json_mode, budget,
                            max_tokens=None):
        seen.append({"url": url, "model": model})
        import json
        return {"text": json.dumps(ANSWER), "parsed": ANSWER, "model": model,
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}

    monkeypatch.setattr(generator, "_complete", fake_complete)
    docs = _docs()
    summary, _, _ = _judge(docs, run_id="run-ledger")
    assert summary["status"] == "ok" and summary["provider"] == "tokenfactory"
    assert seen and seen[0]["url"].startswith("https://api.tokenfactory.nebius.com/")
    assert seen[0]["model"] == tokenfactory.TF_ULTRA
    assert tokenfactory.spent_usd("run-ledger") > 0
    assert tokenfactory.calls_taken("run-ledger") == 1
    assert docs[0]["confidence_level"] == "C2"


class _Coll:
    def __init__(self):
        self.docs, self.zone = [], None

    async def delete_many(self, q):
        self.docs = []

    async def insert_many(self, docs):
        self.docs.extend(dict(d) for d in docs)

    async def update_one(self, q, upd, upsert=False):
        self.zone = upd["$set"]


def test_run_zone_records_the_judge_outcome(judge_calls):
    docs = _docs()
    ports, zones = _Coll(), _Coll()
    db = {"poe_run_ports": ports, "poe_run_zones": zones}
    run = SimpleNamespace(run_id="run-zone", ports_coll="poe_run_ports",
                          zones_coll="poe_run_zones", variant="tinyfish")

    async def go():
        ttoken = tokenfactory.bind_run("run-zone")
        try:
            with poe.zone_extract_budget():
                await poe._judge_zone_run(ZONE, docs, RAW, SOURCES, WL, run, lambda m: None)
                return await poe._persist_zone_run(db, ZONE, docs, "ia", SOURCES, {}, run,
                                                   None, lambda m: None)
        finally:
            tokenfactory.reset_run(ttoken)

    zone_doc = asyncio.run(go())
    assert zone_doc["judge"]["status"] == "ok"
    assert zones.zone["judge"]["verdicts"] == {"poe": 1, "not_poe": 1}
    by_name = {d["name"]: d for d in ports.docs}
    assert by_name["Fort-de-France"]["confidence_level"] == "C2"
    assert by_name["Fort-de-France"]["judge"]["verdict"] == "poe"
    assert by_name["Le Marin"]["confidence_level"] == "C1"


def test_live_generation_never_runs_the_judge():
    """generate_zone_poe calls the judge only with a run (live poe_ports: no judge, no level)."""
    import inspect

    src = inspect.getsource(poe.generate_zone_poe)
    call = src.index("await _judge_zone_run(")
    assert src.count("await _judge_zone_run(") == 1
    assert "if run is not None:" in src[call - 120:call]


def test_judge_lift_then_geo_reaches_c3(judge_calls):
    docs = _docs()[:1]
    docs[0]["geocode_agreement_km"] = 0.3
    _judge(docs)
    assert docs[0]["confidence_level"] == "C3"


# --- the judge's call is reserved before extraction --------------------------------

def _share(n):
    """Rules giving each zone a share of exactly ``n`` calls (one zone in the run)."""
    return {"formalities.extract_max_llm_calls": n, "formalities.extract_min_zone_calls": n}


def _in_zone(run_id, rules, body):
    async def go():
        rtoken = _rules(**rules)
        ttoken = tokenfactory.bind_run(run_id)
        try:
            tokenfactory.plan_zone_calls(1, run_id)
            with poe.zone_extract_budget() as state:
                out = await body(state)
            return out, dict(state)
        finally:
            tokenfactory.reset_run(ttoken)
            run_rules.reset_rules(rtoken)
    return asyncio.run(go())


def test_reserved_call_survives_an_extraction_that_wants_the_whole_share(judge_calls):
    docs = _docs()
    run = SimpleNamespace(run_id="run-res")

    async def body(state):
        assert poe._reserve_judge_call(RAW, SOURCES, lambda m: None) is True
        granted = poe._take_extract_calls(10)  # the readers ask for far more than the share
        summary = await poe._judge_zone_run(ZONE, docs, RAW, SOURCES, WL, run, lambda m: None)
        return granted, summary

    (granted, summary), state = _in_zone("run-res", _share(3), body)
    assert granted == 2  # the readers got the share minus the judge's call
    assert summary["status"] == "ok" and len(judge_calls) == 1
    assert docs[0]["confidence_level"] == "C2" and docs[1]["confidence_level"] == "C1"
    assert tokenfactory.calls_taken("run-res") == 3  # never more than the share
    assert "judge_reserved" not in state


def test_without_official_text_nothing_is_reserved():
    async def body(state):
        return poe._reserve_judge_call(f"[SOURCE: https://navily.com/x]\n{FDF}", [],
                                       lambda m: None)

    reserved, state = _in_zone("run-noofficial", _share(3), body)
    assert reserved is False and state["used"] == 0
    assert tokenfactory.calls_taken("run-noofficial") == 0


def test_no_reservation_when_the_judge_is_off_or_sample_mode(monkeypatch):
    async def body(state):
        return poe._reserve_judge_call(RAW, SOURCES, lambda m: None)

    reserved, state = _in_zone("run-off", {**_share(3), "formalities.judge_in_runs": False}, body)
    assert reserved is False and state["used"] == 0
    monkeypatch.setenv("SAMPLE_MODE", "true")
    reserved, state = _in_zone("run-sample", _share(3), body)
    assert reserved is False and state["used"] == 0


def test_unused_reservation_goes_back_to_the_run(monkeypatch):
    run = SimpleNamespace(run_id="run-back")

    async def body(state):
        assert poe._reserve_judge_call(RAW, SOURCES, lambda m: None)
        assert state["used"] == 1
        # No port extracted: the judge is skipped and gives its call back.
        summary = await poe._judge_zone_run(ZONE, [], RAW, SOURCES, WL, run, lambda m: None)
        return summary

    summary, state = _in_zone("run-back", _share(3), body)
    assert summary["reason"] == "no ports"
    assert state["used"] == 0 and "judge_reserved" not in state
    assert tokenfactory.calls_taken("run-back") == 0


def test_share_of_one_goes_to_the_judge_without_marking_extraction_capped():
    async def body(state):
        reserved = poe._reserve_judge_call(RAW, SOURCES, lambda m: None)
        return reserved, poe.extraction_gaps()

    (reserved, gaps), state = _in_zone("run-one", _share(1), body)
    assert reserved is True and gaps == {}


def _generate_harness(monkeypatch, extract_wants):
    """generate_zone_poe in run mode with stubbed I/O; extraction asks for ``extract_wants``
    calls from the zone's share before returning its ports."""
    import app.db

    seen = {}

    async def no_settings():
        return {}

    async def find_sources(zone, *args, **kwargs):
        return [{"url": URL, "official": True}], True, None

    async def collect(official, log, rec=None, **kwargs):
        return [f"[SOURCE: {URL}]\n{FDF} {MARIN}"], {}, SOURCES, {}

    async def extract(zone, context, used_sources, log, rec=None, run=None,
                      catalog_text=None, settings=None):
        seen["extract_granted"] = poe._take_extract_calls(extract_wants)
        docs = _docs()
        for d in docs:
            poe.attach_confidence_level(d, whitelist=WL)
        return docs

    async def persist_run(db, zone, docs, status, used_sources, hashes, run, rec, log):
        seen["docs"] = docs
        return {"mrgid": zone["mrgid"], "status": status, "poe_count": len(docs),
                "judge": (poe._zone_extract.get() or {}).get("judge")}

    async def evidence(*args, **kwargs):
        seen["evidence_run"] = kwargs.get("run")
        return 0

    class _Zones:
        async def find_one(self, query):
            return dict(ZONE)

    class _DB:
        eez_zones = _Zones()

    monkeypatch.setattr(app.db, "get_settings", no_settings)
    monkeypatch.setattr(poe, "load_exceptions", lambda: {})
    monkeypatch.setattr(poe, "build_whitelist", lambda *a, **k: list(WL))
    monkeypatch.setattr(poe, "_find_sources", find_sources)
    monkeypatch.setattr(poe, "_collect_texts", collect)
    monkeypatch.setattr(poe, "extract_structured_ports", lambda raw: [])
    monkeypatch.setattr(poe, "_extract_and_geocode", extract)
    monkeypatch.setattr(poe, "_store_evidence", evidence)
    monkeypatch.setattr(poe, "_persist_zone_run", persist_run)
    return _DB(), seen


def test_generate_zone_judges_a_zone_whose_extraction_uses_the_whole_share(
        monkeypatch, judge_calls):
    db, seen = _generate_harness(monkeypatch, extract_wants=50)
    run = SimpleNamespace(run_id="run-gen", variant="tinyfish", recorder=None)

    async def go():
        rtoken = _rules(**_share(4))
        ttoken = tokenfactory.bind_run("run-gen")
        try:
            tokenfactory.plan_zone_calls(1, "run-gen")
            return await poe.generate_zone_poe(db, 1, run=run)
        finally:
            tokenfactory.reset_run(ttoken)
            run_rules.reset_rules(rtoken)

    monkeypatch.setattr(poe, "ZoneRecorder", lambda *a, **k: None)
    result = asyncio.run(go())
    assert seen["extract_granted"] == 3  # 4 - the judge's reserved call
    assert len(judge_calls) == 1
    assert result["judge"]["status"] == "ok"
    levels = {d["name"]: d["confidence_level"] for d in seen["docs"]}
    assert levels == {"Fort-de-France": "C2", "Le Marin": "C1"}
    assert tokenfactory.calls_taken("run-gen") == 4
    # Evidence records are stored for this run (poe_run_evidence), not the shared collection.
    assert seen["evidence_run"] is run


def test_generate_zone_gives_the_reservation_back_on_error(monkeypatch):
    db, seen = _generate_harness(monkeypatch, extract_wants=1)
    run = SimpleNamespace(run_id="run-err", variant="tinyfish", recorder=None)

    async def boom(*a, **k):
        raise RuntimeError("extraction failed")

    monkeypatch.setattr(poe, "_extract_and_geocode", boom)
    monkeypatch.setattr(poe, "ZoneRecorder", lambda *a, **k: None)

    async def go():
        rtoken = _rules(**_share(4))
        ttoken = tokenfactory.bind_run("run-err")
        try:
            tokenfactory.plan_zone_calls(1, "run-err")
            with pytest.raises(RuntimeError):
                await poe.generate_zone_poe(db, 1, run=run)
        finally:
            tokenfactory.reset_run(ttoken)
            run_rules.reset_rules(rtoken)

    asyncio.run(go())
    assert tokenfactory.calls_taken("run-err") == 0
