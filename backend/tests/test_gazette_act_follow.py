"""A gazette's page for an act can be a stub. Replay on the real page of Cape Verde's
Boletim Oficial: the 1,332-char page of DL 13/2025 holds no port, and its one link leads to
the 151,226-char act with every maritime post. Fixtures: the page's text and anchors, and the
act as extract_cascade reads it (its HTML is 1 MB of inline assets, so only the anchors are
kept). Offline: fetches are stubbed.
"""
import asyncio
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from app.core import extract, run_rules
from app.services import authority_roles, poe_pipeline as poe

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUN = json.loads((FIXTURES / "run_ac33e9_candidates.json").read_text(encoding="utf-8"))["zones"]
CV = {**RUN["8362"]["zone"], "sov_iso2": "CV"}
VIEW = "https://boe.incv.cv/Bulletins/View/85465"
ACT = "https://boe.incv.cv/Bulletins/DownloadAct?id=85465"
VIEW_TEXT = (FIXTURES / "incv_view_85465_text.txt").read_text(encoding="utf-8")
VIEW_HTML = (FIXTURES / "incv_view_85465_anchors.html").read_text(encoding="utf-8")
ACT_TEXT = (FIXTURES / "incv_act_85465_extract_cascade.txt").read_text(encoding="utf-8")
MINDELO = "Porto Grande de S. Vicente, situado na cidade do Mindelo"
OTHER_POSTS = ["Porto da Praia", "Porto de Palmeiras", "Porto de Porto Novo", "Porto do Tarrafal",
               "Porto de Sal Rei", "Porto de Porto Inglês", "Porto Vale de Cavaleiros", "Porto de Furna"]
FILLER = "Informação oficial. " * 40


def _page(url, text, html=""):
    return {"text": text, "html": html, "md5": url, "level": "N1", "blocked": False}


def _collect(queue, pages=None, max_fetch=14, zone=CV):
    """(fetched urls, kept texts, events) with fetches stubbed; ``pages`` overrides them."""
    pages = {VIEW: _page(VIEW, VIEW_TEXT, VIEW_HTML), ACT: _page(ACT, ACT_TEXT), **(pages or {})}
    calls, events = [], []

    async def cascade(url, min_chars=200, log=None):
        calls.append(url)
        page = pages.get(url, _page(url, FILLER))
        if isinstance(page, Exception):
            raise page
        return page

    class Rec:
        async def event(self, step, **payload):
            events.append((step, payload))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(poe, "extract_cascade", cascade)
        texts = asyncio.run(poe._collect_texts(queue, lambda m: None, rec=Rec(), max_fetch=max_fetch,
                                               zone=zone))[0]
    return calls, texts, events


def _queue():
    """The end of the gatekeeper on ac33e9's candidates, with the gazette page found."""
    pinned = [dict(c, official=True) for c in RUN["8362"]["pinned"]]
    found = {"url": VIEW, "domain": poe.domain_of(VIEW), "official": True, "list_discovery": 1}
    official = [found, *(dict(c, official=True) for c in RUN["8362"]["official"])]
    lists = poe.designating_list_candidates(official, CV)
    held = {poe._normalize_url(c["url"]) for c in lists}
    return pinned + lists + poe._best_per_domain(
        [c for c in official if poe._normalize_url(c["url"]) not in held])


def test_fixtures_are_the_real_page_and_act():
    assert hashlib.md5(ACT_TEXT.encode("utf-8")).hexdigest() == "ae199865cc8afdd91477db242b07fec6"
    assert len(ACT_TEXT) == 151226
    assert "Mindelo" not in VIEW_TEXT and "Fronteira marítima" not in VIEW_TEXT


def test_the_attachment_rules_alone_never_reach_the_act():
    """Why this rule exists: no ".pdf" in the link, and no follow-up pattern in its path."""
    assert extract.official_attachments(VIEW_HTML, VIEW, limit=4) == []
    assert extract.internal_followups(VIEW_HTML, VIEW, limit=3) == []


def test_the_real_page_has_one_link_to_its_act():
    assert extract.full_text_link(VIEW_HTML, VIEW) == ACT


# --- replay ----------------------------------------------------------------------------

def test_a_run_reaches_the_act_text_and_mindelo():
    calls, texts, _ = _collect(_queue())
    assert VIEW in calls and ACT in calls
    assert MINDELO in next(t for t in texts if t.startswith(f"[SOURCE: {VIEW}]"))


