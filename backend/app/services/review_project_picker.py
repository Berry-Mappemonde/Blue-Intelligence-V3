"""Projects Review Suggest: project page + action sites.

Closed list of URLs and site_ids. No invented GPS. No Gold.
``unlocated`` stays in queue (no keep site).
"""
from __future__ import annotations

from urllib.parse import urlparse

from app.services.poe_zone_fiche import PUBLISHED_RUN
from app.services.project_listing import is_homepage_url, is_listing_url
from app.services.review_choices import (
    empty_choices,
    gold_ready,
    project_sites,
    project_urls,
    save_choices_doc,
    site_ok,
)
from app.services.review_lessons import (
    format_lessons_for_prompt,
    pick_fewshot,
    save_proposal,
    token_scores_from_lessons,
    url_lesson_delta,
)
from app.services.review_queue import get_fiche, save_comment

_JUNK_PROJECT = (
    "donate", "donation", "donations", "careers", "jobs", "job",
    "news", "presse", "press", "about", "contact", "privacy",
    "legal", "login", "signup", "wp-content", "wp-admin",
)

PROJECT_SYSTEM = (
    "Tu es le réviseur Projets de Blue Intelligence. "
    "Tu choisis les pages PROJET (pas home / donate / jobs) et les lieux d'action visitable en bateau. "
    "Réponds uniquement en JSON strict. N'invente aucune URL ni aucun site_id."
)

PROJECT_PROMPT = """Projet : {title}
URLs déjà sur la fiche (liste fermée) :
{urls}

Sites déjà sur la fiche (liste fermée) :
{sites}

Leçons Gold :
{lessons}

JSON :
{{
  "keep_urls": ["<url>"],
  "drop_urls": ["<url>"],
  "keep_sites": ["<site_id>"],
  "drop_sites": ["<site_id>"],
  "comment": "2-4 phrases en français"
}}
"""


def _sid(value) -> str:
    return "" if value is None else str(value)


def _junk_path(url: str) -> bool:
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        return False
    return any(tok in path for tok in _JUNK_PROJECT)


def local_pick_project(fiche: dict | None,
                       token_scores: dict[str, float] | None = None) -> dict:
    fiche = fiche or {}
    title = str(fiche.get("title") or "")
    keep_urls, drop_urls = [], []
    for url in project_urls(fiche):
        adj = url_lesson_delta(url, token_scores)
        junk = is_homepage_url(url) or is_listing_url(url) or _junk_path(url)
        if junk and adj < 0.3:
            drop_urls.append(url)
        else:
            keep_urls.append(url)
    keep_sites, drop_sites = [], []
    for site in project_sites(fiche):
        sid = _sid(site.get("site_id"))
        if site_ok(site, title=title):
            keep_sites.append(sid)
        else:
            drop_sites.append(sid)
    comment = (
        "Proposition locale Projets. "
        f"{len(keep_urls)} URL(s) projet, {len(keep_sites)} site(s) action."
        + (" Aucun site tenable — unlocated." if not keep_sites else "")
    )
    return {
        "keep": keep_urls + [f"site:{s}" for s in keep_sites],
        "drop": drop_urls + [f"site:{s}" for s in drop_sites],
        "keep_urls": keep_urls,
        "drop_urls": drop_urls,
        "keep_sites": keep_sites,
        "drop_sites": drop_sites,
        "comment": comment,
        "engine": "local",
        "local_keep": list(keep_urls),
        "list_kind": "action" if keep_sites else "unlocated",
    }


def _listed(urls, allowed: set[str]) -> list[str]:
    out, seen = [], set()
    for raw in urls or []:
        u = str(raw or "").strip()
        if u in allowed and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def merge_project(local: dict, llm: dict | None,
                  allowed_urls: set[str], allowed_sites: set[str]) -> dict:
    if not llm:
        return local
    keep_urls = _listed(llm.get("keep_urls") or llm.get("keep"), allowed_urls)
    drop_urls = _listed(llm.get("drop_urls") or llm.get("drop"), allowed_urls)
    keep_sites = _listed(llm.get("keep_sites"), allowed_sites)
    drop_sites = _listed(llm.get("drop_sites"), allowed_sites)
    if not keep_urls and not keep_sites and not drop_urls and not drop_sites:
        return local
    comment = str(llm.get("comment") or "").strip() or local.get("comment") or ""
    return {
        "keep": keep_urls + [f"site:{s}" for s in keep_sites],
        "drop": [u for u in drop_urls if u not in keep_urls]
                + [f"site:{s}" for s in drop_sites if s not in keep_sites],
        "keep_urls": keep_urls,
        "drop_urls": [u for u in drop_urls if u not in keep_urls],
        "keep_sites": keep_sites,
        "drop_sites": [s for s in drop_sites if s not in keep_sites],
        "comment": comment[:1200],
        "engine": llm.get("engine") or "llm",
        "local_keep": list(local.get("keep_urls") or []),
        "list_kind": "action" if keep_sites else "unlocated",
    }


