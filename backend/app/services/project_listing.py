"""Home → catalogue (Projects CDC C5 “listing to discover”).

The 21 curated seeds give the patterns of a page that *lists* projects.
v1 MasterSeeds often have only the home (`https://domain/`).
This step finds the catalogue URL before card discovery.

Hop 1: SERP hygiene (same host, not home / news / donate / wp-content) →
curated-leaf shortcut (`/projects/`) → else LLM judge of 3–5 URLs →
listing Agent. The leaf filter is no longer a SERP veto.
"""
from __future__ import annotations

from collections import Counter
from urllib.parse import urlparse

from app.services.master_seeds import (
    SKIP_LISTING_NETLOCS, domain_matches_org, domain_of, is_shared_hub,
    name_owns_hub,
)
from app.static_data.seeds import CRAWL_BLACKLIST, CURATED_SEEDS, URL_PATTERNS

LISTING_JUDGE_CAP = 5


class ListingJudgeQuotaError(Exception):
    """NVIDIA / OpenRouter / Claude 429: no TinyFish Agent behind."""
_FILE_EXTS = frozenset({
    "pdf", "jpg", "jpeg", "png", "gif", "zip", "svg", "mp4", "webp", "css", "js",
})

LANG_PREFIXES = {
    "en", "fr", "de", "es", "it", "pt", "nl", "int", "uk", "us", "eu",
}

# “Index” segments learned from the 21 curated listings + URL_PATTERNS.
_LISTING_LEAVES: frozenset[str] | None = None

# Path prefixes never to take as a catalogue (PDF, CMS, news).
_NOT_LISTING_PREFIXES = (
    "/wp-content", "/wp-admin", "/uploads", "/feed", "/tag/", "/tags/",
    "/category/", "/author/", "/cdn-cgi",
)


def _norm_seg(seg: str) -> str:
    return (seg or "").strip().strip("/").lower()


_BLOCKED_SEGS = frozenset(_norm_seg(s) for s in CRAWL_BLACKLIST)


def listing_leaves() -> frozenset[str]:
    """Last segments of the 21 curated + URL_PATTERNS motifs (no slash)."""
    global _LISTING_LEAVES
    if _LISTING_LEAVES is not None:
        return _LISTING_LEAVES
    leaves: set[str] = set()
    for pat in URL_PATTERNS:
        tok = _norm_seg(pat.replace("/where-we-work/", "where-we-work"))
        if tok:
            leaves.add(tok)
            if tok.endswith("s") and len(tok) > 4:
                leaves.add(tok[:-1])
            else:
                leaves.add(tok + "s")
    for seed in CURATED_SEEDS:
        parts = [p for p in urlparse(seed.get("url") or "").path.split("/") if p]
        for p in parts:
            n = _norm_seg(p)
            if n and n not in LANG_PREFIXES:
                leaves.add(n)
    # Frequent variants not covered by a single curated seed.
    leaves.update({
        "projects", "project", "projets", "projet",
        "campaigns", "campaign", "initiatives", "initiative",
        "programs", "program", "programmes", "programme",
        "grants", "grant", "actions", "missions", "expeditions",
        "hope-spots", "hope-spot", "our-work", "our-work",
        "where-we-work", "nos-actions", "fondation",
        "nos-programmes", "nos-programme", "nos-projets", "nos-projet",
        "iw-projects", "iw-project", "reef-plus", "our-campaigns",
        "decade-actions",
    })
    _LISTING_LEAVES = frozenset(leaves)
    return _LISTING_LEAVES


def reset_listing_leaves() -> None:
    """Tests."""
    global _LISTING_LEAVES
    _LISTING_LEAVES = None


def path_parts(path: str) -> list[str]:
    return [p for p in (path or "").strip("/").split("/") if p]


def is_homepage_url(url: str | None) -> bool:
    """Site root (optionally /fr, /en). Not a catalogue."""
    if not (url or "").strip():
        return True
    parts = path_parts(urlparse(url).path)
    if not parts:
        return True
    if len(parts) == 1 and parts[0].lower() in LANG_PREFIXES:
        return True
    return False


