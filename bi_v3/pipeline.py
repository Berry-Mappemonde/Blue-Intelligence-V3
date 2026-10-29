"""Full V3 run: Tavily (search+extract) -> Nemotron Super extraction -> geo -> Nemotron Ultra judge -> C0..C4."""
from __future__ import annotations
import json, os
from . import tavily, tf, certify
from .dedup import deduplicate, normalize_name
from .geo import nominatim, geo_agreement, haversine_km

LAYERS = {
    "port_of_entry": ["{t} ports of entry for yachts clearance list", "{t} official port of entry customs clearance yacht", "{t} entrée douane plaisance port de clearance"],
    "marina": ["{t} marina list official port de plaisance", "{t} marinas capacity berths official"],
    "harbour_office": ["{t} capitainerie port de plaisance contact VHF", "{t} harbour master office marina"],
}
EXTRACT_SYS = ("You extract maritime facilities from web pages. Return ONLY a JSON array. Each item: "
               '{"name": str, "town": str, "quote": str (verbatim <=200 chars from the page proving it), "lat": number|null, "lon": number|null} '
               '(lat/lon ONLY if the page itself prints the coordinates, else null). '
               "Only facilities of the requested type located in the requested territory. No invention: if the page does not state it, omit it.")
JUDGE_SYS = ("You are a strict evidence judge. For each numbered claim, decide if the quote (taken from the url) SUPPORTS that the named "
             'facility of that type exists in that territory. Return ONLY JSON: [{"i": int, "verdict": "supported"|"unsupported", "why": str<=80}]')

def _load_ref(seed_dir):
    wpi = json.load(open(os.path.join(seed_dir, "wpi_ports.json")))["ports"]
    v2 = json.load(open(os.path.join(seed_dir, "curated_marinas.json")))["marinas"]
    return wpi, v2

def _second_source(e, wpi, v2):
    """Independent non-LLM coordinate: WPI (NGA) or BI V2 curated seed, name-similar and < 5 km from geocode."""
    from .dedup import text_similarity
    if e.get("lat") is None:
        return None
    best = None
    for p in wpi + v2:
        if p.get("lat") is None:
            continue
        km = haversine_km(e["lat"], e["lon"], p["lat"], p["lon"])
        if km < 5 and text_similarity(e["name"], p["name"]) >= 0.6 and (best is None or km < best[0]):
            best = (km, p)
    return {"lat": best[1]["lat"], "lon": best[1]["lon"], "src": "wpi" if "index" in best[1] else "bi-v2"} if best else None

def _coords_in_text(lat, lon, txt):
    import re
    nums = [float(x) for x in re.findall(r"-?\d{1,3}[.,]\d{2,}", txt.replace(",", "."))]
    near = lambda v: any(abs(abs(n) - abs(v)) < 0.01 for n in nums)
    # also accept degree-minute forms: 14°28' -> 14.4667
    for d, m in re.findall(r"(\d{1,3})\s*°\s*(\d{1,2}(?:[.,]\d+)?)", txt):
        nums.append(int(d) + float(m.replace(",", ".")) / 60)
    return near(lat) and near(lon)

CANON_SYS = ("Group names that denote the SAME physical facility. Return ONLY JSON: [[index,...],...] covering every index once. "
             "Be conservative: different towns or different facilities stay separate.")

def _canonicalize(ents, territory, layer):
    if len(ents) < 2:
        return ents
    lst = "\n".join(f'{i}. {e["name"]} (town: {e.get("town")})' for i, e in enumerate(ents))
    groups = tf.parse_json(tf.chat("extract", CANON_SYS, f"Territory {territory}, type {layer}:\n{lst}", max_tokens=1500))
    if not isinstance(groups, list):
        return ents
    out, seen = [], set()
    for g in groups:
        idx = [i for i in (g if isinstance(g, list) else []) if isinstance(i, int) and 0 <= i < len(ents) and i not in seen]
        if not idx:
            continue
        seen.update(idx); base = ents[idx[0]]
        base["aliases"] = [ents[i]["name"] for i in idx[1:]]
        for i in idx[1:]:
            base["evidence"] += ents[i]["evidence"]; base["town"] = base.get("town") or ents[i].get("town")
        out.append(base)
    out += [e for i, e in enumerate(ents) if i not in seen]
    return out

