"""Whitelist coverage of the EEZs the Berry-Mappemonde route crosses.

Route (backend/data/route.geojson) x EEZ polygons (backend/data/eez_world_map.geojson),
then for each crossed EEZ the whitelist the pipeline builds (poe_pipeline.build_whitelist
with the zone's mrgid, territories.json and poe_exceptions.json) and, where the zone has been audited
(backend/data/whitelist_sources.json), whether every required authority's official URL
passes url_allowed.

Gap:
  yes          an audited authority's URL is not matched, or the whitelist is empty;
  no           every audited authority (customs, immigration, maritime / port authority)
               is matched;
  not checked  the whitelist is not empty but the zone has not been audited yet.

    python scripts/route_whitelist_report.py           # print the Markdown
    python scripts/route_whitelist_report.py --write   # write docs/ROUTE-WHITELIST.md
    python scripts/route_whitelist_report.py --check   # exit 1 if the doc is stale

Offline: no network, no Mongo.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

DATA = BACKEND / "data"
ROUTE = DATA / "route.geojson"
EEZ = DATA / "eez_world_map.geojson"
SOURCES = DATA / "whitelist_sources.json"
DOC = BACKEND.parent / "docs" / "ROUTE-WHITELIST.md"

# Sovereign names in eez_world_map.geojson that pycountry does not resolve as written.
SOVEREIGN_ISO2 = {
    "Cape Verde": "CV", "East Timor": "TL", "Republic of Mauritius": "MU",
    "Western Sahara": "EH", "Venezuela": "VE",
}
ROLE_LABELS = {"customs": "customs", "immigration": "immigration",
               "maritime_authority": "maritime authority", "port_authority": "port authority"}


def sovereign_iso2(name: str | None) -> str | None:
    if not name:
        return None
    if name in SOVEREIGN_ISO2:
        return SOVEREIGN_ISO2[name]
    import pycountry
    try:
        return pycountry.countries.lookup(name).alpha_2
    except LookupError:
        hits = pycountry.countries.search_fuzzy(name)
        return hits[0].alpha_2 if hits else None


def route_zones() -> list[dict]:
    """EEZ properties of every polygon a route segment intersects, sorted by sovereign, name."""
    from shapely.geometry import shape
    from shapely.strtree import STRtree

    route = json.loads(ROUTE.read_text(encoding="utf-8"))
    eez = json.loads(EEZ.read_text(encoding="utf-8"))["features"]
    geoms = [shape(f["geometry"]) for f in eez]
    tree = STRtree(geoms)
    hits: dict[int, dict] = {}
    for feat in route["features"]:
        line = shape(feat["geometry"])
        for i in tree.query(line):
            if geoms[i].intersects(line):
                props = eez[i]["properties"]
                hits[int(props["mrgid"])] = dict(props)
    zones = []
    for mrgid, p in hits.items():
        sov2 = sovereign_iso2(p.get("sovereign"))
        zones.append({"mrgid": mrgid, "zone": p.get("name"), "iso2": p.get("iso2"),
                      "sovereign": p.get("sovereign"), "sov_iso2": sov2})
    return sorted(zones, key=lambda z: (z["sovereign"] or "", z["zone"] or "", z["mrgid"]))


def load_sources() -> dict:
    return json.loads(SOURCES.read_text(encoding="utf-8"))


def whitelist_for(zone: dict, exceptions: dict | None = None) -> list[str]:
    from app.services.poe_pipeline import build_whitelist, load_exceptions
    return build_whitelist(zone["iso2"], zone["sov_iso2"],
                           exceptions if exceptions is not None else load_exceptions(),
                           mrgid=zone["mrgid"])


def whitelist_key(zone: dict) -> str | None:
    """Territory code for a polygon curated in territories.json, else the ISO2 used."""
    from app.services.territory_ref import territory_for_mrgid
    terr = territory_for_mrgid(zone["mrgid"])
    return terr.get("code") if terr else (zone["iso2"] or zone["sov_iso2"])


def listed_domains(zone: dict, exceptions: dict) -> set[str]:
    """Domains listed by hand for the zone (not PSL labels): the territory record's
    official_domains, whitelist_mrgid[mrgid] and, when the sovereign runs entry, the
    sovereign's manual list; for any other zone, manual[iso2 or sov_iso2]."""
    from app.services.territory_ref import sovereign_runs_entry, territory_for_mrgid
    terr = territory_for_mrgid(zone["mrgid"])
    if not terr:
        return set((exceptions.get("manual") or {}).get(zone["iso2"] or zone["sov_iso2"], []))
    out = set(terr.get("official_domains") or [])
    out |= set((exceptions.get("whitelist_mrgid") or {}).get(str(zone["mrgid"]), []))
    if sovereign_runs_entry(terr):
        out |= set((exceptions.get("manual") or {}).get(terr.get("sovereign_iso2"), []))
    return out


def missing_roles(audit: dict, whitelist: list[str]) -> list[str]:
    """Roles of which no audited authority URL passes url_allowed."""
    from app.services.poe_pipeline import url_allowed
    roles: dict[str, bool] = {}
    for a in audit["authorities"]:
        roles[a["role"]] = roles.get(a["role"], False) or url_allowed(a["url"], whitelist)
    return [r for r, ok in roles.items() if not ok]


