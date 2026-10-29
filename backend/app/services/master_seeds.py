"""Expanded MasterSeeds (Projects CDC C5 / §18 / §22).

Union of v1 funders + 21 curated listings. Follow the Money writes into
the same table: the `max_partner_orgs` cap applies only to
*new* organizations, not to a foundation already seen in v1.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BACKEND_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BACKEND_DIR / "data"
MASTER_SEEDS_PATH = DATA_DIR / "master_seeds.json"

NOISE_NAMES = {"unknown", "n/a", "none", "null", "-", "?"}

SKIP_LISTING_NETLOCS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "youtu.be", "linkedin.com", "google.com", "wikipedia.org", "bit.ly",
    "tinyurl.com", "t.co", "goo.gl", "maps.google.com",
}

# Journals / publishers: a SERP hit is not the organization's site (BMKG ≠ nature.com).
PUBLISHER_NETLOCS = {
    "nature.com", "springer.com", "springerlink.com", "springernature.com",
    "wiley.com", "onlinelibrary.wiley.com", "sciencedirect.com", "elsevier.com",
    "tandfonline.com", "taylorandfrancis.com", "mdpi.com", "frontiersin.org",
    "hindawi.com", "researchgate.net", "academia.edu", "theconversation.com",
    "phys.org", "eurekalert.org", "science.org", "sciencemag.org", "pnas.org",
    "cell.com", "lancet.com", "nejm.org", "sciencedaily.com", "newscientist.com",
    "academic.oup.com", "iop.org", "iopscience.iop.org",
}

_INITIAL_STOP = {
    "the", "of", "for", "and", "de", "la", "le", "et", "a", "an", "in", "on",
    "at", "to", "by", "or", "und", "der", "die", "das",
}

# Too short / too common to glue a domain (Pew/Oak/WWF stay).
_TOKEN_STOP_3 = _INITIAL_STOP | {
    "usa", "org", "com", "net", "www", "inc", "ltd", "llc", "new", "old",
    "red", "bay", "sea", "ngo",
}

# “Blue Carbon” ≠ bluenaturalcapital.org; fall back to the tokens
# complete if the name has nothing left after this filter.
_GENERIC_ORG_TOKENS = {
    "blue", "ocean", "oceans", "marine", "fund", "fonds", "foundation",
    "fondation", "institute", "institut", "university", "universite",
    "national", "federal", "ministry", "ministere", "coastal",
    "california", "climate", "research", "project", "projects",
    "conservation", "environment", "environmental", "international",
    "global", "alliance", "initiative", "program", "programme",
    "agency", "commission", "council", "center", "centre", "group",
    "trust", "society", "association", "organization", "organisation",
    "germany", "france", "united", "states", "america",
}

# A domain carried by ≥ N distinct v1 names is a shared catalog
# (surfrider.org = 99 funders, not the Coastal Commission).
_FREQUENT_HUB_MIN = 3
_FREQUENT_HUBS: frozenset[str] | None = None

# Shared catalogs: many v1 funders have this "home" because
# that their cards live on the hub (Decade Actions, HUB Ocean), not on
# the organization's site. This is not a home to crawl.
SHARED_HUB_NETLOCS = {
    "oceandecade.org",
    "hubocean.earth",
    "surfrider.org",
    "archive.oceanx.org",
    "oceanx.org",
    "oceanriskalliance.org",
    "cordis.europa.eu",
    "iwlearn.net",
}

# The hub IS the home of these names (the secretariat, not a hosted org).
HUB_OWNER_TOKENS = {
    "oceandecade.org": (
        "ocean decade",
        "un ocean decade",
        "ocean decade team",
    ),
    "hubocean.earth": (
        "hub ocean",
        "hubocean",
    ),
    "surfrider.org": (
        "surfrider",
        "surfrider foundation",
    ),
    "archive.oceanx.org": (
        "oceanx",
    ),
    "oceanx.org": (
        "oceanx",
    ),
    "oceanriskalliance.org": (
        "ocean risk alliance",
        "orraa",
    ),
    "cordis.europa.eu": (
        "cordis",
        "cordis europe",
    ),
    "iwlearn.net": (
        "iwlearn",
    ),
}


def _curated_seeds() -> list[dict]:
    from app.static_data.seeds import CURATED_SEEDS
    return CURATED_SEEDS


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def norm_name(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_noise_name(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    if n.lower() in NOISE_NAMES:
        return True
    if n.upper().startswith("TEST_"):
        return True
    return False


def frequent_hub_netlocs() -> frozenset[str]:
    """v1 domains shared by several organizations (except Decade / HUB)."""
    global _FREQUENT_HUBS
    if _FREQUENT_HUBS is not None:
        return _FREQUENT_HUBS
    by: dict[str, set[str]] = defaultdict(set)
    try:
        raw = json.loads(MASTER_SEEDS_PATH.read_text(encoding="utf-8"))
        seeds = raw if isinstance(raw, list) else (raw.get("seeds") or [])
    except Exception:
        seeds = []
    for item in seeds:
        if not isinstance(item, dict):
            continue
        d = domain_of(item.get("url"))
        n = (item.get("name") or "").strip()
        if d and n:
            by[d].add(norm_name(n))
    curated_doms = {domain_of(c.get("url")) for c in _curated_seeds()}
    _FREQUENT_HUBS = frozenset(
        d for d, names in by.items()
        if len(names) >= _FREQUENT_HUB_MIN and d not in curated_doms
    )
    return _FREQUENT_HUBS


def reset_frequent_hubs() -> None:
    """Tests / rebuild catalogue."""
    global _FREQUENT_HUBS
    _FREQUENT_HUBS = None


def set_frequent_hubs_from_projects(projects: list[dict]) -> frozenset[str]:
    """Domains shared by ≥ N distinct funders in the v1 GeoJSON."""
    global _FREQUENT_HUBS
    by: dict[str, set[str]] = defaultdict(set)
    for doc in projects or []:
        d = domain_of(doc.get("url"))
        if not d:
            continue
        for name in funder_names_from_project(doc):
            if is_noise_name(name):
                continue
            by[d].add(norm_name(name))
    curated_doms = {domain_of(c.get("url")) for c in _curated_seeds()}
    _FREQUENT_HUBS = frozenset(
        d for d, names in by.items()
        if len(names) >= _FREQUENT_HUB_MIN and d not in curated_doms
    )
    return _FREQUENT_HUBS


def is_shared_hub(url_or_domain: str | None) -> bool:
    """True if the host is a shared catalog (Decade, Surfrider v1, …)."""
    d = domain_of(url_or_domain)
    if not d:
        raw = (url_or_domain or "").strip().lower().replace("www.", "")
        d = raw.split("/")[0]
    if not d:
        return False
    if d in SHARED_HUB_NETLOCS:
        return True
    return d in frequent_hub_netlocs()


def name_owns_hub(name: str, url_or_domain: str | None) -> bool:
    """The funder *is* the hub (Ocean Decade, HUB Ocean), not a hosted org."""
    d = domain_of(url_or_domain)
    if not d:
        raw = (url_or_domain or "").strip().lower().replace("www.", "")
        d = raw.split("/")[0]
    n = norm_name(name)
    if not n or d not in HUB_OWNER_TOKENS:
        return False
    for owner in HUB_OWNER_TOKENS[d]:
        on = norm_name(owner)
        if not on:
            continue
        if n == on:
            return True
        # Surfrider chapters; not “Ocean Decade Programme …” programs.
        if d == "surfrider.org" and n.startswith(on + " "):
            return True
    return False


def is_shared_hub_home(seed: dict | None) -> bool:
    """Registered home = borrowed hub, not this organization's site."""
    seed = seed or {}
    url = (seed.get("url") or "").strip()
    name = seed.get("name") or ""
    if not is_shared_hub(url):
        return False
    if name_owns_hub(name, url):
        return False
    if domain_matches_org(url, name):
        return False
    return True


