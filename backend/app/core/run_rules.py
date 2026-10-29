"""Catalog of tunable rules — resolution, snapshot, read.

A number is not an opinion. It is either a law (contract), a
measurement (physical phenomenon), a calibration (labeled set), or a
budget (operator quota). The catalog (`data/run_rules.json`) carries the
default, the interval and the principle. A run may move a rule
*within* the interval; the snapshot freezes what was actually used.

Resolution order: run override > profile > Mongo settings > default.
`kind=loi` rules are recorded but refuse any override.
"""
from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.config import DATA_DIR

CATALOG_FILE = DATA_DIR / "run_rules.json"

_catalog_cache: dict | None = None
_current: ContextVar[dict | None] = ContextVar("run_rules_snapshot", default=None)

MODES = ("projects", "formalities", "marinas", "capitaineries", "amp", "science", "climatology", "shared")
KINDS = ("loi", "geometrie", "score", "budget")
GROUPS = ("identity", "space", "qualification", "geocode", "reading", "budget", "contracts")
PROFILE_ORDER = ("cdc_default", "strict", "recall")


class RuleError(ValueError):
    """Override outside the interval, law touched, or unknown identifier."""


def catalog_path() -> Path:
    return CATALOG_FILE


def load_catalog(*, force: bool = False) -> dict:
    global _catalog_cache
    if _catalog_cache is None or force:
        with CATALOG_FILE.open(encoding="utf-8") as fh:
            _catalog_cache = json.load(fh)
        _validate_catalog(_catalog_cache)
    return _catalog_cache


def _validate_catalog(cat: dict) -> None:
    rules = cat.get("rules")
    if not isinstance(rules, list) or not rules:
        raise RuleError("catalogue vide ou invalide")
    seen: set[str] = set()
    for rule in rules:
        rid = rule.get("id")
        if not rid or rid in seen:
            raise RuleError(f"règle sans id ou doublon: {rid}")
        seen.add(rid)
        if rule.get("mode") not in MODES:
            raise RuleError(f"{rid}: mode inconnu {rule.get('mode')}")
        if rule.get("kind") not in KINDS:
            raise RuleError(f"{rid}: kind inconnu {rule.get('kind')}")
        if rule.get("group") not in GROUPS:
            raise RuleError(f"{rid}: group inconnu {rule.get('group')}")
        title, help_ = rule.get("title") or {}, rule.get("help") or {}
        if not isinstance(title, dict) or not title.get("fr") or not title.get("en"):
            raise RuleError(f"{rid}: title.fr/en requis")
        if not isinstance(help_, dict) or not help_.get("fr") or not help_.get("en"):
            raise RuleError(f"{rid}: help.fr/en requis")
        if rule.get("kind") == "loi" and rule.get("group") != "contracts":
            raise RuleError(f"{rid}: une loi va dans Contrats")
        if rule.get("kind") != "loi":
            _assert_in_interval(rid, rule.get("value"), rule.get("interval"))
        _assert_numeric_matches_value(rid, rule)


def _assert_numeric_matches_value(rid: str, rule: dict) -> None:
    """`numeric` est la seule chose que le client peut lire : `JSON.parse` efface
    la différence entre `15.0` et `15`. On refuse donc tout désaccord avec le type
    littéral du catalogue, que `_coerce` utilise déjà.
    """
    value = rule.get("value")
    num = rule.get("numeric")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if num is not None:
            raise RuleError(f"{rid}: numeric superflu sur une valeur non numérique")
        return
    expected = "float" if isinstance(value, float) else "int"
    if num != expected:
        raise RuleError(f"{rid}: numeric={num!r} ne colle pas à la valeur ({expected})")


def reload_catalog() -> dict:
    return load_catalog(force=True)


def all_rules() -> list[dict]:
    return list(load_catalog()["rules"])


def get_rule_def(rule_id: str) -> dict:
    for rule in all_rules():
        if rule["id"] == rule_id:
            return rule
    raise RuleError(f"règle inconnue: {rule_id}")


def catalog_default(rule_id: str, fallback: Any = None) -> Any:
    try:
        return deepcopy(get_rule_def(rule_id)["value"])
    except RuleError:
        return fallback


