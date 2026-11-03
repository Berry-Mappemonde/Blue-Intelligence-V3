"""LLM geocode budget, Nightshift back-off on 5xx, and island / region names that are not
ports. Run 20261103-181506-d7963f: Cape Verde took 3,960 s for an 1,800 s zone, ~44 min
in llm_geocode_port during a Nightshift outage (24 min on "Santiago").

Offline: geocoders, the LLM geocode and Nightshift are stubbed. No network, no Mongo.
"""
import asyncio
import json

import httpx
import pytest

from app.core import generator, geo, run_rules
from app.services import poe_pipeline as poe

CV = {"mrgid": 8362, "name": "Cape Verde", "geoname": "Cape Verde", "iso2": "CV",
      "sov_iso2": "CV", "sovereign": "Cape Verde",
      "geometry": {"type": "Polygon", "coordinates": [[
          [-26.5, 13.5], [-21.5, 13.5], [-21.5, 18.5], [-26.5, 18.5], [-26.5, 13.5]]]}}
PRAIA = {"lat": "14.9150", "lon": "-23.5087", "display_name": "Praia, Santiago, Cabo Verde",
         "class": "place", "type": "city"}
SANTO_ANTAO = {"lat": "17.0500", "lon": "-25.1700", "display_name": "Santo Antão, Cabo Verde",
               "class": "place", "type": "island"}


from app.services import listing_ref

_LISTING = {}


def _listing():
    """project_listing() once per test session (~17 s, the pipeline step calls it per zone)."""
    if "p" not in _LISTING:
        _LISTING["p"] = _REAL_PROJECT_LISTING()
    return _LISTING["p"]


_REAL_PROJECT_LISTING = listing_ref.project_listing


def _rules(**values):
    return run_rules.bind_rules({"chosen": {k: {"value": v} for k, v in values.items()}})


def _pipeline(monkeypatch, names, rows_by_name=None, llm=None):
    """_extract_and_geocode on Cape Verde with stubbed extractor, geocoders and LLM geocode.
    Returns (docs, logs, events, llm_calls, zone state)."""
    rows_by_name = rows_by_name or {}
    llm_calls: list[str] = []

    async def fake_extract(context, zone, log, rec=None, catalog_text=None, settings=None):
        return [{"name": n, "extraction_engine": "llm"} for n in names]

    async def fake_nominatim(query, country_code=None, limit=1):
        return [dict(r) for n, r in rows_by_name.items() if query.lower().startswith(n.lower())]

    async def no_geonames(query, country_code=None, limit=1):
        return []

    async def fake_llm_geocode(port, zone, settings=None, log=None):
        llm_calls.append(port["name"])
        return await (llm or (lambda p: asyncio.sleep(0, result=None)))(port)

    monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
    monkeypatch.setattr(geo, "_nominatim_rows", fake_nominatim)
    monkeypatch.setattr(geo, "_geonames_rows", no_geonames)
    monkeypatch.setattr(poe, "llm_geocode_port", fake_llm_geocode)
    monkeypatch.setattr(poe, "_hmem_recall", lambda docs, run_id: [[] for _ in docs])
    monkeypatch.setattr(poe, "_hmem_remember", lambda docs, run_id: {})
    monkeypatch.setattr(listing_ref, "project_listing", _listing)

    class _Rec:
        events = []

        async def event(self, step, **payload):
            self.events.append((step, payload))

    rec = _Rec()
    rec.events = []
    logs: list[str] = []

    async def go():
        with poe.zone_extract_budget() as state:
            docs = await poe._extract_and_geocode(dict(CV), "ctx", [{"url": "https://www.gov.cv/x"}],
                                                  logs.append, rec=rec, whitelist=["gov.cv"])
            return docs, dict(state)

    docs, state = asyncio.run(go())
    return docs, logs, rec.events, llm_calls, state


async def _slow(port):
    await asyncio.sleep(30)
    return (15.0, -23.5)


# --- 1. per-port timeout, zone budget, zone deadline -----------------------------------

