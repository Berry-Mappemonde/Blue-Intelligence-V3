"""GET /api/health — cheap public probe.

Reads cgroup v2 memory (or process RSS) and pings the database.
No grids, no settings, no environment values in the response.
Where neither is readable (macOS has no /proc), memory_used_pct is null.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api")

_PROCESS_START = time.monotonic()
_MEMORY_DEGRADED_PCT = 85.0
_DB_PING_TIMEOUT_S = 1.0


def _cgroup_v2_relative(cgroup_proc: Path) -> str | None:
    try:
        lines = cgroup_proc.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split(":")
        # cgroup v2 single hierarchy: "0::/system.slice/…"
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            relative = parts[2].strip()
            return relative or "/"
    return None


def _cgroup_bytes(base: Path) -> tuple[int, int] | None:
    try:
        current = int((base / "memory.current").read_text(encoding="utf-8").strip())
        raw_max = (base / "memory.max").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    # "max" means no limit: the service ceiling is not readable.
    if raw_max == "max":
        return None
    try:
        maximum = int(raw_max)
    except ValueError:
        return None
    if maximum <= 0:
        return None
    return current, maximum


def cgroup_memory_used_pct(
    cgroup_proc: Path | None = None,
    sysfs_root: Path | None = None,
) -> float | None:
    """Percent of the service cgroup v2 memory.max, or None if unreadable."""
    proc = cgroup_proc or Path("/proc/self/cgroup")
    root = (sysfs_root or Path("/sys/fs/cgroup")).resolve()
    relative = _cgroup_v2_relative(proc)
    if relative is None:
        return None
    base = (root / relative.lstrip("/")).resolve()
    try:
        base.relative_to(root)
    except ValueError:
        return None
    pair = _cgroup_bytes(base)
    if pair is None:
        return None
    current, maximum = pair
    return 100.0 * current / maximum


def rss_memory_used_pct(
    status_path: Path | None = None,
    meminfo_path: Path | None = None,
) -> float | None:
    """Process RSS / total RAM, in percent. None if /proc cannot be read."""
    status = status_path or Path("/proc/self/status")
    meminfo = meminfo_path or Path("/proc/meminfo")
    try:
        rss_kb = None
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
                break
        total_kb = None
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        return None
    if rss_kb is None or total_kb is None or total_kb <= 0:
        return None
    return 100.0 * rss_kb / total_kb


def memory_used_pct() -> float | None:
    """cgroup v2 current/max when the service limit is readable, else RSS/RAM.

    None when neither is readable (no /proc, e.g. macOS): memory is unknown, not full.
    """
    used = cgroup_memory_used_pct()
    if used is None:
        used = rss_memory_used_pct()
    if used is None:
        return None
    if used < 0:
        return 0.0
    return float(used)


def uptime_s() -> int:
    return max(0, int(time.monotonic() - _PROCESS_START))


async def db_ping_ok() -> bool:
    """True when Mongo answers ping within one second. Failures stay boolean."""
    try:
        import pymongo

        from app.db import client

        with pymongo.timeout(_DB_PING_TIMEOUT_S):
            await asyncio.wait_for(
                client.admin.command("ping"),
                timeout=_DB_PING_TIMEOUT_S,
            )
        return True
    except Exception:
        return False


@router.get("/health")
async def api_health():
    used = memory_used_pct()
    db_ok = await db_ping_ok()
    memory_ok = used is None or used < _MEMORY_DEGRADED_PCT
    status = "ok" if db_ok and memory_ok else "degraded"
    return JSONResponse(
        {
            "status": status,
            "memory_used_pct": used,
            "uptime_s": uptime_s(),
        },
        headers={"Cache-Control": "no-store"},
    )