def needs_official_home(seed: dict | None) -> bool:
    """No URL, journal, or borrowed v1 home → look for the real site."""
    seed = seed or {}
    url = (seed.get("url") or "").strip()
    if not url:
        return True
    if is_publisher_host(url):
        return True
    return is_shared_hub_home(seed)


def official_site_query(name: str) -> str:
    """One shot: “BMKG official site”. No site:hub."""
    n = (name or "").strip()
    return f'"{n}" official site' if n else ""


def official_site_retry_query(name: str) -> str | None:
    """2nd shot if the long name yields nothing: acronym in parentheses."""
    m = re.search(r"\(([A-Z][A-Z0-9]{1,7})\)", name or "")
    if not m:
        return None
    return f'"{m.group(1)}" official website'


def is_publisher_host(url_or_domain: str | None) -> bool:
    """True if the host is a journal / publisher, not the organization."""
    d = domain_of(url_or_domain)
    if not d:
        raw = (url_or_domain or "").strip().lower().replace("www.", "")
        d = raw.split("/")[0]
    if not d:
        return False
    for pub in PUBLISHER_NETLOCS:
        if d == pub or d.endswith("." + pub):
            return True
    return False


def official_name_tokens(name: str) -> list[str]:
    """Tokens to match a domain: words, acronyms (BMFTR, Pew, WWF)."""
    tokens = [t for t in norm_name(name).split() if len(t) >= 4]
    for t in norm_name(name).split():
        if len(t) == 3 and t not in _TOKEN_STOP_3 and t not in tokens:
            tokens.append(t)
    for ac in re.findall(r"\b([A-Z]{2,7})\b", name or ""):
        al = ac.lower()
        if al not in tokens and al not in _TOKEN_STOP_3:
            tokens.append(al)
    m = re.search(r"\(([A-Z][A-Z0-9]{1,7})\)", name or "")
    if m:
        ac = m.group(1).lower()
        if len(ac) >= 2 and ac not in tokens:
            tokens.append(ac)
    words = [
        w for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", name or "")
        if w.lower() not in _INITIAL_STOP
    ]
    while words and words[-1].lower() in {"inc", "ltd", "llc", "gmbh"}:
        words.pop()
    if len(words) >= 3:
        for k in (3, 4, min(5, len(words))):
            initials = "".join(w[0] for w in words[:k]).lower()
            if 3 <= len(initials) <= 6 and initials not in tokens:
                tokens.append(initials)
    elif 2 <= len(words) <= 5:
        initials = "".join(w[0] for w in words).lower()
        if 3 <= len(initials) <= 6 and initials not in tokens:
            tokens.append(initials)
    return tokens


