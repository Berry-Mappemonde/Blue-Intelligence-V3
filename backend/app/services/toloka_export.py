"""Stratified human-review batch from a run's entities.

Prepare only. This module writes a local TSV and a local JSON file.
It never calls Toloka and it contains no upload code. A batch is
never sent without Clement approval.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_BATCH_SIZE = 23
DEFAULT_OVERLAP = 3
DEFAULT_SEED = 42
QUESTION = "Is this link the right evidence for this object?"
NOTE = "never sent without Clement approval"

TSV_NAME = "toloka_batch.tsv"
JSON_NAME = "toloka_batch.json"
AGGREGATION_NAME = "toloka_aggregation.json"

TSV_COLUMNS = (
    "item_id",
    "name",
    "layer",
    "lat",
    "lon",
    "evidence_url",
    "quoted_text",
    "question",
    "confidence_level",
)

YES = "yes"
CONFIRMED = "confirmed"
SPLIT = "split"
REJECTED = "rejected"


def _positive_int(raw: str | None, default: int) -> int:
    """Env absent, empty, or not a positive integer → default."""
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    return value if value > 0 else default


def batch_size_from_env() -> int:
    return _positive_int(os.environ.get("TOLOKA_BATCH_SIZE"), DEFAULT_BATCH_SIZE)


def overlap_from_env() -> int:
    return _positive_int(os.environ.get("TOLOKA_OVERLAP"), DEFAULT_OVERLAP)


def seed_from_env() -> int:
    raw = os.environ.get("TOLOKA_SAMPLE_SEED")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_SEED
    try:
        return int(str(raw).strip())
    except ValueError:
        return DEFAULT_SEED


def _num(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _format_coord(value) -> str:
    if value is None:
        return ""
    text = format(float(value), ".7f").rstrip("0").rstrip(".")
    return text or "0"


def confidence_level(entity: dict) -> str:
    """Stratum label: explicit level, else a 0–100 score band.

    Score bands are not the C0–C4 certification scale. An explicit
    ``level`` or ``confidence_level`` is kept as written.
    """
    for key in ("confidence_level", "level"):
        value = entity.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    confidence = entity.get("confidence")
    if confidence is None or confidence == "":
        return "unknown"
    if isinstance(confidence, str):
        text = confidence.strip()
        return text or "unknown"
    score = _num(confidence)
    if score is None:
        return str(confidence).strip() or "unknown"
    if score < 20:
        return "score:0-19"
    if score < 40:
        return "score:20-39"
    if score < 60:
        return "score:40-59"
    if score < 80:
        return "score:60-79"
    return "score:80-100"


def _layer_of(entity: dict) -> str:
    for key in ("layer", "kind"):
        value = entity.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "unknown"


def _name_of(entity: dict) -> str:
    value = entity.get("name")
    if value is None:
        return ""
    return str(value).strip()


def _coords(entity: dict) -> tuple:
    lat = _num(entity.get("lat"))
    lon = _num(entity.get("lon"))
    if lat is not None and lon is not None:
        return lat, lon
    geom = entity.get("geometry")
    if isinstance(geom, dict) and geom.get("type") == "Point":
        coords = geom.get("coordinates") or []
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon_g = _num(coords[0])
            lat_g = _num(coords[1])
            return (
                lat if lat is not None else lat_g,
                lon if lon is not None else lon_g,
            )
    return lat, lon


def _evidence(entity: dict) -> tuple[str, str]:
    """First evidence URL, preferring an entry that also has a quote."""
    chosen_url, chosen_quote = "", ""
    evidence = entity.get("evidence") or []
    if isinstance(evidence, dict):
        evidence = [evidence]
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("evidence_url") or "").strip()
            quote = str(
                item.get("quote")
                or item.get("quoted_text")
                or item.get("quoted_excerpt")
                or item.get("text")
                or ""
            ).strip()
            if url and quote:
                return url, quote
            if url and not chosen_url:
                chosen_url, chosen_quote = url, quote
    if not chosen_url:
        urls = entity.get("source_urls") or entity.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        if isinstance(urls, list):
            for url in urls:
                if isinstance(url, str) and url.strip():
                    chosen_url = url.strip()
                    break
        if not chosen_url:
            for key in ("evidence_url", "url", "source_url"):
                url = entity.get(key)
                if isinstance(url, str) and url.strip():
                    chosen_url = url.strip()
                    break
    if not chosen_quote:
        for key in ("quoted_text", "quote", "quoted_excerpt", "excerpt"):
            quote = entity.get(key)
            if isinstance(quote, str) and quote.strip():
                chosen_quote = quote.strip()
                break
    return chosen_url, chosen_quote


def _item_id(name: str, layer: str, url: str, lat, lon) -> str:
    raw = "\n".join([name, layer, url, _format_coord(lat), _format_coord(lon)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _as_entity(raw: dict) -> dict:
    """Flatten a GeoJSON Feature. Other dicts pass through."""
    if raw.get("type") != "Feature":
        return raw
    props_raw = raw.get("properties")
    props = dict(props_raw) if isinstance(props_raw, dict) else {}
    geom = raw.get("geometry")
    if isinstance(geom, dict):
        props.setdefault("geometry", geom)
    return props


def entity_to_item(raw: dict) -> dict | None:
    """One review item, or None when name or evidence URL is missing."""
    if not isinstance(raw, dict):
        return None
    entity = _as_entity(raw)
    name = _name_of(entity)
    url, quote = _evidence(entity)
    if not name or not url:
        return None
    lat, lon = _coords(entity)
    layer = _layer_of(entity)
    level = confidence_level(entity)
    return {
        "item_id": _item_id(name, layer, url, lat, lon),
        "name": name,
        "layer": layer,
        "lat": lat,
        "lon": lon,
        "evidence_url": url,
        "quoted_text": quote,
        "question": QUESTION,
        "confidence_level": level,
    }


def allocate_strata(sizes: dict[tuple[str, str], int], n: int) -> dict[tuple[str, str], int]:
    """Largest-remainder allocation across (layer, confidence_level).

    Shares are proportional to stratum size. Ties go to the larger
    stratum, then to layer and level in alphabetical order. Integer
    arithmetic only, so the split does not depend on float rounding.
    """
    if n < 0:
        raise ValueError("batch_size must be >= 0")
    if n == 0 or not sizes:
        return {key: 0 for key in sizes}
    total = sum(sizes.values())
    if total <= n:
        return dict(sizes)
    keys = sorted(sizes)
    alloc = {key: (n * sizes[key]) // total for key in keys}
    leftover = n - sum(alloc.values())
    order = sorted(
        keys,
        key=lambda key: (
            -((n * sizes[key]) % total),
            -sizes[key],
            key[0],
            key[1],
        ),
    )
    for key in order:
        if leftover <= 0:
            break
        if alloc[key] < sizes[key]:
            alloc[key] += 1
            leftover -= 1
    return alloc


def _item_sort_key(item: dict) -> tuple:
    return (
        item.get("layer") or "",
        item.get("confidence_level") or "",
        (item.get("name") or "").lower(),
        item.get("evidence_url") or "",
        item.get("item_id") or "",
    )


def build_batch(
    entities,
    *,
    batch_size: int | None = None,
    overlap: int | None = None,
    seed: int | None = None,
    run_id: str | None = None,
) -> dict:
    """Stratified sample. Overlap is metadata (annotators per item).

    Rows are not repeated: overlap 3 still yields one row per item.
    ``batch_size`` / ``overlap`` / ``seed`` fall back to env, then to
    23 / 3 / 42 when those variables are absent.
    """
    if batch_size is None:
        batch_size = batch_size_from_env()
    if overlap is None:
        overlap = overlap_from_env()
    if seed is None:
        seed = seed_from_env()
    if batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    if overlap <= 0:
        raise ValueError("overlap must be a positive integer")

    skipped_unusable = 0
    seen: set[str] = set()
    skipped_duplicate = 0
    prepared: list[dict] = []
    for raw in entities or []:
        item = entity_to_item(raw) if isinstance(raw, dict) else None
        if item is None:
            skipped_unusable += 1
            continue
        if item["item_id"] in seen:
            skipped_duplicate += 1
            continue
        seen.add(item["item_id"])
        prepared.append(item)

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in prepared:
        groups[(item["layer"], item["confidence_level"])].append(item)
    for key in groups:
        groups[key].sort(key=_item_sort_key)

    sizes = {key: len(rows) for key, rows in groups.items()}
    alloc = allocate_strata(sizes, batch_size)
    rng = random.Random(seed)
    chosen: list[dict] = []
    for key in sorted(alloc):
        take = alloc[key]
        pool = groups[key]
        if take <= 0:
            continue
        if take >= len(pool):
            picked = list(pool)
        else:
            picked = rng.sample(pool, take)
        chosen.extend(picked)
    chosen.sort(key=_item_sort_key)

    strata = []
    for key in sorted(groups):
        layer, level = key
        strata.append({
            "layer": layer,
            "confidence_level": level,
            "available": len(groups[key]),
            "sampled": alloc.get(key, 0),
        })

    batch = {
        "note": NOTE,
        "question": QUESTION,
        "overlap": overlap,
        "batch_size": len(chosen),
        "requested_batch_size": batch_size,
        "seed": seed,
        "skipped": {
            "no_name_or_url": skipped_unusable,
            "duplicate": skipped_duplicate,
        },
        "strata": strata,
        "items": chosen,
    }
    if run_id:
        batch["run_id"] = str(run_id)
    return batch


def _json_item(item: dict) -> dict:
    return {
        "item_id": item["item_id"],
        "name": item["name"],
        "layer": item["layer"],
        "lat": item["lat"],
        "lon": item["lon"],
        "evidence_url": item["evidence_url"],
        "quoted_text": item["quoted_text"],
        "question": item["question"],
        "confidence_level": item["confidence_level"],
    }


def _tsv_row(item: dict) -> dict:
    row = _json_item(item)
    row["lat"] = _format_coord(item["lat"])
    row["lon"] = _format_coord(item["lon"])
    return row


def write_batch(batch: dict, output_dir) -> dict:
    """Write ``toloka_batch.tsv`` and ``toloka_batch.json``. Local files only."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "note": batch.get("note") or NOTE,
        "question": batch.get("question") or QUESTION,
        "overlap": batch.get("overlap", DEFAULT_OVERLAP),
        "batch_size": batch.get("batch_size", len(batch.get("items") or [])),
        "requested_batch_size": batch.get("requested_batch_size"),
        "seed": batch.get("seed"),
        "skipped": batch.get("skipped") or {},
        "strata": batch.get("strata") or [],
        "items": [_json_item(item) for item in batch.get("items") or []],
    }
    if batch.get("run_id"):
        payload["run_id"] = batch["run_id"]

    json_path = directory / JSON_NAME
    tsv_path = directory / TSV_NAME
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with tsv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(TSV_COLUMNS),
            delimiter="\t",
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        for item in payload["items"]:
            writer.writerow(_tsv_row(item))
    return {
        "tsv": str(tsv_path),
        "json": str(json_path),
        "batch_size": payload["batch_size"],
        "overlap": payload["overlap"],
    }


