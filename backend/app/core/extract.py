"""
extract_core.py — URL reading: one door, one cascade.

Entry point: ``read_url`` / ``read_urls``. Same question everywhere
("give me the text of this URL"), including PDFs and JavaScript pages.

  N1/N2 (free, ms)       : direct httpx + compared DUAL PARSING
                           trafilatura ∥ Readability/BS4 (the richer wins,
                           similarity between the two is a quality signal)
                           PDF: %PDF- / Content-Type / Content-Disposition magic,
                           then PyMuPDF (+ OCR if scanned).
  N3 (free, local)       : Playwright/Chromium browser render (render_core)
                           for JavaScript pages and "soft" challenges.
                           Chromium runs in a subprocess (like
                           PyMuPDF): an ABRT no longer kills the API. Never on a
                           .pdf URL, an image (.png/.jpg…) or a hard captcha
                           (SiteGround, Akamai). On only if simple HTML
                           AND Fetch have already failed.
  Mirror                 : Jina ∥ TinyFish Fetch (if key), then Wayback.

TinyFish Fetch is not removed: it is the right tool when we need the
DOM after JavaScript (Google Maps /place/ card). The cascade does not parse
a Maps card as a decree. ``prefer_fetch=True`` (bottom-up, AMP,
harbormasters) tries Fetch first; usable text avoids Chromium.

Also provides: SERP regex filtering (aggregators, social networks, anti-bot
interstitial pages), blocked-page detection (a challenge is NEVER
ingested as content), page metadata (og:image, external links)
and selective internal-link follow-up (depth=2: /annuaire, /contacts…).
"""
import asyncio
import difflib
import hashlib
import json
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

# Disabled for v1/v2 variants (SearXNG only) — TinyFish Fetch stays
# reserved for the "tinyfish" variant. Default True so Swarm is not broken.
allow_tinyfish_fetch: ContextVar[bool] = ContextVar("allow_tinyfish_fetch", default=True)

import httpx
from bs4 import BeautifulSoup

# PyMuPDF is not safe in the API process (native "double free" crash).
# Extraction in a subprocess + sha256 disk cache → text.
# The semaphore limits RAM (gazettes up to 180 pages); it is no longer a
# safety lock: several PDFs can proceed in parallel.
from app.config import BACKEND_DIR, DATA_DIR
from app.core.public_url import fetch_checked

PDF_CACHE_DIR = DATA_DIR / "cached_pdfs"
# OCR of a scanned Gaceta (5 pages) far exceeds 25 s.
PDF_SUBPROCESS_TIMEOUT_S = 90
PDF_MAX_CONCURRENT = 3
_pdf_slots = threading.Semaphore(PDF_MAX_CONCURRENT)

UA_BROWSER = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
# r.jina.ai returns 403 (Cloudflare) if we pose as Chrome; a reader UA is enough.
UA_READER = {
    "User-Agent": "Mozilla/5.0 (compatible; BlueIntelligence-Reader/1.0)",
    "Accept": "text/plain, */*;q=0.8",
}

HARD_CHALLENGE_RE = re.compile(
    r"challenge validation|sec-cpt-if|sec-container|akamai|"
    r"just a moment|_cf_chl_|cf-browser-verification|"
    r"challenges\.cloudflare|performing security verification|"
    r"sg-captcha|robot challenge screen|checking the site connection security",
    re.I,
)

# --- SERP filter: regex exclusion before any parsing --------------------------
# HARD cut: networks, OTAs, dictionaries, challenges — never a PoE source.
SERP_HARD_RE = re.compile(
    r"(tripadvisor|booking\.com|expedia|airbnb|pinterest|facebook\.com|instagram\.com"
    r"|tiktok\.|youtube\.com|twitter\.com|/x\.com|linkedin\.com|reddit\.com|quora\.com"
    r"|hotels?\.com|kayak\.|skyscanner|cruisemapper|vesselfinder"
    r"|merriam-webster|dictionary\.com|thefreedictionary|cambridge\.org/(?:\w+/)?dictionary"
    r"|wiktionary|britannica\.com|wikihow|howtogeek|investopedia|linguee|wordreference"
    r"|vocabulary\.com|twominenglish|askdifference|factsinstitute"
    r"|guiahardware|stationx\.net|common-ports-cheat|ip-tracker\.org"
    r"|doordash\.com"
    r"|//unblock\.|\.unblock\.|/cdn-cgi/|captcha|datadome|perimeterx"
    r"|queue-it\.net|incapsula|distilnetworks"
    r"|\.docx?($|\?)|\.xlsx?($|\?)|\.pptx?($|\?)|\.zip($|\?)|\.exe($|\?)"
    r"|\.png($|\?)|\.jpe?g($|\?)|\.gif($|\?)|\.webp($|\?)|\.svg($|\?)"
    r"|\.bmp($|\?)|\.ico($|\?)|\.tiff?($|\?)|\.avif($|\?))",
    re.I,
)

# SOFT cut: tourism words that also exist on state pages
# (…/tourism-yacht-clearance). An official domain is not dropped for that.
SERP_SOFT_RE = re.compile(
    r"(brochure|touris[mt]|baggage|luggage|duty.?free|/vts[-_/.]|vts.?manual)",
    re.I,
)

# Backward compat Swarm / tests: hard + soft (no domain protection).
SERP_EXCLUDE_RE = re.compile(
    r"(?:%s)|(?:%s)" % (SERP_HARD_RE.pattern, SERP_SOFT_RE.pattern),
    re.I,
)

_OFFICIAL_URL_RE = re.compile(
    r"gov|gouv|gob|douane|customs|aduana|zoll|immigration|border|"
    r"maritime|port.?authority|coast.?guard|admin",
    re.I,
)

# --- Blocked-page detection (anti-bot challenges, interstitials) -------------
BLOCKED_MARKERS_RE = re.compile(
    r"checking your browser|just a moment|verify (?:that )?you are (?:a )?human"
    r"|are you a robot|enable javascript and cookies|cf-browser-verification"
    r"|_cf_chl_|cf_chl_opt|attention required.{0,4}cloudflare|cloudflare ray id"
    r"|px-captcha|datadome|request unsuccessful|incapsula incident"
    r"|access to this page has been denied|pardon our interruption"
    r"|please complete the security check|request has been blocked"
    r"|automated access to this (?:site|page)|ddos protection by"
    r"|complete the captcha|prove (?:that )?you are human|browser verification"
    r"|unblock request|access from your area has been temporarily limited"
    r"|challenge validation|sec-cpt-if|sec-container"
    r"|sg-captcha|robot challenge screen|checking the site connection security",
    re.I,
)

FOLLOWUP_PATTERNS = (
    "annuaire", "contact", "directory", "port-of-entry", "ports-of-entry",
    "clearance", "douane", "customs", "bureaux", "offices", "liste", "list-of",
    "projets", "projects", "annexe", "habilit", "designated", "decreto",
    "gazette", "legislation", "aduana", "anexo",
    "puerto", "terminal", "plaisance", "first-arrival", "small-craft",
    "formalit", "seaport", "marina-mercante", "inventario",
    "capitanias", "jurisdiccion",
)


def looks_official_url(url: str) -> bool:
    """Domain or path that smells like the state / customs (not proof, a net)."""
    return bool(url and _OFFICIAL_URL_RE.search(url))


def serp_drop_reason(url: str, extra_re=None, protect: bool = False) -> str | None:
    """Why drop this URL, or None to keep it. Auditable."""
    if not (url or "").startswith("http"):
        return "not_http"
    if SERP_HARD_RE.search(url):
        return "hard"
    if extra_re and extra_re.search(url):
        return "extra"
    if SERP_SOFT_RE.search(url) and not (protect or looks_official_url(url)):
        return "soft_tourism"
    return None


def audit_serp_filter(results: list[dict], url_key: str = "url", extra_re=None,
                      protect_fn=None) -> list[dict]:
    """Log {url, kept, reason} — to review what the filter did."""
    out = []
    for r in results:
        u = r.get(url_key) or ""
        protect = bool(protect_fn(u)) if protect_fn else looks_official_url(u)
        reason = serp_drop_reason(u, extra_re=extra_re, protect=protect)
        out.append({"url": u, "kept": reason is None, "reason": reason,
                    "protected": protect})
    return out


def serp_filter(results: list[dict], url_key: str = "url", extra_re=None,
                protect_fn=None) -> list[dict]:
    """Drop irrelevant URLs. A state domain is never discarded
    for a tourism word in the path."""
    out = []
    for r in results:
        u = r.get(url_key) or ""
        protect = bool(protect_fn(u)) if protect_fn else looks_official_url(u)
        if serp_drop_reason(u, extra_re=extra_re, protect=protect) is None:
            out.append(r)
    return out


def url_looks_like_pdf(url: str | None) -> bool:
    """Path .pdf — even if the server serves captcha HTML instead."""
    path = (urlparse(url or "").path or "").lower()
    return path.endswith(".pdf")


# Ministères renommés / DNS NXDOMAIN connus (SCT mexicain → SICT, etc.).
RETIRED_HOST_SUFFIXES = (
    "sct.gob.mx",
)

_DOWNLOAD_PATH_RE = re.compile(
    r"/file-download/|/download\.php\b",
    re.I,
)

_FETCH_FAIL_ERRORS = frozenset({
    "ConnectError", "ConnectTimeout", "ReadTimeout", "TimeoutException",
    "NetworkError", "ProxyError", "RemoteProtocolError",
})

_WAYBACK_PAUSE_AFTER = 3
_WAYBACK_PAUSE_S = 15 * 60
_wayback_503s = 0
_wayback_pause_until = 0.0


def url_host(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower().rstrip(".")


def host_is_retired(url: str | None) -> bool:
    """Hôte dont le DNS est mort — ne pas fetcher ni capturer."""
    host = url_host(url)
    if not host:
        return False
    return any(host == suf or host.endswith("." + suf)
               for suf in RETIRED_HOST_SUFFIXES)


def url_looks_like_download(url: str | None, disposition: str = "") -> bool:
    """Fichier à télécharger (PDF, attachment) — Chromium ne peut pas le photographier."""
    if "attachment" in (disposition or "").lower():
        return True
    if url_looks_like_pdf(url):
        return True
    path = urlparse(url or "").path or ""
    return bool(_DOWNLOAD_PATH_RE.search(path))


def should_skip_screenshot(url: str | None, page: dict | None = None) -> str | None:
    """Raison de sauter la capture Review, ou None."""
    page = page or {}
    if host_is_retired(url) or host_is_retired(page.get("final_url")):
        return "hôte retiré (DNS mort)"
    if page.get("error") == "dead_host":
        return "hôte retiré (DNS mort)"
    if page.get("is_pdf"):
        return "PDF"
    if page.get("download") or url_looks_like_download(
            url, page.get("disposition") or ""):
        return "téléchargement / PDF"
    if page.get("fetch_failed") or page.get("error") in _FETCH_FAIL_ERRORS:
        return "URL déjà morte / fetch N1 échoué"
    if page.get("blocked"):
        return "anti-bot"
    return None


def reset_wayback_circuit() -> None:
    """Tests : rouvre le circuit Wayback."""
    global _wayback_503s, _wayback_pause_until
    _wayback_503s = 0
    _wayback_pause_until = 0.0


def wayback_circuit_open(now: float | None = None) -> bool:
    return (now if now is not None else time.time()) < _wayback_pause_until


def _note_wayback_503() -> None:
    global _wayback_503s, _wayback_pause_until
    _wayback_503s += 1
    if _wayback_503s >= _WAYBACK_PAUSE_AFTER:
        _wayback_pause_until = time.time() + _WAYBACK_PAUSE_S
        _wayback_503s = 0


def _note_wayback_ok() -> None:
    global _wayback_503s
    _wayback_503s = 0


_IMAGE_EXTS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "tif", "tiff", "avif",
})