def is_listing_path(path: str, *, apply_blacklist: bool = True) -> bool:
    """Page that lists projects: 1–3 segments, leaf in the curated motifs.

    `/projects/` and `/hope-spots/` pass (1 segment).
    `/en/where-we-work/` passes (leaf = motif).
    `/projects/coral-restore` is a card, not a catalogue.
    """
    raw = path or ""
    low = raw.lower()
    if apply_blacklist and any(b in low for b in CRAWL_BLACKLIST):
        return False
    if any(low.startswith(p) or p in low for p in _NOT_LISTING_PREFIXES):
        return False
    parts = path_parts(raw)
    if not parts or len(parts) > 3:
        return False
    leaves = listing_leaves()
    meaningful = [_norm_seg(p) for p in parts if _norm_seg(p) not in LANG_PREFIXES]
    if not meaningful:
        return False
    last = meaningful[-1]
    if last not in leaves:
        return False
    # A card often has a slug after the motif (4th segment already excluded).
    # 2+ meaningful whose last is NOT only the motif? last IS a leaf
    # `/projects/coral-restore` → meaningful=[projects, coral-restore], last not leaf → False. OK.
    return True


def is_listing_url(url: str | None, *, home_ok: bool = False) -> bool:
    if not (url or "").strip().startswith("http"):
        return False
    if is_homepage_url(url):
        return bool(home_ok)
    return is_listing_path(urlparse(url).path)


def is_curated_listing_url(url: str | None) -> bool:
    """The 21 curated ones are already the catalogue, even if the path exits the leaf filter."""
    key = (url or "").rstrip("/")
    if not key:
        return False
    return any((s.get("url") or "").rstrip("/") == key for s in CURATED_SEEDS)


def needs_listing_hop(seed: dict | None) -> bool:
    """True if we do not yet have a qualified catalogue URL."""
    seed = seed or {}
    kind = (seed.get("listing_kind") or "").strip().lower()
    url = (seed.get("listing_url") or seed.get("url") or "").strip()
    if kind == "home_only":
        return False
    if kind == "projects_index":
        if url and is_homepage_url(url):
            return True
        return False
    if not url:
        return True
    if is_listing_url(url) or is_curated_listing_url(url):
        return False
    return True


def pick_listing_url(urls: list[str] | None) -> str | None:
    """One catalogue: shortest path among valid candidates."""
    ok = [u for u in (urls or []) if is_listing_url(u)]
    if not ok:
        return None
    ok.sort(key=lambda u: (len(path_parts(urlparse(u).path)), len(u)))
    return ok[0]


def apply_learned_listings(seeds: list[dict], extras: list[dict] | None) -> list[dict]:
    """Reattach memorized catalogues (Mongo) onto v1 MasterSeeds."""
    by_domain: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for extra in extras or []:
        if (extra.get("listing_kind") or "").strip().lower() != "projects_index":
            continue
        u = (extra.get("url") or "").strip()
        if not u or is_homepage_url(u):
            continue
        if not (
            is_listing_url(u)
            or is_curated_listing_url(u)
            or listing_hygiene_ok(u)
        ):
            continue
        d = domain_of(u) or domain_of(extra.get("domain") or "")
        if d:
            by_domain[d] = u
        n = (extra.get("name") or "").strip().lower()
        if n:
            by_name[n] = u
    out = []
    for seed in seeds or []:
        item = dict(seed)
        d = domain_of(item.get("url"))
        n = (item.get("name") or "").strip().lower()
        learned = by_name.get(n) if n else None
        if not learned and d:
            # A catalogue memorized on oceandecade.org is not BMKG's.
            if is_shared_hub(d) and not name_owns_hub(item.get("name") or "", d):
                learned = None
            else:
                learned = by_domain.get(d)
        learned_d = domain_of(learned) if learned else ""
        official_d = domain_of(item.get("home_url") or item.get("url"))
        if (
            learned
            and (item.get("home_status") or "") == "official"
            and official_d
            and learned_d
            and learned_d != official_d
        ):
            learned = None
        if learned and is_shared_hub(learned_d) and not name_owns_hub(
            item.get("name") or "", learned_d
        ):
            learned = None
        if learned:
            item["url"] = learned
            item["listing_url"] = learned
            item["listing_kind"] = "projects_index"
            if item.get("home_status") == "official" or item.get("queue") == "crawl":
                item["queue"] = "crawl"
        out.append(item)
    return out


