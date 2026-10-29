"""AMP Review Suggest: which visit URL for THIS site_id.

Closed list (visit_candidates). Never manager_url. Never an invented URL.
``no_visit`` = Formalities UNCLOS. Does not write amp_sites or Gold.
"""
from __future__ import annotations

from app.services.amp import rank_visit_candidate, urls_equivalent
from app.services.poe_zone_fiche import PUBLISHED_RUN
from app.services.review_choices import empty_choices, gold_ready, save_choices_doc
from app.services.review_lessons import (
    format_lessons_for_prompt,
    pick_fewshot,
    save_proposal,
    token_scores_from_lessons,
    url_lesson_delta,
)
from app.services.review_queue import get_fiche, save_comment

AMP_SYSTEM = (
    "Tu es le réviseur AMP de Blue Intelligence. "
    "Tu choisis la page VISITE (permis, mouillage, plaisance) d'UN site ProtectedSeas. "
    "Réponds uniquement en JSON strict."
)

AMP_PROMPT = """Site AMP : {label} (site_id={site_id}, pays={country}, désignation={designation}).
URL gestionnaire (INTERDITE comme visite) : {manager}

Question : parmi ces candidats DÉJÀ trouvés, laquelle est la page visite de CE site ?
Jamais la homepage gestionnaire. N'invente aucune URL hors liste.
Si aucune n'est une visite : no_visit=true et keep=[].

Leçons Gold :
{lessons}

Candidats :
{docs}

JSON :
{{
  "keep": ["<url>"],
  "drop": ["<url>"],
  "no_visit": false,
  "comment": "2-4 phrases en français"
}}
"""


def _sid(value) -> str:
    return "" if value is None else str(value)


def candidate_urls(fiche: dict | None) -> list[dict]:
    out, seen = [], set()
    for rec in (fiche or {}).get("visit_candidates") or []:
        if isinstance(rec, dict):
            url = (rec.get("url") or "").strip()
            row = rec
        else:
            url = str(rec or "").strip()
            row = {"url": url}
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(row)
    return out


def local_pick_amp(fiche: dict | None,
                   token_scores: dict[str, float] | None = None) -> dict:
    fiche = fiche or {}
    manager = fiche.get("manager_url")
    name = fiche.get("name") or ""
    keep, drop, reasons = [], [], []
    scored: list[tuple[float, str]] = []
    for rec in candidate_urls(fiche):
        url = rec.get("url")
        same = bool(rec.get("same_as_manager")) or urls_equivalent(url, manager)
        if same:
            drop.append(url)
            reasons.append({"url": url, "verdict": "drop", "why": "même URL que le gestionnaire"})
            continue
        score = float(rank_visit_candidate(
            url, manager, title=rec.get("title") or "",
            snippet=rec.get("status") or "", name=name))
        score += url_lesson_delta(url, token_scores)
        if score >= 4:
            scored.append((score, url))
            reasons.append({"url": url, "verdict": "keep", "why": f"indice visite score={score:.1f}"})
        else:
            drop.append(url)
            reasons.append({"url": url, "verdict": "drop", "why": "homepage / hors sujet"})
    scored.sort(key=lambda x: x[0], reverse=True)
    keep = [u for _, u in scored[:2]]
    drop = [u for u in drop if u not in keep]
    no_visit = not keep
    comment = (
        "Proposition locale AMP. "
        + (f"{len(keep)} page(s) visite." if keep else "Aucune visite distincte — no_visit.")
    )
    return {
        "keep": keep,
        "drop": drop,
        "no_visit": no_visit,
        "comment": comment,
        "reasons": reasons,
        "engine": "local",
        "local_keep": list(keep),
        "list_kind": "none" if no_visit else "visit",
    }


def _docs_blob(fiche: dict) -> str:
    lines = []
    for rec in candidate_urls(fiche):
        flag = " SAME_AS_MANAGER" if rec.get("same_as_manager") else ""
        lines.append(
            f"- {rec.get('url')}{flag} status={rec.get('status') or ''} "
            f"src={rec.get('source') or ''}"
        )
    return "\n".join(lines) or "(aucun candidat)"


