"""Score a PoE run against a hand-checked gold file (backend/data/gold/*.json).

A gold file lists, per zone (mrgid), the PoEs (``poe``) and the rejected names
(``not_poe``) a reviewer checked against State sources, each with a note. This module
reads it, never writes it.

Per zone and overall:
  - C2 precision: run ports at C2 or above that match a gold PoE / C2+ ports that
    match any gold entry (PoE or rejected). A C2+ port that matches nothing is listed
    in ``unmatched`` and left out of the ratio, not counted as right or wrong;
  - C2 recall: gold PoEs matched by a C2+ run port / gold PoEs;
  - any-level recall: gold PoEs matched by a run port at any level / gold PoEs;
  - wrong C2s: C2+ run ports that match a rejected name, with their quote;
  - missed: gold PoEs no run port matches;
  - C1 noise: run ports below C2 that match a rejected name;
  - unmatched: run ports that match no gold entry, or more than one (never guessed).

Names: every name a run port carries (its name, ``merged_names`` from #47, the names of
its ``clearance_points`` from #50) against every form of a gold name (the whole name,
the part before a parenthesis, each side of an en dash) plus the hand-made aliases in
backend/data/gold/aliases.json, through the pipeline's keys (registry_check.name_key and
port_key). An exact full-name match wins over a key match. Two marinas named after the same
place are one name (``_place_key``). A match on a gold PoE wins over a match on a rejected
name, whichever name of the port gives it: a commune is rejected as a name, and is a PoE
through its marina.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from app.agents.registry_check import name_key, port_key

GOLD_DIR = Path(__file__).resolve().parents[2] / "data" / "gold"
ALIASES_FILE = GOLD_DIR / "aliases.json"
_LEVEL_RANK = {"C0": 0, "C1": 1, "C2": 2, "C3": 3, "C4": 4}
_DASHES = re.compile(r"\s+[–—]\s+")
# A marina's name, on name_key's output: its kind of facility, then the place it is at.
_MARINA_LEAD = re.compile(r"^(?:la |le |l )?(?:marina|port de plaisance(?: \w+)?|puerto deportivo)"
                          r" (?:de la |de |del |du |des |d )(?=\S)")


# --- gold files ----------------------------------------------------------------------

def gold_files(gold_dir: Path = GOLD_DIR) -> list[Path]:
    return sorted(p for p in gold_dir.glob("*.json") if p.name != ALIASES_FILE.name)


def load_gold(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_aliases(path: str | Path = ALIASES_FILE) -> dict[int, dict[str, list[str]]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}
    return {int(k): v for k, v in raw.items() if not k.startswith("_")}


def gold_mrgids(gold: dict) -> set[int]:
    return {int(z["mrgid"]) for z in gold.get("zones") or []}


def gold_for_zones(mrgids, gold_dir: Path = GOLD_DIR) -> tuple[Path, dict] | None:
    """The first gold file that covers every one of ``mrgids``, or None."""
    want = {int(m) for m in mrgids or []}
    if not want:
        return None
    for path in gold_files(gold_dir):
        gold = load_gold(path)
        if want <= gold_mrgids(gold):
            return path, gold
    return None


# --- names ---------------------------------------------------------------------------

def _forms(name: str | None) -> list[str]:
    """The whole name, the part before a parenthesis, each side of an en dash."""
    name = (name or "").strip()
    if not name:
        return []
    out = [name]
    base = re.sub(r"\(.*?\)", " ", name).strip()
    if base and base != name:
        out.append(base)
    for part in _DASHES.split(base):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def _place_key(name: str) -> str | None:
    """The place of a marina's name, with its own marker, or None for any other name.

    The same marina goes by different words: "La marina de l'Étang Z'Abricot" (gold) and
    "Port de plaisance communautaire de l'Étang Z'abricots" (the clearance list) have no
    other key in common. The marker keeps it apart from the other keys: a marina's place
    never matches a bare place name. A final s is dropped, as in registry_check."""
    key = name_key(name)
    place = _MARINA_LEAD.sub("", key)
    if place == key:
        return None
    return "marina at " + (place[:-1] if re.search(r"[a-z]{3}[^s]s$", place) else place)


def _keys(name: str | None) -> set[str]:
    keys: set[str] = set()
    for form in _forms(name):
        for k in (name_key(form), port_key(form), _place_key(form)):
            if k and len(k.replace(" ", "")) >= 3:
                keys.add(k)
    return keys


class _GoldIndex:
    """Gold entries of one zone with their exact keys and their other keys."""

    def __init__(self, zone: dict, aliases: dict[str, list[str]]):
        self.entries: list[dict] = []
        for kind in ("poe", "not_poe"):
            for e in zone.get(kind) or []:
                names = [e["name"]] + list(aliases.get(e["name"], []))
                self.entries.append({
                    "kind": kind, "entry": e, "name": e["name"],
                    "exact": {name_key(n) for n in names},
                    "keys": set().union(*(_keys(n) for n in names)),
                })

    def _hits(self, names: list[str]) -> list[dict]:
        exact = {name_key(n) for n in names if n}
        hits = [g for g in self.entries if g["exact"] & exact]
        if not hits:
            keys = set().union(*(_keys(n) for n in names)) if names else set()
            hits = [g for g in self.entries if g["keys"] & keys]
        return list({id(g): g for g in hits}.values())

    def _poe_hits(self, names: list[str]) -> list[dict]:
        """Gold PoEs any of ``names`` matches, on its own: a rejected name that a name
        matches exactly must not hide the PoE another name matches by key."""
        hits = [g for n in names for g in self._hits([n])
                if g["kind"] == "poe"]
        keys = set().union(*(_keys(n) for n in names)) if names else set()
        hits += [g for g in self.entries if g["kind"] == "poe" and g["keys"] & keys]
        return list({id(g): g for g in hits}.values())

    def match(self, run_names: list[str]) -> tuple[dict | None, list[str]]:
        """(gold entry, []) on one match; (None, candidate names) on none or several.

        A gold PoE matched by any name of the port wins over a rejected name, its own name
        included: "Fort-de-France" is rejected as a name, and is a PoE through the marina
        among its clearance points. Several distinct PoEs: the port's own name
        (``run_names[0]``) decides if it names one of them, otherwise it is ambiguous. With
        no PoE, the port's own name decides, then its first other rejected name."""
        if not run_names:
            return None, []
        own = self._hits(run_names[:1])
        poes = self._poe_hits(run_names)
        names = list(dict.fromkeys(g["name"] for g in poes))
        if len(names) > 1:
            own_poes = [g for g in own if g["kind"] == "poe"]
            if len({g["name"] for g in own_poes}) == 1:
                return own_poes[0], []
            return None, names
        if poes:
            return poes[0], []
        if own:
            names = list(dict.fromkeys(g["name"] for g in own))
            return (own[0], []) if len(names) == 1 else (None, names)
        hits = self._hits(run_names[1:])
        return (hits[0], []) if hits else (None, [])