def path_looks_like_image(path: str | None) -> bool:
    """Last segment .png/.jpg/… — WordPress portraits, not a card."""
    name = unquote((path or "").split("?")[0]).rsplit("/", 1)[-1].lower()
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1] in _IMAGE_EXTS


def url_looks_like_image(url: str | None) -> bool:
    """Image URL — Chromium + trafilatura on it blows RAM."""
    return path_looks_like_image(urlparse(url or "").path)


def content_is_image(content: bytes | None, content_type: str = "",
                     url: str = "") -> bool:
    """Real image bytes (magic / Content-Type). Error HTML is not one."""
    blob = content or b""
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if blob[:3] == b"\xff\xd8\xff":
        return True
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return True
    head = blob.lstrip()[:80].lower()
    if head.startswith(b"<") or head.startswith(b"{") or head.startswith(b"<!doctype"):
        return False
    ctype = (content_type or "").lower().split(";", 1)[0].strip()
    if ctype.startswith("image/"):
        return True
    return bool(url_looks_like_image(url) and blob[:1] not in (b"<", b"{"))


_HTMLISH_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "application/xhtml",
    "text/xml", "application/xml", "text/plain",
})


def content_looks_like_html(content: bytes | None, content_type: str = "") -> bool:
    """A real document to parse. A PNG read as UTF-8 is not one."""
    blob = content or b""
    ctype = (content_type or "").lower().split(";", 1)[0].strip()
    head = blob.lstrip()[:80]
    if head.startswith(b"<") or head.lower().startswith(b"<!doctype"):
        return True
    if ctype.startswith("image/") or ctype in {
        "application/pdf", "application/zip", "application/octet-stream",
        "application/gzip", "audio/mpeg", "video/mp4",
    }:
        return False
    if ctype in _HTMLISH_TYPES or ctype.startswith("text/"):
        return True
    return False


def looks_bot_challenge_response(resp, content: bytes | None, url: str = "") -> bool:
    """SiteGround captcha / interstitial: 202 + HTML, sg-captcha header, or markers."""
    headers = getattr(resp, "headers", None)
    if headers is not None and headers.get("sg-captcha"):
        return True
    blob = content or b""
    probe = blob[:6000].decode("utf-8", errors="replace")
    if looks_hard_challenge(text=probe[:2000], html=probe):
        return True
    status = getattr(resp, "status_code", None)
    head = blob[:400].lstrip().lower()
    if status == 202 and url_looks_like_pdf(url) and (
            head.startswith(b"<") or head.startswith(b"<!doctype")):
        return True
    return False


