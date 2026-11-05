"""Mainland names are dropped by the zone check, whatever their source (offline).

Run 20261105-183713-b60354 kept 25 mainland names in the Canaries. "Puerto de Bilbao" stood on the
BOE, which didn't read as a national text, and the geocoder then found Calle Bilbao in Telde,
which is in the polygon. Texts and points are the run's.
"""
import asyncio

import pytest

from app.core import geo
from app.services import listing_ref
from app.services import poe_pipeline as poe

CANARIES = {"mrgid": 8364, "name": "Canary Islands", "geoname": "Canary Islands",
            "sov_iso2": "ES", "sovereign": "Spain",
            "geometry": {"type": "Polygon", "coordinates": [[
                [-19.0, 26.5], [-12.5, 26.5], [-12.5, 30.5], [-19.0, 30.5], [-19.0, 26.5]]]}}
BOE = "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2019-7617"
TABLE = ("Puestos fronterizos marítimos.\n"
         "| Puerto de Barcelona. | Jefatura Superior de Policía de Cataluña. |\n"
         "| Puerto de Bilbao. | Jefatura Superior de Policía del País Vasco. |\n"
         "| Puerto de Las Palmas. | Jefatura Superior de Policía de Canarias. |\n"
         "| Puerto de Málaga. | Comisaría Provincial de Málaga. |\n"
         "| Puerto de Arrecife. | Comisaría Local de Arrecife. |\n"
         "| Puerto de Sagunto. | Comisaría Local de Sagunto. |\n")
RAW = f"[SOURCE: {BOE}]\n{TABLE}"
# (name: (hit with the zone in the query, the name looked up alone))
STREET = {"class": "highway", "type": "residential"}
CITY = {"class": "boundary", "type": "administrative"}
PORT = {"class": "landuse", "type": "port"}
PLACES = {
    "Bilbao": (dict(lat="28.0318505", lon="-15.4196338", **STREET),
               dict(lat="43.2630", lon="-2.9350", **CITY)),
    "Málaga": (dict(lat="28.092365", lon="-15.4150896", **STREET),
               dict(lat="36.7213", lon="-4.4214", **CITY)),
    "Arrecife": (dict(lat="28.9639681", lon="-13.5499014", **PORT),
                 dict(lat="28.9639681", lon="-13.5499014", **PORT)),
    "Calle Mayor": (dict(lat="28.1", lon="-15.4", **STREET), None),
}


def _names(ports):
    return {p["name"] for p in ports}


# --- 1. a national list is national -------------------------------------------------------

def test_the_boe_is_a_national_text():
    scope = poe.legal_phrase_scope(CANARIES)
    assert "boe.es" in scope["national"]
    assert poe._national_document(BOE, scope)


@pytest.mark.parametrize("name", ["Bilbao", "Málaga", "Sagunto", "Barcelona"])
def test_a_mainland_port_of_a_national_list_is_dropped(name):
    assert name not in _names(poe.zone_catalog(RAW, CANARIES)[0])


@pytest.mark.parametrize("name", ["Las Palmas", "Arrecife"])
def test_the_zone_s_own_ports_of_that_list_stay(name):
    assert name in _names(poe.zone_catalog(RAW, CANARIES)[0])


def test_a_zone_with_no_sovereign_elsewhere_is_not_scoped():
    assert poe.legal_phrase_scope({"mrgid": 8362, "name": "Cape Verde"}) is None


# --- 2. a same-named street is not a port -------------------------------------------------

