"""Formalities Review document judge.

Local (URL bonus + catalogue) ∥ NIM → OpenRouter → Claude cascade.
Full-page captures / PDF pages go to the LLM (vision if the backend follows).
Never writes review_gold or poe_ports.
"""
from __future__ import annotations

import asyncio

from app.core.extract import (
    catalog_is_sufficient,
    extract_cascade,
    extract_structured_ports,
    looks_like_port_catalog,
    pdf_page_jpegs,
    should_skip_screenshot,
)
from app.core.judge import complete_json_cascade
from app.services.poe_pipeline import list_url_bonus
from app.services.poe_zone_label import search_polygon_name
from app.services.territory_ref import td_url_fits_polygon
from app.services.review_choices import (
    empty_choices,
    gold_ready,
    save_choices_doc,
)
from app.services.review_queue import get_fiche, list_queue, save_comment
from app.services.poe_zone_fiche import PUBLISHED_RUN
from app.services.review_lessons import (
    format_lessons_for_prompt,
    pick_fewshot,
    save_proposal,
    token_scores_from_lessons,
    url_lesson_delta,
)

DOC_CAP = 12
IMAGE_CAP = 6
TEXT_EXCERPT = 1800

PICKER_SYSTEM = (
    "Tu es le réviseur Formalités de Blue Intelligence. "
    "Tu choisis les documents d'État d'UN polygone VLIZ, pas les ports un par un. "
    "Réponds uniquement en JSON strict."
)

PICKER_PROMPT = """Polygone VLIZ : {label} (mrgid={mrgid}, iso2={iso2}, souverain={sovereign}).

Question : parmi ces documents DÉJÀ trouvés par les runs, lesquels sont la liste
officielle des points d'entrée DÉSIGNÉS de CE polygone (décret, catalogue, PDF
d'État, page qui LISTE les ports) ?

Une liste nationale mixte (cargo + plaisance, ex. puertos habilitados Mexique)
peut être gardée. Une page tourisme / home / actu / projet / un seul port
s'écarte. Tu peux garder PLUSIEURS documents (page + PDF).
N'invente aucune URL hors liste.

Leçons Gold (décisions humaines antérieures — suis ce jugement) :
{lessons}

Documents :
{docs}

JSON :
{{
  "keep": ["<url>", "..."],
  "drop": ["<url>", "..."],
  "list_kind": "pleasure" | "mixed_designated" | "cargo_only" | "none",
  "comment": "2-5 phrases en français sur l'ensemble des liens",
  "reasons": [{{"url": "...", "verdict": "keep|drop", "why": "≤80 car"}}]
}}
"""


def _sid(value) -> str:
    return "" if value is None else str(value)


def rank_td_urls(fiche: dict | None) -> list[str]:
    rows = []
    for rec in (fiche or {}).get("sources_td") or []:
        url = rec.get("url")
        if url and td_url_fits_polygon(url, fiche):
            rows.append((list_url_bonus(url) + (0.4 if rec.get("official") else 0), url))
    rows.sort(key=lambda x: x[0], reverse=True)
    seen, out = set(), []
    for _, url in rows:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out[:DOC_CAP]


async def _evidence_for(url: str, log) -> dict:
    page = {}
    try:
        page = await extract_cascade(url, keep_raw=True, log=log)
    except Exception as e:
        log(f"doc picker fetch {url[:70]}: {type(e).__name__}")
        page = {}
    text = (page.get("text") or "")[:8000]
    images: list[bytes] = []
    if page.get("is_pdf") and page.get("raw"):
        try:
            images = await asyncio.to_thread(pdf_page_jpegs, page["raw"], 2)
        except Exception:
            images = []
    elif not page.get("is_pdf"):
        skip = should_skip_screenshot(url, page)
        if skip:
            log(f"screenshot {url[:70]}: sauté ({skip})")
        else:
            try:
                from app.core.render import render_screenshot
                shot = await render_screenshot(url, log=log)
                if shot:
                    images = [shot]
            except Exception:
                images = []
    catalog = extract_structured_ports(text) if text else []
    return {
        "url": url,
        "text": text,
        "excerpt": text[:TEXT_EXCERPT],
        "images": images,
        "is_pdf": bool(page.get("is_pdf")),
        "catalog_n": len(catalog),
        "looks_catalog": looks_like_port_catalog(text) if text else False,
        "sufficient": catalog_is_sufficient(catalog, text),
        "bonus": list_url_bonus(url),
    }


