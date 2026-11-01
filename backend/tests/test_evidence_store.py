"""Official-source evidence: records from fetched official text, stored and looked up by zone."""
from __future__ import annotations

import asyncio
import collections
import hashlib
import itertools
import json
import re

import pytest

from app.adapters import evidence_store
from app.agents import registry_check
from app.agents.registry_check import TIER1, TIER2, TIER_REGISTERED, check_ports, entry_mention, grounding_variants

BERMUDA = {"name": "Bermuda", "mrgid": 8402}
ST_GEORGE = {"name": "Port of St. George’s", "lat": 32.3810447, "lon": -64.676717, "mrgid": 8402}
GOV_BM = "https://www.customs.gov.bm/yachts"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("TRACE_STDOUT", "0")
    monkeypatch.setenv("SAMPLE_MODE", "true")
    monkeypatch.setenv("HMEM_DIR", str(tmp_path / "hmem"))
    for name in ("ZETARIS_API_KEY", "ZETARIS_ORG_ID", "ZETARIS_REST_URL", "METERLESS_RUNTIME_TOKEN", "NVIDIA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def trace_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def mention(text, name):
    return entry_mention(text, grounding_variants(name))


# --- records come from fetched official text only ---------------------------------------
def test_record_is_built_from_fetched_official_text_only():
    page = "Notice to mariners. All visiting yachts must clear customs at St. George's Harbour."
    context = (f"[SOURCE: {GOV_BM}]\n{page}\n\n"
               "[SOURCE: https://www.noonsite.com/bermuda]\nClear customs at Hamilton.\n\n"
               "[SYNTHÈSE DE RECHERCHE (à recouper)]\nHamilton is a port of entry.")
    full_sha = hashlib.sha256(b"the full fetched page, longer than the kept block").hexdigest()
    used = [{"url": GOV_BM, "sha256": full_sha, "collected_at": "2026-10-25T00:00:00+00:00", "level": "N1-trafilatura"}]
    records = evidence_store.build_records(
        8402, ["Port of St. George’s", "Port of Hamilton"], evidence_store.documents_from_context(context, used))
    # Hamilton is named only by a non-official site and by the model's synthesis: no record.
    assert records == [{
        "mrgid": 8402, "port_key": "saint george", "url": GOV_BM, "domain": "customs.gov.bm",
        "matched_name": "saint george", "snippet": page, "content_sha256": full_sha,
        "fetched_at": "2026-10-25T00:00:00+00:00", "extract_level": "N1-trafilatura"}]
    assert set(records[0]) == set(evidence_store.RECORD_FIELDS)


def test_snippet_is_a_verbatim_slice_containing_the_match():
    text = ("Lorem ipsum dolor sit amet. " * 40 + "Visiting yachts clear customs and immigration at "
            "St. George's on arrival; Hamilton is for commercial shipping. " + "Consectetur adipiscing. " * 40)
    record = evidence_store.build_records(8402, ["Port of St. George’s"],
                                          [{"url": GOV_BM, "text": text}])[0]
    assert record["snippet"] in text and len(record["snippet"]) <= registry_check.SNIPPET_CHARS
    assert "St. George's" in record["snippet"] and "customs" in record["snippet"]
    assert record["content_sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_at_most_three_records_per_port_best_match_per_url():
    docs = [{"url": f"https://www.gov.bm/page{i}", "text": f"{'x ' * i}Yachts clear customs at St. George's."}
            for i in range(5)]
    records = evidence_store.build_records(8402, ["Port of St. George’s"], docs)
    assert len(records) == evidence_store.MAX_PER_PORT
    assert len({r["url"] for r in records}) == 3


# --- stronger grounding -----------------------------------------------------------------
@pytest.mark.parametrize("text", [
    # Bermuda address and contact listings: Hamilton is named, but not designated.
    "Department of Immigration, Cabinet Building, 105 Front Street, Hamilton.",
    "Please contact Customs at the following locations: Cabinet Building, Hamilton.",
    "Customs Department, Custom House, 40 Front Street, Hamilton HM 12.",
    "BRIDGETOWN PORT University Row Hamilton immigration.seaport@gov.bm",
    # Strong terms, so only the address check can reject these.
    "Ports of entry enquiries: Cabinet Building, Hamilton.",
    "To clear customs, visit Custom House, 40 Front Street, Hamilton HM 12.",
    "Hamilton office, write to clear-in@customs.gov.bm",
    # A bare mention far from any entry term.
    "Hamilton is the capital of Bermuda. " + "It has shops and restaurants. " * 9 + "Customs forms are online.",
    "Hamilton is the capital of Bermuda.",
])
def test_address_or_bare_mentions_do_not_count(text):
    assert mention(text, "Port of Hamilton") is None
    assert evidence_store.build_records(8402, ["Port of Hamilton"], [{"url": GOV_BM, "text": text}]) == []


@pytest.mark.parametrize("name, text, term", [
    ("Port of Hamilton", "Yachts may also clear in at Hamilton by arrangement.", "clear in"),
    ("Port of St. George’s", "St. George's is the port of entry for all pleasure craft.", "port of entry"),
    ("Porto / Marina de Ponta Delgada", "O posto de fronteira marítimo funciona na Marina de Ponta Delgada.",
     "posto de fronteira"),
    ("Puerto de Ensenada", "Puertos habilitados para embarcaciones de recreo: Ensenada y Manzanillo.",
     "puertos habilitados"),
    ("Marigot", "Les formalités de douane se font au bureau du port de Marigot.", "douane"),
])
def test_entry_term_near_the_name_counts_in_english_and_other_languages(name, text, term):
    found = mention(text, name)
    assert found is not None and found["entry_term"] == term and found["gap"] <= registry_check.PROXIMITY_CHARS


# Verbatim from the Bermuda customs declaration notice (gov.bm): goods are declared in
# Hamilton's Long Room, which does not make the Port of Hamilton an entry point.
LONG_ROOM = ("3.2 How do I present the BCD? \nThe BCD and supporting documents should be presented at the "
             "Hamilton or Airport Long Room.  \nYou do not have to lodge the BCD at the same location the goods "
             "arrive. \n3.3 Can I get assistance in completing my Declaration? \nYes.  Customs Officers are not "
             "permitted to fill out your form for you")
# Verbatim from CBP's Alaska pleasure-boat page (cbp.gov).
CBP_ALASKA = ("Anchorage, Alaska\nDesignated Marina: Anchorage Small Boat Harbor\nHours of Operation: 8 a.m. to "
              "4 p.m., Seven Days a Week\nPhone Number: 907-271-6309, ext. 360")


def test_long_room_customs_case_does_not_count():
    assert mention(LONG_ROOM, "Port of Hamilton") is None


def test_weak_term_with_only_the_town_left_by_stripping_does_not_count():
    # A port word is nearby, but the match is only "Hamilton" from "Port of Hamilton".
    assert mention("Customs officers board yachts in Hamilton harbour.", "Port of Hamilton") is None


def test_weak_term_without_a_port_word_does_not_count():
    # Full name, weak term, no port word ("Antigua and Barbuda Customs").
    assert mention("Antigua and Barbuda Customs and Excise Division.", "Barbuda") is None


def test_weak_term_counts_with_the_full_name_and_a_port_word():
    found = mention("The customs office is at the Port of Hamilton ferry terminal.", "Port of Hamilton")
    assert found is not None and found["strength"] == "weak" and found["matched_name"] == "port of hamilton"


@pytest.mark.parametrize("name, text, term", [
    ("Brisbane", "Points of entry for small craft include the port of Brisbane.", "points of entry"),
    ("Marigot", "Le port de Marigot est un point d'entrée pour les plaisanciers.", "point d entree"),
    ("Ensenada", "Puntos de entrada: puerto de Ensenada.", "puntos de entrada"),
])
def test_point_of_entry_is_weak_and_counts_with_a_port_word(name, text, term):
    found = mention(text, name)
    assert found is not None and found["entry_term"] == term and found["strength"] == "weak"


def test_point_of_entry_without_a_port_word_does_not_count():
    # An airport-only sentence: "airport" is not a port word.
    assert mention("Points of entry include Brisbane International Airport.", "Brisbane") is None
    assert mention("Brisbane Airport is a designated point of entry for air travellers.", "Brisbane") is None


def test_strong_term_is_enough_even_for_the_town_name():
    text = ("Customs officers are stationed at Bermuda’s main ports of entry: St. George’s "
            "L. F. Wade International Airport Hamilton")
    found = mention(text, "Port of Hamilton")
    assert found is not None and found["entry_term"] == "ports of entry" and found["strength"] == "strong"


def test_designated_marina_is_a_strong_term_at_its_real_distance():
    found = mention(CBP_ALASKA, "Anchorage Small Boat Harbor")
    assert found["entry_term"] == "designated marina" and found["strength"] == "strong" and found["gap"] == 2
    assert registry_check.PROXIMITY_CHARS == 200  # not widened for this case


def test_a_facility_word_alone_never_names_the_port():
    # "Porto" is a variant of "Porto / Marina de Ponta Delgada" only as a facility word.
    assert mention("O porto tem alfândega.", "Porto / Marina de Ponta Delgada") is None


# --- stored snippets reach tier 1 / tier 2 ------------------------------------------------
def test_stored_snippet_reaches_tier1_with_a_registry_match(isolated):
    records = evidence_store.build_records(
        8402, ["St. George’s"], [{"url": GOV_BM, "text": "All yachts must clear customs at St. George's Harbour.",
                                  "sha256": "a" * 64, "fetched_at": "2026-10-01T00:00:00+00:00"}])
    ports, summary = check_ports([ST_GEORGE], BERMUDA, "ev-t1", stored_records=records)
    reg = ports[0]["registry"]
    assert reg["tier"] == TIER1 and ports[0]["zetaris_validation"] == "official"
    assert reg["official_evidence"] == [{
        "url": GOV_BM, "domain": "customs.gov.bm", "name_found": "saint george", "entry_term": "clear customs",
        "snippet": "All yachts must clear customs at St. George's Harbour.", "evidence_source": "stored_snippet",
        "fetched_at": "2026-10-01T00:00:00+00:00", "content_sha256": "a" * 64}]
    assert reg["reasons"][0].startswith(f"Official Commercial Port of Entry: named in official source {GOV_BM} "
                                        "(stored snippet), and listed in UN/LOCODE BMSGE")
    event = [r for r in trace_rows(isolated / "trace.jsonl") if r["action"] == "registry_check"][0]
    assert event["official_evidence"][0]["evidence_source"] == "stored_snippet"
    assert event["input_summary"]["stored_records"] == 1 and summary["official_count"] == 1


def test_stored_snippet_reaches_tier2_without_a_registry_match():
    url = "https://www.bahamascustoms.gov.bs/accordions/about-ecas-2/"
    records = evidence_store.build_records(8404, ["Conch Inn Marina"], [
        {"url": url, "text": "Ports of Entry Abaco Marsh Harbour - Cherokee Air - Conch Inn Marina - Mangoes Marina"}])
    port = {"name": "Conch Inn Marina", "lat": 26.5465414, "lon": -77.0515419, "mrgid": 8404}
    ports, _ = check_ports([port], {"name": "Bahamas", "mrgid": 8404}, "ev-t2", stored_records=records)
    assert ports[0]["registry"]["tier"] == TIER2
    assert ports[0]["registry"]["official_evidence"][0]["evidence_source"] == "stored_snippet"


@pytest.mark.parametrize("change", [
    {"snippet": "St. George's is a lovely historic town."},          # snippet no longer passes the check
    {"url": "https://www.noonsite.com/bermuda"},                      # not an official URL
    {"port_key": "hamilton"},                                        # another port's record
])
def test_stored_records_that_fail_the_check_do_not_count(change):
    record = {"mrgid": 8402, "port_key": "saint george", "url": GOV_BM,
              "snippet": "All yachts must clear customs at St. George's Harbour.", **change}
    ports, _ = check_ports([ST_GEORGE], BERMUDA, "ev-bad", stored_records=[record])
    assert ports[0]["registry"]["tier"] == TIER_REGISTERED and ports[0]["registry"]["official_evidence"] == []


# --- lookup: Mongo, sample pages, and never official on failure ------------------------
def test_lookup_failure_gives_no_evidence_and_never_official(monkeypatch, isolated):
    monkeypatch.delenv("SAMPLE_MODE")

    def broken(*args, **kwargs):
        raise OSError("disk unreadable")

    monkeypatch.setattr(evidence_store, "_mongo_records", broken)
    monkeypatch.setattr(evidence_store, "sample_documents", broken)
    assert evidence_store.lookup(8402, ["Port of St. George’s"], "ev-fail") == []
    event = [r for r in trace_rows(isolated / "trace.jsonl") if r["action"] == "evidence_lookup"][-1]
    assert event["agent_name"] == "ZetarisAdapter" and event["status"] == "error" and event["records"] == 0
    monkeypatch.setenv("SAMPLE_MODE", "true")  # registry fixtures, but the evidence lookup still fails
    from app.pipeline.run_pipeline import check_registries
    ports, summary = check_registries([dict(ST_GEORGE)], "Bermuda", "ev-fail-2")
    assert ports[0]["registry"]["tier"] == TIER_REGISTERED and summary["official_count"] == 0


def test_mongo_down_falls_back_to_the_committed_pages(monkeypatch, isolated):
    monkeypatch.delenv("SAMPLE_MODE")

    def down(mrgid):
        raise ConnectionError("mongo unreachable")

    monkeypatch.setattr(evidence_store, "_mongo_records", down)
    records = evidence_store.lookup(8402, ["Port of St. George’s"], "ev-down")
    assert {r["domain"] for r in records} == {"gov.bm", "planning.gov.bm"}
    event = [r for r in trace_rows(isolated / "trace.jsonl") if r["action"] == "evidence_lookup"][-1]
    assert event["status"] == "fallback" and event["source"] == "sample_pages"
    assert evidence_store.lookup(None, ["Port of St. George’s"], "ev-none") == []


def test_mongo_records_are_used_when_available(monkeypatch):
    monkeypatch.delenv("SAMPLE_MODE")
    stored = [{"mrgid": 8402, "port_key": "saint george", "url": GOV_BM,
               "snippet": "All yachts must clear customs at St. George's Harbour."}]
    monkeypatch.setattr(evidence_store, "_mongo_records", lambda mrgid: stored if mrgid == 8402 else [])
    assert evidence_store.lookup(8402, ["Port of St. George’s"], "ev-mongo") == stored


# --- storage (Mongo collection poe_evidence) -----------------------------------------------
class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, key, direction):
        self.docs.sort(key=lambda d: d.get(key) or "", reverse=direction < 0)
        return self

    def skip(self, n):
        self.docs = self.docs[n:]
        return self

    async def to_list(self, n):
        return self.docs[:n]


class FakeCollection:
    def __init__(self):
        self.docs, self.indexes, self.ids = [], [], itertools.count(1)

    async def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    async def update_one(self, flt, update, upsert=False):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in flt.items()):
                doc.update(update["$set"])
                return
        if upsert:
            self.docs.append({"_id": next(self.ids), **flt, **update["$set"]})

    def find(self, flt, projection=None):
        return FakeCursor(d for d in self.docs if all(d.get(k) == v for k, v in flt.items()))

    async def delete_many(self, flt):
        gone = set(flt["_id"]["$in"])
        self.docs = [d for d in self.docs if d["_id"] not in gone]


