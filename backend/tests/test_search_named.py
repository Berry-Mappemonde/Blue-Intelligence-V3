"""Single door search_named — no network."""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import search as named  # noqa: E402
from app.core.extract import serp_filter  # noqa: E402
from app.services import capitainerie_enrich as ce  # noqa: E402
from app.services import marina_enrich as me  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_tavily_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


DDG_HTML = """
<html><body>
<div class="result results_links web-result">
  <a class="result__a" href="https://port.gouv.fr/capitainerie">Capitainerie officielle</a>
  <a class="result__snippet">Téléphone et VHF canal 9</a>
</div>
<div class="result result--ad">
  <a class="result__a" href="https://ads.example/x">Pub</a>
</div>
<div class="result results_links web-result">
  <a class="result__a" href="https://www.tripadvisor.com/Marina">Tripadvisor marina</a>
  <a class="result__snippet">avis</a>
</div>
<div class="result results_links web-result">
  <a class="result__a" href="/l/?uddg=https%3A%2F%2Fwww.larochelle.port.fr%2Fcontact">
    Port de La Rochelle
  </a>
</div>
</body></html>
"""


class _FakeResp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


class _FakeClient:
    last = None
    queue: list = []

    def __init__(self, *a, **k):
        _FakeClient.last = self
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        if _FakeClient.queue:
            return _FakeClient.queue.pop(0)
        return _FakeResp(200, DDG_HTML)

    async def get(self, url, **kwargs):
        self.gets.append({"url": url, **kwargs})
        if _FakeClient.queue:
            return _FakeClient.queue.pop(0)
        return _FakeResp(404, "")


class TestSerpTiktok:
    def test_tiktok_dropped_like_facebook(self):
        kept = [r["url"] for r in serp_filter([
            {"url": "https://www.tiktok.com/@marina"},
            {"url": "https://port.gouv.fr/contact"},
        ])]
        assert kept == ["https://port.gouv.fr/contact"]


class TestDomainHelpers:
    def test_url_matches_suffix_and_bare_gov(self):
        assert named.url_matches_domains(
            "https://www.douane.gouv.fr/ports", ["gouv.fr"]) is True
        assert named.url_matches_domains(
            "https://cbp.gov/ports", ["gov"]) is True
        assert named.url_matches_domains(
            "https://www.noonsite.com/x", ["gouv.fr"]) is False

    def test_ddg_query_adds_site_unless_already_present(self):
        q = named.ddg_query("Port de Nice clearance", ["douane.gouv.fr", "gov.fr"])
        assert "site:douane.gouv.fr" in q
        scoped = named.ddg_query('site:parc.fr visite "Cap de Creus"', ["parc.fr"])
        assert scoped.startswith("site:parc.fr")

    def test_ddg_kl_from_iso2(self):
        assert named.ddg_kl("FR") == "fr-fr"
        assert named.ddg_kl(None) == "wt-wt"


class TestDuckDuckGoParse:
    def test_unwraps_uddg_skips_ads(self):
        hits = named._parse_ddg_html(DDG_HTML, max_results=10)
        urls = [h["url"] for h in hits]
        assert "https://port.gouv.fr/capitainerie" in urls
        assert "https://www.larochelle.port.fr/contact" in urls
        assert "https://www.tripadvisor.com/Marina" in urls
        assert not any("ads.example" in u for u in urls)
        assert hits[0]["engine"] == "duckduckgo"
        assert "VHF" in hits[0]["snippet"]