def load_run_entities(path) -> tuple[list, dict]:
    """Load entities from a run directory or a JSON / GeoJSON file.

    A directory is read from ``entities.geojson``. ``manifest.json``
    may supply ``run_id``. Any layer present in the file is kept.
    """
    source = Path(path)
    meta: dict = {}
    if source.is_dir():
        manifest_path = source / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = None
            if isinstance(manifest, dict) and manifest.get("run_id"):
                meta["run_id"] = str(manifest["run_id"])
        source = source / "entities.geojson"
    if not source.is_file():
        raise FileNotFoundError(f"entities not found: {source}")
    data = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data, meta
    if isinstance(data, dict):
        if isinstance(data.get("features"), list):
            return data["features"], meta
        if isinstance(data.get("entities"), list):
            if data.get("run_id") and "run_id" not in meta:
                meta["run_id"] = str(data["run_id"])
            return data["entities"], meta
    raise ValueError(f"unsupported entities file: {source}")


def prepare_run(path, output_dir, **kwargs) -> dict:
    """Load a run, sample, and write TSV + JSON. No network."""
    entities, meta = load_run_entities(path)
    if kwargs.get("run_id") is None and meta.get("run_id"):
        kwargs["run_id"] = meta["run_id"]
    batch = build_batch(entities, **kwargs)
    written = write_batch(batch, output_dir)
    written["run_id"] = batch.get("run_id")
    written["note"] = NOTE
    return written


