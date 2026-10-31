"""Échelle C0–C4 et citation vérifiée (bibliothèque, sans système de run propre).

Aucun réseau : pas d'appel HTTP.
"""
from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.poe_confidence import apply_score, score_port  # noqa: E402
from app.services.poe_pipeline import ports_to_geojson  # noqa: E402
from app.services import v3_certify  # noqa: E402
from app.services.v3_certify import (  # noqa: E402
    LEVELS,
    is_official,
    verified_quote,
    with_official,
)

GOV = "https://www.martinique.gouv.fr/x"
# State whitelist the pipeline builds for a French zone (build_whitelist).
WL = ["gouv.fr"]


# Every ladder entry point below uses WL unless a test passes its own.
def _wl(fn):
    def call(*args, **kwargs):
        kwargs.setdefault("whitelist", WL)
        return fn(*args, **kwargs)
    return call


assess_level = _wl(v3_certify.assess_level)
assess_port = _wl(v3_certify.assess_port)
attach_confidence_level = _wl(v3_certify.attach_confidence_level)
confidence_level = _wl(v3_certify.confidence_level)
level_from_v2 = _wl(v3_certify.level_from_v2)


def level(entity):
    """Ladder on evidence resolved against WL."""
    return v3_certify.level(with_official(entity, WL))


# PLAN-V3 §5 C3 geo: two geocoders < 500 m, at sea or at the quay.
GEO_OK = {"geocode_agree": True, "geocode_agreement_km": 0.3, "spatial_kind": "in_eez"}
GEO_OK_ENTITY = {"agree": True, "independent_sources": 2, "agreement_km": 0.3,
                 "spatial_kind": "in_eez"}


EXCERPT = "Fort-de-France is designated as a port of entry for yachts"
EVIDENCE_TEXT = [{"url": GOV, "text": "Arrêté: " + EXCERPT + " arriving from abroad."}]


def _judged(verdict, excerpt=EXCERPT):
    """port.judge exactly as agents.poe_judge.parse_judgments builds it."""
    from app.agents import poe_judge

    parsed = {"ports": [{"name": "Marina du Marin", "verdict": verdict,
                         "reason": "listed in the decree", "quoted_excerpt": excerpt}]}
    return next(iter(poe_judge.parse_judgments(parsed, EVIDENCE_TEXT).values()))


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
    assert is_official("https://navily.com/x", WL) is False

    official = {
        "name": "Marina du Marin",
        "evidence": [{"url": GOV, "extracted": True, "judge": "supported"}],
    }
    assert level(official) == "C2"
    official["geo"] = dict(GEO_OK_ENTITY)
    assert level(official) == "C3"
    official["human"] = "confirmed"
    assert level(official) == "C4"
    assert confidence_level(official) == "C4"
    assert level({}) == "C0"

    # The facility's own domain is not a State source (name-token rule removed).
    own_domain = {
        "name": "Le Marin",
        "evidence": [{"url": "https://lemarin.fr/a", "extracted": True}],
    }
    assert level(own_domain) == "C0"
    # A caller-supplied official flag is not trusted: only the whitelist counts.
    flagged = {
        "name": "Le Marin",
        "evidence": [{"url": "https://lemarin.fr/a", "extracted": True, "official": True}],
    }
    assert confidence_level(flagged) == "C0"


def test_llm_only_never_above_c1_even_with_geo_and_human():
    port = _v2(
        claude_agreement=True,
        **GEO_OK,
        human="confirmed",
        confidence_reasons=["source d'État", "deux lecteurs LLM"],
    )
    assert level_from_v2(port) == "C1"
    judged = _v2(judge=_judged("poe"), **GEO_OK, human="confirmed")
    assert level_from_v2(judged) == "C4"


def test_v2_official_from_whitelist_and_catalogue_ladder():
    assert level_from_v2(_v2(source_urls=["https://navily.com/x"])) == "C0"
    # The V2 reason "source d'État" does not make a non-whitelisted host official.
    assert level_from_v2(_v2(
        source_urls=["https://navily.com/x"],
        confidence_reasons=["source d'État"],
    )) == "C0"
    assert level_from_v2(_v2(), whitelist=[]) == "C0"
    assert level_from_v2(_v2(), whitelist=None) == "C0"
    assert level_from_v2(_v2()) == "C1"

    catalogue = _v2(
        extraction_engine="catalog",
        extraction_agreement=True,
        confidence_reasons=["source d'État", "catalogue officiel"],
        **GEO_OK,
    )
    assert level_from_v2(catalogue) == "C3"
    assert level_from_v2({**catalogue, "human": "confirmed"}) == "C4"
    assert level_from_v2({**catalogue, "geocode_agree": False}) == "C2"
    assert level_from_v2(_v2(from_synthesis=True, extraction_engine="catalog",
                             **GEO_OK, human="confirmed")) == "C0"


