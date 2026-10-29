"""Full journal of a Projects run.

The Console buffer (`GET /api/swarm/status`) keeps only the last 200
lines in memory, and agent cards show only 8. Here, **all**
lines (swarm narrative + TinyFish agents) are kept without a cap:

  - file ``backend/data/runs/<run_id>.journal.jsonl`` (disk artifact);
  - Mongo collection ``project_run_journal`` (daily VPS backups);
  - back-compat: ``kind=log`` and ``kind=meta`` lines are also
    appended to ``<run_id>.swarm.jsonl`` (already produced by in-flight runs).

The first line (``kind=meta``, seq=1) records the run **parameters and
rules** (profile, hash, `chosen`, code fingerprint — no secret).
The JSON API always returns ``params`` + ``header_text`` (even with
``tail=true``). Already-finished runs without a meta line are rebuilt
from ``project_runs.params``. The live ``projects`` map is never
written.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.core.events import RUNS_DIR

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")

JOURNAL_KIND_LOG = "log"
JOURNAL_KIND_AGENT = "agent"
JOURNAL_KIND_META = "meta"
JOURNAL_KINDS = frozenset({JOURNAL_KIND_LOG, JOURNAL_KIND_AGENT, JOURNAL_KIND_META})

_SECRET_KEY_RE = re.compile(r"(api_key|secret|password|token|passwd)", re.I)

JOURNAL_LIMIT_DEFAULT = 2000
JOURNAL_LIMIT_MAX = 10_000
JOURNAL_DOWNLOAD_MAX = 100_000

# Agent lines kept on the live card (the file is not truncated).
AGENT_LIVE_TAIL = 8


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_run_id(run_id: str) -> str:
    rid = (run_id or "").strip()
    if not _RUN_ID_RE.match(rid):
        raise ValueError("invalid run_id")
    return rid


def journal_path(run_id: str) -> Path:
    return RUNS_DIR / f"{safe_run_id(run_id)}.journal.jsonl"


def swarm_log_path(run_id: str) -> Path:
    return RUNS_DIR / f"{safe_run_id(run_id)}.swarm.jsonl"


def _jsonl_paths(run_id: str) -> list[Path]:
    """Unified file first, else the old .swarm.jsonl (already-started runs)."""
    rid = safe_run_id(run_id)
    primary = RUNS_DIR / f"{rid}.journal.jsonl"
    legacy = RUNS_DIR / f"{rid}.swarm.jsonl"
    if primary.is_file():
        return [primary]
    if legacy.is_file():
        return [legacy]
    return []


def _strip_secrets(obj):
    """Drop api_key / secret-like keys. No secret in the journal."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEY_RE.search(str(k)):
                continue
            out[k] = _strip_secrets(v)
        return out
    if isinstance(obj, list):
        return [_strip_secrets(x) for x in obj]
    return obj


def _chosen_rules(p: dict) -> tuple[dict, str | None, str | None, dict | None]:
    """Accept the Mongo snapshot (``rules.chosen``) or the already-public form."""
    rules = p.get("rules") if isinstance(p.get("rules"), dict) else {}
    if isinstance(rules.get("chosen"), dict):
        return (
            rules["chosen"],
            rules.get("profile") or p.get("profile"),
            rules.get("hash") or p.get("hash"),
            rules.get("counts") or p.get("counts"),
        )
    chosen = {
        k: v for k, v in rules.items()
        if isinstance(v, dict) and "value" in v
    }
    return (
        chosen,
        p.get("profile") or rules.get("profile"),
        p.get("hash") or rules.get("hash"),
        p.get("counts") if isinstance(p.get("counts"), dict) else rules.get("counts"),
    )


