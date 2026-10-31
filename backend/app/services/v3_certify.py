"""Certification levels C0..C4.

Same ladder as the V3 prototype (`certify.level`): a level never rests on an
LLM-only point above C1. C1 needs an extracted source on a whitelisted
State host, C2 a non-LLM corroboration or a NemotronJudge "poe" verdict
with a verified excerpt (a "not_poe" verdict caps the level at C1), C3 two geocoders
within 500 m, at sea or at the quay, C4 a human confirmation on top of C3.

`confidence_level` applies that ladder after a verified-quote check: the
quoted evidence must literally appear in the fetched page (whitespace and
case normalized). Otherwise that evidence is not extracted and the level
is recomputed — it can only fall.

`level_from_v2` maps a port already scored by `poe_confidence` onto the
same scale. The numeric 0–100 score is left untouched.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

LEVELS = ("C0", "C1", "C2", "C3", "C4")
_RANK = {name: i for i, name in enumerate(LEVELS)}

GEO_AGREE_KM = 0.5
GEO_KINDS = frozenset({"in_eez", "coastal_land"})
_WS = re.compile(r"\s+")


def is_official(url: str, whitelist: Sequence[str] | None) -> bool:
    """Official = the URL's host is on the zone's State whitelist.

    The whitelist is the pipeline's own (`poe_pipeline.build_whitelist`,
    matched with `url_allowed`): host only, never path or query, and no
    name-token or keyword heuristic. No whitelist, nothing is official.
    """
    if not isinstance(url, str) or not url.strip() or not whitelist:
        return False
    from app.services.poe_pipeline import url_allowed

    return url_allowed(url, list(whitelist))


def _evidence(entity: dict) -> list[dict]:
    return [e for e in (entity.get("evidence") or []) if isinstance(e, dict)]


def level(entity: dict) -> str:
    """C0–C4 ladder on resolved evidence (`official` already set from the
    whitelist, see `with_official`). Does not look at quote text — see
    `confidence_level`."""
    ev = _evidence(entity)
    if not ev:
        return "C0"
    lvl = "C0"
    if any(e.get("extracted") and e.get("official") is True for e in ev):
        lvl = "C1"
    if lvl == "C1" and any(e.get("judge") == "supported" for e in ev):
        lvl = "C2"
    g = entity.get("geo") if isinstance(entity.get("geo"), dict) else {}
    if lvl == "C2" and _geo_agrees(g):
        lvl = "C3"
    if lvl == "C3" and entity.get("human") == "confirmed":
        lvl = "C4"
    return lvl


def _geo_agrees(geo: dict) -> bool:
    """PLAN-V3 §5 C3 gate: two independent geocoders within GEO_AGREE_KM of
    each other, and the point at sea or at the quay (land mask).

    The pipeline's own `geocode_agree` flag uses a 2 km tolerance
    (core.geo.GEOCODE_AGREE_KM) and is not enough on its own.
    """
    if geo.get("agree") is not True or not _enough_sources(geo):
        return False
    km = geo.get("agreement_km")
    if isinstance(km, bool) or not isinstance(km, (int, float)) or km < 0:
        return False
    if km > GEO_AGREE_KM:
        return False
    return geo.get("spatial_kind") in GEO_KINDS


def _enough_sources(geo: dict) -> bool:
    raw = geo.get("independent_sources", 0)
    if isinstance(raw, bool) or raw is None:
        return False
    try:
        return float(raw) >= 2
    except (TypeError, ValueError):
        return False


def normalize_quote(text: str) -> str:
    """Collapse whitespace and case-fold. Punctuation stays literal."""
    if not isinstance(text, str):
        return ""
    return _WS.sub(" ", text).strip().casefold()


def verified_quote(quote: str, page_text: str) -> bool:
    """True when the whole quote appears in the page after normalization.

    An empty quote is not verified. A matching prefix is not enough: the
    full quote has to be in the page.
    """
    needle = normalize_quote(quote)
    if not needle:
        return False
    return needle in normalize_quote(page_text)


def _page_for(evidence: dict, entity: dict, pages: Mapping[str, str] | None) -> str:
    url = evidence.get("url") if isinstance(evidence.get("url"), str) else ""
    if pages is not None and url in pages:
        value = pages[url]
        return value if isinstance(value, str) else ""
    for source in (evidence, entity):
        page = source.get("page_text")
        if isinstance(page, str) and page:
            return page
    return ""


def _with_verified_quotes(entity: dict, pages: Mapping[str, str] | None = None) -> dict:
    """Copy whose unverified quotes are not extracted and cannot stay judged.

    A quote that is present and verified does not raise `extracted` when
    the caller already set it to false — verification only downgrades.
    """
    out = dict(entity)
    ev_out = []
    for raw in _evidence(entity):
        e = dict(raw)
        quote = e.get("quote")
        if isinstance(quote, str) and quote.strip():
            ok = verified_quote(quote, _page_for(e, entity, pages))
            e["quote_verified"] = ok
            if not ok:
                e["extracted"] = False
                if e.get("judge") == "supported":
                    e.pop("judge", None)
        ev_out.append(e)
    out["evidence"] = ev_out
    return out


def _pack(before: str, after: str, checked: dict) -> dict:
    out = {"confidence_level": after}
    if _RANK[after] < _RANK[before]:
        out["downgraded_from"] = before
    flags = [
        e.get("quote_verified")
        for e in _evidence(checked)
        if isinstance(e.get("quote"), str) and e.get("quote").strip()
    ]
    if flags:
        out["quote_verified"] = all(flags)
    return out


def with_official(entity: dict, whitelist: Sequence[str] | None) -> dict:
    """Copy whose evidence `official` flags come from the whitelist only.

    A caller-supplied `official: True` is not trusted.
    """
    out = dict(entity)
    out["evidence"] = [
        {**e, "official": is_official(e.get("url") or "", whitelist)}
        for e in _evidence(entity)
    ]
    return out


def assess_level(entity: dict, pages: Mapping[str, str] | None = None, *,
                 whitelist: Sequence[str] | None = None) -> dict:
    """Ladder before and after the quote check.

    Returns confidence_level, and downgraded_from / quote_verified when
    a quote was checked.
    """
    entity = with_official(entity, whitelist)
    before = level(entity)
    checked = _with_verified_quotes(entity, pages)
    after = level(checked)
    return _pack(before, after, checked)


def confidence_level(entity: dict, pages: Mapping[str, str] | None = None, *,
                     whitelist: Sequence[str] | None = None) -> str:
    """C0–C4 after dropping evidence whose quote is not in the fetched page."""
    return assess_level(entity, pages, whitelist=whitelist)["confidence_level"]


def _reasons(port: dict) -> list[str]:
    raw = port.get("confidence_reasons") or []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [r for r in raw if isinstance(r, str)]
    return []


def _urls(port: dict) -> list[str]:
    out: list[str] = []
    for url in port.get("source_urls") or []:
        if isinstance(url, str) and url.strip() and url not in out:
            out.append(url)
    return out


def _non_llm_corroboration(port: dict) -> bool:
    """Catalogue or LLM∩NER. Two LLM readers do not count."""
    engine = (port.get("extraction_engine") or "").lower()
    reasons = set(_reasons(port))
    if engine == "catalog":
        return True
    if "catalogue officiel" in reasons or "catalogue + LLM + NER" in reasons:
        return True
    if port.get("extraction_agreement") is True:
        return True
    if "LLM et NER d'accord" in reasons or "LLM ∩ NER ∩ Claude" in reasons:
        return True
    return False


JUDGE_SUPPORTED = "supported"
JUDGE_CONTRADICTED = "contradicted"


def judge_outcome(port: dict) -> str | None:
    """Read the NemotronJudge result (`agents.poe_judge`) on a port.

    port.judge = {verdict: poe | not_poe | unclear, reason, quoted_excerpt,
    excerpt_verified, disagrees}. `excerpt_verified` is computed by
    poe_judge against the official evidence text, not by the model.

    "poe" with a verified, non-empty excerpt -> supported (lifts to C2).
    "not_poe" -> contradicted (caps the level at C1). Anything else,
    including "poe" without a verified excerpt and "unclear" -> None.
    """
    judge = port.get("judge")
    if not isinstance(judge, dict):
        return None
    verdict = str(judge.get("verdict") or "").strip().lower()
    if verdict == "not_poe":
        return JUDGE_CONTRADICTED
    excerpt = judge.get("quoted_excerpt")
    if (verdict == "poe" and judge.get("excerpt_verified") is True
            and isinstance(excerpt, str) and excerpt.strip()):
        return JUDGE_SUPPORTED
    return None


def _lifts_above_c1(port: dict) -> bool:
    if _non_llm_corroboration(port):
        return True
    return judge_outcome(port) == JUDGE_SUPPORTED


def _cap(result: dict, port: dict) -> dict:
    """A "not_poe" verdict contradicts the evidence: the level goes down to C1."""
    if judge_outcome(port) != JUDGE_CONTRADICTED:
        return result
    lvl = result["confidence_level"]
    if _RANK[lvl] <= _RANK["C1"]:
        return result
    out = dict(result)
    out["confidence_level"] = "C1"
    out.setdefault("downgraded_from", lvl)
    out["judge_contradicted"] = True
    return out


def _geo_from_v2(port: dict) -> dict:
    geo = port.get("geo")
    if isinstance(geo, dict) and ("agree" in geo or "independent_sources" in geo):
        return {
            "agree": geo.get("agree"),
            "independent_sources": geo.get("independent_sources", 0),
            "agreement_km": geo.get("agreement_km"),
            "spatial_kind": geo.get("spatial_kind", port.get("spatial_kind")),
        }
    alts = port.get("geocode_alternates") or {}
    n = 0
    if isinstance(alts, dict):
        n = sum(1 for key in ("nominatim", "geonames") if alts.get(key))
    if port.get("geocode_corroboration") and port.get("lat") is not None:
        n = max(n, 2)
    agree = port.get("geocode_agree")
    if agree is True:
        n = max(n, 2)
    km = port.get("geocode_agreement_km")
    if km is None:
        km = port.get("agreement_km")
    return {"agree": agree is True, "independent_sources": n,
            "agreement_km": km, "spatial_kind": port.get("spatial_kind")}


def _from_v2(port: dict, whitelist: Sequence[str] | None) -> dict:
    """Shape a scored V2 port as a certify entity. Does not mutate `port`."""
    reasons = _reasons(port)
    urls = _urls(port)
    synthesis = bool(port.get("from_synthesis")) or "synthèse web sans page" in reasons
    extracted = bool(urls) and not synthesis
    lift = _lifts_above_c1(port)
    quote = port.get("quote") if isinstance(port.get("quote"), str) else ""
    page = port.get("page_text") if isinstance(port.get("page_text"), str) else ""
    evidence = []
    for url in urls:
        item = {"url": url, "extracted": extracted}
        if is_official(url, whitelist):
            item["official"] = True
        if lift:
            item["judge"] = "supported"
        if quote.strip():
            item["quote"] = quote
            if page:
                item["page_text"] = page
        evidence.append(item)
    name = port.get("name") if isinstance(port.get("name"), str) else ""
    return {
        "name": name,
        "evidence": evidence,
        "geo": _geo_from_v2(port),
        "human": port.get("human"),
        "page_text": page,
    }


def assess_port(port: dict, *, whitelist: Sequence[str] | None = None,
                pages: Mapping[str, str] | None = None) -> dict:
    """Map a prototype entity or a V2 scored port, then apply the quote check."""
    if not isinstance(port, dict):
        return {"confidence_level": "C0"}
    if isinstance(port.get("evidence"), list):
        return _cap(assess_level(port, pages, whitelist=whitelist), port)
    entity = _from_v2(port, whitelist)
    before = level(entity)
    checked = _with_verified_quotes(entity, pages)
    return _cap(_pack(before, level(checked), checked), port)


def level_from_v2(port: dict, *, whitelist: Sequence[str] | None = None,
                  pages: Mapping[str, str] | None = None) -> str:
    """C0–C4 for a V2 port (`poe_confidence` fields) or a prototype entity.

    A whitelisted (State) host is required for C1+. An LLM-only reading (single extractor,
    or two LLM readers) never goes above C1. Catalogue, LLM∩NER, or a judge
    "poe" with excerpt_verified can; a judge "not_poe" caps at C1.
    `human == "confirmed"` lifts only C3 to C4.
    """
    return assess_port(port, whitelist=whitelist, pages=pages)["confidence_level"]


def attach_confidence_level(port: dict, *, whitelist: Sequence[str] | None = None,
                            pages: Mapping[str, str] | None = None) -> dict:
    """Set `confidence_level` on a port. Does not change the numeric score."""
    try:
        assessed = assess_port(port, whitelist=whitelist, pages=pages)
    except Exception:
        port["confidence_level"] = "C0"
        return port
    port["confidence_level"] = assessed["confidence_level"]
    if assessed.get("judge_contradicted"):
        port["judge_contradicted"] = True
    else:
        port.pop("judge_contradicted", None)
    if assessed.get("downgraded_from"):
        port["confidence_downgraded_from"] = assessed["downgraded_from"]
    else:
        port.pop("confidence_downgraded_from", None)
    if "quote_verified" in assessed:
        port["quote_verified"] = assessed["quote_verified"]
    else:
        port.pop("quote_verified", None)
    return port
