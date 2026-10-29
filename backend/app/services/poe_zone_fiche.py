"""poe_zone_fiche — Review card of a VLIZ polygon (read-only).

Review (union): all unique TD URLs, all ports (v1 treated
as a run + prod runs + seeds), all BU per port.

Formalities map: the unique run by default. The Gold snapshot appears
only if “Show review” is checked (`visible` / `review`).

Never writes poe_ports / eez_zones. WPI is not a source.
Noonsite and forums do not enter.
"""
from __future__ import annotations

import json
from functools import lru_cache
from urllib.parse import unquote, urlparse

from app.config import DATA_DIR
from app.core.dedup import normalize_name
from app.services.poe_pipeline import domain_of, list_url_bonus, zone_to_item
from app.services.poe_zone_label import attach_zone_labels
from app.services.poe_seeds import SEARCH_EXCLUDE_DOMAINS, url_is_excluded_search
from app.services.poe_rules import counts_as_data_gap, entry_mode, source_is_gold
from app.services.review_gold import is_test_run
from app.services.territory_ref import curated_td_urls, td_url_fits_polygon

# Map banner / popup: one TD URL (list/PDF first). Review does not apply this cap.
FICHE_TD_URL_CAP = 1
PUBLISHED_RUN = "published"


@lru_cache(maxsize=1)
def _community_hosts() -> tuple[str, ...]:
    hosts = {h.lower() for h in SEARCH_EXCLUDE_DOMAINS}
    path = DATA_DIR / "territories.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for h in data.get("blacklist_domains") or []:
            if h:
                hosts.add(str(h).lower().lstrip("."))
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return tuple(sorted(hosts))


def url_is_community(url: str) -> bool:
    """True if Noonsite, wiki, forum, magazine — never state evidence."""
    if url_is_excluded_search(url):
        return True
    host = (urlparse(url or "").hostname or "").lower().lstrip(".")
    if not host:
        return False
    for banned in _community_hosts():
        if host == banned or host.endswith("." + banned):
            return True
    return False


def _clean_url(raw) -> str:
    text = str(raw or "").strip()
    if not text.startswith("http"):
        return ""
    return text.split("#", 1)[0].rstrip("/")


