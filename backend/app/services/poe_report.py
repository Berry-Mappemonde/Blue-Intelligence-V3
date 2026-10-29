"""poe_report — Quantitative report of a PoE run, built from TWO materials:
  1. analysis of the run's structured EVENTS (poe_run_events: searches,
     gatekeeper, collection cascade, LLM∥NER comparisons, dual geocoding…);
  2. PORT-BY-PORT COMPARISON with the v1 database (poe_diff).

Outputs: JSON dict (API) and French natural-language markdown.
"""
import statistics

from app.services.poe_diff import diff_run_vs_baseline

_DIFF_SAMPLE = 40


def _pct(part: int, total: int) -> str:
    return f"{100.0 * part / total:.1f} %" if total else "n/a"


def _quantiles(values: list[float]) -> dict:
    if not values:
        return {}
    vs = sorted(values)
    return {
        "n": len(vs),
        "mean": round(statistics.fmean(vs), 2),
        "median": round(vs[len(vs) // 2], 2),
        "p90": round(vs[max(0, int(len(vs) * 0.9) - 1)], 2),
        "max": round(vs[-1], 2),
    }


def _top(counter: dict, n: int = 12) -> list[list]:
    return [[k, v] for k, v in sorted(counter.items(), key=lambda x: -x[1])[:n]]


async def build_run_report(db, run_id: str, include_diff: bool = True) -> dict:
    run = await db.poe_runs.find_one({"_id": run_id}) or {}
    zones = await db.poe_run_zones.find({"run_id": run_id}).to_list(1000)
    ports = await db.poe_run_ports.find({"run_id": run_id}).to_list(20000)
    events = await db.poe_run_events.find(
        {"run_id": run_id}, {"step": 1, "mrgid": 1, "zone": 1, "payload": 1}
    ).to_list(200000)

    by_step: dict[str, list] = {}
    for e in events:
        by_step.setdefault(e["step"], []).append(e)

    # --- Recherche -----------------------------------------------------------
    searches = by_step.get("search", [])
    zones_with_searx = {e["mrgid"] for e in searches
                        if e["payload"].get("engine") == "searxng" and e["payload"].get("n")}
    zones_with_tf = {e["mrgid"] for e in searches
                     if e["payload"].get("engine") == "tinyfish" and e["payload"].get("n")}
    zones_grounded = {e["mrgid"] for e in searches if e["payload"].get("engine") == "grounded"}
    compares = by_step.get("search_compare", [])
    zones_discordant = {e["mrgid"] for e in compares if e["payload"].get("discordant")}
    lang_counter: dict[str, int] = {}
    for e in searches:
        lang_counter[e["payload"].get("lang") or "?"] = lang_counter.get(e["payload"].get("lang") or "?", 0) + 1
    search_stats = {
        "queries_total": len(searches),
        "by_engine": {
            "searxng": sum(1 for e in searches if e["payload"].get("engine") == "searxng"),
            "tinyfish": sum(1 for e in searches
                            if e["payload"].get("engine") == "tinyfish"
                            and not e["payload"].get("scoped")),
            "tinyfish_scoped": sum(1 for e in searches
                                   if e["payload"].get("engine") == "tinyfish"
                                   and e["payload"].get("scoped")),
            "grounded": sum(1 for e in searches if e["payload"].get("engine") == "grounded"),
        },
        "by_lang": lang_counter,
        "zones_with_searxng_results": len(zones_with_searx),
        "zones_with_tinyfish_results": len(zones_with_tf),
        "zones_search_discordant": len(zones_discordant),
        "zones_needing_grounded": len(zones_grounded),
        "level2_retries": sum(1 for e in searches if e["payload"].get("level2")),
    }

    # --- Gatekeeper ----------------------------------------------------------
    # A relaunched (resumed) zone emits several events: keep only the
    # LAST per zone. Domains kept as fallback (ia_sans_source) are
    # NOT official domains — they are counted separately.
    gk_by_zone: dict = {}
    for e in by_step.get("gatekeeper", []):
        gk_by_zone[e.get("mrgid")] = e
    gk = list(gk_by_zone.values())
    official_domains: dict[str, int] = {}
    fallback_domains: dict[str, int] = {}
    rejected_domains: dict[str, int] = {}
    for e in gk:
        bucket = official_domains if e["payload"].get("strictly_official") else fallback_domains
        for d in e["payload"].get("official") or []:
            bucket[d] = bucket.get(d, 0) + 1
        for d in e["payload"].get("rejected") or []:
            rejected_domains[d] = rejected_domains.get(d, 0) + 1
    gatekeeper_stats = {
        "zones_evaluated": len(gk),
        "strictly_official": sum(1 for e in gk if e["payload"].get("strictly_official")),
        "fallback_non_official": sum(1 for e in gk if not e["payload"].get("strictly_official")),
        "bootstrap_events": len(by_step.get("bootstrap", [])),
        "serp_dropped_urls": sum(len(e["payload"].get("dropped") or [])
                                 for e in by_step.get("serp_filter", [])),
        "top_official_domains": _top(official_domains),
        "top_fallback_domains": _top(fallback_domains),
        "top_rejected_domains": _top(rejected_domains),
    }

    # --- Collecte (cascade, blocages, double parsing) --------------------------
    fetches = by_step.get("fetch", [])
    levels: dict[str, int] = {}
    blocked_urls = []
    parse_sims = []
    parse_disagree = 0
    for e in fetches:
        p = e["payload"]
        levels[p.get("level") or "?"] = levels.get(p.get("level") or "?", 0) + 1
        if p.get("blocked"):
            blocked_urls.append(p.get("url"))
        parse = p.get("parse") or {}
        if parse.get("similarity") is not None and parse.get("agree") is not None:
            parse_sims.append(float(parse["similarity"]))
            if parse.get("agree") is False:
                parse_disagree += 1
    fetch_stats = {
        "fetches_total": len(fetches),
        "by_level": levels,
        "render_used": sum(1 for e in fetches if e["payload"].get("render_used")),
        "blocked_pages": len(blocked_urls),
        "blocked_urls": blocked_urls[:20],
        "depth2_followups": len(by_step.get("depth2", [])),
        "sources_used": len(by_step.get("source_used", [])),
        "parse_similarity": _quantiles(parse_sims),
        "parse_disagreements": parse_disagree,
    }

    # --- Extraction LLM ∥ NER ---------------------------------------------------
    # Last event per zone (synthesis retries emit several
    # comparisons); “active NER” = NER actually detected names
    # (a mute NER is a neutral signal, not availability).
    extr_by_zone: dict = {}
    for e in by_step.get("extraction_compare", []):
        extr_by_zone[e.get("mrgid")] = e
    extr = list(extr_by_zone.values())
    extraction_stats = {
        "zones_extracted": len(extr),
        "llm_ports_total": sum(e["payload"].get("llm_n") or 0 for e in extr),
        "ner_active_zones": sum(1 for e in extr if (e["payload"].get("ner_n") or 0) > 0),
        "confirmed_llm_and_ner": sum(len(e["payload"].get("both") or []) for e in extr),
        "llm_only": sum(len(e["payload"].get("llm_only") or []) for e in extr),
        "ner_only_candidates": sum(len(e["payload"].get("ner_only") or []) for e in extr),
        "ner_fallbacks": sum(1 for e in extr if e["payload"].get("fallback") == "ner"),
    }

    # --- Dual geocoding ----------------------------------------------------------
    geo = by_step.get("geocode", [])
    both = [e for e in geo if e["payload"].get("nominatim") and e["payload"].get("geonames")]
    agreement_kms = [e["payload"]["agreement_km"] for e in both
                     if e["payload"].get("agreement_km") is not None]
    arbitration: dict[str, int] = {}
    for e in geo:
        a = e["payload"].get("arbitration")
        if a:
            arbitration[a] = arbitration.get(a, 0) + 1
    geocode_stats = {
        "ports_geocoded_attempts": len(geo),
        "nominatim_hits": sum(1 for e in geo if e["payload"].get("nominatim")),
        "geonames_hits": sum(1 for e in geo if e["payload"].get("geonames")),
        "both_providers": len(both),
        "agree": sum(1 for e in both if e["payload"].get("agree") is True),
        "disagree": sum(1 for e in both if e["payload"].get("agree") is False),
        "agreement_km": _quantiles([float(k) for k in agreement_kms]),
        "arbitration": arbitration,
        "geonames_available": any(e["payload"].get("geonames_available") for e in geo),
    }

    # --- Zones & durations -------------------------------------------------------
    zdones = by_step.get("zone_done", [])
    durations = [float(e["payload"].get("duration_s") or 0) for e in zdones]
    slowest = sorted(zdones, key=lambda e: -(e["payload"].get("duration_s") or 0))[:5]
    by_status: dict[str, int] = {}
    for z in zones:
        by_status[z.get("status") or "?"] = by_status.get(z.get("status") or "?", 0) + 1
    zone_errors = [{"mrgid": z["mrgid"], "name": z.get("name"), "error": z.get("last_error")}
                   for z in zones if z.get("status") == "erreur"]

    # --- Ports (signaux) -----------------------------------------------------------
    geocoded = [p for p in ports if p.get("lat") is not None]
    port_stats = {
        "total": len(ports),
        "geocoded": len(geocoded),
        "validated_in_eez": sum(1 for p in ports if p.get("validated")),
        "extraction_agreement_true": sum(1 for p in ports if p.get("extraction_agreement") is True),
        "extraction_agreement_false": sum(1 for p in ports if p.get("extraction_agreement") is False),
        "extraction_agreement_none": sum(1 for p in ports if p.get("extraction_agreement") is None),
        "geocode_agree_true": sum(1 for p in ports if p.get("geocode_agree") is True),
        "geocode_agree_false": sum(1 for p in ports if p.get("geocode_agree") is False),
        "geocode_agree_none": sum(1 for p in ports if p.get("geocode_agree") is None),
        "multi_source": sum(1 for p in ports if len(set(p.get("source_urls") or [])) >= 2),
    }

    report = {
        "run": {
            "run_id": run_id,
            "label": run.get("label"),
            "state": run.get("state"),
            "started_at": run.get("started_at") or run.get("created_at"),
            "finished_at": run.get("finished_at"),
            "zones_total": run.get("zones_total"),
            "zones_done": run.get("zones_done"),
            "summary": run.get("summary"),
        },
        "zones": {
            "by_status": by_status,
            "errors": zone_errors,
            "duration_s": _quantiles(durations),
            "slowest": [{"zone": e.get("zone"), "duration_s": e["payload"].get("duration_s")}
                        for e in slowest],
        },
        "search": search_stats,
        "gatekeeper": gatekeeper_stats,
        "fetch": fetch_stats,
        "extraction": extraction_stats,
        "geocode": geocode_stats,
        "ports": port_stats,
        "events_total": len(events),
    }

    if include_diff:
        diff = await diff_run_vs_baseline(db, run_id)
        report["diff"] = {
            "summary": diff["summary"],
            "zones": diff["zones"],
            "added_sample": diff["added"][:_DIFF_SAMPLE],
            "removed_sample": diff["removed"][:_DIFF_SAMPLE],
            "matched_flagged_sample": [m for m in diff["matched"] if m["flags"]][:_DIFF_SAMPLE],
        }
    return report


# ---------------------------------------------------------------------------
# Markdown render (French, natural language)
# ---------------------------------------------------------------------------
def report_to_markdown(rep: dict) -> str:
    r, z = rep["run"], rep["zones"]
    s, g, f = rep["search"], rep["gatekeeper"], rep["fetch"]
    x, geo, p = rep["extraction"], rep["geocode"], rep["ports"]
    lines = []
    add = lines.append

    add(f"# Rapport de run PoE — `{r['run_id']}`" + (f" ({r['label']})" if r.get("label") else ""))
    add("")
    add(f"État : **{r.get('state')}** — {r.get('zones_done')}/{r.get('zones_total')} zones, "
        f"démarré {r.get('started_at')}, terminé {r.get('finished_at') or 'en cours'}. "
        f"{rep.get('events_total', 0)} événements structurés journalisés.")
    add("")

    add("## Zones")
    add("")
    add(f"- Statuts : {z['by_status']}")
    if z.get("duration_s"):
        d = z["duration_s"]
        add(f"- Durée par zone : médiane {d.get('median')} s, moyenne {d.get('mean')} s, "
            f"p90 {d.get('p90')} s, max {d.get('max')} s")
    if z.get("slowest"):
        add("- Zones les plus lentes : "
            + ", ".join(f"{e['zone']} ({e['duration_s']} s)" for e in z["slowest"]))
    if z.get("errors"):
        add(f"- Zones en erreur ({len(z['errors'])}) : "
            + ", ".join(f"{e['name']}" for e in z["errors"][:15])
            + ("…" if len(z["errors"]) > 15 else ""))
    add("")

    add("## Recherche de sources")
    add("")
    eng = s.get("by_engine") or {}
    add(f"- {s['queries_total']} requêtes émises ({eng.get('searxng', 0)} SearXNG, "
        f"{eng.get('tinyfish', 0)} TinyFish, {eng.get('tinyfish_scoped', 0)} TinyFish scoped, "
        f"{eng.get('grounded', 0)} recherches groundées) ; langues : {s['by_lang']}")
    add(f"- Zones servies par SearXNG : {s.get('zones_with_searxng_results', 0)} ; "
        f"par TinyFish : {s.get('zones_with_tinyfish_results', 0)} ; "
        f"discordance SERP : {s.get('zones_search_discordant', 0)} ; "
        f"zones ayant nécessité la recherche groundée : {s['zones_needing_grounded']} ; "
        f"Level-2 retries : {s['level2_retries']}")
    add("")

    add("## Gatekeeper (sources officielles)")
    add("")
    total_gk = g["zones_evaluated"] or 1
    add(f"- {g['strictly_official']}/{g['zones_evaluated']} zones avec sources strictement "
        f"officielles ({_pct(g['strictly_official'], total_gk)}) ; "
        f"{g['fallback_non_official']} en repli non officiel (`ia_sans_source`)")
    add(f"- {g['serp_dropped_urls']} URLs écartées par le filtre SERP "
        f"(agrégateurs, interstitiels…) ; {g['bootstrap_events']} bootstrap(s) de whitelist")
    if g.get("top_official_domains"):
        add("- Domaines officiels les plus utilisés : "
            + ", ".join(f"{d} ({n})" for d, n in g["top_official_domains"][:8]))
    if g.get("top_fallback_domains"):
        add("- Domaines de repli non officiels les plus retenus (`ia_sans_source`) : "
            + ", ".join(f"{d} ({n})" for d, n in g["top_fallback_domains"][:8]))
    add("")

    add("## Collecte (cascade + double parsing comparé)")
    add("")
    add(f"- {f['fetches_total']} pages téléchargées — niveaux : {f['by_level']}")
    add(f"- Rendu Chromium local utilisé {f['render_used']} fois (remplaçant gratuit de TinyFish)")
    add(f"- **{f['blocked_pages']} page(s) de blocage anti-bot détectées et invalidées** "
        "(jamais ingérées)" + (f" — ex : {', '.join(u for u in f['blocked_urls'][:3] if u)}"
                               if f.get("blocked_urls") else ""))
    if f.get("parse_similarity"):
        ps = f["parse_similarity"]
        add(f"- Accord trafilatura ∥ Readability : similarité médiane {ps.get('median')} "
            f"(sur {ps.get('n')} pages), {f['parse_disagreements']} divergence(s) fortes")
    add(f"- {f['depth2_followups']} liens internes réglementaires suivis (depth-2) ; "
        f"{f['sources_used']} sources exploitées")
    add("")

    add("## Extraction LLM ∥ NER (parallèle comparé)")
    add("")
    add(f"- {x['llm_ports_total']} ports extraits par le LLM sur {x['zones_extracted']} zones ; "
        f"NER local actif (≥ 1 détection) sur {x['ner_active_zones']} zones")
    add(f"- **{x['confirmed_llm_and_ner']} ports confirmés par les deux extracteurs**, "
        f"{x['llm_only']} vus par le LLM seul, {x['ner_only_candidates']} noms vus par le NER "
        f"seul (candidats à vérifier) ; {x['ner_fallbacks']} zone(s) en fallback NER pur")
    add("")

    add("## Géocodage double (Nominatim ∥ GeoNames)")
    add("")
    add(f"- {geo['ports_geocoded_attempts']} ports géocodés — Nominatim : {geo['nominatim_hits']}, "
        f"GeoNames : {geo['geonames_hits']}, les deux : {geo['both_providers']}")
    if geo["both_providers"]:
        ak = geo.get("agreement_km") or {}
        add(f"- **Accord < 2 km : {geo['agree']}/{geo['both_providers']} "
            f"({_pct(geo['agree'], geo['both_providers'])})** ; désaccords : {geo['disagree']} "
            f"(écart médian {ak.get('median')} km) ; arbitrages : {geo['arbitration']}")
    if not geo["geonames_available"]:
        add("- ⚠ GeoNames indisponible pendant ce run (compte non activé pour le webservice "
            "gratuit) — le signal d'accord inter-géocodeurs est resté vide ; activer le compte "
            "sur geonames.org pour le prochain run")
    add("")

    add("## Ports produits (signaux de confiance)")
    add("")
    add(f"- {p['total']} ports, dont {p['geocoded']} géocodés ({_pct(p['geocoded'], p['total'])}) "
        f"et {p['validated_in_eez']} validés dans leur ZEE ({_pct(p['validated_in_eez'], p['total'])})")
    add(f"- Accord d'extraction LLM∩NER : {p['extraction_agreement_true']} oui / "
        f"{p['extraction_agreement_false']} non / {p['extraction_agreement_none']} sans NER")
    add(f"- Accord de géocodage : {p['geocode_agree_true']} oui / {p['geocode_agree_false']} non / "
        f"{p['geocode_agree_none']} mono-fournisseur ; multi-source : {p['multi_source']}")
    add("")

    if rep.get("diff"):
        ds = rep["diff"]["summary"]
        add("## Comparaison port par port avec la base v1")
        add("")
        add(f"- Base v1 : {ds['baseline_total']} ports ; run : {ds['candidate_total']} ports "
            f"(sur {ds['zones_compared']} zones comparées)")
        add(f"- **Appariés : {ds['matched']}** (dont {ds['unchanged']} inchangés, "
            f"{ds['renamed']} renommés, {ds['moved']} déplacés de > {ds['moved_km_threshold']} km, "
            f"{ds['resourced']} re-sourcés)")
        add(f"- **Nouveaux (run seul) : {ds['added']}** ; **disparus (v1 seule) : {ds['removed']}**")
        add("")
        d = rep["diff"]
        churn = sorted(d.get("zones") or [],
                       key=lambda z: -(z.get("added", 0) + z.get("removed", 0)))[:8]
        if churn:
            add("Zones au plus fort renouvellement (ajouts + disparitions) :")
            for zrow in churn:
                add(f"- {zrow.get('zone_name')}: v1={zrow.get('v1_count')} → v2={zrow.get('v2_count')} "
                    f"(appariés {zrow.get('matched')}, +{zrow.get('added')}, −{zrow.get('removed')})")
            add("")
        if d.get("added_sample"):
            add("Exemples de ports **nouveaux** :")
            for p in d["added_sample"][:6]:
                add(f"- [{p.get('zone_name')}] {p.get('name')}"
                    + (f" ({(p.get('source_urls') or ['?'])[0]})" if p.get("source_urls") else ""))
            add("")
        if d.get("removed_sample"):
            add("Exemples de ports **disparus** (présents en v1 seulement) :")
            for p in d["removed_sample"][:6]:
                add(f"- [{p.get('zone_name')}] {p.get('name')}"
                    + (f" ({(p.get('source_urls') or ['?'])[0]})" if p.get("source_urls") else ""))
            add("")
        flagged = d.get("matched_flagged_sample") or []
        if flagged:
            add("Exemples de ports **appariés mais modifiés** :")
            for m in flagged[:6]:
                flags = ", ".join(m.get("flags") or [])
                add(f"- [{m.get('zone_name')}] {m.get('v1', {}).get('name')} → {m.get('v2', {}).get('name')} "
                    f"({flags}" + (f", {m.get('move_km')} km" if m.get("move_km") else "") + ")")
            add("")
    return "\n".join(lines)
