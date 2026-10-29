"""MasterSeeds catalog: audit outside Complet + homes / lists / queue.

Does not touch `projects`. Only enriches the input list (CDC C5 / §18).
"""
from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

from app.services.master_seeds import (
    DATA_DIR, NOISE_NAMES, SKIP_LISTING_NETLOCS, domain_of, domain_matches_org,
    funder_names_from_project, is_noise_name, is_publisher_host,
    is_shared_hub, listing_url_from_project_urls, name_owns_hub,
    names_soft_match, now_iso, norm_name,
)
from app.services.project_listing import (
    infer_listing_from_project_urls, is_homepage_url, is_listing_url,
)

HOME_STATUS_OFFICIAL = "official"
HOME_STATUS_BORROWED = "borrowed_hub"
HOME_STATUS_PUBLISHER = "publisher"
HOME_STATUS_SOCIAL = "social"
HOME_STATUS_EMPTY = "empty"
HOME_STATUS_UNKNOWN = "unknown"

NAME_OK = "ok"
NAME_COMPOUND = "compound"
NAME_EXCLUDE = "exclude"

QUEUE_CRAWL = "crawl"
QUEUE_RESOLVE = "resolve"
QUEUE_SKIP = "skip"

FTM_DISCOVER = "discover"
FTM_SKIP = "skip"
FTM_SEARCH = "search"

SEARCH_JOURNAL_PATH = DATA_DIR / "official_homes_search.jsonl"
SEARCH_PROGRESS_PATH = DATA_DIR / "official_homes_search.progress.json"
LISTING_JOURNAL_PATH = DATA_DIR / "official_listings_search.jsonl"
LISTING_PROGRESS_PATH = DATA_DIR / "official_listings_search.progress.json"
SPLITS_REPORT_PATH = DATA_DIR / "compound_splits.json"
HOME_REVIEW_PATH = DATA_DIR / "home_reviews.json"
AUDIT_PATH = DATA_DIR / "master_seeds_audit.json"
HOME_SOURCE_SEARCH = "search"
HOME_SOURCE_REVIEW = "review"
REVIEW_REJECT = "reject"
REVIEW_NO_PROJECTS = "no_projects"
REVIEW_KEEP = "keep"
LISTING_SOURCE_SEARCH = "search"
LISTING_SOURCE_V1 = "v1_url"
MIN_SPLIT_PART = 3

SEARCH_HOME_STATUSES = {
    HOME_STATUS_BORROWED,
    HOME_STATUS_UNKNOWN,
    HOME_STATUS_EMPTY,
    HOME_STATUS_PUBLISHER,
}

_SEARCH_OVERLAY_FIELDS = (
    "home_url", "url", "home_status", "home_source", "queue", "listing_kind",
    "listing_url",
)

_EXCLUDE_EXACT = NOISE_NAMES | {
    "in-kind", "in kind", "inkind",
    "individual supporters and business partners",
    "shippers, tankers and large vessels",
    "donor funded, manta trust affiliate, barefoot manta resort, conservation international",
}

_KEEP_AND = (
    "gordon and betty",
    "david and lucile",
    "jeremy and hannelore",
    "parks and recreation",
    "land and natural",
    "science and technology",
    "science and engineering",
    "climate and environment",
    "food and agriculture",
    "disease control and prevention",
    "city and county",
    "fish and wildlife",
    "ocean and atmospheric",
    "oceanic and atmospheric",
    "marine and environmental",
    "marine and coastal",
    "data and information",
    "education and research",
    "nature and people",
    "arts and ",
    "research and production",
    "research and innovation",
    "management and exploitation",
    "fisheries and oceans",
    "science and research",
    "oil and gas",
    "arctic and antarctic",
    "conservation and climate",
)

_ORG_HINT = re.compile(
    r"\b(foundation|fondation|fund|trust|institut|institute|university|"
    r"universit|conservancy|alliance|commission|council|agency|society|"
    r"association|ministry|ministere|centre|center|laboratory|lab)\b",
    re.I,
)

_LAND_NAME_RE = re.compile(
    r"\b(chasse|chasseur|chasseurs|hunt|hunting|hunter|hunters)\b",
    re.I,
)

def classify_name(name: str) -> str:
    """ok / compound / exclude — avant tout Search « official site »."""
    raw = (name or "").strip()
    if is_noise_name(raw):
        return NAME_EXCLUDE
    n = norm_name(raw)
    if n in _EXCLUDE_EXACT:
        return NAME_EXCLUDE
    if n.startswith("in kind") or n.startswith("in-kind") or n.startswith("inkind"):
        return NAME_EXCLUDE
    if raw.lower().startswith("multiple ("):
        return NAME_EXCLUDE
    display = re.sub(r"\s*\(partner\)\s*$", "", raw, flags=re.I).strip()
    n = norm_name(display)
    if _LAND_NAME_RE.search(display):
        return NAME_EXCLUDE
    if display.count(",") >= 2:
        return NAME_COMPOUND
    if "," in display:
        if re.search(
            r",\s*(ministry|universit|helmholtz|department of|"
            r"far eastern|mnr\b|gmbh|inc\.?|ltd\.?)\b",
            display,
            re.I,
        ) and display.count(",") == 1:
            pass
        else:
            parts = [p.strip() for p in display.split(",") if p.strip()]
            if any(_ORG_HINT.search(p) for p in parts):
                return NAME_COMPOUND
            if len(parts) == 2 and all(_looks_like_org_part(p) for p in parts):
                return NAME_COMPOUND
    if re.search(r"\band\b", display, re.I):
        low = n
        if any(k in low for k in _KEEP_AND):
            return NAME_OK
        return NAME_COMPOUND
    return NAME_OK


WIDE_CORP_NETLOCS = frozenset({
    "axa.com", "bloomberg.org", "bloomberg.com",
})


