"""Marina / harbormaster Review field judge.

One card at a time — no world batch on the OSM dump.
Identity + GPS if the point exists. Fields: keep the OSM tag / official
page, drop OTAs and unsourced values. Does not write live or Gold.
"""
from __future__ import annotations

from urllib.parse import urlparse

from app.services.poe_zone_fiche import PUBLISHED_RUN
from app.services.review_choices import (
    FIELD_KEEP,
    empty_choices,
    gold_ready,
    save_choices_doc,
)
from app.services.review_lessons import save_proposal
from app.services.review_queue import get_fiche, save_comment

_OTA_HOSTS = (
    "tripadvisor.", "booking.com", "expedia.", "hotels.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "tripadvisor.co",
)

_TAG_FOR_FIELD = {
    "canal_vhf": ("channel", "vhf", "seamark:radio_station:channel",
                  "seamark:harbour:vhf"),
    "telephone": ("phone", "contact:phone", "telephone"),
    "telephone_capitainerie": ("phone", "contact:phone", "telephone"),
    "places_visiteurs": ("berths", "capacity:berths", "capacity"),
    "tirant_eau_max_metres": ("draft", "maxdraft", "seamark:harbour:maxdraft"),
    "website": ("website", "contact:website"),
    "services_disponibles": ("leisure", "seamark:harbour:category"),
}


def _sid(value) -> str:
    return "" if value is None else str(value)


def _has_coords(fiche: dict) -> bool:
    try:
        lat, lon = float(fiche["lat"]), float(fiche["lon"])
    except (KeyError, TypeError, ValueError):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def _ota_url(url: str | None) -> bool:
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    return any(tok in host for tok in _OTA_HOSTS)


def _tags(fiche: dict) -> dict:
    raw = fiche.get("tags") or {}
    return raw if isinstance(raw, dict) else {}


def _tag_has(fiche: dict, field: str) -> bool:
    tags = _tags(fiche)
    if not tags:
        return False
    keys = _TAG_FOR_FIELD.get(field) or (field,)
    low = {str(k).lower(): v for k, v in tags.items()}
    for key in keys:
        val = low.get(key.lower())
        if val not in (None, "", []):
            return True
    return False


def _field_source_ok(fiche: dict, field: str) -> bool:
    src = str((fiche.get("field_sources") or {}).get(field) or "").lower()
    if src in ("osm", "tag", "official", "shom", "noaa"):
        return True
    if src in ("llm", "agent", "inferred", "hallucinated"):
        return False
    return _tag_has(fiche, field)


def marina_urls(fiche: dict) -> list[str]:
    out = []
    for key in ("website", "maps_place_url", "maps_url"):
        href = (fiche.get(key) or "").strip()
        if href and href not in out:
            out.append(href)
    return out


def local_pick_fields(fiche: dict | None, kind: str = "marina") -> dict:
    fiche = fiche or {}
    keep, drop = [], []
    identity = "keep" if (fiche.get("name") or fiche.get("osm_id")) else "drop"
    gps = "keep" if _has_coords(fiche) else "drop"
    keep += [t for t, v in (("identity", identity), ("gps", gps)) if v == "keep"]
    drop += [t for t, v in (("identity", identity), ("gps", gps)) if v == "drop"]

    overlay = None
    if kind == "capitainerie":
        has_overlay = bool(
            fiche.get("shom_id") or fiche.get("noaa_id")
            or (fiche.get("control_ref") or {}).get("status") == "published_ref"
        )
        overlay = "keep" if has_overlay else "drop"
        (keep if overlay == "keep" else drop).append("overlay")

    url_keep, url_drop = [], []
    for url in marina_urls(fiche):
        if _ota_url(url):
            url_drop.append(url)
            drop.append(url)
        else:
            url_keep.append(url)
            keep.append(url)

    fields: dict[str, str] = {}
    wanted = (
        ("canal_vhf", "places_visiteurs", "tirant_eau_max_metres",
         "telephone_capitainerie", "services_disponibles")
        if kind == "marina"
        else ("telephone", "canal_vhf")
    )
    for field in wanted:
        if field not in FIELD_KEEP and field != "telephone":
            continue
        val = fiche.get(field)
        if val in (None, "", []):
            continue
        if _field_source_ok(fiche, field):
            fields[field] = "keep"
            keep.append(f"field:{field}")
        else:
            fields[field] = "drop"
            drop.append(f"field:{field}")

    comment = (
        "Proposition locale champs. "
        f"identité={identity} GPS={gps}. "
        f"{sum(1 for v in fields.values() if v == 'keep')} champ(s) sourcé(s), "
        f"{sum(1 for v in fields.values() if v == 'drop')} écarté(s)."
    )
    return {
        "keep": keep,
        "drop": drop,
        "identity": identity,
        "gps": gps,
        "overlay": overlay,
        "url_keep": url_keep,
        "url_drop": url_drop,
        "fields": fields,
        "comment": comment,
        "engine": "local",
        "local_keep": list(keep),
        "list_kind": "fields",
        "no_visit": False,
    }


async def suggest_field_documents(db, kind: str, entity_id: str, *,
                                  log=None) -> dict:
    if kind not in ("marina", "capitainerie"):
        raise ValueError("field picker is marina|capitainerie")
    log = log or (lambda m: None)
    eid = _sid(entity_id)
    packed = await get_fiche(db, kind, PUBLISHED_RUN, eid)
    if not packed or not packed.get("fiche"):
        raise ValueError("fiche not found")
    fiche = packed["fiche"]
    picked = local_pick_fields(fiche, kind)
    public = empty_choices()
    public["identity"] = picked["identity"]
    public["gps"] = picked["gps"]
    if picked.get("overlay"):
        public["overlay"] = picked["overlay"]
    for url in picked.get("url_keep") or []:
        public["urls"][url] = "keep"
    for url in picked.get("url_drop") or []:
        public["urls"][url] = "drop"
    public["fields"] = dict(picked.get("fields") or {})
    choices = await save_choices_doc(db, kind, eid, public)
    saved = await save_comment(db, kind, PUBLISHED_RUN, eid, picked["comment"])
    await save_proposal(db, eid, picked, fiche, kind=kind)
    ready = gold_ready(fiche, choices, kind=kind, comment=picked["comment"])
    log(f"{kind} field picker {eid}: identity={picked['identity']} gps={picked['gps']}")
    return {
        "kind": kind,
        "id": eid,
        "choices": choices,
        "comment": picked["comment"],
        "comment_updated_at": saved.get("updated_at"),
        "engine": "local",
        "gold_ready": ready,
        "gold_on": bool(packed.get("gold_on")),
        "wrote_marinas": False,
        "wrote_capitaineries": False,
        "wrote_projects": False,
        "wrote_poe_ports": False,
    }