def public_run_params(params: dict | None) -> dict:
    """Run parameters + rules, no secrets. wrote_projects stays false."""
    p = params if isinstance(params, dict) else {}
    chosen, profile, digest, counts = _chosen_rules(p)
    code = p.get("code") if isinstance(p.get("code"), dict) else {}
    out = {
        "mode": p.get("mode"),
        "label": p.get("label"),
        "force_rescan": p.get("force_rescan"),
        "from_scratch": p.get("from_scratch") if p.get("from_scratch") is not None
        else p.get("force_rescan"),
        "wrote_projects": False,
        "profile": profile,
        "hash": digest,
        "counts": counts,
        "code": {
            "git_sha": code.get("git_sha"),
            "git_dirty": code.get("git_dirty"),
            "tinyfish_configured": code.get("tinyfish_configured"),
            "serper_configured": code.get("serper_configured"),
            "openrouter_configured": code.get("openrouter_configured"),
            "nvidia_configured": code.get("nvidia_configured"),
            "claude_enabled": code.get("claude_enabled"),
        },
        "rules": _strip_secrets(chosen),
    }
    return _strip_secrets(out)


def header_summary(pub: dict | None) -> str:
    pub = pub or {}
    bits = [
        f"profile={pub.get('profile') or '—'}",
        f"hash={pub.get('hash') or '—'}",
        f"mode={pub.get('mode') or '—'}",
        f"force_rescan={pub.get('force_rescan')}",
        "wrote_projects: false",
    ]
    return "Run params " + " ".join(bits)


def _rule_title(rule_id: str) -> str:
    try:
        from app.core.run_rules import get_rule_def
        loc = (get_rule_def(rule_id).get("title") or {})
        return loc.get("fr") or loc.get("en") or rule_id
    except Exception:
        return rule_id


def journal_header_to_text(params: dict | None) -> str:
    """Readable block: parameters + each rule used."""
    pub = public_run_params(params) if params else {}
    if not pub.get("profile") and not pub.get("rules") and not pub.get("mode"):
        return ""
    code = pub.get("code") or {}
    lines = [
        "# --- Paramètres ---",
        f"# mode: {pub.get('mode')}",
        f"# label: {pub.get('label') or '—'}",
        f"# force_rescan: {pub.get('force_rescan')}",
        f"# from_scratch: {pub.get('from_scratch')}",
        f"# profile: {pub.get('profile')}",
        f"# hash: {pub.get('hash')}",
        f"# git_sha: {code.get('git_sha')}",
        f"# git_dirty: {code.get('git_dirty')}",
        f"# tinyfish_configured: {code.get('tinyfish_configured')}",
        f"# serper_configured: {code.get('serper_configured')}",
        f"# openrouter_configured: {code.get('openrouter_configured')}",
        f"# nvidia_configured: {code.get('nvidia_configured')}",
        f"# claude_enabled: {code.get('claude_enabled')}",
        "# wrote_projects: false",
    ]
    rules = pub.get("rules") or {}
    n = len(rules) if isinstance(rules, dict) else 0
    counts = pub.get("counts") or {}
    lines.append(f"# --- Règles ({counts.get('total') or n}) ---")
    for rid in sorted(rules):
        rec = rules[rid] if isinstance(rules[rid], dict) else {"value": rules[rid]}
        unit = rec.get("unit") or ""
        kind = rec.get("kind") or ""
        source = rec.get("source") or ""
        val = rec.get("value")
        title = _rule_title(rid)
        extra = f"  ({source})" if source else ""
        unit_s = f" {unit}" if unit and unit not in ("bool", "ratio") else ""
        lines.append(
            f"# [{kind}] {rid} = {val}{unit_s}{extra}  — {title}".rstrip()
        )
    return "\n".join(lines) + "\n"


def params_from_items(items: list[dict] | None) -> dict | None:
    for e in items or []:
        if e.get("kind") == JOURNAL_KIND_META and isinstance(e.get("params"), dict):
            return e["params"]
    return None


def params_from_journal(run_id: str) -> dict | None:
    """First ``kind=meta`` line of the file (old runs do not have one)."""
    try:
        for e in iter_journal_file(run_id):
            if e.get("kind") == JOURNAL_KIND_META and isinstance(e.get("params"), dict):
                return e["params"]
            return None
    except ValueError:
        return None
    return None


