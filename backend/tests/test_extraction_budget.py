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


def test_zone_cancelled_from_outside_cancels_its_batches(reader, monkeypatch):
    """Round 4, finding 2: cancelling the zone stops the batches in flight."""
    started: list[str] = []
    stopped: list[str] = []

    async def hang(context, zone, settings=None, log=None):
        started.append(context)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.append(context)
            raise
        return []

    monkeypatch.setattr(poe, "extract_ports", hang)

    async def run():
        async def zone():
            with poe.zone_extract_budget():
                return await poe._extract_ports_core("x", ZONE, lambda m: None,
                                                     catalog_text=_big_zone(3))

        task = asyncio.ensure_future(zone())
        while len(started) < 3:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)  # in the same loop: nothing is left running
        return len(stopped), [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    stopped_in_loop, left = asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert stopped_in_loop == 3 and left == []


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


# --- failed batches (finding 3) ------------------------------------------------------

def _flaky(reader, fails: dict):
    """Reader stub: a batch naming ``Port X`` raises while ``fails[X]`` > 0."""
    async def extract(context, zone, settings=None, log=None):
        text = llm.reader_context(context)
        reader.append(text)
        names = list(dict.fromkeys(NAME_RE.findall(text)))
        for n in names:
            if fails.get(n, 0) > 0:
                fails[n] -= 1
                raise RuntimeError("provider 502")
        return [{"name": n, "city": None, "note": None} for n in names]
    return extract


def _three_sources() -> str:
    return "\n\n".join(_source(f"https://gov.example/r{i}", _prose(15000) + f" {n}.")
                       for i, n in enumerate(("Port Alpha", "Port Bravo", "Port Charlie")))


def test_failed_batch_is_retried_once_and_the_zone_stays_complete(reader, monkeypatch):
    monkeypatch.setattr(poe, "extract_ports", _flaky(reader, {"Port Bravo": 1}))
    ports, gaps = _zone(_three_sources())
    assert {p["name"] for p in ports} == {"Port Alpha", "Port Bravo", "Port Charlie"}
    assert len(reader) == 4 and gaps == {}


def test_batch_failing_twice_marks_the_zone_partial(reader, monkeypatch):
    monkeypatch.setattr(poe, "extract_ports", _flaky(reader, {"Port Bravo": 5}))
    logs: list[str] = []
    ports, gaps = _zone(_three_sources(), logs)
    assert {p["name"] for p in ports} == {"Port Alpha", "Port Charlie"}
    assert len(reader) == 4  # one retry, not more
    assert gaps == {"failed_batches": 1}
    assert any("1 lot(s) en échec relancé(s) une fois" in m for m in logs)


# --- what one call means (finding 4) -------------------------------------------------

@pytest.fixture
def provider_chain(monkeypatch):
    """Real llm.extract_ports with every provider stubbed: Nightshift unavailable, NIM
    failing, OpenRouter answering, Claude on. Records which providers were reached."""
    import app.core.ml as ml
    from app.core import claude, nvidia
    reached: list[str] = []

    async def nightshift(context, zone, log=None):
        reached.append("nightshift")
        return None

    async def nim(context, zone, settings=None, log=None, **kwargs):
        reached.append("nvidia")
        raise RuntimeError("NIM down")

    async def openrouter(context, zone, settings=None, log=None):
        reached.append("openrouter")
        return [{"name": "Port Router", "extraction_engine": "openrouter"}]

    async def claude_reader(context, zone, settings=None, log=None):
        reached.append("claude")
        return [{"name": "Port Claude", "extraction_engine": "claude"}]

    monkeypatch.setattr(ml, "extract_entities", lambda text: [])
    monkeypatch.setattr(poe, "extract_structured_ports", lambda raw: [])
    monkeypatch.setattr(poe, "extract_ports", llm.extract_ports)
    monkeypatch.setattr(llm, "extract_ports_nightshift", nightshift)
    monkeypatch.setattr(nvidia, "nvidia_enabled", lambda settings=None: True)
    monkeypatch.setattr(nvidia, "second_extract_choice", lambda context: None)
    monkeypatch.setattr(nvidia, "extract_ports_nvidia", nim)
    monkeypatch.setattr(llm, "get_llm_key", lambda settings=None: "test-key")
    monkeypatch.setattr(llm, "extract_ports_openrouter", openrouter)
    monkeypatch.setattr(claude, "claude_enabled", lambda settings=None: True)
    monkeypatch.setattr(claude, "budget_allows_call", lambda settings=None: True)
    monkeypatch.setattr(claude, "extract_ports_claude", claude_reader)
    return reached


def _zone_with_cap(cap: int) -> tuple[list[dict], dict, int]:
    snap = snapshot_for_run(mode="formalities", overrides={"formalities.extract_max_llm_calls": cap})
    token = bind_rules(snap)

    async def run():
        with poe.zone_extract_budget() as state:
            ports = await poe._extract_ports_core("Port Bravo.", ZONE, lambda m: None,
                                                  catalog_text="Port Bravo.", settings={})
            return ports, poe.extraction_gaps(), state["used"]

    try:
        return asyncio.run(run())
    finally:
        reset_rules(token)


def test_each_provider_step_counts_as_one_call(provider_chain):
    ports, gaps, used = _zone_with_cap(10)
    assert provider_chain == ["nightshift", "nvidia", "openrouter"]
    assert [p["name"] for p in ports] == ["Port Router"]
    assert used == 3 and gaps == {}


def test_fallback_chain_stops_at_the_cap(provider_chain):
    ports, gaps, used = _zone_with_cap(2)
    # Nightshift + NIM use the two calls: OpenRouter and both Claude paths stay off.
    assert provider_chain == ["nightshift", "nvidia"]
    assert ports == [] and gaps == {"capped": True} and used == 2


# --- calls_taken persisted with the run (finding 5) ----------------------------------

def test_calls_taken_is_in_the_run_cost_summary():
    tokenfactory.reset_ledger()
    try:
        tokenfactory.take_calls(4, 100, "run-r3-cost")
        summary = tokenfactory.attach({}, "run-r3-cost")
        assert summary["tokenfactory"]["calls_taken"] == 4
        persisted = {"calls": 2, "calls_taken": 7}  # stored cost, process restarted
        assert tokenfactory.public_cost("run-r3-other", persisted)["calls_taken"] == 7
    finally:
        tokenfactory.reset_ledger()


def test_resume_after_restart_keeps_the_calls_already_taken(run_harness, monkeypatch):
    async def fake_zone(db, mrgid, logger=None, force=False, run=None):
        with poe.zone_extract_budget():
            assert poe._take_extract_calls(3) == 3
        await db.poe_run_zones.update_one({"run_id": run.run_id, "mrgid": mrgid},
                                          {"$set": {"status": "ia", "poe_count": 1}}, upsert=True)
        return {"status": "ia", "poe_count": 1}

    monkeypatch.setattr(run_harness.poe, "generate_zone_poe", fake_zone)
    db = _MemDB([{"mrgid": 1, "name": "A"}, {"mrgid": 2, "name": "B"}])
    summary = _execute(run_harness, db, "run-r3-resume")
    assert summary["tokenfactory"]["calls_taken"] == 6
    doc = asyncio.run(db.poe_runs.find_one({"_id": "run-r3-resume"}))
    assert doc["calls_taken"] == 6

    # Restart: the in-memory count is gone; zone B is regenerated on resume.
    tokenfactory.reset_ledger()
    db.poe_run_zones.docs = [d for d in db.poe_run_zones.docs if d["mrgid"] == 1]
    summary = _execute(run_harness, db, "run-r3-resume", resume=True)
    assert summary["tokenfactory"]["calls_taken"] == 9
    assert tokenfactory.calls_taken("run-r3-resume") == 9


def test_resume_after_restart_keeps_the_usd_already_spent(run_harness, monkeypatch):
    """Round 4, finding 1: the USD ledger is restored with calls_taken, so the budget and
    the run summary cover the whole run, not only the resumed session."""
    monkeypatch.setattr(tokenfactory, "prices_for", lambda model: (1.0, 0.0))

    async def fake_zone(db, mrgid, logger=None, force=False, run=None):
        tokenfactory.ensure_budget(run.run_id)
        tokenfactory.record("m", {"prompt_tokens": 1_000_000}, run.run_id)  # 1 USD
        await db.poe_run_zones.update_one({"run_id": run.run_id, "mrgid": mrgid},
                                          {"$set": {"status": "ia", "poe_count": 1}}, upsert=True)
        return {"status": "ia", "poe_count": 1}

    monkeypatch.setattr(run_harness.poe, "generate_zone_poe", fake_zone)
    db = _MemDB([{"mrgid": 1, "name": "A"}, {"mrgid": 2, "name": "B"}])
    summary = _execute(run_harness, db, "run-r4-usd")
    assert summary["tokenfactory"]["usd_estimated"] == 2.0

    # Restart: zone B is regenerated on resume; the summary covers both sessions.
    tokenfactory.reset_ledger()
    db.poe_run_zones.docs = [d for d in db.poe_run_zones.docs if d["mrgid"] == 1]
    summary = _execute(run_harness, db, "run-r4-usd", resume=True)
    cost = summary["tokenfactory"]
    assert cost["usd_estimated"] == 3.0 and cost["calls"] == 3
    assert tokenfactory.spent_usd("run-r4-usd") == 3.0

    # Crash before the end (no run summary): restored from the per-zone cost; the
    # budget counts the dollars spent before the restart.
    tokenfactory.reset_ledger()
    doc = asyncio.run(db.poe_runs.find_one({"_id": "run-r4-usd"}))
    doc.pop("summary")
    db.poe_run_zones.docs = [d for d in db.poe_run_zones.docs if d["mrgid"] == 1]
    monkeypatch.setattr(tokenfactory, "budget_usd", lambda: 3.0)
    summary = _execute(run_harness, db, "run-r4-usd", resume=True)
    assert summary["errors"] == 1  # budget already reached: zone B is refused
    assert summary["tokenfactory"]["usd_estimated"] == 3.0


# --- whole-text CPU work off the event loop (finding 6) ------------------------------

def test_whole_text_steps_run_off_the_event_loop(monkeypatch):
    import threading
    import app.db

    threads: dict[str, int] = {}
    real_context = poe.extraction_context

    async def no_settings():
        return {}

    async def find_sources(zone, *args, **kwargs):
        return [{"url": "https://gov.example/1", "official": True}], True, None

    async def collect(official, log, rec=None, **kwargs):
        return [_source("https://gov.example/1", _prose(40000) + " Port Alpha.")], {}, official, {}

    async def extract(zone, context, used_sources, log, rec=None, run=None,
                      catalog_text=None, settings=None):
        return [{"name": "Port Alpha"}]

    async def persist(db, zone, docs, status, *args, **kwargs):
        return {"mrgid": zone["mrgid"], "status": status, "poe_count": len(docs)}

    async def evidence(*args, **kwargs):
        return 0

    def parse(raw):
        threads["parse"] = threading.get_ident()
        return []

    def context(raw, raw_catalog, rag_q):
        threads["rag"] = threading.get_ident()
        return real_context(raw, raw_catalog, rag_q)

    class _Zones:
        async def find_one(self, query):
            return {"mrgid": 1, "name": "Threadland"}

    class _DB:
        eez_zones = _Zones()

    monkeypatch.setattr(app.db, "get_settings", no_settings)
    monkeypatch.setattr(poe, "load_exceptions", lambda: {})
    monkeypatch.setattr(poe, "build_whitelist", lambda *a, **k: [])
    monkeypatch.setattr(poe, "_find_sources", find_sources)
    monkeypatch.setattr(poe, "_collect_texts", collect)
    monkeypatch.setattr(poe, "extract_structured_ports", parse)
    monkeypatch.setattr(poe, "extraction_context", context)
    monkeypatch.setattr(poe, "select_list_context", lambda q, raw, max_chars: raw[:max_chars])
    monkeypatch.setattr(poe, "_extract_and_geocode", extract)
    monkeypatch.setattr(poe, "_store_evidence", evidence)
    monkeypatch.setattr(poe, "_persist_zone", persist)

    async def run():
        loop_thread = threading.get_ident()
        result = await poe.generate_zone_poe(_DB(), 1)
        return loop_thread, result

    loop_thread, result = asyncio.run(run())
    assert result["poe_count"] == 1
    assert set(threads) == {"parse", "rag"}
    assert all(t != loop_thread for t in threads.values())
