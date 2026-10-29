"""
render_core — Local browser render (Playwright + headless Chromium).

``render_html`` (Full / PoE cascade) and ``render_screenshot`` (Review
vision) launch Chromium in a *subprocess* (``app.core.render_worker``).
A native crash (munmap_chunk under MemoryHigh=2G) or a site that never
finishes loading kills only the child: the API continues, RAM is
released. The process group (Python + Chrome) is killed on timeout.

Clean degradation: if playwright or its browser is not installed,
we return None and the cascade continues without render.
Install: pip install playwright && playwright install chromium
"""
import asyncio
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from app.config import BACKEND_DIR

UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_pw = None
_browser = None
_lock = asyncio.Lock()
# One Chromium at a time in the MemoryHigh=2G cgroup.
_sem = asyncio.Semaphore(1)
_unavailable = False
RENDER_SUBPROCESS_GRACE_S = 20
SCREENSHOT_MAX_HEIGHT = 2400


class RenderUnavailable(Exception):
    """Playwright / browser missing — disable subsequent renders."""


def _render_worker_env() -> dict:
    env = os.environ.copy()
    backend = str(BACKEND_DIR)
    current = env.get("PYTHONPATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    if backend not in parts:
        env["PYTHONPATH"] = os.pathsep.join([backend, *parts]) if parts else backend
    return env


def _kill_worker_group(proc: subprocess.Popen) -> None:
    """Kill Python + Chromium (setsid session), not only the parent."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_worker_argv(argv: list[str], timeout_s: int) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "app.core.render_worker", *argv]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_render_worker_env(),
        cwd=str(BACKEND_DIR),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s + RENDER_SUBPROCESS_GRACE_S)
    except subprocess.TimeoutExpired:
        _kill_worker_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = b"", b""
        raise subprocess.TimeoutExpired(cmd, timeout_s + RENDER_SUBPROCESS_GRACE_S,
                                        output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _decode_worker(proc: subprocess.CompletedProcess) -> None:
    if proc.returncode == 2:
        err = (proc.stderr or b"").decode("utf-8", "replace")[:200]
        raise RenderUnavailable(err or "playwright missing")


def _run_render_worker(url: str, timeout_s: int, settle_ms: int) -> str | None:
    """Spawn ``app.core.render_worker`` HTML — hookable from tests."""
    with tempfile.TemporaryDirectory(prefix="bi-render-") as tmp:
        out = Path(tmp) / "page.html"
        try:
            proc = _run_worker_argv(
                [url, str(out), str(timeout_s), str(settle_ms)], timeout_s)
        except subprocess.TimeoutExpired:
            return None
        _decode_worker(proc)
        if proc.returncode != 0 or not out.is_file():
            return None
        text = out.read_text(encoding="utf-8")
        return text or None


def _run_screenshot_worker(url: str, timeout_s: int, settle_ms: int,
                           max_height: int = SCREENSHOT_MAX_HEIGHT) -> bytes | None:
    """Spawn ``app.core.render_worker --screenshot`` — hookable from tests."""
    with tempfile.TemporaryDirectory(prefix="bi-shot-") as tmp:
        out = Path(tmp) / "page.jpg"
        try:
            proc = _run_worker_argv(
                ["--screenshot", url, str(out), str(timeout_s),
                 str(settle_ms), str(max_height)],
                timeout_s)
        except subprocess.TimeoutExpired:
            return None
        _decode_worker(proc)
        if proc.returncode != 0 or not out.is_file():
            return None
        data = out.read_bytes()
        return data or None


async def _get_browser(log):
    """Former shared browser — no longer used for Review screenshots."""
    global _pw, _browser, _unavailable
    if _unavailable:
        return None
    async with _lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
            if _pw is None:
                _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage",
                                     "--disable-gpu", "--renderer-process-limit=1"])
            return _browser
        except Exception as e:
            _unavailable = True
            log(f"render: Playwright indisponible ({type(e).__name__}: {str(e)[:80]}) — rendu désactivé")
            return None


async def render_html(url: str, timeout_s: int = 45, settle_ms: int = 2500, log=None) -> str | None:
    """HTML rendered by isolated Chromium (DOM after JavaScript), or None."""
    global _unavailable
    log = log or (lambda m: None)
    if _unavailable:
        return None
    async with _sem:
        try:
            return await asyncio.to_thread(
                _run_render_worker, url, timeout_s, settle_ms)
        except RenderUnavailable as e:
            _unavailable = True
            log(f"render: Playwright indisponible ({e}) — rendu désactivé")
            return None
        except Exception as e:
            log(f"render {url[:70]}: échec ({type(e).__name__}: {str(e)[:60]})")
            return None


async def render_screenshot(url: str, timeout_s: int = 45, settle_ms: int = 2500,
                            log=None) -> bytes | None:
    """Bounded JPEG capture (Review vision) in a subprocess, or None."""
    global _unavailable
    from app.core.extract import should_skip_screenshot
    log = log or (lambda m: None)
    skip = should_skip_screenshot(url)
    if skip:
        log(f"screenshot {url[:70]}: sauté ({skip})")
        return None
    if _unavailable:
        return None
    async with _sem:
        try:
            return await asyncio.to_thread(
                _run_screenshot_worker, url, timeout_s, settle_ms,
                SCREENSHOT_MAX_HEIGHT)
        except RenderUnavailable as e:
            _unavailable = True
            log(f"render: Playwright indisponible ({e}) — rendu désactivé")
            return None
        except Exception as e:
            log(f"screenshot {url[:70]}: échec ({type(e).__name__}: {str(e)[:60]})")
            return None


async def shutdown_render():
    """Clean shutdown (called on app shutdown and after each sheet)."""
    global _pw, _browser
    try:
        if _browser is not None:
            await _browser.close()
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    _browser = None
    _pw = None
