"""Reviewer keep/drop choices — five queues, same gesture.

Collection `review_choices`: what the reviewer ticks on the union card.
Never writes `projects` / `poe_ports` / `eez_zones` / `marinas` /
`capitaineries` / `amp_sites`. URLs are stored as lists
(not as Mongo keys — a `.` in the URL would break a subdocument).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.geo import haversine_km, ocean_fallback_coords
from app.services.poe_pipeline import zone_to_item

CHOICE_KINDS = ("eez", "project", "marina", "capitainerie", "amp")
TARGETS = (
    "td", "port", "bu",
    "url", "site", "identity", "gps", "field", "overlay",
    "visit", "no_visit", "gps_edit",
)
ACTIONS = ("keep", "drop", "clear")
SCALAR_TARGETS = ("identity", "gps", "overlay", "no_visit")
FIELD_KEEP = ("canal_vhf", "places_visiteurs", "tirant_eau_max_metres",
              "telephone", "telephone_capitainerie", "services_disponibles",
              "website", "maps_place_url")

GOLD_INCOMPLETE = (
    "gold incomplete: keep at least one official list "
    "(or UNCLOS none / zero ports)"
)
GOLD_INCOMPLETE_BY_KIND = {
    "eez": GOLD_INCOMPLETE,
    "project": (
        "gold incomplete: keep a project URL and accept at least one "
        "site_ok action site"
    ),
    "marina": "gold incomplete: accept identity and GPS",
    "capitainerie": "gold incomplete: accept building identity and GPS",
    "amp": (
        "gold incomplete: keep a visit URL distinct from manager, "
        "or confirm no visit page"
    ),
}

FALLBACK_SOURCES = frozenset({
    "ocean-region-fallback", "ocean_fallback", "ocean-fallback",
    "fallback", "ocean_region_fallback", "snap_to_ocean", "hq", "hq_suspect",
})
FALLBACK_KM = 50.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sid(value) -> str:
    return "" if value is None else str(value)


def choice_key(kind: str, entity_id: str) -> str:
    return f"{kind}:{_sid(entity_id)}"


def gold_incomplete_message(kind: str) -> str:
    return GOLD_INCOMPLETE_BY_KIND.get(kind or "eez", GOLD_INCOMPLETE)


def empty_choices() -> dict:
    return {
        "td": {},
        "ports": {},
        "bu": {},
        "urls": {},
        "sites": {},
        "fields": {},
        "visit": {},
        "identity": None,
        "gps": None,
        "overlay": None,
        "no_visit": False,
        "gps_edit": {},
    }


def public_choices(doc: dict | None) -> dict:
    """Maps for the UI: td[url], ports[port_id], bu[port_id][url] → keep|drop."""
    if not doc:
        return empty_choices()
    td: dict[str, str] = {}
    for row in doc.get("td") or []:
        url, act = row.get("url"), row.get("action")
        if url and act in ("keep", "drop"):
            td[str(url)] = act
    ports: dict[str, str] = {}
    for row in doc.get("ports") or []:
        pid, act = row.get("port_id"), row.get("action")
        if pid and act in ("keep", "drop"):
            ports[str(pid)] = act
    bu: dict[str, dict[str, str]] = {}
    for row in doc.get("bu") or []:
        pid, url, act = row.get("port_id"), row.get("url"), row.get("action")
        if pid and url and act in ("keep", "drop"):
            bu.setdefault(str(pid), {})[str(url)] = act
    urls: dict[str, str] = {}
    for row in doc.get("urls") or []:
        url, act = row.get("url"), row.get("action")
        if url and act in ("keep", "drop"):
            urls[str(url)] = act
    sites: dict[str, str] = {}
    for row in doc.get("sites") or []:
        sid, act = row.get("site_id"), row.get("action")
        if sid and act in ("keep", "drop"):
            sites[str(sid)] = act
    fields: dict[str, str] = {}
    for row in doc.get("fields") or []:
        fid, act = row.get("field"), row.get("action")
        if fid and act in ("keep", "drop"):
            fields[str(fid)] = act
    visit: dict[str, str] = {}
    for row in doc.get("visit") or []:
        url, act = row.get("url"), row.get("action")
        if url and act in ("keep", "drop"):
            visit[str(url)] = act
    gps_edit: dict[str, dict] = {}
    for row in doc.get("gps_edit") or []:
        sid = _sid(row.get("site_id") or "main")
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        gps_edit[sid] = {"lat": lat, "lon": lon}
    identity = doc.get("identity") if doc.get("identity") in ("keep", "drop") else None
    gps = doc.get("gps") if doc.get("gps") in ("keep", "drop") else None
    overlay = doc.get("overlay") if doc.get("overlay") in ("keep", "drop") else None
    return {
        "td": td,
        "ports": ports,
        "bu": bu,
        "urls": urls,
        "sites": sites,
        "fields": fields,
        "visit": visit,
        "identity": identity,
        "gps": gps,
        "overlay": overlay,
        "no_visit": bool(doc.get("no_visit")),
        "gps_edit": gps_edit,
    }


def _pack(public: dict) -> dict:
    td = [{"url": u, "action": a} for u, a in (public.get("td") or {}).items()
          if a in ("keep", "drop")]
    ports = [{"port_id": pid, "action": a}
             for pid, a in (public.get("ports") or {}).items()
             if a in ("keep", "drop")]
    bu = []
    for pid, urls in (public.get("bu") or {}).items():
        for url, act in (urls or {}).items():
            if act in ("keep", "drop"):
                bu.append({"port_id": pid, "url": url, "action": act})
    url_rows = [{"url": u, "action": a}
                for u, a in (public.get("urls") or {}).items()
                if a in ("keep", "drop")]
    site_rows = [{"site_id": sid, "action": a}
                 for sid, a in (public.get("sites") or {}).items()
                 if a in ("keep", "drop")]
    field_rows = [{"field": fid, "action": a}
                  for fid, a in (public.get("fields") or {}).items()
                  if a in ("keep", "drop")]
    visit_rows = [{"url": u, "action": a}
                  for u, a in (public.get("visit") or {}).items()
                  if a in ("keep", "drop")]
    gps_edit = []
    for sid, xy in (public.get("gps_edit") or {}).items():
        if not isinstance(xy, dict):
            continue
        try:
            gps_edit.append({
                "site_id": sid,
                "lat": float(xy["lat"]),
                "lon": float(xy["lon"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "td": td,
        "ports": ports,
        "bu": bu,
        "urls": url_rows,
        "sites": site_rows,
        "fields": field_rows,
        "visit": visit_rows,
        "identity": public.get("identity") if public.get("identity") in ("keep", "drop") else None,
        "gps": public.get("gps") if public.get("gps") in ("keep", "drop") else None,
        "overlay": public.get("overlay") if public.get("overlay") in ("keep", "drop") else None,
        "no_visit": bool(public.get("no_visit")),
        "gps_edit": gps_edit,
    }


def _norm_url(url: str | None) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        from app.services.amp import normalize_url
        return normalize_url(raw) or raw.rstrip("/").lower()
    except Exception:
        return raw.rstrip("/").lower()


def urls_same(a: str | None, b: str | None) -> bool:
    na, nb = _norm_url(a), _norm_url(b)
    return bool(na and nb and na == nb)


def _has_coords(doc: dict | None) -> bool:
    if not doc:
        return False
    try:
        lat, lon = float(doc["lat"]), float(doc["lon"])
    except (KeyError, TypeError, ValueError):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def site_ok(site: dict | None, *, title: str = "") -> bool:
    """Visitable action place — not snapped / fallback / HQ."""
    if not site or not _has_coords(site):
        return False
    if site.get("snapped") or site.get("snapped_coastal") or site.get("hq_suspect"):
        return False
    geo = str(site.get("geo_source") or "").strip().lower()
    if geo in FALLBACK_SOURCES or "snap" in geo:
        return False
    if site.get("unlocated"):
        return False
    name = str(site.get("name") or site.get("title") or title or "")
    try:
        lat, lon = float(site["lat"]), float(site["lon"])
        if name:
            flat, flon = ocean_fallback_coords(name)
            if haversine_km(lat, lon, flat, flon) < FALLBACK_KM:
                return False
    except Exception:
        pass
    return True


def project_sites(fiche: dict | None) -> list[dict]:
    """Project sites[], or a site synthesized from the head GPS."""
    fiche = fiche or {}
    sites = []
    for i, s in enumerate(fiche.get("sites") or []):
        if not isinstance(s, dict):
            continue
        row = dict(s)
        row["site_id"] = _sid(row.get("site_id") or row.get("id") or f"site-{i}")
        sites.append(row)
    if sites:
        return sites
    if _has_coords(fiche):
        return [{
            "site_id": "main",
            "name": fiche.get("location") or fiche.get("title"),
            "lat": fiche.get("lat"),
            "lon": fiche.get("lon"),
            "geo_source": fiche.get("geo_source"),
            "snapped": bool(fiche.get("snapped") or fiche.get("snapped_coastal")),
            "hq_suspect": bool(fiche.get("hq_suspect")),
        }]
    return []


def effective_site(site: dict, choices: dict | None) -> dict:
    """GPS edited in review_choices until Gold."""
    out = dict(site or {})
    sid = _sid(out.get("site_id") or "main")
    xy = ((choices or {}).get("gps_edit") or {}).get(sid)
    if isinstance(xy, dict) and xy.get("lat") is not None and xy.get("lon") is not None:
        out["lat"] = xy["lat"]
        out["lon"] = xy["lon"]
        out["geo_source"] = "review_edit"
        out["snapped"] = False
        out["snapped_coastal"] = False
        out["hq_suspect"] = False
    return out


def project_urls(fiche: dict | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in (fiche or {}).get("urls") or []:
        href = raw if isinstance(raw, str) else (raw or {}).get("url")
        href = (href or "").strip()
        if href and href not in seen:
            seen.add(href)
            out.append(href)
    href = str((fiche or {}).get("url") or "").strip()
    if href and href not in seen:
        out.insert(0, href)
    return out


def comment_http_urls(comment: str | None) -> list[str]:
    """URLs pasted in the reviewer comment (lists found by hand)."""
    try:
        from app.services.amp import extract_urls
        return [u for u in extract_urls(comment) if str(u).startswith("http")]
    except Exception:
        return []


def kept_official_urls(fiche: dict | None, choices: dict | None,
                       comment: str | None = "") -> list[str]:
    """Ticked TD + comment URLs — the polygon's documentary truth."""
    fiche = fiche or {}
    td_map = (choices or {}).get("td") or {}
    seen: set[str] = set()
    out: list[str] = []
    for rec in fiche.get("sources_td") or []:
        url = rec.get("url")
        if url and td_map.get(url) == "keep" and url not in seen:
            seen.add(url)
            out.append(url)
    for url, act in td_map.items():
        if act == "keep" and url and url not in seen:
            seen.add(url)
            out.append(url)
    for url in comment_http_urls(comment):
        if url not in seen:
            seen.add(url)
            out.append(url)
    from app.services.poe_rules import is_noonsite_url
    # A noonsite.com URL never lights Gold, even if the reviewer ticked it.
    return [u for u in out if not is_noonsite_url(u)]