def looks_blocked(text: str, html: str = "", title: str = "") -> bool:
    """True if the page is an anti-bot challenge / blocking interstitial.
    Long genuine regulatory content that *cites* these words stays accepted."""
    probe = " ".join(p for p in (title or "", (text or "")[:4000], (html or "")[:6000]) if p)
    if not probe or not BLOCKED_MARKERS_RE.search(probe):
        return False
    return len((text or "").strip()) < 1500


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def pdf_cache_key(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _pdf_cache_path(digest: str) -> Path:
    # .act1: TURÍSTICA annotation (SCT columns) — invalidates text-only caches.
    return Path(PDF_CACHE_DIR) / f"{digest}.act1.txt"


def _read_pdf_cache(digest: str) -> str | None:
    path = _pdf_cache_path(digest)
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        return None
    return None


def _write_pdf_cache(digest: str, text: str) -> None:
    cache_dir = Path(PDF_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _pdf_cache_path(digest)
    tmp = cache_dir / f".{digest}.txt.partial"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _pdf_worker_env() -> dict:
    env = os.environ.copy()
    backend = str(BACKEND_DIR)
    current = env.get("PYTHONPATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    if backend not in parts:
        env["PYTHONPATH"] = os.pathsep.join([backend, *parts]) if parts else backend
    return env


def _run_pdf_worker(inp: str, out: str, max_pages: int) -> None:
    """Spawn ``app.core.pdf_worker`` — hookable from tests."""
    proc = subprocess.run(
        [sys.executable, "-m", "app.core.pdf_worker", inp, out, str(max_pages)],
        timeout=PDF_SUBPROCESS_TIMEOUT_S,
        capture_output=True,
        env=_pdf_worker_env(),
        cwd=str(BACKEND_DIR),
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[:300]
        raise RuntimeError(f"pdf_worker exit {proc.returncode}: {err}")


def pdf_page_jpegs(content: bytes, max_pages: int = 2) -> list[bytes]:
    """First PDF pages as JPEG, out of process (Review vision)."""
    if not content:
        return []
    fd, inp = tempfile.mkstemp(suffix=".pdf")
    out_dir = tempfile.mkdtemp(prefix="pdf-preview-")
    try:
        os.write(fd, content)
        os.close(fd)
        fd = -1
        proc = subprocess.run(
            [sys.executable, "-m", "app.core.pdf_preview", inp, out_dir,
             str(max_pages)],
            timeout=PDF_SUBPROCESS_TIMEOUT_S,
            capture_output=True,
            env=_pdf_worker_env(),
            cwd=str(BACKEND_DIR),
        )
        if proc.returncode != 0:
            return []
        out: list[bytes] = []
        for path in sorted(Path(out_dir).glob("*.jpg")):
            out.append(path.read_bytes())
        return out
    except Exception:
        return []
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(inp)
        except OSError:
            pass
        try:
            shutil.rmtree(out_dir, ignore_errors=True)
        except OSError:
            pass


def parse_pdf_text(content: bytes, max_pages: int = 180) -> str:
    """Extract PDF text out of process, with sha256 disk cache.

    The API process does not import ``fitz``: a native crash stays confined
    to the subprocess. A cache hit avoids any spawn (replayed gazettes).
    """
    if not content:
        return ""
    digest = pdf_cache_key(content)
    cached = _read_pdf_cache(digest)
    # An empty cache (scan without OCR, before tesseract) must not freeze the failure.
    if cached is not None and cached.strip():
        return cached
    with _pdf_slots:
        cached = _read_pdf_cache(digest)
        if cached is not None and cached.strip():
            return cached
        fd, inp = tempfile.mkstemp(suffix=".pdf")
        out = inp + ".txt"
        try:
            os.write(fd, content)
            os.close(fd)
            fd = -1
            _run_pdf_worker(inp, out, max_pages)
            text = Path(out).read_text(encoding="utf-8")
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            for p in (inp, out):
                try:
                    os.unlink(p)
                except OSError:
                    pass
    try:
        _write_pdf_cache(digest, text)
    except OSError:
        pass
    return text


async def parse_pdf_with_nemotron_vl(content: bytes, settings: dict | None = None,
                                      max_pages: int = 180, log=None) -> str | None:
    """Extract and understand PDF using Nemotron 3.5 Super VL with reasoning_effort='none'.

    This calls Nemotron directly for PDF comprehension (not just text extraction).
    Falls back to None if NVIDIA key not available or call fails.
    """
    from app.core.nvidia import NEMOTRON_SUPER_VL_MODEL, get_nvidia_key, _nvidia_sem, complete_json_nvidia_tracked
    
    key = get_nvidia_key(settings)
    if not key:
        if log:
            log("Nemotron VL: NVIDIA_API_KEY not configured")
        return None
    
    # First extract text via existing PyMuPDF
    text = await asyncio.to_thread(parse_pdf_text, content, max_pages)
    if not text or not text.strip():
        return None
    
    # Generate PDF page JPEGs for vision input
    images = pdf_page_jpegs(content, max_pages=2)
    
    # Truncate to reasonable size for LLM context
    text = text[:15000]
    
    prompt = f"""Extract structured information from this PDF content. Return JSON with:
- "text": the extracted text (cleaned, no artifacts)
- "ports": array of ports found with name, city, lat, lon if present
- "official": boolean - is this an official government document?
- "document_type": string (gazette, decree, catalog, list, etc.)

PDF content (truncated):
{text}"""
    
    try:
        async with _nvidia_sem():
            # Returns (data, served_model); reasoning is already off for JSON roles.
            data, _served = await complete_json_nvidia_tracked(
                "Tu réponds uniquement en JSON strict.", prompt, settings,
                model=NEMOTRON_SUPER_VL_MODEL, role="json", fallback=False, max_tokens=3000,
                images=images)
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        if log:
            log(f"Nemotron VL PDF: {type(e).__name__}: {str(e)[:80]}")
        return None


def parse_html_n1(html: str) -> str:
    import trafilatura
    return trafilatura.extract(html) or ""


def parse_html_n2(html: str) -> dict:
    from readability import Document as ReadabilityDoc
    doc = ReadabilityDoc(html)
    title = (doc.short_title() or "").strip()
    soup = BeautifulSoup(doc.summary(), "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    if len(text) < 200:
        full = BeautifulSoup(html, "html.parser")
        text = re.sub(r"\s+", " ", full.get_text(" ")).strip()[:12000]
    return {"text": text, "title": title}


def _parse_similarity(a: str, b: str) -> float:
    """Similarity between the two parsers' texts (quality signal)."""
    if not a or not b:
        return 0.0
    na = re.sub(r"\s+", " ", a)[:3000]
    nb = re.sub(r"\s+", " ", b)[:3000]
    return round(difflib.SequenceMatcher(None, na, nb).ratio(), 3)


def _extract_agree_sim() -> float:
    from app.core.run_rules import get_rule
    return float(get_rule("shared.extract_agree_sim", 0.55))


async def dual_parse_html(html: str) -> dict:
    """Compared PARALLEL PARSING: trafilatura ∥ Readability on the same HTML.
    The richer text wins; similarity between the two is kept
    as a signal (strong agreement = robust extraction, divergence = inspect).
    Return {text, level, title, n1_chars, n2_chars, similarity, agree}."""
    async def _n1():
        try:
            return (await asyncio.to_thread(parse_html_n1, html)).strip()
        except Exception:
            return ""

    async def _n2():
        try:
            return await asyncio.to_thread(parse_html_n2, html)
        except Exception:
            return {"text": "", "title": ""}

    t1, n2 = await asyncio.gather(_n1(), _n2())
    t2, title2 = (n2.get("text") or "").strip(), (n2.get("title") or "").strip()
    sim = _parse_similarity(t1, t2)
    if len(t1) >= len(t2):
        text, level = t1, "N1-trafilatura"
    else:
        text, level = t2, "N2-readability"
    return {
        "text": text, "level": level, "title": title2,
        "n1_chars": len(t1), "n2_chars": len(t2),
        "similarity": sim, "agree": (sim >= _extract_agree_sim()) if (t1 and t2) else None,
    }


def page_metadata(html: str, url: str) -> dict:
    """Title, meta description, main image, external links (BS4)."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text().strip() if soup.title else "") or ""
    meta = soup.find("meta", attrs={"name": "description"}) or \
        soup.find("meta", attrs={"property": "og:description"})
    meta_desc = meta.get("content", "").strip() if meta else ""
    image = None
    for attrs in ({"property": "og:image"}, {"name": "twitter:image"}, {"property": "twitter:image"}):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content", "").strip():
            image = urljoin(url, m["content"].strip())
            break
    if not image:
        for img in soup.find_all("img", src=True):
            src = img["src"].strip()
            low = src.lower()
            if src.startswith("data:") or low.endswith(".svg"):
                continue
            if any(b in low for b in ("logo", "icon", "sprite", "avatar", "placeholder", "pixel")):
                continue
            image = urljoin(url, src)
            break
    base_host = urlparse(url).netloc
    ext_links, seen_d = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        d = urlparse(href).netloc
        name = re.sub(r"\s+", " ", a.get_text(" ")).strip()
        if href.startswith("http") and d and d != base_host and d not in seen_d and 3 < len(name) < 80:
            seen_d.add(d)
            ext_links.append({"name": name, "url": href})
        if len(ext_links) >= 15:
            break
    return {"title": title, "meta_desc": meta_desc, "image": image, "ext_links": ext_links}


def internal_followups(html: str, base_url: str, patterns=FOLLOWUP_PATTERNS, limit: int = 3) -> list[str]:
    """Selective depth=2: internal links like /annuaire, /contacts, /clearance…"""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        if urlparse(href).netloc != base_host or href.rstrip("/") == base_url.rstrip("/"):
            continue
        path = urlparse(href).path.lower()
        if any(p in path for p in patterns) and href not in seen and not SERP_HARD_RE.search(href):
            seen.add(href)
            out.append(href)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Main cascade
# ---------------------------------------------------------------------------
def _is_ssl_cert_error(exc: BaseException) -> bool:
    """Missing intermediate certificate (SIS Egypt / Sectigo lesson)."""
    blob = ""
    cur: BaseException | None = exc
    for _ in range(6):
        if cur is None:
            break
        blob += f" {type(cur).__name__} {cur}".lower()
        cur = cur.__cause__ or getattr(cur, "__context__", None)
    return any(tok in blob for tok in (
        "certificate", "sslcert", "cert verify", "ssl: certificate",
    ))


async def fetch_raw(url: str, timeout: int = 25) -> httpx.Response:
    """GET with manual redirects. Each hop must be a public http(s) URL."""
    kwargs = dict(timeout=timeout, follow_redirects=False, headers=UA_BROWSER)
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            r = await fetch_checked(client, url)
            r.raise_for_status()
            return r
    except httpx.RequestError as e:
        if not _is_ssl_cert_error(e):
            raise
    async with httpx.AsyncClient(**kwargs, verify=False) as client:
        r = await fetch_checked(client, url)
        r.raise_for_status()
        return r


def is_maps_render_url(url: str | None) -> bool:
    """Google Maps card or search: JS DOM, not a decree. Fetch only."""
    raw = (url or "").strip()
    if not raw.startswith("http"):
        return False
    host = (urlparse(raw).hostname or "").lower()
    if "google." not in host and not host.endswith("goo.gl"):
        return False
    path = (urlparse(raw).path or "").lower()
    query = (urlparse(raw).query or "").lower()
    if "/maps/place/" in path or "/maps/search/" in path or "/maps/dir/" in path:
        return True
    if "/maps" in path and ("api=1" in query or "query=" in query):
        return True
    return False


def content_is_pdf(content: bytes | None, content_type: str = "",
                    url: str = "", content_disposition: str = "") -> bool:
    """Real PDF. Error HTML served under a .pdf URL is not one."""
    blob = content or b""
    if blob[:5] == b"%PDF-":
        return True
    head = blob.lstrip()[:80].lower()
    if head.startswith(b"<") or head.startswith(b"{") or head.startswith(b"<!doctype"):
        return False
    ctype = (content_type or "").lower()
    if "application/pdf" in ctype or ctype.strip() == "application/pdf":
        return True
    cd = (content_disposition or "").lower()
    if ".pdf" in cd and "filename" in cd:
        return True
    path = (urlparse(url or "").path or "").lower()
    if path.endswith(".pdf") and blob[:5] == b"%PDF-":
        return True
    if path.endswith(".pdf") and not head:
        return True
    return False


def text_usable(text: str | None, min_chars: int = 200, *,
                html: str = "", title: str = "") -> bool:
    """Text long enough and not an anti-bot challenge."""
    t = (text or "").strip()
    if len(t) < int(min_chars or 0):
        return False
    return not looks_blocked(text or "", html=html, title=title)


def fetch_record_usable(rec: dict | None, min_chars: int = 200, *,
                         keep_if_links: bool = False) -> bool:
    """Fetch produced something we can keep — no need to start Chromium."""
    rec = rec or {}
    if rec.get("blocked") or rec.get("error") == "bot_blocked":
        return False
    if text_usable(rec.get("text") or "", min_chars, title=rec.get("title") or ""):
        return True
    if keep_if_links:
        links = rec.get("links") or []
        if any(_link_href(x) for x in links):
            return True
    return False


def _link_href(raw) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        return str(raw.get("url") or raw.get("href") or "").strip()
    return ""


def html_hrefs(html: str | None, url: str, limit: int = 50) -> list[str]:
    """All http(s) hrefs, internals included — needed for AMP / depth-2."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        if not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        out.append(href)
        if len(out) >= limit:
            break
    return out


def empty_page(url: str, *, level: str = "failed", error: str | None = None) -> dict:
    return {
        "url": url, "text": "", "md5": None, "level": level, "title": "",
        "meta_desc": "", "image": None, "ext_links": [], "links": [],
        "is_pdf": False, "html": None, "blocked": False, "render_used": False,
        "parse": None, "fetch_compare": None, "final_url": url, "error": error,
        "raw": None,
    }


def page_from_fetch_record(url: str, rec: dict | None) -> dict:
    rec = rec or {}
    blocked = bool(rec.get("blocked") or rec.get("error") == "bot_blocked")
    text = "" if blocked else (rec.get("text") or "")
    links = [_link_href(x) for x in (rec.get("links") or [])]
    links = [u for u in links if u.startswith("http")]
    out = empty_page(url, level="blocked" if blocked else "N3-fetch",
                      error=None if not rec.get("error") else rec.get("error"))
    out["text"] = text
    out["title"] = rec.get("title") or ""
    out["blocked"] = blocked
    out["links"] = links
    out["final_url"] = rec.get("final_url") or url
    if text:
        out["md5"] = hashlib.md5(text.encode("utf-8")).hexdigest()
        if not blocked:
            out["level"] = rec.get("level") or "N3-fetch"
    return out


def as_fetch_record(page: dict | None, url: str = "") -> dict:
    """TinyFish Fetch shape, for harvest_bu_catalog / AMP without changing the contract."""
    page = page or {}
    url = url or page.get("url") or ""
    links = list(page.get("links") or [])
    for item in page.get("ext_links") or []:
        href = _link_href(item)
        if href.startswith("http") and href not in links:
            links.append(href)
    text = page.get("text") or ""
    blocked = bool(page.get("blocked"))
    err = page.get("error")
    if not text and not blocked and not err:
        err = page.get("level") if page.get("level") in ("failed", "blocked") else None
    return {
        "text": "" if blocked else text,
        "title": page.get("title") or "",
        "blocked": blocked,
        "links": links,
        "level": page.get("level") or "",
        "error": err,
        "final_url": page.get("final_url") or url,
        "html": page.get("html"),
        "is_pdf": bool(page.get("is_pdf")),
        "render_used": bool(page.get("render_used")),
    }


def _is_mirror_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("jina.ai") or host.endswith("web.archive.org") or host.endswith("archive.org")


# "4.- Ensenada", "1. Apia", or first Jina item "[1.-](url)Bahía Colonet"
_HEAD = r"(?:\[\d+\.-\]\([^)]+\)|\d+\.-\s+|\d+[.)]\s+)"
_CATALOG_HEAD = re.compile(
    rf"(?:^|\n)[ \t]*(?:#{{1,6}}\s+)?{_HEAD}([^\n|#]{{2,80}})\n"
    rf"((?:.*\n){{0,16}}?)(?=[ \t]*(?:#{{1,6}}\s+)?{_HEAD}|\Z)",
    re.M,
)
# Jina: **Latitud:**31.89  — EN: Latitude: -13.8  — tables: | Latitud: | 31.89 |
_CATALOG_SEP = r"[\s|*]*"
_CATALOG_LAT = re.compile(rf"(?:latitud(?:e)?|lat\.?):{_CATALOG_SEP}([+-]?\d+(?:\.\d+)?)", re.I)
_CATALOG_LON = re.compile(rf"(?:longitud(?:e)?|long\.?|lng|lon):{_CATALOG_SEP}([+-]?\d+(?:\.\d+)?)", re.I)
_CATALOG_STATE = re.compile(
    rf"(?:entidad federativa|province|state|région|region|departamento|governorate):"
    rf"{_CATALOG_SEP}([^\n|*]+)",
    re.I,
)
# Legal phrasing: port of Alofi, port de Papeete, puerto de Ensenada, porto de Santos…
_PORT_OF_RE = re.compile(
    r"\b(?:ports?\s+of\s+|porto?s?\s+d(?:e\s+|['’])|puertos?\s+de\s+|"
    r"portos?\s+de\s+|havens?\s+van\s+|hafen\s+von\s+|porti?\s+di\s+)"
    r"(?-i:([A-ZÀ-Ý][\w'’. -]{0,40}?))"
    r"(?=,|;|\.| the | or |\n| et | ou | y | e | und | oder )",
    re.I,
)
_PORT_OF_SKIP = frozenset({
    "entry", "entries", "entrée", "entree", "entrada", "ingresso",
    "call", "departure", "the", "a", "an", "any",
    "registry", "register", "registre", "destination",
    "commerce", "plaisance", "recreo", "recreio", "mer", "mar",
    "base", "principal", "éligibles", "eligibles", "eligible",
    "rattachement", "liste",
    "arrival", "first", "marina", "zarpe", "permiso", "funcionario",
    "autonome", "autonoma", "autónomo",
})
_CATALOG_MARKERS_RE = re.compile(
    r"puertos habilitados|puertos y terminales habilitados|"
    r"designated ports|ports? d['’]entrée|"
    r"ports? of entry|puertos de entrada|portos de entrada|"
    r"ports? de plaisance|capitan[ií]as?\s+de\s+puerto|"
    r"places of first arrival|approved ports|"
    r"porti\s+detar|porteve\s+detare|dega\s+doganore",
    re.I,
)
# Capitanía / Capitanias — not "Capitán de Puerto" (title, statute prose).
# The name must start with a capital (independent of IGNORECASE).
_CAPITANIA_RE = re.compile(
    r"Capitan(?:[ií]as?|te)\s+[«\"']?\s*d[ae]\s+Puerto\s+"
    r"(?:d[ae]\s+|del?\s+|po\s+)?"
    r"(?-i:([A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚáéíóúñü'’.\-]+"
    r"(?:\s*-\s*[A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚáéíóúñü'’.\-]+"
    r"|\s+(?:de|del|la|las|los|y|&)\s+[A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚáéíóúñü'’.\-]+"
    r"|\s+[A-ZÁÉÍÓÚÑÜ][A-Za-zÁÉÍÓÚáéíóúñü'’.\-]+"
    r"){0,4}))",
    re.I,
)
# OCR line like "7) Capitante «de Puerto de Puerto: Sucre »."
_CAPITANIA_NUM_LINE_RE = re.compile(
    r"(?m)^\s*\d{1,2}\s*[).:—\-]\s*.{0,24}Puerto\s+(?:d[ae]\s+|po\s+)?"
    r"(?-i:([A-ZÁÉÍÓÚÑÜ][^\n]{1,40}))",
    re.I,
)
_CAPITANIA_CUT_RE = re.compile(
    r"\b(?:tendr[aá]|art[ií]culo|su sede|geogr[aá]fica|dependencias|"
    r"estar[aá]|permiso|funcionario|circunscrip|resoluci[oó]n|"
    r"ministerio|gaceta|contralor[ií]a|decisi[oó]n|indica)\b",
    re.I,
)
_CAPITANIA_SKIP = frozenset({
    "la republica", "la república", "la circunscripcion", "la circunscripción",
    "cada circunscripcion", "el permiso", "un funcionario", "puerto",
})
_VE_OCR_ALIASES = {
    "gulria": "Güiria", "guiria": "Güiria", "giliria": "Güiria",
    "gualra": "La Guaira", "guaira": "La Guaira", "maracalbo": "Maracaibo",
    "puertosucre": "Puerto Sucre",
}
_VE_CANON = (
    "Maracaibo", "Las Piedras", "La Vela de Coro", "Puerto Cabello",
    "La Guaira", "Guanta-Puerto La Cruz", "Puerto Sucre", "Carúpano",
    "Pampatar", "Güiria", "Caripito", "Ciudad Guayana", "Ciudad Bolívar",
    "Amazonas", "Apure",
)
# Line-start only: "ports de plaisance de français…" prose
# from the customs landing must not become a toponym.
_PLAISANCE_PORT_RE = re.compile(
    r"(?m)^(?:[-•*]|\d+[.)])?\s*Ports?\s+de\s+plaisance\s+"
    r"(?:de\s+|d['’]|du\s+|des\s+)?"
    r"([A-ZÀ-Ý][\w'’. \-]{1,50})"
)
_PLAISANCE_NAME_SKIP_RE = re.compile(
    r"(?i)\b(français|francais|depuis|également|egalement|eligibles|"
    r"éligibles|qui|que|non|pas|sont|aussi|cette|ppf|schengen|liste|"
    r"carte|rattachement|navires?)\b",
)
_GENERIC_PORT_NAMES = frozenset({
    "marina", "plaisance", "port", "puerto", "harbour", "harbor",
})
_FR_REGION_RE = re.compile(
    r"^(hauts?-?\s*de\s+france|normandie|bretagne|paca|nouvelle\s+aquitaine|"
    r"corse|occitanie|pays\s+de\s+la\s+loire|provence)",
    re.I,
)
# SCT / SEMAR table: "1 Bahía Colonet\nBaja California\nPuerto\ndate\nlat\nlon"
_MX_HABILITADO_ROW_RE = re.compile(
    r"(?m)^\s*(?P<n>\d{1,3})\s+(?P<name>[A-ZÁÉÍÓÚÑÜ][^\n]{1,80})\n"
    r"\s*(?P<state>[A-ZÁÉÍÓÚÑÜ][^\n]{2,50})\n"
    r"\s*(?P<kind>Puerto|Terminal|Marina|Muelle|Recinto)[^\n]*\n"
    r"\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})\n"
    r"\s*(?P<lat>[+-]?\d{1,3}\.\d+)\n"
    r"\s*(?P<lon>[+-]?\d{2,3}\.\d+)",
    re.I,
)
# MPI: each PoFA is a markdown table "| Name |\n| --- |\n| Approved vessels |"
_NZ_POFA_MD_RE = re.compile(
    r"(?m)^\s*\|\s*([A-Z][^|\n]{2,70}?)\s*\|\s*\n\s*\|\s*---\s*\|\s*\n"
    r"\s*\|\s*Approved vessels\s*\|",
)
_NZ_POFA_PLAIN_RE = re.compile(
    r"(?m)^\s*([A-Z][^\n]{2,70})\n\s*Approved vessels\b",
)
_DOUANE_BUREAU_RE = re.compile(
    r"(?i:bureau(?:x)?\s+des?\s+douanes?\s+(?:de\s+|d['’]|du\s+)?)"
    r"([A-ZÀ-Ý][\w'’.\-]+(?:[\s\-][A-ZÀ-Ý][\w'’.\-]+){0,3})",
)
_CATALOG_ACTIVITY = re.compile(
    rf"(?:tipo de actividad|actividad):{_CATALOG_SEP}([^\n|*]+)",
    re.I,
)
_MX_TURISTICA_BLOCK_RE = re.compile(
    r"\[ACTIVIDAD_TURISTICA\]\s*(.*?)(?:\n\[|\Z)",
    re.I | re.S,
)
_ANNEXE_MARITIME_RE = re.compile(
    r"fronti[eè]res\s+maritimes\s*(.*?)(?="
    r"fronti[eè]res\s+a[eé]riennes|"
    r"liste des documents|"
    r"annexe\s*i\s*i|"
    r"annexe\s*ii|"
    r"\Z)",
    re.I | re.S,
)
_PPC_SITE_RE = re.compile(
    r"(?m)^\s*\|?\s*([A-ZÀ-Ý][A-Za-zÀ-ÿ'’.\-]+"
    r"(?:[\s\-][A-ZÀ-Ý][A-Za-zÀ-ÿ'’.\-]+){0,3})"
    r"(?:\s*\|\s*)?(?:Permanent|Temporaire|sur demande)?\s*\|?\s*$",
)
_UK_PLEASURE_SECTION_RE = re.compile(
    r"(?:list of (?:uk )?(?:ports|marinas)|designated ports|"
    r"ports? of entry for pleasure|"
    r"pleasure craft ports?)\s*:?\s*(.*?)(?=\n#{1,3}\s|\Z)",
    re.I | re.S,
)
# SX customs page: "Some examples include: Simpsonbay Marina, … Greatbay harbor"
# Do not match "authorized ports in Sint Maarten." (too short a phrase).
_SX_EXAMPLES_RE = re.compile(
    r"(?:some\s+examples\s+include|examples\s+include)\s*[:\s]+"
    r"(.+?)(?:Customs Officers have access|\.\s+Furthermore|\Z)",
    re.I | re.S,
)
_SX_SKIP_PLACE_RE = re.compile(
    r"(?i)\b(airport|aeroport|a[eé]roport|post office|coastline|"
    r"entire coastline|juliana)\b",
)
# SIS Egypt: "Hurghada Marina:" titles inside "specialized marinas … including"
_EG_SIS_LIST_RE = re.compile(
    r"specialized marinas.{0,160}?including\s*:?\s*(.+?)"
    r"(?:The State Sets|legislative framework|"
    r"Prime Minister.?s decision No\.?\s*2721|\Z)",
    re.I | re.S,
)
_EG_MARINA_HEAD_RE = re.compile(
    r"(?<![A-Za-z])("
    r"[A-Z][A-Za-z][\w'’-]*"
    r"(?:\s+[A-Z][A-Za-z0-9][\w'’-]*){0,4}"
    r"\s+Marina"
    r"(?:\s*\([^)]{0,50}\))?"
    r"(?:\s+North\s+Coast)?"
    r")\s*:",
)
_EG_SKIP_MARINA_RE = re.compile(
    r"(?i)\b(proposed|planned|establishing|existing|airport|"
    r"specialized marinas|egyptian marinas|international marinas|"
    r"tourist harbou?rs?)\b",
)
# Kartelë Dogana AL: "2. Lezhë - Porti detar\nShëngjin" (the port, not the town).
_AL_PORTI_DETAR_RE = re.compile(
    r"(?i)porti\s+detar\s+([A-ZÀ-ÝË][A-Za-zÀ-ÿËëÇç]{2,24})",
)
_AL_PORTI_SKIP = frozenset({
    "detar", "peshkimit", "aplikantit", "mbikëqyrëse", "kompetente",
    "qyteti", "adresa", "orari",
})
_JORF_READERS = {
    "JORFTEXT000030235682": [
        # Same decree (NOR INTV1430080A): Légifrance is behind Cloudflare.
        "https://www.info-droits-etrangers.org/wp-content/uploads/2020/01/"
        "ARR%C3%8AT%C3%89_du_4_f%C3%A9vrier_2015_version_initiale.pdf",
    ],
}
_FR_AUTH_RE = re.compile(
    r"^(paf|douane|police aux fronti|garde-fronti|autorit|version\s|"
    r"liste des ports|r[eé]gion|commune|port de plaisance|ppf\b)",
    re.I,
)
_DELEG_BLOCK_RE = re.compile(
    r"delegaciones\s*:\s*(.*?)(?=art[ií]culo|estaci[oó]n de pilotos|\Z)",
    re.I | re.S,
)
_DELEG_LINE_RE = re.compile(
    r"^[\s\-—–•·]+([A-ZÁÉÍÓÚÑÜ][\w'’.\-áéíóúñüÁÉÍÓÚÑÜ ()]{2,50})\s*$",
    re.M,
)
_PDF_ABS_RE = re.compile(r"https?://[^\s\]\)'\"<>]+\.pdf(?:\?[^\s\]\)'\"<>]*)?", re.I)
_PDF_HREF_RE = re.compile(
    r"""(?:href|src)\s*=\s*["']([^"']+\.pdf(?:\?[^"']*)?)["']""",
    re.I,
)
_PDF_MD_RE = re.compile(r"\[[^\]]*\]\(([^)]+\.pdf(?:\?[^)]*)?)\)", re.I)
# Path only — "douane" / "customs" in the hostname match
# every brochure on a customs homepage (France lesson: 31 English PDFs).
_LIST_PDF_PATH_RE = re.compile(
    r"liste|listen|plaisance|eligibles|ppf|puerto|terminal|habilit|"
    r"port.?of.?entry|ports.?of.?entry|ports-entree|portos-de-entrada|"
    r"points-d-entree|points-of-entry|designat|gazett|legislat|"
    r"decreto|decret|arrete|capitanias|jurisdiccion|ley-de-marinas|"
    r"jorftext|c1331|pleasure-craft|pleasure_craft|"
    r"akciz|peshkimit|anijet|autorizim|porti-detar",
    re.I,
)
_JUNK_PDF_PATH_RE = re.compile(
    r"formulaire|immigration|export|brexit|travellers?|tax-refund|"
    r"leaflet|brochure|results-en|counterfeit",
    re.I,
)
_YEAR_IN_PATH_RE = re.compile(r"/20(\d{2})/")


def looks_hard_challenge(text: str = "", html: str = "", title: str = "") -> bool:
    """Akamai / "Challenge Validation": Chromium cannot solve it."""
    probe = " ".join(p for p in (title or "", (text or "")[:2000], (html or "")[:4000]) if p)
    return bool(probe and HARD_CHALLENGE_RE.search(probe))


def looks_like_port_catalog(text: str) -> bool:
    """A real catalog (coords or numbered decree), not any statute
    that contains "1. Article"."""
    if not text:
        return False
    from app.core.run_rules import get_rule
    lat_hits = len(re.findall(r"latitud(?:e)?\s*:", text, re.I))
    lat_need = int(get_rule("formalities.catalog_lat_hits", 8))
    if lat_hits >= lat_need:
        return True
    if _CATALOG_MARKERS_RE.search(text) and (text.count(".-") >= lat_need or lat_hits >= 3):
        return True
    if re.search(r"puertos\s+y\s+terminales\s+habilitados", text, re.I):
        return True
    if len(_MX_HABILITADO_ROW_RE.findall(text)) >= 5:
        return True
    if len(_CAPITANIA_RE.findall(text)) >= 4:
        return True
    if re.search(r"liste des ports de plaisance [ée]ligibles", text, re.I):
        return True
    if sum(1 for ln in text.splitlines() if _FR_REGION_RE.match(ln.strip())) >= 3:
        return True
    if len(_NZ_POFA_MD_RE.findall(text)) >= 3 or len(_NZ_POFA_PLAIN_RE.findall(text)) >= 3:
        return True
    if (re.search(r"points? de passage contr[oô]l[eé]s", text, re.I)
            and re.search(r"fronti[eè]res\s+maritimes", text, re.I)):
        return True
    if len(re.findall(r"place(?:s)? of first arrival", text, re.I)) >= 2:
        return True
    if (re.search(r"approved ports", text, re.I)
            and re.search(r"place(?:s)? of first arrival", text, re.I)):
        return True
    if (re.search(r"simpson\s*bay", text, re.I)
            and re.search(r"great\s*bay", text, re.I)):
        return True
    if (re.search(r"examples include", text, re.I)
            and len(re.findall(r"\bmarina\b", text, re.I)) >= 3):
        return True
    if (re.search(r"specialized marinas.{0,200}?including", text, re.I)
            and sum(1 for raw in _EG_MARINA_HEAD_RE.findall(text)
                    if _eg_normalize_marina(raw)) >= 3):
        return True
    if (re.search(r"porti\s+detar", text, re.I)
            and re.search(r"dega\s+doganore|porteve\s+detare|anijet?\s+e\s+peshkimit",
                          text, re.I)
            and len(_AL_PORTI_DETAR_RE.findall(text)) >= 3):
        return True
    return False


def extract_structured_ports(text: str) -> list[dict]:
    """Read an official list from the source text (not a hardcoded list):
    "N.- Name" titles + lat/lon, or legal phrasing "port of X".
    Decree coordinates are kept to avoid mass geocoding.
    Each [SOURCE: …] block is read in isolation: concatenating a statute PDF
    to a catalog must not swallow the last port."""
    if not text:
        return []
    # _CATALOG_HEAD reads blocks via (?:.*\n): without a final newline, the last
    # port (coords on the last line) is swallowed. TinyFish Fetch does not
    # always add one.
    if not text.endswith("\n"):
        text = text + "\n"
    parts = re.split(r"\n(?=\[SOURCE: )", text) if "[SOURCE:" in text else [text]
    if len(parts) == 1:
        return _extract_structured_ports_one(text)
    out, seen_keys = [], set()
    for part in parts:
        for p in _extract_structured_ports_one(part):
            key = p["name"].casefold()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(p)
    return out


def _clean_legal_port_name(raw: str) -> str:
    n = " ".join((raw or "").replace(":", " ").split()).strip(" .;,-«»\"'")
    n = _CAPITANIA_CUT_RE.split(n, maxsplit=1)[0]
    n = re.sub(r"\s+tendr[aá].*$", "", n, flags=re.I)
    n = re.sub(r"\s+ten-\s*$", "", n, flags=re.I)
    n = re.sub(r"^(?:po|da|del)\s+", "", n, flags=re.I)
    return n.strip(" .;,-«»\"'")[:120]


def _fold_ocr(s: str) -> str:
    nfd = unicodedata.normalize("NFD", (s or "").casefold())
    return re.sub(r"[^a-z]+", "", "".join(
        ch for ch in nfd if unicodedata.category(ch) != "Mn"))


def _normalize_capitania_name(name: str) -> str:
    """Fix an obvious OCR error (Gúlria → Güiria), without inventing a port."""
    fold = _fold_ocr(name)
    if fold in _VE_OCR_ALIASES:
        return _VE_OCR_ALIASES[fold]
    best, best_r = None, 0.0
    for canon in _VE_CANON:
        ratio = difflib.SequenceMatcher(None, fold, _fold_ocr(canon)).ratio()
        if ratio > best_r:
            best, best_r = canon, ratio
    if best and best_r >= 0.82:
        return best
    return name


def _extract_capitanias(text: str) -> list[dict]:
    out, seen = [], set()
    raw_names = [m.group(1) for m in _CAPITANIA_RE.finditer(text or "")]
    raw_names += [m.group(1) for m in _CAPITANIA_NUM_LINE_RE.finditer(text or "")]
    for raw in raw_names:
        name = _normalize_capitania_name(_clean_legal_port_name(raw))
        fold = name.casefold()
        if (not name or len(name) < 3 or fold in _CAPITANIA_SKIP or fold in seen
                or "republica" in fold or "república" in fold
                or "refrend" in fold or fold.endswith(("tendrá", "tendra", "su"))
                or _PLAISANCE_NAME_SKIP_RE.search(name)):
            continue
        seen.add(name.casefold())
        out.append({
            "name": name, "city": None,
            "note": "capitanía de puerto (règlement de juridiction)",
            "extraction_engine": "catalog",
        })
    for block in _DELEG_BLOCK_RE.findall(text or ""):
        for m in _DELEG_LINE_RE.finditer(block):
            name = _clean_legal_port_name(m.group(1))
            if (not name or len(name) < 3 or name.casefold() in seen
                    or _FR_AUTH_RE.match(name)):
                continue
            seen.add(name.casefold())
            out.append({
                "name": name, "city": None,
                "note": "délégation de capitanía",
                "extraction_engine": "catalog",
            })
    return out


def _extract_fr_plaisance_table(text: str) -> list[dict]:
    """Customs table: Region / Commune / pleasure-craft port / PPF / authority."""
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    out, seen = [], set()
    i = 0
    while i < len(lines):
        if not _FR_REGION_RE.match(lines[i]):
            i += 1
            continue
        if i + 2 >= len(lines):
            break
        commune, port = lines[i + 1], lines[i + 2]
        if (_FR_AUTH_RE.match(commune) or _FR_REGION_RE.match(commune)
                or _FR_AUTH_RE.match(port) or _FR_REGION_RE.match(port)):
            i += 1
            continue
        key = port.casefold()
        if key not in seen and 3 <= len(port) <= 80:
            seen.add(key)
            out.append({
                "name": port[:120], "city": commune[:80],
                "note": "liste ports de plaisance éligibles",
                "extraction_engine": "catalog",
            })
        i += 3
    for m in _PLAISANCE_PORT_RE.finditer(text or ""):
        name = _clean_legal_port_name(m.group(1))
        fold = name.casefold()
        if (not name or fold in seen or fold in _PORT_OF_SKIP or len(name) < 3
                or fold in _GENERIC_PORT_NAMES
                or fold.startswith(("éligibl", "eligibl", "ppf", "rattachement"))
                or _PLAISANCE_NAME_SKIP_RE.search(name)):
            continue
        seen.add(name.casefold())
        out.append({
            "name": f"Port de plaisance de {name}"[:120], "city": None,
            "note": "liste ports de plaisance éligibles",
            "extraction_engine": "catalog",
        })
    return out


def _turistica_allowlist(text: str) -> set[str] | None:
    """Names / numbers tagged TURÍSTICA, or None if the PDF was not annotated."""
    m = _MX_TURISTICA_BLOCK_RE.search(text or "")
    if not m:
        return None
    allowed: set[str] = set()
    for line in m.group(1).splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        num = re.match(r"^(\d{1,3})\s+(.+)$", line)
        if num:
            allowed.add(num.group(1))
            allowed.add(num.group(2).casefold())
        else:
            allowed.add(line.casefold())
    return allowed


def _mx_activity_is_turistica(block: str) -> bool | None:
    """True / False if "Tipo de actividad" is present, otherwise None."""
    m = _CATALOG_ACTIVITY.search(block or "")
    if not m:
        return None
    return bool(re.search(r"tur[ií]stic", m.group(1), re.I))


def _extract_mx_habilitados_table(text: str) -> list[dict]:
    """SCT PDF: No. / name / State / type / date / lat / lon — TURÍSTICA tag only."""
    out, seen = [], set()
    allow = _turistica_allowlist(text)
    has_tag_col = bool(re.search(r"tur[ií]stica", text or "", re.I))
    if has_tag_col and allow is None:
        # Column present but no annotation: do not swallow commercial ports.
        return []
    for m in _MX_HABILITADO_ROW_RE.finditer(text or ""):
        name = " ".join(m.group("name").split()).strip()
        state = " ".join(m.group("state").split()).strip()
        if allow is not None and m.group("n") not in allow and name.casefold() not in allow:
            continue
        key = name.casefold()
        if key in seen and state:
            name = f"{name} ({state})"
            key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name[:120], "city": state[:80] or None,
            "lat": float(m.group("lat")), "lon": float(m.group("lon")),
            "note": "catalogue officiel (activité turística)",
            "extraction_engine": "catalog",
        })
    return out


_NZ_TABLE_SKIP = frozenset({
    "port contact (website)", "mpi contact", "approved cargo",
    "approved vessels", "choose from this list", "what you must do",
})


def _extract_nz_pofa_tables(text: str) -> list[dict]:
    """MPI register "places of first arrival – seaports" (table per port)."""
    out, seen = [], set()
    names = [m.group(1) for m in _NZ_POFA_MD_RE.finditer(text or "")]
    names += [m.group(1) for m in _NZ_POFA_PLAIN_RE.finditer(text or "")]
    skip = _PORT_OF_SKIP | {"approved vessels", "choose from this list",
                            "what you must do", "northland", "approved ports"}
    blob = text or ""
    if (re.search(r"approved ports", blob, re.I)
            or re.search(r"place(?:s)? of first arrival", blob, re.I)):
        for m in re.finditer(
            r"(?m)^\s*\|\s*([^|\n]+?)\s*\|\s*([^|\n]*?)\s*\|?\s*$", blob):
            a = " ".join(m.group(1).split()).strip(" |-")
            b = " ".join(m.group(2).split()).strip(" |-")
            if not a or a.startswith("---") or a.casefold() in _NZ_TABLE_SKIP:
                raw = b
            else:
                raw = a
            if raw and re.search(r"\b(port|marina|harbour|harbor|northport)\b", raw, re.I):
                names.append(raw)
    for raw in names:
        name = " ".join((raw or "").split()).strip(" |-")
        fold = name.casefold()
        if (not name or fold in seen or fold in skip or len(name) < 3
                or len(name) > 80 or fold in _GENERIC_PORT_NAMES
                or fold.startswith("mpi ") or fold in _NZ_TABLE_SKIP):
            continue
        seen.add(fold)
        out.append({
            "name": name[:120], "city": None,
            "note": "place of first arrival (registre MPI)",
            "extraction_engine": "catalog",
        })
    return out


def _extract_annexe_ppc_maritime(text: str) -> list[dict]:
    """Mayotte Annex I: controlled crossing points, maritime borders only."""
    out, seen = [], set()
    skip = _PORT_OF_SKIP | _GENERIC_PORT_NAMES | {
        "sites", "modalités", "modalites", "ouverture", "liste", "documents",
        "permanent", "temporaire",
    }
    for block in _ANNEXE_MARITIME_RE.findall(text or ""):
        for m in _PPC_SITE_RE.finditer(block):
            name = _clean_legal_port_name(m.group(1))
            fold = name.casefold()
            if (not name or fold in seen or fold in skip or len(name) < 3
                    or re.search(r"pamandzi|a[eé]roport|a[eé]rien", name, re.I)):
                continue
            seen.add(fold)
            out.append({
                "name": name[:120], "city": None,
                "note": "point de passage contrôlé (annexe I, frontières maritimes)",
                "extraction_engine": "catalog",
            })
    return out


def _sx_normalize_place(raw: str) -> str | None:
    """Simpsonbay Marina, Greatbay harbor, Cruise Terminal — not the airport."""
    n = " ".join((raw or "").split()).strip(" .;:")
    n = re.sub(r"^(?:including|and)\s+(?:the\s+)?", "", n, flags=re.I).strip()
    if not n or len(n) < 4 or _SX_SKIP_PLACE_RE.search(n):
        return None
    fold = re.sub(r"[^a-z]+", "", n.casefold())
    if fold.startswith("simpson"):
        return "Simpsonbay Marina"
    if fold in {"greatbay", "greatbayharbor", "greatbayharbour"}:
        return "Greatbay harbor"
    if fold in {"cruiseterminal", "thecruiseterminal"}:
        return "Cruise Terminal"
    if re.search(r"\b(marina|harbor|harbour|port|cruise\s+terminal)\b", n, re.I) and len(n) >= 8:
        return n[:120]
    return None


def _eg_normalize_marina(raw: str) -> str | None:
    """SIS title "Hurghada Marina:" — not a town cited in prose."""
    n = " ".join(html.unescape(raw or "").split()).strip(" .;:*")
    n = re.sub(r"^\*+", "", n).strip()
    if not n or len(n) < 8 or len(n) > 80:
        return None
    if not re.search(r"\bmarina\b", n, re.I):
        return None
    if _EG_SKIP_MARINA_RE.search(n) or n.casefold() in _GENERIC_PORT_NAMES:
        return None
    return n[:120]


def _extract_eg_sis_yacht_marinas(text: str) -> list[dict]:
    """SIS list "specialized marinas … including" (non-exhaustive)."""
    out, seen = [], set()
    text = html.unescape(text or "")
    block = text
    m = _EG_SIS_LIST_RE.search(text)
    if m:
        block = m.group(1)
    for raw in _EG_MARINA_HEAD_RE.findall(block):
        name = _eg_normalize_marina(raw)
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append({
            "name": name[:120], "city": None,
            "note": "marina SIS (liste yacht tourism, Égypte — non exhaustive)",
            "extraction_engine": "catalog",
        })
    return out


def _extract_al_porti_detar(text: str) -> list[dict]:
    """Four ports from the Dogana kartelë (fuel excise, anijet e peshkimit)."""
    out, seen = [], set()
    text = html.unescape(text or "")
    for m in _AL_PORTI_DETAR_RE.finditer(text):
        place = " ".join((m.group(1) or "").split()).strip(" .;:-")
        if not place or place.casefold() in _AL_PORTI_SKIP:
            continue
        name = f"Porti detar {place}"
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name[:120], "city": None,
            "note": "port detar (kartelë Dogana, accise anijet e peshkimit)",
            "extraction_engine": "catalog",
        })
    return out


def _extract_sx_customs_examples(text: str) -> list[dict]:
    """Authority places listed by Sint Maarten customs (not the airport)."""
    out, seen = [], set()
    # SharePoint encodes the colon: "examples include&#58;".
    text = html.unescape(text or "")
    blobs = _SX_EXAMPLES_RE.findall(text)
    if not blobs and re.search(r"simpson\s*bay", text, re.I):
        blobs = [text]
    for blob in blobs:
        chunk = re.sub(r"\s+", " ", blob)
        chunk = re.sub(r"\band\b", ",", chunk, flags=re.I)
        for raw in chunk.split(","):
            name = _sx_normalize_place(raw)
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            out.append({
                "name": name[:120], "city": None,
                "note": "lieu d'autorité douanière (Sint Maarten)",
                "extraction_engine": "catalog",
            })
    return out


def _extract_uk_pleasure_ports(text: str) -> list[dict]:
    """If a UK pleasure-craft port list is published, all of them are Ports of Entry."""
    out, seen = [], set()
    skip = _PORT_OF_SKIP | _GENERIC_PORT_NAMES | {
        "united kingdom", "border force", "hmrc", "yachtline",
    }
    for block in _UK_PLEASURE_SECTION_RE.findall(text or ""):
        for raw in re.findall(
            r"(?m)^\s*(?:[-•*]|\d+[.)]|\\|)\s*([A-Z][A-Za-z'’.\-]+"
            r"(?:[\s\-][A-Z][A-Za-z'’.\-]+){0,4})\s*$",
            block,
        ):
            name = " ".join((raw or "").split()).strip(" |-")
            fold = name.casefold()
            if (not name or fold in seen or fold in skip or len(name) < 3
                    or len(name) > 60):
                continue
            seen.add(fold)
            out.append({
                "name": name[:120], "city": None,
                "note": "pleasure craft — port of entry (sPCR / GOV.UK)",
                "extraction_engine": "catalog",
            })
    return out


def _extract_douane_bureaux(text: str) -> list[dict]:
    """"bureau de douane de Nouméa Port" — formalities page without a table."""
    out, seen = [], set()
    for m in _DOUANE_BUREAU_RE.finditer(text or ""):
        name = _clean_legal_port_name(m.group(1))
        fold = name.casefold()
        if (not name or fold in seen or fold in _PORT_OF_SKIP or len(name) < 3
                or _PLAISANCE_NAME_SKIP_RE.search(name)):
            continue
        seen.add(fold)
        out.append({
            "name": name[:120], "city": None,
            "note": "bureau de douane (page formalités)",
            "extraction_engine": "catalog",
        })
    return out


def _extract_structured_ports_one(text: str) -> list[dict]:
    out, seen = [], set()
    for extra in (
        _extract_mx_habilitados_table(text),
        _extract_nz_pofa_tables(text),
        _extract_capitanias(text),
        _extract_fr_plaisance_table(text),
        _extract_annexe_ppc_maritime(text),
        _extract_uk_pleasure_ports(text),
        _extract_sx_customs_examples(text),
        _extract_eg_sis_yacht_marinas(text),
        _extract_al_porti_detar(text),
        _extract_douane_bureaux(text),
    ):
        for p in extra:
            key = p["name"].casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    for m in _CATALOG_HEAD.finditer(text or ""):
        name = " ".join(m.group(1).split()).strip()
        block = m.group(2) or ""
        lat_m, lon_m = _CATALOG_LAT.search(block), _CATALOG_LON.search(block)
        if not name or not lat_m or not lon_m:
            continue
        activity_ok = _mx_activity_is_turistica(block)
        if activity_ok is False:
            continue
        st = _CATALOG_STATE.search(block)
        state = st.group(1).strip() if st else None
        key = name.casefold()
        if key in seen and state:
            name = f"{name} ({state})"
            key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name[:120], "city": state,
            "lat": float(lat_m.group(1)), "lon": float(lon_m.group(1)),
            "note": "catalogue officiel (nom + coordonnées dans la source)",
            "extraction_engine": "catalog",
        })
    if len(out) >= 10:
        return out
    if len(out) >= 3 and looks_like_port_catalog(text or ""):
        return out
    for m in _PORT_OF_RE.finditer(text or ""):
        name = m.group(1).strip().rstrip(".")
        first = name.split()[0].casefold() if name else ""
        if not name or first in _PORT_OF_SKIP or name.casefold() in seen:
            continue
        if len(name) < 3 or len(name) > 60:
            continue
        if re.search(r"\b(airport|aeropuerto|aéroport|zarpe)\b", name, re.I):
            continue
        seen.add(name.casefold())
        out.append({
            "name": name[:120], "city": None,
            "note": "tournure légale (« port of / port de / puerto de … »)",
            "extraction_engine": "catalog",
        })
    return out