def _url_key(url: str) -> str:
    """Same page: ignore scheme and www. Different paths stay distinct."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parsed.path or "").rstrip("/").lower()
    query = (parsed.query or "").lower()
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def _prefer_rec(prev: dict | None, rec: dict) -> dict:
    if prev is None:
        return rec
    if _url_rank(rec) > _url_rank(prev):
        return rec
    if _url_rank(rec) < _url_rank(prev):
        return prev
    if (rec.get("url") or "").startswith("https") and not (prev.get("url") or "").startswith("https"):
        return rec
    return prev


def _as_source(raw, arm: str) -> dict | None:
    extra: dict = {}
    if isinstance(raw, str):
        url = _clean_url(raw)
    elif isinstance(raw, dict):
        extra = raw
        url = _clean_url(raw.get("url"))
    else:
        return None
    if not url or url_is_community(url):
        return None
    rec = {
        "url": url,
        "domain": extra.get("domain") or domain_of(url),
        "from_arm": arm,
    }
    if extra.get("official") is not None:
        rec["official"] = bool(extra["official"])
    if extra.get("collected_at"):
        rec["collected_at"] = extra["collected_at"]
    return rec


def _collect(raw_lists, arm: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for group in raw_lists:
        for raw in group or []:
            rec = _as_source(raw, arm)
            if rec is None:
                continue
            key = _url_key(rec["url"]) or rec["url"]
            out[key] = _prefer_rec(out.get(key), rec)
    return out


def _url_rank(rec: dict) -> float:
    return list_url_bonus(rec.get("url") or "") + (1.0 if rec.get("official") else 0.0)


def _cap_sources(recs: list[dict], limit: int = FICHE_TD_URL_CAP) -> list[dict]:
    """One URL per domain, list/PDF pages first — map banner."""
    by_dom: dict[str, dict] = {}
    for rec in recs or []:
        dom = (rec.get("domain") or domain_of(rec.get("url") or "") or rec.get("url") or "").lower()
        prev = by_dom.get(dom)
        if prev is None or _url_rank(rec) > _url_rank(prev):
            by_dom[dom] = rec
    ranked = sorted(by_dom.values(), key=_url_rank, reverse=True)
    return ranked[:limit]


def _rank_sources(recs: list[dict]) -> list[dict]:
    """All unique URLs, list/PDF first. No cap, no 1-per-domain."""
    by_url: dict[str, dict] = {}
    for rec in recs or []:
        url = rec.get("url")
        if not url:
            continue
        key = _url_key(url) or url
        by_url[key] = _prefer_rec(by_url.get(key), rec)
    return sorted(by_url.values(), key=_url_rank, reverse=True)


def _best_one(recs: list[dict]) -> dict | None:
    capped = _cap_sources(recs, limit=1)
    return capped[0] if capped else None


def _list_like_from_docs(docs: list[dict] | None) -> list[dict]:
    """A PDF / decree found while searching a port may be the TD list."""
    out: list[dict] = []
    for doc in docs or []:
        for raw in list(doc.get("judge_sources") or []) + list(doc.get("sources_bu") or []):
            rec = _as_source(raw, "td")
            if rec and list_url_bonus(rec["url"]) >= 0.3:
                out.append(rec)
    return out


def bus_by_port_name(docs: list[dict] | None) -> dict[str, list[dict]]:
    """All state URLs of a port (judge / sources_bu), list/PDF first."""
    buckets: dict[str, dict[str, dict]] = {}
    for doc in docs or []:
        key = normalize_name(doc.get("name") or "")
        if not key:
            continue
        bucket = buckets.setdefault(key, {})
        for raw in list(doc.get("judge_sources") or []) + list(doc.get("sources_bu") or []):
            rec = _as_source(raw, "bu")
            if rec is None:
                continue
            ukey = _url_key(rec["url"]) or rec["url"]
            bucket[ukey] = _prefer_rec(bucket.get(ukey), rec)
    return {key: _rank_sources(list(recs.values())) for key, recs in buckets.items() if recs}


def bu_by_port_name(docs: list[dict] | None) -> dict[str, dict]:
    """Best state URL found while searching THIS port (judge / sources_bu)."""
    return {key: recs[0] for key, recs in bus_by_port_name(docs).items() if recs}


def stable_port_id(doc: dict, mrgid: int | None = None) -> str:
    """Review / Gold identity: dedup_key, else `{mrgid}:{normalized name}`."""
    key = str(doc.get("dedup_key") or "").strip()
    if key:
        return key
    try:
        mid = int(mrgid if mrgid is not None else doc.get("mrgid") or 0)
    except (TypeError, ValueError):
        mid = 0
    name = normalize_name(doc.get("name") or "")
    if mid and name:
        return f"{mid}:{name}"
    return str(doc.get("_id") or doc.get("id") or name or "")


def _port_row(doc: dict, url_bu: dict | None = None,
              urls_bu: list[dict] | None = None, *,
              mrgid: int | None = None) -> dict | None:
    name = (doc.get("name") or "").strip()
    if not name:
        return None
    bu_list = [rec for rec in (urls_bu or []) if rec and rec.get("url")]
    if not bu_list and url_bu and url_bu.get("url"):
        bu_list = [url_bu]
    best = url_bu if url_bu and url_bu.get("url") else (bu_list[0] if bu_list else None)
    pid = stable_port_id(doc, mrgid)
    row = {
        "id": pid,
        "port_id": pid,
        "name": name,
        "city": doc.get("city"),
        "lat": doc.get("lat"),
        "lon": doc.get("lon"),
        "confidence": doc.get("confidence"),
        "spatial_kind": doc.get("spatial_kind"),
        "validated": bool(doc.get("validated")),
        "url_bu": best,
        "urls_bu": bu_list,
    }
    row["source_urls"] = [rec["url"] for rec in bu_list]
    return row


def _fiche_kind(zone: dict, n_sources: int, n_ports: int) -> str:
    unclos = zone.get("unclos") if isinstance(zone.get("unclos"), dict) else {}
    if n_sources == 0 and n_ports == 0 and unclos.get("code"):
        return "none"
    if n_sources:
        return "general_list"
    return "none"


def assemble_zone_fiche(zone: dict, ports: list[dict], *,
                        seeds: list[dict] | None = None,
                        run_ports: list[dict] | None = None,
                        run_zones: list[dict] | None = None) -> dict:
    """Build the review card. 0 Atlas writes.

    ``sources_td``: all unique state URLs (list/PDF first).
    ``url_td``: the best one (map banner / popup).
    Each port carries ``urls_bu`` (all) and ``url_bu`` (the first).
    """
    td_raw = [zone.get("sources"), zone.get("sources_td")]
    for rz in run_zones or []:
        td_raw.append(rz.get("sources"))
        td_raw.append(rz.get("sources_td"))
    td_raw.append(curated_td_urls(zone.get("mrgid")))
    td_raw.append(_list_like_from_docs(list(seeds or []) + list(run_ports or [])))
    td_map = _collect(td_raw, "td")
    td_map = {
        key: rec for key, rec in td_map.items()
        if td_url_fits_polygon(rec.get("url") or "", zone)
    }
    bu_lists = bus_by_port_name(list(seeds or []) + list(run_ports or []) + list(ports or []))

    rows = []
    bu_seen: dict[str, dict] = {}
    try:
        zone_mrgid = int(zone.get("mrgid") or 0) or None
    except (TypeError, ValueError):
        zone_mrgid = None
    for doc in ports or []:
        key = normalize_name(doc.get("name") or "")
        urls_bu = list(bu_lists.get(key) or [])
        url_bu = urls_bu[0] if urls_bu else None
        row = _port_row(doc, url_bu, urls_bu, mrgid=zone_mrgid)
        if not row:
            continue
        rows.append(row)
        for rec in urls_bu:
            if rec.get("url"):
                bu_seen.setdefault(rec["url"], rec)
    rows.sort(key=lambda p: (p.get("name") or "").lower())

    td_list = _rank_sources(list(td_map.values()))
    td_total = len(td_list)
    url_td = _best_one(td_list)
    td_keys = {_url_key(rec["url"]) for rec in td_list if rec.get("url")}
    bu_keys = {_url_key(rec["url"]) for rec in bu_seen.values() if rec.get("url")}

    def _with_gold(rec: dict) -> dict:
        url = rec.get("url") or ""
        key = _url_key(url) if url else ""
        # Noonsite never reaches this map (url_is_community). source_is_gold
        # is the law anyway: a noonsite.com URL cannot become Gold.
        if not source_is_gold(
            url,
            seen_by_top_down=key in td_keys,
            seen_by_bottom_up=key in bu_keys,
        ):
            return rec
        return {**rec, "from_arm": "both", "gold": True}

    sources_td = [_with_gold(rec) for rec in td_list]
    if url_td:
        url_td = next(
            (rec for rec in sources_td if rec.get("url") == url_td.get("url")),
            _with_gold(url_td),
        )
    for row in rows:
        marked = [_with_gold(bu) for bu in (row.get("urls_bu") or [])]
        row["urls_bu"] = marked
        for rec in marked:
            if rec.get("url"):
                bu_seen[rec["url"]] = rec
        if row.get("url_bu"):
            row["url_bu"] = _with_gold(row["url_bu"])
            if row["url_bu"].get("url"):
                bu_seen[row["url_bu"]["url"]] = row["url_bu"]

    sources_bu = list(bu_seen.values())
    item = zone_to_item(zone)
    item["url_td"] = url_td
    item["sources_td"] = sources_td
    item["sources_bu"] = sources_bu
    item["sources_td_total"] = td_total
    item["sources_bu_total"] = len(sources_bu)
    item["urls"] = ([url_td] if url_td else []) + [
        rec for rec in sources_bu if not url_td or rec.get("url") != url_td.get("url")
    ]
    item["kind"] = _fiche_kind(zone, td_total + len(sources_bu), len(rows))
    item["ports"] = rows
    item["wrote_poe_ports"] = False
    item["crawled"] = False
    item["entry_mode"] = entry_mode(zone)
    item["data_gap"] = counts_as_data_gap(
        zone,
        missing_designated_list=not sources_td and not sources_bu,
    )
    return item


async def _find_mrgid(coll, mrgid: int) -> list[dict]:
    if coll is None:
        return []
    try:
        return await coll.find({"mrgid": mrgid}).to_list(8000)
    except Exception:
        return []


_LABEL_PROJ = {
    "_id": 0, "mrgid": 1, "name": 1, "geoname": 1, "sovereign": 1,
    "iso2": 1, "sov_iso2": 1, "pol_type": 1,
}


async def _label_zone(db, zone: dict) -> dict:
    """One card = this mrgid. The label accounts for the sovereign's other polygons."""
    sov = (zone.get("sovereign") or "").strip()
    if not sov:
        attach_zone_labels([zone])
        return zone
    try:
        sibs = await db.eez_zones.find({"sovereign": sov}, _LABEL_PROJ).to_list(500)
    except Exception:
        sibs = []
    if not sibs:
        sibs = [{k: zone.get(k) for k in (
            "mrgid", "name", "geoname", "sovereign", "iso2", "sov_iso2", "pol_type")}]
    attach_zone_labels(sibs)
    want = int(zone.get("mrgid") or 0)
    hit = next((s for s in sibs if int(s.get("mrgid") or 0) == want), None)
    if hit:
        for key in ("label", "qualifier", "qualifier_key", "disambiguated"):
            zone[key] = hit.get(key)
    return zone