class FakeDB(dict):
    def __getitem__(self, name):
        return self.setdefault(name, FakeCollection())

    def __getattr__(self, name):
        return self[name]


def test_store_upserts_by_url_and_keeps_three_per_port():
    db = FakeDB()
    records = [{"mrgid": 8402, "port_key": "saint george", "url": f"https://www.gov.bm/p{i}",
                "snippet": "customs at St. George's", "fetched_at": f"2026-10-0{i}T00:00:00+00:00"} for i in range(1, 6)]
    asyncio.run(evidence_store.store(db, records[:2]))
    asyncio.run(evidence_store.store(db, [dict(records[0], snippet="updated")] + records[2:]))
    coll = db[evidence_store.COLLECTION]
    assert coll.indexes[0] == ([("mrgid", 1), ("port_key", 1), ("url", 1)], {"unique": True, "name": "mrgid_port_key_url"})
    assert sorted(d["url"] for d in coll.docs) == ["https://www.gov.bm/p3", "https://www.gov.bm/p4", "https://www.gov.bm/p5"]
    assert asyncio.run(evidence_store.store(db, [])) == 0


def test_generation_stores_records_from_the_collected_text_never_the_model():
    from app.services import poe_pipeline
    db, logs = FakeDB(), []
    raw = (f"[SOURCE: {GOV_BM}]\nAll yachts must clear customs at St. George's Harbour.\n\n"
           "[SYNTHÈSE DE RECHERCHE (à recouper)]\nInvented Harbour is the only port of entry.")
    used = [{"url": GOV_BM, "sha256": "b" * 64, "collected_at": "2026-10-25T00:00:00+00:00", "level": "N1"}]
    docs = [{"name": "Port of St. George’s", "note": "LLM: designated port of entry"},
            {"name": "Invented Harbour", "note": "LLM: port of entry"}]
    stored = asyncio.run(poe_pipeline._store_evidence(db, {"mrgid": 8402}, docs, raw, used, logs.append))
    rows = db[evidence_store.COLLECTION].docs
    assert stored == 1 and [r["port_key"] for r in rows] == ["saint george"] and rows[0]["content_sha256"] == "b" * 64

    class Broken(FakeDB):
        def __getitem__(self, name):
            raise RuntimeError("no database")

    assert asyncio.run(poe_pipeline._store_evidence(Broken(), {"mrgid": 8402}, docs, raw, used, logs.append)) == 0
    assert any(line.startswith("evidence: non stockée") for line in logs)