def _pipeline(monkeypatch, names, engine="nightshift"):
    llm_calls: list[str] = []

    async def extract(context, zone, log, rec=None, catalog_text=None, settings=None):
        return [{"name": n, "extraction_engine": engine} for n in names]

    async def nominatim(query, country_code=None, limit=1):
        for name, (with_zone, alone) in PLACES.items():
            if query == name:
                return [{**alone, "display_name": name}] if alone else []
            if query.startswith(name):
                return [{**with_zone, "display_name": name}]
        return []

    async def no_geonames(query, country_code=None, limit=1):
        return []

    async def llm_geocode(port, zone, settings=None, log=None):
        llm_calls.append(port["name"])

    monkeypatch.setattr(poe, "extract_ports_llm", extract)
    monkeypatch.setattr(geo, "_nominatim_rows", nominatim)
    monkeypatch.setattr(geo, "_geonames_rows", no_geonames)
    monkeypatch.setattr(poe, "llm_geocode_port", llm_geocode)
    monkeypatch.setattr(poe, "_hmem_recall", lambda docs, run_id: [[] for _ in docs])
    monkeypatch.setattr(poe, "_hmem_remember", lambda docs, run_id: {})
    monkeypatch.setattr(listing_ref, "project_listing", lambda: {"ports": []})

    class Rec:
        events: list = []

        async def event(self, step, **payload):
            self.events.append((step, payload))

    rec = Rec()
    rec.events = []
    logs: list[str] = []

    async def go():
        with poe.zone_extract_budget():
            return await poe._extract_and_geocode(dict(CANARIES), "ctx", [{"url": BOE}], logs.append,
                                                  rec=rec, whitelist=["boe.es"])

    return asyncio.run(go()), rec.events, llm_calls


@pytest.mark.parametrize("engine", ["nightshift", "catalog", "ner"])
@pytest.mark.parametrize("name", ["Bilbao", "Málaga"])
def test_a_street_in_the_zone_doesn_t_keep_a_mainland_name_whatever_read_it(monkeypatch, name, engine):
    docs, events, llm_calls = _pipeline(monkeypatch, [name], engine)
    assert docs == [] and llm_calls == []
    (event,) = [p for s, p in events if s == "geocode" and p.get("port") == name]
    assert event["arbitration"] == "outside_zone" and event["street"] is True
    assert event["kept"] is False


def test_a_port_in_the_zone_is_kept_with_its_point(monkeypatch):
    docs, _, _ = _pipeline(monkeypatch, ["Arrecife"])
    assert [(d["name"], d["lat"]) for d in docs] == [("Arrecife", 28.9639681)]


def test_a_street_alone_gives_no_point_and_no_llm_guess(monkeypatch):
    docs, _, llm_calls = _pipeline(monkeypatch, ["Calle Mayor"])
    assert [d["name"] for d in docs] == ["Calle Mayor"] and docs[0]["lat"] is None
    assert llm_calls == []


def test_a_name_found_nowhere_is_kept(monkeypatch):
    docs, _, llm_calls = _pipeline(monkeypatch, ["Muelle Sin Nombre"])
    assert [d["name"] for d in docs] == ["Muelle Sin Nombre"]
    assert llm_calls == ["Muelle Sin Nombre"]


def test_a_zone_with_no_mainland_makes_no_lookup(monkeypatch):
    calls = []

    async def nominatim(query, country_code=None, limit=1):
        calls.append(query)
        return []

    monkeypatch.setattr(geo, "_nominatim_rows", nominatim)
    zone = {"mrgid": 8362, "name": "Cape Verde"}
    assert asyncio.run(poe.name_outside_zone({"name": "Furna"}, {}, zone, object(), object())) is None
    assert calls == []


def test_a_port_on_record_in_the_zone_needs_no_lookup(monkeypatch):
    calls = []

    async def nominatim(query, country_code=None, limit=1):
        calls.append(query)
        return []

    monkeypatch.setattr(geo, "_nominatim_rows", nominatim)
    assert asyncio.run(poe.name_outside_zone({"name": "Arrecife"}, {}, CANARIES, object(), object())) is None
    assert calls == []


def test_a_deferred_name_with_only_a_street_is_outside(monkeypatch):
    async def cached(port, zone):
        return [{"lat": 28.03, "lon": -15.42, "osm_class": "highway"}]

    monkeypatch.setattr(poe, "cached_nominatim_candidates", cached)
    out = asyncio.run(poe.deferred_outside_zone({"name": "Bilbao"}, {}, CANARIES, object(), object()))
    assert out == "seule une rue porte ce nom"