def dropped_official_urls(fiche: dict | None, choices: dict | None) -> list[str]:
    td_map = (choices or {}).get("td") or {}
    out: list[str] = []
    seen: set[str] = set()
    for rec in (fiche or {}).get("sources_td") or []:
        url = rec.get("url")
        if url and td_map.get(url) == "drop" and url not in seen:
            seen.add(url)
            out.append(url)
    for url, act in td_map.items():
        if act == "drop" and url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def gold_ready(fiche: dict | None, choices: dict | None, kind: str | None = None,
               comment: str | None = "") -> bool:
    """Gold lights only if the reviewer decided what the mode requires."""
    fiche = fiche or {}
    ch = choices or empty_choices()
    k = (kind or fiche.get("review_kind") or "").strip()
    if not k:
        inner = str(fiche.get("kind") or "")
        k = inner if inner in CHOICE_KINDS else "eez"
    if k == "project":
        return _gold_ready_project(fiche, ch)
    if k == "marina":
        return _gold_ready_marina(fiche, ch)
    if k == "capitainerie":
        return _gold_ready_capitainerie(fiche, ch)
    if k == "amp":
        return _gold_ready_amp(fiche, ch)
    return _gold_ready_eez(fiche, ch, comment=comment)


def _gold_ready_eez(fiche: dict, ch: dict, comment: str | None = "") -> bool:
    """At least one official list kept (TD or pasted URL), or UNCLOS / none."""
    if kept_official_urls(fiche, ch, comment):
        return True
    td_urls = [r.get("url") for r in (fiche.get("sources_td") or []) if r.get("url")]
    unclos = fiche.get("unclos") if isinstance(fiche.get("unclos"), dict) else {}
    kind = str(fiche.get("kind") or "")
    ports = fiche.get("ports") or []
    if td_urls:
        return False
    return kind == "none" or bool(unclos.get("code")) or not ports


