"""Tavily Search / Extract — mocked httpx, no network."""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import tavily as tv  # noqa: E402

_KEY = "tvly-test-not-a-secret"


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


class _FakeClient:
    posts: list = []
    queue: list = []
    default = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        _FakeClient.posts.append({
            "url": url,
            "json": kwargs.get("json") or {},
            "headers": dict(kwargs.get("headers") or {}),
        })
        if _FakeClient.queue:
            return _FakeClient.queue.pop(0)
        if _FakeClient.default is not None:
            return _FakeClient.default
        raise AssertionError("unexpected Tavily HTTP")


class _BoomClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        raise httpx.ConnectTimeout("timed out")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(tv, "RETRY_SLEEP_S", 0)
    monkeypatch.setattr(tv.httpx, "AsyncClient", _FakeClient)
    tv.CREDITS["search"] = 0
    tv.CREDITS["extract"] = 0
    _FakeClient.posts = []
    _FakeClient.queue = []
    _FakeClient.default = None
    yield


def _use_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", _KEY)


class TestCostsAndKey:
    def test_credit_table(self):
        assert tv.search_credit_cost("basic") == 1
        assert tv.search_credit_cost("fast") == 1
        assert tv.search_credit_cost("ultra-fast") == 1
        assert tv.search_credit_cost("advanced") == 2
        assert tv.extract_credit_cost(0) == 0
        assert tv.extract_credit_cost(1) == 1
        assert tv.extract_credit_cost(5) == 1
        assert tv.extract_credit_cost(6) == 2
        assert tv.extract_credit_cost(20) == 4

    def test_key_absent(self):
        assert tv.tavily_api_key() == ""

    def test_default_cache_dir(self, monkeypatch):
        monkeypatch.delenv("TAVILY_CACHE_DIR", raising=False)
        assert tv.cache_dir().as_posix().endswith("backend/data/cache/tavily")

    def test_cache_id_ignores_api_key(self):
        body = {"query": "ports", "max_results": 5, "search_depth": "basic"}
        plain = tv.request_cache_id("search", body)
        leaked = tv.request_cache_id("search", {**body, "api_key": _KEY})
        assert plain == leaked
        assert len(plain) == 64