def aggregate_answers(answers) -> dict:
    """One item, overlap 3.

    3 ``yes`` → confirmed. A 2-vs-1 split of the three answers → split.
    Anything else (unanimous no, 1/1/1, too few or too many answers) → rejected.
    Only the token ``yes`` (case-insensitive) counts as yes.
    """
    labels = [str(answer).strip().lower() for answer in (answers or [])]
    yes = sum(1 for label in labels if label == YES)
    verdict = REJECTED
    if len(labels) == 3 and yes == 3:
        verdict = CONFIRMED
    elif len(labels) == 3 and sorted(Counter(labels).values()) == [1, 2]:
        verdict = SPLIT
    return {"verdict": verdict, "yes": yes, "n": len(labels)}


def _answers_of(row) -> tuple[list, str | None]:
    if isinstance(row, dict):
        answers = row.get("answers")
        if answers is None:
            answers = row.get("labels")
        item_id = row.get("item_id")
        return list(answers or []), (str(item_id) if item_id else None)
    if isinstance(row, (list, tuple)):
        return list(row), None
    return [row], None


def aggregate_batch(rows) -> dict:
    """Aggregate each item's answers. Local only — nothing is sent."""
    items = []
    counts = {CONFIRMED: 0, SPLIT: 0, REJECTED: 0}
    for row in rows or []:
        answers, item_id = _answers_of(row)
        result = aggregate_answers(answers)
        counts[result["verdict"]] += 1
        entry = {
            "verdict": result["verdict"],
            "yes": result["yes"],
            "n": result["n"],
            "answers": [str(answer) for answer in answers],
        }
        if item_id:
            entry["item_id"] = item_id
        items.append(entry)
    return {
        "note": NOTE,
        "overlap": DEFAULT_OVERLAP,
        "counts": counts,
        "items": items,
    }


def load_answer_rows(path) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    raise ValueError("answers file must be a list, or an object with items")


def write_aggregation(summary: dict, output_dir) -> str:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / AGGREGATION_NAME
    target.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return str(target)
