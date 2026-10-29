"""Judge: is this mention a navigation rule?

Business: yes / no + citation. Not a pixel alignment, not a depth.
Acceptance fixtures go through the heuristic (no LLM).
"""
from __future__ import annotations

import re

from app.core.judge import as_bool, complete_json_cascade

_NAV_TRUE = re.compile(
    r"\b(vhf|canal|channel|interdiction|no[\s-]?anchor|mouill"
    r"|port d['’]entr|tirant|draft|draught|harbour.?master)\b",
    re.I,
)
_NAV_FALSE = re.compile(
    r"\b(restaurant|vue mer|sea view|touris|h[oô]tel|piscine|pool|brunch)\b",
    re.I,
)

_SYSTEM = (
    "You judge whether a short quote is a navigation rule "
    "(VHF channel, no-anchoring, port of entry, draught) "
    "or tourism / amenities. Reply JSON only: "
    '{"navigation_rule": true|false, "quote": "<short excerpt>"}.'
)


def heuristic_navigation_rule(text: str) -> bool:
    blob = (text or "").strip()
    if not blob:
        return False
    if _NAV_TRUE.search(blob):
        return True
    if _NAV_FALSE.search(blob):
        return False
    return False


def parse_navigation_rule(data: dict | None) -> dict:
    data = data if isinstance(data, dict) else {}
    flag = as_bool(data.get("navigation_rule"))
    quote = str(data.get("quote") or "")[:240]
    return {"navigation_rule": bool(flag), "quote": quote}


def should_write_vhf(evidence_text: str | None, extracted_vhf) -> bool:
    """Write a channel only if the mention is a rule (C16)."""
    if extracted_vhf in (None, "", "null", "None"):
        return False
    return heuristic_navigation_rule(evidence_text or "")


def apply_nav_rule_gate(payload: dict | None, evidence_text: str = "") -> dict:
    out = dict(payload or {})
    if out.get("canal_vhf") and not should_write_vhf(evidence_text, out.get("canal_vhf")):
        out["canal_vhf"] = None
    return out


async def judge_navigation_rule(
    text: str,
    settings: dict | None = None,
    *,
    use_llm: bool = False,
) -> dict:
    heur = heuristic_navigation_rule(text)
    quote = (text or "").strip()[:240]
    if not use_llm:
        return {"navigation_rule": heur, "quote": quote, "engine": "heuristic"}
    try:
        data, engine = await complete_json_cascade(
            _SYSTEM,
            f"Text:\n{(text or '')[:2000]}\n",
            settings,
            role="judge",
            max_tokens=200,
        )
        parsed = parse_navigation_rule(data)
        if not parsed.get("quote"):
            parsed["quote"] = quote
        parsed["engine"] = engine
        return parsed
    except Exception:
        return {"navigation_rule": heur, "quote": quote, "engine": "heuristic"}
