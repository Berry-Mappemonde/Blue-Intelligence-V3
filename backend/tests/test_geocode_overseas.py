"""Overseas polygons geocode with their sovereign's code too ("mq,fr"); the polygon
check stays the only gate. The LLM port geocode runs when Nightshift answers.

Run 20261103-004818-e80c7c: 34/34 Nominatim lookups with countrycodes=mq were empty
(Fort-de-France had no point) and Bayonne, matched in a mer.gouv.fr table of contents,
was kept name-only because its mainland point was never found. No network.
"""
import asyncio

import httpx
import pytest

from app.core import generator, geo, llm
from app.services import poe_pipeline as poe

MQ = {"mrgid": 33178, "name": "Martinique", "iso2": "MQ", "sov_iso2": "FR",
      # Box around Martinique's EEZ: enough for the polygon test.
      "geometry": {"type": "Polygon", "coordinates": [[
          [-62.8, 13.6], [-59.6, 13.6], [-59.6, 16.4], [-62.8, 16.4], [-62.8, 13.6]]]}}
FORT_DE_FRANCE = {"lat": "14.6033", "lon": "-61.0682", "display_name": "Fort-de-France, Martinique, France",
                  "class": "place", "type": "city"}
BAYONNE = {"lat": "43.4933", "lon": "-1.4751", "display_name": "Bayonne, Pyrénées-Atlantiques, France",
           "class": "place", "type": "city"}


def _run(coro):
    return asyncio.run(coro)


def _fake_nominatim(monkeypatch, rows_by_name):
    """Nominatim as OSM files the French DROM: under "fr" only."""
    sent = []

    async def fake_rows(query, country_code=None, limit=1):
        sent.append((query, country_code))
        if "fr" not in (country_code or "").split(","):
            return []
        return [dict(r) for name, r in rows_by_name.items() if name.lower() in query.lower()]

    async def no_geonames(query, country_code=None, limit=1):
        return []

    monkeypatch.setattr(geo, "_nominatim_rows", fake_rows)
    monkeypatch.setattr(geo, "_geonames_rows", no_geonames)
    return sent


@pytest.mark.parametrize("zone, expected", [
    ({"mrgid": 33178, "iso2": "MQ", "sov_iso2": "FR"}, "mq,fr"),
    ({"mrgid": 33177, "iso2": "GP", "sov_iso2": "FR"}, "gp,fr"),
    ({"mrgid": 8338, "iso2": "RE", "sov_iso2": "FR"}, "re,fr"),
    # Zone doc without sov_iso2 (V2 referential): read from territories.json.
    ({"mrgid": 33178, "iso2": "MQ"}, "mq,fr"),
    ({"mrgid": 8416, "iso2": "LC", "sov_iso2": "LC"}, "lc"),
    ({"mrgid": 8362, "iso2": "CV"}, "cv"),
    # Self-governing (never_attach_to_sovereign): own code only.
    ({"mrgid": 21803, "iso2": "SX", "sov_iso2": "NL"}, "sx"),
    ({"mrgid": 26520, "iso2": "CW", "sov_iso2": "NL"}, "cw"),
    ({"mrgid": 1, "iso2": None, "sov_iso2": "FR"}, None),
])
def test_countrycodes_add_the_sovereign(zone, expected):
    assert geo.nominatim_countrycodes(zone) == expected


def test_fort_de_france_sends_mq_fr_and_is_placed(monkeypatch):
    sent = _fake_nominatim(monkeypatch, {"Fort-de-France": FORT_DE_FRANCE})
    dual = _run(geo.geocode_port_dual({"name": "Fort-de-France"}, MQ))
    assert sent and {cc for _, cc in sent} == {"mq,fr"}
    assert dual["nominatim"] == pytest.approx([14.6033, -61.0682])


def _pipeline_fakes(monkeypatch, name):
    async def fake_extract(context, zone, log, rec=None, catalog_text=None, settings=None):
        return [{"name": name, "extraction_engine": "catalog"}]

    async def no_llm_geocode(*a, **k):
        raise AssertionError("Nominatim answered: no LLM geocode")

    monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
    monkeypatch.setattr(poe, "llm_geocode_port", no_llm_geocode)
    monkeypatch.setattr(poe, "_hmem_recall", lambda docs, run_id: [[] for _ in docs])
    monkeypatch.setattr(poe, "_hmem_remember", lambda docs, run_id: {})
    from app.services import listing_ref
    monkeypatch.setattr(listing_ref, "project_listing", lambda: {"ports": []})


def test_bayonne_in_martinique_is_dropped_once_it_geocodes_outside(monkeypatch):
    sent = _fake_nominatim(monkeypatch, {"Bayonne": BAYONNE, "Fort-de-France": FORT_DE_FRANCE})
    _pipeline_fakes(monkeypatch, "Bayonne")
    logs = []
    docs = _run(poe._extract_and_geocode(dict(MQ), "ctx", [{"url": "https://www.mer.gouv.fr/x"}],
                                         logs.append, whitelist=["gouv.fr"]))
    assert sent and all(cc == "mq,fr" for _, cc in sent)
    assert docs == []
    assert any("Bayonne" in m and "hors de ce polygone" in m for m in logs)