def _listed(urls, allowed: set[str]) -> list[str]:
    out, seen = [], set()
    for raw in urls or []:
        u = str(raw or "").strip()
        if u in allowed and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def merge_amp(local: dict, llm: dict | None, allowed: set[str],
              manager: str | None) -> dict:
    if not llm:
        return local
    keep = [u for u in _listed(llm.get("keep"), allowed)
            if not urls_equivalent(u, manager)]
    drop = _listed(llm.get("drop"), allowed)
    no_visit = bool(llm.get("no_visit")) or not keep
    if no_visit:
        keep = []
        drop = [u for u in allowed if u not in drop] + drop
        drop = list(dict.fromkeys(drop))
    comment = str(llm.get("comment") or "").strip() or local.get("comment") or ""
    return {
        "keep": keep,
        "drop": [u for u in drop if u not in keep],
        "no_visit": no_visit,
        "comment": comment[:1200],
        "engine": llm.get("engine") or "llm",
        "local_keep": list(local.get("keep") or []),
        "list_kind": "none" if no_visit else "visit",
    }


async def _llm_pick_amp(fiche: dict, settings, log,
                        lessons: list[dict] | None = None) -> dict | None:
    allowed = {rec["url"] for rec in candidate_urls(fiche)}
    if not allowed:
        return None
    few = pick_fewshot(lessons or [], fiche, exclude_id=_sid(fiche.get("site_id")))
    prompt = AMP_PROMPT.format(
        label=fiche.get("name") or "",
        site_id=fiche.get("site_id") or fiche.get("id") or "",
        country=fiche.get("country") or "",
        designation=fiche.get("designation") or "",
        manager=fiche.get("manager_url") or "—",
        lessons=format_lessons_for_prompt(few),
        docs=_docs_blob(fiche),
    )
    try:
        from app.core.judge import complete_json_cascade
        data, engine = await complete_json_cascade(
            AMP_SYSTEM, prompt, settings, role="review",
            max_tokens=800, log=log)
    except Exception as e:
        log(f"amp picker LLM: {type(e).__name__}: {str(e)[:120]}")
        return None
    if not isinstance(data, dict):
        return None
    data["engine"] = engine
    return data


async def suggest_amp_documents(db, entity_id: str, *,
                                settings: dict | None = None,
                                log=None,
                                lessons: list[dict] | None = None) -> dict:
    log = log or (lambda m: None)
    eid = _sid(entity_id)
    packed = await get_fiche(db, "amp", PUBLISHED_RUN, eid)
    if not packed or not packed.get("fiche"):
        raise ValueError("fiche not found")
    fiche = packed["fiche"]
    cands = candidate_urls(fiche)
    if lessons is None:
        from app.services.review_lessons import load_lessons
        lessons = await load_lessons(db, kind="amp")
    scores = token_scores_from_lessons(lessons)
    local = local_pick_amp(fiche, scores)
    if settings is None:
        try:
            from app.db import get_settings
            settings = await get_settings()
        except Exception:
            settings = {}
    llm = None
    if cands:
        llm = await _llm_pick_amp(fiche, settings, log, lessons=lessons)
    picked = merge_amp(local, llm, {r["url"] for r in cands}, fiche.get("manager_url"))
    public = empty_choices()
    public["no_visit"] = bool(picked.get("no_visit"))
    for url in picked["keep"]:
        public["visit"][url] = "keep"
    for url in picked["drop"]:
        if url not in public["visit"]:
            public["visit"][url] = "drop"
    if public["no_visit"]:
        public["visit"] = {u: "drop" for u in public["visit"]}
    choices = await save_choices_doc(db, "amp", eid, public)
    saved = await save_comment(db, "amp", PUBLISHED_RUN, eid, picked["comment"])
    await save_proposal(db, eid, picked, fiche, kind="amp")
    ready = gold_ready(fiche, choices, kind="amp", comment=picked["comment"])
    log(f"amp picker {eid}: keep={len(picked['keep'])} no_visit={picked.get('no_visit')}")
    return {
        "kind": "amp",
        "id": eid,
        "choices": choices,
        "comment": picked["comment"],
        "comment_updated_at": saved.get("updated_at"),
        "engine": picked.get("engine"),
        "gold_ready": ready,
        "gold_on": bool(packed.get("gold_on")),
        "wrote_amp_sites": False,
        "wrote_projects": False,
        "wrote_poe_ports": False,
    }