def listing_search_query(seed: dict) -> str:
    """One shot: site:{domain} … listing. No 2nd language."""
    name = (seed.get("name") or "").strip() if isinstance(seed, dict) else ""
    url = (seed.get("url") or "").strip() if isinstance(seed, dict) else ""
    host = domain_of(url)
    if host:
        return f"site:{host} projects OR projets OR campaigns OR programs listing"
    if name:
        return f'"{name}" marine projects listing'
    return "marine conservation projects listing"


def listing_search_retry_query(seed: dict) -> str:
    """2nd shot if the filter dropped everything: index paths, no news."""
    url = (seed.get("url") or "").strip() if isinstance(seed, dict) else ""
    host = domain_of(url)
    if host:
        return (
            f"site:{host} inurl:projects OR inurl:projets OR inurl:campaigns "
            f"OR inurl:hope-spots OR inurl:programs -news -donate -about"
        )
    name = (seed.get("name") or "").strip() if isinstance(seed, dict) else ""
    if name:
        return f'"{name}" inurl:projects marine -news'
    return listing_search_query(seed or {})


def fiche_search_retry_query(seed: dict) -> str:
    """2nd shot cards: deep pages, excluding already-eliminated URLs."""
    name = (seed.get("name") or "").strip() if isinstance(seed, dict) else ""
    url = (seed.get("url") or "").strip() if isinstance(seed, dict) else ""
    host = domain_of(url)
    if host:
        q = f"site:{host} inurl:project OR inurl:projet OR inurl:campaign -news -donate"
        if name:
            return f"{q} {name}"
        return q
    if name:
        return f'"{name}" marine project page -news'
    return "marine conservation project page -news"


def infer_listing_from_project_urls(
    urls: list[str], funder_name: str = "", *, allow_shared_hub: bool = False,
) -> str | None:
    """Hint: common prefix of v1 cards if it looks like a catalogue.

    Save Our Seas `/project/…` → `https://saveourseas.com/project/`.
    `/wp-content/uploads` → ignored.
    """
    by_host: dict[str, list[list[str]]] = {}
    for u in urls or []:
        if not (u or "").startswith("http"):
            continue
        pr = urlparse(u)
        host = domain_of(u)
        if not host or host in SKIP_LISTING_NETLOCS:
            continue
        if (
            not allow_shared_hub
            and is_shared_hub(host)
            and not name_owns_hub(funder_name, host)
            and not domain_matches_org(host, funder_name)
        ):
            continue
        by_host.setdefault(host, []).append(path_parts(pr.path))
    if not by_host:
        return None
    tokens = [t for t in (funder_name or "").lower().split() if len(t) >= 4]
    ranked = []
    for host, groups in by_host.items():
        name_hit = any(t in host.replace(".", "") for t in tokens)
        if len(groups) < 2 and not name_hit:
            continue
        prefix = _common_prefix(groups)
        if not prefix:
            first = Counter(g[0] for g in groups if g)
            if first:
                seg, n = first.most_common(1)[0]
                if n / len(groups) >= 0.6:
                    prefix = [seg]
        if not prefix:
            continue
        path = "/" + "/".join(prefix) + "/"
        if not is_listing_path(path):
            continue
        bonus = 2 if name_hit else 0
        ranked.append((bonus, len(groups), -len(host), host, path))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    _, _, _, host, path = ranked[0]
    return f"https://{host}{path}"


def _common_prefix(groups: list[list[str]]) -> list[str]:
    if not groups:
        return []
    prefix: list[str] = []
    for segs in zip(*groups):
        normed = {_norm_seg(s) for s in segs}
        if len(normed) != 1:
            break
        prefix.append(next(iter(normed)))
    return prefix


def _hit_url(hit) -> str:
    if isinstance(hit, str):
        return hit.strip()
    if isinstance(hit, dict):
        return (hit.get("url") or hit.get("link") or hit.get("href") or "").strip()
    return ""


def _looks_like_fiche_parts(parts: list[str]) -> bool:
    """`/projects/coral-restore`: leaf + slug. Not a catalogue."""
    meaningful = [_norm_seg(p) for p in parts if _norm_seg(p) not in LANG_PREFIXES]
    if len(meaningful) < 2:
        return False
    leaves = listing_leaves()
    return meaningful[0] in leaves and meaningful[-1] not in leaves


