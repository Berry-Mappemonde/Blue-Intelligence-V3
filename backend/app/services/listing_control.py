"""listing_control — Compare a run (or v1) to listing_ref, without annotating ports.

The listing is the "as-if" Gold diff reference: it is not written
into poe_ports / poe_run_ports. Gaps go into a review queue
(poe_listing_review).
"""
from __future__ import annotations

import re
import uuid

from app.core.dedup import find_duplicate_in_list, normalize_name, text_similarity
from app.services.listing_ref import project_listing
from app.services.poe_pipeline import now_iso

SIM_HIGH = 0.90
SIM_LOW = 0.60
_PREFIX = (
    "porto de ", "port of ", "port de ", "pkk porti i ", "puerto de ", "haven ",
)
_PAREN = re.compile(r"\s*\([^)]*\)\s*")

REVIEW_REASONS = ("contradiction", "run_only", "listing_only", "ambiguous")


def _alias_name(name: str) -> str:
    n = (name or "").strip()
    n = _PAREN.sub(" ", n).strip()
    low = n.lower()
    for p in _PREFIX:
        if low.startswith(p):
            return n[len(p):].strip() or n
    return n


def _score(a: str, b: str) -> float:
    return max(
        text_similarity(a or "", b or ""),
        text_similarity(_alias_name(a or ""), _alias_name(b or "")),
    )


def _best_listing_match(run_port: dict, listing_pool: list[dict]) -> tuple[dict | None, float]:
    if not listing_pool:
        return None, 0.0
    keyed = {p.get("dedup_key"): p for p in listing_pool if p.get("dedup_key")}
    dk = run_port.get("dedup_key")
    if dk and dk in keyed:
        return keyed[dk], 1.0
    hit = find_duplicate_in_list(run_port, listing_pool, title_key="name")
    if hit:
        return hit, _score(run_port.get("name") or "", hit.get("name") or "")
    best, best_s = None, 0.0
    for lp in listing_pool:
        s = _score(run_port.get("name") or "", lp.get("name") or "")
        if s > best_s:
            best, best_s = lp, s
    from app.core.run_rules import get_rule
    sim_low = float(get_rule("formalities.listing_sim_low", SIM_LOW))
    if best_s >= sim_low:
        return best, best_s
    return None, best_s


def _slim_run(p: dict) -> dict:
    return {
        "name": p.get("name"),
        "mrgid": p.get("mrgid"),
        "zone_name": p.get("zone_name"),
        "country_iso2": p.get("country_iso2"),
        "extraction_engine": p.get("extraction_engine"),
        "extraction_agreement": p.get("extraction_agreement"),
        "geocode_source": p.get("geocode_source"),
    }


def _slim_listing(p: dict) -> dict:
    return {
        "name": p.get("name"),
        "role": p.get("role"),
        "slug": p.get("slug"),
        "country": p.get("country"),
        "group": p.get("group"),
        "mrgid": p.get("mrgid"),
        "mrgids": p.get("mrgids") or [],
    }


