"""run_artifacts — Per-run directory: append-only trace + manifest (P0-4).

Mongo stays the source of truth for a run. This module adds what the
prototype file runs had and V2 lacked:

  data/runs/<run_id>/trace.jsonl    append-only, one line per run / unit step;
  data/runs/<run_id>/manifest.json  run_id, git sha, config, models, counts,
                                    SHA-256 of files, sources, tool versions, cost.

The trace is never truncated or rewritten: a crashed run resumes from the
`unit_done` lines it already holds. No secret is written (keys are
stripped the same way as the run journal).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import tempfile
import threading
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from app.core.events import RUNS_DIR
from app.services.run_journal import _strip_secrets

MANIFEST_SCHEMA = "bi-run-manifest/1"
TRACE_NAME = "trace.jsonl"
MANIFEST_NAME = "manifest.json"

# Two independent fields, never derived from each other.
SCORE_FIELD = "confidence"               # V2 score, 0-100
LEVEL_FIELD = "confidence_level"         # C0-C4, nullable until PR #10 lands

# Zone statuses that mean "failed" (poe_runs writes "erreur").
ERROR_STATUSES = ("erreur", "error")

TOOL_PACKAGES = ("fastapi", "motor", "pymongo", "httpx", "shapely", "boto3",
                 "trafilatura", "pydantic")

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_run_id(run_id: str) -> str:
    rid = (run_id or "").strip()
    if not _RUN_ID_RE.match(rid) or rid in (".", ".."):
        raise ValueError("invalid run_id")
    return rid


def runs_root() -> Path:
    raw = (os.environ.get("BI_RUNS_DIR") or "").strip()
    return Path(raw) if raw else RUNS_DIR


def run_dir(run_id: str, root: Path | None = None) -> Path:
    return (root or runs_root()) / safe_run_id(run_id)


def trace_path(run_id: str, root: Path | None = None) -> Path:
    return run_dir(run_id, root) / TRACE_NAME


def manifest_path(run_id: str, root: Path | None = None) -> Path:
    return run_dir(run_id, root) / MANIFEST_NAME


# ── trace ────────────────────────────────────────────────────────────

def append_trace(run_id: str, event: str, root: Path | None = None, **payload) -> dict:
    """Append one line, flushed and fsynced. Never rewrites earlier lines.

    If a crash left the last line without its newline, a newline is added
    first so the new record never merges with the torn one.
    """
    rec = {"ts": now_iso(), "run_id": safe_run_id(run_id), "event": event,
           **_strip_secrets(payload)}
    data = (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    path = trace_path(run_id, root)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as f:
            if f.tell() > 0 and not _ends_with_newline(path):
                data = b"\n" + data
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    return rec


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"


def read_trace(run_id: str, root: Path | None = None) -> list[dict]:
    """All readable lines. A line torn by a crash is skipped with a warning."""
    path = trace_path(run_id, root)
    if not path.is_file():
        return []
    out = []
    raw_lines = path.read_bytes().split(b"\n")
    last = len(raw_lines) - 1
    for i, raw in enumerate(raw_lines):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            where = "truncated final line" if i == last else f"unreadable line {i + 1}"
            logger.warning("trace %s: %s ignored (%d bytes)", path, where, len(raw))
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def completed_units(run_id: str, root: Path | None = None) -> set:
    """Units with a `unit_done` line in the trace."""
    return {rec.get("unit") for rec in read_trace(run_id, root)
            if rec.get("event") == "unit_done" and rec.get("unit") is not None}


def is_error_status(status) -> bool:
    return str(status or "").strip().lower() in ERROR_STATUSES


def errored_units(run_id: str, root: Path | None = None) -> set:
    """Units whose last unit_done / unit_error line is an error.

    A unit that ever finished with a non-error status is never returned.
    """
    last: dict = {}
    ok: set = set()
    for rec in read_trace(run_id, root):
        unit = rec.get("unit")
        if unit is None:
            continue
        if rec.get("event") == "unit_error":
            last[unit] = True
        elif rec.get("event") == "unit_done":
            failed = is_error_status(rec.get("status"))
            last[unit] = failed
            if not failed:
                ok.add(unit)
    return {u for u, failed in last.items() if failed and u not in ok}


# ── hashes / manifest ────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def file_entries(base: Path, names: list[str]) -> list[dict]:
    out = []
    for name in names:
        p = base / name
        if p.is_file():
            out.append({"path": name, "sha256": sha256_file(p),
                        "bytes": p.stat().st_size})
    return out


def tool_versions() -> dict:
    pkgs = {}
    for name in TOOL_PACKAGES:
        try:
            pkgs[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pkgs[name] = None
    return {"python": platform.python_version(), "packages": pkgs}


def models_from_code(code: dict | None) -> dict:
    """Model / provider names from the code fingerprint (booleans and names only)."""
    c = code or {}
    if c.get("tokenfactory_enabled"):
        provider = "tokenfactory"
    elif c.get("nvidia_enabled"):
        provider = "nvidia"
    elif c.get("claude_enabled"):
        provider = "anthropic"
    else:
        provider = None
    return {
        "provider": provider,
        "nvidia_model": c.get("nvidia_model"),
        "judge_model": c.get("tokenfactory_judge_model"),
        "page_model": c.get("tokenfactory_page_model"),
        "claude_model": c.get("claude_model"),
    }


def config_from_params(params: dict | None) -> dict:
    p = params or {}
    rules = p.get("rules") if isinstance(p.get("rules"), dict) else {}
    keep = ("label", "mode", "kind", "limit", "only_zones", "concurrency",
            "variant", "force", "force_rescan", "from_scratch")
    out = {k: p.get(k) for k in keep if k in p}
    out["profile"] = rules.get("profile") or p.get("profile")
    out["rules_hash"] = rules.get("hash")
    return out


def sources_summary(urls) -> dict:
    domains = set()
    n = 0
    for u in urls or []:
        if not u:
            continue
        n += 1
        host = str(u).split("//", 1)[-1].split("/", 1)[0].lower()
        domains.add(host[4:] if host.startswith("www.") else host)
    return {"count": n, "domains": sorted(domains)}


def build_manifest(*, run_id: str, layer: str, state: str, params: dict | None,
                   counts: dict | None = None, files: list[dict] | None = None,
                   sources: dict | None = None, cost: dict | None = None,
                   extra: dict | None = None) -> dict:
    p = params or {}
    code = p.get("code") if isinstance(p.get("code"), dict) else {}
    doc = {
        "schema": MANIFEST_SCHEMA,
        "run_id": safe_run_id(run_id),
        "layer": layer,
        "state": state,
        "written_at": now_iso(),
        "git": {"sha": code.get("git_sha"), "dirty": code.get("git_dirty"),
                "resume_sha": p.get("resume_git_sha")},
        "config": config_from_params(p),
        "models": models_from_code(code),
        "counts": counts or {},
        "files": files or [],
        "sources": sources or {"count": 0, "domains": []},
        "tool_versions": tool_versions(),
        "cost": cost or {},
        "fields": {
            "score": {"name": SCORE_FIELD, "range": "0-100", "origin": "V2"},
            "certification_level": {"name": LEVEL_FIELD, "values": "C0-C4",
                                    "nullable": True},
            "derived_from_each_other": False,
        },
        **(extra or {}),
    }
    return _strip_secrets(doc)


def write_json_atomic(path: Path, doc: dict) -> str:
    """Write via a fsynced temp file in the same directory + rename.

    Readers see either the previous file or the new one, never a partial
    write. Returns the SHA-256 of the result.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp creates 0600
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)
    return sha256_file(path)