# --- run ports -------------------------------------------------------------------------

def run_port(doc: dict) -> dict:
    """A stored run port (poe_run_ports) as the scorer reads it."""
    cps = doc.get("clearance_points") or []
    names = [doc.get("name")] + list(doc.get("merged_names") or []) + [c.get("name") for c in cps]
    judge = doc.get("judge") or {}
    quote = (judge.get("quoted_excerpt") or next((c.get("quote") for c in cps if c.get("quote")), None)
             or doc.get("best_evidence_quote") or "")
    return {"mrgid": int(doc.get("mrgid")), "name": doc.get("name"),
            "names": [n for n in dict.fromkeys(names) if n],
            "level": doc.get("confidence_level") or doc.get("level"),
            "quote": quote, "url": doc.get("best_evidence_url"),
            "judge": judge.get("verdict") or doc.get("judge_verdict")}


def ports_from_csv(path: str | Path) -> list[dict]:
    """Run ports from an export (zone, mrgid, port_name, merged_names, level, …,
    best_evidence_url, best_evidence_quote, judge_verdict); merged_names split on ';'."""
    out = []
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            merged = [m.strip() for m in (row.get("merged_names") or "").split(";") if m.strip()]
            out.append(run_port({
                "mrgid": row["mrgid"], "name": row["port_name"], "merged_names": merged,
                "level": row.get("level") or None,
                "best_evidence_url": row.get("best_evidence_url") or None,
                "best_evidence_quote": row.get("best_evidence_quote") or "",
                "judge_verdict": row.get("judge_verdict") or None}))
    return out