def local_pick(evidences: list[dict],
               token_scores: dict[str, float] | None = None) -> dict:
    """Local heuristic: catalogue / URL bonus. Runs in parallel with the LLM."""
    keep, drop = [], []
    reasons = []
    for ev in evidences:
        url = ev["url"]
        adj = url_lesson_delta(url, token_scores)
        bonus = float(ev.get("bonus") or 0) + adj
        strong = ev["sufficient"] or (
            ev["looks_catalog"] and ev["catalog_n"] >= 3
        ) or (bonus >= 0.45 and ev["catalog_n"] >= 1) or adj >= 0.3
        if strong:
            keep.append(url)
            reasons.append({
                "url": url, "verdict": "keep",
                "why": f"catalogue local n={ev['catalog_n']} bonus={bonus:.2f}",
            })
        else:
            drop.append(url)
            reasons.append({
                "url": url, "verdict": "drop",
                "why": "pas une liste exploitable (heuristique)",
            })
    kind = "mixed_designated" if keep else "none"
    comment = (
        "Proposition locale (parseur + score d'URL), sans LLM. "
        + (f"{len(keep)} document(s) ressemblent à une liste officielle."
           if keep else "Aucun document ne ressemble à une liste officielle.")
    )
    return {
        "keep": keep,
        "drop": drop,
        "list_kind": kind,
        "comment": comment,
        "reasons": reasons,
        "engine": "local",
    }


def _docs_blob(evidences: list[dict]) -> str:
    blocks = []
    for i, ev in enumerate(evidences, 1):
        blocks.append(
            f"{i}. {ev['url']}\n"
            f"   pdf={ev['is_pdf']} bonus={ev['bonus']:.2f} "
            f"catalogue={ev['catalog_n']} looks={ev['looks_catalog']}\n"
            f"   extrait: {(ev['excerpt'] or '').replace(chr(10), ' ')[:TEXT_EXCERPT]}"
        )
    return "\n\n".join(blocks)


def _collect_images(evidences: list[dict]) -> list[bytes]:
    out: list[bytes] = []
    for ev in evidences:
        for img in ev.get("images") or []:
            if img:
                out.append(img)
            if len(out) >= IMAGE_CAP:
                return out
    return out


def _listed(urls: list, allowed: set[str]) -> list[str]:
    out, seen = [], set()
    for raw in urls or []:
        url = str(raw or "").strip()
        if url in allowed and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def merge_picks(local: dict, llm: dict | None, allowed: set[str]) -> dict:
    if not llm:
        return local
    keep = _listed(llm.get("keep"), allowed)
    drop = _listed(llm.get("drop"), allowed)
    if not keep and not drop:
        return local
    for url in allowed:
        if url not in keep and url not in drop:
            drop.append(url)
    kind = llm.get("list_kind") or local.get("list_kind") or "none"
    if kind not in ("pleasure", "mixed_designated", "cargo_only", "none"):
        kind = local.get("list_kind") or "none"
    comment = str(llm.get("comment") or "").strip() or local.get("comment") or ""
    if set(keep) != set(local.get("keep") or []):
        comment = (comment + " (écart vs heuristique locale.)").strip()
    return {
        "keep": keep,
        "drop": [u for u in drop if u not in keep],
        "list_kind": kind,
        "comment": comment[:1200],
        "reasons": llm.get("reasons") or local.get("reasons") or [],
        "engine": llm.get("engine") or "llm",
        "local_keep": list(local.get("keep") or []),
    }


async def _llm_pick(fiche: dict, evidences: list[dict], settings, log,
                    lessons: list[dict] | None = None) -> dict | None:
    allowed = {ev["url"] for ev in evidences}
    few = pick_fewshot(lessons or [], fiche, exclude_id=_sid(fiche.get("mrgid")))
    prompt = PICKER_PROMPT.format(
        label=search_polygon_name(fiche) or fiche.get("name") or fiche.get("label") or "",
        mrgid=fiche.get("mrgid"),
        iso2=fiche.get("iso2") or "",
        sovereign=fiche.get("sovereign") or "",
        lessons=format_lessons_for_prompt(few),
        docs=_docs_blob(evidences),
    )
    images = _collect_images(evidences)
    try:
        data, engine = await complete_json_cascade(
            PICKER_SYSTEM, prompt, settings, role="review",
            max_tokens=1200, openrouter_max_tokens=1200, claude_max_tokens=1200,
            log=log, images=images or None)
    except Exception as e:
        log(f"doc picker LLM: {type(e).__name__}: {str(e)[:120]}")
        if images:
            try:
                data, engine = await complete_json_cascade(
                    PICKER_SYSTEM, prompt, settings, role="review",
                    max_tokens=1200, openrouter_max_tokens=1200,
                    claude_max_tokens=1200, log=log, images=None)
            except Exception as e2:
                log(f"doc picker LLM texte: {type(e2).__name__}: {str(e2)[:120]}")
                return None
        else:
            return None
    if not isinstance(data, dict):
        return None
    data["engine"] = engine
    data["keep"] = _listed(data.get("keep"), allowed)
    data["drop"] = _listed(data.get("drop"), allowed)
    return data