def _fsync_dir(directory: Path) -> None:
    """Persist the rename itself (best effort: not supported everywhere)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_run_manifest(run_id: str, manifest: dict, root: Path | None = None) -> str:
    """Refresh data/runs/<run_id>/manifest.json (the trace is hashed in it)."""
    base = run_dir(run_id, root)
    manifest = dict(manifest)
    manifest["files"] = file_entries(base, [TRACE_NAME])
    return write_json_atomic(base / MANIFEST_NAME, manifest)


async def record_open(run_id: str, layer: str, params: dict | None, *,
                      resumed: bool = False) -> None:
    """run_start / run_resume line + a `running` manifest. Never raises."""
    try:
        await asyncio.to_thread(append_trace, run_id,
                                "run_resume" if resumed else "run_start", layer=layer)
        await asyncio.to_thread(write_run_manifest, run_id, build_manifest(
            run_id=run_id, layer=layer, state="running", params=params))
    except Exception as exc:
        logger.warning("run artifacts open failed: %s", exc)


async def record_close(run_id: str, layer: str, params: dict | None, *,
                       state: str, counts: dict | None = None,
                       cost: dict | None = None) -> None:
    """run_done line + final manifest. Never raises."""
    try:
        await asyncio.to_thread(append_trace, run_id, "run_done", layer=layer,
                                state=state, counts=counts or {})
        await asyncio.to_thread(write_run_manifest, run_id, build_manifest(
            run_id=run_id, layer=layer, state=state, params=params,
            counts=counts, cost=cost))
    except Exception as exc:
        logger.warning("run artifacts close failed: %s", exc)


def read_manifest(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