def test_fort_de_france_in_martinique_is_kept_with_its_point(monkeypatch):
    _fake_nominatim(monkeypatch, {"Fort-de-France": FORT_DE_FRANCE})
    _pipeline_fakes(monkeypatch, "Fort-de-France")
    docs = _run(poe._extract_and_geocode(dict(MQ), "ctx", [{"url": "https://www.mer.gouv.fr/x"}],
                                         lambda m: None, whitelist=["gouv.fr"]))
    assert [d["name"] for d in docs] == ["Fort-de-France"]
    assert docs[0]["lat"] == pytest.approx(14.6033) and docs[0]["lon"] == pytest.approx(-61.0682)


def test_cache_key_differs_by_countrycodes():
    old = geo.geocode_cache_id("nominatim", "Fort-de-France, Martinique", "mq", 5)
    new = geo.geocode_cache_id("nominatim", "Fort-de-France, Martinique", "mq,fr", 5)
    assert old != new
    assert new == geo.geocode_cache_id("nominatim", "fort-de-france, martinique ", "MQ,FR", 5)


def test_old_mq_miss_is_not_reused(monkeypatch):
    """A miss cached under "mq" is not read for "mq,fr": the HTTP call is made."""
    geo._row_mem.clear()
    store = {}

    class FakeColl:
        async def find_one(self, q):
            return store.get(q["_id"])

        async def update_one(self, q, upd, upsert=False):
            store[q["_id"]] = dict(upd["$set"])

    class FakeDB:
        geocode_cache = FakeColl()

    monkeypatch.setattr(geo, "_geodb", lambda: FakeDB())
    store[geo.geocode_cache_id("nominatim", "Fort-de-France, Martinique", "mq", 1)] = {"rows": []}

    async def no_throttle(*a, **k):
        return None

    monkeypatch.setattr(geo, "_throttle", no_throttle)
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[FORT_DE_FRANCE])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(geo.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    assert _run(geo._nominatim_rows("Fort-de-France, Martinique", "mq", 1)) == []
    assert seen == []  # the old miss still serves "mq"
    rows = _run(geo._nominatim_rows("Fort-de-France, Martinique", "mq,fr", 1))
    assert rows and seen == [{"q": "Fort-de-France, Martinique", "format": "json",
                              "limit": "1", "countrycodes": "mq,fr"}]
    geo._row_mem.clear()


@pytest.fixture
def no_keys(monkeypatch):
    for name in ("NVIDIA_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY",
                 "NEBIUS_API_KEY", "LLM_PROVIDER", "SAMPLE_MODE", "MODEL_BASE_URL", "MODEL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("METERLESS_BASE_URL", generator.DEFAULT_BASE_URL)
    generator._probe_cache.clear()
    generator._probe_locks.clear()
    monkeypatch.setattr(llm, "has_llm", lambda settings=None: False)
    yield
    generator._probe_cache.clear()
    generator._probe_locks.clear()


def _probe_transport(monkeypatch, status):
    calls = []

    async def send(self, request, **kw):
        calls.append(str(request.url))
        if status is None:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(status, json={"data": []}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return calls


def test_llm_port_geocode_runs_when_nightshift_answers(monkeypatch, no_keys):
    calls = _probe_transport(monkeypatch, 200)
    asked = []

    async def fake_ask_json(prompt, settings=None, max_tokens=300, **kw):
        asked.append(prompt)
        return {"latitude": 14.6, "longitude": -61.07, "confidence": 0.8}

    monkeypatch.setattr(llm, "ask_json", fake_ask_json)
    assert _run(llm.llm_geocode_port({"name": "Fort-de-France"}, MQ)) == (14.6, -61.07)
    assert calls == [generator.DEFAULT_BASE_URL + "/models"] and len(asked) == 1


@pytest.mark.parametrize("status", [None, 503])
def test_llm_port_geocode_skipped_when_nightshift_is_down(monkeypatch, no_keys, status):
    _probe_transport(monkeypatch, status)

    async def boom(*a, **k):
        raise AssertionError("no model to ask")

    monkeypatch.setattr(llm, "ask_json", boom)
    assert _run(llm.llm_geocode_port({"name": "Fort-de-France"}, MQ)) is None


def test_nightshift_not_ready_in_sample_mode_or_token_factory(monkeypatch, no_keys):
    calls = _probe_transport(monkeypatch, 200)
    monkeypatch.setenv("SAMPLE_MODE", "true")
    assert _run(generator.nightshift_ready()) is False
    monkeypatch.delenv("SAMPLE_MODE")
    monkeypatch.setenv("LLM_PROVIDER", "tokenfactory")
    assert _run(generator.nightshift_ready()) is False
    assert calls == []


def test_has_llm_itself_is_unchanged(monkeypatch):
    for name in ("NVIDIA_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("METERLESS_RUNTIME_TOKEN", "x")
    from app.core import claude
    monkeypatch.setattr(claude, "claude_enabled", lambda s=None: False)
    assert llm.has_llm({}) is False