def _looks_like_org_part(part: str) -> bool:
    """“BlueInvest” or “Corals for Conservation”: an organization, not a suffix."""
    raw = (part or "").strip()
    if not raw or classify_name(raw) == NAME_EXCLUDE:
        return False
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9'\-]*", raw) if w]
    if len(words) >= 2:
        return True
    return bool(re.match(r"^[A-ZÀ-Ÿ][A-Za-zÀ-ÿ0-9\-]{3,}$", raw))


def is_terrestrial_noise_name(name: str) -> bool:
    """Chasse / hunt : hors Follow the Money et hors Complet."""
    display = re.sub(r"\s*\(partner\)\s*$", "", (name or "").strip(), flags=re.I)
    return bool(_LAND_NAME_RE.search(display))


def _syllable_count(word: str) -> int:
    w = re.sub(r"[^a-zà-ÿ]", "", (word or "").lower())
    if not w:
        return 0
    if w.endswith("e") and len(w) > 2:
        w = w[:-1]
    groups = re.findall(r"[aeiouyàâäéèêëïîôùûü]+", w)
    return max(1, len(groups)) if groups else 1


def name_needs_official_search(name: str) -> bool:
    """Name too short or one syllable → Search “official site”, not the raw URL."""
    display = re.sub(r"\s*\(partner\)\s*$", "", (name or "").strip(), flags=re.I)
    words = re.findall(r"[A-Za-zÀ-ÿ0-9]+", display)
    if not words:
        return True
    if len(words) != 1:
        return False
    w = words[0]
    if len(w) <= 5:
        return True
    return _syllable_count(w) <= 1


def is_wide_corporate_home(seed: dict | None) -> bool:
    """Group HQ (axa.com) without an ocean / projects page → outside Complet."""
    seed = seed or {}
    url = (
        (seed.get("listing_url") or "").strip()
        or (seed.get("url") or "").strip()
        or (seed.get("home_url") or "").strip()
    )
    d = domain_of(url)
    if d not in WIDE_CORP_NETLOCS:
        return False
    if is_homepage_url(url):
        return True
    if re.search(r"ocean|marine|project|projet|fund|philanthrop", url, re.I):
        return False
    return True


def partner_site_reachable(url: str, timeout: float = 5.0) -> bool:
    """HTTPS/HTTP answers. Broken certificate or timeout → False (does not burn the cap)."""
    raw = (url or "").strip()
    if not raw.startswith("http"):
        return False
    import ssl
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    headers = {"User-Agent": "BlueIntelligence/1.0 (+https://blueintelligence.online)"}
    ctx = ssl.create_default_context()
    for method in ("HEAD", "GET"):
        try:
            req = Request(raw, method=method, headers=headers)
            with urlopen(req, timeout=timeout, context=ctx) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                if 200 <= int(code or 0) < 400:
                    return True
                if int(code or 0) in {401, 403, 405}:
                    return True
        except ssl.SSLError:
            return False
        except URLError as exc:
            reason = str(getattr(exc, "reason", exc) or "")
            if "SSL" in reason or "certificate" in reason.lower():
                return False
            continue
        except Exception:
            continue
    return False


def _best_own_domain(name: str, urls: list[str]) -> tuple[str, str]:
    """(owner domain, reason) or ('', '')."""
    counts: Counter[str] = Counter()
    social = 0
    for u in urls or []:
        d = domain_of(u)
        if not d:
            continue
        if d in SKIP_LISTING_NETLOCS:
            social += 1
            continue
        counts[d] += 1
    if not counts and social:
        return "", HOME_STATUS_SOCIAL
    if not counts:
        return "", HOME_STATUS_EMPTY
    own = []
    for d, n in counts.items():
        if is_publisher_host(d):
            continue
        if domain_matches_org(d, name) or name_owns_hub(name, d):
            own.append((n, -len(d), d))
    if own:
        own.sort(reverse=True)
        return own[0][-1], HOME_STATUS_OFFICIAL
    if all(is_publisher_host(d) for d in counts):
        return "", HOME_STATUS_PUBLISHER
    if all(is_shared_hub(d) for d in counts):
        return "", HOME_STATUS_BORROWED
    if any(is_shared_hub(d) for d in counts) and not own:
        return "", HOME_STATUS_BORROWED
    return "", HOME_STATUS_UNKNOWN


def classify_home(name: str, urls: list[str]) -> dict:
    """Classify the home without writing a borrowed catalog."""
    own, status = _best_own_domain(name, urls)
    borrowed = ""
    hubs = [
        domain_of(u) for u in (urls or [])
        if is_shared_hub(u) and domain_of(u)
    ]
    if hubs:
        borrowed = Counter(hubs).most_common(1)[0][0]
    home_url = f"https://{own}/" if own else None
    if not home_url and status == HOME_STATUS_OFFICIAL:
        status = HOME_STATUS_UNKNOWN
    return {
        "home_status": status,
        "home_url": home_url,
        "borrowed_domain": borrowed or None,
        "home_source": "v1_url" if home_url else "",
    }


def infer_own_listing(name: str, urls: list[str], home_url: str | None) -> str | None:
    """Common prefix of cards *on the owner domain*."""
    host = domain_of(home_url)
    if not host:
        return None
    if not domain_matches_org(home_url or host, name) and not name_owns_hub(name, host):
        return None
    own = [u for u in (urls or []) if domain_of(u) == host]
    if len(own) < 2:
        return None
    # The home is already official: do not re-filter via frequent hubs
    # (fondationdelamer.org porterait sinon trop de noms v1).
    inferred = infer_listing_from_project_urls(own, name, allow_shared_hub=True)
    if not inferred:
        return None
    if domain_of(inferred) != host:
        return None
    if not is_listing_url(inferred):
        return None
    return inferred


