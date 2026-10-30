"""Offline measurement: how much source text the port-extraction readers never see.

For each zone it rebuilds the context the pipeline would assemble (one "[SOURCE: url]"
block per fetched page, each capped at 60,000 chars by ``_collect_texts``), runs the real
``extraction_context`` on it and reports the branch (raw / rag / catalog), the window the
readers send, the chars never sent, and whether each designated port's passage reaches the
reader window and the NER window (first 20,000 chars).

Inputs, all from the repository, no network:
- ``backend/data/sample/official_pages/*.json`` keep the full length (``chars``) of each
  fetched page and exact slices of it at their ``offset``. The rest of each page is
  absent, so it is rebuilt with neutral filler of the same length: branch and sizes are
  exact, the RAG chunk ranking is optimistic (filler never competes with a real list).
- ``--texts DIR``: real fetched texts, one ``<mrgid>__<anything>.txt`` per page (the
  first line may be the URL). With them the RAG ranking is measured, not estimated.
- ``seed/ports_of_entry.geojson``: ports already known per zone.

    cd backend
    python scripts/measure_extraction_context.py [--texts DIR] [--json]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core import rag  # noqa: E402

rag._st_failed = True  # TF-IDF ranking: never load (or download) an embedding model

from app.core.extract import extract_structured_ports  # noqa: E402
from app.core.llm import LLM_CONTEXT_CHARS  # noqa: E402
from app.services.poe_pipeline import extraction_context  # noqa: E402

SOURCE_CAP = 60000  # poe_pipeline._collect_texts: text[:60000] per source
NER_CHARS = 20000   # poe_pipeline._extract_ports_core._ner: context[:20000]
FILLER = "General administrative information about the agency and its services. "


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _fragments(text: str, size: int = 60) -> list[str]:
    """Normalized ~60-char pieces of a passage (RAG re-joins chunks with "\\n…\\n")."""
    flat = _norm(text)
    return [flat[i:i + size] for i in range(0, len(flat) - size + 1, size)] or [flat]


def _rebuild(page: dict) -> str:
    """Full-length text: known sections at their offset, neutral filler elsewhere."""
    n = int(page.get("chars") or 0)
    buf = list((FILLER * (n // len(FILLER) + 1))[:n])
    for sec in page.get("sections") or []:
        off, text = int(sec["offset"]), sec["text"]
        buf[off:off + len(text)] = list(text)
    return "".join(buf)[:max(n, 1)]


def load_sample_pages(root: Path) -> dict:
    zones: dict = collections.defaultdict(lambda: {"name": None, "pages": []})
    for path in sorted((root / "data/sample/official_pages").glob("*.json")):
        page = json.loads(path.read_text(encoding="utf-8"))
        for z in page.get("zones") or []:
            zone = zones[int(z["mrgid"])]
            zone["name"] = z.get("zone_name")
            zone["pages"].append({
                "file": path.name, "url": page["url"], "chars": page["chars"],
                "text": _rebuild(page), "designates": page.get("designates") or [],
                "sections": page.get("sections") or [], "rebuilt": True,
            })
    return zones


def load_texts_dir(d: Path) -> dict:
    zones: dict = collections.defaultdict(lambda: {"name": None, "pages": []})
    for path in sorted(d.glob("*.txt")):
        mrgid = int(path.name.split("__", 1)[0])
        text = path.read_text(encoding="utf-8", errors="replace")
        first, _, rest = text.partition("\n")
        url = first.strip() if first.startswith("http") else path.name
        body = rest if first.startswith("http") else text
        zones[mrgid]["pages"].append({"file": path.name, "url": url, "chars": len(body),
                                      "text": body, "designates": [], "sections": [],
                                      "rebuilt": False})
    return zones


def seed_counts(root: Path) -> dict[int, tuple[str, int]]:
    path = root.parent / "seed/ports_of_entry.geojson"
    if not path.exists():
        return {}
    c: dict[int, list] = {}
    for f in json.loads(path.read_text(encoding="utf-8"))["features"]:
        p = f["properties"]
        c.setdefault(int(p["mrgid"]), [p.get("zone_name"), 0])[1] += 1
    return {k: (v[0], v[1]) for k, v in c.items()}


def measure_zone(mrgid: int, zone: dict) -> dict:
    blocks, lost_at_collection, spans = [], 0, []
    pos = 0
    for page in zone["pages"]:
        header = f"[SOURCE: {page['url']}]\n"
        kept = page["text"][:SOURCE_CAP]
        lost_at_collection += max(0, len(page["text"]) - SOURCE_CAP)
        start = pos + len(header)
        for sec in page["sections"]:
            spans.append((page, sec, start + int(sec["offset"])))
        blocks.append(header + kept)
        pos += len(header) + len(kept) + 2  # "\n\n" join
    raw = "\n\n".join(blocks)
    raw_catalog = extract_structured_ports(raw)
    rag_q = ("official designated ports of entry customs gazette decree "
             f"puertos habilitados puertos de entrada port of entry {zone['name']}")
    context, window, branch = extraction_context(raw, raw_catalog, rag_q)
    sent = context[:window]
    sent_n, ner_n = _norm(sent), _norm(context[:NER_CHARS])
    passages = []
    for page, sec, abs_off in spans:
        if abs_off >= len(raw):
            reach = "lost at collection (beyond 60k of its source)"
        elif branch == "rag":
            frags = _fragments(sec["text"])
            hit = sum(1 for f in frags if f in sent_n)
            reach = f"rag kept {hit}/{len(frags)} fragments" + (
                " (optimistic)" if page["rebuilt"] else "")
        else:
            end = abs_off + len(sec["text"])
            reach = ("sent" if end <= window else
                     "partly sent" if abs_off < window else "NOT sent")
        names = {n: _norm(re.sub(r"^port of ", "", n, flags=re.I)) in sent_n
                 for n in page["designates"]}
        passages.append({"file": page["file"], "offset_in_raw": abs_off,
                         "designates": page["designates"], "reader": reach,
                         "names_in_sent": names,
                         "ner": "yes" if _norm(sec["text"][:200]) in ner_n else "no"})
    return {
        "mrgid": mrgid, "zone": zone["name"], "sources": len(zone["pages"]),
        "source_chars": [p["chars"] for p in zone["pages"]],
        "raw_chars": len(raw), "lost_at_collection": lost_at_collection,
        "catalog_names": len(raw_catalog), "branch": branch, "window": window,
        "sent_chars": len(sent),
        "never_sent": lost_at_collection + max(0, len(raw) - len(sent)),
        "never_sent_pct": round(100 * (lost_at_collection + max(0, len(raw) - len(sent)))
                                / max(1, len(raw) + lost_at_collection), 1),
        "passages": passages,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--texts", type=Path, help="directory of real fetched texts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    zones = load_texts_dir(a.texts) if a.texts else load_sample_pages(BACKEND)
    rows = [measure_zone(m, z) for m, z in sorted(zones.items())]
    seeds = seed_counts(BACKEND)
    if a.json:
        print(json.dumps({"zones": rows, "llm_context_chars": LLM_CONTEXT_CHARS},
                         ensure_ascii=False, indent=1))
        return 0
    print(f"LLM_CONTEXT_CHARS={LLM_CONTEXT_CHARS}  source cap={SOURCE_CAP}  NER={NER_CHARS}")
    for r in rows:
        known = seeds.get(r["mrgid"], (None, 0))[1]
        print(f"\n{r['zone']} (mrgid {r['mrgid']}): {r['sources']} source(s) "
              f"{r['source_chars']} → raw {r['raw_chars']}, branch={r['branch']} "
              f"(catalog names {r['catalog_names']}), sent {r['sent_chars']}/{r['window']}, "
              f"never sent {r['never_sent']} ({r['never_sent_pct']}%), seed ports {known}")
        for p in r["passages"]:
            print(f"  - {p['file']} @ {p['offset_in_raw']}: {p['designates']} → "
                  f"reader: {p['reader']}; names in sent: {p['names_in_sent']}; NER: {p['ner']}")
    big = sorted(((n, v) for m, (v, n) in seeds.items()), reverse=True)[:10]
    print("\nseed: zones with most known ports:", ", ".join(f"{v} {n}" for n, v in big))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
