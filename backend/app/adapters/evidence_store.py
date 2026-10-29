"""Official-source evidence records: stored snippets of fetched official text.

A record is built deterministically from text fetched at generation (or from the
committed official-page snapshots in sample mode), never from model output:

    {mrgid, port_key, url, domain, matched_name, snippet, content_sha256, fetched_at,
     extract_level}

``snippet`` is a verbatim slice (at most 400 chars) around a mention of the port that
passes registry_check.entry_mention: an official URL, the port's name near an entry term,
not inside an address. At most MAX_PER_PORT records per port (best match per URL).

Records live in the Mongo collection ``poe_evidence`` (unique on mrgid + port_key + url),
not on the port document, because the run pipeline and /api/poe/verify read ports from
Zetaris. ``lookup`` reads Mongo, or the snapshots in backend/data/sample/official_pages/
in SAMPLE_MODE or when Mongo is unreachable; any other failure means no evidence, so a
lookup can never make a port official on its own.
"""
from __future__ import annotations

import hashlib
import json
import os

from app.adapters.zetaris import ZetarisClient
from app.agents import registry_check
from app.agents.evaluator import is_official_source
from app.core import paths

COLLECTION = "poe_evidence"
MAX_PER_PORT = 3
MONGO_TIMEOUT_MS = 2000
SAMPLE_PAGES_DIR = paths.SAMPLE_DATA_DIR / "official_pages"
RECORD_FIELDS = ("mrgid", "port_key", "url", "domain", "matched_name", "snippet",
                 "content_sha256", "fetched_at", "extract_level")


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def build_records(mrgid: int | None, port_names: list[str], documents: list[dict]) -> list[dict]:
    """Evidence records for each port name from official documents.

    documents: [{url, text, sha256, fetched_at, extract_level}], where sha256 is the hash of
    the page's full fetched text (text may be a section of it). Only official URLs count;
    each port keeps its closest match per URL, MAX_PER_PORT URLs at most.
    """
    if mrgid is None:
        return []
    official = [d for d in documents if d.get("text") and is_official_source([d.get("url") or ""])]
    records = []
    for name in dict.fromkeys(n for n in port_names if n):
        variants = registry_check.grounding_variants(name)
        best: dict[str, tuple[int, dict]] = {}
        for doc in official:
            mention = registry_check.entry_mention(doc["text"], variants)
            if not mention:
                continue
            record = dict(mrgid=int(mrgid), port_key=registry_check.port_key(name), url=doc["url"],
                          domain=registry_check._domain(doc["url"]), matched_name=mention["matched_name"],
                          snippet=mention["snippet"], content_sha256=doc.get("sha256") or sha256_text(doc["text"]),
                          fetched_at=doc.get("fetched_at"), extract_level=doc.get("extract_level"))
            if doc["url"] not in best or mention["gap"] < best[doc["url"]][0]:
                best[doc["url"]] = (mention["gap"], record)
        records += [r for _, r in sorted(best.values(), key=lambda item: item[0])][:MAX_PER_PORT]
    return records


def documents_from_context(text: str | None, used_sources: list[dict]) -> list[dict]:
    """Generation documents: the "[SOURCE: url]" blocks of the collected text with their
    fetch metadata (sha256 of the full fetched text, collected_at, extraction level)."""
    meta = {s.get("url"): s for s in used_sources or []}
    return [dict(url=url, text=body, sha256=(meta.get(url) or {}).get("sha256"),
                 fetched_at=(meta.get(url) or {}).get("collected_at"),
                 extract_level=(meta.get(url) or {}).get("level"))
            for url, body in registry_check.source_texts_from_context(text).items()]


async def store(db, records: list[dict]) -> int:
    """Upsert records (one per mrgid + port_key + url) and keep the newest MAX_PER_PORT per port."""
    if not records:
        return 0
    coll = db[COLLECTION]
    await coll.create_index([("mrgid", 1), ("port_key", 1), ("url", 1)], unique=True,
                            name="mrgid_port_key_url")
    for record in records:
        doc = {k: record.get(k) for k in RECORD_FIELDS}
        await coll.update_one({k: doc[k] for k in ("mrgid", "port_key", "url")}, {"$set": doc}, upsert=True)
    for mrgid, key in {(r["mrgid"], r["port_key"]) for r in records}:
        stale = await coll.find({"mrgid": mrgid, "port_key": key}, {"_id": 1}) \
            .sort("fetched_at", -1).skip(MAX_PER_PORT).to_list(100)
        if stale:
            await coll.delete_many({"_id": {"$in": [d["_id"] for d in stale]}})
    return len(records)


def sample_documents(mrgid: int | None) -> list[dict]:
    """Documents from the committed official-page snapshots for a zone (one per section)."""
    if mrgid is None or not SAMPLE_PAGES_DIR.is_dir():
        return []
    docs = []
    for path in sorted(SAMPLE_PAGES_DIR.glob("*.json")):
        page = json.loads(path.read_text(encoding="utf-8"))
        if int(mrgid) not in {int(z["mrgid"]) for z in page.get("zones") or []}:
            continue
        docs += [dict(url=page["url"], text=section["text"], sha256=page["sha256"],
                      fetched_at=page["fetched_at"], extract_level=page.get("extract_level"))
                 for section in page.get("sections") or []]
    return docs


def _mongo_records(mrgid: int) -> list[dict]:
    from pymongo import MongoClient
    client = MongoClient(os.environ.get("MONGO_URL") or "mongodb://127.0.0.1:27017",
                         serverSelectionTimeoutMS=MONGO_TIMEOUT_MS, connectTimeoutMS=MONGO_TIMEOUT_MS,
                         socketTimeoutMS=MONGO_TIMEOUT_MS)
    try:
        coll = client[os.environ.get("DB_NAME") or "bi_local"][COLLECTION]
        return list(coll.find({"mrgid": int(mrgid)}, {"_id": 0}).limit(500))
    finally:
        client.close()


def lookup(mrgid: int | None, port_names: list[str], run_id: str) -> list[dict]:
    """Stored evidence records for a zone's ports; [] on any failure (never official).

    SAMPLE_MODE=true reads the official-page snapshots and builds records with
    build_records, as generation does. Otherwise Mongo (poe_evidence); if Mongo is
    unreachable, the snapshots. Traced as a ZetarisAdapter ``evidence_lookup`` event.
    """
    source, status, records, error = "none", "ok", [], None
    try:
        if mrgid is None:
            source = "no-mrgid"
        elif (os.environ.get("SAMPLE_MODE") or "").strip().lower() == "true":
            source, records = "sample_pages", build_records(mrgid, port_names, sample_documents(mrgid))
        else:
            try:
                source, records = "mongo", _mongo_records(mrgid)
            except Exception as exc:  # noqa: BLE001 - Mongo down: use the snapshots
                error, status = f"{type(exc).__name__}: {str(exc)[:120]}", "fallback"
                source, records = "sample_pages", build_records(mrgid, port_names, sample_documents(mrgid))
    except Exception as exc:  # noqa: BLE001 - no evidence is the safe answer
        source, status, records, error = "error", "error", [], f"{type(exc).__name__}: {str(exc)[:120]}"
    by_port = {}
    for r in records:
        by_port[r.get("port_key")] = by_port.get(r.get("port_key"), 0) + 1
    ZetarisClient(run_id=run_id).trace_event(
        "evidence_lookup", status=status, source=source, records=len(records), records_by_port=by_port,
        input_summary={"mrgid": mrgid, "ports": len(port_names)}, **({"error": error} if error else {}))
    return records