def _curated_listing_kind(c: dict) -> str:
    kind = (c.get("listing_kind") or "").strip().lower()
    if kind in {"projects_index", "home_only", "homepage", "unknown"}:
        return kind
    url = (c.get("url") or "").strip()
    if url and is_listing_url(url) and not is_homepage_url(url):
        return "projects_index"
    return "homepage"


def enrich_v1_seed(name: str, urls: list[str], project_count: int) -> dict:
    name_status = classify_name(name)
    home = classify_home(name, urls)
    listing = None
    if home["home_status"] == HOME_STATUS_OFFICIAL:
        listing = infer_own_listing(name, urls, home["home_url"])
        if not listing:
            # Owner-domain root (not a hub).
            listing = listing_url_from_project_urls(urls, name)
            if listing and domain_of(listing) != domain_of(home["home_url"]):
                listing = None
            if listing and is_listing_url(listing):
                pass
            elif listing:
                listing = None
    if listing:
        listing_kind = "projects_index"
        url = listing
    elif home["home_url"]:
        listing_kind = "homepage"
        url = home["home_url"]
    else:
        listing_kind = "unknown"
        url = None
    if name_status == NAME_EXCLUDE:
        queue = QUEUE_SKIP
    elif name_status == NAME_COMPOUND:
        queue = QUEUE_RESOLVE
    elif home["home_status"] == HOME_STATUS_OFFICIAL and url:
        queue = QUEUE_CRAWL
    else:
        queue = QUEUE_RESOLVE
    return {
        "name": name,
        "url": url,
        "listing_kind": listing_kind,
        "source": "v1",
        "project_count": int(project_count or 0),
        "home_url": home["home_url"],
        "listing_url": listing,
        "home_status": home["home_status"],
        "name_status": name_status,
        "queue": queue,
        "borrowed_domain": home["borrowed_domain"],
        "home_source": home["home_source"] or ("inferred" if listing else ""),
    }