def catalog_ports_with_coords(ports: list | None) -> list[dict]:
    """Catalog subset that actually has lat/lon in the source."""
    return [
        p for p in (ports or [])
        if p.get("lat") is not None and p.get("lon") is not None
    ]


def catalog_ports_to_keep(ports: list | None) -> list[dict]:
    """Ports to keep when skipping the LLM.

    An SCT table has coords: keep them. An official list without GPS
    (PPF FR, MPI NZ, SIS EG, kartelë AL, Customs SX) already has names: keep
    them for the geocoder. Returning [] here after the parser read names
    is the "catalog sufficient (0 coordinated) — LLM skipped" bug.
    """
    coords = catalog_ports_with_coords(ports)
    if coords:
        return coords
    return [
        p for p in (ports or [])
        if (p.get("name") or "").strip() and is_geocodeable_name(p.get("name") or "")
    ]


def catalog_is_sufficient(ports: list | None, text: str = "") -> bool:
    """Skip the LLM if the parser already has a usable official list.

    - ≥ 3 ports with lat/lon (SCT-style catalog);
    - or looks_like_port_catalog + ≥ 1 coordinated port;
    - or looks_like_port_catalog + enough names (PPF / MPI / SIS / kartelë).
    Lone "port de X" fragments are not enough (see tests).
    On skip, `catalog_ports_to_keep` returns coords **or** geocodeable names —
    never an empty list if the parser already has ports.
    """
    if not ports:
        return False
    from app.core.run_rules import get_rule
    with_coords = len(catalog_ports_with_coords(ports))
    min_coords = int(get_rule("formalities.catalog_min_coords", 3))
    like_min = int(get_rule("formalities.catalog_looks_like_min_coords", 1))
    if with_coords >= min_coords:
        return True
    if looks_like_port_catalog(text or "") and with_coords >= like_min:
        return True
    named = [p for p in ports if (p.get("name") or "").strip()]
    min_names = int(get_rule("formalities.catalog_min_names", 8))
    if looks_like_port_catalog(text or "") and len(named) >= min_names:
        return True
    cat = [p for p in named if p.get("extraction_engine") == "catalog"]
    return bool(looks_like_port_catalog(text or "") and len(cat) >= 4)