def compare_to_listing(run_ports: list[dict], listing_ports: list[dict],
                       run_mrgids: set[int] | None = None,
                       restrict_to_run_zones: bool = True) -> dict:
    """Classify each port of the run / listing. No mutation of input dicts."""
    if restrict_to_run_zones:
        if run_mrgids is None:
            run_mrgids = {int(p["mrgid"]) for p in run_ports if p.get("mrgid") is not None}
        scope = {int(m) for m in run_mrgids}
    else:
        scope = None

    by_zone_listing: dict[int, list[dict]] = {}
    listing_poe_keys: set[tuple] = set()
    for lp in listing_ports:
        ids = [int(x) for x in (lp.get("mrgids") or ([lp["mrgid"]] if lp.get("mrgid") else []))]
        if scope is not None and not (set(ids) & scope):
            continue
        if lp.get("role") == "poe":
            listing_poe_keys.add((lp.get("slug"), normalize_name(lp.get("name"))))
        for mid in ids:
            if scope is not None and mid not in scope:
                continue
            by_zone_listing.setdefault(mid, []).append(
                {**lp, "mrgid": mid,
                 "dedup_key": f"{mid}:{normalize_name(lp.get('name'))}"})

    confident, contradiction, run_only, ambiguous = [], [], [], []
    matched_listing: set[tuple] = set()

    for rp in run_ports:
        mid = int(rp.get("mrgid") or 0)
        pool = by_zone_listing.get(mid, [])
        hit, score = _best_listing_match(rp, pool)
        row = {
            "mrgid": mid,
            "zone_name": rp.get("zone_name"),
            "score": round(score, 3),
            "run": _slim_run(rp),
        }
        if hit is None:
            run_only.append({**row, "reason": "absent"})
            continue
        key = (hit.get("slug"), normalize_name(hit.get("name")))
        matched_listing.add(key)
        row["listing"] = _slim_listing(hit)
        from app.core.run_rules import get_rule
        if score < float(get_rule("formalities.listing_sim_high", SIM_HIGH)):
            ambiguous.append({**row, "reason": "ambiguous"})
            continue
        if hit.get("role") == "other":
            contradiction.append({**row, "reason": "other"})
        else:
            confident.append(row)

    listing_only = []
    seen_lo: set[tuple] = set()
    uk_app_entry_not_a_gap = 0
    from app.services.poe_rules import is_uk_app_entry
    for lp in listing_ports:
        if lp.get("role") != "poe":
            continue
        ids = [int(x) for x in (lp.get("mrgids") or ([lp["mrgid"]] if lp.get("mrgid") else []))]
        if scope is not None and not (set(ids) & scope):
            continue
        key = (lp.get("slug"), normalize_name(lp.get("name")))
        if key in matched_listing or key in seen_lo:
            continue
        seen_lo.add(key)
        # UK: no designated list. A listing name the run did not repeat is not a hole.
        if is_uk_app_entry(lp):
            uk_app_entry_not_a_gap += 1
            continue
        listing_only.append({
            "mrgid": ids[0] if ids else None,
            "zone_name": lp.get("zone_name"),
            "reason": "missed_by_run",
            "listing": _slim_listing({**lp, "mrgid": ids[0] if ids else lp.get("mrgid")}),
        })

    listing_poe_n = len(listing_poe_keys) if scope is not None else sum(
        1 for p in listing_ports if p.get("role") == "poe")
    run_n = len(run_ports)
    # UK misses are not holes: drop them from the coverage denominator too.
    coverage_denom = listing_poe_n - uk_app_entry_not_a_gap
    if coverage_denom < 0:
        coverage_denom = 0
    summary = {
        "run_ports": run_n,
        "listing_poe": listing_poe_n,
        "confident": len(confident),
        "contradiction": len(contradiction),
        "run_only": len(run_only),
        "listing_only": len(listing_only),
        "ambiguous": len(ambiguous),
        "uk_app_entry_not_a_gap": uk_app_entry_not_a_gap,
        "coverage": round(len(confident) / coverage_denom, 4) if coverage_denom else None,
        "noise": round((len(contradiction) + len(run_only)) / run_n, 4) if run_n else None,
        "restricted_to_run_zones": restrict_to_run_zones,
    }
    return {
        "summary": summary,
        "confident": confident,
        "review": {
            "contradiction": contradiction,
            "run_only": run_only,
            "listing_only": listing_only,
            "ambiguous": ambiguous,
        },
    }


def review_items(report: dict, run_id: str, variant: str | None = None) -> list[dict]:
    """Flatten the review queue (not confident matches)."""
    now = now_iso()
    items = []
    for reason in REVIEW_REASONS:
        for row in (report.get("review") or {}).get(reason) or []:
            items.append({
                "_id": str(uuid.uuid4()),
                "run_id": run_id,
                "variant": variant,
                "reason": reason,
                "mrgid": row.get("mrgid"),
                "zone_name": row.get("zone_name"),
                "name_run": (row.get("run") or {}).get("name"),
                "name_listing": (row.get("listing") or {}).get("name"),
                "listing_role": (row.get("listing") or {}).get("role"),
                "score": row.get("score"),
                "detail": row.get("reason"),
                "created_at": now,
            })
    return items


async def load_run_ports(db, run_id: str) -> tuple[list[dict], dict]:
    if run_id == "v1":
        ports = await db.poe_ports.find({}).to_list(20000)
        meta = {"run_id": "v1", "label": "v1", "variant": "v1",
                "state": "baseline", "ports": len(ports)}
        return ports, meta
    doc = await db.poe_runs.find_one({"_id": run_id})
    if not doc:
        raise KeyError(run_id)
    ports = await db.poe_run_ports.find({"run_id": run_id}).to_list(20000)
    variant = (doc.get("params") or {}).get("variant") or doc.get("label")
    meta = {
        "run_id": run_id,
        "label": doc.get("label"),
        "variant": variant,
        "state": doc.get("state"),
        "ports": len(ports),
        "zones_done": doc.get("zones_done"),
    }
    return ports, meta


async def build_listing_control_report(db, run_id: str,
                                       listing_proj: dict | None = None,
                                       restrict_to_run_zones: bool = True) -> dict:
    ports, meta = await load_run_ports(db, run_id)
    proj = listing_proj or project_listing()
    run_mrgids = {int(p["mrgid"]) for p in ports if p.get("mrgid") is not None}
    if run_id != "v1":
        extra = {int(z["mrgid"]) async for z in db.poe_run_zones.find(
            {"run_id": run_id}, {"mrgid": 1}) if z.get("mrgid") is not None}
        run_mrgids |= extra
    cmp = compare_to_listing(ports, proj["ports"], run_mrgids=run_mrgids,
                             restrict_to_run_zones=restrict_to_run_zones)
    return {
        "run": meta,
        "listing_ref_id": proj["listing_ref_id"],
        "listing_stats": proj["stats"],
        "unresolved_slugs": proj.get("unresolved") or [],
        **cmp,
    }


