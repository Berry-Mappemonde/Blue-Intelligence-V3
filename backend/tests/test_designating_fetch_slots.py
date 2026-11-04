"""Designating lists win a fetch slot. Replay of run 20261104-125841-ac33e9: search returned
Annex 4 and the Orden INT for the Canaries, and ``_best_per_domain`` dropped both before the
fetch queue (tests/fixtures/run_ac33e9_candidates.json, rebuilt from the run's search events).
Offline: fetches are stubbed."""
import asyncio
import inspect
import json
from pathlib import Path

import pytest

from app.core import run_rules
from app.services import poe_pipeline as poe

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "run_ac33e9_candidates.json"
RUN = json.loads(FIXTURE.read_text(encoding="utf-8"))["zones"]
ANNEX_4 = "handbook-annex-04_en.pdf"
ORDEN_INT = "act.php?id=BOE-A-2023-17072"
CANARIES, OTHERS = "8364", ["8362", "8416", "33178"]


def _zone(key):
    return {**RUN[key]["zone"], "sov_iso2": {"8364": "ES", "33178": "FR"}.get(key, RUN[key]["zone"]["iso2"])}


def _official(key):
    return [dict(c, official=True) for c in RUN[key]["official"]]


def _queue(key):
    """The end of the gatekeeper: pinned, then the designating lists, then the generic pages."""
    pinned = [dict(c, official=True) for c in RUN[key]["pinned"]]
    norm = {poe._normalize_url(c["url"]) for c in pinned}
    official = [c for c in _official(key) if poe._normalize_url(c["url"]) not in norm]
    lists = poe.designating_list_candidates(official, _zone(key))
    held = norm | {poe._normalize_url(c["url"]) for c in lists}
    return pinned + lists + poe._best_per_domain(
        [c for c in official if poe._normalize_url(c["url"]) not in held])


def _fetched(key, max_fetch=14):
    calls = []

    async def cascade(url, min_chars=200, log=None):
        calls.append(url)
        return {"text": "Información oficial. " * 40, "html": "", "md5": url, "level": "N1",
                "blocked": False}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(poe, "extract_cascade", cascade)
        asyncio.run(poe._collect_texts(_queue(key), lambda m: None, max_fetch=max_fetch,
                                       zone=_zone(key)))
    return calls


def _has(urls, part):
    return any(part in u for u in urls)


def test_the_loss_in_ac33e9_was_the_per_domain_pruning():
    kept = [c["url"] for c in poe._best_per_domain(_official(CANARIES))]
    official = [c["url"] for c in _official(CANARIES)]
    for part in (ANNEX_4, ORDEN_INT):
        assert _has(official, part) and not _has(kept, part)


def test_both_spanish_lists_are_fetched_inside_max_fetch_14():
    calls = _fetched(CANARIES)
    assert len(calls) <= 14
    assert _has(calls, ANNEX_4) and _has(calls, ORDEN_INT)


def test_they_are_fetched_ahead_of_the_generic_pages():
    urls = [c["url"] for c in _queue(CANARIES)]
    generic = next(i for i, u in enumerate(urls) if "sanidad.gob.es" in u)
    assert max(i for i, u in enumerate(urls) if ANNEX_4 in u or ORDEN_INT in u) < generic


def test_the_authority_roles_keep_their_slots():
    calls = _fetched(CANARIES)
    assert _has(calls, "salvamentomaritimo.es") and _has(calls, "policia.es")
    assert any(h in u for u in calls for h in ("puertos.es", "palmasport.es", "puertoscanarios.es",
                                               "puertosdetenerife.org"))


@pytest.mark.parametrize("key", OTHERS)
def test_no_other_zone_of_the_run_has_a_list_crowded_out(key):
    pinned = {poe._normalize_url(c["url"]) for c in RUN[key]["pinned"]}
    assert not poe.designating_list_candidates(
        [c for c in _official(key) if poe._normalize_url(c["url"]) not in pinned], _zone(key))


@pytest.mark.parametrize("key", OTHERS)
def test_no_other_zones_queue_changes(key):
    pinned = [dict(c) for c in RUN[key]["pinned"]]
    norm = {poe._normalize_url(c["url"]) for c in pinned}
    before = pinned + poe._best_per_domain(
        [c for c in _official(key) if poe._normalize_url(c["url"]) not in norm])
    assert [c["url"] for c in _queue(key)] == [c["url"] for c in before]
    assert [c["domain"] for c in before] == RUN[key]["gatekeeper_domains"][:len(before)]