def listing_hygiene_ok(url: str | None) -> bool:
    """Same light constraint for SERP, Agent and memory. Not a leaf veto."""
    if not (url or "").startswith("http"):
        return False
    if is_homepage_url(url):
        return False
    path = urlparse(url).path or ""
    low = path.lower()
    if any(low.startswith(p) or p in low for p in _NOT_LISTING_PREFIXES):
        return False
    parts = path_parts(path)
    if not parts or len(parts) > 3:
        return False
    segs = [_norm_seg(p) for p in parts]
    if any(s in _BLOCKED_SEGS for s in segs):
        return False
    last = segs[-1]
    if "." in last and last.rsplit(".", 1)[-1] in _FILE_EXTS:
        return False
    if _looks_like_fiche_parts(parts):
        return False
    return True


def _collect_same_host_hits(hits, seed, *, exclude_urls=None) -> list[str]:
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
        if key in excluded or key in seen:
            continue
        if seed_url and key == seed_url:
            continue
        d = domain_of(href)
        if host and d != host:
            continue
        if not host and (not d or d in SKIP_LISTING_NETLOCS):
            continue
        seen.add(key)
        urls.append(href)
    return urls


def hygiene_listing_urls(hits, seed, max_urls=LISTING_JUDGE_CAP, *, exclude_urls=None) -> list[str]:
    """3–5 judge candidates: same host, not home / news / donate / card / wp-content.

    Curated leaves (`/projects/`) go first for the shortcut, without
    capping early: a `/projects/` at SERP hit 6 is not lost.
    """
    try:
        cap = max(0, int(max_urls or 0))
    except (TypeError, ValueError):
        cap = 0
    same = _collect_same_host_hits(hits, seed, exclude_urls=exclude_urls)
    ok = [u for u in same if listing_hygiene_ok(u)]
    leaves = [u for u in ok if is_listing_url(u)]
    rest = [u for u in ok if not is_listing_url(u)]
    ordered = leaves + rest
    return ordered[:cap] if cap else ordered


def merge_listing_candidates(*groups, cap=LISTING_JUDGE_CAP) -> list[str]:
    """Union crawl + Fetch + Search, leaves first, judge cap."""
    seen: set[str] = set()
    leaves, rest = [], []
    for group in groups:
        for u in group or []:
            if not u:
                continue
            key = u.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            if is_listing_url(u):
                leaves.append(u)
            elif listing_hygiene_ok(u):
                rest.append(u)
    ordered = leaves + rest
    try:
        limit = max(0, int(cap or 0))
    except (TypeError, ValueError):
        limit = LISTING_JUDGE_CAP
    return ordered[:limit] if limit else ordered


def accept_listing_url(url: str | None, seed: dict | None = None) -> str | None:
    """Listing Agent output: hygiene, not the `/projects/` leaf veto."""
    raw = (url or "").strip()
    if not raw.startswith("http"):
        return None
    href = raw.split("#")[0].split("?")[0]
    if not listing_hygiene_ok(href):
        return None
    if isinstance(seed, dict):
        host = domain_of(seed.get("url") or "")
        if host and domain_of(href) != host:
            return None
        seed_url = (seed.get("url") or "").rstrip("/")
        if seed_url and href.rstrip("/") == seed_url:
            return None
    return href


def filter_listing_urls(hits, seed, max_urls=3, *, exclude_urls=None) -> list[str]:
    """Curated-leaf shortcut only (`/projects/`, `/hope-spots/`)."""
    try:
        cap = max(0, int(max_urls or 0))
    except (TypeError, ValueError):
        cap = 0
    urls, seen = [], set()
    for href in _collect_same_host_hits(hits, seed, exclude_urls=exclude_urls):
        if not is_listing_url(href):
            continue
        key = href.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        urls.append(href)
        if cap and len(urls) >= cap:
            break
    return urls[:cap] if cap else urls


def parent_listing_url(url: str | None) -> str | None:
    """`/projects/coral-restore` → `https://host/projects/` if the parent is an index."""
    raw = (url or "").strip()
    if not raw.startswith("http"):
        return None
    parsed = urlparse(raw.split("#")[0].split("?")[0])
    host = domain_of(raw)
    parts = path_parts(parsed.path)
    if not host or len(parts) < 2:
        return None
    for cut in range(len(parts) - 1, 0, -1):
        path = "/" + "/".join(parts[:cut]) + "/"
        if is_listing_path(path):
            scheme = parsed.scheme or "https"
            return f"{scheme}://{parsed.netloc}{path}"
    return None