def domain_matches_org(url_or_domain: str | None, name: str) -> bool:
    """The domain carries a distinctive name token (not just “blue”)."""
    d = domain_of(url_or_domain)
    if not d:
        raw = (url_or_domain or "").strip().lower().replace("www.", "")
        d = raw.split("/")[0]
    if not d:
        return False
    compact = d.replace(".", "").replace("-", "")
    tokens = official_name_tokens(name)
    if d in SHARED_HUB_NETLOCS:
        return name_owns_hub(name, d)
    compact_name = norm_name(name).replace(" ", "")
    if compact_name and len(compact_name) >= 5 and compact_name in compact:
        return True
    distinctive = [t for t in tokens if t not in _GENERIC_ORG_TOKENS]
    first_label = d.split(".")[0].replace("-", "")
    if is_shared_hub(d):
        distinctive = [
            t for t in distinctive
            if len(t) >= 4 or t == first_label
        ]
        if not distinctive:
            return False
        use = distinctive
    else:
        use = distinctive or tokens
    return bool(use) and any(t in compact for t in use)


def names_match(a: str, b: str, aliases: list | None = None) -> bool:
    if not a or not b:
        return False
    na, nb = norm_name(a), norm_name(b)
    if na and na == nb:
        return True
    for al in aliases or []:
        nal = norm_name(al)
        if nal and nal in (na, nb):
            return True
    return False


_NAME_ARTICLES = ("the ", "la ", "le ", "les ", "el ", "l ", "los ", "las ")
_NAME_SUFFIXES = (
    " foundation", " fondation", " fund", " trust",
    " institute", " institut",
)


def _strip_name_articles(n: str) -> str:
    s = n
    changed = True
    while changed:
        changed = False
        for art in _NAME_ARTICLES:
            if s.startswith(art):
                s = s[len(art):]
                changed = True
    return s