# --- the rule -------------------------------------------------------------------------

CANARY_ZONE = _zone(CANARIES)
PUBLISHER = "https://www.boe.es/buscar/act.php?id=X"


def _candidate(url, rank=None):
    c = {"url": url, "domain": poe.domain_of(url)}
    return dict(c, list_discovery=rank) if rank else c


@pytest.mark.parametrize("url", [
    "https://www.douane.gouv.fr/files/liste-points-clearance-agrees-mise-a-jour.pdf",
    "https://aai.gov.cv/cat-geral/56-os-postos-habilitados-de-fronteiras-phf-de-cabo-verde-sao",
    "https://example.gov/ports-of-entry-regulations",
    "https://example.gob.es/puestos-fronterizos.pdf",
    "https://example.gob.es/Puertos%20habilitados%20lista.pdf",
])
def test_a_url_that_says_it_is_a_designating_list(url):
    assert poe.designating_list_candidates([_candidate(url)], {"name": "X"})


@pytest.mark.parametrize("url", [
    "https://www.mf.gov.cv/documents/198414/4674538/Decreto_Lei+de+Execucao+do+OE+2025.pdf",
    "https://www.sanidad.gob.es/areas/sanidadExterior/puntosFronterizos/docs/Rah_01042021.pdf",
    "https://www.agenciatributaria.es/",
    "https://www.governo.cv/documentos/b-o-estatuto-dos-inspetores-das-pescas/",
])
def test_a_generic_page_is_not(url):
    assert not poe.designating_list_candidates([_candidate(url)], {"name": "X"})


def test_discovery_counts_only_on_a_publisher_of_the_list():
    found = poe.designating_list_candidates(
        [_candidate("https://www.sanidad.gob.es/x.pdf", 1), _candidate(PUBLISHER, 2)], CANARY_ZONE)
    assert [c["url"] for c in found] == [PUBLISHER]


def test_the_schengen_publishers_are_not_those_of_a_zone_outside_schengen():
    assert not poe.designating_list_candidates(
        [_candidate(PUBLISHER, 1)], {"mrgid": 8416, "name": "Saint Lucia", "iso2": "LC"})


def test_slots_are_capped_per_publisher_and_in_all():
    boe = [_candidate(f"https://www.boe.es/a?id={i}", i) for i in range(1, 6)]
    europa = [_candidate(f"https://home-affairs.ec.europa.eu/b{i}", i) for i in range(1, 6)]
    found = poe.designating_list_candidates(boe + europa, CANARY_ZONE)
    assert len(found) == 6 and sum("boe.es" in c["url"] for c in found) == 3
    assert len(poe.designating_list_candidates(boe + europa, CANARY_ZONE, max_slots=2)) == 2


def test_the_best_discovery_rank_goes_first():
    found = poe.designating_list_candidates(
        [_candidate("https://www.boe.es/late", 6), _candidate("https://www.boe.es/early", 1)],
        CANARY_ZONE)
    assert [c["url"] for c in found] == ["https://www.boe.es/early", "https://www.boe.es/late"]


def test_the_tag_survives_a_url_already_found_by_another_query():
    merged = poe._merge_candidates([_candidate(PUBLISHER)], [_candidate(PUBLISHER, 6)])
    assert merged[0]["list_discovery"] == 6


def test_the_rule_reads_its_slots():
    token = run_rules.bind_rules({"chosen": {"formalities.designating_fetch_slots": {"value": 1}}})
    try:
        boe = [_candidate(f"https://www.boe.es/a?id={i}", i) for i in range(1, 4)]
        assert len(poe.designating_list_candidates(boe, CANARY_ZONE)) == 1
    finally:
        run_rules.reset_rules(token)
    assert run_rules.catalog_default("formalities.designating_fetch_slots") \
        == poe.DESIGNATING_FETCH_SLOTS == 6


def test_no_url_or_port_is_hardcoded():
    source = inspect.getsource(poe.designating_list_candidates)
    code = source.split('"""')[-1]
    assert not any(word in code for word in ("boe.es", "europa.eu", "Arrecife", "Tenerife"))
