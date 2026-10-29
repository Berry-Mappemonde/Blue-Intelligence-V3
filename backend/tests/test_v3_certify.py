"""Échelle C0–C4, citation vérifiée, manifeste de run immuable.

Aucun réseau : pas d'appel HTTP. Les variables d'environnement nouvelles
sont optionnelles.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.run_pipeline import _port_out  # noqa: E402
from app.services.poe_confidence import apply_score, score_port  # noqa: E402
from app.services.poe_pipeline import ports_to_geojson  # noqa: E402
from app.services.v3_certify import (  # noqa: E402
    assess_level,
    assess_port,
    confidence_level,
    is_official,
    level,
    level_from_v2,
    verified_quote,
)
from app.services.v3_run_manifest import (  # noqa: E402
    code_fingerprint,
    default_runs_dir,
    from_geojson,
    new_run_id,
    rules_fingerprint,
    write_run,
)

GOV = "https://www.martinique.gouv.fr/x"


def _v2(**overrides):
    port = {
        "name": "Marina du Marin",
        "source_urls": [GOV],
        "extraction_engine": "llm",
        "confidence_reasons": ["source d'État", "un seul extracteur"],
    }
    port.update(overrides)
    return port


def test_level_matches_prototype_ladder():
    aggregator = {
        "name": "Marina du Marin",
        "evidence": [{
            "url": "https://navily.com/x", "extracted": True, "judge": "supported",
        }],
    }
    assert level(aggregator) == "C0"
    assert confidence_level(aggregator) == "C0"
    assert is_official("https://navily.com/x", "Marina du Marin") is False

    official = {
        "name": "Marina du Marin",
        "evidence": [{"url": GOV, "extracted": True, "judge": "supported"}],
    }
    assert level(official) == "C2"
    official["geo"] = {"agree": True, "independent_sources": 2}
    assert level(official) == "C3"
    official["human"] = "confirmed"
    assert level(official) == "C4"
    assert confidence_level(official) == "C4"
    assert level({}) == "C0"

    own_domain = {
        "name": "Le Marin",
        "evidence": [{"url": "https://lemarin.fr/a", "extracted": True}],
    }
    assert level(own_domain) == "C1"


def test_llm_only_never_above_c1_even_with_geo_and_human():
    port = _v2(
        claude_agreement=True,
        geocode_agree=True,
        human="confirmed",
        confidence_reasons=["source d'État", "deux lecteurs LLM"],
    )
    assert level_from_v2(port) == "C1"
    judged = _v2(judge="supported", geocode_agree=True, human="confirmed")
    assert level_from_v2(judged) == "C4"


def test_v2_official_flag_and_catalogue_ladder():
    assert level_from_v2(_v2(
        source_urls=["https://navily.com/x"],
        confidence_reasons=["source nationale non whitelistée"],
    ), official_source=False) == "C0"
    assert level_from_v2(_v2(
        source_urls=["https://navily.com/x"],
        confidence_reasons=[],
    ), official_source=True) == "C1"

    catalogue = _v2(
        extraction_engine="catalog",
        extraction_agreement=True,
        confidence_reasons=["source d'État", "catalogue officiel"],
        geocode_agree=True,
    )
    assert level_from_v2(catalogue) == "C3"
    assert level_from_v2({**catalogue, "human": "confirmed"}) == "C4"
    assert level_from_v2({**catalogue, "geocode_agree": False}) == "C2"
    assert level_from_v2(_v2(from_synthesis=True, extraction_engine="catalog",
                             geocode_agree=True, human="confirmed")) == "C0"


def test_advisory_judge_does_not_lift_and_unverified_quote_downgrades():
    url = GOV
    base = _v2()
    advisory = {
        **base,
        "judge": {
            "verdict": "poe",
            "quoted_excerpt": "port of entry",
            "excerpt_verified": True,
        },
    }
    assert level_from_v2(advisory, pages={url: "port of entry"}) == "C1"

    supported = {
        **base,
        "judge": {
            "verdict": "supported",
            "quoted_excerpt": "Port of entry at Fort-de-France",
            "excerpt_verified": False,
        },
    }
    pages = {url: "notice: port of entry at fort-de-france for yachts"}
    assert level_from_v2(supported, pages=pages) == "C2"
    missing = assess_port(supported, pages={url: "unrelated page"})
    assert missing["confidence_level"] == "C1"
    assert missing["downgraded_from"] == "C2"

    lying = {
        **base,
        "judge": {
            "verdict": "supported",
            "quoted_excerpt": "not in the page",
            "excerpt_verified": True,
        },
    }
    assert level_from_v2(lying, pages={url: "official list"}) == "C1"


def test_verified_quote_normalizes_whitespace_and_case_only():
    page = "Hello   WORLD\nfrom Martinique"
    assert verified_quote("  hello world from   martinique ", page)
    assert verified_quote("hello\u00a0world from martinique", page)
    assert not verified_quote("", page)
    assert not verified_quote("   ", page)
    assert not verified_quote("hello world from guadeloupe", page)

    prefix = "a" * 60
    long_page = prefix + " tail-of-page"
    long_quote = prefix + " different-tail"
    assert long_quote[:60].lower() in long_page.lower()
    assert not verified_quote(long_quote, long_page)


def test_bad_extraction_quote_downgrades_and_does_not_mutate():
    url = GOV
    entity = {
        "name": "Marina du Marin",
        "evidence": [{
            "url": url,
            "quote": "Port   of\nENTRY at   fort-de-france",
            "extracted": True,
            "judge": "supported",
        }],
        "geo": {"agree": True, "independent_sources": 2},
    }
    pages = {url: "port of entry at fort-de-france"}
    assert level(entity) == "C3"
    assert confidence_level(entity, pages=pages) == "C3"
    kept = assess_level(entity, pages=pages)
    assert kept["quote_verified"] is True
    assert "downgraded_from" not in kept

    snapshot = deepcopy(entity)
    dropped = assess_level(entity, pages={url: "different text"})
    assert dropped["confidence_level"] == "C0"
    assert dropped["downgraded_from"] == "C3"
    assert dropped["quote_verified"] is False
    assert entity == snapshot

    port = _v2(
        extraction_engine="catalog",
        confidence_reasons=["source d'État", "catalogue officiel"],
        geocode_agree=True,
        quote="Official   CATALOGUE\ntext",
    )
    assert level_from_v2(port, pages={url: "official catalogue text"}) == "C3"
    fabricated = assess_port(port, pages={url: "a completely different sentence"})
    assert fabricated["confidence_level"] == "C0"
    assert fabricated["downgraded_from"] == "C3"


def test_catalogue_survives_a_bad_judge_excerpt():
    port = _v2(
        extraction_engine="catalog",
        confidence_reasons=["source d'État", "catalogue officiel"],
        geocode_agree=True,
        judge={
            "verdict": "supported",
            "quoted_excerpt": "not on the page",
            "excerpt_verified": True,
        },
    )
    assert level_from_v2(port, pages={GOV: "official catalogue text"}) == "C3"


def test_apply_score_sets_level_without_changing_numeric_score():
    port = {
        "name": "Papeete",
        "extraction_engine": "catalog",
        "extraction_agreement": True,
        "claude_agreement": True,
        "validated": True,
        "spatial_kind": "in_eez",
        "geocode_agree": True,
        "lat": -17.5,
        "lon": -149.5,
        "note": "catalogue officiel",
        "source_urls": ["https://douane.gouv.fr/x"],
        "osm_confidence": 0.9,
    }
    scored = score_port(port, official_source=True)
    apply_score(port, scored)
    assert port["confidence"] == scored["confidence"]
    assert port["confidence_parts"] == scored["parts"]
    assert port["confidence_level"] == "C3"

    llm = _v2(geocode_agree=True, claude_agreement=True, lat=14.5, lon=-61.0,
              spatial_kind="in_eez", validated=True)
    scored_llm = score_port(llm, official_source=True)
    apply_score(llm, scored_llm)
    assert llm["confidence"] == scored_llm["confidence"]
    assert llm["confidence_level"] == "C1"
    assert llm["confidence"] > 40


def test_geojson_and_run_output_include_confidence_level():
    doc = {
        "_id": "p1",
        "name": "Papeete",
        "lat": -17.5,
        "lon": -149.5,
        "source_urls": [GOV],
        "extraction_engine": "catalog",
        "geocode_agree": True,
        "confidence_reasons": ["source d'État", "catalogue officiel"],
    }
    props = ports_to_geojson([doc])["features"][0]["properties"]
    assert props["confidence_level"] == "C3"
    stored = {**doc, "_id": "p2", "confidence_level": "C1"}
    assert ports_to_geojson([stored])["features"][0]["properties"]["confidence_level"] == "C1"

    out = _port_out({
        "name": "Papeete",
        "verdict": "pass",
        "confidence": 70,
        "lat": -17.5,
        "lon": -149.5,
        "reasons_en": [],
        "source_urls": [GOV],
        "confidence_reasons": ["source d'État", "un seul extracteur"],
        "extraction_engine": "llm",
    })
    assert out["confidence_level"] == "C1"


def test_levels_work_when_new_env_is_absent(monkeypatch):
    monkeypatch.delenv("V3_RUNS_DIR", raising=False)
    assert level_from_v2({"name": "X"}) == "C0"
    path = default_runs_dir()
    assert path.parts[-3:] == ("data", "v3", "runs")


def test_runs_dir_env_is_optional(monkeypatch, tmp_path):
    monkeypatch.setenv("V3_RUNS_DIR", str(tmp_path / "custom-runs"))
    assert default_runs_dir() == tmp_path / "custom-runs"
    monkeypatch.setenv("V3_RUNS_DIR", "  ")
    assert default_runs_dir().parts[-3:] == ("data", "v3", "runs")


def test_code_fingerprint_tracks_python_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-value")
    (tmp_path / "a.py").write_text("a = 1\n")
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-secret-value\n")
    (tmp_path / "note.txt").write_text("sk-or-secret-value\n")
    first = code_fingerprint(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    assert "secret" not in first
    (tmp_path / "a.py").write_text("a = 2\n")
    assert code_fingerprint(tmp_path) != first
    (tmp_path / "a.py").write_text("a = 1\n")
    (tmp_path / "note.txt").write_text("other\n")
    assert code_fingerprint(tmp_path) == first


def test_default_code_fingerprint_is_stable():
    assert re.fullmatch(r"[0-9a-f]{12}", code_fingerprint())
    assert code_fingerprint() == code_fingerprint()


def test_run_id_mixes_timestamp_code_and_rules(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n")
    rid = new_run_id(tmp_path, now=0)
    assert rid.startswith("19700101T000000-c")
    assert re.fullmatch(r"\d{8}T\d{6}-c[0-9a-f]{12}-r[0-9a-f]{8}", rid)
    assert f"-c{code_fingerprint(tmp_path)}-r{rules_fingerprint()}" == rid[rid.index("-c"):]
    other_rules = new_run_id(tmp_path, rules={"levels": "C0-C4 v2"}, now=0)
    assert other_rules.split("-r")[0] == rid.split("-r")[0]
    assert other_rules != rid
    (tmp_path / "a.py").write_text("a = 2\n")
    other_code = new_run_id(tmp_path, now=0)
    assert other_code.split("-r")[1] == rid.split("-r")[1]
    assert other_code != rid


def test_manifest_hashes_every_artifact_and_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret-value")
    runs = tmp_path / "runs"
    entities = [
        {"name": "Pointe-à-Pitre", "lat": 16.24, "lon": -61.53,
         "layer": "marina", "level": "C1"},
        {"name": "Sans point", "lat": None, "lon": None, "layer": "marina", "level": "C0"},
    ]
    note = "café\n"
    _, manifest = write_run(
        runs, "r1", {"note.txt": note}, entities=entities, meta={"kind": "test"})
    dest = runs / "r1"
    assert {p.name for p in dest.iterdir()} == {
        "entities.geojson", "note.txt", "manifest.json",
    }
    assert set(manifest["sha256"]) == {"entities.geojson", "note.txt"}
    for name, digest in manifest["sha256"].items():
        assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    assert manifest["sha256"]["note.txt"] == hashlib.sha256(note.encode()).hexdigest()
    loaded = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert loaded["sha256"] == manifest["sha256"]
    assert loaded["run_id"] == "r1"
    assert loaded["rules"]["levels"] == "C0-C4 v1"
    assert loaded["counts"]["marina"] == {"C1": 1, "C0": 1}
    assert loaded["entities"] == 2
    assert loaded["kind"] == "test"
    assert "nvapi-secret-value" not in json.dumps(loaded)
    back = from_geojson(dest / "entities.geojson")
    assert back[0]["name"] == "Pointe-à-Pitre"
    assert back[0]["lat"] == 16.24 and back[0]["lon"] == -61.53
    assert back[1]["lat"] is None and back[1]["lon"] is None

    with pytest.raises(FileExistsError, match="immutable"):
        write_run(runs, "r1", {"note.txt": "changed"})
    assert (dest / "note.txt").read_text(encoding="utf-8") == note

    (runs / "empty").mkdir()
    with pytest.raises(FileExistsError, match="immutable"):
        write_run(runs, "empty", {"a.txt": "x"})
    with pytest.raises(ValueError):
        write_run(runs, "../x", {"a.txt": "x"})
    with pytest.raises(ValueError):
        write_run(runs, "r9", {"manifest.json": "{}"})