def _is_c2(level: str | None) -> bool:
    return _LEVEL_RANK.get(level or "", -1) >= 2


def _ratio(num: int, den: int) -> dict:
    return {"n": num, "of": den, "value": round(num / den, 4) if den else None}


# --- scoring ---------------------------------------------------------------------------

def score_ports(ports: list[dict], gold: dict, aliases: dict | None = None) -> dict:
    """Score run ports (``run_port`` shape) against a gold document."""
    aliases = load_aliases() if aliases is None else aliases
    zones_out, totals = [], {"c2_right": 0, "c2_judged": 0, "c2_found": 0, "any_found": 0, "poe": 0}
    by_zone: dict[int, list[dict]] = {}
    for p in ports:
        by_zone.setdefault(int(p["mrgid"]), []).append(p)
    for zone in gold.get("zones") or []:
        mrgid = int(zone["mrgid"])
        index = _GoldIndex(zone, aliases.get(mrgid, {}))
        poe_names = [e["name"] for e in zone.get("poe") or []]
        found_any: set[str] = set()
        found_c2: set[str] = set()
        c2_right = c2_judged = 0
        wrong_c2, c1_noise, unmatched = [], [], []
        for p in by_zone.get(mrgid, []):
            g, candidates = index.match(p["names"])
            if g is None:
                unmatched.append({"name": p["name"], "level": p["level"],
                                  "candidates": candidates})
                continue
            if g["kind"] == "poe":
                found_any.add(g["name"])
                if _is_c2(p["level"]):
                    found_c2.add(g["name"])
                    c2_right += 1
                    c2_judged += 1
            else:
                if _is_c2(p["level"]):
                    c2_judged += 1
                    wrong_c2.append({"name": p["name"], "level": p["level"], "gold": g["name"],
                                     "quote": p["quote"], "url": p["url"],
                                     "gold_note": g["entry"].get("note")})
                else:
                    c1_noise.append({"name": p["name"], "level": p["level"], "gold": g["name"]})
        n_poe = len(poe_names)
        zones_out.append({
            "mrgid": mrgid, "zone": zone.get("zone"), "gold_poe": n_poe,
            "run_ports": len(by_zone.get(mrgid, [])),
            "c2_precision": _ratio(c2_right, c2_judged),
            "c2_recall": _ratio(len(found_c2), n_poe),
            "any_level_recall": _ratio(len(found_any), n_poe),
            "wrong_c2": wrong_c2,
            "missed": [n for n in poe_names if n not in found_any],
            "c1_noise": c1_noise,
            "unmatched": unmatched,
        })
        totals["c2_right"] += c2_right
        totals["c2_judged"] += c2_judged
        totals["c2_found"] += len(found_c2)
        totals["any_found"] += len(found_any)
        totals["poe"] += n_poe
    covered = {z["mrgid"] for z in zones_out}
    return {
        "gold_run": gold.get("run"), "checked_by": gold.get("checked_by"),
        "checked_on": gold.get("checked_on"),
        "overall": {
            "c2_precision": _ratio(totals["c2_right"], totals["c2_judged"]),
            "c2_recall": _ratio(totals["c2_found"], totals["poe"]),
            "any_level_recall": _ratio(totals["any_found"], totals["poe"]),
            "wrong_c2": sum(len(z["wrong_c2"]) for z in zones_out),
            "missed": sum(len(z["missed"]) for z in zones_out),
            "c1_noise": sum(len(z["c1_noise"]) for z in zones_out),
            "unmatched": sum(len(z["unmatched"]) for z in zones_out),
        },
        "zones": zones_out,
        "run_zones_not_in_gold": sorted(m for m in by_zone if m not in covered),
    }


