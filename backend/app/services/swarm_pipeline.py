import asyncio
import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.core.llm import (
    extract_project, gatekeeper_check, has_llm, s_ocean_meets_min,
)
from app.static_data.categories import normalize_category
from app.core.dedup import is_duplicate
from app.core.extract import extract_cascade, path_looks_like_image, url_looks_like_image
from app.core.public_url import PublicFetchError, fetch_checked, public_url_error
from app.core.project_geo import geocode_project_site, site_publishable, valid_coords
from app.core.rag import select_context
from app.static_data.seeds import (
    CRAWL_BLACKLIST, CURATED_SEEDS, MASTER_SEEDS, TEST_SEED_COUNT, URL_PATTERNS,
)
from app.services.master_seeds import (
    SKIP_LISTING_NETLOCS, domain_matches_org, domain_of, is_known_funder,
    is_publisher_host, is_shared_hub, name_owns_hub, needs_official_home,
    norm_name, official_site_query, official_site_retry_query,
    prefer_official_home, seeds_for_run,
)
from app.services.seed_catalog import (
    FTM_DISCOVER, FTM_SEARCH, FTM_MIN_S_OCEAN, decide_follow_the_money_partner,
    find_catalog_seed, ftm_page_allows_collect, is_crawl_ready,
    partner_site_reachable, rank_ftm_inbox,
)
from app.core.tinyfish import (
    DISCOVERY_SCHEMA, FICHE_AGENT_DURATION_S, LISTING_AGENT_DURATION_S,
    LISTING_SCHEMA, POLL_GRACE_S, PROJECTS_DISCOVERY_PURPOSE,
    PROJECTS_LISTING_PURPOSE, discovery_goal, listing_goal, find_live_url,
    await_agent_cooldown, is_http_status, is_sse_stream_end, mark_agent_429,
    public_agent_config, retry_after_s, sse_wall_budget_s, tf_api_key,
    tf_cancel_run, tf_fetch, tf_get_run, tf_poll_run, tf_run_async, tf_run_sse,
    tf_search,
)
from app.services.project_listing import (
    LISTING_JUDGE_CAP, ListingJudgeQuotaError, accept_listing_url,
    apply_learned_listings, fiche_search_retry_query, hygiene_listing_urls,
    infer_listing_from_project_urls, is_listing_url, listing_search_query,
    listing_search_retry_query, llm_judge_listing, merge_listing_candidates,
    needs_listing_hop, pick_listing_url,
)
from app.core.serper import serper_api_key, serper_search
from app.core.events import HeartbeatWatch
from app.services.run_journal import (
    AGENT_LIVE_TAIL,
    JOURNAL_KIND_AGENT,
    JOURNAL_KIND_LOG,
    JOURNAL_KIND_META,
    append_journal,
    header_summary,
    mongo_insert_journal,
    public_run_params,
)

logger = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def pick_image(full_soup, content_soup, base_url):
    for attrs in ({"property": "og:image"}, {"name": "twitter:image"}, {"property": "twitter:image"}):
        m = full_soup.find("meta", attrs=attrs)
        if m and m.get("content", "").strip():
            return urljoin(base_url, m["content"].strip())
    for img in content_soup.find_all("img", src=True):
        src = img["src"].strip()
        low = src.lower()
        if src.startswith("data:") or low.endswith(".svg"):
            continue
        if any(b in low for b in ("logo", "icon", "sprite", "avatar", "placeholder", "pixel")):
            continue
        return urljoin(base_url, src)
    return None


def _hit_url(hit) -> str:
    if isinstance(hit, str):
        return hit.strip()
    if isinstance(hit, dict):
        return (hit.get("url") or hit.get("link") or hit.get("href") or "").strip()
    return ""


def _is_skip_listing_domain(domain: str) -> bool:
    d = (domain or "").lower()
    if not d:
        return True
    for skip in SKIP_LISTING_NETLOCS:
        if d == skip or d.endswith("." + skip):
            return True
    return False


def is_project_fiche_path(path: str, *, apply_blacklist: bool = True) -> bool:
    """Vraie fiche : motif URL_PATTERNS, ≥ 2 segments. Blacklist = Fetch/Search."""
    if not path:
        return False
    if path_looks_like_image(path):
        return False
    if apply_blacklist and any(b in path.lower() for b in CRAWL_BLACKLIST):
        return False
    if not any(p in path for p in URL_PATTERNS):
        return False
    parts = [p for p in path.strip("/").split("/") if p]
    return len(parts) >= 2


def project_search_query(seed: dict) -> str:
    """One sentence: site:{domain} … ocean project, or "{name}" marine conservation."""
    name = (seed.get("name") or "").strip() if isinstance(seed, dict) else ""
    url = (seed.get("url") or "").strip() if isinstance(seed, dict) else ""
    domain = domain_of(url)
    if domain:
        if name:
            return f"site:{domain} {name} ocean project"
        return f"site:{domain} ocean project"
    if name:
        return f'"{name}" marine conservation'
    return "ocean project"


def filter_discover_urls(hits, seed, max_urls, *, exclude_urls=None) -> list[str]:
    """Same host (or found domain), URL_PATTERNS, off blacklist, http, dedup."""
    try:
        cap = max(0, int(max_urls or 0))
    except (TypeError, ValueError):
        cap = 0
    excluded = {u.rstrip("/") for u in (exclude_urls or []) if u}
    host = domain_of((seed or {}).get("url") or "") if isinstance(seed, dict) else ""
    seed_url = ""
    if isinstance(seed, dict):
        seed_url = (seed.get("url") or "").rstrip("/")
    urls, seen = [], set()
    for hit in hits or []:
        raw = _hit_url(hit)
        if not raw.startswith("http"):
            continue
        href = raw.split("#")[0].split("?")[0]
        if not href.startswith("http"):
            continue
        key = href.rstrip("/")
        if key in excluded:
            continue
        if seed_url and key == seed_url:
            continue
        d = domain_of(href)
        if host:
            if d != host:
                continue
        elif _is_skip_listing_domain(d):
            continue
        if url_looks_like_image(href):
            continue
        path = urlparse(href).path
        if href in seen:
            continue
        if is_project_fiche_path(path, apply_blacklist=True):
            seen.add(href)
            urls.append(href)
            if cap and len(urls) >= cap:
                break
    if urls:
        return urls[:cap]
    # Same host, deep page, not news/donate: Scripps /research/… has no /project/.
    for hit in hits or []:
        raw = _hit_url(hit)
        if not raw.startswith("http"):
            continue
        href = raw.split("#")[0].split("?")[0]
        key = href.rstrip("/")
        if key in excluded or (seed_url and key == seed_url):
            continue
        d = domain_of(href)
        if host and d != host:
            continue
        if not host and _is_skip_listing_domain(d):
            continue
        if url_looks_like_image(href):
            continue
        path = urlparse(href).path
        if not _is_soft_fiche_path(path):
            continue
        if href in seen:
            continue
        seen.add(href)
        urls.append(href)
        if cap and len(urls) >= cap:
            break
    return urls[:cap]


def _is_soft_fiche_path(path: str) -> bool:
    """Usable internal page if no /project/ pattern matched."""
    if not path:
        return False
    low = path.lower()
    if path_looks_like_image(path):
        return False
    if any(b in low for b in CRAWL_BLACKLIST):
        return False
    parts = [p for p in path.strip("/").split("/") if p]
    return len(parts) >= 2


def official_site_from_hits(hits, funder_name: str = "") -> str:
    """The organization's real site: name token (or acronym) in the domain.

    Never the first leftover SERP hit (BMKG ≠ nature.com).
    Journals / publishers and shared hubs excluded. Nothing of our own → empty.
    """
    scored: list[tuple[int, int, str]] = []
    for i, hit in enumerate(hits or []):
        raw = _hit_url(hit)
        if not raw.startswith("http"):
            continue
        href = raw.split("#")[0].split("?")[0]
        d = domain_of(href)
        if _is_skip_listing_domain(d) or is_publisher_host(d):
            continue
        parsed = urlparse(href)
        if not parsed.netloc:
            continue
        if not domain_matches_org(href, funder_name):
            continue
        scheme = parsed.scheme or "https"
        scored.append((2, -i, f"{scheme}://{parsed.netloc}/"))
    if not scored:
        return ""
    scored.sort(reverse=True)
    return scored[0][-1]


def agent_host_is_worthwhile(url: str, name: str) -> bool:
    """The TinyFish Agent only starts if the host is (probably) the organization."""
    if not (url or "").strip():
        return False
    d = domain_of(url)
    if not d or _is_skip_listing_domain(d) or is_publisher_host(d):
        return False
    return domain_matches_org(url, name)


def _links_from_fetch_record(rec: dict | None, base_url: str) -> list[str]:
    out = []
    for link in (rec or {}).get("links") or []:
        if isinstance(link, dict):
            raw = (link.get("url") or link.get("href") or "").strip()
        else:
            raw = str(link or "").strip()
        if not raw:
            continue
        href = urljoin(base_url, raw).split("#")[0].split("?")[0]
        if href.startswith("http") and not url_looks_like_image(href):
            out.append(href)
    return out


