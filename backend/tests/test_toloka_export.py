"""Toloka review batch: local stratified sample, no network.

HTTP entry points are patched. The service never calls them.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import toloka_export as tx  # noqa: E402


def _entity(name, layer, level, url, quote="quoted bit", lat=14.5, lon=-61.0, **extra):
    item = {
        "name": name,
        "layer": layer,
        "level": level,
        "lat": lat,
        "lon": lon,
        "source_urls": [url],
        "evidence": [{"url": url, "quote": quote}],
    }
    item.update(extra)
    return item


def _many(n, layer="marina", level="C2", prefix="M"):
    return [
        _entity(f"{prefix}{i}", layer, level, f"https://example.invalid/{layer}/{level}/{i}")
        for i in range(n)
    ]


def test_docstring_requires_clement_approval():
    assert "never sent without Clement approval" in (tx.__doc__ or "")
    script = Path(tx.__file__).resolve().parents[2] / "scripts" / "toloka_export.py"
    text = script.read_text(encoding="utf-8")
    assert "never sent without Clement approval" in text


def test_source_has_no_toloka_client_or_upload():
    service = Path(tx.__file__).read_text(encoding="utf-8")
    script = (
        Path(tx.__file__).resolve().parents[2] / "scripts" / "toloka_export.py"
    ).read_text(encoding="utf-8")
    for text in (service, script):
        assert "import requests" not in text
        assert "import httpx" not in text
        assert "import urllib" not in text
        assert "urlopen" not in text
        assert "platform.toloka.ai" not in text
        assert "def upload" not in text
        assert "TOLOKA_API_KEY" not in text


def test_export_does_not_touch_the_network(tmp_path, monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("network disabled")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setenv("TOLOKA_API_KEY", "sentinel-not-a-real-key")

    run = tmp_path / "run"
    run.mkdir()
    (run / "entities.geojson").write_text(
        json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-61.1, 14.6]},
                "properties": {
                    "name": "Le Marin",
                    "layer": "marina",
                    "level": "C2",
                    "evidence": [{
                        "url": "https://example.invalid/marin",
                        "quote": "Port de plaisance",
                    }],
                },
            }],
        }),
        encoding="utf-8",
    )
    (run / "manifest.json").write_text(
        json.dumps({"run_id": "run-local"}),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    written = tx.prepare_run(run, out)
    assert calls == []
    blob = Path(written["json"]).read_text(encoding="utf-8")
    assert "sentinel-not-a-real-key" not in blob
    assert "sentinel-not-a-real-key" not in Path(written["tsv"]).read_text(encoding="utf-8")
    batch = json.loads(blob)
    assert batch["run_id"] == "run-local"
    assert batch["note"] == "never sent without Clement approval"
    item = batch["items"][0]
    assert item["name"] == "Le Marin"
    assert item["layer"] == "marina"
    assert item["lat"] == 14.6
    assert item["lon"] == -61.1
    assert item["evidence_url"] == "https://example.invalid/marin"
    assert item["quoted_text"] == "Port de plaisance"
    assert item["question"] == "Is this link the right evidence for this object?"


def test_default_batch_is_23_and_overlap_does_not_duplicate(monkeypatch):
    monkeypatch.delenv("TOLOKA_BATCH_SIZE", raising=False)
    monkeypatch.delenv("TOLOKA_OVERLAP", raising=False)
    monkeypatch.delenv("TOLOKA_SAMPLE_SEED", raising=False)
    entities = _many(40, "marina", "C2") + _many(40, "port_of_entry", "C3", "P")
    batch = tx.build_batch(entities)
    assert batch["requested_batch_size"] == 23
    assert batch["batch_size"] == 23
    assert len(batch["items"]) == 23
    assert batch["overlap"] == 3
    assert batch["seed"] == 42
    assert len({item["item_id"] for item in batch["items"]}) == 23


def test_env_overrides_defaults_and_absence_keeps_them(monkeypatch):
    monkeypatch.delenv("TOLOKA_BATCH_SIZE", raising=False)
    monkeypatch.delenv("TOLOKA_OVERLAP", raising=False)
    assert tx.batch_size_from_env() == 23
    assert tx.overlap_from_env() == 3

    monkeypatch.setenv("TOLOKA_BATCH_SIZE", "")
    monkeypatch.setenv("TOLOKA_OVERLAP", "nope")
    assert tx.batch_size_from_env() == 23
    assert tx.overlap_from_env() == 3

    monkeypatch.setenv("TOLOKA_BATCH_SIZE", "5")
    monkeypatch.setenv("TOLOKA_OVERLAP", "3")
    batch = tx.build_batch(_many(10))
    assert len(batch["items"]) == 5
    assert batch["overlap"] == 3
    explicit = tx.build_batch(_many(10), batch_size=7, overlap=3)
    assert len(explicit["items"]) == 7


def test_stratified_by_layer_and_confidence_level():
    entities = (
        _many(40, "marina", "C2", "A")
        + _many(40, "marina", "C3", "B")
        + _many(20, "port_of_entry", "C2", "C")
        + _many(20, "harbour_office", "C1", "D")
    )
    batch = tx.build_batch(entities, batch_size=23, seed=1)
    sampled = {
        (row["layer"], row["confidence_level"]): row["sampled"]
        for row in batch["strata"]
    }
    assert sampled == {
        ("harbour_office", "C1"): 4,
        ("marina", "C2"): 8,
        ("marina", "C3"): 7,
        ("port_of_entry", "C2"): 4,
    }
    assert sum(sampled.values()) == 23
    layers = {item["layer"] for item in batch["items"]}
    assert layers == {"harbour_office", "marina", "port_of_entry"}


def test_any_layer_is_kept_when_the_batch_covers_them():
    entities = [
        _entity("A", "amp", "C1", "https://example.invalid/amp"),
        _entity("B", "marina", "C2", "https://example.invalid/marina"),
        _entity("C", "capitainerie", "C3", "https://example.invalid/cap"),
        _entity("D", "port_of_entry", "C0", "https://example.invalid/poe"),
    ]
    batch = tx.build_batch(entities, batch_size=23)
    assert {item["layer"] for item in batch["items"]} == {
        "amp", "marina", "capitainerie", "port_of_entry",
    }


def test_numeric_confidence_bands_share_a_stratum():
    entities = [
        _entity("Low", "marina", None, "https://example.invalid/l", confidence=10),
        _entity("Also", "marina", None, "https://example.invalid/a", confidence=15),
        _entity("High", "marina", None, "https://example.invalid/h", confidence=90),
    ]
    for entity in entities:
        entity.pop("level")
    batch = tx.build_batch(entities, batch_size=23)
    levels = {item["name"]: item["confidence_level"] for item in batch["items"]}
    assert levels["Low"] == levels["Also"] == "score:0-19"
    assert levels["High"] == "score:80-100"


def test_skips_missing_url_and_duplicates():
    entities = [
        _entity("Ok", "marina", "C2", "https://example.invalid/ok", quote="a quote"),
        {"name": "No url", "layer": "marina", "level": "C2"},
        {"layer": "marina", "level": "C2", "source_urls": ["https://example.invalid/noname"]},
        _entity("Ok", "marina", "C2", "https://example.invalid/ok", quote="a quote"),
    ]
    batch = tx.build_batch(entities, batch_size=23)
    assert [item["name"] for item in batch["items"]] == ["Ok"]
    assert batch["items"][0]["quoted_text"] == "a quote"
    assert batch["skipped"] == {"no_name_or_url": 2, "duplicate": 1}


def test_fewer_entities_than_batch_returns_all_eligible():
    batch = tx.build_batch(_many(4), batch_size=23, overlap=3)
    assert batch["batch_size"] == 4
    assert batch["overlap"] == 3


def test_sample_is_reproducible_and_seed_changes_membership():
    entities = _many(30)
    first = [
        item["item_id"]
        for item in tx.build_batch(entities, batch_size=5, seed=1)["items"]
    ]
    second = [
        item["item_id"]
        for item in tx.build_batch(entities, batch_size=5, seed=1)["items"]
    ]
    assert first == second
    seen = {tuple(first)}
    for seed in range(2, 12):
        ids = tuple(
            item["item_id"]
            for item in tx.build_batch(entities, batch_size=5, seed=seed)["items"]
        )
        seen.add(ids)
    assert len(seen) > 1


def test_write_tsv_and_json(tmp_path):
    batch = tx.build_batch(
        [_entity("Marina X", "marina", "C2", "https://example.invalid/x", quote='say "hi"\tthere')],
        batch_size=23,
        overlap=3,
        run_id="r1",
    )
    written = tx.write_batch(batch, tmp_path)
    with open(written["tsv"], encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert len(rows) == 1
    assert rows[0]["name"] == "Marina X"
    assert rows[0]["layer"] == "marina"
    assert rows[0]["evidence_url"] == "https://example.invalid/x"
    assert rows[0]["quoted_text"] == 'say "hi"\tthere'
    assert rows[0]["question"] == tx.QUESTION
    assert rows[0]["lat"] == "14.5"
    assert rows[0]["lon"] == "-61"
    payload = json.loads(Path(written["json"]).read_text(encoding="utf-8"))
    assert payload["overlap"] == 3
    assert payload["run_id"] == "r1"
    assert payload["items"][0]["quoted_text"] == 'say "hi"\tthere'


@pytest.mark.parametrize(
    ("answers", "verdict"),
    [
        (["yes", "yes", "yes"], "confirmed"),
        (["Yes", " YES ", "yes"], "confirmed"),
        (["yes", "yes", "no"], "split"),
        (["no", "no", "yes"], "split"),
        (["no", "no", "no"], "rejected"),
        (["yes", "no", "unsure"], "rejected"),
        (["yes", "yes"], "rejected"),
        ([], "rejected"),
        (["yes", "yes", "yes", "yes"], "rejected"),
        (["oui", "oui", "oui"], "rejected"),
    ],
)
def test_aggregate_answers(answers, verdict):
    assert tx.aggregate_answers(answers)["verdict"] == verdict


def test_aggregate_batch_counts():
    summary = tx.aggregate_batch([
        {"item_id": "a", "answers": ["yes", "yes", "yes"]},
        ["yes", "yes", "no"],
        ["no", "no", "no"],
    ])
    assert summary["counts"] == {"confirmed": 1, "split": 1, "rejected": 1}
    assert summary["items"][0]["item_id"] == "a"
    assert summary["note"] == tx.NOTE
    assert "never sent without Clement approval" in summary["note"]


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "toloka_export.py"
    spec = importlib.util.spec_from_file_location("toloka_export_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_writes_batch_and_aggregation(tmp_path, monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise AssertionError("network disabled")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)

    run = tmp_path / "run"
    run.mkdir()
    (run / "entities.geojson").write_text(
        json.dumps({"entities": _many(3, "amp", "C2", "S")}),
        encoding="utf-8",
    )
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps([["yes", "yes", "yes"], ["yes", "yes", "no"]]),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    cli = _load_cli()
    assert cli.main(["--run", str(run), "--out", str(out), "--answers", str(answers)]) == 0
    assert calls == []
    assert (out / "toloka_batch.tsv").is_file()
    assert (out / "toloka_batch.json").is_file()
    summary = json.loads((out / "toloka_aggregation.json").read_text(encoding="utf-8"))
    assert summary["counts"]["confirmed"] == 1
    assert summary["counts"]["split"] == 1
    batch = json.loads((out / "toloka_batch.json").read_text(encoding="utf-8"))
    assert {item["layer"] for item in batch["items"]} == {"amp"}
    assert all(
        item["question"] == "Is this link the right evidence for this object?"
        for item in batch["items"]
    )
