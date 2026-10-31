"""Projects discovery: N1 motifs → Fetch → Search TF ∥ Serper (1 shot). No network."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "bi_test_project_discover_search")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.core.tinyfish import POE_PURPOSE, PROJECTS_DISCOVERY_PURPOSE, PROJECTS_LISTING_PURPOSE
from app.services import run_journal
from app.services.swarm_pipeline import (
    Swarm, _is_soft_fiche_path, filter_discover_urls, is_project_fiche_path,
    official_site_from_hits, project_search_query,
)
from tests.test_project_runs import _FakeDB

SEED = {
    "name": "Example Ocean",
    "url": "https://example.org/projects/",
    "listing_kind": "projects_index",
}
HOME_HTML = """
<html><body>
<a href="/about">About</a>
<a href="/contact">Contact</a>
<a href="/news/hello">News</a>
<a href="/donate">Donate</a>
</body></html>
"""
FICHE_HTML = """
<html><body>
<a href="/projects/coral-restore">Coral</a>
<a href="/about">About</a>
</body></html>
"""


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_paid_env(monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(
        "app.services.swarm_pipeline.partner_site_reachable",
        lambda url, timeout=5.0: True,
    )


def _swarm(settings=None):
    sw = Swarm(_FakeDB())
    sw.running = True
    sw.force_rescan = True
    sw.queue = asyncio.Queue()
    sw.settings = {
        "tinyfish_api_key": "tf-test",
        "serper_api_key": "sp-test",
        "allow_tinyfish_agent": True,
        "follow_the_money": True,
        "max_partner_orgs": 5,
        **(settings or {}),
    }
    return sw


async def _queued(sw, seed=SEED, max_urls=6):
    await sw._discover(seed, max_urls)
    await asyncio.sleep(0)
    items = []
    while not sw.queue.empty():
        items.append(sw.queue.get_nowait())
        sw.queue.task_done()
    return items


class _HtmlClient:
    html = HOME_HTML

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        class _Resp:
            text = _HtmlClient.html
            status_code = 200
            headers = {}
        return _Resp()


def _install_offline_html(monkeypatch):
    """The crawler checks the host, then GETs with follow_redirects=False.

    These tests stay offline: DNS answers a public address and the HTML
    client ignores the extra keyword.
    """
    import socket

    import app.services.swarm_pipeline as sp

    def _public(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(sp.httpx, "AsyncClient", _HtmlClient)
    monkeypatch.setattr("app.core.public_url.socket.getaddrinfo", _public)


def test_project_search_query_site_and_name():
    assert project_search_query(SEED) == "site:example.org Example Ocean ocean project"
    assert project_search_query({"url": "https://www.example.org/"}) == "site:example.org ocean project"
    assert project_search_query({"name": "Wild Oysters"}) == '"Wild Oysters" marine conservation'


def test_filter_exclude_urls_skips_already_eliminated():
    hits = [
        {"url": "https://example.org/projects/coral"},
        {"url": "https://example.org/projects/kelp"},
    ]
    out = filter_discover_urls(
        hits, SEED, 10, exclude_urls=["https://example.org/projects/coral"])
    assert out == ["https://example.org/projects/kelp"]


def test_portrait_png_is_not_a_project_fiche():
    path = "/wp-content/uploads/2025/12/Mabelys-Ramos-1.png"
    assert is_project_fiche_path(path) is False
    assert _is_soft_fiche_path(path) is False


def test_filter_drops_image_urls_even_on_soft_path():
    hits = [
        {"url": "https://marviva.net/wp-content/uploads/2026/05/Sandra-Vilardy.png"},
        {"url": "https://marviva.net/equipo/sandra-vilardy"},
        {"url": "https://marviva.net/projects/manglares"},
    ]
    seed = {"name": "MarViva", "url": "https://marviva.net/"}
    out = filter_discover_urls(hits, seed, 10)
    assert out == ["https://marviva.net/projects/manglares"]
    assert all(not u.endswith(".png") for u in out)


def test_filter_soft_keeps_research_when_no_project_path():
    hits = [
        {"url": "https://scripps.edu/science/earth-section/"},
        {"url": "https://scripps.edu/news/expedition"},
        {"url": "https://other.edu/science/ignored"},
    ]
    seed = {"name": "Scripps", "url": "https://scripps.edu/"}
    out = filter_discover_urls(hits, seed, 10)
    assert out == ["https://scripps.edu/science/earth-section/"]


def test_filter_drops_news_and_other_domain():
    hits = [
        {"url": "https://example.org/projects/coral"},
        {"url": "https://example.org/news/project-launch"},
        {"url": "https://other.org/projects/ignored"},
        {"url": "https://example.org/projects/coral"},
        "ftp://example.org/projects/x",
    ]
    out = filter_discover_urls(hits, SEED, 10)
    assert out == ["https://example.org/projects/coral"]


def test_filter_treats_www_as_same_host_and_cuts_max():
    hits = [
        {"url": "https://www.example.org/projects/a"},
        {"url": "https://example.org/projects/b"},
        {"url": "https://example.org/projects/c"},
    ]
    out = filter_discover_urls(hits, {"url": "https://www.example.org/"}, 2)
    assert out == [
        "https://www.example.org/projects/a",
        "https://example.org/projects/b",
    ]


def test_official_site_skips_facebook():
    hits = [
        {"url": "https://www.facebook.com/wildoysters"},
        {"url": "https://wild-oysters.org/about"},
    ]
    assert official_site_from_hits(hits, "Wild Oysters") == "https://wild-oysters.org/"


def test_official_site_skips_shared_hub_prefers_name_domain():
    hits = [
        {"url": "https://oceandecade.org/decade-actions/"},
        {"url": "https://www.bmkg.go.id/profil"},
        {"url": "https://news.example.com/bmkg"},
    ]
    assert official_site_from_hits(
        hits, "Agency for Meteorology (BMKG) – Indonesia"
    ) == "https://www.bmkg.go.id/"
    only_hub = [{"url": "https://hubocean.earth/use-cases"}]
    assert official_site_from_hits(only_hub, "Aker Biomarine") == ""


def test_official_site_rejects_publisher_and_first_serp():
    name = "Agency for Meteorology (BMKG) – Indonesia"
    assert official_site_from_hits(
        [{"url": "https://www.nature.com/articles/s41586-bmkg"}], name
    ) == ""
    assert official_site_from_hits(
        [
            {"url": "https://www.nature.com/articles/s41586-bmkg"},
            {"url": "https://www.usgs.gov/centers/"},
        ],
        name,
    ) == ""
    assert official_site_from_hits(
        [
            {"url": "https://www.nature.com/articles/s41586-bmkg"},
            {"url": "https://www.bmkg.go.id/"},
        ],
        name,
    ) == "https://www.bmkg.go.id/"


def test_official_site_matches_short_acronym():
    hits = [
        {"url": "https://www.helmholtz.de/en/"},
        {"url": "https://www.awi.de/en/"},
    ]
    assert official_site_from_hits(
        hits, "Alfred Wegener Institute (AWI)"
    ) == "https://www.awi.de/"
    assert official_site_from_hits(
        hits, "Alfred Wegener Institute"
    ) == "https://www.awi.de/"


def test_official_site_nature_org_not_nature_com():
    assert official_site_from_hits(
        [{"url": "https://www.nature.com/"}], "The Nature Conservancy"
    ) == ""
    assert official_site_from_hits(
        [{"url": "https://www.nature.org/"}], "The Nature Conservancy"
    ) == "https://www.nature.org/"


def test_purpose_is_projects_not_poe():
    assert "project" in PROJECTS_DISCOVERY_PURPOSE.lower()
    assert PROJECTS_DISCOVERY_PURPOSE != POE_PURPOSE
    assert PROJECTS_LISTING_PURPOSE != PROJECTS_DISCOVERY_PURPOSE
    assert PROJECTS_LISTING_PURPOSE != POE_PURPOSE
    src = Path(__file__).resolve().parents[1] / "app" / "services" / "swarm_pipeline.py"
    text = src.read_text(encoding="utf-8")
    assert "google_style_shots" not in text
    assert "db.projects.insert_one" not in text
    assert "db.projects.update_one" not in text
    assert "listing_goal(" in text
    assert "discovery_goal(" in text
    assert "_tinyfish_listing_discover" in text


def test_n1_patterns_skip_search_and_agent(monkeypatch):
    sw = _swarm()
    calls = []

    async def fake_crawl(seed, max_urls):
        return ["https://example.org/projects/coral"]

    async def boom(*a, **k):
        calls.append("unexpected")
        return []

    async def boom_agent(*a, **k):
        calls.append("agent")
        return []

    monkeypatch.setattr(sw, "_crawl_discover", fake_crawl)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_fetch", boom)
    monkeypatch.setattr(sp, "tf_search", boom)
    monkeypatch.setattr(sp, "serper_search", boom)
    monkeypatch.setattr(sw, "_tinyfish_discover", boom_agent)

    queued = _run(_queued(sw))
    assert [i["url"] for i in queued] == ["https://example.org/projects/coral"]
    assert calls == []
    assert sw._serper_queries == 0
    assert sw.wrote_projects is False
    assert sw.db.projects.docs == []
    assert sw.status()["wrote_projects"] is False


def test_fetch_fiches_skip_search(monkeypatch):
    sw = _swarm()
    search_calls = []

    async def fake_crawl(seed, max_urls):
        return []

    async def fake_fetch(urls, key, **kw):
        assert kw.get("links") is True
        assert kw.get("purpose") == PROJECTS_DISCOVERY_PURPOSE
        return {urls[0]: {"links": [
            "https://example.org/projects/js-page",
            "https://example.org/news/ignored",
            "https://other.org/projects/nope",
        ]}}

    async def track_search(*a, **k):
        search_calls.append("search")
        return []

    async def boom_agent(*a, **k):
        search_calls.append("agent")
        return []

    monkeypatch.setattr(sw, "_crawl_discover", fake_crawl)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_fetch", fake_fetch)
    monkeypatch.setattr(sp, "tf_search", track_search)
    monkeypatch.setattr(sp, "serper_search", track_search)
    monkeypatch.setattr(sw, "_tinyfish_discover", boom_agent)

    queued = _run(_queued(sw))
    assert [i["url"] for i in queued] == ["https://example.org/projects/js-page"]
    assert search_calls == []
    assert sw.db.projects.docs == []


def test_n1_and_fetch_empty_runs_tf_and_serper_in_parallel(monkeypatch, tmp_path):
    monkeypatch.setattr(run_journal, "RUNS_DIR", tmp_path)
    sw = _swarm()
    sw.run_id = "20260912-192000-srch01"
    seen = {"tf": [], "sp": [], "agent": 0}

    async def fake_crawl(seed, max_urls):
        return []

    async def fake_fetch(urls, key, **kw):
        return {urls[0]: {"links": []}}

    async def fake_tf(query, key, **kw):
        seen["tf"].append({"query": query, "include": kw.get("include_domains"),
                           "purpose": kw.get("purpose")})
        return [{"url": "https://example.org/projects/from-tf"}]

    async def fake_sp(query, key, **kw):
        seen["sp"].append(query)
        return [
            {"url": "https://example.org/projects/from-sp"},
            {"url": "https://example.org/news/project-launch"},
            {"url": "https://other.org/projects/nope"},
        ]

    async def boom_agent(*a, **k):
        seen["agent"] += 1
        return []

    monkeypatch.setattr(sw, "_crawl_discover", fake_crawl)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_fetch", fake_fetch)
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)
    monkeypatch.setattr(sw, "_tinyfish_discover", boom_agent)

    queued = _run(_queued(sw))
    urls = [i["url"] for i in queued]
    assert urls == [
        "https://example.org/projects/from-tf",
        "https://example.org/projects/from-sp",
    ]
    assert len(seen["sp"]) == 1
    assert seen["sp"][0] == "site:example.org Example Ocean ocean project"
    assert seen["tf"][0]["query"] == seen["sp"][0]
    assert seen["tf"][0]["include"] == ["example.org"]
    assert seen["tf"][0]["purpose"] == PROJECTS_DISCOVERY_PURPOSE
    assert seen["agent"] == 0
    assert sw._serper_queries == 1
    assert sw.db.projects.docs == []
    packed = run_journal.read_journal_file(sw.run_id)
    assert packed["wrote_projects"] is False
    msgs = " ".join(e["msg"] for e in packed["items"])
    assert "N1: 0 fiches" in msgs
    assert "Fetch:" in msgs
    assert "Search: TinyFish 1 + Serper 3 → 2 after filter (serper 1)" in msgs
    assert "Search vide → TinyFish Agent" not in msgs


def test_no_serper_hard_cap_still_calls_serper(monkeypatch):
    sw = _swarm()
    sw._serper_queries = 1800
    seen = {"tf": 0, "sp": 0}

    async def fake_crawl(seed, max_urls):
        return []

    async def fake_fetch(urls, key, **kw):
        return {}

    async def fake_tf(query, key, **kw):
        seen["tf"] += 1
        return [{"url": "https://example.org/projects/still-ok"}]

    async def fake_sp(query, key, **kw):
        seen["sp"] += 1
        return [{"url": "https://example.org/projects/also-ok"}]

    monkeypatch.setattr(sw, "_crawl_discover", fake_crawl)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_fetch", fake_fetch)
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)
    monkeypatch.setattr(sw, "_tinyfish_discover", fake_crawl)

    queued = _run(_queued(sw))
    assert "https://example.org/projects/still-ok" in [i["url"] for i in queued]
    assert "https://example.org/projects/also-ok" in [i["url"] for i in queued]
    assert seen["tf"] == 1
    assert seen["sp"] == 1
    assert sw._serper_queries == 1801


def test_home_internal_links_are_not_n1_fiches(monkeypatch):
    """On a catalogue, About/News do not count as cards — Search runs."""
    _HtmlClient.html = HOME_HTML
    sw = _swarm()
    search = {"tf": 0, "sp": 0}

    async def fake_fetch(urls, key, **kw):
        return {}

    async def fake_tf(query, key, **kw):
        search["tf"] += 1
        return [{"url": "https://example.org/projects/via-home"}]

    async def fake_sp(query, key, **kw):
        search["sp"] += 1
        return []

    import app.services.swarm_pipeline as sp
    _install_offline_html(monkeypatch)
    monkeypatch.setattr(sp, "tf_fetch", fake_fetch)
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)
    monkeypatch.setattr(sw, "_tinyfish_discover", fake_fetch)

    queued = _run(_queued(sw))
    assert search == {"tf": 1, "sp": 1}
    assert [i["url"] for i in queued] == ["https://example.org/projects/via-home"]


def test_crawl_pattern_html_does_not_call_search(monkeypatch):
    _HtmlClient.html = FICHE_HTML
    sw = _swarm()
    search = []

    async def track(*a, **k):
        search.append("x")
        return {}

    import app.services.swarm_pipeline as sp
    _install_offline_html(monkeypatch)
    monkeypatch.setattr(sp, "tf_fetch", track)
    monkeypatch.setattr(sp, "tf_search", track)
    monkeypatch.setattr(sp, "serper_search", track)
    monkeypatch.setattr(sw, "_tinyfish_discover", track)

    queued = _run(_queued(sw))
    assert [i["url"] for i in queued] == ["https://example.org/projects/coral-restore"]
    assert search == []


def test_ftm_name_without_url_searches_then_discovers(monkeypatch):
    sw = _swarm()
    sw.master_seeds = []
    sw.partner_domains = set()
    sw.recursive_tasks = []
    discovered = []
    search_q = []

    async def fake_discover(seed, max_urls, depth=0):
        discovered.append((seed["name"], seed["url"], depth))

    async def fake_tf(query, key, **kw):
        search_q.append(("tf", query, kw.get("include_domains")))
        return [
            {"url": "https://facebook.com/wild"},
            {"url": "https://wild-oysters.org/about"},
        ]

    async def fake_sp(query, key, **kw):
        search_q.append(("sp", query))
        return []

    sw._discover = fake_discover
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)

    async def go():
        await sw._follow_the_money(
            {"partners": [{"name": "Wild Oysters", "url": None}]}, depth=0)
        assert discovered == []
        assert search_q == []
        await sw._flush_ftm_inbox()
        await asyncio.sleep(0)

    _run(go())
    assert search_q[0][1] == '"Wild Oysters" official site'
    assert ("tf", '"Wild Oysters" official site', None) in search_q
    assert ("sp", '"Wild Oysters" official site') in search_q
    assert sw._serper_queries == 1
    assert discovered == [("Wild Oysters (partner)", "https://wild-oysters.org/", 1)]
    assert sw.db.projects.docs == []
    assert sw.wrote_projects is False


def test_ftm_without_search_hit_ignores_partner(monkeypatch):
    sw = _swarm()
    sw.master_seeds = []
    sw.partner_domains = set()
    sw.recursive_tasks = []
    discovered = []

    async def fake_discover(seed, max_urls, depth=0):
        discovered.append(seed)

    async def empty(*a, **k):
        return []

    sw._discover = fake_discover
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", empty)
    monkeypatch.setattr(sp, "serper_search", empty)

    async def go():
        await sw._follow_the_money(
            {"partners": [{"name": "Nobody Known", "url": ""}]}, depth=0)
        await sw._flush_ftm_inbox()

    _run(go())
    assert discovered == []
    assert sw.recursive_tasks == []
    assert sw.db.projects.docs == []


def test_ftm_exclude_name_does_not_search(monkeypatch):
    sw = _swarm()
    sw.master_seeds = [
        {"name": "Rare Fish Forever", "url": "https://rare.org/program/fish-forever/",
         "aliases": ["Rare"], "home_status": "official", "queue": "crawl"},
    ]
    sw.partner_domains = set()
    sw.recursive_tasks = []
    search = []

    async def track(*a, **k):
        search.append(1)
        return []

    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", track)
    monkeypatch.setattr(sp, "serper_search", track)

    _run(sw._follow_the_money(
        {"partners": [{"name": "Unknown", "url": None}]}, depth=0))
    assert search == []
    assert sw.recursive_tasks == []


def test_ftm_hub_url_searches_official_site(monkeypatch):
    sw = _swarm()
    sw.master_seeds = [
        {"name": "Rare Fish Forever", "url": "https://rare.org/program/fish-forever/",
         "aliases": ["Rare"], "home_status": "official", "queue": "crawl"},
    ]
    sw.partner_domains = set()
    sw.recursive_tasks = []
    discovered = []
    search_q = []

    async def fake_discover(seed, max_urls, depth=0):
        discovered.append((seed["name"], seed["url"], seed.get("home_status")))

    async def fake_tf(query, key, **kw):
        search_q.append(query)
        return [{"url": "https://oceandecade.org/actions/x"}, {"url": "https://bmkg.go.id/"}]

    async def fake_sp(query, key, **kw):
        return []

    sw._discover = fake_discover
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)

    async def go():
        await sw._follow_the_money({
            "partners": [{"name": "BMKG", "url": "https://oceandecade.org/actions/foo"}],
        }, depth=0)
        assert discovered == []
        await sw._flush_ftm_inbox()
        await asyncio.sleep(0)

    _run(go())
    assert search_q[0] == '"BMKG" official site'
    assert discovered == [("BMKG (partner)", "https://bmkg.go.id/", "official")]
    assert all("oceandecade.org" not in (u or "") for _, u, _ in discovered)


def test_known_v1_name_without_url_does_not_search(monkeypatch):
    sw = _swarm()
    sw.master_seeds = [
        {"name": "Rare Fish Forever", "url": "https://rare.org/program/fish-forever/",
         "aliases": ["Rare"]},
    ]
    sw.partner_domains = set()
    sw.recursive_tasks = []
    discovered = []
    search = []

    async def fake_discover(seed, max_urls, depth=0):
        discovered.append((seed["name"], seed["url"]))

    async def track(*a, **k):
        search.append(1)
        return []

    sw._discover = fake_discover
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", track)
    monkeypatch.setattr(sp, "serper_search", track)

    async def go():
        await sw._follow_the_money({"partners": [{"name": "Rare", "url": None}]}, depth=0)
        await asyncio.sleep(0)

    _run(go())
    assert search == []
    assert discovered == [("Rare Fish Forever", "https://rare.org/program/fish-forever/")]


def test_agent_last_resort_only_when_search_empty(monkeypatch):
    sw = _swarm()
    agent = {"n": 0}

    async def fake_crawl(seed, max_urls):
        return []

    async def empty_fetch(*a, **k):
        return {}

    async def empty_search(*a, **k):
        return []

    async def fake_agent(*a, **k):
        agent["n"] += 1
        return ["https://example.org/projects/from-agent"]

    monkeypatch.setattr(sw, "_crawl_discover", fake_crawl)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_fetch", empty_fetch)
    monkeypatch.setattr(sp, "tf_search", empty_search)
    monkeypatch.setattr(sp, "serper_search", empty_search)
    monkeypatch.setattr(sw, "_tinyfish_discover", fake_agent)

    queued = _run(_queued(sw))
    assert agent["n"] == 1
    assert [i["url"] for i in queued] == ["https://example.org/projects/from-agent"]
    assert sw.db.projects.docs == []


def test_no_serper_key_still_runs_tf_search(monkeypatch):
    sw = _swarm({"serper_api_key": ""})
    seen = {"tf": 0, "sp": 0}

    async def fake_crawl(seed, max_urls):
        return []

    async def fake_fetch(*a, **k):
        return {}

    async def fake_tf(query, key, **kw):
        seen["tf"] += 1
        return [{"url": "https://example.org/projects/tf-only"}]

    async def fake_sp(*a, **k):
        seen["sp"] += 1
        return []

    monkeypatch.setattr(sw, "_crawl_discover", fake_crawl)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_fetch", fake_fetch)
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)
    monkeypatch.setattr(sw, "_tinyfish_discover", fake_crawl)

    queued = _run(_queued(sw))
    assert seen == {"tf": 1, "sp": 0}
    assert sw._serper_queries == 0
    assert [i["url"] for i in queued] == ["https://example.org/projects/tf-only"]


def test_mode_test_journal_shows_n1_then_search(monkeypatch, tmp_path):
    """Simulated mode=test run (1 home seed): N1 → Search in the journal, no full 776."""
    monkeypatch.setattr(run_journal, "RUNS_DIR", tmp_path)
    sw = _swarm()
    sw.run_id = "20260912-193000-test01"
    sw.mode = "test"

    async def fake_crawl(seed, max_urls):
        return []

    async def fake_fetch(urls, key, **kw):
        return {urls[0]: {"links": []}}

    async def fake_tf(query, key, **kw):
        return [{"url": "https://example.org/projects/test-seed"}]

    async def fake_sp(query, key, **kw):
        return []

    monkeypatch.setattr(sw, "_crawl_discover", fake_crawl)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_fetch", fake_fetch)
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)
    monkeypatch.setattr(sw, "_tinyfish_discover", fake_crawl)

    queued = _run(_queued(sw, SEED, 6))
    assert queued
    packed = run_journal.read_journal_file(sw.run_id)
    assert packed["wrote_projects"] is False
    text = run_journal.journal_to_text(sw.run_id)
    assert "N1: 0 fiches" in text
    assert "Search: TinyFish" in text
    assert packed["wrote_projects"] is False
    assert sw.db.projects.docs == []
    assert sw.mode == "test"