async def suggest_eez_documents(db, entity_id: str, *,
                                settings: dict | None = None,
                                log=None,
                                lessons: list[dict] | None = None) -> dict:
    """Pre-fill keep/drop + comment. No Gold."""
    log = log or (lambda m: None)
    eid = _sid(entity_id)
    packed = await get_fiche(db, "eez", PUBLISHED_RUN, eid)
    if not packed or not packed.get("fiche"):
        raise ValueError("fiche not found")
    fiche = packed["fiche"]
    urls = rank_td_urls(fiche)
    if not urls:
        comment = packed.get("comment") or "Aucun document TD à juger sur cette fiche."
        return {
            "kind": "eez", "id": eid, "choices": packed.get("choices") or empty_choices(),
            "comment": comment, "engine": "none",
            "gold_ready": gold_ready(fiche, packed.get("choices"), kind="eez",
                                     comment=comment),
            "gold_on": bool(packed.get("gold_on")),
            "wrote_poe_ports": False,
        }

    if settings is None:
        try:
            from app.db import get_settings
            settings = await get_settings()
        except Exception:
            settings = {}

    if lessons is None:
        from app.services.review_lessons import load_lessons
        lessons = await load_lessons(db)
    scores = token_scores_from_lessons(lessons)
    evidences: list[dict] = []
    try:
        for url in urls:
            evidences.append(await _evidence_for(url, log))
        local_task = asyncio.to_thread(local_pick, list(evidences), scores)
        llm_task = _llm_pick(fiche, list(evidences), settings, log, lessons=lessons)
        local, llm = await asyncio.gather(local_task, llm_task)
    finally:
        from app.core.render import shutdown_render
        await shutdown_render()
    picked = merge_picks(local, llm, {ev["url"] for ev in evidences})

    public = empty_choices()
    for url in picked["keep"]:
        public["td"][url] = "keep"
    for url in picked["drop"]:
        public["td"][url] = "drop"
    choices = await save_choices_doc(db, "eez", eid, public)
    saved = await save_comment(db, "eez", PUBLISHED_RUN, eid, picked["comment"])
    await save_proposal(db, eid, picked, fiche)
    ready = gold_ready(fiche, choices, kind="eez", comment=picked["comment"])
    log(f"doc picker {eid}: keep={len(picked['keep'])} engine={picked.get('engine')}")
    return {
        "kind": "eez",
        "id": eid,
        "choices": choices,
        "comment": picked["comment"],
        "comment_updated_at": saved.get("updated_at"),
        "list_kind": picked.get("list_kind"),
        "engine": picked.get("engine"),
        "local_keep": picked.get("local_keep") or local.get("keep"),
        "gold_ready": ready,
        "gold_on": bool(packed.get("gold_on")),
        "wrote_poe_ports": False,
        "wrote_projects": False,
    }


async def all_eez_ids(db) -> list[str]:
    """All Formalities cards (published union), not the Stable / pre-Gold filter."""
    packed = await list_queue(
        db, "eez", PUBLISHED_RUN, offset=0, limit=2000,
        q="", pre_gold=False, stable=False)
    out, seen = [], set()
    for it in packed.get("items") or []:
        eid = _sid(it.get("id"))
        if eid and eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out


async def run_suggest_batch(db, state, *, settings: dict | None = None) -> dict:
    """Suggest batch: one suggestion per polygon. Overwrites ticks + comments."""
    log = state.log if hasattr(state, "log") else (lambda m: None)
    ids = await all_eez_ids(db)
    state.total = len(ids)
    state.progress = 0
    if settings is None:
        try:
            from app.db import get_settings
            settings = await get_settings()
        except Exception:
            settings = {}
    from app.services.review_lessons import load_lessons
    lessons = await load_lessons(db)
    ok = fail = 0
    last_id = None
    for eid in ids:
        if getattr(state, "cancel", False):
            log("lot Proposer : annulé")
            break
        try:
            out = await suggest_eez_documents(
                db, eid, settings=settings, log=log, lessons=lessons)
            ok += 1
            last_id = eid
            state.results.append({
                "id": eid, "ok": True,
                "engine": out.get("engine"),
                "keep": len((out.get("choices") or {}).get("td") or {}),
            })
            log(f"lot {eid}: engine={out.get('engine')}")
        except Exception as e:
            fail += 1
            state.results.append({
                "id": eid, "ok": False, "error": f"{type(e).__name__}",
            })
            log(f"lot {eid}: {type(e).__name__}: {str(e)[:120]}")
        state.progress = ok + fail
    summary = {
        "ok": ok, "fail": fail, "total": len(ids),
        "last_id": last_id, "cancelled": bool(getattr(state, "cancel", False)),
    }
    state.summary = summary
    return summary