def enrich_journal_payload(packed: dict, params: dict | None = None) -> dict:
    """Add ``params`` + ``header_text`` even if ``tail=true`` skips seq=1."""
    raw = params
    if not raw:
        raw = params_from_items(packed.get("items") or [])
    packed["params"] = public_run_params(raw) if raw else {}
    packed["header_text"] = journal_header_to_text(raw) if raw else ""
    packed["wrote_projects"] = False
    return packed


def _public_entry(entry: dict) -> dict:
    out = {}
    for key in ("seq", "ts", "kind", "level", "msg", "agent", "engine",
                "source", "url", "status", "params", "profile", "hash"):
        val = entry.get(key)
        if val is not None and val != "":
            out[key] = val
    if "kind" not in out:
        out["kind"] = JOURNAL_KIND_LOG
    if "level" not in out:
        out["level"] = "info"
    if out.get("kind") == JOURNAL_KIND_META and "params" in out:
        out["params"] = public_run_params(out["params"])
    return out


def append_journal(run_id: str, entry: dict, seq: int | None = None) -> dict:
    """Append a line to the disk journal. Do not touch ``projects``."""
    rid = safe_run_id(run_id)
    rec = _public_entry(entry)
    rec["run_id"] = rid
    if seq is not None:
        rec["seq"] = int(seq)
    if not rec.get("ts"):
        rec["ts"] = now_iso()
    line = json.dumps(rec, ensure_ascii=False, default=str)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    primary = journal_path(rid)
    with _LOCK:
        with primary.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if rec.get("kind") in (JOURNAL_KIND_LOG, JOURNAL_KIND_META):
            # Same narrative as in-flight runs (.swarm.jsonl file).
            with swarm_log_path(rid).open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    return rec


def iter_journal_file(run_id: str, kind: str | None = None):
    """Read the journal file (or the fallback .swarm.jsonl)."""
    want = (kind or "").strip().lower() or None
    if want == "all":
        want = None
    for path in _jsonl_paths(run_id):
        try:
            with path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    pub = _public_entry(entry)
                    if want and pub.get("kind") != want:
                        continue
                    yield pub
        except OSError as exc:
            logger.warning("journal read failed run_id=%s path=%s: %s", run_id, path, exc)
        return


def count_journal_file(run_id: str, kind: str | None = None) -> int:
    return sum(1 for _ in iter_journal_file(run_id, kind=kind))


def read_journal_file(run_id: str, *, skip: int = 0, limit: int = JOURNAL_LIMIT_DEFAULT,
                      kind: str | None = None, tail: bool = False) -> dict:
    """Page the journal file. ``tail=true`` = the last *limit* lines."""
    items = list(iter_journal_file(run_id, kind=kind))
    total = len(items)
    skip = max(0, int(skip or 0))
    limit = max(1, min(JOURNAL_LIMIT_MAX, int(limit or JOURNAL_LIMIT_DEFAULT)))
    if tail:
        skip = max(0, total - limit)
    page = items[skip:skip + limit]
    return {
        "run_id": run_id,
        "total": total,
        "skip": skip,
        "count": len(page),
        "kind": kind or "all",
        "items": page,
        "wrote_projects": False,
        "source": "file" if _jsonl_paths(run_id) else "empty",
    }