def listing_from_hits(hits, seed: dict | None = None) -> str:
    """List page on the home domain. No Agent, no LLM judge.

    Accept a `/projects/` hit or walk up from a `/projects/slug` card.
    """
    seed = seed or {}
    host_seed = {
        "url": (seed.get("home_url") or seed.get("url") or "").strip(),
        "name": (seed.get("name") or "").strip(),
    }
    host = domain_of(host_seed["url"])
    if not host:
        return ""
    found: list[str] = []
    for href in _collect_same_host_hits(hits, host_seed):
        if is_listing_url(href):
            found.append(href)
            continue
        parent = parent_listing_url(href)
        if parent and domain_of(parent) == host:
            found.append(parent)
    return pick_listing_url(found) or ""


LISTING_JUDGE_SYSTEM = (
    "Tu juges des URL de catalogue de projets d'une organisation marine. "
    "Réponds uniquement en JSON strict : "
    '{"url": "https://example.org/projects/", "accept": true, "reason": ""} '
    "url doit être l'une des candidates, ou null. "
    "accept=true seulement si la page LISTE plusieurs projets, campagnes, "
    "programmes ou hope spots DE CETTE organisation (index, directory, "
    "where we work, our work, our programmes). "
    "accept=false pour une homepage, une fiche projet unique, une actualité, "
    "un don, une page about/contact, un PDF ou /wp-content. "
    "N'invente aucune URL."
)


def listing_judge_prompt(seed: dict, candidates: list) -> str:
    lines = []
    for i, cand in enumerate(candidates[:LISTING_JUDGE_CAP], 1):
        if isinstance(cand, dict):
            url = cand.get("url") or ""
            title = (cand.get("title") or "").strip()
        else:
            url, title = str(cand), ""
        extra = f" | {title}" if title else ""
        lines.append(f"{i}. {url}{extra}")
    home = ((seed or {}).get("url") or "").strip()
    name = ((seed or {}).get("name") or "").strip()
    return (
        f"Organisation : {name}\n"
        f"Homepage (interdite comme listing) : {home}\n"
        f"Candidats :\n" + "\n".join(lines)
    )


def parse_listing_judge(data: dict | None, candidates: list, home: str | None) -> str | None:
    from app.core.judge import parse_yes_no
    yes = parse_yes_no(
        data,
        allowed_urls=candidates,
        forbidden_urls=[home] if home else None,
    )
    return yes.url if yes.accepted else None


async def llm_judge_listing(
    seed: dict,
    candidates: list,
    *,
    settings: dict | None = None,
    log=None,
) -> str | None:
    """NVIDIA (json chain) → OpenRouter → Claude. One URL among 3–5, never off-list."""
    packed = []
    for cand in candidates or []:
        if isinstance(cand, dict):
            url = (cand.get("url") or cand.get("link") or "").strip()
            title = cand.get("title") or ""
        else:
            url, title = str(cand).strip(), ""
        if url.startswith("http"):
            packed.append({"url": url, "title": title})
        if len(packed) >= LISTING_JUDGE_CAP:
            break
    if not packed:
        return None
    from app.core.judge import ask_yes_no

    notes: list[str] = []

    def _log(msg):
        notes.append(str(msg or ""))
        if log:
            log(msg)

    home = (seed or {}).get("url")
    try:
        yes = await ask_yes_no(
            LISTING_JUDGE_SYSTEM,
            listing_judge_prompt(seed or {}, packed),
            settings=settings,
            log=_log,
            role="json",
            max_tokens=400,
            allowed_urls=packed,
            forbidden_urls=[home] if home else None,
            on_empty="inconclusive",
        )
    except Exception as e:
        blob = f"{e} {' '.join(notes)}"
        if "429" in blob:
            raise ListingJudgeQuotaError(str(e)[:160]) from e
        raise
    if yes.accepted and yes.url:
        return yes.url
    blob = f"{yes.reason} {yes.engine} {' '.join(notes)}"
    if (not yes.engine or yes.reason == "llm_error") and "429" in blob:
        raise ListingJudgeQuotaError(blob[:160])
    return None
