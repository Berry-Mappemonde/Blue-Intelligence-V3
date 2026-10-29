"""Code fingerprint of a PoE run — ties poe_runs.params to a commit and flags.

No secret: only booleans / SHA / caps. Already
finished runs are not retro-labeled.
"""
from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _module_has(mod: str, name: str) -> bool:
    try:
        m = importlib.import_module(mod)
        return hasattr(m, name)
    except Exception:
        return False


def git_state(cwd: Path | None = None) -> tuple[str | None, bool | None]:
    """Return (sha, dirty). (None, None) if git is unavailable."""
    root = str(cwd or _REPO_ROOT)
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip()
    except Exception:
        return None, None
    try:
        dirty_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=3,
        )
        dirty = bool(dirty_out.strip())
    except Exception:
        dirty = None
    return sha or None, dirty


def _key_configured(*candidates: str) -> bool:
    return any(bool((c or "").strip()) for c in candidates)


def build_code_fingerprint(settings: dict | None = None,
                           zone_timeout_s: int | None = None) -> dict:
    """Snapshot of the code and flags (not secrets) at run time."""
    from app.core import claude

    s = settings or {}
    sha, dirty = git_state()
    timeout = 900 if zone_timeout_s is None else zone_timeout_s

    from app.core import nvidia, tokenfactory

    env_or = os.environ.get("OPENROUTER_API_KEY") or ""
    env_nv = os.environ.get("NVIDIA_API_KEY") or ""
    env_tf = os.environ.get("TINYFISH_API_KEY") or ""
    env_serper = os.environ.get("SERPER_API_KEY") or ""
    env_searx = (os.environ.get("SEARXNG_URL") or "").strip().rstrip("/") or None

    return {
        "git_sha": sha,
        "git_dirty": dirty,
        "claude_enabled": claude.claude_enabled(s),
        "claude_allows_call": claude.budget_allows_call(s),
        "claude_model": claude.CLAUDE_MODEL,
        "claude_budget_usd": claude.get_claude_budget_usd(s),
        "nvidia_configured": _key_configured(
            s.get("nvidia_api_key"), env_nv),
        "nvidia_enabled": nvidia.nvidia_enabled(s),
        "nvidia_model": nvidia.primary_model() if nvidia.nvidia_enabled(s) else None,
        "nvidia_judge_chain": list(nvidia.models_for("judge")) if nvidia.nvidia_enabled(s) else None,
        "nvidia_page_chain": list(nvidia.models_for("page")) if nvidia.nvidia_enabled(s) else None,
        "tokenfactory_configured": bool(tokenfactory.get_key()),
        "tokenfactory_enabled": tokenfactory.active(s),
        "tokenfactory_budget_usd": tokenfactory.budget_usd(),
        "tokenfactory_judge_model": tokenfactory.routed_model("judge", settings=s),
        "tokenfactory_page_model": tokenfactory.routed_model("page", settings=s),
        "openrouter_configured": _key_configured(
            s.get("openrouter_api_key"), env_or),
        "tinyfish_configured": _key_configured(
            s.get("tinyfish_api_key"), env_tf),
        "serper_configured": _key_configured(
            s.get("serper_api_key"), env_serper),
        "searxng_url": env_searx,
        "catalog_skip": True,
        "zone_timeout_s": timeout,
        "features": {
            "p0_pdf_isolated": _module_has("app.core.pdf_worker", "extract_pdf_file"),
            "p0_geocode_cache": _module_has("app.core.geo", "geocode_cache_id"),
            "p0_exceptions_lock": _module_has(
                "app.services.poe_pipeline", "save_exceptions"),
            "listing_control": _module_has(
                "app.services.listing_control", "compare_to_listing"),
            "claude_adapter": _module_has("app.core.claude", "extract_ports_claude"),
            "haiku_source_coords": _module_has("app.core.llm", "coords_appear_in_text"),
            "geocode_name_veto": _module_has("app.core.extract", "is_geocodeable_name"),
            "haiku_geocode_tiebreak": _module_has(
                "app.core.claude", "arbitrate_geocode_claude"),
            "geocode_name": _module_has("app.core.geo", "geocode_name"),
            "llm_geocode_port": _module_has("app.core.llm", "llm_geocode_port"),
            "seed_union": _module_has("app.services.poe_seeds", "build_seed_union"),
            "seed_verify": _module_has("app.services.poe_seeds", "verdict_for_seed"),
            "seed_osm": _module_has("app.services.osm_seeds", "refresh_osm_cache"),
            "seed_enrich": _module_has("app.services.poe_seed_enrich", "execute_enrich"),
            "seed_mine_sources": _module_has(
                "app.services.poe_seed_enrich", "mine_paid_sources"),
            "tinyfish_poe_agent": _module_has("app.core.tinyfish", "tf_poe_agent"),
            "claude_sonnet_judge": _module_has("app.core.claude", "complete_json_claude"),
            "seed_database": _module_has(
                "app.services.poe_seeds", "persist_seed_database"),
            "seed_search_query": _module_has(
                "app.services.poe_seeds", "seed_search_query"),
            "wpi_counterlist": _module_has(
                "app.services.wpi_ports", "load_wpi_ports"),
            "nvidia_adapter": _module_has(
                "app.core.nvidia", "complete_json_nvidia"),
            "tokenfactory_adapter": _module_has(
                "app.core.tokenfactory", "complete_json"),
        },
    }


def merge_run_params(base: dict, fingerprint: dict) -> dict:
    """Merge the fingerprint under params.code without overwriting variant / label."""
    out = dict(base)
    out["code"] = fingerprint
    return out


def resume_params(existing: dict, current_fingerprint: dict) -> dict:
    """Keep the original fingerprint; note the resume SHA if it differs."""
    params = dict(existing or {})
    orig = params.get("code") if isinstance(params.get("code"), dict) else {}
    orig_sha = orig.get("git_sha")
    now_sha = current_fingerprint.get("git_sha")
    params["resumed"] = True
    if orig_sha and now_sha and orig_sha != now_sha:
        params["resume_git_sha"] = now_sha
    elif not orig:
        params["code"] = current_fingerprint
    return params
