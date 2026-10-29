"""Apply the manual review of the 33 name_only in poe_seed_ports.

Never touch poe_ports. Do not run seeds/build.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.dedup import normalize_name
from app.services.poe_pipeline import now_iso
from app.services.poe_seeds import _observation, verdict_for_seed

DEFAULT_REVIEW_FILE = (
    Path(__file__).resolve().parents[2] / "docs" / "data"
    / "poe-name-only-33-verifications.json"
)
# repo layout: backend/app/services → parents[2] is backend, docs is at repo root
DEFAULT_REVIEW_FILE = Path(__file__).resolve().parents[3] / "docs" / "data" / "poe-name-only-33-verifications.json"


def load_review(path: Path | None = None) -> dict:
    return json.loads((path or DEFAULT_REVIEW_FILE).read_text(encoding="utf-8"))


def _find(coll, key: str) -> dict | None:
    if not key:
        return None
    return coll.find_one({"$or": [{"_id": key}, {"dedup_key": key}]})


def _listing_obs(name: str) -> dict:
    return _observation("listing", {
        "name": name, "listing_role": "poe",
        "extraction_engine": "listing",
    })


def _refresh_verdict(coll, key: str) -> None:
    hit = _find(coll, key)
    if not hit:
        return
    tmp = dict(hit)
    tmp["has_coords"] = bool(
        tmp.get("has_coords") or (
            tmp.get("lat") is not None and tmp.get("lon") is not None))
    coll.update_one({"_id": hit["_id"]}, {"$set": {
        "has_coords": tmp["has_coords"],
        "verify_verdict": verdict_for_seed(tmp),
    }})


def _attach_listing(coll, target_key: str, listing_name: str) -> bool:
    hit = _find(coll, target_key)
    if not hit:
        return False
    sources = list(hit.get("seed_sources") or [])
    if "listing" not in sources:
        sources.append("listing")
    obs = list(hit.get("observations") or [])
    key = ("listing", listing_name, None)
    seen = {(o.get("origin"), o.get("name"), o.get("osm_id")) for o in obs}
    if key not in seen:
        obs.append(_listing_obs(listing_name))
    role = hit.get("listing_role") or "poe"
    if role != "poe":
        role = "poe"
    upd = {
        "seed_sources": sources,
        "observations": obs[:24],
        "listing_role": role,
        "listing_name": hit.get("listing_name") or listing_name,
    }
    tmp = {**hit, **upd}
    tmp["has_coords"] = bool(
        tmp.get("has_coords") or (
            tmp.get("lat") is not None and tmp.get("lon") is not None))
    upd["verify_verdict"] = verdict_for_seed(tmp)
    coll.update_one({"_id": hit["_id"]}, {"$set": upd})
    return True


def _new_split_seed(parent: dict, name: str, lat: float, lon: float,
                    reviewed_at: str) -> dict:
    mid = parent.get("mrgid")
    key = f"{mid}:{normalize_name(name)}"
    seed = {
        "_id": key,
        "dedup_key": key,
        "name": name,
        "mrgid": mid,
        "zone_name": parent.get("zone_name"),
        "country_iso2": parent.get("country_iso2"),
        "lat": lat,
        "lon": lon,
        "has_coords": True,
        "validated": False,
        "seed_sources": ["listing"],
        "listing_role": "poe",
        "listing_name": name,
        "osm_confidence": None,
        "osm_tags": [],
        "osm_id": None,
        "osm_ids": [],
        "osm_customs": False,
        "osm_border": False,
        "osm_port_of_entry": None,
        "osm_kinds": [],
        "source_urls": [],
        "observations": [_listing_obs(name)],
        "extraction_engine": "listing",
        "geocode_source": "manual_review",
        "review_action": "nouveau_point",
        "review_from_split": parent.get("dedup_key") or parent.get("_id"),
        "reviewed_at": reviewed_at,
        "built_at": parent.get("built_at"),
    }
    seed["verify_verdict"] = verdict_for_seed(seed)
    return seed


def apply_one(coll, item: dict, reviewed_at: str) -> dict:
    """Apply one review card. Return {n, action, ok, detail}."""
    action = item.get("action")
    key = item.get("dedup_key")
    name = item.get("name") or ""
    doc = _find(coll, key)
    out = {"n": item.get("n"), "action": action, "dedup_key": key, "ok": False}

    if action != "corriger_zee" and doc is None:
        out["detail"] = "document introuvable"
        return out

    common = {
        "review_action": action,
        "review_confidence": item.get("confidence"),
        "review_note": (item.get("notes") or "")[:400],
        "reviewed_at": reviewed_at,
    }
    if item.get("label_point"):
        common["review_label"] = item["label_point"]

    if action == "nouveau_point":
        coll.update_one({"_id": doc["_id"]}, {"$set": {
            **common,
            "lat": item["lat"],
            "lon": item["lon"],
            "has_coords": True,
            "geocode_source": "manual_review",
        }})
        _refresh_verdict(coll, doc["_id"])
        out["ok"] = True
        out["detail"] = "gps posé"
        return out

    if action == "fusion":
        target = item.get("fusion_target")
        coll.update_one({"_id": doc["_id"]}, {"$set": {
            **common,
            "review_target_dedup_key": target,
        }})
        attached = _attach_listing(coll, target, name) if target else False
        tgt = _find(coll, target) if target else None
        if tgt is not None and tgt.get("lat") is None and item.get("lat") is not None:
            coll.update_one({"_id": tgt["_id"]}, {"$set": {
                "lat": item["lat"],
                "lon": item["lon"],
                "has_coords": True,
                "geocode_source": "manual_review",
            }})
            attached = True
        if target:
            _refresh_verdict(coll, target)
        out["ok"] = True
        out["detail"] = f"cible {target} attach={attached}"
        return out

    if action == "abandonner":
        coll.update_one({"_id": doc["_id"]}, {"$set": common})
        out["ok"] = True
        out["detail"] = "écarté"
        return out

    if action == "corriger_zee":
        target = item.get("fusion_target_optional") or item.get("new_dedup_key")
        tgt = _find(coll, target) if target else None
        if doc is None:
            out["detail"] = "source introuvable"
            return out
        coll.update_one({"_id": doc["_id"]}, {"$set": {
            **common,
            "review_target_dedup_key": target,
            "review_mrgid_correct": item.get("mrgid_correct"),
        }})
        if tgt is not None:
            _attach_listing(coll, tgt["_id"], name)
            if tgt.get("lat") is None and item.get("lat") is not None:
                coll.update_one({"_id": tgt["_id"]}, {"$set": {
                    "lat": item["lat"],
                    "lon": item["lon"],
                    "has_coords": True,
                    "geocode_source": "manual_review",
                    "mrgid": item.get("mrgid_correct") or tgt.get("mrgid"),
                    "zone_name": item.get("zone_correcte") or tgt.get("zone_name"),
                }})
            _refresh_verdict(coll, tgt["_id"])
            out["ok"] = True
            out["detail"] = f"fusion ZEE → {target}"
            return out
        if target:
            fresh = dict(doc)
            fresh["_id"] = target
            fresh["dedup_key"] = target
            fresh["mrgid"] = item.get("mrgid_correct")
            fresh["zone_name"] = item.get("zone_correcte") or fresh.get("zone_name")
            fresh["lat"] = item.get("lat")
            fresh["lon"] = item.get("lon")
            fresh["has_coords"] = item.get("lat") is not None
            fresh["geocode_source"] = "manual_review"
            fresh.update(common)
            coll.insert_one(fresh)
            out["ok"] = True
            out["detail"] = f"inséré {target}"
            return out
        out["detail"] = "pas de cible ZEE"
        return out

    if action == "scinder":
        targets = list(item.get("fusion_targets") or [])
        created = []
        for pt in item.get("points") or []:
            tkey = pt.get("dedup_key")
            if tkey and _find(coll, tkey):
                _attach_listing(coll, tkey, pt.get("name") or name)
                targets.append(tkey)
                continue
            if pt.get("lat") is None:
                continue
            seed = _new_split_seed(
                doc, pt["name"], float(pt["lat"]), float(pt["lon"]), reviewed_at)
            if _find(coll, seed["_id"]) is None:
                coll.insert_one(seed)
                created.append(seed["_id"])
            else:
                _attach_listing(coll, seed["_id"], pt["name"])
            targets.append(seed["_id"])
        # unique preserve order
        seen, uniq = set(), []
        for t in targets:
            if t and t not in seen:
                seen.add(t)
                uniq.append(t)
        coll.update_one({"_id": doc["_id"]}, {"$set": {
            **common,
            "review_target_dedup_keys": uniq,
        }})
        out["ok"] = True
        out["detail"] = f"cibles {uniq} created={created}"
        return out

    out["detail"] = f"action inconnue {action}"
    return out


def apply_review(coll, poe_ports, review: dict | None = None) -> dict:
    """Apply the 33. `poe_ports` is read to verify we do not write."""
    review = review or load_review()
    before = poe_ports.count_documents({})
    reviewed_at = now_iso()
    results = [apply_one(coll, item, reviewed_at) for item in review["verifications"]]
    after = poe_ports.count_documents({})
    if after != before:
        raise RuntimeError(f"poe_ports a changé {before} → {after}")
    ok = sum(1 for r in results if r["ok"])
    return {
        "reviewed_at": reviewed_at,
        "applied": ok,
        "total": len(results),
        "poe_ports": after,
        "wrote_poe_ports": False,
        "results": results,
    }