class TestSearch:
    def test_noop_without_key(self, tmp_path):
        assert _run(tv.tavily_search("ports of entry")) == []
        assert _FakeClient.posts == []
        assert tv.CREDITS["search"] == 0
        assert list(tmp_path.glob("*.json")) == []

    def test_maps_results_and_keeps_url(self, monkeypatch):
        _use_key(monkeypatch)
        _FakeClient.default = _Resp(200, {
            "results": [
                {"title": "Douane", "url": "https://www.douane.gouv.fr/ports",
                 "content": "liste des ports", "score": 0.9},
                {"title": "no url", "content": "x"},
                {"title": "bad", "url": "not-a-url"},
            ],
            "api_key": _KEY,
        })
        hits = _run(tv.tavily_search("ports", max_results=5, include_domains=["gouv.fr"]))
        assert len(hits) == 1
        assert hits[0]["url"] == "https://www.douane.gouv.fr/ports"
        assert hits[0]["engine"] == "tavily"
        assert hits[0]["domain"] == "douane.gouv.fr"
        assert hits[0]["snippet"] == "liste des ports"
        assert hits[0]["score"] == 0.9
        assert tv.CREDITS["search"] == 1
        post = _FakeClient.posts[0]
        assert post["url"] == tv.SEARCH_URL
        assert post["headers"]["Authorization"] == f"Bearer {_KEY}"
        assert "api_key" not in post["json"]
        assert post["json"]["search_depth"] == "basic"
        assert post["json"]["max_results"] == 5
        cached = list(Path(tv.cache_dir()).glob("*.json"))
        assert len(cached) == 1
        blob = cached[0].read_text(encoding="utf-8")
        assert _KEY not in blob
        assert cached[0].stem == tv.request_cache_id("search", post["json"])

    def test_advanced_costs_two(self, monkeypatch):
        _use_key(monkeypatch)
        _FakeClient.default = _Resp(200, {"results": [
            {"url": "https://example.org/a", "title": "A", "content": "a"},
        ]})
        hits = _run(tv.tavily_search("q", search_depth="advanced"))
        assert hits[0]["url"] == "https://example.org/a"
        assert tv.CREDITS["search"] == 2
        assert _FakeClient.posts[0]["json"]["search_depth"] == "advanced"

    def test_cache_hit_is_free(self, monkeypatch):
        _use_key(monkeypatch)
        _FakeClient.default = _Resp(200, {"results": [
            {"url": "https://example.org/a", "title": "A", "content": "a"},
        ]})
        assert _run(tv.tavily_search("ports", max_results=5))
        assert _run(tv.tavily_search("ports", max_results=5))
        assert len(_FakeClient.posts) == 1
        assert tv.CREDITS["search"] == 1

    def test_401_is_empty_and_not_cached(self, monkeypatch, tmp_path):
        _use_key(monkeypatch)
        _FakeClient.queue = [_Resp(401, {"error": "unauthorized"})]
        assert _run(tv.tavily_search("ports")) == []
        assert tv.CREDITS["search"] == 0
        assert list(tmp_path.glob("*.json")) == []

    def test_429_retries_once(self, monkeypatch):
        _use_key(monkeypatch)
        _FakeClient.queue = [
            _Resp(429, {}),
            _Resp(200, {"results": [
                {"url": "https://sis.gov.eg/yacht", "title": "Yacht", "content": "marina"},
            ]}),
        ]
        hits = _run(tv.tavily_search("Egypt"))
        assert hits[0]["url"] == "https://sis.gov.eg/yacht"
        assert len(_FakeClient.posts) == 2
        assert tv.CREDITS["search"] == 1

    def test_timeout_returns_empty(self, monkeypatch):
        _use_key(monkeypatch)
        monkeypatch.setattr(tv.httpx, "AsyncClient", _BoomClient)
        assert _run(tv.tavily_search("ports")) == []
        assert tv.CREDITS["search"] == 0


class TestExtract:
    def test_noop_without_key_or_urls(self, monkeypatch):
        assert _run(tv.tavily_extract(["https://example.org/a"])) == []
        assert _FakeClient.posts == []
        _use_key(monkeypatch)
        assert _run(tv.tavily_extract([])) == []
        assert _run(tv.tavily_extract(["not-a-url"])) == []
        assert _FakeClient.posts == []
        assert tv.CREDITS["extract"] == 0

    def test_keeps_source_url_and_bills_per_five(self, monkeypatch):
        _use_key(monkeypatch)
        urls = [f"https://example.org/p{i}" for i in range(6)]
        _FakeClient.default = _Resp(200, {"results": [
            {"url": u, "raw_content": f"texte {u}"} for u in urls
        ]})
        rows = _run(tv.tavily_extract(urls + ["ftp://skip", urls[0]]))
        assert [r["url"] for r in rows] == urls
        assert all(r["raw_content"].startswith("texte ") for r in rows)
        assert tv.CREDITS["extract"] == 2
        assert _FakeClient.posts[0]["url"] == tv.EXTRACT_URL
        assert _FakeClient.posts[0]["json"]["urls"] == urls
        assert "api_key" not in _FakeClient.posts[0]["json"]
        assert _FakeClient.posts[0]["headers"]["Authorization"] == f"Bearer {_KEY}"

    def test_extract_cache_hit_is_free(self, monkeypatch):
        _use_key(monkeypatch)
        _FakeClient.default = _Resp(200, {"results": [
            {"url": "https://example.org/a", "raw_content": "x" * 20},
        ]})
        assert _run(tv.tavily_extract(["https://example.org/a"]))
        assert _run(tv.tavily_extract(["https://example.org/a"]))
        assert len(_FakeClient.posts) == 1
        assert tv.CREDITS["extract"] == 1