def _is_published(run_id: str | None) -> bool:
    return not run_id or run_id == PUBLISHED_RUN


async def _find_run_mrgid(coll, run_id: str, mrgid: int) -> list[dict]:
    if coll is None:
        return []
    try:
        return await coll.find({"run_id": run_id, "mrgid": int(mrgid)}).to_list(8000)
    except Exception:
        return []


async def _runs_by_id(db) -> dict[str, dict]:
    coll = getattr(db, "poe_runs", None)
    if coll is None:
        return {}
    try:
        docs = await coll.find({}).to_list(5000)
    except Exception:
        return {}
    return {str(d.get("_id")): d for d in docs or []}


def _keep_prod_run(doc: dict, runs_by_id: dict[str, dict]) -> bool:
    rid = str(doc.get("run_id") or "")
    if not rid:
        return True
    meta = runs_by_id.get(rid) or {"_id": rid, "label": rid}
    return not is_test_run(meta)


def _filter_prod_runs(docs: list[dict], runs_by_id: dict[str, dict]) -> list[dict]:
    return [d for d in docs or [] if _keep_prod_run(d, runs_by_id)]


def _port_rank(doc: dict) -> tuple:
    has_geo = doc.get("lat") is not None and doc.get("lon") is not None
    return (
        1 if doc.get("validated") else 0,
        1 if has_geo else 0,
        int(doc.get("confidence") or 0),
    )