def _gold_ready_project(fiche: dict, ch: dict) -> bool:
    urls = project_urls(fiche)
    url_ok = any((ch.get("urls") or {}).get(u) == "keep" for u in urls)
    if not url_ok:
        return False
    title = str(fiche.get("title") or "")
    smap = ch.get("sites") or {}
    for site in project_sites(fiche):
        if smap.get(_sid(site.get("site_id"))) != "keep":
            continue
        if site_ok(effective_site(site, ch), title=title):
            return True
    return False


def _gold_ready_marina(fiche: dict, ch: dict) -> bool:
    return ch.get("identity") == "keep" and ch.get("gps") == "keep" and _has_coords(fiche)


def _gold_ready_capitainerie(fiche: dict, ch: dict) -> bool:
    if ch.get("identity") != "keep" or ch.get("gps") != "keep":
        return False
    if not _has_coords(fiche):
        return False
    fmap = ch.get("fields") or {}
    for key in ("telephone", "canal_vhf"):
        if not fiche.get(key):
            continue
        # Shown → decided (keep if sourced, clear if invented). No silent Gold.
        if fmap.get(key) not in ("keep", "drop"):
            return False
    return True


def _gold_ready_amp(fiche: dict, ch: dict) -> bool:
    manager = fiche.get("manager_url")
    if ch.get("no_visit"):
        return True
    for url, act in (ch.get("visit") or {}).items():
        if act == "keep" and url and not urls_same(url, manager):
            return True
    return False