class TestCascade:
    def test_search_named_uses_tavily_before_ddg(self, monkeypatch):
        _use_key(monkeypatch)
        from app.core import search as named
        _FakeClient.default = _Resp(200, {"results": [{
            "url": "https://douane.gouv.fr/ports",
            "title": "Liste",
            "content": "ports d'entrée",
        }]})

        async def boom_ddg(*a, **k):
            raise AssertionError("DDG must not run when Tavily returned hits")

        monkeypatch.setattr(named, "duckduckgo_html_search", boom_ddg)
        hits = _run(named.search_named("ports of entry", key=""))
        assert [h["url"] for h in hits] == ["https://douane.gouv.fr/ports"]
        assert hits[0]["engine"] == "tavily"

    def test_search_named_alongside_tinyfish(self, monkeypatch):
        _use_key(monkeypatch)
        from app.core import search as named
        _FakeClient.default = _Resp(200, {"results": [{
            "url": "https://douane.gouv.fr/b",
            "title": "B",
            "content": "b",
        }]})

        async def fake_pages(*a, **k):
            return [{"url": "https://douane.gouv.fr/a", "title": "A", "engine": "tinyfish"}]

        async def boom_ddg(*a, **k):
            raise AssertionError("DDG")

        monkeypatch.setattr(named, "tf_search_pages", fake_pages)
        monkeypatch.setattr(named, "duckduckgo_html_search", boom_ddg)
        hits = _run(named.search_named("ports", key="tf-test"))
        assert [h["url"] for h in hits] == [
            "https://douane.gouv.fr/a", "https://douane.gouv.fr/b"]
        assert hits[0]["engine"] == "tinyfish"
        assert hits[1]["engine"] == "tavily"

    def test_swarm_discover_keeps_tavily_url(self, monkeypatch):
        _use_key(monkeypatch)
        monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        from app.services.swarm_pipeline import Swarm
        _FakeClient.default = _Resp(200, {"results": [{
            "url": "https://example.org/projects/coral-restore",
            "title": "Coral",
            "content": "reef",
        }]})
        sw = Swarm(None)
        sw.settings = {}
        seed = {"name": "Example Ocean", "url": "https://example.org/projects/"}
        urls, n_tf, n_sp = _run(sw._search_discover(seed, 6))
        assert urls == ["https://example.org/projects/coral-restore"]
        assert n_tf == 0 and n_sp == 0
        assert sw._last_tavily_n == 1
        assert _FakeClient.posts[0]["url"] == tv.SEARCH_URL
        assert _FakeClient.posts[0]["json"]["include_domains"] == ["example.org"]
        assert "api_key" not in _FakeClient.posts[0]["json"]

    def test_swarm_without_key_does_not_call(self):
        from app.services.swarm_pipeline import Swarm
        sw = Swarm(None)
        sw.settings = {}
        seed = {"name": "Example Ocean", "url": "https://example.org/projects/"}
        urls, n_tf, n_sp = _run(sw._search_discover(seed, 6))
        assert urls == []
        assert (n_tf, n_sp, sw._last_tavily_n) == (0, 0, 0)
        assert _FakeClient.posts == []

    def test_find_sources_merges_tavily_url(self, monkeypatch):
        _use_key(monkeypatch)
        from app.services import poe_pipeline as poe
        page = "https://www.douane.gouv.fr/demarche/ports-entree"
        _FakeClient.default = _Resp(200, {"results": [{
            "url": page, "title": "Ports", "content": "liste officielle",
        }]})

        async def empty(*a, **k):
            return []

        async def no_grounded(*a, **k):
            raise AssertionError("grounded search must not run once Tavily has a source")

        monkeypatch.setattr(poe, "search_searxng", empty)
        monkeypatch.setattr(poe, "_tf_search_safe", empty)
        monkeypatch.setattr(poe, "_serper_search_safe", empty)
        monkeypatch.setattr(poe, "search_grounded", no_grounded)
        monkeypatch.setattr(poe, "search_hint_queries", lambda *a, **k: [])
        monkeypatch.setattr(poe, "save_exceptions", lambda exc: None)
        zone = {
            "name": "France", "geoname": "France", "iso2": "FR",
            "sov_iso2": "FR", "sovereign": "France",
        }
        official, strict, _syn = _run(poe._find_sources(
            zone, poe.build_whitelist("FR", "FR"), {}, lambda m: None, tf_key=""))
        matched = [c for c in official if c.get("url") == page]
        assert matched
        engine = matched[0].get("engine")
        assert engine == "tavily" or "tavily" in (engine or [])
        assert strict is True
        assert _FakeClient.posts
        assert all(p["url"] == tv.SEARCH_URL for p in _FakeClient.posts)
        assert all(p["headers"]["Authorization"] == f"Bearer {_KEY}" for p in _FakeClient.posts)

    def test_extract_cascade_keeps_source_url(self, monkeypatch):
        _use_key(monkeypatch)
        monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
        from app.core import extract as ext
        page_url = "https://www.douane.gouv.fr/demarche/ports-entree"
        text = "Official list of ports of entry for pleasure craft. " * 12
        _FakeClient.default = _Resp(200, {"results": [{
            "url": page_url, "raw_content": text,
        }]})

        async def boom_fetch(url, timeout=25):
            raise RuntimeError("offline")

        async def no_mirror(*a, **k):
            return None

        monkeypatch.setattr(ext, "fetch_raw", boom_fetch)
        monkeypatch.setattr(ext, "_jina_mirror_text", no_mirror)
        monkeypatch.setattr(ext, "_wayback_mirror_text", no_mirror)
        monkeypatch.setattr(ext, "_jorf_reader_text", no_mirror)
        page = _run(ext.extract_cascade(page_url, allow_render=False))
        assert page["url"] == page_url
        assert page["final_url"] == page_url
        assert page["level"] == "N3-mirror-tavily"
        assert "ports of entry" in page["text"]
        assert _FakeClient.posts[0]["url"] == tv.EXTRACT_URL
        assert page_url in _FakeClient.posts[0]["json"]["urls"]
        assert tv.CREDITS["extract"] == 1

    def test_mirror_skips_tavily_without_key(self, monkeypatch):
        from app.core import extract as ext
        called = []

        async def boom_extract(*a, **k):
            called.append(1)
            raise AssertionError("extract must not run without TAVILY_API_KEY")

        async def no_mirror(*a, **k):
            return None

        monkeypatch.setattr(tv, "tavily_extract", boom_extract)
        monkeypatch.setattr(ext, "_jina_mirror_text", no_mirror)
        monkeypatch.setattr(ext, "_wayback_mirror_text", no_mirror)
        monkeypatch.setattr(ext, "_tinyfish_mirror_text", no_mirror)
        assert _run(ext.fetch_mirror_text("https://www.douane.gouv.fr/x")) is None
        assert called == []


