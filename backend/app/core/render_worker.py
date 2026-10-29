"""Chromium subprocess (isolated Playwright).

HTML: ``python -m app.core.render_worker URL OUT.html [timeout_s] [settle_ms]``
JPEG: ``python -m app.core.render_worker --screenshot URL OUT.jpg
        [timeout_s] [settle_ms] [max_height]``

A native crash (munmap_chunk, SIGABRT) kills only this process — the API
continues. Chromium RAM is released on exit, like ``pdf_worker``.
"""
from __future__ import annotations

import sys
from pathlib import Path

UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--renderer-process-limit=1",
    "--js-flags=--max-old-space-size=256",
]

VIEWPORT = {"width": 1280, "height": 900}
SCREENSHOT_MAX_HEIGHT = 2400


def _open_page(url: str, timeout_s: int, settle_ms: int):
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
    try:
        context = browser.new_context(
            user_agent=UA_BROWSER, viewport=VIEWPORT, locale="en-US")
        page = context.new_page()
        page.set_default_timeout(timeout_s * 1000)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            page.wait_for_timeout(settle_ms)
        return pw, browser, page
    except Exception:
        browser.close()
        pw.stop()
        raise


def render_url_to_file(url: str, out: str, timeout_s: int = 45,
                      settle_ms: int = 2500) -> None:
    pw = browser = None
    try:
        pw, browser, page = _open_page(url, timeout_s, settle_ms)
        Path(out).write_text(page.content() or "", encoding="utf-8")
    finally:
        if browser is not None:
            browser.close()
        if pw is not None:
            pw.stop()


def screenshot_url_to_file(url: str, out: str, timeout_s: int = 45,
                           settle_ms: int = 2500,
                           max_height: int = SCREENSHOT_MAX_HEIGHT) -> None:
    """JPEG recadré (pas une page entière de 50 000 px)."""
    pw = browser = None
    try:
        pw, browser, page = _open_page(url, timeout_s, settle_ms)
        try:
            doc_h = page.evaluate(
                "() => Math.ceil(document.documentElement.scrollHeight || 0)")
        except Exception:
            doc_h = VIEWPORT["height"]
        height = max(1, min(int(doc_h or VIEWPORT["height"]), int(max_height),
                            SCREENSHOT_MAX_HEIGHT))
        page.screenshot(
            path=out, type="jpeg", quality=65,
            clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height})
    finally:
        if browser is not None:
            browser.close()
        if pw is not None:
            pw.stop()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    shot = False
    if argv and argv[0] == "--screenshot":
        shot = True
        argv = argv[1:]
    if len(argv) < 2:
        print("usage: python -m app.core.render_worker [--screenshot] "
              "URL OUT [timeout_s] [settle_ms] [max_height]", file=sys.stderr)
        return 2
    url, out = argv[0], argv[1]
    timeout_s = int(argv[2]) if len(argv) > 2 else 45
    settle_ms = int(argv[3]) if len(argv) > 3 else 2500
    max_height = int(argv[4]) if len(argv) > 4 else SCREENSHOT_MAX_HEIGHT
    try:
        if shot:
            screenshot_url_to_file(url, out, timeout_s, settle_ms, max_height)
        else:
            render_url_to_file(url, out, timeout_s, settle_ms)
    except ImportError as e:
        print(f"playwright missing: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
