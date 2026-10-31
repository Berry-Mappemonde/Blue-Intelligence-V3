"""Échelle C0–C4 et citation vérifiée (bibliothèque, sans système de run propre).

Aucun réseau : pas d'appel HTTP.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.run_pipeline import _port_out  # noqa: E402
from app.services.poe_confidence import apply_score, score_port  # noqa: E402
from app.services.poe_pipeline import ports_to_geojson  # noqa: E402
from app.services.v3_certify import (  # noqa: E402
    LEVELS,
    assess_level,
    assess_port,
    attach_confidence_level,
    confidence_level,
    is_official,
    level,
    level_from_v2,
    verified_quote,
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


def test_apply_score_leaves_the_level_to_its_callers():
    """poe_confidence (V2 score) is untouched: callers attach the C-level."""
    import inspect

    from app.services import poe_confidence, poe_pipeline

    assert "v3_certify" not in inspect.getsource(poe_confidence)
    port = {
        "name": "Papeete",
        "extraction_engine": "catalog",
        "note": "catalogue officiel",
        "source_urls": ["https://douane.gouv.fr/x"],
    }
    scored = score_port(port, official_source=True)
    out = apply_score(port, scored)
    assert out is port
    assert "confidence_level" not in port
    assert port["confidence"] == scored["confidence"]

    before = dict(port)
    attach_confidence_level(port, official_source=True)
    assert port["confidence_level"] in LEVELS
    assert {k: port[k] for k in before} == before

    src = inspect.getsource(poe_pipeline)
    assert src.count("attach_confidence_level(") >= 2


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


def test_levels_work_without_env():
    assert level_from_v2({"name": "X"}) == "C0"


def test_unified_run_is_the_only_run_mechanism():
    """No file-run store beside unified_runs / run_artifacts (PR #14)."""
    import importlib.util

    from app.services import run_artifacts

    assert importlib.util.find_spec("app.services.v3_run_manifest") is None
    assert run_artifacts.LEVEL_FIELD == "confidence_level"
    assert run_artifacts.SCORE_FIELD == "confidence"