def test_judge_outcome_reads_the_real_poe_judge_shape():
    poe = _judged("poe")
    assert set(poe) == {"verdict", "reason", "quoted_excerpt", "excerpt_verified"}
    assert poe["excerpt_verified"] is True
    assert v3_certify.judge_outcome({"judge": poe}) == "supported"
    assert v3_certify.judge_outcome({"judge": _judged("not_poe")}) == "contradicted"
    assert v3_certify.judge_outcome({"judge": _judged("unclear")}) is None
    unverified = _judged("poe", excerpt="Fort-de-France is a nice place to anchor at night")
    assert unverified["excerpt_verified"] is False
    assert v3_certify.judge_outcome({"judge": unverified}) is None
    # The prototype string verdict is not what the judge emits.
    assert v3_certify.judge_outcome({"judge": "supported"}) is None
    assert v3_certify.judge_outcome({"judge": {"verdict": "supported",
                                               "quoted_excerpt": EXCERPT,
                                               "excerpt_verified": True}}) is None


def test_judge_poe_with_verified_excerpt_lifts_to_c2():
    base = _v2()
    assert level_from_v2(base) == "C1"
    assert level_from_v2({**base, "judge": _judged("poe")}) == "C2"
    assert level_from_v2({**base, "judge": _judged("unclear")}) == "C1"
    unverified = _judged("poe", excerpt="Fort-de-France is a nice place to anchor at night")
    assert level_from_v2({**base, "judge": unverified}) == "C1"
    # A verified judge does not lift a non-official point.
    off = _v2(source_urls=["https://navily.com/x"], judge=_judged("poe"))
    assert level_from_v2(off) == "C0"


def test_judge_not_poe_caps_at_c1():
    catalogue = _v2(extraction_engine="catalog", note="catalogue officiel", **GEO_OK)
    assert level_from_v2(catalogue) == "C3"
    contradicted = {**catalogue, "judge": _judged("not_poe"), "human": "confirmed"}
    assert level_from_v2(contradicted) == "C1"
    out = assess_port(contradicted)
    assert out["downgraded_from"] == "C4"
    assert out["judge_contradicted"] is True
    port = dict(contradicted)
    attach_confidence_level(port)
    assert port["confidence_level"] == "C1"
    assert port["confidence_downgraded_from"] == "C4"
    assert port["judge_contradicted"] is True
    # Never lifts: a C0 point stays C0.
    assert level_from_v2(_v2(source_urls=["https://navily.com/x"],
                             judge=_judged("not_poe"))) == "C0"
    # Prototype entity shape carrying the judge dict is capped the same way.
    entity = {
        "name": "Marina du Marin",
        "evidence": [{"url": GOV, "extracted": True, "judge": "supported"}],
        "geo": dict(GEO_OK_ENTITY),
        "judge": _judged("not_poe"),
    }
    assert confidence_level(entity) == "C3"  # the bare ladder ignores port.judge
    assert assess_port(entity)["confidence_level"] == "C1"


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
        "geo": dict(GEO_OK_ENTITY),
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
        **GEO_OK,
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
        **GEO_OK,
        judge=_judged("poe", excerpt="this sentence is not on the page at all"),
    )
    assert port["judge"]["excerpt_verified"] is False
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
    attach_confidence_level(port)
    assert port["confidence_level"] in LEVELS
    assert {k: port[k] for k in before} == before

    src = inspect.getsource(poe_pipeline)
    assert src.count("attach_confidence_level(") >= 2


def test_level_never_derived_from_the_v2_score():
    port = _v2(extraction_engine="catalog", lat=14.5, lon=-61.0, **GEO_OK)
    expected = level_from_v2(port)
    assert expected == "C3"
    for score in ({"confidence": 0, "confidence_parts": {}, "confidence_reasons": []},
                  {"confidence": 100, "confidence_parts": {"source": 30},
                   "confidence_reasons": ["source d'État", "catalogue + LLM + NER"]}):
        assert level_from_v2({**port, **score}) == expected
    # V2 reasons alone do not lift: an LLM-only reading stays C1.
    llm = _v2(confidence=99, confidence_reasons=["catalogue officiel", "LLM et NER d'accord"],
              **GEO_OK)
    assert level_from_v2(llm) == "C1"
    # Attaching the level never touches the V2 score fields.
    scored = {**port, "confidence": 42, "confidence_parts": {"source": 30},
              "confidence_reasons": ["x"]}
    attach_confidence_level(scored)
    assert (scored["confidence"], scored["confidence_parts"], scored["confidence_reasons"]) == (
        42, {"source": 30}, ["x"])
    # And the V2 score does not read the level.
    a = score_port({**port, "confidence_level": "C0"}, official_source=True)
    b = score_port({**port, "confidence_level": "C4"}, official_source=True)
    assert a == b


