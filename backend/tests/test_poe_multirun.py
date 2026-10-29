"""v1/v2/tinyfish variants, best-of synthesis, SearXNG health helpers.

No network. Dedicated Mongo for best-of (bi_test_poe_bestof).
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import poe_pipeline as poe  # noqa: E402
from app.services.poe_bestof import (  # noqa: E402
    is_legal_fragment, synthesize_zone, synthesize_best_of,
)
from app.core.extract import allow_tinyfish_fetch  # noqa: E402


@pytest.fixture(autouse=True)
def _no_tavily_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


class TestNormalizeVariant:
    def test_aliases(self):
        assert poe.normalize_variant("v1") == "v1"
        assert poe.normalize_variant("searx_only") == "v2"
        assert poe.normalize_variant("tf") == "tinyfish"
        assert poe.normalize_variant(None) == "tinyfish"

    def test_unknown(self):
        with pytest.raises(ValueError):
            poe.normalize_variant("swarm")


class TestFindSourcesVariants:
    zone = {
        "name": "France", "geoname": "France", "iso2": "FR", "sov_iso2": "FR",
        "sovereign": "France",
    }

    def _run(self, monkeypatch, searx_hits, tf_hits, variant, searx_by_query=None):
        searx_calls = []
        tf_calls = []

        async def fake_searx(query, log):
            searx_calls.append(query)
            if searx_by_query is not None:
                return list(searx_by_query(query))
            return list(searx_hits)

        async def fake_tf(query, key, log, location=None, language=None,
                          include_domains=None):
            tf_calls.append({"query": query, "scoped": bool(include_domains)})
            return list(tf_hits)

        async def fake_grounded(zone, whitelist, log, query_override=None):
            return [], None

        async def fake_serper(query, key, log, gl=None, hl="en"):
            return []

        monkeypatch.setattr(poe, "search_searxng", fake_searx)
        monkeypatch.setattr(poe, "_tf_search_safe", fake_tf)
        monkeypatch.setattr(poe, "_serper_search_safe", fake_serper)
        monkeypatch.setattr(poe, "search_grounded", fake_grounded)
        monkeypatch.setattr(poe, "search_hint_queries", lambda zone, exceptions=None: [])
        monkeypatch.setattr(poe, "save_exceptions", lambda exc: None)
        official, strict, syn = asyncio.run(poe._find_sources(
            self.zone, poe.build_whitelist("FR", "FR"), {}, lambda m: None,
            tf_key="test-key", variant=variant))
        return official, strict, syn, searx_calls, tf_calls

    def test_v2_never_calls_tinyfish(self, monkeypatch):
        hits = [{"url": "https://www.douane.gouv.fr/x", "domain": "douane.gouv.fr",
                 "engine": "searxng"}]
        _o, _s, _syn, searx_calls, tf_calls = self._run(
            monkeypatch, hits, hits, "v2")
        assert tf_calls == []
        assert len(searx_calls) >= 1

    def test_v1_skips_localized_when_en_hits(self, monkeypatch):
        hits = [{"url": "https://www.douane.gouv.fr/x", "domain": "douane.gouv.fr",
                 "engine": "searxng"}]
        _o, _s, _syn, searx_calls, tf_calls = self._run(
            monkeypatch, hits, hits, "v1")
        assert tf_calls == []
        assert len(searx_calls) >= 1
        assert "official designated" in searx_calls[0]

    def test_v1_falls_back_to_localized(self, monkeypatch):
        def by_q(query):
            if "official designated" in query:
                return []
            return [{"url": "https://www.douane.gouv.fr/fr", "domain": "douane.gouv.fr"}]

        _o, _s, _syn, searx_calls, tf_calls = self._run(
            monkeypatch, [], [], "v1", searx_by_query=by_q)
        assert tf_calls == []
        assert len(searx_calls) >= 2

    def test_tinyfish_still_calls_tf(self, monkeypatch):
        tf_hits = [{"url": "https://www.douane.gouv.fr/x", "domain": "douane.gouv.fr",
                    "engine": "tinyfish"}]
        _o, _s, _syn, _searx, tf_calls = self._run(
            monkeypatch, [], tf_hits, "tinyfish")
        assert tf_calls
        assert any(not c["scoped"] for c in tf_calls)


class TestLegalAndBestOf:
    def test_legal_fragments(self):
        assert is_legal_fragment({"name": "État", "note": None})
        assert is_legal_fragment({"name": "Calais", "note": "tournure légale (« port of »)"})
        assert is_legal_fragment({"name": "Derby consists of", "note": None})
        assert not is_legal_fragment({"name": "Calais", "note": "PPF"})

    def test_keep_v1_drop_legal(self):
        v1 = [{"name": "Calais", "lat": 50.9, "lon": 1.8, "validated": True,
               "source_urls": ["https://douane.gouv.fr/a"], "note": "PPF"}]
        tf = [
            {"name": "Calais", "lat": 50.9, "lon": 1.8, "validated": True,
             "source_urls": ["https://mer.gouv.fr/b"]},
            {"name": "État", "lat": 49.4, "lon": 0.1, "validated": True,
             "note": "tournure légale (« port of »)"},
        ]
        out = synthesize_zone({"v1": v1, "tinyfish": tf})
        names = {p["name"] for p in out}
        assert names == {"Calais"}
        calais = out[0]
        assert calais["multi_run"] is True
        assert "https://mer.gouv.fr/b" in calais["source_urls"]

    def test_wipe_keeps_v1(self):
        v1 = [{"name": "Zeebrugge", "lat": 51.3, "lon": 3.2, "validated": True}]
        out = synthesize_zone({"v1": v1, "tinyfish": []})
        assert [p["name"] for p in out] == ["Zeebrugge"]

    def test_alias_porto_de(self):
        v1 = [{"name": "Porto de Santos", "lat": -23.9, "lon": -46.3, "validated": True}]
        tf = [{"name": "Santos", "lat": -23.9, "lon": -46.3, "validated": True}]
        out = synthesize_zone({"v1": v1, "tinyfish": tf})
        assert len(out) == 1
        assert out[0]["multi_run"] is True

    def test_tf_fill_empty_v1(self):
        tf = [{"name": "Haifa Port", "lat": 32.8, "lon": 35.0, "validated": True}]
        out = synthesize_zone({"v1": [], "tinyfish": tf})
        assert [p["name"] for p in out] == ["Haifa Port"]


@pytest.fixture
def bestof_db():
    from motor.motor_asyncio import AsyncIOMotorClient

    async def _seed():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        tdb = client["bi_test_poe_bestof"]
        await client.drop_database("bi_test_poe_bestof")
        await tdb.poe_ports.insert_one({
            "mrgid": 1, "zone_name": "Z", "name": "Alpha", "lat": 1.0, "lon": 2.0,
            "validated": True, "source_urls": ["https://gov.tl/a"],
            "dedup_key": "1:alpha",
        })
        await tdb.poe_runs.insert_many([
            {"_id": "r-tf", "label": "tf", "params": {"variant": "tinyfish"}, "state": "done"},
            {"_id": "r-v2", "label": "v2", "params": {"variant": "v2"}, "state": "done"},
        ])
        await tdb.poe_run_ports.insert_many([
            {"run_id": "r-tf", "mrgid": 1, "zone_name": "Z", "name": "État",
             "lat": 9.0, "lon": 9.0, "validated": True,
             "note": "tournure légale (« port of »)", "source_urls": []},
            {"run_id": "r-tf", "mrgid": 1, "zone_name": "Z", "name": "Beta",
             "lat": 3.0, "lon": 4.0, "validated": True,
             "source_urls": ["https://gov.tl/b"]},
            {"run_id": "r-v2", "mrgid": 1, "zone_name": "Z", "name": "Alpha",
             "lat": 1.0, "lon": 2.0, "validated": True,
             "source_urls": ["https://gov.tl/a2"]},
        ])
        return tdb

    loop = asyncio.new_event_loop()
    tdb = loop.run_until_complete(_seed())
    yield tdb, loop
    loop.run_until_complete(tdb.client.drop_database("bi_test_poe_bestof"))
    loop.close()


class TestBestOfPersist:
    def test_writes_run_not_v1(self, bestof_db):
        tdb, loop = bestof_db
        before = loop.run_until_complete(tdb.poe_ports.count_documents({}))
        summary = loop.run_until_complete(synthesize_best_of(
            tdb, ["r-tf", "r-v2"], include_v1=True, dest_run_id="r-best",
            label="best-of-test"))
        after = loop.run_until_complete(tdb.poe_ports.count_documents({}))
        assert after == before == 1
        assert summary["ports_total"] >= 2
        names = loop.run_until_complete(tdb.poe_run_ports.distinct(
            "name", {"run_id": "r-best"}))
        assert "Alpha" in names
        assert "Beta" in names
        assert "État" not in names
        run = loop.run_until_complete(tdb.poe_runs.find_one({"_id": "r-best"}))
        assert run["params"]["variant"] == "bestof"
        assert run["params"]["code"]["git_sha"]
        assert run["synthetic"] is True


class TestTinyfishFetchFlag:
    def test_contextvar_default_on(self):
        assert allow_tinyfish_fetch.get() is True

    def test_can_disable(self):
        tok = allow_tinyfish_fetch.set(False)
        try:
            assert allow_tinyfish_fetch.get() is False
        finally:
            allow_tinyfish_fetch.reset(tok)
        assert allow_tinyfish_fetch.get() is True