class TestCacheHygiene:
    """Review PR #8 finding 1: empty / failed answers must not be frozen."""

    @pytest.fixture(autouse=True)
    def _stats(self):
        tv.reset_stats()
        yield
        tv.reset_stats()

    def test_empty_search_is_not_cached(self, monkeypatch, tmp_path):
        _use_key(monkeypatch)
        _FakeClient.queue = [
            _Resp(200, {"results": []}),
            _Resp(200, {"results": [
                {"url": "https://douane.gouv.fr/ports", "title": "P", "content": "p"},
            ]}),
        ]
        assert _run(tv.tavily_search("ports")) == []
        assert list(tmp_path.glob("*.json")) == []
        hits = _run(tv.tavily_search("ports"))
        assert [h["url"] for h in hits] == ["https://douane.gouv.fr/ports"]
        assert len(_FakeClient.posts) == 2

    def test_old_empty_entry_is_a_miss(self, monkeypatch, tmp_path):
        _use_key(monkeypatch)
        body, _ = tv._search_body("ports", 10, "basic", None, None)
        (tmp_path / f"{tv.request_cache_id('search', body)}.json").write_text(
            json.dumps({"results": []}), encoding="utf-8")
        _FakeClient.default = _Resp(200, {"results": [
            {"url": "https://douane.gouv.fr/ports", "title": "P", "content": "p"},
        ]})
        assert _run(tv.tavily_search("ports"))
        assert len(_FakeClient.posts) == 1

    def test_failed_extract_url_is_retried(self, monkeypatch, tmp_path):
        _use_key(monkeypatch)
        ok, bad = "https://douane.gouv.fr/ok", "https://douane.gouv.fr/bad"
        _FakeClient.queue = [
            _Resp(200, {"results": [{"url": ok, "raw_content": "read"}],
                        "failed_results": [{"url": bad, "error": "timeout"}]}),
            _Resp(200, {"results": [{"url": bad, "raw_content": "now read"}]}),
        ]
        rows = _run(tv.tavily_extract([ok, bad]))
        assert [r["url"] for r in rows] == [ok]
        for f in tmp_path.glob("*.json"):
            blob = f.read_text(encoding="utf-8")
            assert bad not in blob and "failed_results" not in blob
        rows = _run(tv.tavily_extract([ok, bad]))
        assert {r["url"] for r in rows} == {ok, bad}
        # second call sends only the URL that failed; the read one is a cache hit
        assert _FakeClient.posts[1]["json"]["urls"] == [bad]

    def test_ttl_expires_entries(self, monkeypatch):
        _use_key(monkeypatch)
        _FakeClient.default = _Resp(200, {"results": [
            {"url": "https://example.org/a", "title": "A", "content": "a"},
        ]})
        now = [1_000_000.0]
        monkeypatch.setattr(tv.time, "time", lambda: now[0])
        assert _run(tv.tavily_search("ports"))
        now[0] += 6 * 86400
        assert _run(tv.tavily_search("ports"))
        assert len(_FakeClient.posts) == 1
        now[0] += 2 * 86400  # 8 days > default 7
        assert _run(tv.tavily_search("ports"))
        assert len(_FakeClient.posts) == 2

    def test_ttl_is_a_run_rule(self, monkeypatch):
        from app.core.run_rules import bind_rules, get_rule_def, reset_rules
        rule = get_rule_def("tavily.cache_ttl_days")
        assert rule["value"] == 7 and rule["kind"] == "budget"
        token = bind_rules({"chosen": {"tavily.cache_ttl_days": {"value": 0}}})
        try:
            assert tv.cache_ttl_days() == 0
            _use_key(monkeypatch)
            _FakeClient.default = _Resp(200, {"results": [
                {"url": "https://example.org/a", "title": "A", "content": "a"},
            ]})
            _run(tv.tavily_search("ports"))
            _run(tv.tavily_search("ports"))
            assert len(_FakeClient.posts) == 2  # 0 days = cache not read
        finally:
            reset_rules(token)
        assert tv.cache_ttl_days() == 7

    def test_hit_ratio_recorded_per_run(self, monkeypatch):
        from app.core.tokenfactory import bind_run
        _use_key(monkeypatch)
        _FakeClient.default = _Resp(200, {"results": [
            {"url": "https://example.org/a", "title": "A", "content": "a"},
        ]})

        async def two_searches(rid):
            bind_run(rid)
            await tv.tavily_search("ports")
            await tv.tavily_search("ports")

        _run(two_searches("run-tv-a"))
        _run(two_searches("run-tv-b"))
        a, b = tv.summary("run-tv-a"), tv.summary("run-tv-b")
        assert (a["cache_hits"], a["cache_misses"], a["cache_hit_ratio"]) == (1, 1, 0.5)
        assert (b["cache_hits"], b["cache_misses"], b["cache_hit_ratio"]) == (2, 0, 1.0)
        doc = tv.attach({"run_id": "run-tv-a"}, "run-tv-a")
        assert doc["tavily"]["cache_hit_ratio"] == 0.5