def build_v1_catalog(projects: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = defaultdict(lambda: {"urls": [], "count": 0})
    for doc in projects:
        url = (doc.get("url") or "").strip()
        for name in funder_names_from_project(doc):
            if is_noise_name(name):
                continue
            buckets[name]["count"] += 1
            if url:
                buckets[name]["urls"].append(url)
    return [
        enrich_v1_seed(name, bucket["urls"], bucket["count"])
        for name, bucket in buckets.items()
    ]


def merge_curated_catalog(v1_seeds: list[dict], curated: list[dict] | None = None) -> list[dict]:
    """Name/alias union only: a CORDIS-hosted org is no longer dropped."""
    if curated is None:
        from app.static_data.seeds import CURATED_SEEDS
        curated = CURATED_SEEDS
    out: list[dict] = []
    used = set()

    def _matches(v: dict, c: dict) -> bool:
        return names_soft_match(c["name"], v.get("name") or "", c.get("aliases") or [])

    for c in curated:
        matches = [v for v in v1_seeds if _matches(v, c)]
        kind = _curated_listing_kind(c)
        listing_url = c["url"] if kind == "projects_index" else None
        if kind == "projects_index" and is_homepage_url(c.get("url")):
            kind = "homepage"
            listing_url = None
        home_url = None
        d = domain_of(c.get("url"))
        if d:
            home_url = f"https://{d}/"
        crawl_url = listing_url or c.get("url")
        item = {
            "name": c["name"],
            "url": crawl_url,
            "country": c.get("country"),
            "category": c.get("category"),
            "listing_kind": kind,
            "source": "curated",
            "project_count": sum(int(m.get("project_count") or 0) for m in matches),
            "aliases": list(c.get("aliases") or []),
            "home_url": home_url,
            "listing_url": listing_url,
            "home_status": HOME_STATUS_OFFICIAL,
            "name_status": NAME_OK,
            "queue": QUEUE_CRAWL if crawl_url else QUEUE_RESOLVE,
            "borrowed_domain": None,
            "home_source": "curated",
        }
        out.append(item)
        for m in matches:
            used.add(norm_name(m.get("name") or ""))

    for v in v1_seeds:
        if norm_name(v.get("name") or "") in used:
            continue
        if any(_matches(v, c) for c in curated):
            continue
        out.append({k: val for k, val in v.items() if k != "priority"})
    return out


def build_enriched_master_seeds(projects: list[dict], curated: list[dict] | None = None) -> list[dict]:
    from app.services.master_seeds import set_frequent_hubs_from_projects
    set_frequent_hubs_from_projects(projects)
    return merge_curated_catalog(build_v1_catalog(projects), curated)


def is_crawl_ready(seed: dict | None) -> bool:
    """Complet queue: official home or list page. No hub to resolve."""
    seed = seed or {}
    name = (seed.get("name") or "").strip()
    if not name:
        return False
    if is_terrestrial_noise_name(name) or is_wide_corporate_home(seed):
        return False
    q = (seed.get("queue") or "").strip().lower()
    if q in {QUEUE_RESOLVE, QUEUE_SKIP}:
        return False
    if (seed.get("name_status") or "") in {NAME_COMPOUND, NAME_EXCLUDE}:
        return False
    status = (seed.get("home_status") or "").strip().lower()
    if status in {
        HOME_STATUS_BORROWED, HOME_STATUS_PUBLISHER, HOME_STATUS_SOCIAL,
        HOME_STATUS_EMPTY, HOME_STATUS_UNKNOWN,
    }:
        return False
    url = (
        (seed.get("listing_url") or "").strip()
        or (seed.get("url") or "").strip()
        or (seed.get("home_url") or "").strip()
    )
    if q == QUEUE_CRAWL:
        return bool(url)
    if status == HOME_STATUS_OFFICIAL:
        return bool(url)
    # Follow the Money partner / legacy seed: clean URL, not a hub.
    if not url:
        return False
    from app.services.master_seeds import is_shared_hub_home, needs_official_home
    if is_shared_hub_home(seed) or needs_official_home(seed):
        return False
    return True


def catalog_summary(seeds: list[dict]) -> dict:
    def _count(key, value):
        return sum(1 for s in seeds if (s.get(key) or "") == value)

    return {
        "n": len(seeds),
        "n_with_url": sum(1 for s in seeds if s.get("url")),
        "n_crawl": sum(1 for s in seeds if is_crawl_ready(s)),
        "n_resolve": _count("queue", QUEUE_RESOLVE),
        "n_skip": _count("queue", QUEUE_SKIP),
        "n_official": _count("home_status", HOME_STATUS_OFFICIAL),
        "n_borrowed": _count("home_status", HOME_STATUS_BORROWED),
        "n_unknown_home": _count("home_status", HOME_STATUS_UNKNOWN),
        "n_compound": _count("name_status", NAME_COMPOUND),
        "n_projects_index": _count("listing_kind", "projects_index"),
        "n_home_only": _count("listing_kind", "home_only"),
        "n_homepage": _count("listing_kind", "homepage"),
        "n_listing_search": sum(
            1 for s in seeds
            if (s.get("listing_source") or "") == LISTING_SOURCE_SEARCH
            and (s.get("listing_kind") or "") == "projects_index"
        ),
        "n_split": sum(1 for s in seeds if (s.get("source") or "") == "split"),
    }


def audit_rows(seeds: list[dict]) -> list[dict]:
    rows = []
    for s in seeds:
        rows.append({
            "name": s.get("name"),
            "project_count": s.get("project_count") or 0,
            "source": s.get("source"),
            "name_status": s.get("name_status") or "",
            "home_status": s.get("home_status") or "",
            "listing_kind": s.get("listing_kind") or "",
            "queue": s.get("queue") or "",
            "home_url": s.get("home_url") or "",
            "listing_url": s.get("listing_url") or "",
            "url": s.get("url") or "",
            "borrowed_domain": s.get("borrowed_domain") or "",
            "home_source": s.get("home_source") or "",
            "listing_source": s.get("listing_source") or "",
            "split_into": ", ".join(s.get("split_into") or []),
            "review_action": s.get("review_action") or "",
        })
    rows.sort(key=lambda r: (-int(r["project_count"] or 0), (r["name"] or "").lower()))
    return rows


def _atomic_write_text(path: Path, text: str) -> Path:
    """tmp write + fsync + replace: a crash leaves the old file intact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
    return path


def apply_official_site_result(seed: dict, site: str | None) -> dict:
    """Apply a Search B hit. Keep `borrowed_domain` for audit."""
    site = (site or "").strip()
    if site:
        listing = (seed.get("listing_url") or "").strip() or None
        if listing and domain_of(listing) != domain_of(site):
            listing = None
            seed["listing_url"] = None
        seed["home_url"] = site
        seed["url"] = listing or site
        seed["home_status"] = HOME_STATUS_OFFICIAL
        seed["home_source"] = HOME_SOURCE_SEARCH
        seed["queue"] = QUEUE_CRAWL
        if not seed.get("listing_kind") or seed["listing_kind"] == "unknown":
            seed["listing_kind"] = "homepage"
        return seed
    seed["home_status"] = HOME_STATUS_UNKNOWN
    seed["queue"] = QUEUE_RESOLVE
    seed["home_source"] = HOME_SOURCE_SEARCH
    return seed


def append_search_journal(path: Path, record: dict) -> Path:
    """Append + fsync: each B result is recorded before the checkpoint."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    return path


def load_search_journal(path: Path) -> dict[str, dict]:
    """Last record by name (append-only journal)."""
    out: dict[str, dict] = {}
    path = Path(path)
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = (rec.get("name") or "").strip()
        if name:
            out[name] = rec
    return out


def journal_done_names(
    records: dict[str, dict],
    *,
    retry_unknown: bool = False,
    result_key: str | None = None,
) -> set[str]:
    """Names already Searched. Network errors are not "done" (resume)."""
    done: set[str] = set()
    for name, rec in (records or {}).items():
        if result_key:
            result = rec.get(result_key)
        else:
            result = rec.get("site") if "site" in rec else rec.get("listing")
        if rec.get("error") and not result:
            continue
        if result:
            done.add(norm_name(name))
        elif not retry_unknown:
            done.add(norm_name(name))
    return done


def search_candidates(
    seeds: list[dict],
    *,
    done_names: set[str] | None = None,
    retry_unknown: bool = False,
) -> list[dict]:
    """B seeds: name ok, borrowed/unknown home, not already Search-recorded."""
    done = {norm_name(n) for n in (done_names or set()) if n}
    out = []
    for s in seeds:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        if (s.get("name_status") or NAME_OK) != NAME_OK:
            continue
        if (s.get("queue") or "") == QUEUE_SKIP:
            continue
        key = norm_name(name)
        if key in done:
            continue
        source = (s.get("home_source") or "").strip()
        status = (s.get("home_status") or "").strip()
        if source == HOME_SOURCE_REVIEW:
            continue
        if source == HOME_SOURCE_SEARCH:
            if retry_unknown and status == HOME_STATUS_UNKNOWN:
                pass
            else:
                continue
        if status not in SEARCH_HOME_STATUSES:
            continue
        out.append(s)
    return out


def overlay_search_results(
    seeds: list[dict],
    *,
    previous: list[dict] | None = None,
    journal: Path | dict | None = None,
) -> int:
    """Re-apply Search homes (previous catalog + journal). The journal wins."""
    by = {norm_name(s.get("name") or ""): s for s in seeds}
    touched: set[str] = set()
    for old in previous or []:
        if (old.get("home_source") or "") != HOME_SOURCE_SEARCH:
            continue
        key = norm_name(old.get("name") or "")
        cur = by.get(key)
        if not cur:
            continue
        for field in _SEARCH_OVERLAY_FIELDS:
            if field in old:
                cur[field] = old[field]
        if old.get("borrowed_domain") and not cur.get("borrowed_domain"):
            cur["borrowed_domain"] = old["borrowed_domain"]
        touched.add(key)
    records: dict[str, dict]
    if isinstance(journal, dict):
        records = journal
    elif journal is not None:
        records = load_search_journal(Path(journal))
    else:
        records = {}
    for rec in records.values():
        key = norm_name(rec.get("name") or "")
        cur = by.get(key)
        if not cur:
            continue
        if rec.get("error") and not rec.get("site"):
            continue
        apply_official_site_result(cur, rec.get("site") or "")
        touched.add(key)
    return len(touched)


def write_search_progress(path: Path, payload: dict) -> Path:
    body = {"updated_at": now_iso(), **payload}
    return _atomic_write_text(Path(path), json.dumps(body, ensure_ascii=False, indent=2) + "\n")


def apply_listing_result(seed: dict, listing: str | None, *, source: str = LISTING_SOURCE_SEARCH) -> dict:
    """Set a list page on the home domain. Miss → homepage, still crawlable."""
    listing = (listing or "").strip()
    home = (seed.get("home_url") or seed.get("url") or "").strip()
    if listing:
        if home and domain_of(listing) != domain_of(home):
            listing = ""
        elif not is_listing_url(listing):
            listing = ""
    if listing:
        seed["listing_url"] = listing
        seed["url"] = listing
        seed["listing_kind"] = "projects_index"
        seed["listing_source"] = source
        if (seed.get("home_status") or "") == HOME_STATUS_OFFICIAL:
            seed["queue"] = QUEUE_CRAWL
        return seed
    seed["listing_source"] = source
    if not seed.get("listing_kind") or seed["listing_kind"] == "unknown":
        seed["listing_kind"] = "homepage"
    if home and not seed.get("url"):
        seed["url"] = home
    return seed


def listing_candidates(
    seeds: list[dict],
    *,
    done_names: set[str] | None = None,
    retry_unknown: bool = False,
) -> list[dict]:
    """Official homes without an already qualified list page."""
    done = {norm_name(n) for n in (done_names or set()) if n}
    out = []
    for s in seeds:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        if (s.get("name_status") or NAME_OK) != NAME_OK:
            continue
        if (s.get("home_status") or "") != HOME_STATUS_OFFICIAL:
            continue
        if (s.get("queue") or "") == QUEUE_SKIP:
            continue
        if (s.get("listing_kind") or "") == "home_only":
            continue
        key = norm_name(name)
        if key in done:
            continue
        listing = (s.get("listing_url") or "").strip()
        kind = (s.get("listing_kind") or "").strip()
        if kind == "projects_index" and listing and is_listing_url(listing):
            continue
        source = (s.get("listing_source") or "").strip()
        if source == LISTING_SOURCE_SEARCH and not listing and not retry_unknown:
            continue
        host = domain_of(s.get("home_url") or s.get("url"))
        if not host:
            continue
        if is_shared_hub(host) and not name_owns_hub(name, host):
            continue
        out.append(s)
    return out


def overlay_listing_results(
    seeds: list[dict],
    *,
    previous: list[dict] | None = None,
    journal: Path | dict | None = None,
) -> int:
    """Re-apply list pages (previous catalog + journal C)."""
    by = {norm_name(s.get("name") or ""): s for s in seeds}
    touched: set[str] = set()
    for old in previous or []:
        src = (old.get("listing_source") or "").strip()
        if src not in {LISTING_SOURCE_SEARCH, LISTING_SOURCE_V1}:
            continue
        if (old.get("listing_kind") or "") != "projects_index":
            continue
        key = norm_name(old.get("name") or "")
        cur = by.get(key)
        if not cur:
            continue
        apply_listing_result(cur, old.get("listing_url") or "", source=src)
        touched.add(key)
    if isinstance(journal, dict):
        records = journal
    elif journal is not None:
        records = load_search_journal(Path(journal))
    else:
        records = {}
    for rec in records.values():
        key = norm_name(rec.get("name") or "")
        cur = by.get(key)
        if not cur:
            continue
        if rec.get("error") and not rec.get("listing"):
            continue
        src = rec.get("source") or LISTING_SOURCE_SEARCH
        apply_listing_result(cur, rec.get("listing") or "", source=src)
        touched.add(key)
    return len(touched)


def infer_listings_onto_official_homes(seeds: list[dict], projects: list[dict] | None) -> int:
    """C outside Search: common prefix of v1 cards on the official home."""
    buckets: dict[str, list[str]] = defaultdict(list)
    for doc in projects or []:
        url = (doc.get("url") or "").strip()
        if not url:
            continue
        for name in funder_names_from_project(doc):
            if is_noise_name(name):
                continue
            buckets[norm_name(name)].append(url)
    n = 0
    for seed in listing_candidates(seeds):
        inferred = infer_own_listing(
            seed.get("name") or "",
            buckets.get(norm_name(seed.get("name") or ""), []),
            seed.get("home_url"),
        )
        if inferred:
            apply_listing_result(seed, inferred, source=LISTING_SOURCE_V1)
            n += 1
    return n


def apply_home_review(seed: dict, rec: dict) -> dict:
    """Human review of a home B. Wins over Search."""
    action = (rec.get("action") or REVIEW_REJECT).strip()
    seed["review_action"] = action
    seed["home_source"] = HOME_SOURCE_REVIEW
    note = (rec.get("note") or "").strip()
    if note:
        seed["review_note"] = note
    if action == REVIEW_REJECT:
        seed["home_url"] = None
        seed["url"] = None
        seed["listing_url"] = None
        seed["listing_kind"] = "unknown"
        seed["listing_source"] = ""
        seed["home_status"] = HOME_STATUS_UNKNOWN
        seed["queue"] = QUEUE_RESOLVE
        return seed
    home = (rec.get("home_url") or seed.get("home_url") or "").strip() or None
    listing = (rec.get("listing_url") or "").strip() or None
    seed["home_url"] = home
    if listing:
        seed["listing_url"] = listing
        seed["url"] = listing
        seed["listing_kind"] = "projects_index"
        seed["listing_source"] = HOME_SOURCE_REVIEW
    elif rec.get("listing_kind") == "home_only" or action == REVIEW_NO_PROJECTS:
        seed["listing_url"] = None
        seed["url"] = home
        seed["listing_kind"] = "home_only"
        seed["listing_source"] = HOME_SOURCE_REVIEW
    if home:
        seed["home_status"] = HOME_STATUS_OFFICIAL
    if action == REVIEW_NO_PROJECTS:
        seed["queue"] = QUEUE_SKIP
    else:
        seed["queue"] = assign_queue(seed)
    return seed


def overlay_home_reviews(seeds: list[dict], reviews: list[dict] | Path | None) -> int:
    """Re-apply the manual review (wins over B/C)."""
    if reviews is None:
        return 0
    if isinstance(reviews, Path):
        if not reviews.is_file():
            return 0
        payload = json.loads(reviews.read_text(encoding="utf-8"))
        rows = payload.get("reviews") if isinstance(payload, dict) else payload
    else:
        rows = reviews
    by = {norm_name(s.get("name") or ""): s for s in seeds}
    n = 0
    for rec in rows or []:
        key = norm_name(rec.get("name") or "")
        cur = by.get(key)
        if not cur:
            continue
        apply_home_review(cur, rec)
        n += 1
    return n


def assign_queue(seed: dict) -> str:
    """Step E: Complet queue = official home or list page, simple name."""
    action = (seed.get("review_action") or "").strip()
    if action == REVIEW_REJECT:
        return QUEUE_RESOLVE
    if action == REVIEW_NO_PROJECTS:
        return QUEUE_SKIP
    if seed.get("split_into"):
        return QUEUE_SKIP
    if is_terrestrial_noise_name(seed.get("name") or ""):
        return QUEUE_SKIP
    name_status = (seed.get("name_status") or NAME_OK).strip()
    if name_status == NAME_EXCLUDE:
        return QUEUE_SKIP
    if name_status == NAME_COMPOUND:
        return QUEUE_RESOLVE
    if is_wide_corporate_home(seed):
        return QUEUE_RESOLVE
    status = (seed.get("home_status") or "").strip()
    if status in {
        HOME_STATUS_BORROWED, HOME_STATUS_PUBLISHER, HOME_STATUS_SOCIAL,
        HOME_STATUS_EMPTY, HOME_STATUS_UNKNOWN,
    }:
        return QUEUE_RESOLVE
    url = (
        (seed.get("listing_url") or "").strip()
        or (seed.get("url") or "").strip()
        or (seed.get("home_url") or "").strip()
    )
    if status == HOME_STATUS_OFFICIAL and url:
        return QUEUE_CRAWL
    if url and status == HOME_STATUS_OFFICIAL:
        return QUEUE_CRAWL
    return QUEUE_RESOLVE


def refresh_catalog_queues(seeds: list[dict]) -> list[dict]:
    """Recompute `queue` for the whole catalogue (E)."""
    for seed in seeds:
        seed["queue"] = assign_queue(seed)
    return seeds


def refresh_catalog_classifications(seeds: list[dict]) -> list[dict]:
    """Recompute `name_status` (except curated) then `queue`. No D splits."""
    for seed in seeds:
        if (seed.get("source") or "") != "curated":
            seed["name_status"] = classify_name(seed.get("name") or "")
        seed["queue"] = assign_queue(seed)
    return seeds


_AND_DELIM = re.compile(r"\s*(?:&|\band\b)\s*", re.I)
_COMMA_DELIM = re.compile(r"\s*[,;]\s*")
_ACRONYM_PART = re.compile(r"^[A-Z][A-Z0-9]{2,7}$")
_ACRONYM_PREFIX = re.compile(r"^[A-Z]{2,8}\b")


def _strip_partner(name: str) -> str:
    return re.sub(r"\s*\(partner\)\s*$", "", (name or "").strip(), flags=re.I).strip()


def _is_org_part(part: str, seeds: list[dict] | None = None) -> bool:
    """Avoid splitting "Science, Technology and …" into fake organizations."""
    part = _strip_partner(part)
    if not part or classify_name(part) != NAME_OK:
        return False
    if seeds and _find_seed_by_name(seeds, part):
        return True
    if _ORG_HINT.search(part):
        return True
    if _ACRONYM_PART.match(part):
        return True
    if _ACRONYM_PREFIX.match(part) and len(part) >= 6:
        return True
    return False


def split_compound_parts(name: str, seeds: list[dict] | None = None) -> list[str] | None:
    """Split a glued name. None if a single organization or parts too vague."""
    if classify_name(name) != NAME_COMPOUND:
        return None
    raw = _strip_partner(name)
    and_parts = [p.strip(" .") for p in _AND_DELIM.split(raw) if p.strip(" .")]
    if len(and_parts) >= 2 and all(_is_org_part(p, seeds) for p in and_parts):
        return [_strip_partner(p) for p in and_parts]
    comma_parts = [p.strip(" .") for p in _COMMA_DELIM.split(raw) if p.strip(" .")]
    if len(comma_parts) >= 2 and all(_is_org_part(p, seeds) for p in comma_parts):
        return [_strip_partner(p) for p in comma_parts]
    return None


def _find_seed_by_name(seeds: list[dict], name: str) -> dict | None:
    for seed in seeds:
        if names_soft_match(seed.get("name") or "", name, seed.get("aliases") or []):
            return seed
    return None


def apply_compound_splits(seeds: list[dict]) -> dict:
    """D: reclassify fake compounds, split real ones, exclude the split source."""
    report = {
        "reclassified_ok": [],
        "merged": [],
        "created": [],
        "split_sources": [],
        "unsplittable": [],
    }
    for seed in list(seeds):
        if (seed.get("name_status") or "") != NAME_COMPOUND:
            continue
        name = (seed.get("name") or "").strip()
        if classify_name(name) == NAME_OK:
            seed["name_status"] = NAME_OK
            report["reclassified_ok"].append(name)
            continue
        parts = split_compound_parts(name, seeds)
        if not parts:
            report["unsplittable"].append(name)
            continue
        handled = []
        leftover = []
        for part in parts:
            existing = _find_seed_by_name(seeds, part)
            if existing:
                aliases = list(existing.get("aliases") or [])
                if name not in aliases and norm_name(name) != norm_name(existing.get("name") or ""):
                    aliases.append(name)
                    existing["aliases"] = aliases
                report["merged"].append({"from": name, "into": existing.get("name"), "part": part})
                handled.append(part)
                continue
            if len(norm_name(part)) < MIN_SPLIT_PART:
                leftover.append(part)
                continue
            created = {
                "name": part,
                "url": None,
                "listing_kind": "unknown",
                "source": "split",
                "project_count": 0,
                "home_url": None,
                "listing_url": None,
                "home_status": HOME_STATUS_EMPTY,
                "name_status": NAME_OK,
                "queue": QUEUE_RESOLVE,
                "borrowed_domain": None,
                "home_source": "",
                "split_from": name,
            }
            seeds.append(created)
            report["created"].append({"from": name, "name": part})
            handled.append(part)
        if handled and not leftover and len(handled) == len(parts):
            seed["split_into"] = parts
            seed["queue"] = QUEUE_SKIP
            report["split_sources"].append(name)
        elif not handled:
            report["unsplittable"].append(name)
    refresh_catalog_queues(seeds)
    report["counts"] = {
        "reclassified_ok": len(report["reclassified_ok"]),
        "merged": len(report["merged"]),
        "created": len(report["created"]),
        "split_sources": len(report["split_sources"]),
        "unsplittable": len(report["unsplittable"]),
        "n": len(seeds),
    }
    return report


def persist_catalog(
    seeds: list[dict],
    catalog_path: Path,
    audit_path: Path | None = None,
    source: str = "",
) -> Path:
    """Checkpoint catalogue + audit (atomiques)."""
    path = dump_catalog(seeds, catalog_path, source=source)
    if audit_path is not None:
        write_audit(seeds, audit_path, source=source)
    return path


def write_audit(seeds: list[dict], path: Path, source: str = "") -> Path:
    path = Path(path)
    payload = {
        "generated_at": now_iso(),
        "source": source,
        "summary": catalog_summary(seeds),
        "rows": audit_rows(seeds),
    }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    csv_path = path.with_suffix(".csv")
    rows = payload["rows"]
    if rows:
        tmp = csv_path.with_name(csv_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(csv_path)
    return path


def dump_catalog(seeds: list[dict], path: Path | None = None, source: str = "") -> Path:
    from app.services.master_seeds import MASTER_SEEDS_PATH
    path = Path(path or MASTER_SEEDS_PATH)
    extra = catalog_summary(seeds)
    payload = {
        "generated_at": now_iso(),
        "source": source,
        **extra,
        "seeds": [{k: v for k, v in s.items() if k != "priority"} for s in seeds],
    }
    return _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def find_catalog_seed(
    catalog: list[dict] | None,
    name: str,
    url: str | None = None,
) -> dict | None:
    """Same organization as the partner: name/alias, else unique domain."""
    seeds = catalog or []
    found = _find_seed_by_name(seeds, name)
    if found:
        return found
    domain = domain_of(url)
    if not domain or is_shared_hub(domain):
        return None
    for seed in seeds:
        for key in ("url", "listing_url", "home_url"):
            if domain_of(seed.get(key)) == domain:
                return seed
    return None


def accept_partner_url(name: str, url: str | None) -> dict | None:
    """Official home (or list page): not a journal, not a borrowed hub."""
    raw = (url or "").strip()
    if not raw.startswith("http"):
        return None
    href = raw.split("#")[0].split("?")[0]
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    domain = domain_of(href)
    if not domain or domain in SKIP_LISTING_NETLOCS:
        return None
    if is_publisher_host(href):
        return None
    if is_shared_hub(domain) and not name_owns_hub(name, domain):
        return None
    if not domain_matches_org(href, name):
        return None
    home = f"{parsed.scheme}://{parsed.netloc}/"
    listing = is_listing_url(href)
    crawl = href if listing else home
    return {
        "name": (name or "").strip(),
        "url": crawl,
        "home_url": home,
        "listing_url": crawl if listing else None,
        "listing_kind": "projects_index" if listing else "homepage",
        "home_status": HOME_STATUS_OFFICIAL,
        "name_status": NAME_OK,
        "queue": QUEUE_CRAWL,
        "source": "follow_the_money",
        "home_source": "partner_url",
        "project_count": 0,
    }


def catalog_partner_seed(seed: dict) -> dict | None:
    """Complet seed: already classified listing or home, not the raw partner URL."""
    url = (
        (seed.get("listing_url") or "").strip()
        or (seed.get("url") or "").strip()
        or (seed.get("home_url") or "").strip()
    )
    if not url:
        return None
    kind = (seed.get("listing_kind") or "").strip() or "homepage"
    if (seed.get("listing_url") or "").strip() and is_listing_url(seed.get("listing_url")):
        kind = "projects_index"
    elif is_listing_url(url):
        kind = "projects_index"
    elif is_homepage_url(url) and kind == "projects_index":
        kind = "homepage"
    return {
        "name": (seed.get("name") or "").strip(),
        "url": url,
        "home_url": (seed.get("home_url") or "").strip() or url,
        "listing_url": (seed.get("listing_url") or "").strip() or None,
        "listing_kind": kind,
        "home_status": HOME_STATUS_OFFICIAL,
        "name_status": NAME_OK,
        "queue": QUEUE_CRAWL,
        "source": seed.get("source") or "catalog",
        "home_source": seed.get("home_source") or seed.get("source") or "catalog",
        "aliases": list(seed.get("aliases") or []),
        "project_count": int(seed.get("project_count") or 0),
    }


def decide_follow_the_money_partner(
    name: str,
    url: str | None = None,
    *,
    catalog: list[dict] | None = None,
    searched_home: str | None = None,
    did_search: bool = False,
) -> dict:
    """Same scale as catalog A–E: simple name, official home, no hub.

    `did_search` distinguishes "official-site Search not launched yet" from a
    Search already tried (empty or rejected).
    """
    raw_name = (name or "").strip()
    raw_url = (url or "").strip() or None
    home = (searched_home or "").strip() or None
    empty = {
        "action": FTM_SKIP, "reason": "empty_name",
        "seed": None, "needs_search": False,
    }
    if not raw_name:
        return empty
    if is_terrestrial_noise_name(raw_name):
        return {**empty, "reason": "exclude"}
    status = classify_name(raw_name)
    if status == NAME_EXCLUDE:
        return {**empty, "reason": "exclude"}
    if status == NAME_COMPOUND:
        return {**empty, "reason": "compound"}

    found = find_catalog_seed(catalog, raw_name, raw_url)
    if found:
        if is_crawl_ready(found):
            seed = catalog_partner_seed(found)
            if seed:
                return {
                    "action": FTM_DISCOVER, "reason": "catalog",
                    "seed": seed, "needs_search": False,
                }
        return {**empty, "reason": "catalog_not_ready"}

    # Name too short / one syllable: Search required, never the raw URL
    # (Wacan → wacan.com, chasse → chasseurdefrance.com).
    if name_needs_official_search(raw_name):
        if not did_search:
            return {
                "action": FTM_SEARCH, "reason": "short_name",
                "seed": None, "needs_search": True,
            }
        if home:
            accepted = accept_partner_url(raw_name, home)
            if accepted:
                accepted["home_source"] = "search"
                return {
                    "action": FTM_DISCOVER, "reason": "search",
                    "seed": accepted, "needs_search": False,
                }
            return {**empty, "reason": "search_rejected"}
        return {**empty, "reason": "search_empty"}

    accepted = accept_partner_url(raw_name, raw_url) if raw_url else None
    if accepted:
        return {
            "action": FTM_DISCOVER, "reason": "partner_url",
            "seed": accepted, "needs_search": False,
        }

    if did_search:
        if home:
            accepted = accept_partner_url(raw_name, home)
            if accepted:
                accepted["home_source"] = "search"
                return {
                    "action": FTM_DISCOVER, "reason": "search",
                    "seed": accepted, "needs_search": False,
                }
            return {**empty, "reason": "search_rejected"}
        return {**empty, "reason": "search_empty"}

    return {
        "action": FTM_SEARCH, "reason": "needs_official_site",
        "seed": None, "needs_search": True,
    }


FTM_MIN_S_OCEAN = 0.7

_UNI_NAME = re.compile(
    r"\b(university|universit[eé]|universidad|school of medicine|"
    r"institute of technology|indian institute)\b",
    re.I,
)


def ftm_page_allows_collect(
    published: bool,
    s_ocean=None,
    min_s_ocean: float = FTM_MIN_S_OCEAN,
) -> bool:
    """FTM only votes from a published site. Missing S_ocean = tests / bare excerpts."""
    if not published:
        return False
    if s_ocean is None or s_ocean == "":
        return True
    try:
        score = float(s_ocean)
    except (TypeError, ValueError):
        return False
    return score >= float(min_s_ocean)


def _ftm_inbox_penalty(name: str, url: str | None) -> float:
    """Univ / .edu: do not drop, push back in the ranking."""
    penalty = 0.0
    if _UNI_NAME.search(name or ""):
        penalty += 20.0
    host = domain_of(url) or ""
    if host.endswith(".edu") or ".ac." in host:
        penalty += 20.0
    return penalty


def rank_ftm_inbox(entries: list[dict]) -> list[dict]:
    """One row per organization: mentions × 10 + URL + max S_ocean − univ."""
    groups: dict[str, dict] = {}
    for raw in entries or []:
        seed = raw.get("seed") if isinstance(raw.get("seed"), dict) else {}
        name = (seed.get("name") or raw.get("name") or "").strip()
        if not name:
            continue
        key = norm_name(name)
        url = (seed.get("url") or raw.get("url") or "").strip()
        try:
            s_ocean = float(raw.get("s_ocean") or 0)
        except (TypeError, ValueError):
            s_ocean = 0.0
        cur = groups.get(key)
        if not cur:
            cur = {
                "name": name,
                "seed": dict(seed) if seed else None,
                "url": url,
                "mentions": 0,
                "max_s_ocean": s_ocean,
                "has_url": bool(url),
            }
            groups[key] = cur
        cur["mentions"] += 1
        cur["max_s_ocean"] = max(float(cur.get("max_s_ocean") or 0), s_ocean)
        if url and not cur.get("url"):
            cur["url"] = url
        if seed and (not cur.get("seed") or (seed.get("url") and not (cur["seed"] or {}).get("url"))):
            cur["seed"] = dict(seed)
        cur["has_url"] = bool(cur.get("url") or (cur.get("seed") or {}).get("url"))
    ranked = list(groups.values())
    for row in ranked:
        url = row.get("url") or (row.get("seed") or {}).get("url")
        row["score"] = (
            int(row["mentions"]) * 10
            + (1.0 if row.get("has_url") else 0.0)
            + float(row.get("max_s_ocean") or 0)
            - _ftm_inbox_penalty(row.get("name") or "", url)
        )
    ranked.sort(key=lambda r: (-float(r["score"]), (r.get("name") or "").lower()))
    return ranked