_JUNK_NAME_RE = re.compile(
    r"\b(moet|worden|ingediend|ligt|binnenkomst|uniforme|armes|"
    r"continuously|conducted|described|consists)\b",
    re.I,
)
_GEOCODE_STOP = frozenset({
    "the", "and", "or", "of", "a", "an", "to", "for", "in", "on",
    "de", "het", "van", "een", "is", "la", "le", "les", "du", "des",
    "un", "une", "et", "ou", "el", "los", "las", "y",
})

# Legal entity / port authority — not "port of / port de" (Port of Spain).
_CORPORATE_PORT_PREFIXES = (
    "ports autonomes de ", "ports autonomes d'",
    "port autonome de ", "port autonome d'",
    "autorité portuaire de ", "autorite portuaire de ",
    "autoridad portuaria de ", "port authority of ",
    "office portuaire de ",
)


def geocode_query_name(name: str) -> str:
    """Toponym to geocode: Nouméa, not "Port autonome de Nouméa".

    Do not strip "port of / port de / puerto de" — "Port of Spain"
    would become "Spain".
    """
    n = (name or "").strip()
    low = n.lower()
    for p in _CORPORATE_PORT_PREFIXES:
        if low.startswith(p):
            rest = n[len(p):].strip(" \t-–,")
            return rest or n
    return n


