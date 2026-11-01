"""PDF sources: plain text with caps, fallback, and an explicit scanned status.

Fixtures (tests/fixtures/pdf, a few KB each):
  text.pdf       one page with a text layer (Martinique ports of entry)
  scanned.pdf    one page holding only an image, no text layer
  multipage.pdf  five pages, PAGE-MARKER-<n> on each
No network: fetch_raw and the mirrors are stubbed.
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import extract as ext  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "pdf"
PDF_URL = "https://www.martinique.gouv.fr/content/download/ports-entree.pdf"


def _pdf(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(ext, "PDF_CACHE_DIR", tmp_path / "pdf-cache")

    async def no_mirror(*a, **k):
        return None

    for name in ("_jina_mirror_text", "_wayback_mirror_text", "_jorf_reader_text",
                 "_tinyfish_mirror_text", "_tavily_mirror_text"):
        monkeypatch.setattr(ext, name, no_mirror)


def _serve(monkeypatch, content: bytes, ctype: str = "application/pdf"):
    async def fake_fetch(url, timeout=25):
        return httpx.Response(200, content=content, headers={"content-type": ctype},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(ext, "fetch_raw", fake_fetch)


class TestReadPdf:
    def test_text_pdf(self):
        logs = []
        res = _run(ext.read_pdf(_pdf("text.pdf"), log=logs.append))
        assert res["status"] == "ok" and res["engine"] == "pymupdf"
        assert "Fort-de-France" in res["text"] and "Le Marin" in res["text"]

    def test_worker_failure_falls_back_to_pdfminer(self, monkeypatch):
        def crash(inp, out, max_pages):
            raise RuntimeError("pdf_worker exit -11")

        monkeypatch.setattr(ext, "_run_pdf_worker", crash)
        logs = []
        res = _run(ext.read_pdf(_pdf("text.pdf"), log=logs.append))
        assert res["status"] == "ok" and res["engine"] == "pdfminer"
        assert "Fort-de-France" in res["text"]
        assert any("repli pdfminer" in m for m in logs)

    def test_plain_engine_alone(self):
        text = ext.parse_pdf_text_plain(_pdf("text.pdf"))
        assert "Saint-Pierre" in text

    def test_scanned_pdf_is_flagged_for_vl(self):
        logs = []
        res = _run(ext.read_pdf(_pdf("scanned.pdf"), url=PDF_URL, log=logs.append))
        assert res["status"] == "no_text" and res["text"] == ""
        line = [m for m in logs if "sans texte extractible" in m]
        assert line and "Nemotron VL" in line[0] and PDF_URL[:60] in line[0]

    def test_oversized_is_not_parsed(self, monkeypatch):
        called = []
        monkeypatch.setattr(ext, "_run_pdf_worker", lambda *a: called.append(a))
        monkeypatch.setattr(ext, "parse_pdf_text_plain", lambda *a, **k: called.append(a))
        monkeypatch.setattr(ext, "PDF_MAX_BYTES", 1024)
        logs = []
        res = _run(ext.read_pdf(_pdf("text.pdf"), log=logs.append))
        assert res["status"] == "oversized" and res["text"] == ""
        assert called == []
        assert any("PDF trop gros" in m for m in logs)

    @pytest.mark.parametrize("engine", ["pymupdf", "pdfminer"])
    def test_page_cap(self, monkeypatch, engine):
        if engine == "pdfminer":
            def crash(inp, out, max_pages):
                raise RuntimeError("no worker")
            monkeypatch.setattr(ext, "_run_pdf_worker", crash)
        res = _run(ext.read_pdf(_pdf("multipage.pdf"), max_pages=2))
        assert res["engine"] == engine
        assert "PAGE-MARKER-1" in res["text"] and "PAGE-MARKER-2" in res["text"]
        assert "PAGE-MARKER-3" not in res["text"]

    def test_both_engines_fail(self, monkeypatch):
        def crash(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(ext, "_run_pdf_worker", crash)
        monkeypatch.setattr(ext, "parse_pdf_text_plain", crash)
        logs = []
        res = _run(ext.read_pdf(b"%PDF-1.4 broken", log=logs.append))
        assert res["status"] == "error"
        assert any("PDF illisible" in m for m in logs)


class TestCascadeUsesPdfText:
    """Fetched pages become source text through extract_cascade (PoE _read_sources,
    complete_with_cascade, read_urls)."""

    def test_pdf_source_text(self, monkeypatch):
        _serve(monkeypatch, _pdf("text.pdf"))
        page = _run(ext.extract_cascade(PDF_URL, allow_render=False))
        assert page["is_pdf"] and page["pdf_status"] == "ok"
        assert page["level"] == "N1-pymupdf"
        assert "Fort-de-France" in page["text"]

    def test_pdf_served_as_octet_stream(self, monkeypatch):
        _serve(monkeypatch, _pdf("text.pdf"), ctype="application/octet-stream")
        page = _run(ext.extract_cascade("https://www.martinique.gouv.fr/doc?id=12",
                                        allow_render=False))
        assert page["is_pdf"] and "Le Marin" in page["text"]

    def test_worker_crash_still_gives_source_text(self, monkeypatch):
        def crash(inp, out, max_pages):
            raise RuntimeError("pdf_worker exit -11")

        monkeypatch.setattr(ext, "_run_pdf_worker", crash)
        _serve(monkeypatch, _pdf("text.pdf"))
        page = _run(ext.extract_cascade(PDF_URL, allow_render=False))
        assert page["level"] == "N1-pdfminer"
        assert "Saint-Pierre" in page["text"]

    def test_scanned_pdf_is_marked(self, monkeypatch):
        _serve(monkeypatch, _pdf("scanned.pdf"))
        logs = []
        page = _run(ext.extract_cascade(PDF_URL, allow_render=False, log=logs.append))
        assert page["pdf_status"] == "no_text" and page["pdf_scanned"] is True
        assert page["level"] == "failed" and page["text"] == ""
        assert any("candidat Nemotron VL" in m for m in logs)

    def test_oversized_pdf_is_marked(self, monkeypatch):
        monkeypatch.setattr(ext, "PDF_MAX_BYTES", 1024)
        _serve(monkeypatch, _pdf("text.pdf"))
        page = _run(ext.extract_cascade(PDF_URL, allow_render=False))
        assert page["pdf_status"] == "oversized" and page["text"] == ""

    def test_fetch_record_keeps_pdf_status(self, monkeypatch):
        _serve(monkeypatch, _pdf("scanned.pdf"))
        out = _run(ext.complete_with_cascade([PDF_URL], {}, allow_render=False))
        rec = out[PDF_URL]
        assert rec["is_pdf"] and rec["pdf_scanned"] is True
        assert rec["pdf_status"] == "no_text"