def rows() -> list[dict]:
    from app.services.poe_pipeline import load_exceptions
    exceptions = load_exceptions()
    audits = load_sources()["zones"]
    out = []
    for z in route_zones():
        wl = whitelist_for(z, exceptions)
        audit = audits.get(str(z["mrgid"]))
        row = {**z, "key": whitelist_key(z), "domains": wl}
        if audit:
            before = audit.get("was_missing") or []
            now = missing_roles(audit, wl)
            row["gap"] = "yes" if now else "no"
            row["note"] = ("audited; was missing " + ", ".join(ROLE_LABELS[r] for r in before)
                           if before else "audited")
            if now:
                row["note"] += "; still missing " + ", ".join(ROLE_LABELS[r] for r in now)
            if audit.get("no_authority"):
                row["note"] += "; no " + ", ".join(ROLE_LABELS[r] for r in audit["no_authority"])
        elif not wl:
            row["gap"], row["note"] = "yes", "empty whitelist"
        else:
            row["gap"], row["note"] = "not checked", ""
        out.append(row)
    return out


def render(table: list[dict] | None = None) -> str:
    table = rows() if table is None else table
    sources = load_sources()
    n = {g: sum(1 for r in table if r["gap"] == g) for g in ("yes", "no", "not checked")}
    lines = [
        "# Route whitelist audit",
        "",
        "Generated by `backend/scripts/route_whitelist_report.py` (`--write`); do not edit by hand.",
        "",
        "EEZs crossed by the Berry-Mappemonde route (`backend/data/route.geojson` x",
        "`backend/data/eez_world_map.geojson`) and the official domains the PoE whitelist",
        "accepts for each (`poe_pipeline.build_whitelist`, matched by host with `url_allowed`;",
        "design in `docs/TERRITORY-WHITELIST.md`). A polygon curated in",
        "`backend/data/territories.json` (key = territory code) gets the record's",
        "`official_domains` plus `poe_exceptions.json` `whitelist_mrgid[mrgid]`, and the",
        "sovereign's national labels only when the record sets `sovereign_runs_entry`",
        "(Mayotte and Martinique yes, New Caledonia and the TAAF no). Any other zone",
        "(key = ISO2) gets the government labels of its own ISO2, else the sovereign's",
        "(`iso2 or sov_iso2`), from the Public Suffix List plus `poe_exceptions.json`",
        "manual / auto entries.",
        "",
        "**Gap**: `yes` when an audited authority (customs, immigration, maritime authority,",
        "port authority) has no matching domain, or the whitelist is empty; `no` when all",
        "audited authorities match; `not checked` when the zone has not been audited yet.",
        f"Audited on {sources.get('checked')}: the pilot zones and the French overseas",
        "zones on the route; \"was missing\" is frozen at audit time. Sources per domain are below and in",
        "`backend/data/whitelist_sources.json`. A role with no authority in a zone (no port,",
        "no customs post) is listed under \"no authority\" with the official text that says so.",
        "",
        f"{len(table)} zones: {n['yes']} gap, {n['no']} no gap, {n['not checked']} not checked.",
        "",
        "| mrgid | zone | sovereign | key | domains listed | gap | note |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in table:
        doms = ", ".join(f"`{d}`" for d in r["domains"]) or "(none)"
        lines.append(f"| {r['mrgid']} | {r['zone']} | {r['sovereign']} | {r['key'] or '-'} "
                     f"| {doms} | {r['gap']} | {r['note']} |")
    lines += ["", "## Domains added and their sources", ""]
    for mrgid, audit in sources["zones"].items():
        lines += [f"### {audit['zone']} ({mrgid}, whitelist key `{audit['whitelist_key']}`)", "",
                  "| role | authority | domain | added | official URL | source |",
                  "|---|---|---|---|---|---|"]
        for a in audit["authorities"]:
            lines.append(f"| {ROLE_LABELS[a['role']]} | {a['name']} | `{a['domain']}` "
                         f"| {'yes' if a.get('added') else 'already listed'} "
                         f"| {a['url']} | {a['source']} |")
        lines.append("")
        for role, reason in (audit.get("no_authority") or {}).items():
            lines.append(f"No {ROLE_LABELS[role]} authority: {reason}")
            lines.append("")
        if audit.get("notes"):
            lines += [f"Notes: {audit['notes']}", ""]
    lines += [
        "## Next",
        "",
        "- Audit the `not checked` zones the same way (one authority per role, official",
        "  source each). Zones with an empty whitelist come first: Bonaire, Saba,",
        "  Sint-Eustatius, Western Sahara.",
        "- Add Bonaire, Saba and Sint-Eustatius (Caribbean Netherlands, BQ) to",
        "  `territories.json` once the customs and immigration bodies there are sourced;",
        "  Sint Maarten, Aruba, Curaçao, Cook Islands, Niue, Greenland and the Faroes are",
        "  self-governing and are never attached to a sovereign (`never_attach_to_sovereign`).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true", help="write docs/ROUTE-WHITELIST.md")
    ap.add_argument("--check", action="store_true", help="exit 1 if the doc is out of date")
    args = ap.parse_args()
    text = render()
    if args.check:
        current = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
        if current != text:
            print("docs/ROUTE-WHITELIST.md is out of date: run with --write", file=sys.stderr)
            return 1
        return 0
    if args.write:
        DOC.write_text(text, encoding="utf-8")
        print(f"wrote {DOC}")
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