def names_soft_match(a: str, b: str, aliases: list | None = None) -> bool:
    """Same organization: articles / Foundation suffix, acronym alias (WWF)."""
    if names_match(a, b, aliases):
        return True
    na = _strip_name_articles(norm_name(a))
    nb = _strip_name_articles(norm_name(b))
    if na and na == nb:
        return True
    for x, y in ((na, nb), (nb, na)):
        if len(x.split()) < 2:
            continue
        if y.startswith(x + " ") and y[len(x):] in _NAME_SUFFIXES:
            return True
    for al in aliases or []:
        nal = norm_name(al)
        if not nal or " " in nal or not (2 <= len(nal) <= 6):
            continue
        # Both names must carry the acronym. Else “WWF Oceans”
        # (alias WWF) collerait n'importe quel partenaire.
        a_has = na == nal or na.startswith(nal + " ")
        b_has = nb == nal or nb.startswith(nal + " ")
        if a_has and b_has:
            return True
    wa = re.findall(r"[A-Za-zÀ-ÿ0-9]+", a or "")
    wb = re.findall(r"[A-Za-zÀ-ÿ0-9]+", b or "")
    if (
        wa and wb
        and len(wa) >= 2 and len(wb) >= 2
        and wa[0].lower() == wb[0].lower()
        and wa[0].isupper() and wb[0].isupper()
        and 2 <= len(wa[0]) <= 5
        and wa[0].lower() not in _GENERIC_ORG_TOKENS
        and wa[0].lower() not in _INITIAL_STOP
    ):
        return True
    return False


def prefer_official_home(seed: dict | None) -> dict:
    """If the home is official, do not crawl a reattached hub (Mongo / v1)."""
    item = dict(seed or {})
    name = item.get("name") or ""
    home = (item.get("home_url") or "").strip()
    url = (item.get("url") or "").strip()
    if (item.get("home_status") or "").strip().lower() != "official":
        return item
    official = home or url
    if not official:
        return item
    if url and is_shared_hub(url) and not name_owns_hub(name, url):
        item["url"] = home or official
        if domain_of(url) != domain_of(item["url"]):
            item["listing_kind"] = "homepage"
            if domain_of(item.get("listing_url") or "") != domain_of(item["url"]):
                item["listing_url"] = None
    return item


def listing_url_from_project_urls(urls: list[str], funder_name: str = "") -> str | None:
    """Root of the *owner* domain — never a borrowed hub."""
    counts: Counter[str] = Counter()
    for u in urls:
        d = domain_of(u)
        if not d or d in SKIP_LISTING_NETLOCS or is_publisher_host(d):
            continue
        if is_shared_hub(d) and not domain_matches_org(d, funder_name) and not name_owns_hub(funder_name, d):
            continue
        counts[d] += 1
    if not counts:
        return None
    scored = []
    for d, n in counts.items():
        own = domain_matches_org(d, funder_name) or name_owns_hub(funder_name, d)
        scored.append((2 if own else 0, n, -len(d), d))
    scored.sort(reverse=True)
    if scored[0][0] < 2:
        return None
    return f"https://{scored[0][-1]}/"


def funder_names_from_project(doc: dict) -> list[str]:
    """Names as stored. Do not re-split an already joined string."""
    raw = doc.get("funders")
    if isinstance(raw, list) and raw:
        return [str(x).strip() for x in raw if str(x).strip()]
    one = (doc.get("funder") or "").strip()
    return [one] if one else []