def test_rules_and_defaults():
    from app.core.run_rules import catalog_default
    assert catalog_default("formalities.llm_geocode_port_timeout_s") == poe.LLM_GEOCODE_PORT_TIMEOUT_S == 30.0
    assert catalog_default("formalities.llm_geocode_zone_budget_s") == poe.LLM_GEOCODE_ZONE_BUDGET_S == 180.0


def test_slow_llm_geocode_times_out_per_port_and_the_port_stays_unplaced(monkeypatch):
    token = _rules(**{"formalities.llm_geocode_port_timeout_s": 0.2,
                      "formalities.llm_geocode_zone_budget_s": 10.0})
    try:
        docs, logs, events, calls, state = _pipeline(
            monkeypatch, ["Porto Inglês", "Vale de Cavaleiros"], llm=_slow)
    finally:
        run_rules.reset_rules(token)
    assert calls == ["Porto Inglês", "Vale de Cavaleiros"]
    assert {d["name"]: d["lat"] for d in docs} == {"Porto Inglês": None, "Vale de Cavaleiros": None}
    assert sum("pas de réponse en 0 s" in m or "pas de réponse en" in m for m in logs) == 2
    (ev,) = [p for step, p in events if step == "geocode_budget"]
    assert ev["timed_out"] == ["Porto Inglês", "Vale de Cavaleiros"] and ev["not_tried"] == []
    assert state["geocode_budget"]["timed_out"] == ev["timed_out"]


def test_zone_budget_stops_the_remaining_ports(monkeypatch):
    token = _rules(**{"formalities.llm_geocode_port_timeout_s": 0.3,
                      "formalities.llm_geocode_zone_budget_s": 0.5})
    try:
        docs, _logs, events, calls, _s = _pipeline(
            monkeypatch, ["Porto Inglês", "Vale de Cavaleiros", "Porto da Furna"], llm=_slow)
    finally:
        run_rules.reset_rules(token)
    assert calls == ["Porto Inglês", "Vale de Cavaleiros"]  # 0.3 s + 0.2 s, then the budget is spent
    (ev,) = [p for step, p in events if step == "geocode_budget"]
    assert ev["not_tried"] == ["Porto da Furna"]
    assert len(docs) == 3 and all(d["lat"] is None for d in docs)


def test_zone_deadline_is_respected(monkeypatch):
    # the deadline is read inside _extract_and_geocode: bind it around the call
    real = poe._extract_and_geocode

    async def with_deadline(*a, **k):
        loop = asyncio.get_running_loop()
        token = poe.zone_deadline.set((loop.time() + poe.GEOCODE_DEADLINE_RESERVE_S + 0.3, 1800.0))
        try:
            return await real(*a, **k)
        finally:
            poe.zone_deadline.reset(token)

    monkeypatch.setattr(poe, "_extract_and_geocode", with_deadline)
    docs, _logs, events, calls, _s = _pipeline(
        monkeypatch, ["Porto Inglês", "Vale de Cavaleiros"], llm=_slow)
    assert calls == ["Porto Inglês"]  # the first gets what is left before the reserve
    (ev,) = [p for step, p in events if step == "geocode_budget"]
    assert ev["timed_out"] == ["Porto Inglês"] and ev["not_tried"] == ["Vale de Cavaleiros"]


def test_fast_llm_geocode_is_unchanged(monkeypatch):
    async def fast(port):
        return (14.9, -23.5)
    docs, _logs, events, calls, state = _pipeline(monkeypatch, ["Porto Inglês"], llm=fast)
    assert calls == ["Porto Inglês"] and docs[0]["lat"] == 14.9
    assert not [e for e in events if e[0] == "geocode_budget"] and "geocode_budget" not in state


# --- 2. Nightshift 5xx: back off, no retries per port --------------------------------------

