"""Single door read_url / extract_cascade — no network."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import extract as ext  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _no_tavily_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


class _FakeResp:
    def __init__(self, content: bytes, content_type="text/html",
                 disposition="", url="https://example.gov/x", status_code=200):
        self.content = content
        self.status_code = status_code
        self.headers = {
            "content-type": content_type,
            "content-disposition": disposition,
        }
        self.url = url

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")


class TestPdfUrlAndCaptcha:
    def test_url_looks_like_pdf(self):
        assert ext.url_looks_like_pdf(
            "https://blueactionfund.org/wp-content/uploads/a.pdf") is True
        assert ext.url_looks_like_pdf("https://example.org/projects/") is False

    def test_url_looks_like_image(self):
        assert ext.url_looks_like_image(
            "https://marviva.net/wp-content/uploads/2026/05/Sandra-Vilardy.png") is True
        assert ext.url_looks_like_image(
            "https://arcticnet.ca/wp-content/uploads/2025/10/SSF-Funding-from-Canada.png") is True
        assert ext.path_looks_like_image("/photos/team.jpg") is True
        assert ext.url_looks_like_image("https://example.org/projects/coral") is False

    def test_serp_hard_drops_image_urls(self):
        assert ext.serp_drop_reason(
            "https://marviva.net/wp-content/uploads/2025/12/Mabelys-Ramos-1.png"
        ) == "hard"
        assert ext.serp_drop_reason(
            "https://example.org/projects/coral-reef") is None

    def test_siteground_is_hard_challenge(self):
        html = "<html><body>Checking the site connection security. sg-captcha</body></html>"
        assert ext.looks_hard_challenge(html=html, title="Robot Challenge Screen") is True
        assert ext.looks_blocked("Robot Challenge Screen", html=html,
                                 title="Robot Challenge Screen") is True

    def test_jina_captcha_mirror_rejected(self):
        blob = (
            "Title: Robot Challenge Screen\n\n"
            "Warning: This page maybe requiring CAPTCHA\n\n"
            "Checking the site connection security\n"
        )
        assert ext._mirror_usable(blob) is False
        assert ext._mirror_usable("GRANT FACT SHEET " * 40) is True

    def test_sg_header_is_challenge(self):
        resp = _FakeResp(b"<html>hi</html>", url="https://x.org/a.pdf", status_code=202)
        resp.headers["sg-captcha"] = "challenge"
        assert ext.looks_bot_challenge_response(resp, resp.content, resp.url) is True


class TestContentIsPdf:
    def test_magic_bytes(self):
        assert ext.content_is_pdf(b"%PDF-1.4 rest", "text/html", "https://x") is True

    def test_html_at_pdf_url_is_not_pdf(self):
        html = b"<!DOCTYPE html><html><body>error</body></html>"
        assert ext.content_is_pdf(
            html, "text/html", "https://gov.example/decreto.pdf") is False

    def test_content_disposition(self):
        assert ext.content_is_pdf(
            b"%PDF-1.7", "application/octet-stream",
            "https://gov.example/download",
            'attachment; filename="lista.pdf"') is True

    def test_html_not_pdf_despite_disposition_name(self):
        html = b"<html><p>not a pdf</p></html>"
        assert ext.content_is_pdf(
            html, "text/html", "https://gov.example/x",
            'attachment; filename="lista.pdf"') is False


class TestContentIsImage:
    def test_png_magic(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        assert ext.content_is_image(png, "text/html", "https://x") is True

    def test_jpeg_magic(self):
        assert ext.content_is_image(b"\xff\xd8\xff\xe0rest", "application/octet-stream") is True

    def test_html_at_png_url_is_not_image(self):
        html = b"<!DOCTYPE html><html><body>error</body></html>"
        assert ext.content_is_image(
            html, "text/html", "https://cdn.example/portrait.png") is False

    def test_content_type_image(self):
        assert ext.content_is_image(b"xxxx", "image/png") is True


class TestContentLooksLikeHtml:
    def test_html_and_png(self):
        assert ext.content_looks_like_html(
            b"<!DOCTYPE html><html></html>", "text/html") is True
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        assert ext.content_looks_like_html(png, "image/png") is False
        assert ext.content_looks_like_html(b"\x00\x01\x02\xff", "application/octet-stream") is False


class TestMapsUrl:
    def test_place_and_search(self):
        assert ext.is_maps_render_url(
            "https://www.google.com/maps/place/Port+des+Minimes/") is True
        assert ext.is_maps_render_url(
            "https://www.google.com/maps/search/?api=1&query=Marina+46,-1") is True
        assert ext.is_maps_render_url("https://douane.gouv.fr/ports") is False


class TestFetchUsable:
    def test_blocked_not_usable(self):
        rec = {"text": "Just a moment", "blocked": True}
        assert ext.fetch_record_usable(rec, min_chars=10) is False

    def test_keep_if_links(self):
        rec = {"text": "", "links": ["https://parc.fr/visite"]}
        assert ext.fetch_record_usable(rec, min_chars=200) is False
        assert ext.fetch_record_usable(rec, min_chars=200, keep_if_links=True) is True

    def test_long_text(self):
        rec = {"text": "Ports of entry " * 40}
        assert ext.fetch_record_usable(rec, min_chars=200) is True


class TestReadUrls:
    def test_fetch_success_skips_cascade(self, monkeypatch):
        called = []

        async def fake_tf(urls, key, **k):
            return {u: {"text": "Official designated ports of entry. " * 20,
                        "title": "List", "links": []} for u in urls}

        async def boom_cascade(url, **k):
            called.append(url)
            raise AssertionError("cascade must not run on usable Fetch")

        monkeypatch.setattr("app.core.tinyfish.tf_fetch", fake_tf)
        monkeypatch.setattr("app.core.tinyfish.tf_api_key", lambda settings=None: "k")
        monkeypatch.setattr(ext, "extract_cascade", boom_cascade)

        page = _run(ext.read_url(
            "https://douane.gouv.fr/decreto.pdf",
            prefer_fetch=True, fetch_key="k", min_chars=200))
        assert called == []
        assert page["level"] == "N3-fetch"
        assert "designated ports" in page["text"]
        assert page.get("render_used") is False

    def test_empty_fetch_falls_back_to_cascade(self, monkeypatch):
        seen = []

        async def empty_tf(urls, key, **k):
            return {u: {"text": "", "error": "empty"} for u in urls}

        async def fake_cascade(url, **k):
            seen.append(k.get("skip_fetch_mirror"))
            page = ext.empty_page(url, level="N1-pymupdf")
            page["text"] = "1.- Puerto X\nLatitud:10.1\nLongitud:-20.2\n" * 8
            return page

        monkeypatch.setattr("app.core.tinyfish.tf_fetch", empty_tf)
        monkeypatch.setattr(ext, "extract_cascade", fake_cascade)

        page = _run(ext.read_url(
            "https://dof.gob.mx/nota.pdf", prefer_fetch=True, fetch_key="k"))
        assert seen == [True]
        assert page["level"] == "N1-pymupdf"
        assert "Puerto X" in page["text"]

    def test_maps_never_cascades(self, monkeypatch):
        called = []
        maps = "https://www.google.com/maps/place/Port+des+Minimes/"

        async def fake_tf(urls, key, **k):
            return {u: {"text": "Maps pin DOM", "links": [maps]} for u in urls}

        async def boom_cascade(url, **k):
            called.append(url)
            return ext.empty_page(url)

        monkeypatch.setattr("app.core.tinyfish.tf_fetch", fake_tf)
        monkeypatch.setattr(ext, "extract_cascade", boom_cascade)

        page = _run(ext.read_url(maps, prefer_fetch=True, fetch_key="k"))
        assert called == []
        assert page["level"] == "N3-fetch"
        assert page["links"]

    def test_extract_cascade_skips_maps(self):
        page = _run(ext.extract_cascade(
            "https://www.google.com/maps/place/Foo/"))
        assert page["error"] == "maps_render_url"
        assert page["text"] == ""
        assert page["render_used"] is False

    def test_html_at_pdf_url_parsed_as_html(self, monkeypatch):
        body = ("<html><body><p>" + ("port of entry Nouméa " * 40)
                + "</p></body></html>")

        async def fake_raw(url, timeout=25):
            return _FakeResp(body.encode(), "text/html",
                             url="https://gob.mx/nota.pdf")

        async def no_render(url, log=None):
            raise AssertionError("no chromium")

        async def no_mirror(url, log=None, skip_tinyfish=False):
            return None

        monkeypatch.setattr(ext, "fetch_raw", fake_raw)
        monkeypatch.setattr("app.core.render.render_html", no_render)
        monkeypatch.setattr(ext, "fetch_mirror_text", no_mirror)

        page = _run(ext.extract_cascade("https://gob.mx/nota.pdf", min_chars=200))
        assert page["is_pdf"] is False
        assert len(page["text"]) >= 200
        assert page["level"] in ("N1-trafilatura", "N2-readability")

    def test_captcha_at_pdf_url_skips_chromium_uses_mirror(self, monkeypatch):
        html = (
            "<html><head><title>Robot Challenge Screen</title></head>"
            "<body>Checking the site connection security. sg-captcha</body></html>"
        )
        rendered = []
        mirrors = []

        async def fake_raw(url, timeout=25):
            return _FakeResp(
                html.encode(), "text/html",
                url="https://www.blueactionfund.org/wp-content/uploads/x.pdf",
                status_code=202)

        async def boom_render(url, log=None):
            rendered.append(url)
            raise AssertionError("no chromium on captcha pdf")

        async def fake_mirror(url, log=None, skip_tinyfish=False):
            mirrors.append(url)
            return ("GRANT FACT SHEET Strengthening MPAs " * 20, "N3-mirror-tinyfish")

        monkeypatch.setattr(ext, "fetch_raw", fake_raw)
        monkeypatch.setattr("app.core.render.render_html", boom_render)
        monkeypatch.setattr(ext, "fetch_mirror_text", fake_mirror)

        page = _run(ext.extract_cascade(
            "https://www.blueactionfund.org/wp-content/uploads/x.pdf",
            min_chars=200))
        assert rendered == []
        assert mirrors == [
            "https://www.blueactionfund.org/wp-content/uploads/x.pdf"]
        assert page["render_used"] is False
        assert page["level"] == "N3-mirror-tinyfish"
        assert "GRANT FACT SHEET" in page["text"]
        assert page["blocked"] is False

    def test_short_html_at_pdf_url_still_mirrors(self, monkeypatch):
        """205 chars of HTML (N1 threshold) must not block TinyFish."""
        html = "<html><body>" + ("lorem ipsum " * 18) + "</body></html>"
        rendered = []
        mirrors = []

        async def fake_raw(url, timeout=25):
            return _FakeResp(html.encode(), "text/html",
                             url="https://cdn.example/fact.pdf")

        async def boom_render(url, log=None):
            rendered.append(url)
            raise AssertionError("no chromium on .pdf url")

        async def fake_mirror(url, log=None, skip_tinyfish=False):
            mirrors.append(url)
            return ("Blue Action Fund grant fact sheet " * 25, "N3-mirror-tinyfish")

        monkeypatch.setattr(ext, "fetch_raw", fake_raw)
        monkeypatch.setattr("app.core.render.render_html", boom_render)
        monkeypatch.setattr(ext, "fetch_mirror_text", fake_mirror)

        page = _run(ext.extract_cascade("https://cdn.example/fact.pdf", min_chars=200))
        assert rendered == []
        assert mirrors
        assert page["level"] == "N3-mirror-tinyfish"

    def test_pdf_magic_uses_pymupdf(self, monkeypatch):
        async def fake_raw(url, timeout=25):
            return _FakeResp(
                b"%PDF-1.4 fake", "application/pdf",
                disposition='attachment; filename="decreto.pdf"',
                url="https://gob.mx/download")

        monkeypatch.setattr(ext, "fetch_raw", fake_raw)
        monkeypatch.setattr(
            ext, "parse_pdf_text",
            lambda content, max_pages=180: "Articulo 1 puertos habilitados. " * 20)

        async def no_mirror(url, log=None, skip_tinyfish=False):
            return None

        monkeypatch.setattr(ext, "fetch_mirror_text", no_mirror)

        page = _run(ext.extract_cascade("https://gob.mx/download", min_chars=80))
        assert page["is_pdf"] is True
        assert page["level"] == "N1-pymupdf"
        assert page["render_used"] is False

    def test_skip_fetch_mirror_skips_tinyfish(self, monkeypatch):
        tf_called = []

        async def fake_tf(url, log):
            tf_called.append(url)
            return "tinyfish catalog " * 80, []

        async def fake_jina(url, log):
            return None

        async def fake_wb(url, log):
            return None

        monkeypatch.setattr(ext, "_tinyfish_mirror_text", fake_tf)
        monkeypatch.setattr(ext, "_jina_mirror_text", fake_jina)
        monkeypatch.setattr(ext, "_wayback_mirror_text", fake_wb)
        monkeypatch.setattr("app.core.tinyfish.tf_api_key", lambda settings=None: "k")

        _run(ext.fetch_mirror_text("https://aduana.gob.mx/list", skip_tinyfish=True))
        assert tf_called == []

    def test_complete_with_cascade_replaces_empty_fetch(self, monkeypatch):
        url = "https://gob.mx/decreto.pdf"
        fetched = {url: {"text": "", "error": "empty"}}

        async def fake_cascade(u, **k):
            page = ext.empty_page(u, level="N1-pymupdf")
            page["text"] = "Decreto oficial " * 30
            return page

        monkeypatch.setattr(ext, "extract_cascade", fake_cascade)
        out = _run(ext.complete_with_cascade([url], fetched, min_chars=200))
        rec = out[url]
        assert "Decreto oficial" in rec["text"]
        assert rec["level"] == "N1-pymupdf"

    def test_complete_with_cascade_keeps_maps(self, monkeypatch):
        maps = "https://www.google.com/maps/place/X/"

        async def boom(u, **k):
            raise AssertionError("maps must not enter cascade")

        monkeypatch.setattr(ext, "extract_cascade", boom)
        out = _run(ext.complete_with_cascade(
            [maps], {maps: {"text": "", "links": []}}))
        assert out[maps]["text"] == ""

    def test_html_hrefs_keeps_internal(self):
        html = '<html><a href="/visite">Visite</a><a href="https://ext.example/x">x</a></html>'
        links = ext.html_hrefs(html, "https://parc.fr/index")
        assert "https://parc.fr/visite" in links
        assert "https://ext.example/x" in links

    def test_png_url_skips_fetch_and_chromium(self, monkeypatch):
        fetched = []
        rendered = []
        mirrored = []

        async def boom_raw(url, timeout=25):
            fetched.append(url)
            raise AssertionError("no fetch on .png url")

        async def boom_render(url, log=None):
            rendered.append(url)
            raise AssertionError("no chromium on .png url")

        async def boom_mirror(url, log=None, skip_tinyfish=False):
            mirrored.append(url)
            return ("portrait page " * 40, "N3-mirror-wayback")

        monkeypatch.setattr(ext, "fetch_raw", boom_raw)
        monkeypatch.setattr("app.core.render.render_html", boom_render)
        monkeypatch.setattr(ext, "fetch_mirror_text", boom_mirror)

        page = _run(ext.extract_cascade(
            "https://marviva.net/wp-content/uploads/2026/05/Sandra-Vilardy.png",
            min_chars=200))
        assert fetched == []
        assert rendered == []
        assert mirrored == []
        assert page["error"] == "image_url"
        assert page["text"] == ""
        assert page["render_used"] is False

    def test_image_magic_skips_chromium(self, monkeypatch):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00IHDR" + b"\xff" * 80
        rendered = []
        mirrored = []

        async def fake_raw(url, timeout=25):
            return _FakeResp(png, "image/png", url="https://cdn.example/download")

        async def boom_render(url, log=None):
            rendered.append(url)
            raise AssertionError("no chromium on image bytes")

        async def boom_mirror(url, log=None, skip_tinyfish=False):
            mirrored.append(url)
            return None

        monkeypatch.setattr(ext, "fetch_raw", fake_raw)
        monkeypatch.setattr("app.core.render.render_html", boom_render)
        monkeypatch.setattr(ext, "fetch_mirror_text", boom_mirror)

        page = _run(ext.extract_cascade("https://cdn.example/download", min_chars=200))
        assert rendered == []
        assert mirrored == []
        assert page["error"] == "image_content"
        assert page["text"] == ""

    def test_binary_octet_stream_skips_chromium(self, monkeypatch):
        rendered = []
        mirrored = []

        async def fake_raw(url, timeout=25):
            return _FakeResp(b"\x00\x01\x02\xff" * 40, "application/octet-stream",
                             url="https://cdn.example/blob")

        async def boom_render(url, log=None):
            rendered.append(url)
            raise AssertionError("no chromium on binary")

        async def boom_mirror(url, log=None, skip_tinyfish=False):
            mirrored.append(url)
            return None

        monkeypatch.setattr(ext, "fetch_raw", fake_raw)
        monkeypatch.setattr("app.core.render.render_html", boom_render)
        monkeypatch.setattr(ext, "fetch_mirror_text", boom_mirror)

        page = _run(ext.extract_cascade("https://cdn.example/blob", min_chars=200))
        assert rendered == []
        assert mirrored == []
        assert page["error"] == "binary_content"
        assert page["text"] == ""


class TestKeepIfLinks:
    def test_amp_empty_text_with_links_skips_cascade(self, monkeypatch):
        called = []
        url = "https://parc.fr/"

        async def fake_tf(urls, key, **k):
            return {u: {"text": "", "links": ["https://parc.fr/visite"]} for u in urls}

        async def boom(u, **k):
            called.append(u)
            return ext.empty_page(u)

        monkeypatch.setattr("app.core.tinyfish.tf_fetch", fake_tf)
        monkeypatch.setattr(ext, "extract_cascade", boom)

        pages = _run(ext.read_urls(
            [url], prefer_fetch=True, fetch_key="k", keep_if_links=True, min_chars=200))
        assert called == []
        assert pages[url]["links"] == ["https://parc.fr/visite"]


class TestRetiredHostsAndDownloads:
    def test_sct_mexico_is_retired(self):
        assert ext.host_is_retired(
            "https://www.sct.gob.mx/JURE/doc/ley.pdf") is True
        assert ext.host_is_retired(
            "https://elmirador.sct.gob.mx/los-puertos") is True
        assert ext.host_is_retired("https://www.sict.gob.mx/x") is False
        assert ext.host_is_retired("https://douane.gouv.fr/") is False

    def test_file_download_and_pdf_look_like_download(self):
        assert ext.url_looks_like_download(
            "https://www.budget.gouv.fr/documentation/file-download/18633") is True
        assert ext.url_looks_like_download(
            "https://example.gov/loi.pdf") is True
        assert ext.url_looks_like_download(
            "https://example.gov/page", "attachment; filename=a.bin") is True
        assert ext.url_looks_like_download("https://example.gov/ports") is False

    def test_skip_screenshot_reasons(self):
        url = "https://www.sct.gob.mx/x"
        assert "DNS" in (ext.should_skip_screenshot(url) or "")
        assert ext.should_skip_screenshot(
            "https://ok.gov/a", {"fetch_failed": True}) == (
            "URL déjà morte / fetch N1 échoué")
        assert ext.should_skip_screenshot(
            "https://ok.gov/a", {"download": True}) == "téléchargement / PDF"
        assert ext.should_skip_screenshot("https://ok.gov/ports") is None

    def test_n1_failure_skips_chromium(self, monkeypatch):
        async def fail(url, timeout=25):
            raise RuntimeError("down")

        rendered = []

        async def boom_render(url, log=None, **k):
            rendered.append(url)
            return None

        async def no_mirror(*a, **k):
            return None

        monkeypatch.setattr(ext, "fetch_raw", fail)
        monkeypatch.setattr("app.core.render.render_html", boom_render)
        monkeypatch.setattr(ext, "fetch_mirror_text", no_mirror)
        page = _run(ext.extract_cascade("https://down.example.gov/ports"))
        assert page.get("fetch_failed") is True
        assert rendered == []

    def test_extract_cascade_skips_retired_host(self, monkeypatch):
        async def boom(*a, **k):
            raise AssertionError("retired host must not be fetched")

        monkeypatch.setattr(ext, "fetch_raw", boom)
        page = _run(ext.extract_cascade(
            "https://www.sct.gob.mx/JURE/doc/ley-navegac-comercio-maritimos.pdf"))
        assert page["error"] == "dead_host"
        assert page["text"] == ""


class TestJina422AndWaybackPause:
    def test_jina_422_is_not_retried(self, monkeypatch):
        calls = []

        async def once(url, headers, timeout):
            calls.append(url)
            req = __import__("httpx").Request("GET", url)
            resp = __import__("httpx").Response(422, request=req)
            raise __import__("httpx").HTTPStatusError(
                "422", request=req, response=resp)

        async def no_sleep(_):
            raise AssertionError("422 must not sleep/retry")

        monkeypatch.setattr(ext, "_fetch_bytes", once)
        monkeypatch.setattr(ext.asyncio, "sleep", no_sleep)
        logs = []
        out = _run(ext._jina_mirror_text("https://example.gov/x", logs.append))
        assert out is None
        assert len(calls) == 1
        assert any("422" in m for m in logs)

    def test_wayback_pauses_after_three_503(self, monkeypatch):
        ext.reset_wayback_circuit()
        snaps = []

        async def snap(url):
            snaps.append(url)
            req = __import__("httpx").Request("GET", "https://web.archive.org/cdx")
            resp = __import__("httpx").Response(503, request=req)
            raise __import__("httpx").HTTPStatusError(
                "503", request=req, response=resp)

        monkeypatch.setattr(ext, "_wayback_snapshot_url", snap)
        logs = []
        for _ in range(3):
            assert _run(ext._wayback_mirror_text("https://example.gov/x", logs.append)) is None
        assert len(snaps) == 3
        assert ext.wayback_circuit_open() is True
        assert _run(ext._wayback_mirror_text("https://example.gov/y", logs.append)) is None
        assert len(snaps) == 3
        assert any("en pause" in m for m in logs)
        ext.reset_wayback_circuit()
        assert ext.wayback_circuit_open() is False