def is_geocodeable_name(name: str, geocodeable=None) -> bool:
    """False → do not call Nominatim/GeoNames (fragment, not a toponym).

    `geocodeable is False` (LLM flag) always wins. A local net rejects
    NL/CI canary-style sentences even if the flag is missing or True.
    The legal entity "Port autonome de …" is judged on the listing alias.
    """
    if geocodeable is False:
        return False
    n = geocode_query_name(name or "")
    if len(n) < 2 or len(n) > 80:
        return False
    if _JUNK_NAME_RE.search(n):
        return False
    tokens = re.findall(r"[^\W\d_]+", n, flags=re.UNICODE)
    if not tokens:
        return False
    if all(t.casefold() in _GEOCODE_STOP for t in tokens):
        return False
    caps = sum(1 for t in tokens if t[:1].isupper())
    if len(tokens) >= 3 and caps == 0:
        return False
    return True


def _pdf_path(url: str) -> str:
    return unquote(urlparse(url or "").path or "")


def _pdf_stem(url: str) -> str:
    leaf = Path(_pdf_path(url)).name
    return leaf.rsplit(".", 1)[0].casefold()


def _pdf_recency_bonus(path: str) -> int:
    """Prefer the current vintage over a 2022 PPF map still linked."""
    years = [2000 + int(y) for y in _YEAR_IN_PATH_RE.findall(path or "")]
    if years:
        return max(0, max(years) - 2020)
    if "/uploads/" in (path or "").lower():
        return 4
    return 0


