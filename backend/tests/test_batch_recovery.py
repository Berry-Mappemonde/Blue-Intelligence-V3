"""A batch whose reply is not valid JSON is repaired, then re-asked once in strict
« JSON only », then listed as failed: never dropped silently (stubbed LLM, offline).

Cape Verde pilot (run 20261102-224042-f9be8d): the one batch holding the port list
returned invalid JSON on the first call and on the retry, and the zone lost it.
"""
import asyncio
import json
import re

import httpx
import pytest

from app.core import generator, llm, tokenfactory
from app.services import poe_pipeline as poe

ZONE = {"name": "Testland", "sovereign": "Testland", "mrgid": 990003}
PROSE = "General information about travel formalities, visas and fees for visitors. "
NAME_RE = re.compile(r"\bPort [A-Z][a-z]+\b")
NAMES = ("Port Alpha", "Port Bravo", "Port Charlie")


def _valid(names) -> str:
    return json.dumps({"ports": [{"name": n, "city": None, "note": None, "lat": None,
                                  "lon": None, "geocodeable": True} for n in names]})


def _truncated(names) -> str:
    """A reply cut at max_tokens in the middle of the last record."""
    return _valid(names)[:-60]


GARBAGE = "Les ports désignés sont Port Bravo et d'autres, voir le décret."


def _three_sources() -> str:
    return "\n\n".join(f"[SOURCE: https://gov.example/r{i}]\n" + (PROSE * 200)[:15000] + f" {n}."
                       for i, n in enumerate(NAMES))


@pytest.fixture
def nightshift(monkeypatch, tmp_path):
    """Nightshift reachable through a mock transport; ``script[name]`` is the list of
    replies for the batch naming ``name`` (last one repeats). Records every request."""
    for name in ("SAMPLE_MODE", "METERLESS_MODEL", "MODEL_NAME", "MODEL_BASE_URL", "LLM_PROVIDER",
                 "NEBIUS_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("METERLESS_BASE_URL", generator.DEFAULT_BASE_URL)
    monkeypatch.setenv("METERLESS_RUNTIME_TOKEN", "t")
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("TRACE_STDOUT", "0")
    generator._probe_cache.clear()
    generator._probe_locks.clear()
    script: dict[str, list[str]] = {}
    sent: list[tuple[str, str]] = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "meterless/auto"}]})
        user = json.loads(request.content)["messages"][-1]["content"]
        name = next((n for n in NAME_RE.findall(user.split("EXTRAITS DES SOURCES")[-1])), "?")
        replies = script.get(name) or [_valid([name])]
        seen = sum(1 for n, _ in sent if n == name)
        sent.append((name, user))
        text = replies[min(seen, len(replies) - 1)]
        return httpx.Response(200, json={"model": "nvidia/nemotron-3-super-120b-a12b",
                                         "choices": [{"message": {"content": text}}]})

    original = httpx.AsyncClient

    def factory(**kwargs):
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(generator.httpx, "AsyncClient", factory)
    import app.core.ml as ml
    monkeypatch.setattr(ml, "extract_entities", lambda text: [])
    monkeypatch.setattr(poe, "extract_structured_ports", lambda raw: [])
    monkeypatch.setattr(poe, "extract_ports", llm.extract_ports)
    tokenfactory.reset_ledger()
    yield script, sent
    tokenfactory.reset_ledger()
    generator._probe_cache.clear()
    generator._probe_locks.clear()


def _zone(raw: str, logs: list | None = None):
    log = logs.append if logs is not None else (lambda m: None)

    async def run():
        with poe.zone_extract_budget():
            ports = await poe._extract_ports_core(raw[:20000], ZONE, log, catalog_text=raw,
                                                  settings={})
            return ports, poe.extraction_gaps()

    return asyncio.run(run())


def _calls(sent, name):
    return [u for n, u in sent if n == name]


# --- repair_json -------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('```json\n{"ports": [{"name": "Praia"},]}\n```', {"ports": [{"name": "Praia"}]}),
    ('Voici: {"ports": [{"name": "Praia"}, {"name": "Mindelo", "city": "São Vic',
     {"ports": [{"name": "Praia"}]}),
    ('{"ports": [{"name": "Praia", "lat": 14.9, "lon": -23.5}, {"name": "Palmeira", "lat": 16.7',
     {"ports": [{"name": "Praia", "lat": 14.9, "lon": -23.5}]}),
    ('{"ports": [{"name": "A", "note": "x, y"}]} trailing {', {"ports": [{"name": "A", "note": "x, y"}]}),
])
def test_repair_json_keeps_only_complete_records(raw, expected):
    assert llm.repair_json(raw) == expected


@pytest.mark.parametrize("raw", [GARBAGE, "", None, '{"ports": [{"name": "Pal'])
def test_repair_json_gives_up_rather_than_invent(raw):
    assert llm.repair_json(raw) is None


def test_repair_is_off_outside_port_extraction():
    assert llm.parse_json_flexible('{"ports": [{"name": "A"},') is None
    assert llm.parse_json_flexible('{"ports": [{"name": "A"},', repair=True) == {"ports": [{"name": "A"}]}


# --- batches -----------------------------------------------------------------------

