"""Whitelist coverage of the EEZs the Berry-Mappemonde route crosses.

Route (backend/data/route.geojson) x EEZ polygons (backend/data/eez_world_map.geojson),
then for each crossed EEZ the whitelist the pipeline builds (poe_pipeline.build_whitelist
with poe_exceptions.json) and, where the zone has been audited
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
                           exceptions if exceptions is not None else load_exceptions())


def without_added(exceptions: dict, audit: dict) -> dict:
    """The exceptions as they were before this audit added its domains."""
    key = audit["whitelist_key"]
    added = {a["domain"] for a in audit["authorities"] if a.get("added")}
    manual = {k: list(v) for k, v in (exceptions.get("manual") or {}).items()}
    manual[key] = [d for d in manual.get(key, []) if d not in added]
    return {**exceptions, "manual": manual}


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
        row = {**z, "key": z["iso2"] or z["sov_iso2"], "domains": wl}
        if audit:
            before = missing_roles(audit, whitelist_for(z, without_added(exceptions, audit)))
            now = missing_roles(audit, wl)
            row["gap"] = "yes" if now else "no"
            row["note"] = ("audited; was missing " + ", ".join(ROLE_LABELS[r] for r in before)
                           if before else "audited")
            if now:
                row["note"] += "; still missing " + ", ".join(ROLE_LABELS[r] for r in now)
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
        "accepts for each (`poe_pipeline.build_whitelist`: government labels of the zone's",
        "ISO2 from the Public Suffix List, plus `poe_exceptions.json` manual / auto entries;",
        "matched by host with `url_allowed`). A zone uses its own ISO2 when it has one, else",
        "the sovereign's (`iso2 or sov_iso2`): Martinique (MQ) does not inherit France's list.",
        "",
        "**Gap**: `yes` when an audited authority (customs, immigration, maritime authority,",
        "port authority) has no matching domain, or the whitelist is empty; `no` when all",
        "audited authorities match; `not checked` when the zone has not been audited yet.",
        f"Audited on {sources.get('checked')}: the pilot zones. Sources per domain are below",
        "and in `backend/data/whitelist_sources.json`.",
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
    lines += [
        "## Next",
        "",
        "- Audit the `not checked` zones the same way (one authority per role, official",
        "  source each). Zones with an empty whitelist come first: Collectivity of Saint",
        "  Martin, Saint-Barthélemy, Bonaire, Saba, Sint-Eustatius, Western Sahara.",
        "- Other French overseas zones (Guadeloupe, French Guiana, Réunion, Mayotte,",
        "  Saint-Martin, Saint-Barthélemy) have the same pattern as Martinique: their own",
        "  ISO2 means `douane.gouv.fr` is not in their whitelist.",
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