def _pdf_list_score(url: str) -> int:
    """Score the PDF path, never the hostname (douane.gouv.fr ≠ a list)."""
    path = _pdf_path(url)
    if not path:
        return 0
    score = 0
    if _LIST_PDF_PATH_RE.search(path):
        score += 2
    low = path.lower()
    for tok, pts in (
        ("plaisance", 3), ("eligibles", 2), ("ppf", 2), ("liste", 2),
        ("habilit", 2), ("ports-entree", 2), ("port-of-entry", 2),
    ):
        if tok in low:
            score += pts
    if _JUNK_PDF_PATH_RE.search(path):
        score -= 4
    if score > 0:
        score += _pdf_recency_bonus(path)
    return score


def official_attachments(text: str, base_url: str, limit: int = 4) -> list[str]:
    """Official PDFs linked from a state page (list attachment, decree).

    Keep only PDFs whose *path* looks like a list. A PDF
    "10-questions-before-exporting-en.pdf" on douane.gouv.fr is not one.
    """
    if not text or not base_url:
        return []
    raw: list[str] = list(_PDF_ABS_RE.findall(text))
    raw += [urljoin(base_url, h) for h in _PDF_HREF_RE.findall(text)]
    raw += [urljoin(base_url, h) for h in _PDF_MD_RE.findall(text)]
    base_host = (urlparse(base_url).hostname or "").lower()
    scored, seen = [], set()
    for u in raw:
        u = (u or "").split("#")[0]
        if not u.startswith("http") or u in seen or _is_mirror_url(u):
            continue
        if SERP_HARD_RE.search(u):
            continue
        host = (urlparse(u).hostname or "").lower()
        score = _pdf_list_score(u)
        if host != base_host and score <= 0:
            continue
        if score <= 0:
            continue
        seen.add(u)
        scored.append((-score, u))
    scored.sort()
    out, stems = [], set()
    for _, u in scored:
        stem = _pdf_stem(u)
        if stem in stems:
            continue
        stems.add(stem)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def should_follow_attachments(text: str, url: str) -> bool:
    """Follow a PDF even if the page is already long (SCT lesson: the list
    is often the linked file, not the HTML)."""
    blob = (text or "")[:4000]
    path = _pdf_path(url)
    if (looks_like_port_catalog(text or "")
            or bool(_CATALOG_MARKERS_RE.search(blob))
            or bool(_LIST_PDF_PATH_RE.search(path))):
        return True
    # Page without a catalog marker, but a "list / PPF" PDF is already linked.
    return bool(url and official_attachments(text or "", url, limit=1))


async def _fetch_bytes(url: str, headers: dict, timeout: float):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        r = await fetch_checked(client, url)
        r.raise_for_status()
        return r.content, (r.headers.get("content-type") or "").lower()


async def _wayback_snapshot_url(url: str) -> str | None:
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, headers=UA_BROWSER) as client:
        r = await fetch_checked(client, "https://web.archive.org/cdx/search/cdx", params={
            "url": url, "output": "json", "filter": "statuscode:200",
            "fl": "timestamp,original", "limit": 1,
        })
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list) or len(rows) < 2:
            return None
        ts, orig = rows[1][0], rows[1][1]
        return f"https://web.archive.org/web/{ts}id_/{orig}"


def _mirror_http_status(exc: Exception) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


def _mirror_http_err(exc: Exception) -> str:
    code = _mirror_http_status(exc)
    if code is not None:
        return f"HTTP {code}"
    return type(exc).__name__


def _mirror_usable(text: str | None) -> bool:
    if not text or len(text) < 400:
        return False
    if looks_blocked(text) or looks_hard_challenge(text=text, html=text):
        return False
    return True


def _append_fetch_links(text: str, links: list | None) -> str:
    extra = [u for u in (links or []) if isinstance(u, str) and u.startswith("http")]
    if not extra:
        return text
    return (text or "") + "\n" + "\n".join(extra[:20])


def _arbitrate_mirror_texts(jina_text: str | None, tf_text: str | None) -> tuple[str, str, dict] | None:
    """Jina ∥ TinyFish Fetch: challenge dropped, catalog wins, else the longest."""
    j_ok = _mirror_usable(jina_text)
    t_ok = _mirror_usable(tf_text)
    sim = _parse_similarity(jina_text or "", tf_text or "") if (j_ok and t_ok) else None
    compare = {
        "jina_chars": len(jina_text or ""),
        "tf_chars": len(tf_text or ""),
        "similarity": sim,
        "winner": None,
        "catalog": None,
    }
    if j_ok and t_ok:
        j_cat = looks_like_port_catalog(jina_text or "")
        t_cat = looks_like_port_catalog(tf_text or "")
        compare["catalog"] = {"jina": j_cat, "tinyfish": t_cat}
        if j_cat != t_cat:
            winner, text = ("tinyfish", tf_text) if t_cat else ("jina", jina_text)
        elif sim is not None and sim < _extract_agree_sim() and (j_cat or t_cat):
            winner, text = ("tinyfish", tf_text) if t_cat else ("jina", jina_text)
        else:
            winner, text = (("tinyfish", tf_text)
                            if len(tf_text or "") >= len(jina_text or "")
                            else ("jina", jina_text))
        compare["winner"] = winner
        level = "N3-mirror-tinyfish" if winner == "tinyfish" else "N3-mirror-jina"
        return text, level, compare
    if t_ok:
        compare["winner"] = "tinyfish"
        return tf_text, "N3-mirror-tinyfish", compare
    if j_ok:
        compare["winner"] = "jina"
        return jina_text, "N3-mirror-jina", compare
    return None


async def _jina_mirror_text(url: str, log) -> str | None:
    for attempt in range(2):
        try:
            content, _ = await _fetch_bytes(f"https://r.jina.ai/{url}", UA_READER, 90)
            text = content.decode("utf-8", errors="replace")
            if _mirror_usable(text):
                log(f"miroir Jina: {len(text)} chars")
                return text
            if text:
                log(f"miroir Jina: texte inutilisable ({len(text)} chars)")
        except Exception as e:
            log(f"miroir Jina: indisponible ({_mirror_http_err(e)})")
            if _mirror_http_status(e) == 422:
                return None
            if attempt == 0:
                await asyncio.sleep(1.2)
    return None


async def _tinyfish_mirror_text(url: str, log) -> tuple[str | None, list]:
    from app.core.tinyfish import tf_api_key, tf_fetch
    key = tf_api_key()
    if not key:
        return None, []
    try:
        recs = await tf_fetch([url], key, ttl=0, links=True, log=log)
    except Exception as e:
        log(f"miroir TinyFish: échec ({type(e).__name__})")
        return None, []
    rec = recs.get(url) or {}
    if rec.get("blocked"):
        log("miroir TinyFish: bot_blocked — contenu invalidé")
        return None, []
    text = rec.get("text") or ""
    links = rec.get("links") or []
    text = _append_fetch_links(text, links)
    if not _mirror_usable(text):
        return None, links
    log(f"miroir TinyFish: {len(text)} chars")
    return text, links


async def _jorf_reader_text(url: str, log) -> str | None:
    """Copies of the same JORF text when Légifrance returns a Cloudflare challenge."""
    blob = (url or "").upper()
    for key, mirrors in _JORF_READERS.items():
        if key not in blob:
            continue
        for mu in mirrors:
            try:
                content, ctype = await _fetch_bytes(mu, UA_BROWSER, 45)
                if content[:5] == b"%PDF-" or "pdf" in (ctype or ""):
                    text = await asyncio.to_thread(parse_pdf_text, content)
                else:
                    parsed = await dual_parse_html(
                        content.decode("utf-8", errors="replace"))
                    text = parsed.get("text") or ""
                if _mirror_usable(text):
                    log(f"miroir JORF {key}: {len(text)} chars")
                    return text
            except Exception as e:
                log(f"miroir JORF {key}: indisponible ({_mirror_http_err(e)})")
    return None


async def _wayback_mirror_text(url: str, log) -> str | None:
    if wayback_circuit_open():
        log("miroir Wayback: en pause (503 répétés)")
        return None
    try:
        snap = await _wayback_snapshot_url(url)
        if snap:
            content, ctype = await _fetch_bytes(snap, UA_BROWSER, 45)
            if content[:5] == b"%PDF-" or "pdf" in ctype:
                text = await asyncio.to_thread(parse_pdf_text, content)
            else:
                parsed = await dual_parse_html(content.decode("utf-8", errors="replace"))
                text = parsed.get("text") or ""
            if _mirror_usable(text):
                _note_wayback_ok()
                log(f"miroir Wayback: {len(text)} chars")
                return text
    except Exception as e:
        if _mirror_http_status(e) == 503:
            _note_wayback_503()
        log(f"miroir Wayback: indisponible ({_mirror_http_err(e)})")
    return None


async def fetch_mirror_text(url: str, log=None, skip_tinyfish: bool = False
                            ) -> tuple[str, str] | None:
    """Generic mirror when the official page is behind a challenge.
    Jina ∥ TinyFish Fetch (if key), then Wayback. The original URL stays
    the source; the mirror is only a reader.

    skip_tinyfish: Fetch was already tried upstream (prefer_fetch) — do not
    pay for it a second time.

    Return (text, level) — optional compare as 3rd element if arbitrated."""
    log = log or (lambda m: None)
    if _is_mirror_url(url):
        return None

    from app.core.tinyfish import tf_api_key
    use_tf = (not skip_tinyfish) and allow_tinyfish_fetch.get() and bool(tf_api_key())
    if use_tf:
        jina_text, tf_pair = await asyncio.gather(
            _jina_mirror_text(url, log),
            _tinyfish_mirror_text(url, log),
        )
        tf_text, _links = tf_pair if isinstance(tf_pair, tuple) else (None, [])
        picked = _arbitrate_mirror_texts(jina_text, tf_text)
        if picked:
            return picked
    else:
        jina_text = await _jina_mirror_text(url, log)
        if _mirror_usable(jina_text):
            return jina_text, "N3-mirror-jina"

    wb = await _wayback_mirror_text(url, log)
    if wb:
        return wb, "N3-mirror-wayback"
    if "legifrance.gouv.fr" in (url or "").lower():
        jorf = await _jorf_reader_text(url, log)
        if jorf:
            return jorf, "N3-mirror-jorf"
    return None