def test_the_reader_gets_mindelo_and_every_post_in_its_batches():
    _calls, texts, _ = _collect(_queue())
    batches = poe.extraction_batches("\n\n".join(texts), 20000, 500)
    assert any(MINDELO in b for b in batches)
    for post in OTHER_POSTS:
        assert any(post in b for b in batches), post


def test_the_act_stays_whole_enough_under_the_source_cap():
    """151,226 chars against the 120,000 cap: the list is in the head (char 39,184)."""
    _calls, texts, _ = _collect(_queue())
    kept = next(t for t in texts if t.startswith(f"[SOURCE: {VIEW}]"))
    assert len(kept) <= poe.SOURCE_MAX_CHARS + 200
    assert ACT_TEXT.index(MINDELO) < poe.SOURCE_MAX_CHARS


@pytest.mark.parametrize("max_fetch", [5, 8, 14])
def test_it_takes_no_extra_slot(max_fetch):
    calls, _t, _e = _collect(_queue(), max_fetch=max_fetch)
    without, _t, _e = _collect(_queue(), pages={VIEW: _page(VIEW, VIEW_TEXT, "")}, max_fetch=max_fetch)
    assert ACT in calls and [u for u in calls if u != ACT] == without


def test_the_read_is_recorded():
    _calls, _texts, events = _collect(_queue())
    (depth2,) = [p for s, p in events if s == "depth2" and p.get("gazette_act")]
    assert depth2["parent"] == VIEW and depth2["url"] == ACT and depth2["chars"] == len(ACT_TEXT)


def test_the_act_found_by_search_too_is_read_once():
    queue = [{"url": VIEW, "domain": "incv.cv"}, {"url": ACT, "domain": "incv.cv"}]
    calls, _texts, _ = _collect(queue)
    assert calls.count(ACT) == 1


@pytest.mark.parametrize("act", [_page(ACT, ""), {**_page(ACT, ACT_TEXT), "blocked": True},
                                 RuntimeError("timeout")])
def test_an_act_that_cannot_be_read_leaves_the_page(act):
    calls, texts, _ = _collect([{"url": VIEW, "domain": "incv.cv"}], pages={ACT: act})
    assert ACT in calls and any(t.startswith(f"[SOURCE: {VIEW}]") for t in texts)
    assert not any(MINDELO in t for t in texts)


# --- the rule --------------------------------------------------------------------------

def test_only_a_gazette_of_the_zone_is_followed():
    url = "https://www.enapor.cv/a"
    html = '<a href="/doc?id=1" title="Descarregar a versão PDF"></a>'
    calls, _t, _e = _collect([{"url": url, "domain": "enapor.cv"}],
                             pages={url: _page(url, "x" * 300, html)})
    assert url in calls and "https://www.enapor.cv/doc?id=1" not in calls
    assert authority_roles.zone_gazettes(CV) == ["incv.cv"]
    assert authority_roles.zone_gazettes({"mrgid": 8416, "iso2": "LC"}) == []


def test_a_long_gazette_page_is_no_stub():
    calls, _t, _e = _collect([{"url": VIEW, "domain": "incv.cv"}],
                             pages={VIEW: _page(VIEW, FILLER * 8, VIEW_HTML)})
    assert VIEW in calls and ACT not in calls


def test_the_rule_can_be_turned_off():
    token = run_rules.bind_rules({"chosen": {"formalities.gazette_stub_chars": {"value": 0}}})
    try:
        calls, _t, _e = _collect([{"url": VIEW, "domain": "incv.cv"}])
    finally:
        run_rules.reset_rules(token)
    assert VIEW in calls and ACT not in calls


@pytest.mark.parametrize("html, link", [
    ('<a href="/a.pdf">Texto</a>', "https://boe.incv.cv/a.pdf"),
    ('<a href="/x?id=1">Descargar</a>', "https://boe.incv.cv/x?id=1"),
    ('<a href="/x?id=1">Télécharger</a><a href="/x?id=1">PDF</a>', "https://boe.incv.cv/x?id=1"),
    ('<a href="/about">Sobre</a>', None),
    ('<a href="https://other.cv/a.pdf">PDF</a>', None),
    ('<a href="/a.pdf">Acto</a><a href="/whole.pdf">Boletim</a>', None),
])
def test_one_link_on_the_host_or_none(html, link):
    assert extract.full_text_link(html, VIEW) == link


def test_the_code_names_no_gazette():
    code = inspect.getsource(extract.full_text_link) + inspect.getsource(authority_roles.zone_gazettes)
    for word in ("incv", "http", "DownloadAct", "Bulletins", "85465"):
        assert word not in code