def test_truncated_reply_is_repaired_without_a_retry(nightshift):
    script, sent = nightshift
    script["Port Bravo"] = [_truncated(["Port Bravo", "Port Bravo"])]
    logs: list[str] = []
    ports, gaps = _zone(_three_sources(), logs)
    assert {p["name"] for p in ports} == set(NAMES)
    assert gaps == {}
    assert len(_calls(sent, "Port Bravo")) == 1
    assert any("JSON réparé" in m for m in logs)


def test_invalid_json_is_reasked_once_in_strict_json_only(nightshift):
    script, sent = nightshift
    script["Port Bravo"] = [GARBAGE, _valid(["Port Bravo"])]
    logs: list[str] = []
    ports, gaps = _zone(_three_sources(), logs)
    assert {p["name"] for p in ports} == set(NAMES)
    assert gaps == {}
    first, second = _calls(sent, "Port Bravo")
    assert llm.STRICT_JSON_RETRY not in first
    assert llm.STRICT_JSON_RETRY in second
    # a batch that answered well is not re-prompted
    assert all(llm.STRICT_JSON_RETRY not in u for u in _calls(sent, "Port Alpha"))
    assert any("« JSON seul » strict" in m for m in logs)


def test_batch_still_invalid_marks_the_zone_partial_and_is_listed(nightshift):
    script, sent = nightshift
    script["Port Bravo"] = [GARBAGE]
    logs: list[str] = []
    ports, gaps = _zone(_three_sources(), logs)
    assert {p["name"] for p in ports} == {"Port Alpha", "Port Charlie"}
    assert len(_calls(sent, "Port Bravo")) == 2  # first call + one strict retry, no more
    assert gaps["failed_batches"] == 1
    (detail,) = gaps["failed_batch_details"]
    assert detail["batch"] == 2 and detail["of"] == 3
    assert detail["sources"] == ["https://gov.example/r1"]
    assert detail["attempts"] == 2 and detail["strict_retry"] is True
    assert detail["reason"].startswith("invalid_json") and "nightshift" in detail["reason"]
    assert any("lot 2/3 en échec après 2 essai(s)" in m and "résumé du run" in m for m in logs)


def test_retry_leaves_nightshift_only_when_another_provider_is_configured(nightshift, monkeypatch):
    script, sent = nightshift
    script["Port Bravo"] = [GARBAGE]
    answers = iter([GARBAGE, _valid(["Port Bravo"])])
    asked: list[str] = []

    async def openrouter(prompt, system, key, model=None, json_mode=True, max_tokens=2000,
                         images=None):
        asked.append(prompt)
        return next(answers)

    monkeypatch.setattr(llm, "get_llm_key", lambda settings=None: "test-key")
    monkeypatch.setattr(llm, "_call_openrouter", openrouter)
    script["Port Alpha"] = script["Port Charlie"] = [GARBAGE]  # only Bravo reaches OpenRouter
    monkeypatch.setattr(poe, "extraction_batches",
                        lambda raw, size, overlap: [raw.split("\n\n")[1]])
    ports, gaps = _zone(_three_sources())
    assert {p["name"] for p in ports} == {"Port Bravo"}
    assert gaps == {}
    assert len(_calls(sent, "Port Bravo")) == 1  # the retry did not go back to Nightshift
    assert llm.STRICT_JSON_RETRY in asked[-1]


def test_retry_keeps_nightshift_when_it_is_the_only_provider(nightshift):
    script, sent = nightshift
    script["Port Bravo"] = [GARBAGE, _valid(["Port Bravo"])]
    ports, _gaps = _zone(_three_sources())
    assert "Port Bravo" in {p["name"] for p in ports}
    assert len(_calls(sent, "Port Bravo")) == 2


def test_single_call_invalid_json_is_retried_then_listed(nightshift):
    """Text that fits one call: same recovery, and a failure is listed, not lost."""
    script, sent = nightshift
    raw = "[SOURCE: https://gov.example/one]\n" + PROSE * 20 + " Port Bravo."
    script["Port Bravo"] = [GARBAGE]
    logs: list[str] = []
    _ports, gaps = _zone(raw, logs)
    assert len(_calls(sent, "Port Bravo")) == 2
    assert gaps["failed_batches"] == 1
    assert gaps["failed_batch_details"][0]["sources"] == ["https://gov.example/one"]


def test_failed_batches_reach_the_run_zone_record(nightshift):
    script, _sent = nightshift
    script["Port Bravo"] = [GARBAGE]

    class _Coll:
        def __init__(self):
            self.docs = []

        async def delete_many(self, q):
            return None

        async def insert_many(self, docs):
            self.docs.extend(docs)

        async def update_one(self, q, u, upsert=False):
            self.docs.append(u["$set"])

    class _DB:
        poe_run_ports, poe_run_zones = _Coll(), _Coll()

        def __getitem__(self, name):
            return getattr(self, name)

    class _Run:
        run_id = "run-batch-recovery"
        ports_coll, zones_coll = "poe_run_ports", "poe_run_zones"
        variant = "tinyfish"

    db = _DB()

    async def zone():
        with poe.zone_extract_budget():
            ports = await poe._extract_ports_core("x", ZONE, lambda m: None,
                                                  catalog_text=_three_sources(), settings={})
            docs = [{"name": p["name"], "dedup_key": p["name"]} for p in ports]
            return await poe._persist_zone_run(db, ZONE, docs, "ia", [], {}, _Run(), None,
                                               lambda m: None)

    doc = asyncio.run(zone())
    assert doc["extraction_partial"] is True
    assert doc["extraction_gaps"]["failed_batch_details"][0]["batch"] == 2