async def score_run(db, run_id: str, gold: dict | None = None,
                    gold_path: str | Path | None = None) -> dict | None:
    """Score a stored run (poe_run_zones / poe_run_ports, read only). With no gold given,
    the first gold file that covers every zone of the run is used; None when none does."""
    mrgids = [int(z["mrgid"]) async for z in db.poe_run_zones.find({"run_id": run_id}, {"mrgid": 1})]
    path = Path(gold_path) if gold_path else None
    if gold is None:
        if path is not None:
            gold = load_gold(path)
        else:
            hit = gold_for_zones(mrgids)
            if hit is None:
                return None
            path, gold = hit
    docs = [d async for d in db.poe_run_ports.find({"run_id": run_id})]
    out = score_ports([run_port(d) for d in docs], gold)
    out["run_id"] = run_id
    out["gold_file"] = path.name if path is not None else None
    return out


def format_report(score: dict) -> str:
    """Plain-text report: overall, then per zone, with the lists."""
    def r(x):
        return f"{x['n']}/{x['of']}" + (f" ({x['value']:.0%})" if x["value"] is not None else "")
    o = score["overall"]
    lines = [f"run {score.get('run_id') or '?'} vs gold {score.get('gold_file') or score.get('gold_run')}",
             f"OVERALL  C2 precision {r(o['c2_precision'])}  C2 recall {r(o['c2_recall'])}  "
             f"any-level recall {r(o['any_level_recall'])}  wrong C2 {o['wrong_c2']}  "
             f"missed {o['missed']}  C1 noise {o['c1_noise']}  unmatched {o['unmatched']}"]
    for z in score["zones"]:
        lines.append(f"\n{z['zone']} ({z['mrgid']}): C2 precision {r(z['c2_precision'])}, "
                     f"C2 recall {r(z['c2_recall'])}, any-level recall {r(z['any_level_recall'])}")
        for w in z["wrong_c2"]:
            lines.append(f"  WRONG C2  {w['name']} = gold not_poe {w['gold']!r}  quote: {w['quote'][:160]!r}")
        for m in z["missed"]:
            lines.append(f"  MISSED    {m}")
        for c in z["c1_noise"]:
            lines.append(f"  C1 noise  {c['name']} ({c['level']}) = gold not_poe {c['gold']!r}")
        for u in z["unmatched"]:
            extra = f" (several gold names: {', '.join(u['candidates'])})" if u["candidates"] else ""
            lines.append(f"  UNMATCHED {u['name']} ({u['level']}){extra}")
    if score.get("run_zones_not_in_gold"):
        lines.append(f"\nrun zones not in the gold file: {score['run_zones_not_in_gold']}")
    return "\n".join(lines)


def summary_view(score: dict) -> dict:
    """The score as stored in the run summary: ratios and names, quotes cut to 200 chars,
    no gold notes (they stay in the gold file)."""
    zones = []
    for z in score.get("zones") or []:
        zones.append({
            "mrgid": z["mrgid"], "zone": z["zone"], "gold_poe": z["gold_poe"],
            "c2_precision": z["c2_precision"], "c2_recall": z["c2_recall"],
            "any_level_recall": z["any_level_recall"],
            "wrong_c2": [{"name": w["name"], "gold": w["gold"], "level": w["level"],
                          "quote": (w["quote"] or "")[:200], "url": w["url"]} for w in z["wrong_c2"]],
            "missed": z["missed"],
            "c1_noise": [c["name"] for c in z["c1_noise"]],
            "unmatched": [u["name"] for u in z["unmatched"]],
        })
    return {"gold_file": score.get("gold_file"), "gold_run": score.get("gold_run"),
            "checked_by": score.get("checked_by"), "overall": score["overall"], "zones": zones}