def rules_for_mode(mode: str | None = None, *, include_shared: bool = True) -> list[dict]:
    """Rules for a mode (+ shared). `mode=None` → the whole catalog."""
    if mode and mode not in ("projects", "formalities", "marinas", "capitaineries", "amp", "science", "climatology"):
        raise RuleError(f"mode inconnu: {mode}")
    out = []
    for rule in all_rules():
        if mode is None or rule["mode"] == mode or (include_shared and rule["mode"] == "shared"):
            out.append(rule)
    return out


def profiles() -> dict:
    return dict(load_catalog().get("profiles") or {})


def _loc_pair(raw) -> dict:
    """Normalize a bilingual {fr, en} label (legacy string → both languages)."""
    if isinstance(raw, dict):
        fr = str(raw.get("fr") or raw.get("en") or "").strip()
        en = str(raw.get("en") or raw.get("fr") or "").strip()
        return {"fr": fr, "en": en}
    text = str(raw or "").strip()
    return {"fr": text, "en": text}


def _assert_in_interval(rule_id: str, value: Any, interval: Any) -> None:
    if interval is None or value is None:
        return
    if isinstance(value, bool):
        return
    if not isinstance(interval, (list, tuple)) or len(interval) != 2:
        raise RuleError(f"{rule_id}: intervalle invalide")
    lo, hi = interval
    if isinstance(value, (int, float)) and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        if value < lo or value > hi:
            raise RuleError(f"{rule_id}: {value} hors intervalle [{lo}, {hi}]")


def _coerce(rule: dict, raw: Any) -> Any:
    sample = rule.get("value")
    if isinstance(sample, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "1", "yes", "oui"):
            return True
        if isinstance(raw, str) and raw.lower() in ("false", "0", "no", "non"):
            return False
        raise RuleError(f"{rule['id']}: booléen attendu")
    if isinstance(sample, int) and not isinstance(sample, bool):
        return int(raw)
    if isinstance(sample, float):
        return float(raw)
    if isinstance(sample, str):
        return str(raw)
    return raw


def _source_and_value(rule: dict, settings: dict, profile_vals: dict, overrides: dict) -> tuple[Any, str]:
    rid = rule["id"]
    if rule.get("kind") == "loi" or rule.get("vary") is False:
        if rid in overrides:
            raise RuleError(f"{rid}: règle de loi / non modulable")
        return deepcopy(rule["value"]), "loi" if rule.get("kind") == "loi" else "catalog"
    if rid in overrides:
        val = _coerce(rule, overrides[rid])
        _assert_in_interval(rid, val, rule.get("interval"))
        return val, "override"
    if rid in profile_vals:
        val = _coerce(rule, profile_vals[rid])
        _assert_in_interval(rid, val, rule.get("interval"))
        return val, "profile"
    sk = rule.get("settings_key")
    if sk and settings and settings.get(sk) is not None:
        val = _coerce(rule, settings[sk])
        _assert_in_interval(rid, val, rule.get("interval"))
        return val, "settings"
    return deepcopy(rule["value"]), "catalog"


def resolve_rules(*, mode: str | None = None, settings: dict | None = None,
                  overrides: dict | None = None, profile: str | None = None,
                  include_shared: bool = True) -> dict:
    """Resolve effective values for a mode (or the whole catalog)."""
    cat = load_catalog()
    settings = settings or {}
    overrides = dict(overrides or {})
    profile_name = profile or "cdc_default"
    known = profiles()
    if profile_name not in known:
        raise RuleError(f"profil inconnu: {profile_name}")
    profile_vals = dict(known[profile_name].get("overrides") or {})

    unknown = [k for k in overrides if k not in {r["id"] for r in all_rules()}]
    if unknown:
        raise RuleError(f"règles inconnues: {', '.join(sorted(unknown))}")

    chosen: dict[str, dict] = {}
    for rule in rules_for_mode(mode, include_shared=include_shared):
        value, source = _source_and_value(rule, settings, profile_vals, overrides)
        chosen[rule["id"]] = {
            "value": value,
            "unit": rule.get("unit"),
            "kind": rule["kind"],
            "source": source,
            "vary": bool(rule.get("vary")),
            "interval": rule.get("interval"),
            "principle": rule.get("principle"),
            "cdc": rule.get("cdc"),
        }
    return {
        "catalog_version": cat.get("version"),
        "profile": profile_name,
        "mode": mode,
        "chosen": chosen,
        "overrides": {k: overrides[k] for k in overrides if k in chosen},
    }


