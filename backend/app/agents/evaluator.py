"""LangGraph checking node: score each PoE and decide pass / flag / retry.

After evaluate(), route "retry" through prepare_retry() before the retry step
and re-evaluation. Route "done" through finalise() before returning the ports.
The graph must preserve the full ports list when enriching retry candidates.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from app.services.poe_confidence import apply_score, score_port
from app.core import paths

AGENT_NAME = "EvaluatorAgent"
RETRY_LIMIT = 2
DEFAULT_TRACE_LOG_DIR = paths.LOGS_DIR

_REASONS_EN = {
    "source d'État": "government source",
    "synthèse web sans page": "web synthesis without a source page",
    "source nationale non whitelistée": "national source not on the allowlist",
    "source indéterminée": "unknown source",
    "Noonsite : signal, jamais une source Gold":
        "Noonsite: signal only, never a Gold source",
    "Claude et OpenRouter d'accord": "Claude and OpenRouter agree",
    "catalogue + LLM + NER": "catalogue + LLM + NER",
    "catalogue officiel": "official catalogue",
    "LLM ∩ NER ∩ Claude": "LLM, NER and Claude agree",
    "LLM et NER d'accord": "LLM and NER agree",
    "deux lecteurs LLM": "two LLM readers",
    "NER seul": "NER only",
    "tournure légale": "legal wording",
    "un seul extracteur": "single extractor",
    "non géocodé": "not geocoded",
    "hors ZEE et hors bord terrestre": "outside the EEZ and its land boundary",
    "dans la ZEE, géocodeurs d'accord": "inside the EEZ, geocoders agree",
    "bord terrestre de la ZEE, géocodeurs d'accord": "EEZ land boundary, geocoders agree",
    "port fluvial du pays, géocodeurs d'accord": "domestic river port, geocoders agree",
    "dans la ZEE": "inside the EEZ",
    "bord terrestre de la ZEE": "EEZ land boundary",
    "port fluvial hors ZEE (exception)": "river port outside the EEZ (exception)",
    "point accepté dans la zone": "position accepted within the zone",
    "position incertaine": "uncertain position",
    "connu du listing communautaire (PoE)": "known in the community listing (PoE)",
    "listing : autre port (pas un PoE)": "listing: other port (not a PoE)",
    "OSM : infrastructure portuaire / douane": "OSM: port infrastructure / customs",
    "OSM : correspondance faible": "OSM: weak match",
    "OSM : aucun tag proche": "OSM: no nearby tags",
    "vu dans plusieurs runs": "seen in multiple runs",
    "WPI commerce (contre-liste, pas une preuve PoE)":
        "WPI commercial port (cross-check list, not proof of PoE status)",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reasons_en(reasons: list[str]) -> list[str]:
    return [_REASONS_EN.get(reason, reason) for reason in reasons]


def _with_run_id(state: dict) -> dict:
    """Copy state, generating a UUID if absent; require a filename-safe run ID."""
    out = dict(state)
    run_id = out.get("run_id")
    if run_id is None:
        run_id = str(uuid4())
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]+", run_id) is None:
        raise ValueError("run_id must contain only letters, digits, underscores or hyphens")
    out["run_id"] = run_id
    return out


def _trace_port(port: dict, retry_count: int, action: str,
                *, run_id: str, handoff: str | None = None) -> dict:
    """Build an event; the node writes its batch after processing all ports."""
    entry = {
        "timestamp": _now_iso(),
        "agent_name": AGENT_NAME,
        "run_id": run_id,
        "action": action,
        "port_name": port.get("name"),
        "input_summary": {"port_name": port.get("name"), "retry_count": retry_count},
        "verdict": port.get("verdict"),
        "reasons": _reasons_en(
            list(port.get("confidence_reasons") or []) + list(port.get("check_reasons") or [])
        ),
    }
    if port.get("retry_hint"):
        entry["retry_hint"] = port["retry_hint"]
    if port.get("geocode_corroboration"):
        entry["geocode_corroboration"] = dict(port["geocode_corroboration"])
    if handoff is not None:
        entry["handoff"] = {"from": AGENT_NAME, "to": handoff}
    return entry


def _open_trace_file(path: Path):
    """Open the trace stream; isolated seam for testing file-open batching."""
    return path.open("a", encoding="utf-8")


def _write_trace(entries: list[dict], run_id: str) -> None:
    """Append a node's new events with one open; propagate logging failures."""
    if not entries:
        return
    payload = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
    log_path = Path(os.environ.get("TRACE_LOG_PATH") or
                    DEFAULT_TRACE_LOG_DIR / f"trace-{run_id}.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _open_trace_file(log_path) as log_file:
        log_file.write(payload)
    if os.environ.get("TRACE_STDOUT", "1") != "0":
        print(payload, end="", flush=True)


def is_official_source(urls: list[str | None] | None) -> bool:
    """Recognize HTTP(S) hosts ending in .gov or a government label + ccTLD.

    Match exact labels gouv/gov/gob/gc/govt/go immediately before a two-letter
    country suffix, never paths, queries or arbitrary labels further left.
    This intentionally narrows the production substring check in osm_validate.py
    / enrich_ci_osm.py to remove false positives. For gouv/gov/gob it is a strict
    subset apart from case normalization; requested gc/go domains add coverage
    that production does not have. This is a naming heuristic, not verification
    of domain ownership or an exhaustive government-domain allowlist.
    """
    for url in urls or []:
        if not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https") or not hostname:
            continue
        labels = hostname.removesuffix(".").split(".")
        if len(labels) < 2 or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
            for label in labels
        ):
            continue
        if labels[-1] == "gov" or (
            labels[-2] in {"gouv", "gov", "gob", "gc", "govt", "go"}
            and re.fullmatch(r"[a-z]{2}", labels[-1]) is not None
        ):
            return True
    return False