def run_territory(territory: str, cc: str, seed_dir: str, log=print, max_pages=8):
    wpi, v2 = _load_ref(seed_dir)
    entities = []
    for layer, queries in LAYERS.items():
        hits = []
        for q in queries:
            hits += tavily.search(q.format(t=territory), max_results=8)
        urls = list(dict.fromkeys(h["url"] for h in hits))
        urls.sort(key=lambda u: not certify.is_official(u))
        pages = tavily.extract(urls[:max_pages])
        log(f"[{territory}/{layer}] tavily: {len(hits)} hits, {len(pages)} pages extracted")
        cands = []
        for p in pages:
            txt = (p.get("raw_content") or "")[:12000]
            if len(txt) < 200:
                continue
            out = tf.parse_json(tf.chat("extract", EXTRACT_SYS,
                     f"Territory: {territory}\nFacility type: {layer}\nURL: {p['url']}\n---\n{txt}", max_tokens=2500)) or []
            for c in out if isinstance(out, list) else []:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                c["url"] = p["url"]
                q = (c.get("quote") or "").strip()
                in_page = bool(q) and q[:60].lower() in txt.lower()
                pc = None
                try:
                    la, lo = float(c.get("lat")), float(c.get("lon"))
                    if _coords_in_text(la, lo, txt):
                        pc = {"lat": la, "lon": lo, "src": "page"}
                except (TypeError, ValueError):
                    pass
                cands.append({"layer": layer, "territory": territory, "name": c["name"].strip(), "town": c.get("town"),
                              "key": normalize_name(c["name"]),
                              "evidence": [{"url": p["url"], "quote": q[:240], "extracted": in_page, "tool": "tavily.extract", "page_coords": pc}]})
        ents = _canonicalize(deduplicate(cands), territory, layer)
        log(f"[{territory}/{layer}] nemotron-super: {len(cands)} candidates -> {len(ents)} after dedup")
        # judge (Ultra), one batched call per layer
        claims = [(i, e, ev) for i, e in enumerate(ents) for ev in e["evidence"] if ev["extracted"]][:40]
        if claims:
            prompt = "\n".join(f'{k}. type={layer} territory={territory} name="{e["name"]}" url={ev["url"]} quote="{ev["quote"]}"'
                               for k, (_, e, ev) in enumerate(claims))
            verdicts = tf.parse_json(tf.chat("judge", JUDGE_SYS, prompt, max_tokens=4000)) or []
            for v in verdicts if isinstance(verdicts, list) else []:
                try:
                    claims[int(v["i"])][2]["judge"] = v.get("verdict"); claims[int(v["i"])][2]["why"] = v.get("why")
                except (KeyError, ValueError, IndexError, TypeError):
                    pass
        for e in ents:
            g1 = nominatim(f'{e["name"]}, {e.get("town") or ""}, {territory}', cc) or nominatim(f'{e["name"]}, {territory}', cc)
            if g1:
                e["lat"], e["lon"] = g1["lat"], g1["lon"]
            if not g1 and e.get("town"):
                gt = nominatim(f'{e["town"]}, {territory}', cc)
                if gt:
                    e["lat"], e["lon"], e["geo_precision"] = gt["lat"], gt["lon"], "town"
            pcs = [ev["page_coords"] for ev in e["evidence"] if ev.get("page_coords")]
            g2 = pcs[0] if pcs else _second_source(e, wpi, v2)
            if not g1 and g2:
                e["lat"], e["lon"], e["geo_precision"] = g2["lat"], g2["lon"], g2["src"]
            agr = geo_agreement(g1, g2, 0.5)
            e["geo"] = {"primary": g1 and "nominatim", "secondary": g2 and g2["src"], "agree": agr["agree"], "km": agr["km"],
                        "independent_sources": int(bool(g1)) + int(bool(g2))}
            e.setdefault("lat", None); e.setdefault("lon", None)
            e["source_urls"] = list(dict.fromkeys(ev["url"] for ev in e["evidence"]))
            e["level"] = certify.level(e)
        entities += ents
    return entities