class Swarm:
    def __init__(self, db):
        self.db = db
        self.running = False
        self.mode = "test"
        self.logs = deque(maxlen=4000)
        self.agents = {}
        self.agent_seq = 0
        self.queue = None
        self.main_task = None
        self.workers = []
        self.settings = {}
        self.queued_count = 0
        self.recursive_tasks = []
        self.partner_domains = set()
        self.partner_count = 0
        self.new_partner_count = 0
        self.ftm_inbox: list[dict] = []
        self.master_seeds = []
        self._tf_sem = None
        self._judge_sem = None
        self._serper_sem = None
        self.no_new_streak = 0
        self.saturated = False
        self.run_id = None
        self.recorder = None
        self.force_rescan = False
        self.wrote_projects = False
        self._journal_seq = 0
        self._serper_queries = 0
        self._tf_live_runs: dict[str, str] = {}

    # ---------- state helpers ----------
    def log(self, msg, level="info"):
        entry = {"ts": now_iso(), "msg": msg, "level": level, "kind": JOURNAL_KIND_LOG}
        self.logs.append(entry)
        self._persist_journal(entry)

    def _persist_journal(self, entry: dict):
        """Write the full narrative (file + Mongo). The live map is not touched."""
        rid = self.run_id
        if not rid:
            return
        self._journal_seq += 1
        try:
            rec = append_journal(rid, entry, seq=self._journal_seq)
        except Exception as exc:
            logger.warning("swarm journal append failed run_id=%s: %s", rid, exc)
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(mongo_insert_journal(self.db, rec))
        except RuntimeError:
            pass

    async def _write_journal_header(self):
        """First journal line: parameters + rules. After seq reset."""
        rid = self.run_id
        if not rid:
            return
        params = {}
        try:
            doc = await self.db.project_runs.find_one({"_id": rid}, {"params": 1})
            if doc and isinstance(doc.get("params"), dict):
                params = doc["params"]
        except Exception as exc:
            logger.warning("journal header load failed run_id=%s: %s", rid, exc)
        pub = public_run_params(params)
        self._persist_journal({
            "kind": JOURNAL_KIND_META,
            "level": "info",
            "msg": header_summary(pub),
            "params": params,
            "profile": pub.get("profile"),
            "hash": pub.get("hash"),
        })

    def new_agent(self, engine, mode, url, source=""):
        self.agent_seq += 1
        aid = f"A{self.agent_seq:03d}"
        self.agents[aid] = {
            "id": aid, "engine": engine, "mode": mode, "url": url, "source": source,
            "status": "PENDING", "live_url": None, "logs": [], "ts": now_iso(),
        }
        done = [k for k, a in self.agents.items() if a["status"] not in ("PENDING", "RUNNING")]
        while len(self.agents) > 40 and done:
            del self.agents[done.pop(0)]
        return aid

    def agent_log(self, aid, msg):
        a = self.agents.get(aid)
        clock = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{clock}] {msg}"
        if a:
            a["logs"].append(line)
            a["logs"] = a["logs"][-AGENT_LIVE_TAIL:]
        self._persist_journal({
            "ts": now_iso(),
            "kind": JOURNAL_KIND_AGENT,
            "level": "info",
            "msg": msg,
            "agent": aid,
            "engine": (a or {}).get("engine"),
            "source": (a or {}).get("source"),
            "url": (a or {}).get("url"),
            "status": (a or {}).get("status"),
        })

    def set_agent(self, aid, **kw):
        a = self.agents.get(aid)
        if a:
            a.update(kw)

    async def telemetry(self, url, engine, status, duration_ms, results=0, detail=""):
        await self.db.telemetry.insert_one({
            "_id": str(uuid.uuid4()), "url": url, "engine": engine, "status": status,
            "duration_ms": int(duration_ms), "results": results, "detail": str(detail)[:500],
            "ts": now_iso(), "dataset": "projects", "run_id": self.run_id,
        })

    async def add_failed(self, url, source, funder, reason, stage):
        await self.db.failed.update_one(
            {"url": url},
            {"$set": {"url": url, "source": source, "funder": funder,
                      "reason": str(reason)[:300], "stage": stage, "ts": now_iso(),
                      "dataset": "projects", "run_id": self.run_id},
             "$setOnInsert": {"_id": str(uuid.uuid4())}},
            upsert=True,
        )

    async def _emit(self, step: str, **payload):
        rec = self.recorder
        if rec is not None:
            try:
                await rec.event(step, **payload)
            except Exception as exc:
                logger.warning("swarm recorder.event failed step=%s: %s", step, exc)

    async def _bump_run(self, key: str):
        if not self.run_id:
            return
        from app.services.project_runs import bump_counter
        try:
            await bump_counter(self.db, self.run_id, key)
        except Exception:
            pass

    async def _ensure_isolated_run(self, *, mode: str, settings: dict | None = None,
                                   force_rescan: bool = False, label: str = ""):
        """Open a project_run_* if none is already attached. Does not write `projects`."""
        if self.run_id:
            return self.run_id
        from app.services.project_runs import open_run
        opened = await open_run(
            self.db, mode=mode, label=label or f"projects-{mode}",
            settings=settings or self.settings, force_rescan=force_rescan)
        self.run_id = opened["run_id"]
        self.recorder = opened["recorder"]
        self.wrote_projects = False
        return self.run_id

    async def _write_verdict(self, item: dict, verdict: str, **fields):
        """Persist a run row. Forbids any write into `projects`."""
        if not self.run_id:
            raise RuntimeError("isolated run required — no write to projects")
        from app.services.project_runs import COUNTER_FOR_VERDICT, write_run_project
        url = item["url"]
        funder = item.get("funder") or fields.get("funder") or ""
        doc = {
            "url": url,
            "title": fields.pop("title", None) or url,
            "funder": funder,
            "funders": fields.pop("funders", None) or ([funder] if funder else []),
            "verdict": verdict,
            **fields,
        }
        await write_run_project(self.db, self.run_id, doc)
        key = COUNTER_FOR_VERDICT.get(verdict)
        if key:
            await self._bump_run(key)
        await self._emit("project_verdict", url=url, verdict=verdict, title=doc.get("title"))
        return doc

    def status(self):
        agents = list(self.agents.values())[::-1][:20]
        rid = self.run_id
        return {
            "running": self.running,
            "mode": self.mode,
            "llm": has_llm(self.settings),
            "tinyfish": bool(self._tf_key()),
            "active": sum(1 for a in self.agents.values() if a["status"] in ("PENDING", "RUNNING")),
            "queued": self.queued_count,
            "agents": agents,
            "logs": list(self.logs)[-200:],
            "run_id": rid,
            "wrote_projects": False,
            "journal_lines": self._journal_seq,
            "journal_url": f"/api/projects/runs/{rid}/journal" if rid else None,
        }

    def _tf_key(self):
        return (self.settings.get("tinyfish_api_key") or os.environ.get("TINYFISH_API_KEY") or "").strip()

    def _serper_key(self):
        return serper_api_key(self.settings)

    # ---------- lifecycle ----------
    def _bump_saturation(self, new_project: bool):
        # From scratch re-extracts URLs already on the live map: many
        # unlocated / merged in a row. Global Auto-Stop (designed for an
        # exhausted listing) would cut a world run after ~50 extracts.
        if getattr(self, "force_rescan", False):
            return
        if new_project:
            self.no_new_streak = 0
            return
        self.no_new_streak += 1
        limit = int(self.settings.get("saturation_limit", 50))
        if limit > 0 and self.no_new_streak >= limit and self.running and not self.saturated:
            self.saturated = True
            self.log(f"Auto-Stop: {self.no_new_streak} consecutive extractions without a new unique project — graceful shutdown to save credits", "warn")
            asyncio.create_task(self.stop())

    async def deploy(self, mode: str, clear_db: bool, settings: dict, force_rescan: bool = False,
                     run_id: str | None = None, recorder=None):
        if self.running:
            raise ValueError("swarm already running")
        self.settings = settings
        self.mode = mode
        self.force_rescan = force_rescan
        if clear_db:
            raise ValueError("clear_db is disabled")
        if run_id:
            self.run_id = run_id
            self.recorder = recorder
        else:
            await self._ensure_isolated_run(
                mode=mode, settings=settings, force_rescan=force_rescan)
        self.wrote_projects = False
        self.running = True
        self.logs.clear()
        self._journal_seq = 0
        self._serper_queries = 0
        self._tf_live_runs = {}
        self.no_new_streak = 0
        self.saturated = False
        await self._write_journal_header()
        self.log(f"Isolated run {self.run_id} — writes project_run_* only (wrote_projects: false)")
        self.log(
            f"Deploying Swarm — mode: {mode.upper()} | home→catalogue puis fiches | "
            f"N1 → Fetch → Search (Serper ∥ TinyFish) → Agent listing | Agent fiches "
            f"(wrote_projects: false)"
        )
        if force_rescan:
            self.log("Auto-Stop disarmed — from scratch (known URLs are re-extracted)")
        else:
            self.log(
                f"Auto-Stop armed: shutdown after "
                f"{int(settings.get('saturation_limit', 50))} extractions without new project"
            )
        self.log(f"TinyFish key: {'ACTIVE (N3 last-resort only)' if self._tf_key() else 'MISSING → N1/N2 only'}",
                 "info")
        self.log(f"LLM pipeline: {'ACTIVE' if has_llm(settings) else 'MISSING → heuristic gatekeeper/extractor'}",
                 "info" if has_llm(settings) else "warn")
        self.main_task = asyncio.create_task(self._run())

    def _tf_track_run(self, aid, rid):
        if rid:
            self._tf_live_runs[aid] = rid

    def _tf_untrack_run(self, aid):
        self._tf_live_runs.pop(aid, None)

    async def _tf_cancel_live_runs(self):
        """POST /v1/runs/{id}/cancel — else a stopped Complet leaves PENDING runs."""
        live = list(self._tf_live_runs.items())
        if not live:
            return
        key = tf_api_key(self.settings)
        if not key:
            self._tf_live_runs.clear()
            return
        for aid, rid in live:
            try:
                await tf_cancel_run(rid, key)
                self.agent_log(aid, f"TinyFish run {str(rid)[:8]}… cancel (stop)")
            except Exception as exc:
                logger.warning("tf cancel on stop failed run=%s: %s", rid, exc)
        self._tf_live_runs.clear()

    async def stop(self):
        self.log("Stop requested — cancelling agents and flushing queue", "warn")
        for a in self.agents.values():
            if a["status"] in ("PENDING", "RUNNING"):
                a["status"] = "CANCELLED"
        await self._tf_cancel_live_runs()
        if self.main_task:
            self.main_task.cancel()
        for w in self.workers:
            w.cancel()
        self.workers = []
        if self.queue:
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                    self.queue.task_done()
                except asyncio.QueueEmpty:
                    break
        self.queued_count = 0
        self.running = False
        self.log("Swarm stopped")

    async def _run(self):
        cancelled = False
        error = None
        hb = HeartbeatWatch(self.recorder).start(lambda: {
            "progress": self.queued_count,
            "active": sum(1 for a in self.agents.values()
                          if a["status"] in ("PENDING", "RUNNING")),
            "last_item": next(
                (a.get("url") for a in reversed(list(self.agents.values()))
                 if a.get("status") == "RUNNING"),
                None,
            ),
        })
        try:
            extras = []
            try:
                extras = await self.db.master_seeds.find({}).to_list(2000)
            except Exception:
                extras = []
            self.master_seeds = [
                prefer_official_home(s)
                for s in apply_learned_listings(list(MASTER_SEEDS), extras) + [
                    e for e in extras
                    if (e.get("url") or e.get("name"))
                    and not is_known_funder(MASTER_SEEDS, e.get("name"), e.get("url"))
                ]
            ]
            if self.mode == "test":
                seeds = list(CURATED_SEEDS[:TEST_SEED_COUNT])
            else:
                seeds = seeds_for_run(self.master_seeds)
            max_urls = int(self.settings.get("test_max_urls_per_seed", 6)) if self.mode == "test" \
                else int(self.settings.get("full_max_urls_per_seed", 20))
            self.queue = asyncio.Queue()
            self.recursive_tasks = []
            self.partner_domains = set()
            for s in seeds:
                d = domain_of(s.get("url"))
                if not d:
                    continue
                if is_shared_hub(d) and not name_owns_hub(s.get("name") or "", d):
                    continue
                self.partner_domains.add(d)
            self.partner_count = 0
            self.new_partner_count = 0
            self.ftm_inbox = []
            from app.core.run_rules import get_rule
            from app.core import nvidia as nvidia_core
            concurrency = max(1, min(20, int(get_rule(
                "projects.extract_concurrency",
                self.settings.get("extract_concurrency", 2)))))
            discover_n = max(1, min(12, int(self.settings.get("discover_concurrency", 8))))
            judge_n = max(1, min(4, int(get_rule(
                "projects.listing_judge_concurrency",
                self.settings.get("listing_judge_concurrency", 1)))))
            nv_n = max(1, min(4, int(get_rule(
                "projects.nvidia_max_concurrency",
                self.settings.get("nvidia_max_concurrency", 2)))))
            nvidia_core.configure_concurrency(nv_n)
            self.workers = [asyncio.create_task(self._extract_worker(i)) for i in range(concurrency)]
            self.log(
                f"MasterSeeds loaded: {len(seeds)} portals in queue "
                f"(catalog={len(self.master_seeds)}, "
                f"discover_concurrency={discover_n}, "
                f"listing_judge_concurrency={judge_n}, "
                f"extract_concurrency={concurrency}, "
                f"nvidia_max_concurrency={nv_n})"
            )

            if self.mode == "full" and not getattr(self, "force_rescan", False):
                cached = await self.db.deeplink_pages.find({}).to_list(5000)
                if cached:
                    existing = {p["url"] for p in await self.db.projects.find({}, {"url": 1}).to_list(30000)}
                    fresh = [c for c in cached if c["url"] not in existing]
                    resolutions: dict[str, str | None] = {}
                    queued_pages = []
                    for c in fresh:
                        reason = public_url_error(c["url"], resolve_cache=resolutions)
                        if reason:
                            self.log(
                                "DeepLinkCache: skip non-public URL "
                                f"(not fetched, kept) {c['url'][:180]} — {reason}",
                                "warn",
                            )
                            continue
                        queued_pages.append(c)
                    self.log(
                        f"DeepLinkCache: {len(cached)} pages cached, "
                        f"{len(queued_pages)} not yet extracted → queued"
                    )
                    for c in queued_pages:
                        self.queued_count += 1
                        await self.queue.put({"url": c["url"], "funder": c.get("funder", ""), "source": c.get("source", "cache")})
            elif self.mode == "full" and getattr(self, "force_rescan", False):
                self.log("from scratch — DeepLinkCache non déversé (redécouverte via catalogues)")

            tf_agents = max(1, min(2, int(get_rule(
                "projects.tinyfish_agents",
                self.settings.get("tinyfish_agents", 2)))))
            self._tf_sem = asyncio.Semaphore(tf_agents)
            self._judge_sem = asyncio.Semaphore(judge_n)
            self._serper_sem = asyncio.Semaphore(2)
            discover_sem = asyncio.Semaphore(discover_n)

            async def guarded(seed):
                async with discover_sem:
                    await self._discover(seed, max_urls)

            await asyncio.gather(*[guarded(s) for s in seeds], return_exceptions=True)
            self.log("Discovery phase complete — waiting for extraction queue to drain")
            await self._wait_extract_and_recursive()
            if self.ftm_inbox and self.settings.get("follow_the_money", True):
                self.log(
                    f"Follow the Money: phase inbox — {len(self.ftm_inbox)} mention(s)"
                )
                await self._flush_ftm_inbox()
                await self._wait_extract_and_recursive()
            for w in self.workers:
                w.cancel()
            self.workers = []
            n_sites = 0
            if self.run_id:
                n_sites = await self.db.project_run_projects.count_documents(
                    {"run_id": self.run_id, "verdict": "site"})
            self.log(
                f"Pipeline complete — {n_sites} sites in run {self.run_id} "
                f"(wrote_projects: false)",
                "success",
            )
        except asyncio.CancelledError:
            cancelled = True
        except Exception as e:
            error = str(e)
            self.log(f"Pipeline error: {e}", "error")
        finally:
            from app.services.project_runs import finalize_run
            try:
                await finalize_run(
                    self.db, self.run_id, cancelled=cancelled, error=error)
            except Exception:
                pass
            await hb.aclose()
            self.running = False

    # ---------- discovery (home→catalogue, puis N1 → Fetch → Search → Agent fiches) ----------
    async def _wait_extract_and_recursive(self):
        """Extraction queue + FTM discoveries already launched."""
        while True:
            await self.queue.join()
            pending = [t for t in self.recursive_tasks if not t.done()]
            if not pending:
                break
            self.log(
                f"Follow the Money: waiting on {len(pending)} recursive discovery agent(s)"
            )
            await asyncio.gather(*pending, return_exceptions=True)

    async def _discover(self, seed, max_urls, depth=0):
        seed = prefer_official_home(dict(seed or {}))
        queue = (seed.get("queue") or "").strip().lower()
        if queue in {"resolve", "skip"}:
            self.log(
                f"[{seed.get('name')}] skipped — file {queue} (pas d'Agent)",
                "warn",
            )
            return 0
        classified = bool(seed.get("home_status") or seed.get("queue"))
        status = (seed.get("home_status") or "").strip().lower()
        if classified and status in {
            "borrowed_hub", "publisher", "social", "empty", "unknown",
        }:
            self.log(
                f"[{seed.get('name')}] skipped — home_status={status}",
                "warn",
            )
            return 0
        if needs_official_home(seed):
            if classified:
                self.log(
                    f"[{seed.get('name')}] skipped — home déjà classée, pas de Search live",
                    "warn",
                )
                return 0
            home = await self._resolve_official_home(seed)
            if home:
                seed["url"] = home
                seed["listing_kind"] = "homepage"
            else:
                return 0
        start_url = (seed.get("url") or "").strip()
        if not start_url:
            self.log(f"[{seed.get('name')}] skipped — no listing URL", "warn")
            return 0
        await self._emit("discover_seed", seed=seed.get("name"), url=start_url,
                         depth=depth)
        force = bool(getattr(self, "force_rescan", False))
        if depth == 0:
            state = await self.db.discovery_state.find_one({"seed_url": start_url})
            rescan_days = float(self.settings.get("rescan_after_days", 7))
            if state and state.get("last_scan") and not force:
                try:
                    last = datetime.fromisoformat(state["last_scan"])
                    age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
                    if age_days < rescan_days:
                        self.log(
                            f"[{seed['name']}] discovery skipped — scanned {age_days:.1f}d ago "
                            f"(TTL {rescan_days:g}d, cache reused)"
                        )
                        return 0
                except ValueError:
                    pass
            if force:
                self.log(f"[{seed['name']}] from scratch — TTL et URLs déjà connues non sautés")

        if needs_listing_hop(seed):
            found = await self._resolve_listing(seed)
            if found:
                await self._remember_listing(seed, found)
                seed["url"] = found
                seed["listing_kind"] = "projects_index"
                self.log(f"[{seed['name']}] catalogue: {found}")
            else:
                self.log(f"[{seed['name']}] catalogue introuvable — fiches depuis la home")

        known_urls = []
        if depth == 0 and not force:
            try:
                cached = await self.db.deeplink_pages.find(
                    {"$or": [{"source": start_url}, {"source": seed["url"]}]},
                    {"url": 1},
                ).to_list(300)
            except Exception:
                cached = await self.db.deeplink_pages.find(
                    {"source": seed["url"]}, {"url": 1}).to_list(300)
            known_urls = [c["url"] for c in cached if c.get("url")]
            if known_urls:
                self.log(
                    f"[{seed['name']}] delta scan — {len(known_urls)} known URLs excluded from mission"
                )
        return await self._discover_fiches(seed, max_urls, depth, known_urls)

    async def _resolve_official_home(self, seed):
        """Shared hub or no URL → the organization's real site, then listing hop."""
        name = (seed.get("name") or "").strip()
        if not name:
            self.log("[?] site officiel — nom manquant", "warn")
            return None
        aid = self.new_agent("Site officiel", "listing", seed.get("url") or "", name)
        t0 = time.time()
        self.set_agent(aid, status="RUNNING", engine="Search site officiel")
        log_fn = lambda m: self.agent_log(aid, m)
        search_seed = {"name": name, "url": ""}
        query = official_site_query(name)
        retry = official_site_retry_query(name)
        self.agent_log(aid, f"Search: {query}")
        try:
            tf_hits, sp_hits = await asyncio.gather(
                self._tf_search_discover(query, search_seed, log=log_fn),
                self._serper_discover(query, log=log_fn),
            )
            hits = list(tf_hits or []) + list(sp_hits or [])
            site = official_site_from_hits(hits, funder_name=name)
            if not site and retry:
                self.agent_log(aid, f"Search retry: {retry}")
                self.log(f"[{name}] Search: official site 0 — retry acronyme")
                tf2, sp2 = await asyncio.gather(
                    self._tf_search_discover(retry, search_seed, log=log_fn),
                    self._serper_discover(retry, log=log_fn),
                )
                hits = list(tf2 or []) + list(sp2 or []) + hits
                site = official_site_from_hits(hits, funder_name=name)
            if site:
                self.set_agent(aid, status="SUCCESS")
                self.agent_log(aid, f"site officiel → {site}")
                self.log(f"[{name}] site officiel: {site}")
                await self.telemetry(
                    seed.get("url") or name, "Official site", "SUCCESS",
                    (time.time() - t0) * 1000, 1)
                return site
            self.set_agent(aid, status="FAILED")
            self.agent_log(aid, "site officiel introuvable")
            self.log(f"[{name}] site officiel introuvable — hub ignoré", "warn")
            await self.telemetry(
                seed.get("url") or name, "Official site", "FAILED",
                (time.time() - t0) * 1000, 0, "official site not found")
            return None
        except asyncio.CancelledError:
            self.set_agent(aid, status="CANCELLED")
            raise
        except Exception as e:
            self.set_agent(aid, status="FAILED")
            self.agent_log(
                aid, f"site officiel échec: {type(e).__name__}: {str(e)[:80]}")
            self.log(f"[{name}] site officiel échec: {type(e).__name__}", "warn")
            return None

    async def _resolve_listing(self, seed):
        """Home → one catalog URL. Does not write cards. No run failure if 0."""
        name = seed.get("name") or seed.get("url")
        key = self._tf_key()
        aid = self.new_agent("Listing N1", "listing", seed["url"], seed["name"])
        t0 = time.time()
        found = None
        used_engine = "Crawler"
        n_tf = n_sp = 0
        try:
            self.set_agent(aid, status="RUNNING")
            crawled = fetched = searched = []
            pool: list[str] = []
            judge_quota = False
            self.agent_log(aid, "Listing N1: crawler (hygiène + feuilles curées)")
            try:
                crawled = await self._crawl_listing(seed, LISTING_JUDGE_CAP)
            except Exception as e:
                crawled = []
                self.agent_log(aid, f"Listing N1 échec: {type(e).__name__}: {str(e)[:80]}")
            found = pick_listing_url(crawled)
            self.agent_log(aid, f"Listing N1: {len(crawled)} candidat(s)")
            self.log(f"[{name}] Listing N1: {len(crawled)} candidat(s)")

            if not found:
                inferred = await self._infer_listing_from_live(seed)
                if inferred:
                    found = inferred
                    used_engine = "indice v1"
                    self.agent_log(aid, f"indice v1 → {found}")
                    self.log(f"[{name}] Listing indice v1: {found}")

            if not found and key:
                used_engine = "TinyFish Fetch"
                self.set_agent(aid, engine="Listing Fetch")
                log_fn = lambda m: self.agent_log(aid, m)
                try:
                    fetched = await self._fetch_listing(seed, log=log_fn)
                except Exception as e:
                    fetched = []
                    self.agent_log(aid, f"Listing Fetch échec: {type(e).__name__}: {str(e)[:80]}")
                found = pick_listing_url(fetched)
                self.agent_log(aid, f"Listing Fetch: {len(fetched)} candidat(s)")
                self.log(f"[{name}] Listing Fetch: {len(fetched)} candidat(s)")
            elif not found:
                self.agent_log(aid, "Listing Fetch: skipped (no TinyFish key)")

            if not found:
                used_engine = "Search"
                self.set_agent(aid, engine="Listing Search TF∥Serper")
                log_fn = lambda m: self.agent_log(aid, m)
                try:
                    searched, n_tf, n_sp = await self._search_listing(seed, log=log_fn)
                except Exception as e:
                    searched, n_tf, n_sp = [], 0, 0
                    self.agent_log(aid, f"Listing Search échec: {type(e).__name__}: {str(e)[:80]}")
                found = pick_listing_url(searched)
                line = (
                    f"Listing Search: TinyFish {n_tf} + Serper {n_sp} → "
                    f"{len(searched)} candidats hygiène "
                    f"(serper {self._serper_queries})"
                )
                self.agent_log(aid, line)
                self.log(f"[{name}] {line}")

            if not found:
                pool = merge_listing_candidates(
                    crawled, fetched, searched, cap=LISTING_JUDGE_CAP)
                if pool:
                    used_engine = "Listing judge"
                    self.set_agent(aid, engine="Listing juge LLM")
                    log_fn = lambda m: self.agent_log(aid, m)
                    try:
                        sem = self._judge_sem or asyncio.Semaphore(1)
                        async with sem:
                            found = await llm_judge_listing(
                                seed, pool, settings=self.settings, log=log_fn)
                    except ListingJudgeQuotaError as e:
                        found = None
                        judge_quota = True
                        self.agent_log(
                            aid, f"Listing juge quota 429: {str(e)[:80]}")
                        self.log(f"[{name}] Listing juge quota 429 — pas d'Agent")
                    except Exception as e:
                        found = None
                        self.agent_log(
                            aid,
                            f"Listing juge échec: {type(e).__name__}: {str(e)[:80]}")
                    if found:
                        self.agent_log(aid, f"Listing juge → {found}")
                        self.log(f"[{name}] Listing juge → {found}")
                    elif not judge_quota:
                        self.agent_log(
                            aid, f"Listing juge: aucune des {len(pool)} URLs")
                        self.log(f"[{name}] Listing juge: aucune des {len(pool)} URLs")

            allow_agent = bool(key) and bool(self.settings.get("allow_tinyfish_agent", True))
            if not found and allow_agent and judge_quota:
                reason = "TinyFish Agent listing sauté — quota juge 429"
                self.agent_log(aid, reason)
                self.log(f"[{name}] {reason}")
            elif not found and allow_agent and not pool:
                reason = "TinyFish Agent listing sauté — aucun candidat"
                self.agent_log(aid, reason)
                self.log(f"[{name}] {reason}")
            elif not found and allow_agent:
                if not agent_host_is_worthwhile(seed.get("url") or "", seed.get("name") or ""):
                    reason = (
                        "TinyFish Agent listing sauté — "
                        "site absent ou pas l'organisme"
                    )
                    self.agent_log(aid, reason)
                    self.log(f"[{name}] {reason}")
                else:
                    used_engine = "TinyFish listing"
                    self.set_agent(aid, engine="TinyFish Agent listing")
                    reason = "Listing juge: aucune → TinyFish Agent listing"
                    self.agent_log(aid, reason)
                    self.log(f"[{name}] {reason}")
                    try:
                        sem = self._tf_sem or asyncio.Semaphore(1)
                        async with sem:
                            found = await self._tinyfish_listing_discover(aid, seed, key)
                    except Exception as e:
                        self.agent_log(aid, f"TinyFish Agent listing failed: {str(e)[:120]}")
                        self.log(f"TinyFish listing failed on {name}: {str(e)[:120]}", "error")

            if found:
                self.set_agent(aid, status="SUCCESS")
                self.agent_log(aid, f"catalogue {found} ({used_engine})")
                await self.telemetry(
                    seed["url"], used_engine, "SUCCESS",
                    (time.time() - t0) * 1000, 1)
            else:
                self.set_agent(aid, status="FAILED")
                await self.telemetry(
                    seed["url"], used_engine, "FAILED",
                    (time.time() - t0) * 1000, 0, "listing not found")
            return found
        except asyncio.CancelledError:
            self.set_agent(aid, status="CANCELLED")
            raise
        except Exception as e:
            self.set_agent(aid, status="FAILED")
            self.log(f"[{name}] listing hop error: {str(e)[:120]}", "error")
            return None

    async def _infer_listing_from_live(self, seed) -> str | None:
        """Hint: common prefix of v1 cards of the same funder (not an extraction dump)."""
        name = (seed.get("name") or "").strip()
        if not name:
            return None
        try:
            docs = await self.db.projects.find(
                {}, {"url": 1, "funders": 1, "funder": 1}).to_list(8000)
        except Exception:
            return None
        urls = []
        needle = name.lower()
        for doc in docs or []:
            names = []
            raw = doc.get("funders")
            if isinstance(raw, list):
                names.extend(str(x) for x in raw if x)
            elif raw:
                names.append(str(raw))
            if doc.get("funder"):
                names.append(str(doc["funder"]))
            if any(needle == str(n).strip().lower() for n in names):
                u = (doc.get("url") or "").strip()
                if u:
                    urls.append(u)
        if len(urls) < 2:
            return None
        inferred = infer_listing_from_project_urls(urls, name)
        if inferred and is_listing_url(inferred):
            host = domain_of(seed.get("url") or "")
            if host and domain_of(inferred) != host:
                return None
            if is_shared_hub(inferred) and not name_owns_hub(name, inferred):
                return None
            return inferred
        return None

    async def _remember_listing(self, seed, listing_url: str):
        """In-process memory + Mongo. Never `projects`."""
        domain = domain_of(listing_url) or domain_of(seed.get("url") or "")
        name = seed.get("name") or ""
        for item in self.master_seeds or []:
            same = domain and domain_of(item.get("url")) == domain
            if same and is_shared_hub(domain) and not (
                name_owns_hub(item.get("name") or "", domain)
                or domain_matches_org(domain, item.get("name") or "")
            ):
                same = False
            if same or (name and (item.get("name") or "") == name):
                item["url"] = listing_url
                item["listing_url"] = listing_url
                item["listing_kind"] = "projects_index"
                if item.get("queue") != "skip":
                    item["queue"] = "crawl"
                break
        if not domain:
            return
        try:
            key = {"name": name} if name else {"domain": domain}
            await self.db.master_seeds.update_one(
                key,
                {"$set": {
                    "name": name,
                    "url": listing_url,
                    "listing_url": listing_url,
                    "domain": domain,
                    "listing_kind": "projects_index",
                    "home_status": "official",
                    "queue": "crawl",
                    "source": "listing_hop",
                    "ts": now_iso(),
                },
                 "$setOnInsert": {"_id": str(uuid.uuid4())}},
                upsert=True)
        except Exception:
            pass

    async def _discover_fiches(self, seed, max_urls, depth=0, known_urls=None):
        known_urls = list(known_urls or [])
        key = self._tf_key()
        aid = self.new_agent("Crawler N1", "discover", seed["url"], seed["name"])
        t0 = time.time()
        urls = []
        used_engine = "Crawler"
        crawl_err = fetch_err = search_err = tf_err = ""
        n_tf = n_sp = 0
        try:
            self.set_agent(aid, status="RUNNING")
            self.agent_log(aid, "N1: crawler httpx (motifs URL_PATTERNS fiches)")
            try:
                urls = await self._crawl_discover(seed, max_urls)
            except Exception as e:
                crawl_err = f"{type(e).__name__}: {str(e)[:80]}"
                self.agent_log(aid, f"N1 crawler échec: {crawl_err}")
            n1_n = len(urls)
            self.agent_log(aid, f"N1: {n1_n} fiches")
            self.log(f"[{seed['name']}] N1: {n1_n} fiches")

            if not urls and key:
                used_engine = "TinyFish Fetch"
                self.set_agent(aid, engine="TinyFish Fetch")
                log_fn = lambda m: self.agent_log(aid, m)
                try:
                    urls = await self._fetch_discover(seed, max_urls, log=log_fn)
                except Exception as e:
                    fetch_err = f"{type(e).__name__}: {str(e)[:80]}"
                    self.agent_log(aid, f"Fetch échec: {fetch_err}")
                self.agent_log(aid, f"Fetch: {len(urls)} links")
                self.log(f"[{seed['name']}] Fetch: {len(urls)} links")
            elif not urls:
                self.agent_log(aid, "Fetch: skipped (no TinyFish key)")

            if not urls:
                used_engine = "Search"
                self.set_agent(aid, engine="Search TF∥Serper")
                log_fn = lambda m: self.agent_log(aid, m)
                try:
                    urls, n_tf, n_sp = await self._search_discover(
                        seed, max_urls, log=log_fn,
                        retry_query=fiche_search_retry_query(seed))
                except Exception as e:
                    search_err = f"{type(e).__name__}: {str(e)[:80]}"
                    self.agent_log(aid, f"Search échec: {search_err}")
                    urls, n_tf, n_sp = [], 0, 0
                line = (
                    f"Search: TinyFish {n_tf} + Serper {n_sp} → {len(urls)} "
                    f"after filter (serper {self._serper_queries})"
                )
                self.agent_log(aid, line)
                self.log(f"[{seed['name']}] {line}")

            if not urls and key and self.settings.get("allow_tinyfish_agent", True):
                host_ok = agent_host_is_worthwhile(
                    seed.get("url") or "", seed.get("name") or "")
                if not host_ok:
                    reason = (
                        "TinyFish Agent fiches sauté — "
                        "site absent ou pas l'organisme"
                    )
                    tf_err = "skipped: host not the organization"
                    self.agent_log(aid, reason)
                    self.log(f"[{seed['name']}] {reason}")
                else:
                    used_engine = "TinyFish"
                    self.set_agent(aid, engine="TinyFish Agent fiches")
                    reason = (
                        "Search: 0 hit → TinyFish Agent fiches"
                        if (n_tf + n_sp) == 0
                        else "Search: hits filtrés (0 fiches) → TinyFish Agent fiches"
                    )
                    self.agent_log(aid, reason)
                    self.log(f"[{seed['name']}] {reason}")
                    try:
                        sem = self._tf_sem or asyncio.Semaphore(1)
                        async with sem:
                            urls = await self._tinyfish_discover(
                                aid, seed, key, max_urls, known_urls)
                    except Exception as e:
                        tf_err = str(e)[:120]
                        self.agent_log(aid, f"TinyFish Agent fiches failed: {tf_err}")
                        self.log(f"TinyFish discovery failed on {seed['name']}: {tf_err}", "error")
            urls = urls[:max_urls]
            engine_code = {
                "TinyFish": "N3",
                "TinyFish Fetch": "Fetch",
                "Search": "Search",
            }.get(used_engine, "N1")
            await self._emit(
                "discover_urls", seed=seed.get("name"), n=len(urls),
                engine=engine_code, depth=depth,
            )
            if depth == 0 and (urls or known_urls):
                new_count = len([u for u in urls if u not in set(known_urls)])
                await self.db.discovery_state.update_one(
                    {"seed_url": seed["url"]},
                    {"$set": {"seed_url": seed["url"], "name": seed["name"], "last_scan": now_iso(),
                              "urls_found": len(urls), "new_urls": new_count},
                     "$setOnInsert": {"_id": str(uuid.uuid4())}},
                    upsert=True,
                )
            if urls:
                self.set_agent(aid, status="SUCCESS")
                self.agent_log(aid, f"{len(urls)} project URLs discovered ({used_engine})")
                self.log(f"[{seed['name']}] {len(urls)} project pages found via {used_engine} → DeepLinkCache + queue")
                for u in urls:
                    if url_looks_like_image(u):
                        continue
                    await self.db.deeplink_pages.update_one(
                        {"url": u}, {"$set": {"url": u, "funder": seed["name"], "source": seed["url"], "ts": now_iso()},
                                     "$setOnInsert": {"_id": str(uuid.uuid4())}}, upsert=True)
                    self.queued_count += 1
                    await self.queue.put({"url": u, "funder": seed["name"], "source": seed["url"], "depth": depth})
                await self.telemetry(seed["url"], used_engine, "SUCCESS", (time.time() - t0) * 1000, len(urls))
            else:
                self.set_agent(aid, status="FAILED")
                self.log(f"[{seed['name']}] discovery returned 0 URLs", "warn")
                detail = (
                    f"crawler: {crawl_err or '0 urls'}; fetch: {fetch_err or 'n/a'}; "
                    f"search: {search_err or '0 urls'}; tinyfish: {tf_err or 'not attempted'}"
                )
                await self.telemetry(seed["url"], used_engine, "FAILED", (time.time() - t0) * 1000, 0, detail)
                await self.add_failed(seed["url"], seed["url"], seed["name"], detail, "discover")
            return len(urls)
        except asyncio.CancelledError:
            self.set_agent(aid, status="CANCELLED")
            raise
        except Exception as e:
            self.set_agent(aid, status="FAILED")
            await self.telemetry(seed["url"], used_engine, "FAILED", (time.time() - t0) * 1000, 0, str(e))
            await self.add_failed(seed["url"], seed["url"], seed["name"], str(e), "discover")
            return 0

    def _tf_agent_on_event(self, aid):
        async def on_event(ev):
            et = ev.get("type")
            if et == "STARTED":
                self.agent_log(aid, f"SSE stream open — run {str(ev.get('run_id'))[:8]}…")
            elif et == "STREAMING_URL" and ev.get("streaming_url"):
                self.set_agent(aid, live_url=ev["streaming_url"])
            elif et == "PROGRESS" and ev.get("purpose"):
                self.agent_log(aid, str(ev["purpose"])[:110])
        return on_event

    def _tf_poll_hooks(self, aid):
        ticks = {"n": 0}

        async def on_run(run):
            live = find_live_url(run)
            if live:
                self.set_agent(aid, live_url=live)
            ticks["n"] += 1
            if ticks["n"] == 1 or ticks["n"] % 5 == 0:
                st = run.get("status") or "PENDING"
                self.agent_log(aid, f"status {st} — même run TinyFish")

        def should_stop():
            a = self.agents.get(aid) or {}
            return a.get("status") == "CANCELLED"

        return on_run, should_stop

    async def _tf_agent_run(self, aid, seed, key, goal, schema, max_duration_s):
        """SSE; 429 → pause + retry; stream cut → poll; cap → cancel."""
        cfg = public_agent_config({"max_duration_seconds": max_duration_s})
        run_id = {"id": None}
        inner = self._tf_agent_on_event(aid)
        log = lambda m: self.agent_log(aid, m)
        budget = sse_wall_budget_s(max_duration_s)

        async def on_event(ev):
            rid = ev.get("run_id")
            if rid:
                run_id["id"] = rid
                self._tf_track_run(aid, rid)
            await inner(ev)

        async def cancel_live():
            rid = run_id["id"]
            if not rid:
                return {}
            try:
                await tf_cancel_run(rid, key)
            except Exception:
                pass
            try:
                last = await tf_get_run(rid, key)
            except Exception:
                return {}
            if str(last.get("status") or "").upper() == "COMPLETED":
                return last.get("result") or {}
            return {}

        last_err = None
        try:
            for attempt in range(2):
                await await_agent_cooldown(log)
                run_id["id"] = None
                self._tf_untrack_run(aid)
                try:
                    return await asyncio.wait_for(
                        tf_run_sse(
                            seed["url"], goal, schema, key,
                            on_event=on_event, agent_config=cfg),
                        timeout=budget)
                except asyncio.CancelledError:
                    await cancel_live()
                    raise
                except TimeoutError as e:
                    last_err = e
                    if is_sse_stream_end(e) and run_id["id"]:
                        self.agent_log(
                            aid,
                            f"SSE coupé ({type(e).__name__}) → poll {run_id['id'][:8]} "
                            f"(même run, pas un 2ᵉ Agent)")
                        on_run, should_stop = self._tf_poll_hooks(aid)
                        return await tf_poll_run(
                            run_id["id"], key,
                            budget_s=int(max_duration_s) + POLL_GRACE_S,
                            log=log, on_run=on_run, should_stop=should_stop)
                    if run_id["id"]:
                        self.agent_log(
                            aid,
                            f"SSE plafond {budget:.0f}s → cancel {run_id['id'][:8]} "
                            f"(pas de 2ᵉ Agent)")
                        return await cancel_live()
                    if not is_sse_stream_end(e):
                        self.agent_log(
                            aid,
                            f"SSE plafond {budget:.0f}s sans run_id — sauté "
                            f"(pas de 2ᵉ Agent)")
                        return {}
                    break
                except httpx.HTTPError as e:
                    last_err = e
                    if run_id["id"]:
                        self.agent_log(
                            aid,
                            f"SSE coupé ({type(e).__name__}) → poll {run_id['id'][:8]} "
                            f"(même run, pas un 2ᵉ Agent)")
                        on_run, should_stop = self._tf_poll_hooks(aid)
                        return await tf_poll_run(
                            run_id["id"], key,
                            budget_s=int(max_duration_s) + POLL_GRACE_S,
                            log=log, on_run=on_run, should_stop=should_stop)
                    if is_http_status(e, 429) and attempt == 0:
                        wait = retry_after_s(e)
                        await mark_agent_429(wait)
                        self.agent_log(
                            aid,
                            f"TinyFish Agent 429 — pause {wait:.0f}s, retry SSE "
                            f"(pas de 2ᵉ run)")
                        continue
                    break
            if is_http_status(last_err, 429):
                self.agent_log(aid, "TinyFish Agent 429 encore — sauté")
                return {}
            if is_http_status(last_err, 401) or is_http_status(last_err, 402):
                self.agent_log(aid, f"SSE HTTP {last_err.response.status_code} — sauté")
                return {}
            self.agent_log(aid, f"SSE refusé ({str(last_err)[:80]}) → run-async")
            try:
                return await self._tinyfish_discover_poll(
                    aid, seed, key, goal, schema=schema,
                    max_duration_s=max_duration_s)
            except httpx.HTTPStatusError as e:
                if is_http_status(e, 429):
                    await mark_agent_429(retry_after_s(e))
                    self.agent_log(aid, "TinyFish run-async 429 — sauté")
                    return {}
                raise
        finally:
            self._tf_untrack_run(aid)

    async def _tinyfish_listing_discover(self, aid, seed, key):
        """TinyFish Agent #1: one catalogue URL, no individual cards."""
        self.set_agent(aid, status="RUNNING")
        goal = listing_goal(seed["name"])
        result = await self._tf_agent_run(
            aid, seed, key, goal, LISTING_SCHEMA, LISTING_AGENT_DURATION_S)
        raw = ((result or {}).get("listing_url") or "").strip()
        self.agent_log(aid, f"agent listing finished: {raw or 'empty'}")
        if not raw.startswith("http"):
            return None
        href = raw.split("#")[0].split("?")[0]
        return accept_listing_url(href, seed)

    async def _tinyfish_discover(self, aid, seed, key, max_urls, known_urls=None):
        """TinyFish Agent #2: individual cards (SSE then poll of the same run)."""
        self.set_agent(aid, status="RUNNING")
        goal = discovery_goal(seed["name"], known_urls)
        result = await self._tf_agent_run(
            aid, seed, key, goal, DISCOVERY_SCHEMA, FICHE_AGENT_DURATION_S)
        projects = (result or {}).get("projects") or []
        self.agent_log(aid, f"agent fiches finished: {len(projects)} candidates")
        urls, seen = [], set()
        for p in projects:
            u = (p.get("url") or "").strip()
            if u.startswith("http") and u not in seen:
                seen.add(u)
                urls.append(u)
        return filter_discover_urls([{"url": u} for u in urls], seed, max_urls)

    async def _tinyfish_discover_poll(self, aid, seed, key, goal=None, *,
                                       schema=None, max_duration_s=None):
        schema = schema or DISCOVERY_SCHEMA
        body = await tf_run_async(
            seed["url"], goal or discovery_goal(seed["name"]), schema, key,
            max_duration_s=max_duration_s)
        run_id = body.get("run_id")
        if not run_id:
            raise ValueError(body.get("error", "no run_id"))
        live = find_live_url(body)
        if live:
            self.set_agent(aid, live_url=live)
        self.agent_log(aid, f"run_id {run_id[:12]}… agent navigating")
        on_run, should_stop = self._tf_poll_hooks(aid)
        return await tf_poll_run(
            run_id, key,
            budget_s=int(max_duration_s or 120) + POLL_GRACE_S,
            log=lambda m: self.agent_log(aid, m),
            on_run=on_run, should_stop=should_stop)

    async def _crawl_page_links(self, seed):
        async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers=UA) as client:
            try:
                r = await fetch_checked(client, seed["url"])
            except PublicFetchError as e:
                self.log(
                    f"[{seed.get('name')}] crawl skip non-public URL "
                    f"(not fetched) {(seed.get('url') or '')[:180]} — {e}",
                    "warn",
                )
                return []
            soup = BeautifulSoup(r.text, "html.parser")
        base_host = urlparse(seed["url"]).netloc
        urls, seen = [], set()
        for a in soup.find_all("a", href=True):
            href = urljoin(seed["url"], a["href"]).split("#")[0].split("?")[0]
            if urlparse(href).netloc != base_host or href.rstrip("/") == seed["url"].rstrip("/"):
                continue
            if href in seen:
                continue
            seen.add(href)
            urls.append(href)
        return urls

    async def _crawl_listing(self, seed, max_urls=LISTING_JUDGE_CAP):
        """Internal links: hygiene (the leaf shortcut is applied higher up)."""
        hrefs = await self._crawl_page_links(seed)
        return hygiene_listing_urls([{"url": u} for u in hrefs], seed, max_urls)

    async def _crawl_discover(self, seed, max_urls):
        """1st card pass: URL_PATTERNS paths (≥ 2 segments). The 8
        internal links outside the blacklist do not count as real cards —
        else homes would skip Fetch/Search.
        """
        hrefs = await self._crawl_page_links(seed)
        urls = []
        for href in hrefs:
            if url_looks_like_image(href):
                continue
            path = urlparse(href).path
            if is_project_fiche_path(path, apply_blacklist=False):
                urls.append(href)
            if len(urls) >= max_urls * 2:
                break
        return urls[:max_urls]

    async def _fetch_listing(self, seed, log=None):
        key = self._tf_key()
        url = (seed.get("url") or "").strip()
        if not key or not url:
            return []
        recs = await tf_fetch(
            [url], key, links=True, purpose=PROJECTS_LISTING_PURPOSE,
            log=log or (lambda m: None))
        rec = (recs or {}).get(url) or {}
        if not rec and recs:
            rec = next(iter(recs.values()))
        hits = [{"url": u} for u in _links_from_fetch_record(rec, url)]
        return hygiene_listing_urls(hits, seed, LISTING_JUDGE_CAP)

    async def _fetch_discover(self, seed, max_urls, log=None):
        """TinyFish Fetch of the same URL (JS mirror) → same card-pattern filters."""
        key = self._tf_key()
        url = (seed.get("url") or "").strip()
        if not key or not url:
            return []
        recs = await tf_fetch(
            [url], key, links=True, purpose=PROJECTS_DISCOVERY_PURPOSE,
            log=log or (lambda m: None))
        rec = (recs or {}).get(url) or {}
        if not rec and recs:
            rec = next(iter(recs.values()))
        hits = [{"url": u} for u in _links_from_fetch_record(rec, url)]
        return filter_discover_urls(hits, seed, max_urls)

    async def _tf_search_discover(self, query: str, seed: dict, log=None,
                                 purpose=None) -> list:
        key = self._tf_key()
        if not key:
            return []
        domain = domain_of((seed or {}).get("url") or "")
        include = [domain] if domain else None
        return await tf_search(
            query, key, include_domains=include,
            purpose=purpose or PROJECTS_DISCOVERY_PURPOSE,
            log=log or (lambda m: None))

    async def _serper_discover(self, query: str, log=None) -> list:
        key = self._serper_key()
        if not key:
            return []
        sem = self._serper_sem or asyncio.Semaphore(2)
        async with sem:
            self._serper_queries += 1
            return await serper_search(query, key, log=log or (lambda m: None))

    async def _search_with_filter(self, seed, query, max_urls, *,
                                  filter_fn, purpose, log=None,
                                  retry_query=None):
        """One TF ∥ Serper shot, then retry only if hits were filtered down to 0."""
        tf_hits, sp_hits = await asyncio.gather(
            self._tf_search_discover(query, seed, log=log, purpose=purpose),
            self._serper_discover(query, log=log),
        )
        n_tf, n_sp = len(tf_hits or []), len(sp_hits or [])
        combined = list(tf_hits or []) + list(sp_hits or [])
        urls = filter_fn(combined, seed, max_urls)
        raw = [_hit_url(h) for h in combined if _hit_url(h).startswith("http")]
        if urls or not raw or not retry_query:
            return urls, n_tf, n_sp
        if log:
            log(f"Search: {len(raw)} hits → 0 after filter — retry")
        self.log(f"[{seed.get('name')}] Search: {len(raw)} hits → 0 after filter — retry")
        tf2, sp_hits2 = await asyncio.gather(
            self._tf_search_discover(retry_query, seed, log=log, purpose=purpose),
            self._serper_discover(retry_query, log=log),
        )
        n_tf += len(tf2 or [])
        n_sp += len(sp_hits2 or [])
        urls = filter_fn(
            list(tf2 or []) + list(sp_hits2 or []), seed, max_urls,
            exclude_urls=raw)
        return urls, n_tf, n_sp

    async def _search_listing(self, seed, log=None):
        return await self._search_with_filter(
            seed, listing_search_query(seed), LISTING_JUDGE_CAP,
            filter_fn=hygiene_listing_urls,
            purpose=PROJECTS_LISTING_PURPOSE, log=log,
            retry_query=listing_search_retry_query(seed))

    async def _search_discover(self, seed, max_urls, log=None, retry_query=None):
        """TinyFish Search ∥ Serper, filtered card union. Never 2 EN/FR shots."""
        return await self._search_with_filter(
            seed, project_search_query(seed), max_urls,
            filter_fn=filter_discover_urls,
            purpose=PROJECTS_DISCOVERY_PURPOSE, log=log,
            retry_query=retry_query)

    async def _search_partner_site(self, name: str, log=None) -> str:
        """Same Search as step B: "official site", not "marine conservation"."""
        seed = {"name": name, "url": ""}
        query = official_site_query(name)
        retry = official_site_retry_query(name)
        tf_hits, sp_hits = await asyncio.gather(
            self._tf_search_discover(query, seed, log=log),
            self._serper_discover(query, log=log),
        )
        hits = list(tf_hits or []) + list(sp_hits or [])
        site = official_site_from_hits(hits, funder_name=name)
        if not site and retry:
            tf2, sp2 = await asyncio.gather(
                self._tf_search_discover(retry, seed, log=log),
                self._serper_discover(retry, log=log),
            )
            hits = list(tf2 or []) + list(sp2 or []) + hits
            site = official_site_from_hits(hits, funder_name=name)
        self.log(
            f"Follow the Money: Search official site '{name}' → "
            f"{site or 'aucun site'} (serper {self._serper_queries})"
        )
        return site

    # ---------- extraction ----------
    async def _extract_worker(self, idx):
        while True:
            item = await self.queue.get()
            self.queued_count = max(0, self.queued_count - 1)
            try:
                await self._process_url(item)
            except asyncio.CancelledError:
                self.queue.task_done()
                raise
            except Exception as e:
                self.log(f"Extractor error on {item['url'][:60]}: {str(e)[:100]}", "error")
            finally:
                try:
                    self.queue.task_done()
                except ValueError:
                    pass

    def _queue_partner(self, name: str, purl: str):
        catalog = self.master_seeds or MASTER_SEEDS
        decision = decide_follow_the_money_partner(name, purl, catalog=catalog)
        if decision.get("action") != FTM_DISCOVER or not decision.get("seed"):
            return
        self._queue_partner_seed(decision["seed"])

    def _ftm_new_org_blocked(self, name: str, url: str | None = None) -> bool:
        catalog = self.master_seeds or MASTER_SEEDS
        if find_catalog_seed(catalog, name, url):
            return False
        cap = int(self.settings.get("max_partner_orgs", 15))
        return self.new_partner_count >= cap

    def _queue_partner_seed(
        self, seed: dict, *, start_discover: bool = True, persist: bool = True,
    ):
        if not self.running or not self.settings.get("follow_the_money", True):
            return None
        purl = (seed.get("url") or "").strip()
        name = (seed.get("name") or "").strip()
        domain = domain_of(purl)
        if not domain or not name:
            return None
        hosted_on_hub = is_shared_hub(domain) and not name_owns_hub(name, domain)
        if hosted_on_hub:
            self.log(
                f"Follow the Money: skip '{name}' — hub {domain} (pas de crawl)",
                "warn",
            )
            return None
        if domain in self.partner_domains:
            return None
        catalog = self.master_seeds or MASTER_SEEDS
        known_seed = find_catalog_seed(catalog, name, purl)
        known = known_seed is not None
        cap = int(self.settings.get("max_partner_orgs", 15))
        stored = None
        if not known:
            if self.new_partner_count >= cap:
                return None
            if not partner_site_reachable(purl):
                self.log(
                    f"Follow the Money: skip '{name}' — site injoignable "
                    f"({purl}) — plafond intact",
                    "warn",
                )
                return None
            self.new_partner_count += 1
            stored = {
                "name": name,
                "url": purl,
                "listing_kind": seed.get("listing_kind") or "homepage",
                "home_status": seed.get("home_status") or "official",
                "queue": seed.get("queue") or "crawl",
                "source": "follow_the_money",
                "project_count": 0,
            }
            if seed.get("home_url"):
                stored["home_url"] = seed["home_url"]
            if seed.get("listing_url"):
                stored["listing_url"] = seed["listing_url"]
            self.master_seeds.append(stored)
            if persist:
                try:
                    asyncio.create_task(self._persist_ftm_seed(stored))
                except Exception:
                    pass
            kind = f"new org ({self.new_partner_count}/{cap})"
        else:
            kind = "known v1 — queued (cap does not apply)"
        self.partner_domains.add(domain)
        self.partner_count = len(self.partner_domains)
        self.log(
            f"Follow the Money: {kind} '{name}' ({domain}) → recursive discovery",
            "success",
        )
        if known_seed and known_seed.get("source") != "follow_the_money":
            label = known_seed["name"]
        elif known:
            label = name
        else:
            label = f"{name} (partner)"
        discover = {
            "name": label,
            "url": purl,
            "listing_kind": seed.get("listing_kind") or "homepage",
            "home_status": seed.get("home_status") or "official",
            "queue": seed.get("queue") or "crawl",
        }
        if seed.get("listing_url"):
            discover["listing_url"] = seed["listing_url"]
        if start_discover:
            self.recursive_tasks.append(
                asyncio.create_task(self._discover(discover, 6, depth=1)))
        return {"new": not known, "discover": discover, "stored": stored}

    async def _persist_ftm_seed(self, stored: dict):
        domain = domain_of(stored.get("url"))
        if not domain:
            return
        try:
            await self.db.master_seeds.update_one(
                {"domain": domain},
                {"$set": {
                    "name": stored.get("name"), "url": stored.get("url"),
                    "domain": domain,
                    "source": "follow_the_money",
                    "listing_kind": stored.get("listing_kind"),
                    "home_status": stored.get("home_status"),
                    "queue": stored.get("queue"),
                    "ts": now_iso(),
                },
                 "$setOnInsert": {"_id": str(uuid.uuid4())}},
                upsert=True)
        except Exception:
            pass

    def _refund_new_partner(self, seed: dict):
        name = (seed.get("name") or "").strip()
        domain = domain_of(seed.get("url"))
        self.new_partner_count = max(0, self.new_partner_count - 1)
        if domain:
            self.partner_domains.discard(domain)
        self.partner_count = len(self.partner_domains)
        key = norm_name(name)
        self.master_seeds = [
            s for s in self.master_seeds
            if not (
                (s.get("source") or "") == "follow_the_money"
                and domain_of(s.get("url")) == domain
                and norm_name(s.get("name") or "") == key
            )
        ]
        self.log(
            f"Follow the Money: ticket remboursé '{name}' — 0 fiche, plafond intact",
            "warn",
        )

    def _inbox_ftm(self, name: str, url: str | None, seed: dict | None, proj: dict):
        self.ftm_inbox.append({
            "name": name,
            "url": (url or "").strip(),
            "seed": dict(seed) if seed else None,
            "s_ocean": proj.get("s_ocean"),
        })
        self.log(f"Follow the Money: inbox '{name}' (classement en fin de catalogue)")

    async def _try_new_partner(self, seed: dict) -> bool:
        registered = self._queue_partner_seed(
            seed, start_discover=False, persist=False)
        if not registered:
            return False
        n = await self._discover(registered["discover"], 6, depth=1)
        useful = n is None or int(n) > 0
        if useful:
            if registered.get("new") and registered.get("stored"):
                await self._persist_ftm_seed(registered["stored"])
            return True
        if registered.get("new"):
            self._refund_new_partner(seed)
        return False

    async def _flush_ftm_inbox(self):
        ranked = rank_ftm_inbox(self.ftm_inbox)
        cap = int(self.settings.get("max_partner_orgs", 15))
        self.log(
            f"Follow the Money: inbox {len(self.ftm_inbox)} mention(s) → "
            f"{len(ranked)} org(s), cap {cap}"
        )
        catalog = self.master_seeds or MASTER_SEEDS
        for row in ranked:
            if self.new_partner_count >= cap:
                self.log(f"Follow the Money: {cap}/{cap} tickets utiles atteints")
                break
            name = row.get("name") or ""
            purl = (row.get("url") or (row.get("seed") or {}).get("url") or "").strip() or None
            if self._ftm_new_org_blocked(name, purl):
                continue
            decision = decide_follow_the_money_partner(name, purl, catalog=catalog)
            if decision.get("action") == FTM_SEARCH or decision.get("needs_search"):
                home = await self._search_partner_site(name)
                decision = decide_follow_the_money_partner(
                    name, purl, catalog=catalog,
                    searched_home=home, did_search=True,
                )
            if decision.get("action") != FTM_DISCOVER or not decision.get("seed"):
                reason = decision.get("reason") or "skip"
                self.log(f"Follow the Money: skip '{name}' ({reason})")
                continue
            found = find_catalog_seed(catalog, name, decision["seed"].get("url"))
            if found and is_crawl_ready(found):
                self._queue_partner_seed(decision["seed"])
                continue
            await self._try_new_partner(decision["seed"])
            catalog = self.master_seeds or MASTER_SEEDS

    async def _follow_the_money(self, proj: dict, depth: int, *, published: bool = True):
        if depth != 0:
            return
        min_s = float(self.settings.get("ftm_min_s_ocean", FTM_MIN_S_OCEAN))
        if not ftm_page_allows_collect(published, proj.get("s_ocean"), min_s):
            return
        catalog = self.master_seeds or MASTER_SEEDS
        for p in (proj.get("partners") or []):
            if not isinstance(p, dict) or not p.get("name"):
                continue
            name = p["name"]
            purl = (p.get("url") or "").strip() or None
            decision = decide_follow_the_money_partner(name, purl, catalog=catalog)
            if decision.get("action") == FTM_DISCOVER and decision.get("seed"):
                found = find_catalog_seed(catalog, name, decision["seed"].get("url") or purl)
                if found and is_crawl_ready(found):
                    self._queue_partner_seed(decision["seed"])
                    continue
            if decision.get("reason") in {
                "exclude", "compound", "empty_name", "catalog_not_ready",
            }:
                self.log(f"Follow the Money: skip '{name}' ({decision.get('reason')})")
                continue
            if decision.get("action") in {FTM_DISCOVER, FTM_SEARCH} or decision.get("needs_search"):
                self._inbox_ftm(name, purl, decision.get("seed"), proj)
                continue
            reason = decision.get("reason") or "skip"
            self.log(f"Follow the Money: skip '{name}' ({reason})")

    async def _process_url(self, item):
        if not self.run_id:
            raise RuntimeError("isolated run required — no write to projects")
        url, funder, source = item["url"], item["funder"], item["source"]
        depth = item.get("depth", 0)
        force = bool(item.get("force") or getattr(self, "force_rescan", False))

        existing_run = await self.db.project_run_projects.find_one(
            {"run_id": self.run_id, "url": url})
        if existing_run and not force:
            return existing_run

        if not force:
            v1 = await self.db.projects.find_one({"url": url})
            if v1:
                # Skip crawl (credits) but trace in the run — does not count for Auto-Stop
                await self._write_verdict(
                    item, "seen_v1",
                    title=v1.get("title") or url,
                    lat=v1.get("lat"), lon=v1.get("lon"),
                    location=v1.get("location"),
                    geo_source="v1",
                    v1_id=v1.get("_id"),
                )
                return {"status": "seen_v1", "url": url}

        if url_looks_like_image(url):
            reason = "URL image — portrait ou média, pas une fiche projet"
            await self._write_verdict(item, "rejected", title=url, reason=reason)
            await self.add_failed(url, source, funder, reason, "image")
            await self._emit("rejected", url=url, reason=reason)
            self._bump_saturation(False)
            return {"status": "rejected", "url": url}

        aid = self.new_agent("Cascade N1→N2", "extract", url, source)
        t0 = time.time()
        await self._emit("extract_start", url=url, funder=funder, source=source)
        try:
            self.set_agent(aid, status="RUNNING")
            self.agent_log(aid, "Cascade hybride: N1 trafilatura/PyMuPDF → N2 Readability")
            page = await extract_cascade(url, min_chars=200,
                                         log=lambda m: self.agent_log(aid, m))
            err = page.get("error")
            if err in {"image_url", "image_content", "binary_content"}:
                reason = (
                    "contenu image — pas une fiche projet"
                    if err.startswith("image")
                    else "contenu binaire — pas une fiche projet"
                )
                self.set_agent(aid, status="REJECTED")
                self.agent_log(aid, f"REJECTED: {reason}")
                await self._write_verdict(item, "rejected", title=url, reason=reason)
                await self._emit("rejected", url=url, reason=reason)
                await self.telemetry(
                    url, "Cascade N1→N2", "REJECTED",
                    (time.time() - t0) * 1000, 0, reason)
                await self.add_failed(url, source, funder, reason, "image")
                self._bump_saturation(False)
                return {"status": "rejected", "url": url}
            if not page["text"]:
                raise ValueError(f"cascade N1/N2 sans texte exploitable ({page['level']})")
            page_title = (page["title"] or "").strip() or url
            text = page["text"]
            meta_desc = page["meta_desc"]
            image = page["image"]
            ext_links = page["ext_links"]
            self.agent_log(aid, f"texte extrait via {page['level']} ({len(text)} chars)")

            self.agent_log(aid, "Gatekeeper Protocol (ML local → LLM cascade)")
            gk = await gatekeeper_check(page_title, text, self.settings)
            await self._emit("gatekeeper", url=url, accepted=gk["accepted"],
                             reason=str(gk.get("reason") or "")[:200])
            if not gk["accepted"]:
                self.set_agent(aid, status="REJECTED")
                self.agent_log(aid, f"REJECTED: {gk['reason'][:80]}")
                await self._write_verdict(item, "rejected", title=page_title,
                                          reason=gk["reason"], engine=gk.get("engine"))
                await self._emit("rejected", url=url, reason=gk["reason"],
                                 engine=gk.get("engine"))
                await self.telemetry(url, gk["engine"], "REJECTED", (time.time() - t0) * 1000, 0, gk["reason"])
                await self.add_failed(url, source, funder, gk["reason"], "gatekeeper")
                self._bump_saturation(False)
                return {"status": "rejected", "url": url}

            # Local RAG: on long pages, only relevant chunks go to the LLM
            llm_text = text
            if len(text) > 6000:
                llm_text = select_context(f"marine ocean coastal conservation project {page_title}",
                                          text, max_chars=5000)
                self.agent_log(aid, f"RAG: {len(text)} → {len(llm_text)} chars (chunks pertinents)")

            self.agent_log(aid, f"Extraction + S_ocean scoring ({'LLM cascade' if has_llm(self.settings) else 'heuristic'})")
            proj = await extract_project(page_title, llm_text, meta_desc, url, funder, self.settings, ext_links=ext_links)

            if not s_ocean_meets_min(proj.get("s_ocean"), self.settings):
                min_s = float(self.settings.get("min_marine_score", 0.5))
                reason = (
                    f"S_ocean {proj.get('s_ocean')} < min_marine_score {min_s}"
                )
                self.set_agent(aid, status="REJECTED")
                self.agent_log(aid, f"REJECTED: {reason}")
                await self._write_verdict(
                    item, "rejected",
                    title=proj.get("title") or page_title,
                    reason=reason, engine=proj.get("engine"),
                    s_ocean=proj.get("s_ocean"),
                )
                await self._emit("rejected", url=url, reason=reason,
                                 engine=proj.get("engine"))
                await self.telemetry(
                    url, proj.get("engine") or "Extractor", "REJECTED",
                    (time.time() - t0) * 1000, 0, reason)
                await self.add_failed(url, source, funder, reason, "s_ocean")
                self._bump_saturation(False)
                return {"status": "rejected", "url": url}

            lat, lon = proj.get("latitude"), proj.get("longitude")
            geo_src = "extracted"
            if not valid_coords(lat, lon):
                lat = lon = None
            elif not site_publishable(lat, lon, self.settings)[0]:
                # HQ / inland city extracted: try the toponym before unlocated.
                lat = lon = None
            if lat is None:
                geo = await geocode_project_site(
                    proj.get("location") or "",
                    proj.get("title") or "",
                    self.settings,
                    log=lambda m: self.agent_log(aid, m),
                )
                if geo.get("lat") is not None:
                    lat, lon = geo["lat"], geo["lon"]
                    geo_src = geo.get("source") or geo.get("geo_source") or "geocoded:location"
                await self._emit(
                    "geocode", url=url,
                    source=geo_src if lat is not None else (geo.get("source") or "none"),
                    kind=geo.get("geo_kind") or "havre",
                    lat=lat, lon=lon,
                )
            else:
                await self._emit(
                    "geocode", url=url, source=geo_src, kind="extracted",
                    lat=lat, lon=lon,
                )

            ok, kind = site_publishable(lat, lon, self.settings)
            if not ok:
                self.set_agent(aid, status="FAILED")
                self.agent_log(aid, f"UNLOCATED ({kind}): no boat-accessible site — not published")
                await self._follow_the_money(proj, depth, published=False)
                await self._write_verdict(
                    item, "unlocated", title=proj.get("title") or page_title,
                    location=proj.get("location"), lat=lat, lon=lon,
                    geo_source=geo_src, geo_kind=kind, engine=proj.get("engine"),
                    reason=f"unlocated:{kind}")
                await self._emit("unlocated", url=url, reason=f"unlocated:{kind}",
                                 kind=kind, lat=lat, lon=lon)
                await self.telemetry(url, proj["engine"], "UNLOCATED", (time.time() - t0) * 1000, 0, kind)
                await self.add_failed(url, source, funder, f"unlocated:{kind}", "unlocated")
                self._bump_saturation(False)
                return {"status": "unlocated", "url": url, "kind": kind}
            snapped = False
            self.agent_log(aid, f"site publishable ({kind})")

            merged = await self._dedup_merge(proj, url, funder, lat, lon)
            if merged:
                self.set_agent(aid, status="SUCCESS")
                self.agent_log(aid, f"Merged ({merged}) — dedup_core: <500m / similarity")
                await self.telemetry(url, proj["engine"], "MERGED", (time.time() - t0) * 1000, 1)
                self._bump_saturation(False)
                return {"status": merged, "url": url}

            category = proj.get("category")
            await self._write_verdict(
                item, "site",
                title=proj["title"],
                description=proj.get("description"),
                location=proj.get("location"),
                lat=float(lat), lon=float(lon),
                s_ocean=proj.get("s_ocean", 0.5),
                snapped=snapped,
                geo_source=geo_src,
                geo_kind=kind,
                category=category,
                category_group=normalize_category(category),
                image=image,
                engine=proj["engine"],
                extract_level=page["level"],
            )
            self._bump_saturation(True)
            self.set_agent(aid, status="SUCCESS")
            self.agent_log(aid, f"Site recorded in run — S_ocean {proj.get('s_ocean')}")
            self.log(f"+ {proj['title'][:60]} ({funder}) → run {self.run_id}", "success")
            await self.telemetry(url, proj["engine"], "SUCCESS", (time.time() - t0) * 1000, 1)
            await self._follow_the_money(proj, depth, published=True)
            return {"status": "site", "url": url}
        except asyncio.CancelledError:
            self.set_agent(aid, status="CANCELLED")
            raise
        except Exception as e:
            self.set_agent(aid, status="FAILED")
            self.agent_log(aid, f"FAILED: {str(e)[:80]}")
            try:
                await self._write_verdict(item, "failed", title=url, reason=str(e)[:300])
            except Exception:
                pass
            await self.telemetry(url, "Cascade N1→N2", "FAILED", (time.time() - t0) * 1000, 0, str(e))
            await self.add_failed(url, source, funder, str(e), "extract")
            self._bump_saturation(False)
            return {"status": "failed", "url": url}

    async def _dedup_merge(self, proj, url, funder, lat, lon):
        """Dedup in the run, then read-only v1. Never writes `projects`."""
        from app.services.project_runs import write_run_project
        candidate = {"title": proj["title"], "lat": lat, "lon": lon}
        run_docs = await self.db.project_run_projects.find(
            {"run_id": self.run_id, "verdict": "site"},
            {"title": 1, "lat": 1, "lon": 1, "funders": 1, "url": 1,
             "funder": 1, "location": 1, "description": 1},
        ).to_list(20000)
        for c in run_docs:
            if c.get("url") == url:
                return "merged_run"
            if is_duplicate(candidate, c):
                funders = list(set((c.get("funders") or []) + [funder]))
                await write_run_project(self.db, self.run_id, {
                    "url": c["url"],
                    "title": c.get("title"),
                    "funder": c.get("funder"),
                    "funders": funders,
                    "lat": c.get("lat"),
                    "lon": c.get("lon"),
                    "location": c.get("location"),
                    "description": c.get("description"),
                    "verdict": "site",
                })
                await self._write_verdict(
                    {"url": url, "funder": funder}, "merged_run",
                    title=proj["title"], lat=lat, lon=lon,
                    location=proj.get("location"),
                    merged_into_url=c.get("url"),
                )
                await self._emit("dedup", url=url, into=c.get("url"), how="merged_run")
                return "merged_run"

        # From scratch: re-extract in the run, even if the card exists in v1.
        if getattr(self, "force_rescan", False):
            return None
        v1_docs = await self.db.projects.find(
            {}, {"title": 1, "lat": 1, "lon": 1, "url": 1},
        ).to_list(30000)
        for c in v1_docs:
            if c.get("url") == url:
                await self._write_verdict(
                    {"url": url, "funder": funder}, "seen_v1",
                    title=proj["title"], lat=lat, lon=lon,
                    location=proj.get("location"), v1_id=c.get("_id"),
                )
                return "seen_v1"
            if is_duplicate(candidate, c):
                await self._write_verdict(
                    {"url": url, "funder": funder}, "merged_v1",
                    title=proj["title"], lat=lat, lon=lon,
                    location=proj.get("location"),
                    v1_url=c.get("url"), v1_id=c.get("_id"),
                )
                await self._emit("dedup", url=url, into=c.get("url"), how="merged_v1")
                return "merged_v1"
        return None