async def extract_cascade(url: str, min_chars: int = 200, allow_render: bool = True,
                          log=None, skip_fetch_mirror: bool = False,
                          keep_raw: bool = False) -> dict:
    """
    Local reading engine. The public door is ``read_url``.

    Return {url, text, md5, level, title, meta_desc, image, ext_links, links,
            is_pdf, html, blocked, render_used, parse, final_url, error}.
    level ∈ N1-pymupdf | N1-trafilatura | N2-readability | N3-render | blocked | failed
            | N3-mirror-* .
    md5 = fingerprint of extracted text (stable) else of raw content.
    blocked = anti-bot interstitial detected (content invalidated, never ingested).
    parse = {n1_chars, n2_chars, similarity, agree} — compared dual parsing.
    skip_fetch_mirror: Fetch already tried — Jina / Wayback only.
    """
    log = log or (lambda m: None)
    out = empty_page(url)

    if host_is_retired(url):
        log(f"hôte retiré (DNS mort) — cascade sautée ({url_host(url)})")
        out["error"] = "dead_host"
        return out

    if is_maps_render_url(url):
        log("URL Google Maps — cascade locale sautée (DOM JS, Fetch seulement)")
        out["error"] = "maps_render_url"
        return out

    if url_looks_like_image(url):
        log("URL image — cascade sautée (pas une fiche, pas de Chromium)")
        out["error"] = "image_url"
        return out

    content = None
    ctype = ""
    disposition = ""
    resp = None
    try:
        resp = await fetch_raw(url)
        content = resp.content
        ctype = (resp.headers.get("content-type") or "").lower()
        disposition = resp.headers.get("content-disposition") or ""
        out["final_url"] = str(resp.url) or url
        out["disposition"] = disposition
        out["download"] = "attachment" in disposition.lower()
    except Exception as e:
        log(f"N1 fetch: échec ({type(e).__name__})")
        out["fetch_failed"] = True
        out["error"] = type(e).__name__

    if content is not None:
        out["md5"] = hashlib.md5(content).hexdigest()
        if content_is_image(content, ctype, out["final_url"] or url):
            log("contenu image — cascade sautée, pas de Chromium")
            out["error"] = "image_content"
            return out
        is_pdf = content_is_pdf(content, ctype, out["final_url"] or url, disposition)
        if not is_pdf and not content_looks_like_html(content, ctype):
            log("contenu binaire — cascade sautée, pas de Chromium")
            out["error"] = "binary_content"
            return out
        out["is_pdf"] = is_pdf
        if keep_raw and content:
            out["raw"] = content
        if is_pdf:
            try:
                text = await asyncio.to_thread(parse_pdf_text, content)
                out["text"] = (text or "").strip()
                if out["text"]:
                    out["level"] = "N1-pymupdf"
            except Exception as e:
                log(f"N1 PyMuPDF: échec ({type(e).__name__})")
        else:
            try:
                html = resp.text if resp is not None else content.decode("utf-8", errors="replace")
            except Exception:
                html = content.decode("utf-8", errors="replace")
            out["html"] = html
            out["links"] = html_hrefs(html, out["final_url"] or url)
            try:
                meta = await asyncio.to_thread(page_metadata, html, url)
                out.update({k: meta[k] for k in ("title", "meta_desc", "image", "ext_links")})
            except Exception:
                pass
            parsed = await dual_parse_html(html)
            out["parse"] = {k: parsed[k] for k in ("n1_chars", "n2_chars", "similarity", "agree")}
            out["title"] = out["title"] or parsed["title"]
            if looks_blocked(parsed["text"], html, out["title"]):
                out["blocked"] = True
                log("page interstitielle anti-bot détectée — contenu invalidé")
            elif len(parsed["text"]) >= min_chars:
                out["text"] = parsed["text"]
                out["level"] = parsed["level"]
            elif parsed["text"]:
                out["text"] = parsed["text"]

    # N3 — local browser render (free): JS pages and "soft" challenges.
    # A hard challenge (Akamai, SiteGround, Cloudflare) is never solved by
    # Chromium: fall through to the mirror (TinyFish Fetch passes SiteGround; Jina does not).
    # No Chromium on a PDF — nor on a .pdf URL that returned HTML.
    if resp is not None and looks_bot_challenge_response(
            resp, content, out.get("final_url") or url):
        out["blocked"] = True
        log("captcha anti-bot — Chromium sauté, tentative de miroir")
    pdf_url = url_looks_like_pdf(url) or url_looks_like_pdf(out.get("final_url"))
    hard = looks_hard_challenge(
        out.get("text") or "", out.get("html") or "", out.get("title") or "")
    skip_chromium = bool(
        out["is_pdf"] or pdf_url or hard or out["blocked"]
        or out.get("fetch_failed") or out.get("download")
        or url_looks_like_download(url, out.get("disposition") or ""))
    short = len(out["text"]) < min_chars
    # 205 chars of captcha page must not prevent TinyFish Fetch (≥ 400).
    want_mirror = bool(out["blocked"] or short or (pdf_url and not out["is_pdf"] and len(out["text"]) < 400))
    needs_render = (not skip_chromium) and allow_render and (out["blocked"] or short)
    if skip_chromium and want_mirror and (hard or out["blocked"] or pdf_url):
        if pdf_url and not out["is_pdf"]:
            log("URL .pdf sans octets PDF — Chromium sauté, tentative de miroir")
        elif hard or out["blocked"]:
            log("challenge anti-bot dur — rendu Chromium sauté, tentative de miroir")
    if needs_render:
        from app.core.render import render_html
        rendered = await render_html(url, log=log)
        if rendered:
            out["render_used"] = True
            parsed = await dual_parse_html(rendered)
            r_blocked = looks_blocked(parsed["text"], rendered, parsed["title"])
            if not r_blocked and len(parsed["text"]) > len(out["text"]):
                out["text"] = parsed["text"]
                out["html"] = rendered
                out["title"] = out["title"] or parsed["title"]
                out["parse"] = {k: parsed[k] for k in ("n1_chars", "n2_chars", "similarity", "agree")}
                out["level"] = "N3-render"
                out["blocked"] = False
                out["links"] = html_hrefs(rendered, out["final_url"] or url) or out["links"]
                try:
                    meta = await asyncio.to_thread(page_metadata, rendered, url)
                    for k in ("meta_desc", "image", "ext_links"):
                        out[k] = out[k] or meta[k]
                except Exception:
                    pass
                log(f"N3-render: {len(out['text'])} chars via Chromium local")
            elif r_blocked:
                out["blocked"] = True

    # Generic mirror: official page blocked, too short, or captcha .pdf
    # (205 Chromium chars must not short-circuit TinyFish Fetch).
    if want_mirror and not _is_mirror_url(url):
        mirrored = await fetch_mirror_text(url, log=log, skip_tinyfish=skip_fetch_mirror)
        if mirrored:
            text, level = mirrored[0], mirrored[1]
            out["text"] = text
            out["level"] = level
            out["blocked"] = False
            out["html"] = text if text.lstrip().startswith(("#", "Title:")) else out.get("html")
            if len(mirrored) > 2 and isinstance(mirrored[2], dict):
                out["fetch_compare"] = mirrored[2]

    if out["blocked"]:
        out["text"] = ""
        out["level"] = "blocked"
        return out

    if out["text"]:
        out["md5"] = hashlib.md5(out["text"].encode("utf-8")).hexdigest()
        if out["level"] == "failed":
            out["level"] = "N2-readability"
    return out


async def complete_with_cascade(
    urls: list[str],
    fetched: dict | None,
    *,
    min_chars: int = 200,
    allow_render: bool = True,
    keep_if_links: bool = False,
    log=None,
    concurrency: int = 4,
) -> dict[str, dict]:
    """Fill empty / blocked Fetch records via the local cascade.

    Google Maps URLs stay as-is (Fetch-only). An already usable Fetch
    does not start Chromium. Return Fetch-shaped records.
    """
    log = log or (lambda m: None)
    out = dict(fetched or {})
    need: list[str] = []
    for u in urls or []:
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        if is_maps_render_url(u):
            continue
        if fetch_record_usable(out.get(u), min_chars, keep_if_links=keep_if_links):
            continue
        need.append(u)
    if not need:
        return out

    skip_tf = True  # Fetch already tried (even if it failed)
    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))

    async def _one(u: str):
        async with sem:
            try:
                page = await extract_cascade(
                    u, min_chars=min_chars, allow_render=allow_render,
                    log=log, skip_fetch_mirror=skip_tf)
            except Exception as e:
                log(f"cascade {u[:80]}: {type(e).__name__}")
                page = empty_page(u, error=type(e).__name__)
            return u, page

    done = await asyncio.gather(*[_one(u) for u in need])
    for u, page in done:
        if text_usable(page.get("text"), min_chars) and not page.get("blocked"):
            out[u] = as_fetch_record(page, u)
            log(f"cascade {u[:80]}: {len(page.get('text') or '')} chars via {page.get('level')}")
        elif u not in out:
            out[u] = as_fetch_record(page, u)
        elif page.get("text") and not page.get("blocked"):
            # Fetch was too short / empty: take the better text.
            prev = out.get(u) or {}
            if len(page.get("text") or "") > len(prev.get("text") or ""):
                out[u] = as_fetch_record(page, u)
    return out


async def read_urls(
    urls: list[str] | None,
    *,
    min_chars: int = 200,
    prefer_fetch: bool = False,
    allow_render: bool = True,
    fetch_purpose: str | None = None,
    fetch_key: str | None = None,
    keep_if_links: bool = False,
    log=None,
    concurrency: int = 4,
) -> dict[str, dict]:
    """Single door: text (and links) of a URL list.

    prefer_fetch=True: TinyFish Fetch first (batches of 10). If the text is
    not usable — empty PDF, JS page — enter extract_cascade.
    Chromium only runs if simple HTML AND Fetch have already failed.

    Google Maps URL: Fetch only, never the cascade.
    prefer_fetch=False: local cascade (Projects, top-down).
    """
    log = log or (lambda m: None)
    clean, seen = [], set()
    for u in urls or []:
        if isinstance(u, str) and u.startswith("http") and u not in seen:
            seen.add(u)
            clean.append(u)
    if not clean:
        return {}

    maps = [u for u in clean if is_maps_render_url(u)]
    rest = [u for u in clean if u not in set(maps)]
    out: dict[str, dict] = {}

    from app.core.tinyfish import tf_api_key, tf_fetch
    if not allow_tinyfish_fetch.get():
        key = ""
    elif fetch_key is not None:
        key = (fetch_key or "").strip()
    else:
        key = tf_api_key()

    async def _tf(batch: list[str]) -> dict:
        if not key or not batch:
            return {}
        try:
            return await tf_fetch(batch, key, links=True, purpose=fetch_purpose, log=log) or {}
        except Exception as e:
            log(f"read_urls Fetch: {type(e).__name__}")
            return {}

    if maps:
        recs = await _tf(maps) if key else {}
        for u in maps:
            out[u] = page_from_fetch_record(u, recs.get(u) or {})
            if not key:
                out[u]["error"] = out[u].get("error") or "maps_fetch_only"

    already_fetched = False
    need_cascade: list[str] = []
    if prefer_fetch and rest and key:
        recs = await _tf(rest)
        already_fetched = True
        for u in rest:
            rec = recs.get(u) or {}
            if fetch_record_usable(rec, min_chars, keep_if_links=keep_if_links):
                out[u] = page_from_fetch_record(u, rec)
            else:
                need_cascade.append(u)
    else:
        need_cascade = rest

    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))

    async def _one(u: str):
        async with sem:
            try:
                page = await extract_cascade(
                    u, min_chars=min_chars, allow_render=allow_render,
                    log=log, skip_fetch_mirror=already_fetched)
            except Exception as e:
                log(f"read_urls cascade {u[:80]}: {type(e).__name__}")
                page = empty_page(u, error=type(e).__name__)
            return u, page

    if need_cascade:
        done = await asyncio.gather(*[_one(u) for u in need_cascade])
        for u, page in done:
            out[u] = page
    return out


async def read_url(
    url: str,
    *,
    min_chars: int = 200,
    prefer_fetch: bool = False,
    allow_render: bool = True,
    fetch_purpose: str | None = None,
    fetch_key: str | None = None,
    keep_if_links: bool = False,
    log=None,
) -> dict:
    """Single door for one URL. See ``read_urls``."""
    pages = await read_urls(
        [url], min_chars=min_chars, prefer_fetch=prefer_fetch,
        allow_render=allow_render, fetch_purpose=fetch_purpose,
        fetch_key=fetch_key, keep_if_links=keep_if_links, log=log)
    return pages.get(url) or empty_page(url)