class TestSearchNamed:
    def test_no_searxng_dependency(self):
        import app.core.search as mod
        assert not hasattr(mod, "searx_search")
        assert "app.core.serper" not in inspect.getsource(mod)
        assert "app.services.poe_pipeline" not in inspect.getsource(mod)

    def test_tinyfish_then_serp_filter_never_ddg(self, monkeypatch):
        ddg_calls = []

        async def fake_pages(query, key, **k):
            assert query == "Nouméa clearance"
            assert k.get("exclude_domains") == ("noonsite.com",)
            return [
                {"url": "https://www.tripadvisor.com/x", "title": "trip"},
                {"url": "https://douane.gouv.fr/noumea", "title": "Douane",
                 "snippet": "port d'entrée"},
            ]

        async def boom_ddg(*a, **k):
            ddg_calls.append(1)
            raise AssertionError("DDG must not run when TinyFish key exists")

        monkeypatch.setattr(named, "tf_search_pages", fake_pages)
        monkeypatch.setattr(named, "duckduckgo_html_search", boom_ddg)

        hits = _run(named.search_named(
            "Nouméa clearance", key="k", exclude_domains=("noonsite.com",)))
        assert [h["url"] for h in hits] == ["https://douane.gouv.fr/noumea"]
        assert hits[0]["engine"] == "tinyfish"
        assert ddg_calls == []

    def test_empty_tinyfish_does_not_fall_back_to_ddg(self, monkeypatch):
        ddg_calls = []

        async def empty_pages(*a, **k):
            return []

        async def boom_ddg(*a, **k):
            ddg_calls.append(1)
            return [{"url": "https://example.com"}]

        monkeypatch.setattr(named, "tf_search_pages", empty_pages)
        monkeypatch.setattr(named, "duckduckgo_html_search", boom_ddg)
        hits = _run(named.search_named("Port X", key="k"))
        assert hits == []
        assert ddg_calls == []

    def test_no_key_uses_ddg_and_filters(self, monkeypatch):
        tf_calls = []

        async def boom_tf(*a, **k):
            tf_calls.append(1)
            raise AssertionError("TinyFish must not run without key")

        async def fake_ddg(query, **k):
            assert "site:gouv.fr" in query or k.get("include_domains")
            return [
                {"url": "https://www.facebook.com/marina", "title": "fb"},
                {"url": "https://douane.gouv.fr/ports", "title": "Liste"},
                {"url": "https://blog.com/x", "title": "blog"},
            ]

        monkeypatch.setattr(named, "tf_search_pages", boom_tf)
        monkeypatch.setattr(named, "duckduckgo_html_search", fake_ddg)
        hits = _run(named.search_named(
            "ports d'entrée", key="", include_domains=["gouv.fr"]))
        assert tf_calls == []
        assert [h["url"] for h in hits] == ["https://douane.gouv.fr/ports"]
        assert hits[0]["engine"] == "duckduckgo"

    def test_exclude_domains_on_ddg(self, monkeypatch):
        async def fake_ddg(query, **k):
            return [
                {"url": "https://www.noonsite.com/pacific/niue", "title": "n"},
                {"url": "https://customs.gov.nu/ports", "title": "ok"},
            ]

        monkeypatch.setattr(named, "duckduckgo_html_search", fake_ddg)
        hits = _run(named.search_named(
            "Alofi clearance", key="", exclude_domains=("noonsite.com",)))
        assert [h["url"] for h in hits] == ["https://customs.gov.nu/ports"]

    def test_empty_query(self):
        assert _run(named.search_named("  ", key="k")) == []


class TestDuckDuckGoHttp:
    def test_post_then_get_fallback(self, monkeypatch):
        monkeypatch.setattr(named.httpx, "AsyncClient", _FakeClient)
        _FakeClient.queue = [_FakeResp(403, ""), _FakeResp(200, DDG_HTML)]
        hits = _run(named.duckduckgo_html_search("capitainerie minimes", max_results=5))
        urls = [h["url"] for h in hits]
        assert "https://port.gouv.fr/capitainerie" in urls
        assert _FakeClient.last.posts
        assert _FakeClient.last.gets


class TestCallersKeepOwnQueries:
    def test_capitainerie_uses_search_named_and_keeps_query(self, monkeypatch):
        seen = []

        async def fake_named(query, **k):
            seen.append({"query": query, **k})
            return [
                {"url": "https://www.tripadvisor.com/x"},
                {"url": "https://larochelle.port.fr/capitainerie"},
            ]

        monkeypatch.setattr(ce, "search_named", fake_named)
        urls = _run(ce.discover_contact_urls(
            {"name": "Capitainerie des Minimes", "lat": 46.15, "lon": -1.16,
             "tags": {}},
            tinyfish_key="k"))
        assert seen[0]["query"].startswith('"Capitainerie des Minimes"')
        assert seen[0]["purpose"] == ce.CAPITAINERIE_PURPOSE
        assert urls[0] == "https://larochelle.port.fr/capitainerie"
        assert all("tripadvisor" not in u for u in urls)

    def test_marina_query_stays_marina_not_poe(self):
        q = me.marina_site_query({"name": "Minimes", "lat": 46.15, "lon": -1.16})
        assert "Minimes" in q
        assert "marina" in q
        assert "46.1500" in q
        assert "port of entry" not in q.lower()
        assert "aire marine" not in q.lower()

    def test_resolve_marina_website_prefers_osm_tag(self, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("search must not run when OSM website exists")

        monkeypatch.setattr("app.core.search.search_named", boom)
        url = _run(me.resolve_marina_website({
            "name": "Minimes", "lat": 46.1, "lon": -1.1,
            "tags": {"website": "https://minimes.port.fr"},
        }, key="k"))
        assert url == "https://minimes.port.fr"

    def test_resolve_marina_website_searches_without_tag(self, monkeypatch):
        async def fake_named(query, **k):
            assert "Minimes" in query
            assert k.get("purpose") == me.MARINA_SITE_PURPOSE
            return [{"url": "https://minimes.port.fr", "engine": "tinyfish"}]

        monkeypatch.setattr("app.core.search.search_named", fake_named)
        url = _run(me.resolve_marina_website({
            "name": "Minimes", "lat": 46.1, "lon": -1.1, "tags": {},
        }, key="k"))
        assert url == "https://minimes.port.fr"

    def test_capitainerie_official_site_skips_search(self, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("search must not run when official website exists")

        monkeypatch.setattr(ce, "search_named", boom)
        urls = _run(ce.discover_contact_urls({
            "name": "Capitainerie des Minimes", "lat": 46.15, "lon": -1.16,
            "website": "https://larochelle.port.fr/capitainerie",
            "tags": {},
        }, tinyfish_key="k"))
        assert urls == ["https://larochelle.port.fr/capitainerie"]
