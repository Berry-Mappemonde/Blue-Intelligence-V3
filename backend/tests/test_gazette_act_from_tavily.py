"""Cape Verde's posts come from DL 13/2025, whatever the reader makes of a tariff (offline).

Run 20261105-183713-b60354 found the act's stub, read it through Tavily, whose text has no link,
and never reached the act. Six ports then hung on a port-dues line of enapor.cv, which the reader
gave 7 ports for once and nothing for the next five times. Replayed here with the run's conditions:
the same queue head, Tavily's form of the stub, and the act's own text.
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.adapters import evidence_store
from app.agents import poe_judge
from app.services import border_crossing, gold_score
from app.services import poe_pipeline as poe
from app.services import v3_certify as vc

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VIEW = "https://boe.incv.cv/Bulletins/View/85465"
ACT = "https://boe.incv.cv/Bulletins/DownloadAct?id=85465"
ENAPOR = "https://enapor.cv/web/content/5154"
VIEW_TEXT = (FIXTURES / "incv_view_85465_text.txt").read_text(encoding="utf-8")
VIEW_HTML = (FIXTURES / "incv_view_85465_anchors.html").read_text(encoding="utf-8")
ACT_TEXT = (FIXTURES / "incv_act_85465_extract_cascade.txt").read_text(encoding="utf-8")
TARIFF = ("Type II and III ports - ports of Palmeira, Sal-Rei, Porto Novo, Tarrafal, Vale de "
          "Cavaleiros, Furna, and Porto Inglês - 5%.")
CV = {"name": "Cape Verde", "geoname": "Cape Verde", "mrgid": 8362, "iso2": "CV", "sov_iso2": "CV"}
POSTS = ["Porto da Praia", "Porto Grande de S. Vicente", "Porto de Palmeiras", "Porto de Porto Novo",
         "Porto do Tarrafal", "Porto de Sal Rei", "Porto de Porto Inglês",
         "Porto Vale de Cavaleiros", "Porto de Furna"]


def _collect(*, raw_html=VIEW_HTML, act=ACT_TEXT, queue=None):
    """(calls, kept texts) with the stub as Tavily reads it: its text, and no link."""
    calls: list[tuple[str, bool]] = []

    async def cascade(url, min_chars=200, log=None, keep_raw=False):
        calls.append((url, keep_raw))
        if url == VIEW:
            html = raw_html if keep_raw else "<html><body></body></html>"
            return {"text": VIEW_TEXT, "html": html, "md5": "v", "level": "N0-tavily-extract",
                    "blocked": False}
        if url == ACT:
            return {"text": act, "html": "", "md5": "a", "level": "N1-pymupdf", "blocked": False}
        return {"text": TARIFF * 20, "html": "", "md5": "t", "level": "N1", "blocked": False}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(poe, "extract_cascade", cascade)
        texts = asyncio.run(poe._collect_texts(
            queue or [{"url": VIEW, "domain": "incv.cv"}, {"url": ENAPOR, "domain": "enapor.cv"}],
            lambda m: None, max_fetch=14, zone=CV))[0]
    return calls, texts


def test_the_stub_read_through_tavily_reaches_the_act():
    calls, texts = _collect()
    assert (VIEW, True) in calls and (ACT, False) in calls
    kept = next(t for t in texts if t.startswith(f"[SOURCE: {VIEW}]"))
    assert "Fronteira marítima – Porto de Furna" in kept


def test_it_takes_no_extra_slot():
    calls, texts = _collect()
    assert [u for u, raw in calls if not raw and u != ACT][:2] == [VIEW, ENAPOR]
    assert sum(t.startswith(f"[SOURCE: {VIEW}]") for t in texts) == 1
    assert not [t for t in texts if t.startswith(f"[SOURCE: {ACT}]")]


def test_a_page_with_no_link_leaves_the_stub():
    calls, texts = _collect(raw_html="<html></html>")
    assert ACT not in [u for u, _ in calls]
    assert any(t.startswith(f"[SOURCE: {VIEW}]") for t in texts)


def test_a_link_in_the_first_read_needs_no_second_read():
    calls = []

    async def cascade(url, min_chars=200, log=None, keep_raw=False):
        calls.append((url, keep_raw))
        html = VIEW_HTML if url == VIEW else ""
        return {"text": VIEW_TEXT if url == VIEW else ACT_TEXT, "html": html, "md5": url,
                "level": "N1", "blocked": False}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(poe, "extract_cascade", cascade)
        asyncio.run(poe._collect_texts([{"url": VIEW, "domain": "incv.cv"}], lambda m: None,
                                       max_fetch=14, zone=CV))
    assert (VIEW, True) not in calls and (ACT, False) in calls


def test_the_gazette_has_its_own_query():
    scoped = [q for _, q in poe.entry_list_queries(CV) if q.startswith("site:")]
    assert len(scoped) == 1 and scoped[0].startswith("site:incv.cv postos de fronteira habilitados")


def test_a_zone_with_no_gazette_gets_none():
    assert not [q for _, q in poe.entry_list_queries({"name": "Saint Lucia", "mrgid": 8416,
                                                      "iso2": "LC", "sov_iso2": "LC"})
                if q.startswith("site:")]


def test_the_act_gives_its_nine_posts_with_no_llm():
    assert sorted(r["name"] for r in border_crossing.designated_post_rows(ACT_TEXT)) == sorted(POSTS)


def test_the_posts_enter_the_catalogue_from_the_kept_text():
    _calls, texts = _collect()
    ports, _ = poe.zone_catalog("\n\n".join(texts), CV)
    border = [p["name"] for p in ports if p.get("extraction_engine") == "border_list"]
    assert sorted(border) == sorted(POSTS)


def test_they_are_all_nine_of_the_gold():
    gold = json.loads((Path(gold_score.GOLD_DIR) / "pilot_4zones.json").read_text(encoding="utf-8"))
    zone = next(z for z in gold["zones"] if int(z["mrgid"]) == 8362)
    index = gold_score._GoldIndex(zone, gold_score.load_aliases().get(8362, {}))
    hits = {index.match([n])[0]["name"] for n in POSTS}
    assert hits == {e["name"] for e in zone["poe"]}


def test_a_tariff_gives_no_post():
    assert border_crossing.designated_post_rows(TARIFF) == []
    assert not border_crossing.is_border_crossing_list(TARIFF)


@pytest.mark.parametrize("name", POSTS)
def test_each_post_reaches_the_judge_on_its_own_row(name):
    (record,) = evidence_store.build_records(8362, [name], [{"url": ACT, "text": ACT_TEXT}],
                                             whitelist=["incv.cv"], zone=CV)
    assert record["list_row"] == vc.LIST_ROW_BORDER
    assert record["snippet"].split(" – ")[1].startswith(name)
    assert len(record["snippet"]) > poe_judge.MIN_EXCERPT_CHARS


def test_a_post_reaches_c2_only_through_the_judge():
    port = {"name": "Porto de Furna", "mrgid": 8362, "zone_name": "Cape Verde", "lat": 14.9,
            "lon": -24.7, "validated": True, "spatial_kind": "in_eez", "confidence": 60,
            "extraction_engine": "border_list", "source_urls": [ACT, "https://www.gov.cv/x"]}
    texts = [{"url": ACT, "text": ACT_TEXT}]
    wl = ["incv.cv", "gov.cv"]
    assert vc.level_from_v2(dict(port), whitelist=wl, texts=texts, zone=CV) == "C1"
    row = "m) Fronteira marítima – Porto de Furna, situado na Ilha Brava."
    judged = {**port, "extraction_engine": "nightshift", "judge": {
        "verdict": "poe", "reason": "x", "quoted_excerpt": row, "excerpt_verified": True}}
    assert vc.level_from_v2(judged, whitelist=wl, texts=texts, zone=CV) == "C2"


def test_the_code_names_no_gazette_or_port():
    import inspect
    code = (inspect.getsource(poe._gazette_page_link)
            + inspect.getsource(border_crossing.designated_post_rows))
    for word in ("incv", "85465", "Cabo", "Furna", "Tarrafal"):
        assert word not in code