class TestCreditBudget:
    """Review PR #8 finding 2: credit cap per run, semaphore, credits stored with the run."""

    @pytest.fixture(autouse=True)
    def _stats(self):
        tv.reset_stats()
        yield
        tv.reset_stats()

    @staticmethod
    def _cap(n):
        from app.core.run_rules import bind_rules
        return bind_rules({"chosen": {"tavily.max_credits": {"value": n}}})

    def test_max_credits_is_a_run_rule(self):
        from app.core.run_rules import get_rule_def, reset_rules
        rule = get_rule_def("tavily.max_credits")
        assert rule["kind"] == "budget" and rule["value"] == tv.DEFAULT_MAX_CREDITS
        token = self._cap(3)
        try:
            assert tv.max_credits() == 3
        finally:
            reset_rules(token)

    def test_cap_stops_calls_and_is_recorded(self, monkeypatch):
        from app.core.run_rules import reset_rules
        from app.core.tokenfactory import bind_run
        _use_key(monkeypatch)
        _FakeClient.default = _Resp(200, {"results": [
            {"url": "https://example.org/a", "title": "A", "content": "a"},
        ]})
        token = self._cap(3)

        async def go():
            bind_run("run-cap")
            a = await tv.tavily_search("q1")                          # 1
            b = await tv.tavily_search("q2", search_depth="advanced")  # 2 -> 3
            c = await tv.tavily_search("q3")                          # 4 > 3: refused
            d = await tv.tavily_search("q1")                          # cache hit: free
            return a, b, c, d

        try:
            a, b, c, d = _run(go())
        finally:
            reset_rules(token)
        assert a and b and d and c == []
        assert len(_FakeClient.posts) == 2
        s = tv.summary("run-cap")
        assert s["credits"] == {"search": 3, "extract": 0, "total": 3}
        assert s["calls_capped"] == 1 and s["budget_reached"] is True
        assert tv.summary("other-run")["credits"]["total"] == 0

    def test_failed_call_gives_credits_back(self, monkeypatch):
        from app.core.tokenfactory import bind_run
        _use_key(monkeypatch)
        _FakeClient.queue = [_Resp(500, {})]

        async def go():
            bind_run("run-fail")
            return await tv.tavily_search("q")

        assert _run(go()) == []
        assert tv.summary("run-fail")["credits"]["total"] == 0

    def test_extract_bills_urls_read(self, monkeypatch):
        from app.core.tokenfactory import bind_run
        _use_key(monkeypatch)
        urls = [f"https://example.org/p{i}" for i in range(7)]
        _FakeClient.default = _Resp(200, {
            "results": [{"url": u, "raw_content": "t"} for u in urls[:5]],
            "failed_results": [{"url": u} for u in urls[5:]],
        })

        async def go():
            bind_run("run-ex")
            return await tv.tavily_extract(urls)

        assert len(_run(go())) == 5
        # 7 sent would reserve 2 credits; 5 read bill 1
        assert tv.summary("run-ex")["credits"]["extract"] == 1

    def test_semaphore_bounds_concurrent_calls(self, monkeypatch):
        _use_key(monkeypatch)
        state = {"now": 0, "peak": 0}

        class _SlowClient(_FakeClient):
            async def post(self, url, **kwargs):
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
                await asyncio.sleep(0.01)
                state["now"] -= 1
                return _Resp(200, {"results": [
                    {"url": "https://example.org/a", "title": "A", "content": "a"}]})

        monkeypatch.setattr(tv.httpx, "AsyncClient", _SlowClient)

        async def go():
            return await asyncio.gather(*(tv.tavily_search(f"q{i}") for i in range(12)))

        assert all(_run(go()))
        assert 1 <= state["peak"] <= tv.MAX_CONCURRENT < 12

    def test_restore_keeps_cap_across_resume(self, monkeypatch):
        from app.core.run_rules import reset_rules
        from app.core.tokenfactory import bind_run
        _use_key(monkeypatch)
        _FakeClient.default = _Resp(200, {"results": [
            {"url": "https://example.org/a", "title": "A", "content": "a"}]})
        stored = {"credits": {"search": 4, "extract": 1, "total": 5},
                  "cache_hits": 2, "cache_misses": 5}
        tv.restore(stored, "run-resume")
        token = self._cap(5)

        async def go():
            bind_run("run-resume")
            return await tv.tavily_search("q")

        try:
            assert _run(go()) == []
        finally:
            reset_rules(token)
        assert _FakeClient.posts == []
        s = tv.summary("run-resume")
        assert s["credits"]["total"] == 5 and s["cache_misses"] == 6