async def _llm_pick_project(fiche: dict, settings, log,
                            lessons: list[dict] | None = None) -> dict | None:
    urls = project_urls(fiche)
    sites = project_sites(fiche)
    if not urls and not sites:
        return None
    few = pick_fewshot(lessons or [], fiche, exclude_id=_sid(fiche.get("id")))
    site_lines = []
    for s in sites:
        site_lines.append(
            f"- {s.get('site_id')} name={s.get('name') or ''} "
            f"geo={s.get('geo_source') or ''} snapped={bool(s.get('snapped'))} "
            f"hq={bool(s.get('hq_suspect'))} "
            f"{s.get('lat')},{s.get('lon')}"
        )
    prompt = PROJECT_PROMPT.format(
        title=fiche.get("title") or "",
        urls="\n".join(f"- {u}" for u in urls) or "(aucune)",
        sites="\n".join(site_lines) or "(aucun)",
        lessons=format_lessons_for_prompt(few),
    )
    try:
        from app.core.judge import complete_json_cascade
        data, engine = await complete_json_cascade(
            PROJECT_SYSTEM, prompt, settings, role="review",
            max_tokens=800, log=log)
    except Exception as e:
        log(f"project picker LLM: {type(e).__name__}: {str(e)[:120]}")
        return None
    if not isinstance(data, dict):
        return None
    data["engine"] = engine
    return data


async def suggest_project_documents(db, entity_id: str, *,
                                    settings: dict | None = None,
                                    log=None,
                                    lessons: list[dict] | None = None) -> dict:
    log = log or (lambda m: None)
    eid = _sid(entity_id)
    packed = await get_fiche(db, "project", PUBLISHED_RUN, eid)
    if not packed or not packed.get("fiche"):
        raise ValueError("fiche not found")
    fiche = packed["fiche"]
    if lessons is None:
        from app.services.review_lessons import load_lessons
        lessons = await load_lessons(db, kind="project")
    scores = token_scores_from_lessons(lessons)
    local = local_pick_project(fiche, scores)
    if settings is None:
        try:
            from app.db import get_settings
            settings = await get_settings()
        except Exception:
            settings = {}
    llm = await _llm_pick_project(fiche, settings, log, lessons=lessons)
    picked = merge_project(
        local, llm,
        set(project_urls(fiche)),
        {_sid(s.get("site_id")) for s in project_sites(fiche)},
    )
    public = empty_choices()
    for url in picked.get("keep_urls") or []:
        public["urls"][url] = "keep"
    for url in picked.get("drop_urls") or []:
        public["urls"][url] = "drop"
    for sid in picked.get("keep_sites") or []:
        public["sites"][sid] = "keep"
    for sid in picked.get("drop_sites") or []:
        public["sites"][sid] = "drop"
    choices = await save_choices_doc(db, "project", eid, public)
    saved = await save_comment(db, "project", PUBLISHED_RUN, eid, picked["comment"])
    await save_proposal(db, eid, picked, fiche, kind="project")
    ready = gold_ready(fiche, choices, kind="project", comment=picked["comment"])
    log(f"project picker {eid}: urls={len(picked.get('keep_urls') or [])} "
        f"sites={len(picked.get('keep_sites') or [])}")
    return {
        "kind": "project",
        "id": eid,
        "choices": choices,
        "comment": picked["comment"],
        "comment_updated_at": saved.get("updated_at"),
        "engine": picked.get("engine"),
        "gold_ready": ready,
        "gold_on": bool(packed.get("gold_on")),
        "wrote_projects": False,
        "wrote_poe_ports": False,
    }