def finalize_choices(fiche: dict, choices: dict) -> dict:
    """At Gold: unticked TD → drop. Ports are no longer a Review verdict."""
    out = {
        "td": dict((choices or {}).get("td") or {}),
        "ports": dict((choices or {}).get("ports") or {}),
        "bu": {pid: dict(m) for pid, m in ((choices or {}).get("bu") or {}).items()},
        "urls": dict((choices or {}).get("urls") or {}),
        "sites": dict((choices or {}).get("sites") or {}),
        "fields": dict((choices or {}).get("fields") or {}),
        "visit": dict((choices or {}).get("visit") or {}),
        "identity": (choices or {}).get("identity"),
        "gps": (choices or {}).get("gps"),
        "overlay": (choices or {}).get("overlay"),
        "no_visit": bool((choices or {}).get("no_visit")),
        "gps_edit": dict((choices or {}).get("gps_edit") or {}),
    }
    for rec in fiche.get("sources_td") or []:
        url = rec.get("url")
        if url and url not in out["td"]:
            out["td"][url] = "drop"
    for p in fiche.get("ports") or []:
        pid = _sid(p.get("port_id") or p.get("id"))
        if not pid:
            continue
        bmap = out["bu"].setdefault(pid, {})
        for rec in p.get("urls_bu") or []:
            url = rec.get("url")
            if url and url not in bmap:
                bmap[url] = "drop"
    return out


def build_gold_snapshot(fiche: dict, choices: dict, comment: str = "",
                        kind: str | None = None) -> dict:
    k = (kind or fiche.get("review_kind") or fiche.get("kind") or "eez").strip()
    if k == "project":
        return _snapshot_project(fiche, choices, comment)
    if k == "marina":
        return _snapshot_marina(fiche, choices, comment)
    if k == "capitainerie":
        return _snapshot_capitainerie(fiche, choices, comment)
    if k == "amp":
        return _snapshot_amp(fiche, choices, comment)
    return _snapshot_eez(fiche, choices, comment)


def snapshot_ports_visible(snapshot: dict | None) -> bool:
    """Map points only after extraction from kept docs.

    Old snapshot without ``ports_status`` but with ports: show them
    (Gold from before the documents contract).
    """
    if not snapshot:
        return False
    status = snapshot.get("ports_status")
    if status in ("pending_extract", "failed"):
        return False
    if status in ("extracted", "empty"):
        return True
    return bool(snapshot.get("ports"))


