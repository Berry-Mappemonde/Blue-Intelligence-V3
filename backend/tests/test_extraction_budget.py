"""P0-3 round 3: call cap shared fairly between zones, no uncounted fallback once a zone
is capped, partial zones recorded (stubbed LLM, offline)."""
import asyncio
import re

import pytest

from app.core import llm, tokenfactory
from app.core.run_rules import bind_rules, reset_rules, snapshot_for_run
from app.services import poe_pipeline as poe

ZONE = {"name": "Testland", "sovereign": "Testland", "mrgid": 990002}
PROSE = "General information about travel formalities, visas and fees for visitors. "
NAME_RE = re.compile(r"\bPort [A-Z][a-z]+\b")


def _source(url: str, body: str) -> str:
    return f"[SOURCE: {url}]\n{body}"


def _prose(chars: int) -> str:
    return (PROSE * (chars // len(PROSE) + 1))[:chars]


def _big_zone(sources: int) -> str:
    """Raw text of ``sources`` sources of 15k chars: one batch each."""
    return "\n\n".join(_source(f"https://gov.example/s{i}", _prose(15000) + " Port Alpha.")
                       for i in range(sources))


@pytest.fixture
def reader(monkeypatch):
    sent: list[str] = []

    async def fake_extract(context, zone, settings=None, log=None):
        text = llm.reader_context(context)
        sent.append(text)
        return [{"name": n, "city": None, "note": None, "extraction_engine": "nightshift"}
                for n in dict.fromkeys(NAME_RE.findall(text))]

    import app.core.ml as ml
    monkeypatch.setattr(poe, "extract_ports", fake_extract)
    monkeypatch.setattr(ml, "extract_entities", lambda text: [])
    monkeypatch.setattr(poe, "extract_structured_ports", lambda raw: [])
    tokenfactory.reset_ledger()
    yield sent
    tokenfactory.reset_ledger()


@pytest.fixture
def paid_fallbacks(monkeypatch):
    """Second reader (NIM) and Claude switched on, each call recorded."""
    from app.core import claude, nvidia
    called: list[str] = []

    async def fake_nim(context, zone, settings=None, log=None, model=None, engine=None,
                       fallback=True):
        called.append("nvidia")
        return [{"name": "Port Nim", "extraction_engine": "nvidia-gpt-oss"}]

    async def fake_claude(context, zone, settings=None, log=None):
        called.append("claude")
        return [{"name": "Port Claude", "extraction_engine": "claude"}]

    monkeypatch.setattr(nvidia, "nvidia_enabled", lambda settings=None: True)
    monkeypatch.setattr(nvidia, "second_extract_choice", lambda context: ("m", "nvidia-gpt-oss"))
    monkeypatch.setattr(nvidia, "extract_ports_nvidia", fake_nim)
    monkeypatch.setattr(claude, "claude_enabled", lambda settings=None: True)
    monkeypatch.setattr(claude, "budget_allows_call", lambda settings=None: True)
    monkeypatch.setattr(claude, "extract_ports_claude", fake_claude)
    return called


def _zone(raw: str, logs=None) -> tuple[list[dict], dict]:
    """One zone: extraction inside its own budget scope; returns ports and its gaps."""
    log = logs.append if logs is not None else (lambda m: None)

    async def run():
        with poe.zone_extract_budget():
            ports = await poe._extract_ports_core(raw[:20000], ZONE, log, catalog_text=raw)
            return ports, poe.extraction_gaps()

    return asyncio.run(run())


@pytest.fixture
def run_cap():
    def bind(cap: int, zones: int, floor: int = 1):
        snap = snapshot_for_run(mode="formalities", overrides={
            "formalities.extract_max_llm_calls": cap,
            "formalities.extract_min_zone_calls": floor})
        tokens.append((bind_rules(snap), tokenfactory.bind_run("run-r3-share")))
        tokenfactory.plan_zone_calls(zones, "run-r3-share")

    tokens: list = []
    yield bind
    for rules_token, run_token in tokens:
        tokenfactory.reset_run(run_token)
        reset_rules(rules_token)


def test_first_big_zone_no_longer_starves_the_others(reader, run_cap):
    run_cap(cap=20, zones=4)
    # Zone 1 needs 30 batches; before, it took all 20 calls and zones 2-4 got none.
    _zone(_big_zone(30))
    assert len(reader) == 5  # 20 free / 4 zones
    for _ in range(3):
        reader.clear()
        ports, gaps = _zone(_source("https://gov.example/small", "Port of entry: Port Bravo."))
        assert len(reader) == 1 and [p["name"] for p in ports] == ["Port Bravo"]
        assert gaps == {}
    # Small zones gave back their unused share.
    assert tokenfactory.calls_taken("run-r3-share") == 8


def test_share_has_a_floor(reader, run_cap):
    run_cap(cap=100, zones=50, floor=6)
    _zone(_big_zone(30))
    assert len(reader) == 6  # 100 / 50 = 2, floor 6


def test_capped_zone_is_marked_and_skips_uncounted_readers(reader, run_cap, paid_fallbacks):
    run_cap(cap=3, zones=1)
    ports, gaps = _zone(_big_zone(5))
    # The primary takes the whole share (3 of 5 batches); the second reader is counted
    # too, so it does not start.
    assert len(reader) == 3 and paid_fallbacks == []
    assert gaps == {"capped": True, "unread_batches": 2}
    assert tokenfactory.calls_taken("run-r3-share") == 3

    # Cap used up: next zone (planned beyond the cap) sends nothing, Claude included.
    tokenfactory.plan_zone_calls(1, "run-r3-share")
    reader.clear()
    paid_fallbacks.clear()
    logs: list[str] = []
    ports, gaps = _zone(_source("https://gov.example/small", "Port Bravo."), logs)
    assert reader == [] and paid_fallbacks == []
    assert ports == [] and gaps == {"capped": True}
    assert any("Claude (dernier recours) sauté" in m for m in logs)
    assert tokenfactory.calls_taken("run-r3-share") == 3


def test_claude_last_resort_is_counted(reader, run_cap, paid_fallbacks, monkeypatch):
    from app.core import nvidia

    async def broken(context, zone, settings=None, log=None):
        reader.append(context)
        raise RuntimeError("reader down")

    monkeypatch.setattr(poe, "extract_ports", broken)
    monkeypatch.setattr(nvidia, "nvidia_enabled", lambda settings=None: False)
    run_cap(cap=5, zones=1)
    ports, gaps = _zone(_source("https://gov.example/small", "Port Bravo."))
    assert [p["name"] for p in ports] == ["Port Claude"]
    assert tokenfactory.calls_taken("run-r3-share") == 2  # primary + Claude


class _Coll:
    def __init__(self):
        self.docs: list[dict] = []

    async def delete_many(self, query):
        return None

    async def insert_many(self, docs):
        self.docs.extend(docs)

    async def update_one(self, query, update, upsert=False):
        self.docs.append(update["$set"])


class _RunDB:
    def __init__(self):
        self.poe_run_ports, self.poe_run_zones = _Coll(), _Coll()

    def __getitem__(self, name):
        return getattr(self, name)


class _Run:
    run_id = "run-r3-persist"
    ports_coll, zones_coll = "poe_run_ports", "poe_run_zones"
    variant = "tinyfish"


def _persist(db) -> dict:
    return asyncio.run(poe._persist_zone_run(
        db, ZONE, [{"name": "Port Alpha", "dedup_key": "a"}], "ia", [], {}, _Run(), None,
        lambda m: None))


def test_capped_zone_is_recorded_partial_in_the_run(reader, run_cap):
    run_cap(cap=2, zones=1)
    db = _RunDB()

    async def zone():
        with poe.zone_extract_budget():
            await poe._extract_ports_core("x", ZONE, lambda m: None, catalog_text=_big_zone(4))
            return await poe._persist_zone_run(
                db, ZONE, [{"name": "Port Alpha", "dedup_key": "a"}], "ia", [], {}, _Run(),
                None, lambda m: None)

    doc = asyncio.run(zone())
    assert doc["extraction_partial"] is True
    assert doc["extraction_gaps"] == {"capped": True, "unread_batches": 2}
    assert db.poe_run_zones.docs[-1]["extraction_partial"] is True

    complete = _persist(_RunDB())  # nothing left out: complete
    assert complete["extraction_partial"] is False and complete["extraction_gaps"] is None


# --- per-source ceiling and the extraction deadline (finding 2) ----------------------

def test_source_ceiling_fits_the_zone_timeout():
    from app.core import generator
    from app.core.run_rules import catalog_default
    from app.services import poe_runs
    ceiling = catalog_default("formalities.source_max_chars")
    assert ceiling == poe.SOURCE_MAX_CHARS
    size = catalog_default("formalities.extract_batch_chars")
    overlap = catalog_default("formalities.extract_batch_overlap_chars")
    workers = catalog_default("formalities.extract_batch_concurrency")
    reserve = catalog_default("formalities.extract_deadline_reserve_s")
    assert reserve == poe.EXTRACT_DEADLINE_RESERVE_S
    batches = poe.extraction_batches(_source("https://gov.example/full", _prose(ceiling)), size,
                                     overlap)
    rounds = -(-len(batches) // workers)
    search_and_collect_s = 120  # estimate, see the rule text
    window = poe_runs.ZONE_TIMEOUT_S - reserve - search_and_collect_s
    # Two sources at the ceiling are read in time even if every call takes the full timeout.
    assert 2 * rounds * generator.REQUEST_TIMEOUT_S <= window + generator.REQUEST_TIMEOUT_S
    assert ceiling > 71457  # the Bermuda passage is still kept


def test_deadline_keeps_the_batches_already_read(reader, monkeypatch):
    async def slow_or_fast(context, zone, settings=None, log=None):
        text = llm.reader_context(context)
        reader.append(text)
        if "SLOW" in text:
            await asyncio.sleep(30)
        return [{"name": n, "city": None, "note": None} for n in dict.fromkeys(NAME_RE.findall(text))]

    monkeypatch.setattr(poe, "extract_ports", slow_or_fast)
    raw = "\n\n".join(
        [_source(f"https://gov.example/f{i}", _prose(15000) + f" Port {name}.")
         for i, name in enumerate(("Alpha", "Bravo"))]
        + [_source(f"https://gov.example/s{i}", _prose(15000) + " SLOW Port Slow.") for i in range(2)])
    logs: list[str] = []

    async def zone():
        poe.set_zone_deadline(0.9)  # reserve = 0.3 s (a third): reading stops after 0.6 s
        with poe.zone_extract_budget():
            ports = await poe._extract_ports_core("x", ZONE, logs.append, catalog_text=raw)
            return ports, poe.extraction_gaps()

    ports, gaps = asyncio.run(asyncio.wait_for(zone(), timeout=5))
    assert {p["name"] for p in ports} == {"Port Alpha", "Port Bravo"}
    assert gaps == {"deadline_hit": True, "unread_batches": 2}
    assert any("délai d'extraction atteint: 2/4" in m for m in logs)


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, n):
        return list(self.docs[:n])

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


def _match(doc, query):
    for k, v in query.items():
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _MemColl:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def create_index(self, *a, **k):
        return None

    def find(self, query=None, projection=None):
        return _Cursor([d for d in self.docs if _match(d, query or {})])

    async def find_one(self, query):
        return next((d for d in self.docs if _match(d, query)), None)

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def count_documents(self, query):
        return sum(_match(d, query) for d in self.docs)

    async def update_one(self, query, update, upsert=False):
        doc = await self.find_one(query)
        if doc is None:
            if not upsert:
                return None
            doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
            doc.update(update.get("$setOnInsert", {}))
            self.docs.append(doc)
        doc.update(update.get("$set", {}))


class _MemDB:
    def __init__(self, zones):
        self.eez_zones = _MemColl(zones)
        for name in ("poe_runs", "poe_run_zones", "poe_run_ports", "poe_run_events"):
            setattr(self, name, _MemColl())


@pytest.fixture
def run_harness(monkeypatch, tmp_path):
    import app.core.events as events
    import app.db
    from app.services import poe_runs

    async def no_settings():
        return {}

    monkeypatch.setattr(events, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(app.db, "get_settings", no_settings)
    tokenfactory.reset_ledger()
    yield poe_runs
    tokenfactory.reset_ledger()


def _execute(poe_runs, db, run_id, resume=False):
    from app.core.tasks import TaskState
    return asyncio.run(poe_runs.execute_run(db, TaskState(), run_id, only_zones=[1, 2],
                                            concurrency=1, resume=resume))


def test_run_sets_the_zone_deadline_for_batch_reading(run_harness, monkeypatch):
    seen = []

    async def fake_zone(db, mrgid, logger=None, force=False, run=None):
        seen.append(poe.zone_deadline.get())
        await db.poe_run_zones.update_one({"run_id": run.run_id, "mrgid": mrgid},
                                          {"$set": {"status": "ia", "poe_count": 1}}, upsert=True)
        return {"status": "ia", "poe_count": 1}

    monkeypatch.setattr(run_harness.poe, "generate_zone_poe", fake_zone)
    db = _MemDB([{"mrgid": 1, "name": "A"}, {"mrgid": 2, "name": "B"}])
    _execute(run_harness, db, "run-r3-deadline")
    assert len(seen) == 2 and all(s is not None and s[1] == run_harness.ZONE_TIMEOUT_S for s in seen)
    assert poe.zone_deadline.get() is None