def test_nightshift_502_marks_it_down_for_the_next_calls(monkeypatch, tmp_path):
    for name in ("SAMPLE_MODE", "METERLESS_MODEL", "MODEL_NAME", "MODEL_BASE_URL", "LLM_PROVIDER",
                 "NEBIUS_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("METERLESS_BASE_URL", generator.DEFAULT_BASE_URL)
    monkeypatch.setenv("METERLESS_RUNTIME_TOKEN", "t")
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "trace.jsonl"))
    monkeypatch.setenv("TRACE_STDOUT", "0")
    monkeypatch.setattr(generator, "RETRY_BACKOFF_S", 0.01)
    generator._probe_cache.clear()
    generator._probe_locks.clear()
    posts: list[int] = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "meterless/auto"}]})
        posts.append(1)
        return httpx.Response(502, text="bad gateway")

    original = httpx.AsyncClient
    monkeypatch.setattr(generator.httpx, "AsyncClient",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    try:
        first = asyncio.run(generator.generate("Santiago?", json_mode=True, run_id="t1"))
        assert first["provider"] == "sample" and "http_502" in first["fallback_reason"]
        assert len(posts) == 2  # the request and its single retry
        assert asyncio.run(generator.nightshift_ready()) is False
        second = asyncio.run(generator.generate("Fogo?", json_mode=True, run_id="t2"))
        assert len(posts) == 2  # backed off: no completion sent while marked down
        assert "down_http_502" in second["fallback_reason"]
    finally:
        generator._probe_cache.clear()
        generator._probe_locks.clear()


# --- 3. island / region names are not ports ---------------------------------------------

def test_cape_verde_island_names_are_dropped_before_geocoding(monkeypatch):
    names = ["Santiago", "Fogo", "S. Vicente", "Sal", "Ilha da Boa Vista", "Praia", "Porto Novo",
             "Tarrafal", "Mindelo"]
    docs, logs, events, calls, _s = _pipeline(monkeypatch, names, rows_by_name={"Praia": PRAIA})
    kept = {d["name"] for d in docs}
    assert kept == {"Praia", "Porto Novo", "Tarrafal", "Mindelo"}
    assert not {"Santiago", "Fogo", "S. Vicente", "Sal", "Ilha da Boa Vista"} & set(calls)
    dropped = [p["port"] for step, p in events if step == "geocode" and p.get("arbitration") == "area_name"]
    assert dropped == ["Santiago", "Fogo", "S. Vicente", "Sal", "Ilha da Boa Vista"]
    assert any("île / région de la zone" in m for m in logs)


def test_a_name_the_geocoders_only_see_as_an_island_is_dropped(monkeypatch):
    """Santo Antão is in no static source; Nominatim returns only place=island."""
    docs, _logs, events, calls, _s = _pipeline(monkeypatch, ["Santo Antao", "Praia"],
                                               rows_by_name={"Santo Antao": SANTO_ANTAO,
                                                             "Praia": PRAIA})
    assert [d["name"] for d in docs] == ["Praia"]
    assert "Santo Antao" not in calls
    assert [p["port"] for s, p in events if s == "geocode" and p.get("arbitration") == "area_hit"] == ["Santo Antao"]


@pytest.mark.parametrize("zone, kept", [
    ({"mrgid": 8460, "name": "New Zealand", "iso2": "NZ", "sovereign": "New Zealand"},
     ["Lyttelton", "Bluff", "Opua"]),         # listing qualifiers that are ports
    ({"mrgid": 8365, "name": "Gibraltar", "iso2": "GI", "sovereign": "United Kingdom"},
     ["Gibraltar"]),                         # a city-state's port carries the zone's name
])
def test_ports_named_like_a_listing_qualifier_or_the_zone_are_kept(zone, kept):
    areas = poe.zone_area_names(zone, listing_ref.listing_ports_for_mrgid(_listing()["ports"], zone["mrgid"]))
    assert not [n for n in kept if poe.is_area_name(n, areas)]


def test_island_keys_and_abbreviations():
    areas = {"sao vicente", "fogo"}
    assert poe.is_area_name("S. Vicente", areas) and poe.is_area_name("Ilha do Fogo", areas)
    assert poe.is_area_name("São Vicente Island", areas)
    assert not poe.is_area_name("Porto Grande (São Vicente)", areas)
    assert not poe.only_area_hits({"candidates": [{"osm_type": "island"}]}, "Porto do Fogo")