def _snapshot_eez(fiche: dict, choices: dict, comment: str) -> dict:
    kept_urls = kept_official_urls(fiche, choices, comment)
    by_url = {rec.get("url"): rec for rec in (fiche.get("sources_td") or [])
              if rec.get("url")}
    kept_td: list[dict] = []
    for url in kept_urls:
        rec = dict(by_url.get(url) or {"url": url, "official": True})
        if url not in by_url:
            rec["from_arm"] = "review_comment"
        kept_td.append(rec)
    url_td = kept_td[0] if kept_td else None
    dropped = [{"url": u} for u in dropped_official_urls(fiche, choices)]
    return {
        "mrgid": fiche.get("mrgid"),
        "sources_td": kept_td,
        "url_td": url_td,
        "dropped_td": dropped,
        "ports": [],
        "ports_status": "pending_extract",
        "comment": comment or "",
        "golded_at": now_iso(),
        "kind": fiche.get("kind"),
        "label": fiche.get("label"),
        "zone_name": fiche.get("name") or fiche.get("label"),
        "iso2": fiche.get("iso2"),
    }


def _snapshot_project(fiche: dict, choices: dict, comment: str) -> dict:
    url_map = (choices or {}).get("urls") or {}
    kept_urls = [u for u in project_urls(fiche) if url_map.get(u) == "keep"]
    smap = (choices or {}).get("sites") or {}
    title = str(fiche.get("title") or "")
    kept_sites = []
    for site in project_sites(fiche):
        sid = _sid(site.get("site_id"))
        if smap.get(sid) != "keep":
            continue
        eff = effective_site(site, choices)
        if not site_ok(eff, title=title):
            continue
        kept_sites.append({
            "site_id": sid,
            "name": eff.get("name") or eff.get("location"),
            "lat": eff.get("lat"),
            "lon": eff.get("lon"),
            "geo_source": eff.get("geo_source"),
        })
    return {
        "kind": "project",
        "id": fiche.get("id"),
        "title": fiche.get("title"),
        "urls": kept_urls,
        "url": kept_urls[0] if kept_urls else fiche.get("url"),
        "funders": list(fiche.get("funders") or []),
        "sites": kept_sites,
        "lat": (kept_sites[0]["lat"] if kept_sites else fiche.get("lat")),
        "lon": (kept_sites[0]["lon"] if kept_sites else fiche.get("lon")),
        "comment": comment or "",
        "golded_at": now_iso(),
    }


def _kept_fields(fiche: dict, choices: dict) -> dict:
    fmap = (choices or {}).get("fields") or {}
    out = {}
    for key in FIELD_KEEP:
        if fiche.get(key) in (None, "", []):
            continue
        if fmap.get(key) == "keep":
            out[key] = fiche.get(key)
    return out


def _kept_url_list(fiche: dict, choices: dict, keys: tuple[str, ...]) -> list[str]:
    umap = (choices or {}).get("urls") or {}
    out = []
    for key in keys:
        href = fiche.get(key)
        if href and umap.get(href) == "keep":
            out.append(href)
    return out


def _snapshot_marina(fiche: dict, choices: dict, comment: str) -> dict:
    return {
        "kind": "marina",
        "id": fiche.get("id"),
        "name": fiche.get("name"),
        "osm_id": fiche.get("osm_id"),
        "lat": fiche.get("lat"),
        "lon": fiche.get("lon"),
        "source": fiche.get("source"),
        "urls": _kept_url_list(fiche, choices, ("website", "maps_place_url", "maps_url")),
        "fields": _kept_fields(fiche, choices),
        "comment": comment or "",
        "golded_at": now_iso(),
    }


def _snapshot_capitainerie(fiche: dict, choices: dict, comment: str) -> dict:
    overlay = (choices or {}).get("overlay")
    return {
        "kind": "capitainerie",
        "id": fiche.get("id"),
        "name": fiche.get("name"),
        "osm_id": fiche.get("osm_id"),
        "shom_id": fiche.get("shom_id"),
        "noaa_id": fiche.get("noaa_id"),
        "lat": fiche.get("lat"),
        "lon": fiche.get("lon"),
        "sources": list(fiche.get("sources") or []),
        "overlay": overlay,
        "urls": _kept_url_list(fiche, choices, ("website",)),
        "fields": _kept_fields(fiche, choices),
        "comment": comment or "",
        "golded_at": now_iso(),
    }


def _snapshot_amp(fiche: dict, choices: dict, comment: str) -> dict:
    manager = fiche.get("manager_url")
    visit = None
    if not (choices or {}).get("no_visit"):
        for url, act in ((choices or {}).get("visit") or {}).items():
            if act == "keep" and url and not urls_same(url, manager):
                visit = url
                break
    return {
        "kind": "amp",
        "id": fiche.get("id") or fiche.get("site_id"),
        "site_id": fiche.get("site_id") or fiche.get("id"),
        "name": fiche.get("name"),
        "manager_url": manager,
        "visit_url": visit,
        "no_visit": bool((choices or {}).get("no_visit")),
        "comment": comment or "",
        "golded_at": now_iso(),
    }