def test_geojson_copies_the_stored_level_and_never_recomputes():
    doc = {"_id": "p1", "name": "Papeete", "lat": -17.5, "lon": -149.5,
           "source_urls": [GOV], "extraction_engine": "catalog", "confidence": 80, **GEO_OK}
    props = ports_to_geojson([doc])["features"][0]["properties"]
    assert props["confidence_level"] is None
    assert props["confidence"] == 80
    stored = {**doc, "_id": "p2", "confidence_level": "C1"}
    assert ports_to_geojson([stored])["features"][0]["properties"]["confidence_level"] == "C1"
    bad = {**doc, "_id": "p3", "confidence_level": "C9"}
    assert ports_to_geojson([bad])["features"][0]["properties"]["confidence_level"] is None


def test_run_pipeline_output_has_no_level():
    from app.pipeline.run_pipeline import _port_out

    out = _port_out({"name": "Papeete", "verdict": "pass", "confidence": 70,
                     "source_urls": [GOV], "extraction_engine": "catalog"})
    assert "confidence_level" not in out
    assert out["confidence"] == 70


def _geo_zone_fakes(monkeypatch):
    from app.services import poe_pipeline as poe

    async def fake_extract(context, zone, log, rec=None, catalog_text=None, settings=None):
        return [{"name": "Fort-de-France", "lat": 14.6, "lon": -61.07,
                 "extraction_engine": "catalog"}]

    async def no_dual(*a, **k):
        raise AssertionError("no geocoding: coordinates come from the source")

    monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
    monkeypatch.setattr(poe, "geocode_port_dual", no_dual)
    monkeypatch.setattr(poe, "_hmem_recall", lambda docs, run_id: [[] for _ in docs])
    monkeypatch.setattr(poe, "_hmem_remember", lambda docs, run_id: {})
    zone = {"mrgid": 1, "name": "Martinique", "iso2": "MQ", "sov_iso2": "FR",
            "geometry": None}
    sources = [{"url": GOV, "official": True}]
    return poe, zone, sources


def test_level_is_attached_in_run_mode_only(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    poe, zone, sources = _geo_zone_fakes(monkeypatch)
    live = asyncio.run(poe._extract_and_geocode(zone, "ctx", sources, lambda m: None,
                                                whitelist=WL))
    assert live and all("confidence_level" not in d for d in live)
    assert all(isinstance(d.get("confidence"), int) for d in live)

    run = SimpleNamespace(run_id="run-test", variant="tinyfish")
    in_run = asyncio.run(poe._extract_and_geocode(zone, "ctx", sources, lambda m: None,
                                                  run=run, whitelist=WL))
    assert in_run and all(d["confidence_level"] in LEVELS for d in in_run)
    assert [d["confidence"] for d in in_run] == [d["confidence"] for d in live]


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, n):
        return list(self.rows)


class _Coll:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.inserted, self.updates = [], []

    def find(self, q):
        return _Cursor([r for r in self.rows if all(r.get(k) == v for k, v in q.items())])

    async def insert_one(self, doc):
        self.inserted.append(dict(doc))

    async def update_one(self, q, upd, upsert=False):
        self.updates.append((q, upd))

    async def count_documents(self, q):
        return len(self.rows) + len(self.inserted)

    async def find_one(self, q):
        return {"mrgid": q.get("mrgid"), "status": "ia"}


def test_persist_zone_never_writes_the_level_to_poe_ports():
    import asyncio
    from types import SimpleNamespace

    from app.services import poe_pipeline as poe

    existing = {"_id": "e1", "mrgid": 1, "name": "Fort-de-France", "dedup_key": "1:fortdefrance",
                "lat": 14.6, "lon": -61.07, "osm_confidence": 0.9, "source_urls": [GOV]}
    level_fields = {"confidence_level": "C3", "confidence_downgraded_from": "C4",
                    "quote_verified": True, "judge_contradicted": True}
    merged = {"mrgid": 1, "name": "Fort-de-France", "dedup_key": "1:fortdefrance",
              "lat": 14.6, "lon": -61.07, "source_urls": [GOV], "confidence": 50,
              "extracted_at": "now", "extraction_engine": "catalog", **level_fields}
    fresh = {"mrgid": 1, "name": "Le Marin", "dedup_key": "1:lemarin", "lat": 14.47,
             "lon": -60.87, "source_urls": [GOV], "confidence": 40, "extracted_at": "now",
             **level_fields}
    db = SimpleNamespace(poe_ports=_Coll([existing]), eez_zones=_Coll())
    asyncio.run(poe._persist_zone(db, {"mrgid": 1}, [merged, fresh], "ia",
                                  [{"url": GOV, "official": True}], {}, {}, lambda m: None))
    assert db.poe_ports.inserted and db.poe_ports.updates
    for doc in db.poe_ports.inserted:
        assert not set(level_fields) & set(doc)
    for _, upd in db.poe_ports.updates:
        assert not set(level_fields) & set(upd.get("$set", {}))
        assert "confidence" in upd["$set"]