# --- committed official pages and sample mode end to end ----------------------------------
def test_committed_official_pages_are_well_formed_and_designate_their_ports():
    pages = sorted(evidence_store.SAMPLE_PAGES_DIR.glob("*.json"))
    assert len(pages) == 5
    for path in pages:
        page = json.loads(path.read_text(encoding="utf-8"))
        assert re.fullmatch(r"[0-9a-f]{64}", page["sha256"]) and page["chars"] > 0
        assert page["url"].startswith("https://") and page["fetched_at"].startswith("2026-")
        assert page["sections"] and all(isinstance(s["offset"], int) and s["text"] for s in page["sections"])
        assert sum(len(s["text"]) for s in page["sections"]) <= 4000  # small, relevant sections only
        for zone in page["zones"]:
            records = evidence_store.build_records(zone["mrgid"], page["designates"],
                                                   evidence_store.sample_documents(zone["mrgid"]))
            keys = {(r["port_key"], r["url"]) for r in records}
            for name in page["designates"]:
                assert (registry_check.port_key(name), page["url"]) in keys, (path.name, name)


def test_sample_mode_end_to_end_tier_counts(isolated):
    from app.adapters.zetaris import ZetarisClient
    from app.pipeline.run_pipeline import run_pipeline
    zones = sorted({p["zone_name"] for p in ZetarisClient(run_id="ev-zones").get_ports(limit=500)})
    tiers, evidence = collections.Counter(), {}

    async def run_all():
        for zone in zones:
            result = await run_pipeline(zone)
            for port in result["ports"]:
                tiers[port["registry"]["tier"]] += 1
                if port["registry"]["official_evidence"]:
                    evidence[port["name"]] = sorted({e["evidence_source"] for e in port["registry"]["official_evidence"]})

    asyncio.run(run_all())
    assert len(zones) == 20
    assert dict(tiers) == {TIER1: 2, TIER2: 2, TIER_REGISTERED: 11, "tier3": 5}
    assert evidence == {"Port of St. George’s": ["stored_snippet"], "Deep water Harbor": ["stored_snippet"],
                        "Conch Inn Marina": ["stored_snippet"], "Anchorage Small Boat Harbor": ["stored_snippet"]}


