"""Named search: TinyFish Search, serp_filter, DuckDuckGo if no key.

Entry point: ``search_named``. Same question everywhere ("where is the
official page for *this* name"). Queries and domain lists stay
mode-specific. No SearXNG here: that is the top-down tool
(regulatory list of a polygon), not of a marina, an MPA or
a harbormaster.

  1. TinyFish Search (paginated) if a key is present.
  2. ``serp_filter`` — forums, OTAs, social: already the top-down / AMP filter.
  3. DuckDuckGo HTML only if the TinyFish key is missing (free net).

DuckDuckGo is not a second opinion after an empty TinyFish: if the key
exists, trust TinyFish. The caller's ``include_domains`` / ``exclude_domains``
survive the DDG net (``site:`` operator + host filter).
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from app.core.extract import serp_filter
from app.core.tinyfish import tf_api_key, tf_search_pages

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DDG_SITE_CAP = 6
DDG_DEFAULT_CAP = 8

_WWW_RE = re.compile(r"^www\.", re.I)


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return _WWW_RE.sub("", host)


def url_key(url: str) -> str:
    """Dedupe key: host without www, path without a trailing slash."""
    raw = (url or "").strip()
    try:
        p = urlparse(raw)
    except Exception:
        return raw.rstrip("/").lower()
    host = _WWW_RE.sub("", (p.hostname or "").lower())
    path = (p.path or "").rstrip("/") or "/"
    scheme = (p.scheme or "https").lower()
    return f"{scheme}://{host}{path}"


def domain_list(domains) -> list[str]:
    if not domains:
        return []
    if isinstance(domains, str):
        parts = domains.split(",")
    else:
        parts = list(domains)
    return [str(d).strip().lstrip(".").lower() for d in parts if str(d).strip()]


def url_matches_domains(url: str, domains) -> bool:
    """True if the host is one of the domains (suffix), including a bare TLD (``gov``)."""
    wanted = domain_list(domains)
    if not wanted:
        return True
    host = _host(url)
    if not host:
        return False
    for d in wanted:
        if host == d or host.endswith("." + d):
            return True
    return False


def _hit(url: str, *, title="", snippet="", engine="", domain="") -> dict:
    u = (url or "").strip()
    return {
        "url": u,
        "title": title or "",
        "snippet": snippet or "",
        "engine": engine or "",
        "domain": domain or _host(u),
    }


def _normalize_hit(raw: dict, *, default_engine="") -> dict | None:
    if not isinstance(raw, dict):
        return None
    u = (raw.get("url") or "").strip()
    if not u.startswith("http"):
        return None
    return _hit(
        u,
        title=raw.get("title") or "",
        snippet=raw.get("snippet") or "",
        engine=raw.get("engine") or default_engine,
        domain=raw.get("domain") or "",
    )


def ddg_kl(location: str | None) -> str:
    iso = (location or "").strip().lower()
    if len(iso) == 2 and iso.isalpha():
        return f"{iso}-{iso}"
    return "wt-wt"


def ddg_query(query: str, include_domains=None) -> str:
    """Add ``site:`` when DDG cannot filter at the API, unless the query already has it."""
    q = (query or "").strip()
    if not q:
        return ""
    if "site:" in q.lower():
        return q
    domains = domain_list(include_domains)[:DDG_SITE_CAP]
    if not domains:
        return q
    sites = " OR ".join(f"site:{d}" for d in domains)
    return f"({q}) ({sites})"


def _unwrap_ddg_href(href: str) -> str:
    href = (href or "").strip()
    if "/l/?" in href or "duckduckgo.com/l/" in href:
        m = re.search(r"[?&]uddg=([^&]+)", href)
        if m:
            href = unquote(m.group(1))
    if href.startswith("//"):
        href = "https:" + href
    return href


def _parse_ddg_html(html: str, max_results: int) -> list[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    hits: list[dict] = []
    seen: set[str] = set()
    nodes = soup.select("div.result") or soup.select("div.links_main")
    if not nodes:
        nodes = soup.select("a.result__a, a.result-link")
    for node in nodes:
        if hasattr(node, "get") and "result--ad" in (node.get("class") or []):
            continue
        if node.name == "a":
            a = node
            snippet = ""
        else:
            a = node.select_one("a.result__a") or node.select_one("a.result-link")
            snip_el = node.select_one(".result__snippet") or node.select_one("td.result-snippet")
            snippet = snip_el.get_text(" ", strip=True) if snip_el else ""
        if a is None:
            continue
        href = _unwrap_ddg_href(a.get("href") or "")
        title = a.get_text(" ", strip=True)
        if not href.startswith("http") or not title:
            continue
        key = url_key(href)
        if key in seen:
            continue
        seen.add(key)
        hits.append(_hit(href, title=title, snippet=snippet, engine="duckduckgo"))
        if len(hits) >= max_results:
            break
    return hits


async def duckduckgo_html_search(
    query: str,
    client: httpx.AsyncClient | None = None,
    max_results: int = 3,
    *,
    include_domains=None,
    location: str | None = None,
) -> list[dict]:
    """Liste ``{url, title, snippet, engine, domain}`` — best-effort, silencieux."""
    q = ddg_query(query, include_domains)
    cap = max(1, int(max_results or DDG_DEFAULT_CAP))
    if not q:
        return []
    payload = {"q": q, "kl": ddg_kl(location)}
    headers = {"User-Agent": DDG_UA}

    async def _once(c: httpx.AsyncClient) -> list[dict]:
        r = await c.post(
            DDG_HTML_URL, data=payload, headers=headers,
            timeout=20, follow_redirects=True)
        if r.status_code != 200 or not (r.text or "").strip():
            r = await c.get(
                DDG_HTML_URL, params=payload, headers=headers,
                timeout=20, follow_redirects=True)
        if r.status_code != 200:
            return []
        return _parse_ddg_html(r.text, cap)

    try:
        if client is not None:
            return await _once(client)
        async with httpx.AsyncClient() as own:
            return await _once(own)
    except Exception:
        return []


def _apply_domain_filters(hits: list[dict], *, include_domains=None,
                          exclude_domains=None, engine: str = "") -> list[dict]:
    excluded = domain_list(exclude_domains)
    included = domain_list(include_domains)
    # TinyFish already applies include_domains at the API; DDG does not.
    apply_include = bool(included) and engine != "tinyfish"
    out = []
    for h in hits:
        u = h.get("url") or ""
        if excluded and url_matches_domains(u, excluded):
            continue
        if apply_include and not url_matches_domains(u, included):
            continue
        out.append(h)
    return out


def _dedupe(hits: list[dict]) -> list[dict]:
    out, seen = [], set()
    for h in hits:
        key = url_key(h.get("url") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


async def search_named(
    query: str,
    *,
    key: str | None = None,
    include_domains=None,
    exclude_domains=None,
    location: str | None = None,
    language: str | None = None,
    purpose: str | None = None,
    max_pages: int = 1,
    max_results: int | None = None,
    extra_re=None,
    protect_fn=None,
    stop_when=None,
    log=None,
) -> list[dict]:
    """Official page of an already named place. No SearXNG, no Serper.

    ``key=None`` resolves ``TINYFISH_API_KEY``. ``key=""`` forces the DuckDuckGo net.
    """
    log = log or (lambda m: None)
    q = (query or "").strip()
    if not q:
        return []
    if key is None:
        key = tf_api_key()
    key = (key or "").strip()

    engine = "tinyfish" if key else "duckduckgo"
    raw: list[dict] = []
    if key:
        try:
            raw = await tf_search_pages(
                q, key, location=location, language=language,
                include_domains=include_domains, exclude_domains=exclude_domains,
                purpose=purpose, max_pages=max_pages, stop_when=stop_when, log=log)
        except Exception as e:
            log(f"search_named TinyFish {type(e).__name__}: {str(e)[:80]}")
            raw = []
        log(f"search_named TinyFish {len(raw)} hit(s) for {q[:80]}")
    else:
        cap = int(max_results or DDG_DEFAULT_CAP)
        if max_pages and max_pages > 1:
            cap = max(cap, min(30, int(max_pages) * 10))
        try:
            raw = await duckduckgo_html_search(
                q, max_results=cap, include_domains=include_domains,
                location=location)
        except Exception as e:
            log(f"search_named DDG {type(e).__name__}: {str(e)[:80]}")
            raw = []
        log(f"search_named DDG {len(raw)} hit(s) for {q[:80]}")

    hits = []
    for r in raw or []:
        n = _normalize_hit(r, default_engine=engine)
        if n:
            hits.append(n)
    hits = _dedupe(hits)
    hits = _apply_domain_filters(
        hits, include_domains=include_domains, exclude_domains=exclude_domains,
        engine=engine)
    hits = serp_filter(hits, extra_re=extra_re, protect_fn=protect_fn)
    if max_results is not None:
        hits = hits[: max(0, int(max_results))]
    return hits