def _has_real_source(port: dict) -> bool:
    urls = [u for u in (port.get("source_urls") or []) if u]
    return bool(urls) and not bool(port.get("from_synthesis"))


def _official_evidence_urls(port: dict) -> list[str]:
    """Official URLs whose text designates the port (registry.official_evidence, tiers 1-2).

    A registry match alone (tier_registered) adds nothing here: UN/LOCODE and the World
    Port Index list ports, not clearance status.
    """
    evidence = (port.get("registry") or {}).get("official_evidence") or []
    return [e["url"] for e in evidence if isinstance(e, dict) and is_official_source([e.get("url")])]


def _map_check(port: dict) -> tuple[bool, list[str]]:
    if port.get("lat") is None or port.get("lon") is None:
        return False, ["not geocoded"]
    if port.get("geocode_agree") is False:
        return False, ["geocoders disagree"]
    if port.get("geocode_agree") is None:
        return True, ["single geocoder, not cross-checked"]
    return True, []


_REGISTRY_NAMES = {"wpi": "WPI", "unlocode": "UN/LOCODE"}


def _registry_geocode_corroboration(port: dict) -> dict | None:
    """A registry entry within GEOCODE_AGREE_KM of the port's position, as a second geocode.

    Only for ports with official evidence (tiers 1-2) and a single, unchecked geocode: the
    registry's published position is independent of the seed geocoder. WPI is preferred,
    since UN/LOCODE positions are rounded to the minute (~1.8 km).
    """
    from app.core.geo import GEOCODE_AGREE_KM
    if (not _official_evidence_urls(port) or port.get("geocode_agree") is not None
            or port.get("lat") is None or port.get("lon") is None):
        return None
    close = [m for m in (port.get("registry") or {}).get("matches") or []
             if isinstance(m, dict) and m.get("distance_km") is not None
             and m["distance_km"] <= GEOCODE_AGREE_KM]
    if not close:
        return None
    best = min(close, key=lambda m: (m.get("source") != "wpi", m["distance_km"]))
    return dict(source=best.get("source"), code=best.get("code"), distance_km=best["distance_km"])


def _corroboration_reason(c: dict) -> str:
    return (f"coordinates corroborated by {_REGISTRY_NAMES.get(c.get('source'), c.get('source'))} "
            f"{c.get('code')}, {c['distance_km']:g} km")


def _verdict(confidence: int, checks_ok: bool, retry_hint: str | None) -> str:
    if confidence < 40:
        return "retry" if retry_hint else "flag"
    if confidence > 60 and checks_ok:
        return "pass"
    return "flag"