# --- run isolation (pilot dry-run finding) ---------------------------------------------

def _run_evidence_inputs():
    raw = f"[SOURCE: {GOV_BM}]\nAll yachts must clear customs at St. George's Harbour.\n"
    used = [{"url": GOV_BM, "sha256": "c" * 64, "collected_at": "2026-10-25T00:00:00+00:00",
             "level": "N1"}]
    docs = [{"name": "Port of St. George’s"}]
    return raw, used, docs


def test_run_evidence_goes_to_the_run_collection_never_poe_evidence():
    from types import SimpleNamespace

    from app.services import poe_pipeline
    db, logs = FakeDB(), []
    raw, used, docs = _run_evidence_inputs()
    run = SimpleNamespace(run_id="run-ev-1")
    stored = asyncio.run(poe_pipeline._store_evidence(
        db, {"mrgid": 8402}, docs, raw, used, logs.append, None, run=run))
    assert stored == 1
    assert evidence_store.COLLECTION not in db  # the shared collection is never opened
    rows = db[evidence_store.RUN_COLLECTION].docs
    assert [(r["run_id"], r["port_key"]) for r in rows] == [("run-ev-1", "saint george")]
    assert db[evidence_store.RUN_COLLECTION].indexes[0] == (
        [("run_id", 1), ("mrgid", 1), ("port_key", 1), ("url", 1)],
        {"unique": True, "name": "run_mrgid_port_key_url"})
    # A second run keeps its own copy; neither overwrites the other.
    asyncio.run(poe_pipeline._store_evidence(
        db, {"mrgid": 8402}, docs, raw, used, logs.append, None,
        run=SimpleNamespace(run_id="run-ev-2")))
    assert sorted(r["run_id"] for r in db[evidence_store.RUN_COLLECTION].docs) == [
        "run-ev-1", "run-ev-2"]
    # Without a run (live generation) the shared collection is used, as before.
    asyncio.run(poe_pipeline._store_evidence(db, {"mrgid": 8402}, docs, raw, used, logs.append))
    assert [r.get("run_id") for r in db[evidence_store.COLLECTION].docs] == [None]


def test_store_rejects_an_empty_run_id():
    import pytest
    raw, used, docs = _run_evidence_inputs()
    with pytest.raises(ValueError):
        asyncio.run(evidence_store.store(FakeDB(), [{"mrgid": 1, "port_key": "a", "url": "u"}],
                                         run_id=""))


def test_live_lookup_skips_records_that_carry_a_run_id(monkeypatch):
    seen = {}

    class Coll:
        def find(self, flt, projection=None):
            seen["filter"] = flt
            return self

        def limit(self, n):
            return []

    class Client:
        def __init__(self, *a, **k):
            pass

        def __getitem__(self, name):
            return {evidence_store.COLLECTION: Coll()}

        def close(self):
            pass

    import pymongo
    monkeypatch.setattr(pymongo, "MongoClient", Client)
    evidence_store._mongo_records(8402)
    assert seen["filter"] == {"mrgid": 8402, "run_id": {"$exists": False}}