def build_v1_seeds(projects: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = defaultdict(lambda: {"urls": [], "count": 0})
    for doc in projects:
        url = (doc.get("url") or "").strip()
        for name in funder_names_from_project(doc):
            if is_noise_name(name):
                continue
            buckets[name]["count"] += 1
            if url:
                buckets[name]["urls"].append(url)
    seeds = []
    for name, bucket in buckets.items():
        listing = listing_url_from_project_urls(bucket["urls"], name)
        seeds.append({
            "name": name,
            "url": listing,
            "listing_kind": "homepage" if listing else "unknown",
            "source": "v1",
            "project_count": bucket["count"],
        })
    return seeds


def merge_curated(v1_seeds: list[dict], curated: list[dict] | None = None) -> list[dict]:
    """Union by name/alias. A funder hosted on CORDIS / FPA2 is not dropped."""
    curated = curated if curated is not None else _curated_seeds()
    out: list[dict] = []
    used = set()

    def _matches_curated(v: dict, c: dict) -> bool:
        return names_soft_match(c["name"], v.get("name") or "", c.get("aliases") or [])

    for c in curated:
        matches = [v for v in v1_seeds if _matches_curated(v, c)]
        match = matches[0] if matches else None
        kind = (c.get("listing_kind") or "").strip() or "projects_index"
        item = {
            "name": c["name"],
            "url": c["url"],
            "country": c.get("country"),
            "category": c.get("category"),
            "listing_kind": kind,
            "source": "curated",
            "project_count": sum(int(m.get("project_count") or 0) for m in matches) or int((match or {}).get("project_count") or 0),
            "aliases": list(c.get("aliases") or []),
        }
        out.append(item)
        for m in matches:
            used.add(norm_name(m.get("name") or ""))

    for v in v1_seeds:
        if norm_name(v.get("name") or "") in used:
            continue
        if any(_matches_curated(v, c) for c in curated):
            continue
        item = {k: val for k, val in v.items() if k != "priority"}
        item.setdefault("listing_kind", "homepage" if v.get("url") else "unknown")
        item.setdefault("source", "v1")
        item["project_count"] = int(v.get("project_count") or 0)
        out.append(item)
    return out


def build_master_seeds(projects: list[dict], curated: list[dict] | None = None) -> list[dict]:
    from app.services.seed_catalog import build_enriched_master_seeds
    return build_enriched_master_seeds(projects, curated)


def _without_priority(seed: dict) -> dict:
    return {k: v for k, v in seed.items() if k != "priority"}


def seeds_for_run(seeds: list[dict]) -> list[dict]:
    """Complet queue: curated first, then high project_count. No A–Z alphabet.

    FTM spends its tickets on partners of real portals (Pew, WWF),
    not on the first "Alpha / Brevard" page of the JSON.
    """
    from app.services.seed_catalog import is_crawl_ready
    ready = [s for s in seeds if is_crawl_ready(s)]

    def _key(s: dict) -> tuple:
        curated = 0 if (s.get("source") or "") == "curated" else 1
        count = -int(s.get("project_count") or 0)
        return (curated, count, (s.get("name") or "").lower())

    return sorted(ready, key=_key)


def is_known_funder(seeds: list[dict], name: str | None = None, url: str | None = None) -> bool:
    domain = domain_of(url)
    n = norm_name(name or "")
    for s in seeds:
        if domain and domain_of(s.get("url")) == domain and not is_shared_hub(domain):
            return True
        aliases = s.get("aliases") or []
        if n and names_match(s.get("name") or "", name or "", aliases):
            return True
    return False


def listing_url_for_name(seeds: list[dict], name: str) -> str | None:
    for s in seeds:
        if names_match(s.get("name") or "", name, s.get("aliases") or []) and s.get("url"):
            return s["url"]
    return None


def load_master_seeds(path: Path | None = None) -> list[dict]:
    path = path or MASTER_SEEDS_PATH
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        seeds = data.get("seeds") if isinstance(data, dict) else data
        if isinstance(seeds, list) and seeds:
            return [_without_priority(s) if isinstance(s, dict) else s for s in seeds]
    return [
        _without_priority({
            **s, "source": "curated", "listing_kind": "projects_index", "project_count": 0,
        })
        for s in _curated_seeds()
    ]


def dump_master_seeds(seeds: list[dict], path: Path | None = None, source: str = "") -> Path:
    path = path or MASTER_SEEDS_PATH
    from app.services.seed_catalog import catalog_summary, dump_catalog
    if any(isinstance(s, dict) and s.get("queue") for s in seeds):
        return dump_catalog(seeds, path, source=source)
    payload = {
        "generated_at": now_iso(),
        "source": source,
        "n": len(seeds),
        "n_with_url": sum(1 for s in seeds if s.get("url")),
        **{k: v for k, v in catalog_summary(seeds).items() if k not in {"n", "n_with_url"}},
        "seeds": [_without_priority(s) if isinstance(s, dict) else s for s in seeds],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def projects_from_geojson(fc: dict) -> list[dict]:
    docs = []
    for feat in fc.get("features") or []:
        props = feat.get("properties") or {}
        docs.append({
            "title": props.get("title"),
            "url": props.get("url"),
            "funder": props.get("funder"),
            "funders": props.get("funders"),
        })
    return docs


def fetch_production_projects(base: str = "https://blueintelligence.online") -> list[dict]:
    url = base.rstrip("/") + "/api/projects"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 Blue-Intelligence-Map/master-seeds"})
    with urlopen(req, timeout=60) as resp:
        fc = json.loads(resp.read().decode("utf-8"))
    return projects_from_geojson(fc)
