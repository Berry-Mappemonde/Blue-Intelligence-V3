"""Lists of border crossing points: a designating list (CAHIER_DES_CHARGES_POE §10.1).

Clément, 3 Nov 2026: for a Schengen zone the PoE list is the list of sea border crossing
points, e.g. the Canaries' five posts in the Schengen Handbook Annex 4 (Spain, sea borders)
and in Orden INT/859/2023 Annex V "Puestos fronterizos".

Two row formats are read:
  - Annex 4: numbered items under a State's "Sea borders" heading, "(27) Santa Cruz de
    Tenerife", "(18) La Luz (Las Palmas)";
  - Orden INT Annex V: a two-column table, post | responsible police station,
    "Puerto de Arrecife.   Comisaría Local de Arrecife.". Only the post column counts: a
    "Comisaría …" cell is never a post (it is full of place names), and an "Aeropuerto de …"
    row is an airport.

Both lists are country-wide, so a row belongs to a zone only when a port of that exact name
with known coordinates (WPI, OSM port priors) lies in the zone: "(27) San Sebastián" is the
Basque port, not San Sebastián de La Gomera, and is never a Canaries row. A row no gazetteer
confirms is dropped (and listed by ``zone_rows``), never guessed.

One post can be read twice from the same list, as it comes out of the fetch: the two
parsers both see a line of Annex 4, and the Orden's police-station index repeats a place
name as if it were a post. A row whose name sits inside one other post of the same list is
that post, not a new candidate (``fold_contained_rows``).
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"

LIST_MARKERS = re.compile(
    r"border crossing points?|puestos? fronterizos?|points? de passage frontaliers?"
    r"|valichi di frontiera|grenz(?:ü|ue)bergangsstellen|pontos? de passagem fronteiri"
    r"|fronteira mar[ií]tima\s*[–—-]",
    re.I)
# "h) Fronteira marítima – Porto de Porto Novo, situado na Ilha de Santo Antão;": a decree's item.
_DESIGNATED_POST = re.compile(
    r"\b[a-z]\)\s*Fronteira mar[ií]tima\s*[–—-]\s*([^;,]+?),\s*situad[oa][^;]*?(?:;|\.(?=\s|$))", re.I)
_SEA_HEADING = re.compile(r"(?im)^\s*(?:sea borders|fronteras mar[ií]timas|fronti[eè]res maritimes)\b")
_OTHER_HEADING = re.compile(r"(?im)^\s*(?:air borders|land borders|airports|fronteras (?:a[ée]reas|terrestres))\b")
_ITEM = re.compile(r"\((\d{1,3})\)\s*([^\n(]+?(?:\([^)\n]*\))?)\s*(?=\(\d{1,3}\)|\n|$)")
_SEGMENT_END = re.compile(r"(?<=\.)\s+|\n+")
_POST = re.compile(r"^Puerto\s+(?:(?:de|del)\s+(?:(?:la|las|los)\s+)?)?(.+?)\.?$")


def _fold(name: str | None) -> str:
    nfd = unicodedata.normalize("NFD", (name or "").casefold())
    s = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


_PARTICLES = frozenset({"de", "del", "la", "las", "los", "el", "da", "do", "di"})


def _match_key(name: str | None) -> str:
    """The folded name without its short particles: "Puerto Rosario" (Orden INT) and
    "Puerto Del Rosario" (WPI) are the same port. Every other word must be equal."""
    return " ".join(t for t in _fold(name).split() if t not in _PARTICLES)


_ANNEX4_ITEM_LINE = re.compile(r"(?m)^\s*\(\d{1,3}\)\s+\S")


def is_border_crossing_list(text: str | None) -> bool:
    """A list of border crossing points: its title wording, or Annex 4's own structure,
    a sea-border heading followed by numbered items (the Annex's State pages carry no
    title: the real Spain pages start at "Sea borders")."""
    t = text or ""
    if LIST_MARKERS.search(t):
        return True
    sea = _SEA_HEADING.search(t)
    return bool(sea) and len(_ANNEX4_ITEM_LINE.findall(t[sea.end():sea.end() + 4000])) >= 3


# --- the two row formats -----------------------------------------------------------------

def annex4_rows(text: str, state: str) -> list[dict]:
    """Numbered items of ``state``'s sea-border section of the Schengen Handbook Annex 4.

    The section starts at the first sea-border heading after the State's own heading
    (its English name on a line of its own, any case) and ends at the next air / land
    heading or the next State heading. -> [{number, name, row}]."""
    t = text or ""
    head = re.search(rf"(?im)^\s*{re.escape(state)}\s*$", t)
    start = head.end() if head else 0
    sea = _SEA_HEADING.search(t, start)
    if not sea:
        return []
    end_m = _OTHER_HEADING.search(t, sea.end())
    nxt = re.search(r"(?m)^\s*[A-Z][A-Z \-]{3,}\s*$", t[sea.end():])
    end = min(end_m.start() if end_m else len(t), sea.end() + nxt.start() if nxt else len(t))
    out = []
    for m in _ITEM.finditer(t[sea.end():end]):
        name = " ".join(m.group(2).split()).rstrip(" .;,")
        if name:
            out.append({"number": int(m.group(1)), "name": name, "row": f"({m.group(1)}) {name}"})
    return out


def orden_int_rows(text: str) -> list[dict]:
    """Posts of a "Puestos fronterizos" table: each cell ends with a full stop, after a "|"
    when the table reaches us as text; a cell that starts with "Puerto" is a post, one that
    starts with "Comisaría" (the police-station column) or "Aeropuerto" (an airport) is not.
    -> [{name, row}], name without "Puerto de"."""
    out = []
    for cell in _SEGMENT_END.split(text or ""):
        cell = " ".join(cell.split()).lstrip("| ")
        if not cell.startswith(("Puerto ", "Puerto\u00a0")):
            continue
        m = _POST.match(cell)
        if m and m.group(1).strip():
            out.append({"name": m.group(1).strip(), "row": cell})
    return out


_ENTRY_NUMBER = re.compile(r"^\d{1,3}\.$")
ROW_MAX_CHARS = 80  # a numbered item or a post's cell; a longer one is a sentence


def row_spans(text: str | None) -> list[tuple[int, int]]:
    """(start, end) of each row of a border crossing list, in any form of the text: numbered
    items and "Puerto …" cells, as ``annex4_rows`` and ``orden_int_rows`` read them, wherever
    they are, and a decree's items (``designated_post_rows``). None when the text is not such
    a list."""
    t = text or ""
    if not is_border_crossing_list(t):
        return []
    spans = [m.span() for m in _ITEM.finditer(t)]
    start, before = 0, ""
    for end_mark in [*_SEGMENT_END.finditer(t), None]:
        end = end_mark.start() if end_mark else len(t)
        # "87. Puerto del Rosario." is an entry of a numbered list of towns, not a post.
        lead = len(t[start:end]) - len(t[start:end].lstrip("| "))
        if (t[start + lead:end].startswith(("Puerto ", "Puerto\u00a0"))
                and not _ENTRY_NUMBER.match(before)):
            spans.append((start + lead, end))
        before = t[start:end]
        start = end_mark.end() if end_mark else end
    return ([(a, b) for a, b in spans if b - a <= ROW_MAX_CHARS]
            + [m.span() for m in _DESIGNATED_POST.finditer(t)])


def designated_post_rows(text: str | None) -> list[dict]:
    """The sea posts a decree opens, one item each. -> [{name, row}], once per post: the act
    may give the list twice, as amended and in its consolidated text."""
    out = {}
    for m in _DESIGNATED_POST.finditer(text or ""):
        name = " ".join(m.group(1).split())
        out.setdefault(name.casefold(), {"name": name, "row": " ".join(m.group(0).split())})
    return list(out.values())


def is_designated_post(row: str | None) -> bool:
    return bool(_DESIGNATED_POST.fullmatch((row or "").strip()))


_LEADING_PUERTO = re.compile(r"^Puerto\s+(?:de|del)\s+(?=\S)", re.I)


def _row_forms(name: str) -> list[str]:
    """"La Luz (Las Palmas)" -> ["La Luz (Las Palmas)", "La Luz", "Las Palmas"]; a leading
    "Puerto de" also gives the name without it (Annex 4: "(25) Puerto de Santa Cruz de La
    Palma (La Palma)"). The full form always comes first ("Puerto del Rosario" stays)."""
    forms = [name]
    base = re.sub(r"\(.*?\)", " ", name).strip()
    forms += [base, _LEADING_PUERTO.sub("", base)]
    forms += [q.strip() for q in re.findall(r"\(([^)]*)\)", name)]
    return [f for f in dict.fromkeys(forms) if f]


# --- the zone filter -------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _gazetteer() -> list[tuple[str, float, float, frozenset]]:
    """(match key, lat, lon, mrgids it is recorded in) of WPI ports and OSM port priors."""
    out = []
    try:
        for p in json.loads((DATA / "wpi_ports.json").read_text(encoding="utf-8")).get("ports") or []:
            if p.get("lat") is not None and p.get("lon") is not None:
                out.append((_match_key(p.get("name")), float(p["lat"]), float(p["lon"]), frozenset()))
    except (OSError, ValueError):
        pass
    try:
        for p in json.loads((DATA / "osm_port_priors.json").read_text(encoding="utf-8")).get("ports") or []:
            mrgids = frozenset(int(e["mrgid"]) for e in p.get("eez") or []
                               if e.get("mrgid") is not None and e.get("relation") in ("in_eez", "coastal_15km"))
            if p.get("lat") is not None and p.get("lon") is not None:
                out.append((_match_key(p.get("name")), float(p["lat"]), float(p["lon"]), mrgids))
    except (OSError, ValueError):
        pass
    return out


@lru_cache(maxsize=64)
def _zone_shape(mrgid: int):
    try:
        from shapely.geometry import shape
        doc = json.loads((DATA / "eez_world_map.geojson").read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return None
    for f in doc.get("features") or []:
        if int((f.get("properties") or {}).get("mrgid") or 0) == int(mrgid):
            return shape(f["geometry"])
    return None


def _zone_geometry(zone: dict | None):
    zone = zone or {}
    if zone.get("geometry"):
        try:
            from shapely.geometry import shape
            return shape(zone["geometry"])
        except Exception:  # noqa: BLE001 — fall back to the map by mrgid
            pass
    try:
        return _zone_shape(int(zone.get("mrgid")))
    except (TypeError, ValueError):
        return None


def in_zone(name: str, zone: dict | None) -> tuple[bool, str]:
    """(True, how) when a port of exactly this name is recorded in the zone: an OSM prior
    tagged with its mrgid, or a WPI / OSM point in its polygon (within ~15 km of the shore).
    (False, reason) otherwise, with a homonym elsewhere named in the reason."""
    key = _match_key(name)
    if not key:
        return False, "empty name"
    hits = [g for g in _gazetteer() if g[0] == key]
    if not hits:
        return False, "no port of that name in WPI / OSM"
    try:
        mrgid = int((zone or {}).get("mrgid"))
    except (TypeError, ValueError):
        mrgid = None
    if mrgid is not None and any(mrgid in g[3] for g in hits):
        return True, "OSM port prior of this zone"
    geom = _zone_geometry(zone)
    if geom is not None:
        from shapely.geometry import Point
        for _k, lat, lon, _m in hits:
            if geom.buffer(0.15).contains(Point(lon, lat)):
                return True, f"WPI / OSM point {lat:.2f}, {lon:.2f} in the zone"
        lat, lon = hits[0][1], hits[0][2]
        return False, f"homonym outside the zone ({lat:.2f}, {lon:.2f})"
    return False, "no zone geometry"


def _contains(words: list[str], part: list[str]) -> bool:
    """``part`` is a run of whole words of ``words``, never a piece of a word."""
    n = len(part)
    return bool(n) and any(words[i:i + n] == part for i in range(len(words) - n + 1))


def fold_contained_rows(rows: list[dict]) -> list[dict]:
    """The posts of ``rows`` (rows of one list, in one zone), each once, in reading order.

    A row whose name (``_match_key``) is contained in exactly one other post is folded into
    it: it makes no candidate of its own and is kept in the post's ``folded``. Longer names
    go first, so the post is found whichever row was read first. A row contained in several
    posts could be any of them, so it stays a row of its own: never guessed."""
    words = [_match_key(r["name"]).split() for r in rows]
    posts: list[int] = []
    folded: dict[int, list[dict]] = {}
    for i in sorted(range(len(rows)), key=lambda k: (-len(words[k]), k)):
        hosts = [p for p in posts if _contains(words[p], words[i])]
        if len(hosts) == 1:
            folded.setdefault(hosts[0], []).append(rows[i])
        else:
            posts.append(i)
    return [{**rows[i], "folded": folded[i]} if i in folded else rows[i] for i in sorted(posts)]


def zone_rows(text: str, zone: dict | None, state_name: str | None = None
              ) -> tuple[list[dict], list[dict]]:
    """Rows of a border-crossing-point list that belong to ``zone``, and the rows dropped
    (with why). Annex 4 items of ``state_name``'s sea borders and Orden INT posts, one row
    per post (``fold_contained_rows``).
    -> (kept [{name, row, matched, how, folded?}], dropped [{name, row, reason}])."""
    if not is_border_crossing_list(text):
        return [], []
    rows = (annex4_rows(text, state_name) if state_name else []) + orden_int_rows(text)
    kept, dropped, seen = [], [], set()
    for r in rows:
        if r["row"] in seen:
            continue
        seen.add(r["row"])
        names = _row_forms(r["name"])
        if r["row"].startswith("Puerto ") and _fold(r["row"].rstrip(".")) != _fold(r["name"]):
            names.insert(0, r["row"].rstrip(".")[len("Puerto de "):] if r["row"].startswith("Puerto de ")
                         else r["row"].rstrip("."))
        verdicts = [(n, in_zone(n, zone)) for n in dict.fromkeys(names)]
        ok = next(((n, how) for n, (good, how) in verdicts if good), None)
        if ok:
            kept.append({"name": r["name"], "row": r["row"], "matched": ok[0], "how": ok[1]})
        else:
            dropped.append({"name": r["name"], "row": r["row"],
                            "reason": "; ".join(f"{n}: {how}" for n, (_g, how) in verdicts)})
    return fold_contained_rows(kept), dropped


def zone_state_name(zone: dict | None) -> str | None:
    """English name of the zone's Schengen State, as Annex 4 heads its section."""
    from app.services.territory_ref import schengen_state, with_sovereign_iso2
    zone = with_sovereign_iso2(zone or {})
    state = schengen_state(zone.get("iso2"), zone.get("mrgid"))
    if not state:
        return None
    try:
        import pycountry
        c = pycountry.countries.get(alpha_2=state)
        return (getattr(c, "common_name", None) or c.name) if c else None
    except ImportError:
        return None
