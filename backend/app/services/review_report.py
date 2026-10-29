"""Review report — raw material for pipeline improvements.

Read-only aggregate of `review_comments`, `review_choices` and
`review_gold` (plus live collections to recover titles).
URLs pasted in a comment — e.g. a PoE list of an EEZ polygon
found by hand via Gemini — come out as “proposed URLs”:
candidates for the pipeline to read / fetch on the next run.
The report writes nothing: no rule, no live collection, no Gold.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from app.services.review_choices import public_choices
from app.services.review_lessons import load_lessons, report_rows_from_lessons

REPORT_KINDS = ("eez", "project", "marina", "capitainerie", "amp")
KIND_LABELS = {
    "eez": "Formalités (polygones ZEE)",
    "project": "Projets",
    "marina": "Marinas",
    "capitainerie": "Capitaineries",
    "amp": "AMP",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sid(value) -> str:
    return "" if value is None else str(value)


def _domain(url: str | None) -> str:
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _extract_urls(text: str | None) -> list[str]:
    try:
        from app.services.amp import extract_urls
        return extract_urls(text)
    except Exception:
        return []


def _has_any_choice(ch: dict) -> bool:
    for key in ("td", "ports", "bu", "urls", "sites", "fields", "visit",
                "gps_edit"):
        if ch.get(key):
            return True
    return bool(ch.get("identity") or ch.get("gps") or ch.get("overlay")
                or ch.get("no_visit"))


async def _title_for(db, kind: str, eid: str) -> str:
    try:
        if kind == "eez":
            zone = await db.eez_zones.find_one({"mrgid": int(eid)})
            return (zone or {}).get("name") or eid
        if kind == "project":
            doc = await db.projects.find_one({"_id": eid})
            if not doc:
                doc = await db.projects.find_one({"url": eid})
            return (doc or {}).get("title") or eid
        coll = {"marina": db.marinas,
                "capitainerie": db.capitaineries,
                "amp": db.amp_sites}[kind]
        doc = await coll.find_one({"_id": eid})
        if not doc and kind == "amp":
            doc = await coll.find_one({"site_id": eid})
        return (doc or {}).get("name") or eid
    except Exception:
        return eid


async def build_report(db, kind: str | None = None) -> dict:
    """JSON report: items per card + aggregated pipeline actions."""
    kinds = list(REPORT_KINDS) if not kind or kind == "all" else [kind]
    filt = {"kind": {"$in": kinds}}
    comments = await db.review_comments.find(filt).to_list(50000)
    choice_docs = await db.review_choices.find(filt).to_list(50000)
    gold_docs = await db.review_gold.find(filt).to_list(50000)

    by_key: dict[tuple[str, str], dict] = {}

    def bucket(k: str, eid: str) -> dict:
        return by_key.setdefault((k, eid), {
            "kind": k, "id": eid, "title": eid,
            "comment": "", "comment_updated_at": None,
            "suggested_urls": [],
            "choices": None,
            "gold_on": False, "golded_at": None,
        })

    for d in comments:
        text = (d.get("comment") or "").strip()
        k, eid = _sid(d.get("kind")), _sid(d.get("entity_id"))
        if not text or k not in kinds or not eid:
            continue
        row = bucket(k, eid)
        # Legacy {kind}:{run}:{id} keys possible: keep the most recent.
        up = _sid(d.get("updated_at"))
        if not row["comment"] or up > _sid(row["comment_updated_at"]):
            row["comment"] = text
            row["comment_updated_at"] = d.get("updated_at")
            row["suggested_urls"] = _extract_urls(text)

    for d in choice_docs:
        k, eid = _sid(d.get("kind")), _sid(d.get("entity_id"))
        if k not in kinds or not eid:
            continue
        ch = public_choices(d)
        if _has_any_choice(ch):
            bucket(k, eid)["choices"] = ch

    for d in gold_docs:
        k, eid = _sid(d.get("kind")), _sid(d.get("entity_id"))
        if k not in kinds or not eid:
            continue
        row = bucket(k, eid)
        row["gold_on"] = bool(d.get("on"))
        snap = d.get("snapshot") or {}
        row["golded_at"] = snap.get("golded_at") or d.get("updated_at")

    for (k, eid), row in by_key.items():
        row["title"] = await _title_for(db, k, eid)

    actions = {
        "suggested_urls": [],
        "td_kept": [],
        "td_dropped": [],
        "urls_dropped": [],
        "ports_dropped": [],
        "sites_dropped": [],
        "fields_dropped": [],
        "visit_kept": [],
        "no_visit": [],
        "domains_dropped": [],
        "proposer_errors": [],
        "proposer_agreed": [],
        "proposer_code_hints": [],
    }
    domains: set[str] = set()
    for row in by_key.values():
        base = {"kind": row["kind"], "id": row["id"], "title": row["title"]}
        for url in row["suggested_urls"]:
            actions["suggested_urls"].append({**base, "url": url})
        ch = row["choices"] or {}
        for url, act in (ch.get("td") or {}).items():
            if act == "keep":
                actions["td_kept"].append({**base, "url": url})
            elif act == "drop":
                actions["td_dropped"].append({**base, "url": url})
                domains.add(_domain(url))
        for url in row["suggested_urls"]:
            if not any(a.get("url") == url for a in actions["td_kept"]):
                actions["td_kept"].append({**base, "url": url})
        for url, act in (ch.get("urls") or {}).items():
            if act == "drop":
                actions["urls_dropped"].append({**base, "url": url})
                domains.add(_domain(url))
        for pid, act in (ch.get("ports") or {}).items():
            if act == "drop":
                actions["ports_dropped"].append({**base, "port_id": pid})
        for sid, act in (ch.get("sites") or {}).items():
            if act == "drop":
                actions["sites_dropped"].append({**base, "site_id": sid})
        for fid, act in (ch.get("fields") or {}).items():
            if act == "drop":
                actions["fields_dropped"].append({**base, "field": fid})
        for url, act in (ch.get("visit") or {}).items():
            if act == "keep":
                actions["visit_kept"].append({**base, "url": url})
            elif act == "drop":
                domains.add(_domain(url))
        if ch.get("no_visit"):
            actions["no_visit"].append(base)
    actions["domains_dropped"] = sorted(d for d in domains if d)

    lessons = await load_lessons(db, kind="all")
    lessons = [
        les for les in lessons
        if (les.get("kind") or "eez") in kinds
    ]
    titles = {
        f"{k}:{eid}": row["title"]
        for (k, eid), row in by_key.items()
    }
    titles.update({
        _sid(row["id"]): row["title"]
        for (k, _eid), row in by_key.items() if k == "eez"
    })
    prop = report_rows_from_lessons(lessons, titles)
    actions["proposer_errors"] = prop["proposer_errors"]
    actions["proposer_agreed"] = prop["proposer_agreed"]
    actions["proposer_code_hints"] = prop["proposer_code_hints"]

    order = {k: i for i, k in enumerate(REPORT_KINDS)}
    items = sorted(
        by_key.values(),
        key=lambda r: (order.get(r["kind"], 99), _sid(r["title"]).casefold()))
    summary: dict[str, dict] = {}
    for row in items:
        s = summary.setdefault(row["kind"],
                               {"fiches": 0, "comments": 0, "gold": 0,
                                "proposer_errors": 0})
        s["fiches"] += 1
        if row["comment"]:
            s["comments"] += 1
        if row["gold_on"]:
            s["gold"] += 1
    for err in actions.get("proposer_errors") or []:
        k = err.get("kind")
        if not k:
            continue
        s = summary.setdefault(k, {"fiches": 0, "comments": 0, "gold": 0,
                                   "proposer_errors": 0})
        s["proposer_errors"] = s.get("proposer_errors", 0) + 1

    return {
        "generated_at": _now_iso(),
        "kinds": kinds,
        "summary": summary,
        "items": items,
        "pipeline_actions": actions,
    }


def _label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def _acts(m: dict | None, want: str) -> list[str]:
    return sorted(k for k, a in (m or {}).items() if a == want)


def report_markdown(report: dict) -> str:
    """Readable report (Markdown, in French) ready to share."""
    lines: list[str] = ["# Rapport de review", ""]
    kinds = report.get("kinds") or []
    lines.append(f"Généré le {report.get('generated_at')} — modes : "
                 + ", ".join(_label(k) for k in kinds) + ".")
    lines += ["", "## Synthèse", "",
              "| Mode | Fiches touchées | Commentaires | Gold | Écarts Proposer |",
              "|------|-----------------|--------------|------|-----------------|"]
    summary = report.get("summary") or {}
    for k in kinds:
        s = summary.get(k) or {
            "fiches": 0, "comments": 0, "gold": 0, "proposer_errors": 0}
        lines.append(
            f"| {_label(k)} | {s['fiches']} | {s['comments']} "
            f"| {s['gold']} | {s.get('proposer_errors') or 0} |")

    actions = report.get("pipeline_actions") or {}
    lines += ["", "## URLs proposées par le réviseur "
                  "(à rendre lisibles par le pipeline)", ""]
    sugg = actions.get("suggested_urls") or []
    if sugg:
        for a in sugg:
            lines.append(f"- **[{_label(a['kind'])}] {a['title']}** "
                         f"({a['id']}) — <{a['url']}>")
    else:
        lines.append("_Aucune URL proposée dans les commentaires._")

    lines += ["", "## Actions pipeline proposées", ""]
    def _section(title: str, rows: list, fmt) -> None:
        if not rows:
            return
        lines.append(f"### {title}")
        lines.append("")
        for r in rows:
            lines.append(fmt(r))
        lines.append("")

    _section("TD gardées (listes officielles validées)",
             actions.get("td_kept") or [],
             lambda r: f"- [{_label(r['kind'])}] {r['title']} — <{r['url']}>")
    _section("TD écartées (candidates blacklist)",
             actions.get("td_dropped") or [],
             lambda r: f"- [{_label(r['kind'])}] {r['title']} — <{r['url']}>")
    _section("URLs écartées",
             actions.get("urls_dropped") or [],
             lambda r: f"- [{_label(r['kind'])}] {r['title']} — <{r['url']}>")
    _section("Domaines écartés",
             [{"d": d} for d in actions.get("domains_dropped") or []],
             lambda r: f"- `{r['d']}`")
    _section("Ports écartés (faux positifs)",
             actions.get("ports_dropped") or [],
             lambda r: f"- [{_label(r['kind'])}] {r['title']} — "
                       f"port `{r['port_id']}`")
    _section("Sites écartés",
             actions.get("sites_dropped") or [],
             lambda r: f"- [{_label(r['kind'])}] {r['title']} — "
                       f"site `{r['site_id']}`")
    _section("Champs enrichis écartés (hallucination probable)",
             actions.get("fields_dropped") or [],
             lambda r: f"- [{_label(r['kind'])}] {r['title']} — "
                       f"champ `{r['field']}`")
    _section("Pages visite AMP retenues",
             actions.get("visit_kept") or [],
             lambda r: f"- {r['title']} — <{r['url']}>")
    _section("AMP sans page visite (constat réviseur)",
             actions.get("no_visit") or [],
             lambda r: f"- {r['title']} ({r['id']})")

    misses = actions.get("proposer_errors") or []
    hints = actions.get("proposer_code_hints") or []
    agreed_n = len(actions.get("proposer_agreed") or [])
    if misses or hints or agreed_n:
        lines += ["## Proposer s'est trompé ici", "",
                  "Le juge automatique (**Proposer**) a divergé du Gold "
                  "humain. Ces écarts servent à corriger le juge local "
                  "et le prompt du mode.", ""]
        if misses:
            lines.append("### Écarts keep / drop")
            lines.append("")
            for r in misses:
                lines.append(
                    f"- [{_label(r.get('kind'))}] **{r.get('title')}** "
                    f"({r.get('id')}) — <{r.get('url')}> : "
                    f"Proposer `{r.get('proposer')}`, "
                    f"humain `{r.get('human')}`")
            lines.append("")
        else:
            lines.append("_Aucun écart Proposer / Gold pour l'instant._")
            lines.append("")
        lines.append(f"Accords Proposer / humain : {agreed_n} URL(s).")
        lines.append("")
        if hints:
            lines.append("### Indices pour le code")
            lines.append("")
            for h in hints:
                lines.append(f"- {h.get('hint')}")
            lines.append("")

    lines += ["## Détail des fiches", ""]
    items = report.get("items") or []
    if not items:
        lines.append("_Aucune fiche touchée par la review._")
    for row in items:
        gold = "oui" if row.get("gold_on") else "non"
        lines.append(f"### [{_label(row['kind'])}] {row['title']} "
                     f"({row['id']}) — Gold : {gold}")
        lines.append("")
        if row.get("golded_at"):
            lines.append(f"Certifiée le {row['golded_at']}.")
            lines.append("")
        if row.get("comment"):
            for text_line in str(row["comment"]).splitlines():
                lines.append(f"> {text_line}")
            lines.append("")
        for url in row.get("suggested_urls") or []:
            lines.append(f"- URL proposée : <{url}>")
        ch = row.get("choices") or {}
        pairs = (
            ("TD gardées", _acts(ch.get("td"), "keep")),
            ("TD écartées", _acts(ch.get("td"), "drop")),
            ("Ports gardés", _acts(ch.get("ports"), "keep")),
            ("Ports écartés", _acts(ch.get("ports"), "drop")),
            ("URLs gardées", _acts(ch.get("urls"), "keep")),
            ("URLs écartées", _acts(ch.get("urls"), "drop")),
            ("Sites gardés", _acts(ch.get("sites"), "keep")),
            ("Sites écartés", _acts(ch.get("sites"), "drop")),
            ("Champs gardés", _acts(ch.get("fields"), "keep")),
            ("Champs écartés", _acts(ch.get("fields"), "drop")),
            ("Visites gardées", _acts(ch.get("visit"), "keep")),
            ("Visites écartées", _acts(ch.get("visit"), "drop")),
        )
        for label, values in pairs:
            if values:
                lines.append(f"- {label} : " + ", ".join(values))
        if ch.get("identity"):
            lines.append(f"- Identité : {ch['identity']}")
        if ch.get("gps"):
            lines.append(f"- GPS : {ch['gps']}")
        if ch.get("overlay"):
            lines.append(f"- Overlay : {ch['overlay']}")
        if ch.get("no_visit"):
            lines.append("- Pas de page visite (constat réviseur)")
        for sid, edit in (ch.get("gps_edit") or {}).items():
            try:
                lines.append(f"- GPS corrigé `{sid}` : "
                             f"{edit.get('lat')}, {edit.get('lon')}")
            except Exception:
                continue
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
