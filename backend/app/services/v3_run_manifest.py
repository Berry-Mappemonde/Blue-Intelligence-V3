"""Immutable run directory: runs/<run_id>/manifest.json.

run_id = UTC timestamp + code hash + rules hash. The manifest lists every
artifact written in the directory with its SHA-256. An existing run
directory is never overwritten.

No secret is read: the code hash covers ``.py`` sources only, and the
manifest stores rules, counts and digests. ``V3_RUNS_DIR`` overrides the
default directory; when it is absent, ``backend/data/v3/runs`` is used.
Nothing is created at import time.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import time
from pathlib import Path

from app.services.v3_certify import LEVELS, confidence_level, level_from_v2

RULES = {
    "dedup_km": 0.5,
    "geo_agree_km": 0.5,
    "moved_km": 2.0,
    "levels": "C0-C4 v1",
}

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_CODE_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_RUNS_DIR = _BACKEND_DIR / "data" / "v3" / "runs"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def default_runs_dir() -> Path:
    """Parent of ``runs/<run_id>/``. Env override is optional."""
    raw = os.environ.get("V3_RUNS_DIR")
    if raw is not None and raw.strip():
        return Path(raw.strip())
    return _DEFAULT_RUNS_DIR


def code_fingerprint(root: str | os.PathLike | None = None) -> str:
    """First 12 hex chars of SHA-256 over ``.py`` sources under `root`.

    Default root is ``backend/app``. Paths are part of the digest so two
    files with the same bytes still differ. ``__pycache__`` is skipped.
    """
    root_path = Path(root) if root is not None else _DEFAULT_CODE_ROOT
    digest = hashlib.sha256()
    if root_path.is_file() and root_path.suffix == ".py":
        files = [root_path]
        base = root_path.parent
    else:
        base = root_path
        files = sorted(
            (p for p in base.rglob("*.py")
             if p.is_file() and "__pycache__" not in p.parts),
            key=lambda p: p.relative_to(base).as_posix(),
        )
    for path in files:
        digest.update(path.relative_to(base).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def rules_fingerprint(rules: dict | None = None) -> str:
    """First 8 hex chars of the canonical rules JSON."""
    payload = RULES if rules is None else rules
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:8]


def _timestamp(now) -> str:
    if isinstance(now, time.struct_time):
        return time.strftime("%Y%m%dT%H%M%S", now)
    if now is None:
        return time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))


def new_run_id(root: str | os.PathLike | None = None, rules: dict | None = None,
               *, now=None) -> str:
    """``YYYYMMDDTHHMMSS-c{code12}-r{rules8}`` in UTC."""
    return f"{_timestamp(now)}-c{code_fingerprint(root)}-r{rules_fingerprint(rules)}"


def _stated_level(entity: dict) -> str:
    for key in ("level", "confidence_level"):
        val = entity.get(key)
        if val in LEVELS:
            return val
    if isinstance(entity.get("evidence"), list):
        return confidence_level(entity)
    return level_from_v2(entity)


def _counts(entities: list[dict]) -> dict:
    counts: dict = {}
    for entity in entities:
        layer = entity.get("layer") or "poe"
        lvl = _stated_level(entity)
        bucket = counts.setdefault(layer, {})
        bucket[lvl] = bucket.get(lvl, 0) + 1
    return counts


def to_geojson(entities: list[dict]) -> dict:
    features = []
    for entity in entities:
        lat, lon = entity.get("lat"), entity.get("lon")
        if lat is None or lon is None:
            geometry = None
        else:
            geometry = {"type": "Point", "coordinates": [lon, lat]}
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {k: v for k, v in entity.items() if k not in ("lat", "lon")},
        })
    return {"type": "FeatureCollection", "features": features}


def from_geojson(path: str | os.PathLike) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for feat in data.get("features") or []:
        entity = dict(feat.get("properties") or {})
        geometry = feat.get("geometry")
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if coords:
            entity["lon"], entity["lat"] = coords[0], coords[1]
        else:
            entity["lat"], entity["lon"] = None, None
        out.append(entity)
    return out


def _check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must be a single path segment")
    return run_id


def _artifact_name(name: str) -> str:
    if name == "manifest.json" or not isinstance(name, str) or not _ARTIFACT_RE.fullmatch(name):
        raise ValueError(f"invalid artifact name: {name!r}")
    return name


def _as_bytes(content: str | bytes) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    return bytes(content)


def write_run(runs_dir, run_id: str, artifacts: dict | None = None,
              meta: dict | None = None, *, entities: list[dict] | None = None,
              rules: dict | None = None) -> tuple[str, dict]:
    """Write ``runs_dir/run_id/`` and return ``(directory, manifest)``.

    `artifacts` maps a file name to text or bytes. `entities` are also
    written as ``entities.geojson``. Every file except ``manifest.json``
    is listed under ``sha256``. Raises FileExistsError if the run
    directory already exists.
    """
    run_id = _check_run_id(run_id)
    dest = Path(runs_dir) / run_id
    if dest.exists():
        raise FileExistsError("runs are immutable: " + run_id)

    rules_doc = RULES if rules is None else rules
    files: dict[str, bytes] = {}
    if entities is not None:
        files["entities.geojson"] = json.dumps(
            to_geojson(entities), ensure_ascii=False, indent=1,
        ).encode("utf-8")
    for name, content in (artifacts or {}).items():
        checked = _artifact_name(name)
        if checked in files:
            raise ValueError(f"duplicate artifact: {checked}")
        files[checked] = _as_bytes(content)

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.mkdir(exist_ok=False)
    except FileExistsError:
        raise FileExistsError("runs are immutable: " + run_id) from None

    hashes = {}
    for name in sorted(files):
        blob = files[name]
        (dest / name).write_bytes(blob)
        hashes[name] = hashlib.sha256(blob).hexdigest()

    manifest = {
        "run_id": run_id,
        "rules": rules_doc,
        "python": platform.python_version(),
    }
    if entities is not None:
        manifest["entities"] = len(entities)
        manifest["counts"] = _counts(entities)
    manifest["sha256"] = hashes
    for key, value in (meta or {}).items():
        if key in ("run_id", "sha256"):
            continue
        manifest[key] = value
    (dest / "manifest.json").write_bytes(
        json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))
    return str(dest), manifest