def test_levels_work_without_env():
    assert level_from_v2({"name": "X"}) == "C0"


def test_unified_run_is_the_only_run_mechanism():
    """No file-run store beside unified_runs / run_artifacts (PR #14)."""
    import importlib.util

    from app.services import run_artifacts

    assert importlib.util.find_spec("app.services.v3_run_manifest") is None
    assert run_artifacts.LEVEL_FIELD == "confidence_level"
    assert run_artifacts.SCORE_FIELD == "confidence"


@pytest.mark.parametrize("geo, expected", [
    ({"geocode_agree": True, "geocode_agreement_km": 0.5, "spatial_kind": "in_eez"}, "C3"),
    ({"geocode_agree": True, "geocode_agreement_km": 0.2, "spatial_kind": "coastal_land"}, "C3"),
    # Agreeing under the pipeline's 2 km flag, but not within 500 m.
    ({"geocode_agree": True, "geocode_agreement_km": 1.8, "spatial_kind": "in_eez"}, "C2"),
    ({"geocode_agree": True, "geocode_agreement_km": None, "spatial_kind": "in_eez"}, "C2"),
    ({"geocode_agree": True, "spatial_kind": "in_eez"}, "C2"),
    # Within 500 m but off the land mask.
    ({"geocode_agree": True, "geocode_agreement_km": 0.1, "spatial_kind": "out_of_eez"}, "C2"),
    ({"geocode_agree": True, "geocode_agreement_km": 0.1, "spatial_kind": "inland_river"}, "C2"),
    ({"geocode_agree": True, "geocode_agreement_km": 0.1, "spatial_kind": None}, "C2"),
    ({"geocode_agree": False, "geocode_agreement_km": 0.1, "spatial_kind": "in_eez"}, "C2"),
])
def test_c3_needs_500_m_agreement_and_land_mask(geo, expected):
    port = _v2(extraction_engine="catalog", note="catalogue officiel", lat=14.5, lon=-61.0, **geo)
    assert level_from_v2(port) == expected
    entity = {
        "name": "Marina du Marin",
        "evidence": [{"url": GOV, "extracted": True, "judge": "supported"}],
        "geo": {"agree": geo["geocode_agree"], "independent_sources": 2,
                "agreement_km": geo.get("geocode_agreement_km"),
                "spatial_kind": geo["spatial_kind"]},
    }
    assert level(entity) == expected


@pytest.mark.parametrize("url, expected", [
    ("https://www.martinique.gouv.fr/x", True),
    ("https://douane.gouv.fr/guide?a=1", True),
    ("http://gouv.fr", True),
    # Keywords in the path or query, or lookalike hosts, are not official.
    ("https://blog.example.com/customs-guide", False),
    ("https://example.com/x?ref=douane.gouv.fr", False),
    ("https://example.com/gouv.fr/page", False),
    ("https://governor.com/x", False),
    ("https://notgouv.fr/x", False),
    ("https://gouv.fr.evil.com/x", False),
    ("https://saint-pierre-hotel.com/", False),
    ("https://www.mairie-lemarin.fr/", False),
    ("https://cacem.fr/", False),
    ("https://navily.com/x", False),
    ("", False),
    (None, False),
])
def test_official_is_a_host_match_on_the_whitelist(url, expected):
    assert is_official(url, WL) is expected


def test_official_uses_the_pipeline_whitelist():
    from app.services.poe_pipeline import build_whitelist

    wl = build_whitelist("FR", None, {"manual": {"FR": ["port-autonome.example"]}})
    assert "gouv.fr" in wl
    assert is_official("https://www.douane.gouv.fr/x", wl)
    assert is_official("https://port-autonome.example/a", wl)
    assert not is_official("https://saint-pierre-hotel.com/", wl)
    port = {"name": "Saint-Pierre", "source_urls": ["https://saint-pierre-hotel.com/"],
            "extraction_engine": "catalog"}
    assert v3_certify.level_from_v2(port, whitelist=wl) == "C0"