def snapshot_to_fiche(zone: dict, snapshot: dict) -> dict:
    """Map / popup card from the Gold snapshot (no longer v1)."""
    item = zone_to_item(zone)
    sources_td = list(snapshot.get("sources_td") or [])
    url_td = snapshot.get("url_td")
    ports = list(snapshot.get("ports") or [])
    bu_seen: dict[str, dict] = {}
    for p in ports:
        for rec in p.get("urls_bu") or []:
            if rec.get("url"):
                bu_seen[rec["url"]] = rec
        bu = p.get("url_bu")
        if isinstance(bu, dict) and bu.get("url"):
            bu_seen.setdefault(bu["url"], bu)
    item["url_td"] = url_td
    item["sources_td"] = sources_td
    item["sources_bu"] = list(bu_seen.values())
    item["sources_td_total"] = len(sources_td)
    item["sources_bu_total"] = len(bu_seen)
    item["urls"] = ([url_td] if url_td else []) + [
        rec for rec in bu_seen.values()
        if not url_td or rec.get("url") != (url_td.get("url") if isinstance(url_td, dict) else None)
    ]
    item["kind"] = snapshot.get("kind") or (
        "none" if not sources_td and not ports else "general_list")
    item["ports"] = ports if snapshot_ports_visible(snapshot) else []
    item["poe_count"] = len(item["ports"])
    item["ports_status"] = snapshot.get("ports_status")
    item["wrote_poe_ports"] = False
    item["crawled"] = False
    item["fiche_scope"] = "gold"
    item["gold_published"] = True
    item["golded_at"] = snapshot.get("golded_at")
    return item


def snapshot_port_docs(mrgid: int, snapshot: dict) -> list[dict]:
    """poe_ports-style docs for the map GeoJSON — stable ids `gold:{mrgid}:{port_id}`."""
    docs: list[dict] = []
    iso2 = snapshot.get("iso2")
    zone_name = snapshot.get("zone_name") or snapshot.get("label")
    if not snapshot_ports_visible(snapshot):
        return []
    for p in snapshot.get("ports") or []:
        pid = _sid(p.get("port_id") or p.get("id"))
        bu = p.get("url_bu")
        docs.append({
            "_id": f"gold:{mrgid}:{pid}",
            "name": p.get("name"),
            "city": p.get("city"),
            "lat": p.get("lat"),
            "lon": p.get("lon"),
            "mrgid": int(mrgid),
            "zone_name": zone_name,
            "country_iso2": iso2,
            "confidence": p.get("confidence"),
            "validated": bool(p.get("validated")),
            "spatial_kind": p.get("spatial_kind"),
            "url_bu": bu,
            "source_urls": list(p.get("source_urls") or []),
        })
    return docs


def apply_snapshot_to_doc(kind: str, doc: dict, snapshot: dict | None) -> dict:
    """Map “Show review” layer: the Gold snapshot overlays live."""
    if not snapshot or not isinstance(snapshot, dict):
        return doc
    out = dict(doc)
    if kind == "project":
        if snapshot.get("url"):
            out["url"] = snapshot["url"]
        if snapshot.get("lat") is not None:
            out["lat"] = snapshot["lat"]
        if snapshot.get("lon") is not None:
            out["lon"] = snapshot["lon"]
        if snapshot.get("sites"):
            out["sites"] = snapshot["sites"]
    elif kind == "amp":
        out["manager_url"] = snapshot.get("manager_url") or out.get("manager_url")
        out["visit_url"] = snapshot.get("visit_url")
        if snapshot.get("no_visit"):
            out["visit_url"] = None
            out["visit_url_status"] = "none"
    elif kind in ("marina", "capitainerie"):
        if snapshot.get("lat") is not None:
            out["lat"] = snapshot["lat"]
        if snapshot.get("lon") is not None:
            out["lon"] = snapshot["lon"]
        for key, val in (snapshot.get("fields") or {}).items():
            out[key] = val
    return out


async def get_choices(db, kind: str, entity_id: str) -> dict:
    try:
        doc = await db.review_choices.find_one({"_id": choice_key(kind, entity_id)})
    except Exception:
        doc = None
    return public_choices(doc)