def rules_hash(chosen: dict) -> str:
    """Stable fingerprint of the values (no principles or secrets)."""
    payload = {k: chosen[k]["value"] for k in sorted(chosen)}
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def snapshot_for_run(*, mode: str | None, settings: dict | None = None,
                     overrides: dict | None = None, profile: str | None = None) -> dict:
    """Snapshot recordable in `params.rules` — no secrets."""
    resolved = resolve_rules(mode=mode, settings=settings,
                             overrides=overrides, profile=profile)
    chosen = resolved["chosen"]
    resolved["hash"] = rules_hash(chosen)
    resolved["counts"] = {
        "total": len(chosen),
        "overridden": sum(1 for c in chosen.values() if c["source"] == "override"),
        "from_profile": sum(1 for c in chosen.values() if c["source"] == "profile"),
        "from_settings": sum(1 for c in chosen.values() if c["source"] == "settings"),
        "loi": sum(1 for c in chosen.values() if c["kind"] == "loi"),
    }
    return resolved


def public_catalog(mode: str | None = None) -> dict:
    """API view: principle + profiles + rules (no secrets)."""
    cat = load_catalog()
    known = profiles()
    ordered = [name for name in PROFILE_ORDER if name in known]
    ordered += [name for name in known if name not in PROFILE_ORDER]
    return {
        "version": cat.get("version"),
        "updated": cat.get("updated"),
        "principle": cat.get("principle"),
        "kinds": cat.get("kinds"),
        "groups": cat.get("groups") or [
            {"id": g, "fr": g, "en": g} for g in GROUPS
        ],
        "identity_banner": cat.get("identity_banner") or {},
        "profiles": {
            name: {
                "label": _loc_pair(known[name].get("label")),
                "hint": _loc_pair(known[name].get("hint") or known[name].get("label")),
                "overrides": known[name].get("overrides") or {},
            }
            for name in ordered
        },
        "rules": [
            {
                "id": r["id"],
                "mode": r["mode"],
                "kind": r["kind"],
                "group": r.get("group"),
                "value": r["value"],
                "numeric": r.get("numeric"),
                "unit": r.get("unit"),
                "interval": r.get("interval"),
                "vary": bool(r.get("vary")),
                "settings_key": r.get("settings_key"),
                "title": _loc_pair(r.get("title")),
                "help": _loc_pair(r.get("help") or r.get("principle")),
                "principle": r.get("principle"),
                "cdc": r.get("cdc"),
                "code": r.get("code") or [],
                "legacy": bool(r.get("legacy")),
            }
            for r in rules_for_mode(mode)
        ],
    }


def bind_rules(snapshot: dict | None):
    """Activate the snapshot for the current thread/async context."""
    return _current.set(snapshot)


def reset_rules(token) -> None:
    _current.reset(token)


def current_snapshot() -> dict | None:
    return _current.get()


def get_rule(rule_id: str, fallback: Any = None) -> Any:
    """Effective value: run snapshot if it exists, otherwise catalog default."""
    snap = _current.get()
    if snap:
        chosen = snap.get("chosen") or {}
        if rule_id in chosen:
            return chosen[rule_id]["value"]
    return catalog_default(rule_id, fallback)


def attach_rules(params: dict, snapshot: dict) -> dict:
    """Put the snapshot under `params.rules` without overwriting `code` / variant."""
    out = dict(params)
    out["rules"] = snapshot
    return out


def snapshot_list_fields(params: dict | None) -> dict:
    """Lean fields for a run list: profile, hash, counts."""
    rules = (params or {}).get("rules") or {}
    if not isinstance(rules, dict):
        rules = {}
    digest = str(rules.get("hash") or "")
    return {
        "profile": rules.get("profile") or (params or {}).get("profile"),
        "hash": digest,
        "hash8": digest[:8] if digest else None,
        "counts": rules.get("counts"),
    }
