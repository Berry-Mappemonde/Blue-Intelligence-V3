"""Home → catalogue: filters, two TinyFish Agents, no from-scratch cache dump."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "bi_test_project_listing")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.core.tinyfish import (
    FICHE_AGENT_DURATION_S, LISTING_AGENT_DURATION_S, LISTING_SCHEMA,
    PROJECTS_LISTING_PURPOSE,
)
from app.services.swarm_pipeline import agent_host_is_worthwhile
from app.services.project_listing import (
    ListingJudgeQuotaError, accept_listing_url, apply_learned_listings,
    filter_listing_urls, hygiene_listing_urls, infer_listing_from_project_urls,
    is_homepage_url, is_listing_path, is_listing_url, listing_hygiene_ok,
    listing_search_query, listing_search_retry_query,
    merge_listing_candidates, needs_listing_hop, parse_listing_judge,
    pick_listing_url,
)
from app.services.swarm_pipeline import Swarm
from app.static_data.seeds import CURATED_SEEDS
from tests.test_project_runs import _FakeDB

HOME = {"name": "Example Ocean", "url": "https://example.org/"}
LISTING = {
    "name": "Example Ocean",
    "url": "https://example.org/projects/",
    "listing_kind": "projects_index",
}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_paid_env(monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def _swarm(settings=None):
    sw = Swarm(_FakeDB())
    sw.running = True
    sw.force_rescan = True
    sw.queue = asyncio.Queue()
    sw.master_seeds = [dict(HOME)]
    sw.settings = {
        "tinyfish_api_key": "tf-test",
        "serper_api_key": "sp-test",
        "allow_tinyfish_agent": True,
        "follow_the_money": True,
        "max_partner_orgs": 5,
        **(settings or {}),
    }
    return sw


async def _queued(sw, seed=HOME, max_urls=6):
    await sw._discover(seed, max_urls)
    await asyncio.sleep(0)
    items = []
    while not sw.queue.empty():
        items.append(sw.queue.get_nowait())
        sw.queue.task_done()
    return items


def test_listing_path_accepts_curated_leaves_rejects_fiches():
    assert is_listing_path("/projects/")
    assert is_listing_path("/hope-spots/")
    assert is_listing_path("/en/where-we-work/")
    assert is_listing_path("/campaigns/")
    assert not is_listing_path("/projects/coral-restore")
    assert not is_listing_path("/")
    assert not is_listing_path("/about")
    assert not is_listing_path("/news/project-launch")
    assert not is_listing_path("/wp-content/uploads/")
    assert is_homepage_url("https://example.org/")
    assert is_homepage_url("https://example.org/en")
    assert not is_homepage_url("https://example.org/projects/")
    assert is_listing_url("https://missionblue.org/hope-spots/")
    assert not is_listing_url("https://example.org/projects/coral-restore")


def test_needs_listing_hop_skips_known_and_curated():
    assert needs_listing_hop(HOME) is True
    assert needs_listing_hop(LISTING) is False
    assert needs_listing_hop({"url": "https://example.org/projects/"}) is False
    rare = next(s for s in CURATED_SEEDS if "rare.org" in s["url"])
    assert needs_listing_hop(rare) is False
    mer = next(s for s in CURATED_SEEDS if "fondationdelamer" in s["url"])
    assert needs_listing_hop(mer) is False
    assert "nos-programmes" in mer["url"]
    tagged_home = {"url": "https://example.org/", "listing_kind": "projects_index"}
    assert needs_listing_hop(tagged_home) is True
    ifremer = next(s for s in CURATED_SEEDS if s["name"] == "IFREMER")
    assert needs_listing_hop(ifremer) is False
    assert ifremer.get("listing_kind") == "home_only"


def test_pick_listing_prefers_shortest_index():
    assert pick_listing_url([
        "https://example.org/en/where-we-work/",
        "https://example.org/projects/",
        "https://example.org/projects/coral-restore",
    ]) == "https://example.org/projects/"


def test_filter_listing_same_host_not_fiche_not_home():
    hits = [
        {"url": "https://example.org/"},
        {"url": "https://example.org/projects/coral-restore"},
        {"url": "https://example.org/news/projects"},
        {"url": "https://other.org/projects/"},
        {"url": "https://example.org/projects/"},
        {"url": "https://example.org/hope-spots/"},
    ]
    out = filter_listing_urls(hits, HOME, 5)
    assert out == [
        "https://example.org/projects/",
        "https://example.org/hope-spots/",
    ]
    dropped = filter_listing_urls(
        hits, HOME, 5, exclude_urls=["https://example.org/projects/"])
    assert dropped == ["https://example.org/hope-spots/"]


def test_hygiene_keeps_our_programmes_research_not_fiches():
    hits = [
        {"url": "https://example.org/"},
        {"url": "https://example.org/news/project-launch"},
        {"url": "https://example.org/donate"},
        {"url": "https://example.org/about"},
        {"url": "https://example.org/wp-content/uploads/a.pdf"},
        {"url": "https://example.org/projects/coral-restore"},
        {"url": "https://example.org/research"},
        {"url": "https://example.org/our-programmes"},
        {"url": "https://other.org/research"},
        {"url": "https://example.org/projects/"},
    ]
    hyg = hygiene_listing_urls(hits, HOME, 5)
    assert hyg[0] == "https://example.org/projects/"
    assert "https://example.org/our-programmes" in hyg
    assert "https://example.org/research" in hyg
    assert "https://example.org/projects/coral-restore" not in hyg
    assert "https://example.org/news/project-launch" not in hyg
    assert "https://example.org/" not in hyg
    assert listing_hygiene_ok("https://example.org/research")
    assert listing_hygiene_ok("https://example.org/our-programmes")
    assert not listing_hygiene_ok("https://example.org/projects/coral-restore")
    assert not listing_hygiene_ok("https://example.org/wp-content/uploads/x.pdf")
    assert not is_listing_url("https://example.org/our-programmes")
    assert not is_listing_url("https://example.org/research")
    assert accept_listing_url("https://example.org/our-programmes", HOME) == (
        "https://example.org/our-programmes"
    )
    assert accept_listing_url("https://example.org/projects/coral-restore", HOME) is None
    assert filter_listing_urls(hits, HOME, 5) == ["https://example.org/projects/"]


def test_hygiene_puts_late_leaf_first():
    hits = [{"url": f"https://example.org/page-{i}"} for i in range(6)]
    hits.append({"url": "https://example.org/projects/"})
    hyg = hygiene_listing_urls(hits, HOME, 5)
    assert hyg[0] == "https://example.org/projects/"
    assert len(hyg) == 5


def test_merge_listing_candidates_caps_at_judge():
    merged = merge_listing_candidates(
        ["https://example.org/research"],
        ["https://example.org/our-programmes"],
        ["https://example.org/what-we-do", "https://example.org/projects/"],
        cap=5,
    )
    assert merged[0] == "https://example.org/projects/"
    assert "https://example.org/our-programmes" in merged
    assert len(merged) <= 5


def test_infer_listing_sos_not_wp_content():
    sos = infer_listing_from_project_urls([
        "https://saveourseas.com/project/alpha",
        "https://saveourseas.com/project/beta",
    ], "Save Our Seas Foundation")
    assert sos == "https://saveourseas.com/project/"
    junk = infer_listing_from_project_urls([
        "https://blueactionfund.org/wp-content/uploads/a.pdf",
        "https://blueactionfund.org/wp-content/uploads/b.pdf",
    ], "Blue Action Fund")
    assert junk is None


def test_apply_learned_listings_overlays_v1_home():
    seeds = [
        {"name": "Example Ocean", "url": "https://example.org/", "listing_kind": "homepage"},
    ]
    extras = [{
        "name": "Example Ocean", "url": "https://example.org/projects/",
        "domain": "example.org", "listing_kind": "projects_index",
    }]
    out = apply_learned_listings(seeds, extras)
    assert out[0]["url"] == "https://example.org/projects/"
    assert out[0]["listing_kind"] == "projects_index"
    assert needs_listing_hop(out[0]) is False


def test_apply_learned_listings_keeps_official_home_not_hub():
    seeds = [{
        "name": "Aker Biomarine",
        "url": "https://www.akerbiomarine.com/",
        "home_url": "https://www.akerbiomarine.com/",
        "home_status": "official",
        "queue": "crawl",
        "listing_kind": "homepage",
    }]
    extras = [{
        "name": "Aker Biomarine",
        "url": "https://hubocean.earth/projects/",
        "domain": "hubocean.earth",
        "listing_kind": "projects_index",
    }]
    out = apply_learned_listings(seeds, extras)
    assert out[0]["url"] == "https://www.akerbiomarine.com/"
    assert out[0]["listing_kind"] == "homepage"


def test_discover_uses_official_home_instead_of_skip(monkeypatch):
    sw = _swarm()
    seed = {
        "name": "Aker Biomarine",
        "url": "https://hubocean.earth/",
        "home_url": "https://www.akerbiomarine.com/",
        "home_status": "official",
        "queue": "crawl",
        "listing_kind": "homepage",
    }
    seen = []

    async def fake_listing(s):
        seen.append(s["url"])
        return None

    async def fake_fiches(s, *a, **k):
        seen.append("fiches:" + (s.get("url") or ""))

    monkeypatch.setattr(sw, "_resolve_listing", fake_listing)
    monkeypatch.setattr(sw, "_discover_fiches", fake_fiches)
    _run(sw._discover(seed, 6))
    assert seen[0] == "https://www.akerbiomarine.com/"
    msgs = " ".join(e["msg"] for e in sw.logs)
    assert "home déjà classée" not in msgs


def test_apply_learned_listings_does_not_stamp_hub_on_hosted_org():
    seeds = [
        {"name": "BMKG", "url": "https://oceandecade.org/", "listing_kind": "homepage"},
        {"name": "Ocean Decade", "url": "https://oceandecade.org/", "listing_kind": "homepage"},
    ]
    extras = [{
        "name": "Ocean Decade",
        "url": "https://oceandecade.org/decade-actions/",
        "domain": "oceandecade.org",
        "listing_kind": "projects_index",
    }]
    out = {s["name"]: s for s in apply_learned_listings(seeds, extras)}
    assert out["BMKG"]["url"] == "https://oceandecade.org/"
    assert out["BMKG"]["listing_kind"] == "homepage"
    assert out["Ocean Decade"]["url"] == "https://oceandecade.org/decade-actions/"
    assert out["Ocean Decade"]["listing_kind"] == "projects_index"


def test_apply_learned_listings_keeps_hygiene_not_leaf():
    seeds = [
        {"name": "Example Ocean", "url": "https://example.org/", "listing_kind": "homepage"},
    ]
    extras = [{
        "name": "Example Ocean", "url": "https://example.org/our-programmes",
        "domain": "example.org", "listing_kind": "projects_index",
    }]
    out = apply_learned_listings(seeds, extras)
    assert out[0]["url"] == "https://example.org/our-programmes"
    assert out[0]["listing_kind"] == "projects_index"
    assert needs_listing_hop(out[0]) is False


def test_listing_search_query_is_single_shot_site():
    q = listing_search_query(HOME)
    assert q.startswith("site:example.org")
    assert "listing" in q
    retry = listing_search_retry_query(HOME)
    assert "inurl:projects" in retry
    assert retry != q


def test_known_listing_skips_listing_agent(monkeypatch):
    sw = _swarm()
    listing_agent = {"n": 0}

    async def fake_crawl(seed, max_urls):
        return ["https://example.org/projects/coral"]

    async def boom_listing(*a, **k):
        listing_agent["n"] += 1
        return None

    async def boom(*a, **k):
        raise AssertionError("search/fetch should not run")

    monkeypatch.setattr(sw, "_crawl_discover", fake_crawl)
    monkeypatch.setattr(sw, "_tinyfish_listing_discover", boom_listing)
    monkeypatch.setattr(sw, "_resolve_listing", boom)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_fetch", boom)
    monkeypatch.setattr(sp, "tf_search", boom)

    queued = _run(_queued(sw, LISTING))
    assert [i["url"] for i in queued] == ["https://example.org/projects/coral"]
    assert listing_agent["n"] == 0
    assert sw.db.projects.docs == []
    assert sw.wrote_projects is False


def test_home_n1_finds_listing_then_fiches(monkeypatch):
    sw = _swarm()
    seen = {"listing_agent": 0, "fiche_agent": 0, "search": 0}

    async def fake_listing_crawl(seed, max_urls=5):
        assert seed["url"] == "https://example.org/"
        return ["https://example.org/projects/"]

    async def fake_fiche_crawl(seed, max_urls):
        assert seed["url"] == "https://example.org/projects/"
        return ["https://example.org/projects/coral"]

    async def boom_listing(*a, **k):
        seen["listing_agent"] += 1
        return None

    async def boom_fiche(*a, **k):
        seen["fiche_agent"] += 1
        return []

    async def track_search(*a, **k):
        seen["search"] += 1
        return []

    monkeypatch.setattr(sw, "_crawl_listing", fake_listing_crawl)
    monkeypatch.setattr(sw, "_crawl_discover", fake_fiche_crawl)
    monkeypatch.setattr(sw, "_tinyfish_listing_discover", boom_listing)
    monkeypatch.setattr(sw, "_tinyfish_discover", boom_fiche)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_fetch", track_search)
    monkeypatch.setattr(sp, "tf_search", track_search)
    monkeypatch.setattr(sp, "serper_search", track_search)

    queued = _run(_queued(sw, HOME))
    assert [i["url"] for i in queued] == ["https://example.org/projects/coral"]
    assert seen == {"listing_agent": 0, "fiche_agent": 0, "search": 0}
    assert queued[0]["source"] == "https://example.org/projects/"
    remembered = sw.db.master_seeds.docs
    assert any(d.get("listing_kind") == "projects_index" for d in remembered)
    assert any(d.get("url") == "https://example.org/projects/" for d in remembered)
    assert sw.db.projects.docs == []


def test_shared_hub_resolves_official_site_before_listing(monkeypatch):
    sw = _swarm()
    seen = {"official": [], "listing": []}
    seed = {
        "name": "Agency for Meteorology (BMKG) – Indonesia",
        "url": "https://oceandecade.org/",
        "listing_kind": "homepage",
    }

    async def fake_official(s):
        seen["official"].append(s["url"])
        assert s["name"] == seed["name"]
        return "https://www.bmkg.go.id/"

    async def fake_listing(s):
        seen["listing"].append(s["url"])
        assert s["url"] == "https://www.bmkg.go.id/"
        return "https://www.bmkg.go.id/projects/"

    async def fake_fiche_crawl(s, max_urls):
        assert s["url"] == "https://www.bmkg.go.id/projects/"
        return ["https://www.bmkg.go.id/projects/coral"]

    async def boom(*a, **k):
        raise AssertionError("should not fall back to hub search")

    monkeypatch.setattr(sw, "_resolve_official_home", fake_official)
    monkeypatch.setattr(sw, "_resolve_listing", fake_listing)
    monkeypatch.setattr(sw, "_crawl_discover", fake_fiche_crawl)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", boom)
    monkeypatch.setattr(sp, "serper_search", boom)

    queued = _run(_queued(sw, seed))
    assert seen["official"] == ["https://oceandecade.org/"]
    assert seen["listing"] == ["https://www.bmkg.go.id/"]
    assert [i["url"] for i in queued] == ["https://www.bmkg.go.id/projects/coral"]
    assert sw.db.projects.docs == []


def test_classified_borrowed_hub_skips_live_official_search(monkeypatch):
    sw = _swarm()
    seed = {
        "name": "Agency for Meteorology (BMKG) – Indonesia",
        "url": "https://oceandecade.org/",
        "listing_kind": "unknown",
        "home_status": "borrowed_hub",
        "queue": "resolve",
    }

    async def boom(*a, **k):
        raise AssertionError("classified seed must not Search or Agent")

    monkeypatch.setattr(sw, "_resolve_official_home", boom)
    monkeypatch.setattr(sw, "_resolve_listing", boom)
    monkeypatch.setattr(sw, "_crawl_discover", boom)
    queued = _run(_queued(sw, seed))
    assert queued == []


def test_ocean_decade_skips_official_site_search(monkeypatch):
    sw = _swarm()
    seed = {"name": "Ocean Decade", "url": "https://oceandecade.org/",
            "listing_kind": "homepage"}
    seen = {"official": 0}

    async def fake_official(*a, **k):
        seen["official"] += 1
        raise AssertionError("Ocean Decade owns the hub")

    async def fake_listing(s):
        assert s["url"] == "https://oceandecade.org/"
        return "https://oceandecade.org/decade-actions/"

    async def fake_fiche_crawl(s, max_urls):
        return ["https://oceandecade.org/actions/one"]

    monkeypatch.setattr(sw, "_resolve_official_home", fake_official)
    monkeypatch.setattr(sw, "_resolve_listing", fake_listing)
    monkeypatch.setattr(sw, "_crawl_discover", fake_fiche_crawl)
    queued = _run(_queued(sw, seed))
    assert seen["official"] == 0
    assert [i["url"] for i in queued] == ["https://oceandecade.org/actions/one"]


def test_borrowed_surfrider_home_resolves_official_site(monkeypatch):
    sw = _swarm()
    seen = {"official": []}
    seed = {
        "name": "California Coastal Commission",
        "url": "https://surfrider.org/",
        "listing_kind": "homepage",
    }

    async def fake_official(s):
        seen["official"].append(s["url"])
        return "https://www.coastal.ca.gov/"

    async def fake_listing(s):
        assert s["url"] == "https://www.coastal.ca.gov/"
        return None

    async def fake_fiche_crawl(s, max_urls):
        assert s["url"] == "https://www.coastal.ca.gov/"
        return ["https://www.coastal.ca.gov/programs/whale-tail/"]

    monkeypatch.setattr(sw, "_resolve_official_home", fake_official)
    monkeypatch.setattr(sw, "_resolve_listing", fake_listing)
    monkeypatch.setattr(sw, "_crawl_discover", fake_fiche_crawl)
    queued = _run(_queued(sw, seed))
    assert seen["official"] == ["https://surfrider.org/"]
    assert [i["url"] for i in queued] == [
        "https://www.coastal.ca.gov/programs/whale-tail/"]
    assert sw.db.projects.docs == []


def test_listing_search_retry_blacklists_eliminated(monkeypatch):
    sw = _swarm()
    queries = []

    async def none(*a, **k):
        return None

    async def empty_list(*a, **k):
        return []

    async def fake_tf(query, key, **kw):
        queries.append(("tf", query, kw.get("purpose")))
        if "inurl:projects" in query:
            return [{"url": "https://example.org/projects/"}]
        return [
            {"url": "https://example.org/news/project-launch"},
            {"url": "https://example.org/about"},
        ]

    async def fake_sp(query, key, **kw):
        queries.append(("sp", query))
        return [{"url": "https://example.org/donate"}]

    async def fake_fiche_crawl(seed, max_urls):
        assert seed["url"] == "https://example.org/projects/"
        return ["https://example.org/projects/coral"]

    monkeypatch.setattr(sw, "_crawl_listing", empty_list)
    monkeypatch.setattr(sw, "_fetch_listing", empty_list)
    monkeypatch.setattr(sw, "_infer_listing_from_live", none)
    monkeypatch.setattr(sw, "_crawl_discover", fake_fiche_crawl)
    monkeypatch.setattr(sw, "_tinyfish_listing_discover", empty_list)
    monkeypatch.setattr(sw, "_tinyfish_discover", empty_list)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)

    queued = _run(_queued(sw, HOME))
    assert [i["url"] for i in queued] == ["https://example.org/projects/coral"]
    tf_calls = [x for x in queries if x[0] == "tf"]
    assert len(tf_calls) >= 2
    assert tf_calls[0][2] == PROJECTS_LISTING_PURPOSE
    assert "inurl:projects" in tf_calls[1][1]
    assert sw._serper_queries >= 2
    assert sw.db.projects.docs == []


def test_two_distinct_tinyfish_agents(monkeypatch):
    sw = _swarm()
    calls = []

    async def empty(*a, **k):
        if k.get("log") or True:
            return []
        return []

    async def fake_search(*a, **k):
        return ["https://example.org/what-we-do"], 1, 0

    async def empty_search(*a, **k):
        return [], 0, 0

    async def fake_judge(*a, **k):
        return None

    async def fake_sse(url, goal, schema, key, **kw):
        calls.append({
            "url": url,
            "goal": goal,
            "required": schema.get("required"),
            "cfg": kw.get("agent_config") or {},
        })
        if schema is LISTING_SCHEMA or schema.get("required") == ["listing_url"]:
            return {"listing_url": "https://example.org/projects/", "title": "Projects"}
        return {"projects": [
            {"url": "https://example.org/projects/coral", "title": "Coral"},
        ]}

    monkeypatch.setattr(sw, "_crawl_listing", empty)
    monkeypatch.setattr(sw, "_fetch_listing", empty)
    monkeypatch.setattr(sw, "_infer_listing_from_live", empty)
    monkeypatch.setattr(sw, "_search_listing", fake_search)
    import app.services.swarm_pipeline as sp_two
    monkeypatch.setattr(sp_two, "llm_judge_listing", fake_judge)
    monkeypatch.setattr(sw, "_crawl_discover", empty)
    monkeypatch.setattr(sw, "_fetch_discover", empty)
    monkeypatch.setattr(sw, "_search_discover", empty_search)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_run_sse", fake_sse)

    queued = _run(_queued(sw, HOME))
    assert [i["url"] for i in queued] == ["https://example.org/projects/coral"]
    assert len(calls) == 2
    assert calls[0]["required"] == ["listing_url"]
    assert "listing agent" in calls[0]["goal"].lower()
    assert "do not collect individual" in calls[0]["goal"].lower()
    assert calls[0]["url"] == "https://example.org/"
    assert calls[1]["required"] == ["projects"]
    assert "one individual" in calls[1]["goal"].lower()
    assert calls[1]["url"] == "https://example.org/projects/"
    assert calls[0]["cfg"].get("max_duration_seconds") == LISTING_AGENT_DURATION_S
    assert calls[1]["cfg"].get("max_duration_seconds") == FICHE_AGENT_DURATION_S
    assert "max_steps" not in calls[0]["cfg"]
    assert "max_steps" not in calls[1]["cfg"]
    assert 60 <= LISTING_AGENT_DURATION_S <= 90
    assert 60 <= FICHE_AGENT_DURATION_S <= 90
    assert LISTING_SCHEMA["required"] == ["listing_url"]
    assert sw.db.projects.docs == []
    assert sw.wrote_projects is False
    msgs = " ".join(e["msg"] for e in sw.logs)
    assert "TinyFish Agent listing" in msgs
    assert "TinyFish Agent fiches" in msgs
    assert "Search vide → TinyFish Agent" not in msgs


def test_listing_zero_hits_skips_agent(monkeypatch):
    sw = _swarm()
    listing_agent = {"n": 0}

    async def empty(*a, **k):
        return []

    async def empty_search(*a, **k):
        return [], 0, 0

    async def boom_listing(*a, **k):
        listing_agent["n"] += 1
        raise AssertionError("0 hit must not start TinyFish Agent listing")

    monkeypatch.setattr(sw, "_crawl_listing", empty)
    monkeypatch.setattr(sw, "_fetch_listing", empty)
    monkeypatch.setattr(sw, "_infer_listing_from_live", empty)
    monkeypatch.setattr(sw, "_search_listing", empty_search)
    monkeypatch.setattr(sw, "_crawl_discover", empty)
    monkeypatch.setattr(sw, "_fetch_discover", empty)
    monkeypatch.setattr(sw, "_search_discover", empty_search)
    monkeypatch.setattr(sw, "_tinyfish_listing_discover", boom_listing)
    monkeypatch.setattr(sw, "_tinyfish_discover", empty)

    queued = _run(_queued(sw, HOME))
    assert queued == []
    assert listing_agent["n"] == 0
    msgs = " ".join(e["msg"] for e in sw.logs)
    assert "aucun candidat" in msgs


def test_listing_judge_429_skips_agent(monkeypatch):
    sw = _swarm()
    listing_agent = {"n": 0}

    async def empty(*a, **k):
        return []

    async def fake_search(*a, **k):
        return ["https://example.org/what-we-do"], 1, 0

    async def quota(*a, **k):
        raise ListingJudgeQuotaError("nvidia HTTP 429: rate limited")

    async def boom_listing(*a, **k):
        listing_agent["n"] += 1
        raise AssertionError("429 must not start TinyFish Agent listing")

    monkeypatch.setattr(sw, "_crawl_listing", empty)
    monkeypatch.setattr(sw, "_fetch_listing", empty)
    monkeypatch.setattr(sw, "_infer_listing_from_live", empty)
    monkeypatch.setattr(sw, "_search_listing", fake_search)
    monkeypatch.setattr(sw, "_crawl_discover", empty)
    monkeypatch.setattr(sw, "_fetch_discover", empty)
    async def empty_fiche_search(*a, **k):
        return [], 0, 0

    monkeypatch.setattr(sw, "_search_discover", empty_fiche_search)
    monkeypatch.setattr(sw, "_tinyfish_listing_discover", boom_listing)
    monkeypatch.setattr(sw, "_tinyfish_discover", empty)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "llm_judge_listing", quota)

    queued = _run(_queued(sw, HOME))
    assert queued == []
    assert listing_agent["n"] == 0
    msgs = " ".join(e["msg"] for e in sw.logs)
    assert "429" in msgs
    assert "pas d'Agent" in msgs or "quota juge" in msgs


def test_listing_judge_picks_our_programmes_skips_agent(monkeypatch):
    sw = _swarm()
    seen = {"listing_agent": 0, "judge": 0}

    async def empty(*a, **k):
        return []

    async def none(*a, **k):
        return None

    async def fake_tf(query, key, **kw):
        return [
            {"url": "https://example.org/research", "title": "Research"},
            {"url": "https://example.org/our-programmes", "title": "Programmes"},
            {"url": "https://example.org/news/project-launch", "title": "News"},
        ]

    async def fake_sp(query, key, **kw):
        return [{"url": "https://example.org/what-we-do"}]

    async def fake_judge(seed, candidates, **kw):
        seen["judge"] += 1
        urls = []
        for c in candidates:
            urls.append(c.get("url") if isinstance(c, dict) else c)
        assert "https://example.org/our-programmes" in urls
        assert "https://example.org/research" in urls
        assert "https://example.org/news/project-launch" not in urls
        assert len(candidates) <= 5
        return "https://example.org/our-programmes"

    async def boom_listing(*a, **k):
        seen["listing_agent"] += 1
        return None

    async def fake_fiche_crawl(seed, max_urls):
        assert seed["url"] == "https://example.org/our-programmes"
        return ["https://example.org/projects/coral"]

    monkeypatch.setattr(sw, "_crawl_listing", empty)
    monkeypatch.setattr(sw, "_fetch_listing", empty)
    monkeypatch.setattr(sw, "_infer_listing_from_live", none)
    monkeypatch.setattr(sw, "_crawl_discover", fake_fiche_crawl)
    monkeypatch.setattr(sw, "_tinyfish_listing_discover", boom_listing)
    monkeypatch.setattr(sw, "_tinyfish_discover", empty)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)
    monkeypatch.setattr(sp, "llm_judge_listing", fake_judge)

    queued = _run(_queued(sw, HOME))
    assert [i["url"] for i in queued] == ["https://example.org/projects/coral"]
    assert seen == {"listing_agent": 0, "judge": 1}
    assert queued[0]["source"] == "https://example.org/our-programmes"
    remembered = sw.db.master_seeds.docs
    assert any(d.get("url") == "https://example.org/our-programmes" for d in remembered)
    msgs = " ".join(e["msg"] for e in sw.logs)
    assert "Listing juge → https://example.org/our-programmes" in msgs
    assert "TinyFish Agent listing" not in msgs
    assert sw.db.projects.docs == []


def test_listing_leaf_shortcut_skips_judge(monkeypatch):
    sw = _swarm()
    seen = {"judge": 0}

    async def empty(*a, **k):
        return []

    async def none(*a, **k):
        return None

    async def fake_tf(query, key, **kw):
        return [
            {"url": "https://example.org/research"},
            {"url": "https://example.org/projects/"},
        ]

    async def fake_sp(query, key, **kw):
        return []

    async def boom_judge(*a, **k):
        seen["judge"] += 1
        raise AssertionError("leaf shortcut must skip the listing judge")

    async def fake_fiche_crawl(seed, max_urls):
        assert seed["url"] == "https://example.org/projects/"
        return ["https://example.org/projects/coral"]

    monkeypatch.setattr(sw, "_crawl_listing", empty)
    monkeypatch.setattr(sw, "_fetch_listing", empty)
    monkeypatch.setattr(sw, "_infer_listing_from_live", none)
    monkeypatch.setattr(sw, "_crawl_discover", fake_fiche_crawl)
    monkeypatch.setattr(sw, "_tinyfish_listing_discover", empty)
    monkeypatch.setattr(sw, "_tinyfish_discover", empty)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)
    monkeypatch.setattr(sp, "llm_judge_listing", boom_judge)

    queued = _run(_queued(sw, HOME))
    assert [i["url"] for i in queued] == ["https://example.org/projects/coral"]
    assert seen["judge"] == 0


def test_listing_judge_none_then_agent_keeps_our_programmes(monkeypatch):
    sw = _swarm()
    seen = {"judge": 0}

    async def empty(*a, **k):
        return []

    async def none(*a, **k):
        return None

    async def fake_tf(query, key, **kw):
        return [{"url": "https://example.org/research"}]

    async def fake_sp(query, key, **kw):
        return []

    async def fake_judge(*a, **k):
        seen["judge"] += 1
        return None

    async def fake_sse(url, goal, schema, key, **kw):
        if schema is LISTING_SCHEMA or schema.get("required") == ["listing_url"]:
            return {"listing_url": "https://example.org/our-programmes", "title": "P"}
        return {"projects": [
            {"url": "https://example.org/projects/coral", "title": "Coral"},
        ]}

    monkeypatch.setattr(sw, "_crawl_listing", empty)
    monkeypatch.setattr(sw, "_fetch_listing", empty)
    monkeypatch.setattr(sw, "_infer_listing_from_live", none)
    monkeypatch.setattr(sw, "_crawl_discover", empty)
    monkeypatch.setattr(sw, "_fetch_discover", empty)

    async def empty_fiche_search(*a, **k):
        return [], 0, 0

    monkeypatch.setattr(sw, "_search_discover", empty_fiche_search)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", fake_tf)
    monkeypatch.setattr(sp, "serper_search", fake_sp)
    monkeypatch.setattr(sp, "llm_judge_listing", fake_judge)
    monkeypatch.setattr(sp, "tf_run_sse", fake_sse)

    queued = _run(_queued(sw, HOME))
    assert [i["url"] for i in queued] == ["https://example.org/projects/coral"]
    assert seen["judge"] == 1
    msgs = " ".join(e["msg"] for e in sw.logs)
    assert "Listing juge: aucune" in msgs
    assert "TinyFish Agent listing" in msgs
    remembered = sw.db.master_seeds.docs
    assert any(d.get("url") == "https://example.org/our-programmes" for d in remembered)


def test_sse_drop_after_started_polls_same_run(monkeypatch):
    sw = _swarm()
    seen = {"async": 0, "polls": []}

    async def empty(*a, **k):
        return []

    async def none(*a, **k):
        return None

    async def empty_search(*a, **k):
        return [], 0, 0

    async def pool_search(*a, **k):
        return ["https://example.org/what-we-do"], 1, 0

    async def fake_judge(*a, **k):
        return None

    async def fake_sse(url, goal, schema, key, on_event=None, **kw):
        assert "max_steps" not in (kw.get("agent_config") or {})
        if on_event:
            await on_event({"type": "STARTED", "run_id": "run_same_1"})
        raise TimeoutError("SSE stream ended without COMPLETE event")

    async def boom_async(*a, **k):
        seen["async"] += 1
        raise AssertionError("must not start a second TinyFish run")

    async def fake_poll_run(run_id, key, **kw):
        seen["polls"].append(run_id)
        return {"listing_url": "https://example.org/projects/"}

    async def fake_fiche_crawl(seed, max_urls):
        assert seed["url"] == "https://example.org/projects/"
        return ["https://example.org/projects/coral"]

    monkeypatch.setattr(sw, "_crawl_listing", empty)
    monkeypatch.setattr(sw, "_fetch_listing", empty)
    monkeypatch.setattr(sw, "_infer_listing_from_live", none)
    monkeypatch.setattr(sw, "_search_listing", pool_search)
    monkeypatch.setattr(sw, "_crawl_discover", fake_fiche_crawl)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "llm_judge_listing", fake_judge)
    monkeypatch.setattr(sp, "tf_run_sse", fake_sse)
    monkeypatch.setattr(sp, "tf_run_async", boom_async)
    monkeypatch.setattr(sp, "tf_poll_run", fake_poll_run)

    queued = _run(_queued(sw, HOME))
    assert seen["async"] == 0
    assert seen["polls"] == ["run_same_1"]
    assert [i["url"] for i in queued] == ["https://example.org/projects/coral"]
    agent_text = " ".join(
        line for a in sw.agents.values() for line in (a.get("logs") or []))
    assert "même run" in agent_text


def test_sse_wall_clock_cancels_same_run(monkeypatch):
    sw = _swarm()
    seen = {"async": 0, "cancel": []}

    async def empty(*a, **k):
        return []

    async def none(*a, **k):
        return None

    async def empty_search(*a, **k):
        return [], 0, 0

    async def pool_search(*a, **k):
        return ["https://example.org/what-we-do"], 1, 0

    async def fake_judge(*a, **k):
        return None

    async def hang_sse(url, goal, schema, key, on_event=None, **kw):
        if on_event:
            await on_event({"type": "STARTED", "run_id": "run_wall_1"})
        await asyncio.sleep(30)

    async def boom_async(*a, **k):
        seen["async"] += 1
        raise AssertionError("wall clock must not start run-async")

    async def fake_cancel(run_id, key):
        seen["cancel"].append(run_id)
        return {"status": "CANCELLED"}

    async def fake_get(run_id, key):
        return {"status": "CANCELLED"}

    monkeypatch.setattr(sw, "_crawl_listing", empty)
    monkeypatch.setattr(sw, "_fetch_listing", empty)
    monkeypatch.setattr(sw, "_infer_listing_from_live", none)
    monkeypatch.setattr(sw, "_search_listing", pool_search)
    monkeypatch.setattr(sw, "_crawl_discover", empty)
    monkeypatch.setattr(sw, "_fetch_discover", empty)
    monkeypatch.setattr(sw, "_search_discover", empty_search)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "llm_judge_listing", fake_judge)
    monkeypatch.setattr(sp, "tf_run_sse", hang_sse)
    monkeypatch.setattr(sp, "tf_run_async", boom_async)
    monkeypatch.setattr(sp, "tf_cancel_run", fake_cancel)
    monkeypatch.setattr(sp, "tf_get_run", fake_get)
    monkeypatch.setattr(sp, "sse_wall_budget_s", lambda *_: 0.05)

    queued = _run(_queued(sw, HOME))
    assert queued == []
    assert seen["async"] == 0
    assert "run_wall_1" in seen["cancel"]
    agent_text = " ".join(
        line for a in sw.agents.values() for line in (a.get("logs") or []))
    assert "plafond" in agent_text
    assert "pas de 2ᵉ Agent" in agent_text or "pas de 2e Agent" in agent_text


def test_swarm_stop_cancels_live_tinyfish_runs(monkeypatch):
    sw = _swarm()
    sw._tf_live_runs = {"A1": "run_live_1"}
    sw.agents["A1"] = {"id": "A1", "status": "RUNNING", "logs": []}
    seen = []

    async def fake_cancel(run_id, key):
        seen.append(run_id)
        return {"status": "CANCELLED"}

    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_cancel_run", fake_cancel)
    monkeypatch.setattr(sp, "tf_api_key", lambda *a, **k: "k")

    _run(sw.stop())
    assert seen == ["run_live_1"]
    assert sw.agents["A1"]["status"] == "CANCELLED"
    assert sw._tf_live_runs == {}
    assert sw.running is False


def test_sse_429_retries_then_skips_second_run(monkeypatch):
    sw = _swarm()
    seen = {"sse": 0, "async": 0}

    async def empty(*a, **k):
        return []

    async def none(*a, **k):
        return None

    async def empty_search(*a, **k):
        return [], 0, 0

    async def pool_search(*a, **k):
        return ["https://example.org/what-we-do"], 1, 0

    async def fake_judge(*a, **k):
        return None

    async def fake_sse(*a, **k):
        seen["sse"] += 1
        req = httpx.Request("POST", "https://agent.tinyfish.ai/v1/automation/run-sse")
        resp = httpx.Response(429, request=req)
        raise httpx.HTTPStatusError("429", request=req, response=resp)

    async def boom_async(*a, **k):
        seen["async"] += 1
        raise AssertionError("429 must not start run-async")

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(sw, "_crawl_listing", empty)
    monkeypatch.setattr(sw, "_fetch_listing", empty)
    monkeypatch.setattr(sw, "_infer_listing_from_live", none)
    monkeypatch.setattr(sw, "_search_listing", pool_search)
    monkeypatch.setattr(sw, "_crawl_discover", empty)
    monkeypatch.setattr(sw, "_fetch_discover", empty)
    monkeypatch.setattr(sw, "_search_discover", empty_search)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "llm_judge_listing", fake_judge)
    monkeypatch.setattr(sp, "tf_run_sse", fake_sse)
    monkeypatch.setattr(sp, "tf_run_async", boom_async)
    monkeypatch.setattr(sp, "retry_after_s", lambda *a, **k: 0)
    monkeypatch.setattr(sp, "await_agent_cooldown", noop)
    monkeypatch.setattr(sp, "mark_agent_429", noop)

    queued = _run(_queued(sw, HOME))
    assert queued == []
    assert seen["sse"] >= 2
    assert seen["async"] == 0
    agent_text = " ".join(
        line for a in sw.agents.values() for line in (a.get("logs") or []))
    assert "429" in agent_text
    assert "pas de 2ᵉ run" in agent_text or "pas de 2e run" in agent_text or "sauté" in agent_text


def test_publisher_or_missing_host_skips_tinyfish_agents(monkeypatch):
    sw = _swarm()
    seed = {
        "name": "Agency for Meteorology (BMKG) – Indonesia",
        "url": "https://www.nature.com/articles/s41586-bmkg",
        "listing_kind": "homepage",
    }
    assert agent_host_is_worthwhile(seed["url"], seed["name"]) is False

    async def empty(*a, **k):
        return []

    async def none(*a, **k):
        return None

    async def empty_search(*a, **k):
        return [], 0, 0

    async def boom_listing(*a, **k):
        raise AssertionError("listing Agent must not run on a publisher host")

    async def boom_fiche(*a, **k):
        raise AssertionError("fiche Agent must not run on a publisher host")

    monkeypatch.setattr(sw, "_crawl_listing", empty)
    monkeypatch.setattr(sw, "_fetch_listing", empty)
    monkeypatch.setattr(sw, "_infer_listing_from_live", none)
    monkeypatch.setattr(sw, "_search_listing", empty_search)
    monkeypatch.setattr(sw, "_crawl_discover", empty)
    monkeypatch.setattr(sw, "_fetch_discover", empty)
    monkeypatch.setattr(sw, "_search_discover", empty_search)
    monkeypatch.setattr(sw, "_tinyfish_listing_discover", boom_listing)
    monkeypatch.setattr(sw, "_tinyfish_discover", boom_fiche)

    queued = _run(_queued(sw, seed))
    assert queued == []
    msgs = " ".join(e["msg"] for e in sw.logs)
    assert "site officiel introuvable" in msgs
    assert "TinyFish Agent listing" not in msgs or "sauté" in msgs


def test_parse_listing_judge_rejects_invented():
    cands = [
        {"url": "https://example.org/our-programmes", "title": "P"},
        {"url": "https://example.org/research", "title": "R"},
    ]
    ok = parse_listing_judge(
        {"accept": True, "url": "https://example.org/our-programmes"},
        cands, "https://example.org/")
    assert ok == "https://example.org/our-programmes"
    invented = parse_listing_judge(
        {"accept": True, "url": "https://evil.example/invented"},
        cands, "https://example.org/")
    assert invented is None
    home = parse_listing_judge(
        {"accept": True, "url": "https://example.org/"},
        [{"url": "https://example.org/"}], "https://example.org/")
    assert home is None


def test_filtered_search_log_is_not_empty_search(monkeypatch, tmp_path):
    from app.services import run_journal
    monkeypatch.setattr(run_journal, "RUNS_DIR", tmp_path)
    sw = _swarm()
    sw.run_id = "20260912-221000-lst01"

    async def empty(*a, **k):
        return []

    async def news_only(query, key, **kw):
        return [{"url": "https://example.org/news/project-launch"}]

    async def fake_agent(*a, **k):
        return ["https://example.org/projects/from-agent"]

    monkeypatch.setattr(sw, "_crawl_discover", empty)
    monkeypatch.setattr(sw, "_fetch_discover", empty)
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "tf_search", news_only)
    monkeypatch.setattr(sp, "serper_search", news_only)
    monkeypatch.setattr(sw, "_tinyfish_discover", fake_agent)

    queued = _run(_queued(sw, LISTING))
    assert [i["url"] for i in queued] == ["https://example.org/projects/from-agent"]
    packed = run_journal.read_journal_file(sw.run_id)
    text = " ".join(e["msg"] for e in packed["items"])
    assert "hits filtrés (0 fiches) → TinyFish Agent fiches" in text
    assert "Search vide → TinyFish Agent" not in text
    assert packed["wrote_projects"] is False


def _drain_worker(sw, processed=None):
    processed = processed if processed is not None else []

    async def worker(idx):
        while True:
            try:
                item = await asyncio.wait_for(sw.queue.get(), timeout=0.2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                return
            try:
                processed.append(item.get("url"))
            finally:
                try:
                    sw.queue.task_done()
                except ValueError:
                    pass
    return worker


def test_full_from_scratch_does_not_dump_deeplink_cache(monkeypatch):
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "MASTER_SEEDS", [])
    monkeypatch.setattr(sp, "seeds_for_run", lambda seeds: [])
    sw = Swarm(_FakeDB())
    sw.running = True
    sw.mode = "full"
    sw.force_rescan = True
    sw.settings = {
        "discover_concurrency": 1,
        "extract_concurrency": 1,
        "full_max_urls_per_seed": 1,
        "tinyfish_agents": 1,
        "allow_tinyfish_agent": False,
    }
    sw.db.deeplink_pages.docs = [
        {"url": "https://cached.org/projects/old", "funder": "X", "source": "cache"},
    ]
    monkeypatch.setattr(sw, "_extract_worker", _drain_worker(sw))

    _run(sw._run())
    msgs = " ".join(e["msg"] for e in sw.logs)
    assert "DeepLinkCache non déversé" in msgs
    assert "not yet extracted → queued" not in msgs
    assert sw.wrote_projects is False
    assert sw.db.projects.docs == []


def test_full_incremental_still_dumps_fresh_cache(monkeypatch):
    import app.services.swarm_pipeline as sp
    monkeypatch.setattr(sp, "MASTER_SEEDS", [])
    monkeypatch.setattr(sp, "seeds_for_run", lambda seeds: [])
    processed = []
    sw = Swarm(_FakeDB())
    sw.running = True
    sw.mode = "full"
    sw.force_rescan = False
    sw.run_id = "20260912-221100-inc01"
    sw.settings = {
        "discover_concurrency": 1,
        "extract_concurrency": 1,
        "full_max_urls_per_seed": 1,
        "tinyfish_agents": 1,
        "allow_tinyfish_agent": False,
    }
    sw.db.deeplink_pages.docs = [
        {"url": "https://cached.org/projects/old", "funder": "X", "source": "cache"},
        {"url": "https://cached.org/projects/live", "funder": "X", "source": "cache"},
    ]
    sw.db.projects.docs = [{"url": "https://cached.org/projects/live"}]
    monkeypatch.setattr(sw, "_extract_worker", _drain_worker(sw, processed))

    _run(sw._run())
    assert processed == ["https://cached.org/projects/old"]
    msgs = " ".join(e["msg"] for e in sw.logs)
    assert "not yet extracted → queued" in msgs
    assert sw.db.projects.docs == [{"url": "https://cached.org/projects/live"}]