async def save_choices_doc(db, kind: str, entity_id: str, public: dict) -> dict:
    eid = _sid(entity_id)
    cid = choice_key(kind, eid)
    packed = _pack(public)
    doc = {
        "_id": cid,
        "kind": kind,
        "entity_id": eid,
        "td": packed["td"],
        "ports": packed["ports"],
        "bu": packed["bu"],
        "urls": packed["urls"],
        "sites": packed["sites"],
        "fields": packed["fields"],
        "visit": packed["visit"],
        "identity": packed["identity"],
        "gps": packed["gps"],
        "overlay": packed["overlay"],
        "no_visit": packed["no_visit"],
        "gps_edit": packed["gps_edit"],
        "updated_at": now_iso(),
    }
    await db.review_choices.update_one({"_id": cid}, {"$set": doc}, upsert=True)
    return public_choices(doc)


async def save_choice(db, kind: str, entity_id: str, target: str, action: str,
                      *, url: str | None = None, port_id: str | None = None,
                      site_id: str | None = None, field: str | None = None,
                      lat: float | None = None, lon: float | None = None) -> dict:
    if kind not in CHOICE_KINDS:
        raise ValueError("choice kind must be eez|project|marina|capitainerie|amp")
    if target not in TARGETS:
        raise ValueError("target invalid")
    if action not in ACTIONS:
        raise ValueError("action must be keep|drop|clear")
    eid = _sid(entity_id)
    if not eid:
        raise ValueError("id required")
    current = await get_choices(db, kind, eid)
    if target == "td":
        href = (url or "").strip()
        if not href:
            raise ValueError("url required")
        if action == "clear":
            current["td"].pop(href, None)
        else:
            current["td"][href] = action
    elif target == "port":
        pid = _sid(port_id)
        if not pid:
            raise ValueError("port_id required")
        if action == "clear":
            current["ports"].pop(pid, None)
        else:
            current["ports"][pid] = action
    elif target == "bu":
        pid = _sid(port_id)
        href = (url or "").strip()
        if not pid or not href:
            raise ValueError("port_id and url required")
        bucket = current["bu"].setdefault(pid, {})
        if action == "clear":
            bucket.pop(href, None)
            if not bucket:
                current["bu"].pop(pid, None)
        else:
            bucket[href] = action
    elif target == "url":
        href = (url or "").strip()
        if not href:
            raise ValueError("url required")
        if action == "clear":
            current["urls"].pop(href, None)
        else:
            current["urls"][href] = action
    elif target == "site":
        sid = _sid(site_id)
        if not sid:
            raise ValueError("site_id required")
        if action == "clear":
            current["sites"].pop(sid, None)
        else:
            current["sites"][sid] = action
    elif target == "field":
        fid = (field or "").strip()
        if not fid:
            raise ValueError("field required")
        if action == "clear":
            current["fields"].pop(fid, None)
        else:
            current["fields"][fid] = action
    elif target == "visit":
        href = (url or "").strip()
        if not href:
            raise ValueError("url required")
        if action == "keep":
            current["visit"] = {href: "keep"}
            current["no_visit"] = False
        elif action == "clear":
            current["visit"].pop(href, None)
        else:
            current["visit"][href] = action
    elif target == "no_visit":
        current["no_visit"] = action == "keep"
        if current["no_visit"]:
            current["visit"] = {
                u: "drop" for u, a in (current.get("visit") or {}).items()
                if a == "keep"
            }
    elif target == "gps_edit":
        sid = _sid(site_id or "main")
        if action == "clear":
            current["gps_edit"].pop(sid, None)
        else:
            if lat is None or lon is None:
                raise ValueError("lat and lon required")
            current["gps_edit"][sid] = {"lat": float(lat), "lon": float(lon)}
    elif target in SCALAR_TARGETS:
        if action == "clear":
            current[target] = None if target != "no_visit" else False
        else:
            current[target] = action
    public = await save_choices_doc(db, kind, eid, current)
    return {
        "kind": kind,
        "id": eid,
        "choices": public,
        "wrote_poe_ports": False,
        "wrote_projects": False,
        "wrote_marinas": False,
        "wrote_capitaineries": False,
        "wrote_amp_sites": False,
    }


async def ensure_choice_indexes(db) -> None:
    try:
        await db.review_choices.create_index("kind")
        await db.review_choices.create_index("entity_id")
    except Exception:
        pass