def evaluate(state: dict) -> dict:
    """Annotate ports, fill needs_retry and emit one trace dict per port.

    Older callers should read confidence_reasons / check_reasons / reasons_en
    instead of the removed legacy port field reasons.
    """
    out = _with_run_id(state)
    ports_in = list(state.get("ports") or [])
    retry_count = int(state.get("retry_count") or 0)
    trace = list(state.get("trace") or [])
    entries = []
    out_ports: list[dict] = []
    needs_retry: list[dict] = []

    for raw in ports_in:
        port = dict(raw)
        # Remove the legacy mixed reasons field; the three explicit fields replace it.
        port.pop("reasons", None)
        port.pop("retry_hint", None)
        # An official page that designates the port (registry evidence, fetched now or a
        # stored snippet) is an official, direct source even when the seed carries no URL.
        evidence_urls = _official_evidence_urls(port)
        official = is_official_source(port.get("source_urls")) or bool(evidence_urls)
        corroboration = _registry_geocode_corroboration(port)
        if corroboration:
            port["geocode_agree"] = True
            port["agreement_km"] = corroboration["distance_km"]
            port["geocode_corroboration"] = corroboration
        scored = score_port(port, official_source=official, listing_ports=state.get("listing_ports"))
        apply_score(port, scored)
        confidence = int(port["confidence"])
        source_ok = _has_real_source(port) or bool(evidence_urls)
        map_ok, map_reasons = _map_check(port)
        checks_ok = source_ok and map_ok
        check_reasons = []
        if not source_ok:
            check_reasons.append("no direct source (empty URLs or synthesis only)")
        check_reasons.extend(map_reasons)
        if port.get("geocode_corroboration"):
            check_reasons.append(_corroboration_reason(port["geocode_corroboration"]))
        # Non-official sources receive fewer source points in score_port.
        # Prefer improving the source if both source and coordinates need work.
        retry_hint = None
        if not source_ok or not official:
            retry_hint = "find an official source"
        elif port.get("lat") is None or port.get("lon") is None:
            retry_hint = "geocode the port"
        verdict = _verdict(confidence, checks_ok, retry_hint)
        port["verdict"] = verdict
        port["check_reasons"] = check_reasons
        port["reasons_en"] = _reasons_en(port["confidence_reasons"] + check_reasons)
        out_ports.append(port)
        if verdict == "retry":
            port["retry_hint"] = retry_hint
            needs_retry.append(port)
        entries.append(_trace_port(port, retry_count, "evaluate_port", run_id=out["run_id"]))

    _write_trace(entries, out["run_id"])
    trace.extend(entries)
    out["ports"] = out_ports
    out["retry_count"] = retry_count
    out["needs_retry"] = needs_retry
    out["trace"] = trace
    return out


def route_after_evaluation(state: dict) -> str:
    """Route 'retry' to prepare_retry(), or 'done' to finalise()."""
    needs_retry = state.get("needs_retry") or []
    retry_count = int(state.get("retry_count") or 0)
    if needs_retry and retry_count < RETRY_LIMIT:
        return "retry"
    return "done"


def prepare_retry(state: dict) -> dict:
    """On the 'retry' route, increment the count before handing off to that step."""
    out = _with_run_id(state)
    out["retry_count"] = int(state.get("retry_count") or 0) + 1
    trace = list(state.get("trace") or [])
    entries = []
    for port in state.get("needs_retry") or []:
        entries.append(_trace_port(port, out["retry_count"], "prepare_retry",
                                   run_id=out["run_id"], handoff="retry"))
    _write_trace(entries, out["run_id"])
    trace.extend(entries)
    out["trace"] = trace
    return out


def finalise(state: dict) -> dict:
    """Call when route_after_evaluation() returns 'done'; retain every port.

    Any remaining retry verdict becomes flag, with the English reason
    'unconfirmed after max retries' in check_reasons and reasons_en.
    """
    out = _with_run_id(state)
    retry_count = int(state.get("retry_count") or 0)
    trace = list(state.get("trace") or [])
    entries = []
    ports = []
    for raw in state.get("ports") or []:
        port = dict(raw)
        # Also remove legacy mixed reasons when finalising an older caller's state.
        port.pop("reasons", None)
        if port.get("verdict") == "retry":
            port["verdict"] = "flag"
            port.pop("retry_hint", None)
            port["check_reasons"] = list(port.get("check_reasons") or []) + [
                "unconfirmed after max retries",
            ]
            port["reasons_en"] = _reasons_en(
                list(port.get("confidence_reasons") or []) + port["check_reasons"]
            )
            entries.append(_trace_port(port, retry_count, "finalise_port", run_id=out["run_id"]))
        ports.append(port)
    _write_trace(entries, out["run_id"])
    trace.extend(entries)
    out["ports"] = ports
    out["needs_retry"] = []
    out["trace"] = trace
    return out