async def persist_review(db, report: dict) -> dict:
    """Replace this run's review queue. Do not touch poe_ports / poe_run_ports."""
    run_id = (report.get("run") or {}).get("run_id")
    if not run_id:
        raise ValueError("rapport sans run_id")
    items = review_items(report, run_id, variant=(report.get("run") or {}).get("variant"))
    await db.poe_listing_review.delete_many({"run_id": run_id})
    if items:
        await db.poe_listing_review.insert_many(items)
    await db.poe_listing_reports.update_one(
        {"_id": f"{run_id}:{report.get('listing_ref_id')}"},
        {"$set": {
            "run_id": run_id,
            "variant": (report.get("run") or {}).get("variant"),
            "listing_ref_id": report.get("listing_ref_id"),
            "summary": report.get("summary"),
            "updated_at": now_iso(),
        }},
        upsert=True,
    )
    return {"run_id": run_id, "review_count": len(items),
            "by_reason": {r: len((report.get("review") or {}).get(r) or [])
                          for r in REVIEW_REASONS}}


async def compare_runs_to_listing(db, run_ids: list[str], include_v1: bool = True,
                                  persist: bool = False) -> dict:
    proj = project_listing()
    labels = (["v1"] if include_v1 else []) + list(run_ids)
    reports = []
    for rid in labels:
        rep = await build_listing_control_report(db, rid, listing_proj=proj)
        if persist:
            await persist_review(db, rep)
        reports.append({
            "run": rep["run"],
            "summary": rep["summary"],
            "review": {k: len(v) for k, v in (rep.get("review") or {}).items()},
        })
    return {
        "listing_ref_id": proj["listing_ref_id"],
        "listing_stats": proj["stats"],
        "unresolved_slugs": proj.get("unresolved") or [],
        "reports": reports,
    }


def _add_reason(bucket: dict[int, list[str]], mrgid, reason: str) -> None:
    if mrgid is None:
        return
    mid = int(mrgid)
    bucket.setdefault(mid, [])
    if reason not in bucket[mid]:
        bucket[mid].append(reason)


_SKIP_UNCLOS = {"uninhabited", "antarctic"}


def _unclos_skip(name: str | None) -> str | None:
    from app.services.poe_pipeline import qualify_unclos
    q = qualify_unclos({"name": name or "", "geoname": "", "poe_count": 0})
    code = (q or {}).get("code")
    return code if code in _SKIP_UNCLOS else None


async def suggest_canary_zones(db, run_ids: list[str] | None = None,
                               include_v1: bool = True, limit: int = 25) -> dict:
    """Canary EEZ: listing holes (v1) ∪ error zones of the given runs.

    Launch no crawl. Without poe_ports / runs, the list is empty.
    Uninhabited / antarctic EEZs (UNCLOS) are dropped: Claude is useless there.
    """
    reasons: dict[int, list[str]] = {}
    names: dict[int, str] = {}
    skipped_unclos: list[dict] = []
    v1_ports = await db.poe_ports.count_documents({})
    stored_runs = await db.poe_runs.count_documents({})

    if include_v1 and v1_ports:
        report = await build_listing_control_report(db, "v1", restrict_to_run_zones=False)
        for row in (report.get("review") or {}).get("listing_only") or []:
            mid = row.get("mrgid")
            _add_reason(reasons, mid, "listing_only")
            if mid is not None:
                names[int(mid)] = row.get("zone_name") or names.get(int(mid))

    for rid in run_ids or []:
        async for z in db.poe_run_zones.find(
                {"run_id": rid, "status": "erreur"}, {"mrgid": 1, "name": 1}):
            mid = z.get("mrgid")
            _add_reason(reasons, mid, f"error:{rid}")
            if mid is not None:
                names[int(mid)] = z.get("name") or names.get(int(mid))

    ranked = sorted(reasons.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    zones = []
    for mid, why in ranked:
        code = _unclos_skip(names.get(mid))
        if code:
            skipped_unclos.append({"mrgid": mid, "name": names.get(mid),
                                   "unclos": code})
            continue
        zones.append({"mrgid": mid, "name": names.get(mid), "reasons": why})
        if len(zones) >= max(1, min(limit, 80)):
            break
    return {
        "v1_ports": v1_ports,
        "stored_runs": stored_runs,
        "source_run_ids": list(run_ids or []),
        "mrgids": [z["mrgid"] for z in zones],
        "zones": zones,
        "skipped_unclos": skipped_unclos[:20],
        "atlas_empty": v1_ports == 0 and stored_runs == 0,
    }
