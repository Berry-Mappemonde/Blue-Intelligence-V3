"""A source over formalities.source_max_chars keeps its head plus windows around entry
terms past it, instead of the head alone (offline, synthetic text, no Mongo).

Saint Lucia lesson: the Customs Control and Management Act PDF is 565,541 chars; reg.
136(2)(h) naming Castries, Vieux-Fort, Rodney Bay Marina and Marigot Bay Marina as
"boarding stations ... for boarding and clearance of vessels" sits at char 346,978, and
the 120k head cut dropped it.
"""
import asyncio

import pytest

from app.core.run_rules import bind_rules, catalog_default, reset_rules, snapshot_for_run
from app.services import poe_pipeline as poe

PROSE = "General provisions on customs warehousing, excise and the duties of officers. "
REG_136 = ("136. (2) The Comptroller may by order ... (h) the following are boarding "
           "stations for the purposes of boarding and clearance of vessels: Castries, "
           "Vieux-Fort, Rodney Bay Marina, Marigot Bay Marina.")
PORTS = ("Castries", "Vieux-Fort", "Rodney Bay Marina", "Marigot Bay Marina")
URL = "https://www.govt.lc/media.govt.lc/www/resources/legislation/CustomsAct.pdf"


def _prose(n: int) -> str:
    return (PROSE * (n // len(PROSE) + 1))[:n]


def _act() -> str:
    """565,541 chars, reg. 136(2)(h) at char 346,978."""
    body = _prose(346978) + REG_136
    return body + _prose(565541 - len(body))


SAINT_LUCIA = {"mrgid": 8416, "name": "Saint Lucia", "geoname": "Saint Lucia", "iso2": "LC",
               "sov_iso2": "LC", "sovereign": "Saint Lucia"}
ROUTINE = ("The proper officer may refuse clearance to any ship until the duties and "
           "charges payable in respect of it have been paid. ")


def _realistic_act() -> str:
    """659k chars of a customs act: a routine "clearance" sentence every ~4,000 chars,
    reg. 136's list at char 346,188."""
    filler = "".join(_prose(4000 - len(ROUTINE)) + ROUTINE for _ in range(170))
    body = filler[:346188] + REG_136
    return body + filler[len(body):][:659000 - len(body)]


def _collect(monkeypatch, body, overrides=None, zone=None):
    async def fake_cascade(u, min_chars=200, log=None):
        return {"text": body, "html": "", "md5": "m", "level": "N1-pymupdf", "blocked": False}

    monkeypatch.setattr(poe, "extract_cascade", fake_cascade)
    logs: list[str] = []
    token = bind_rules(snapshot_for_run(mode="formalities", overrides=overrides or {}))
    try:
        texts, _h, _used, _ex = asyncio.run(poe._collect_texts(
            [{"url": URL, "domain": "govt.lc"}], logs.append, zone=zone))
    finally:
        reset_rules(token)
    return texts, logs


def test_rule_default_matches_the_code():
    assert catalog_default("formalities.source_window_chars") == poe.SOURCE_WINDOW_CHARS


def test_saint_lucia_boarding_stations_past_the_cap_are_kept(monkeypatch):
    act = _act()
    assert len(act) == 565541 and act.index("boarding stations") > 346978
    texts, logs = _collect(monkeypatch, act)
    kept = texts[0].split("\n", 1)[1]
    assert len(kept) <= poe.SOURCE_MAX_CHARS
    for port in PORTS:
        assert port in kept
    assert kept.startswith(act[:poe.SOURCE_MAX_CHARS // 2])  # the head is still there
    assert "caractères omis" in kept
    assert any("tête + 1 fenêtre(s)" in m for m in logs)


def test_windows_are_merged_and_in_document_order():
    text = (_prose(200000) + " the designated port of entry is Port Alpha. "
            + _prose(400) + " clearance at Port Bravo. "
            + _prose(100000) + " ports of entry: Port Charlie. " + _prose(50000))
    out, n = poe.cap_source_text(text, 120000, 1500)
    assert len(out) <= 120000
    assert n == 2  # Alpha and Bravo sit 400 chars apart: one merged window
    assert out.index("Port Alpha") < out.index("Port Bravo") < out.index("Port Charlie")
    assert out.count("caractères omis") == 2


def test_realistic_act_keeps_reg_136_over_routine_clearance_sentences(monkeypatch):
    """Review of #40: with "clearance" every ~4k chars, taking windows nearest the head
    filled the budget with routine sentences and dropped Rodney Bay."""
    act = _realistic_act()
    assert len(act) == 659000 and act.index(REG_136) == 346188
    assert act.count("refuse clearance") > 100
    texts, logs = _collect(monkeypatch, act, zone=SAINT_LUCIA)
    kept = texts[0].split("\n", 1)[1]
    assert len(kept) <= poe.SOURCE_MAX_CHARS
    for port in ("Castries", "Vieux-Fort", "Rodney Bay", "Marigot"):
        assert port in kept
    assert any("fenêtre(s)" in m for m in logs)


def test_list_window_outscores_a_routine_clearance_window():
    places = poe.zone_place_names(SAINT_LUCIA)
    assert {"castries", "vieux fort"} <= set(places)
    routine = poe._window_score(_prose(1500) + ROUTINE + _prose(1500), places)
    listing = poe._window_score(_prose(1500) + REG_136 + _prose(1500), places)
    assert routine <= poe.WINDOW_SCORE["clearance"]
    assert listing > 10 * routine
    # list structure counts even without the zone's names
    assert poe._window_score(REG_136, []) > routine


def test_equal_scores_keep_the_windows_nearest_the_head():
    terms = "".join(_prose(20000) + f" port of entry Port N{i}. " for i in range(40))
    text = _prose(130000) + terms + _prose(5000)  # every window the same size
    out, n = poe.cap_source_text(text, 120000, 1500)
    assert len(out) <= 120000
    assert 0 < n < 40
    assert "Port N0." in out and "Port N39." not in out
    assert out.startswith(text[:60000])  # the head keeps at least half the cap


def test_no_term_past_the_head_is_the_old_head_cut():
    text = _prose(200000)
    assert poe.cap_source_text(text, 120000, 1500) == (text[:120000], 0)


def test_source_within_the_cap_is_untouched():
    text = _prose(1000) + " port of entry "
    assert poe.cap_source_text(text, 120000, 1500) == (text, 0)


@pytest.mark.parametrize("window", [300, 5000])
def test_window_is_a_rule(monkeypatch, window):
    texts, _logs = _collect(monkeypatch, _act(), {"formalities.source_window_chars": window})
    kept = texts[0]
    assert "Rodney Bay Marina" in kept
    i = kept.index(REG_136[:40])
    gap_before = kept.rfind("omis …]", 0, i)
    assert 0 < i - gap_before <= window + len("omis …]\n\n") + 1


def test_kept_window_reaches_one_reader_batch_whole(monkeypatch):
    texts, _logs = _collect(monkeypatch, _act())
    size = catalog_default("formalities.extract_batch_chars")
    overlap = catalog_default("formalities.extract_batch_overlap_chars")
    batches = poe.extraction_batches("\n\n".join(texts), size, overlap)
    assert any(REG_136 in b for b in batches)