def journal_to_text(run_id: str, items: list[dict] | None = None,
                    params: dict | None = None) -> str:
    """Readable narrative: parameters + rules, then the timestamped stream."""
    if items is None:
        items = list(iter_journal_file(run_id))
    raw = params or params_from_items(items)
    header = journal_header_to_text(raw)
    stream = [e for e in items if e.get("kind") != JOURNAL_KIND_META]
    lines = [
        f"# Journal run Projets {run_id}",
        f"# wrote_projects: false",
        f"# lignes: {len(items)}",
        "",
    ]
    if header:
        lines.extend([header.rstrip(), "", "# --- Récit ---"])
    for e in stream:
        ts = str(e.get("ts") or "")
        clock = ts[11:19] if len(ts) >= 19 else ts
        level = str(e.get("level") or "info").upper()
        kind = e.get("kind") or JOURNAL_KIND_LOG
        prefix = ""
        if kind == JOURNAL_KIND_AGENT:
            aid = e.get("agent") or ""
            engine = e.get("engine") or ""
            prefix = f"[{aid} {engine}] ".replace("  ", " ")
        lines.append(f"{clock} {level:<7} {prefix}{e.get('msg') or ''}".rstrip())
    return "\n".join(lines) + ("\n" if lines else "")


def journal_to_jsonl(items: list[dict], params: dict | None = None) -> str:
    rows = list(items or [])
    if params and not any(e.get("kind") == JOURNAL_KIND_META for e in rows):
        pub = public_run_params(params)
        rows.insert(0, {
            "kind": JOURNAL_KIND_META,
            "level": "info",
            "msg": header_summary(pub),
            "params": pub,
            "profile": pub.get("profile"),
            "hash": pub.get("hash"),
            "wrote_projects": False,
        })
    return "".join(
        json.dumps(e, ensure_ascii=False, default=str) + "\n" for e in rows
    )


async def mongo_insert_journal(db, rec: dict) -> None:
    if db is None or not rec:
        return
    doc = dict(rec)
    doc.setdefault("_id", f"{rec.get('run_id')}:{rec.get('seq')}")
    try:
        await db.project_run_journal.insert_one(doc)
    except Exception as exc:
        logger.warning(
            "journal Mongo insert failed run_id=%s seq=%s: %s",
            rec.get("run_id"), rec.get("seq"), exc,
        )


async def read_journal_mongo(db, run_id: str, *, skip: int = 0,
                              limit: int = JOURNAL_LIMIT_DEFAULT,
                              kind: str | None = None, tail: bool = False) -> dict:
    """Fallback if the file is gone: ``project_run_journal`` collection."""
    rid = safe_run_id(run_id)
    q: dict = {"run_id": rid}
    want = (kind or "").strip().lower() or None
    if want and want != "all":
        q["kind"] = want
    total = await db.project_run_journal.count_documents(q)
    skip = max(0, int(skip or 0))
    limit = max(1, min(JOURNAL_LIMIT_MAX, int(limit or JOURNAL_LIMIT_DEFAULT)))
    if tail:
        skip = max(0, total - limit)
    cur = db.project_run_journal.find(q)
    if hasattr(cur, "sort"):
        cur = cur.sort("seq", 1)
    if hasattr(cur, "skip"):
        cur = cur.skip(skip)
    docs = await cur.to_list(limit)
    items = [_public_entry(d) for d in docs]
    return {
        "run_id": rid,
        "total": total,
        "skip": skip,
        "count": len(items),
        "kind": kind or "all",
        "items": items,
        "wrote_projects": False,
        "source": "mongo",
    }


async def load_journal(db, run_id: str, *, skip: int = 0,
                       limit: int = JOURNAL_LIMIT_DEFAULT,
                       kind: str | None = None, tail: bool = False) -> dict:
    """File first, Mongo next. Never write ``projects``."""
    if _jsonl_paths(run_id):
        return read_journal_file(
            run_id, skip=skip, limit=limit, kind=kind, tail=tail)
    if db is not None:
        try:
            out = await read_journal_mongo(
                db, run_id, skip=skip, limit=limit, kind=kind, tail=tail)
            if out["total"]:
                return out
        except Exception as exc:
            logger.warning("journal mongo fallback failed run_id=%s: %s", run_id, exc)
    return {
        "run_id": run_id,
        "total": 0,
        "skip": 0,
        "count": 0,
        "kind": kind or "all",
        "items": [],
        "wrote_projects": False,
        "source": "empty",
    }
