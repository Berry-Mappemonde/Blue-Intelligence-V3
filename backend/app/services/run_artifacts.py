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
    """Append one line. Never rewrites earlier lines."""
    rec = {"ts": now_iso(), "run_id": safe_run_id(run_id), "event": event,
           **_strip_secrets(payload)}
    line = json.dumps(rec, ensure_ascii=False, default=str)
    path = trace_path(run_id, root)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    return rec


def read_trace(run_id: str, root: Path | None = None) -> list[dict]:
    """All readable lines; a line torn by a crash is skipped."""
    path = trace_path(run_id, root)
    if not path.is_file():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
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
    """Write via a temp file + rename; return the SHA-256 of the result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return sha256_file(path)


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