def _as_run_port(doc: dict, run_id: str) -> dict:
    if doc.get("run_id"):
        return doc
    out = dict(doc)
    out["run_id"] = run_id
    return out


def _merge_union_ports(v1: list[dict], run_ports: list[dict],
                       seeds: list[dict]) -> list[dict]:
    """v1 = `published` run, equal to the other runs; seeds after."""
    by: dict[str, dict] = {}
    for doc in list(v1 or []) + list(run_ports or []):
        key = normalize_name(doc.get("name") or "")
        if not key:
            continue
        tagged = doc if doc.get("run_id") else _as_run_port(doc, PUBLISHED_RUN)
        prev = by.get(key)
        if prev is None or _port_rank(tagged) > _port_rank(prev):
            by[key] = tagged
    for doc in seeds or []:
        key = normalize_name(doc.get("name") or "")
        if not key or key in by:
            continue
        by[key] = doc
    return list(by.values())


async def build_zone_fiche(db, mrgid: int, run_id: str | None = None,
                           *, union: bool = False) -> dict | None:
    """Load Atlas read-only and assemble the card.

    ``run_id=None`` / ``published``: that run only (the old v1).
    ``union=True`` (Review): v1 like the other runs + polygon
    ``poe_run_ports`` + seeds, canaries excluded.
    A precise ``run_id``: ports and sources of THIS run (debug).
    """
    mid = int(mrgid)
    zone = await db.eez_zones.find_one({"mrgid": mid}, {"geometry": 0})
    seeds = await _find_mrgid(getattr(db, "poe_seed_ports", None), mid)
    if _is_published(run_id):
        if not zone:
            return None
        ports = await db.poe_ports.find({"mrgid": mid}).to_list(2000)
        runs_by_id = await _runs_by_id(db)
        run_zones = _filter_prod_runs(
            await _find_mrgid(getattr(db, "poe_run_zones", None), mid),
            runs_by_id,
        )
        if union:
            run_ports = _filter_prod_runs(
                await _find_mrgid(getattr(db, "poe_run_ports", None), mid),
                runs_by_id,
            )
            ports = _merge_union_ports(ports, run_ports, seeds)
        else:
            run_ports = []
        scope = "union" if union else "published"
    else:
        ports = await _find_run_mrgid(getattr(db, "poe_run_ports", None), run_id, mid)
        run_zones = await _find_run_mrgid(getattr(db, "poe_run_zones", None), run_id, mid)
        run_ports = ports
        scope = "run"
        if not zone:
            zone = run_zones[0] if run_zones else None
        elif run_zones:
            rz = run_zones[0]
            zone = {
                **zone,
                "sources": rz.get("sources") if rz.get("sources") is not None else zone.get("sources"),
                "sources_td": rz.get("sources_td") if rz.get("sources_td") is not None else zone.get("sources_td"),
                "confidence_avg": rz.get("confidence_avg", zone.get("confidence_avg")),
                "status": rz.get("status") or zone.get("status"),
                "poe_count": rz.get("poe_count", zone.get("poe_count")),
            }
        if not zone:
            return None
    zone = await _label_zone(db, zone)
    fiche = assemble_zone_fiche(
        zone, ports, seeds=seeds, run_ports=run_ports, run_zones=run_zones)
    fiche["fiche_scope"] = scope
    return fiche


async def build_map_zone_fiche(db, mrgid: int) -> dict | None:
    """Map card of the certified run: Gold snapshot. Not live."""
    from app.services.review_choices import snapshot_to_fiche
    from app.services.review_gold import eez_is_published, get_override

    mid = int(mrgid)
    ov = await get_override(db, "eez", str(mid))
    if not eez_is_published(ov):
        return None
    zone = await db.eez_zones.find_one({"mrgid": mid}, {"geometry": 0})
    if not zone:
        return None
    zone = await _label_zone(db, zone)
    return snapshot_to_fiche(zone, ov.get("snapshot") or {})
