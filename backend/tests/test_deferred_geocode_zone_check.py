"""A deferred catalogue name is checked against the zone, and "port de X" on a national
document is scoped to the territory.

Run 20261103-114750-f3993a, Canary Islands: 22 catalogue names, geocode deferred (7bde9c2),
so no polygon check ran: 14 Canary harbours, plus Santander, Barcelona, València, Alcudia,
Ibiza, Santa María de Motril (1948 Ordenanzas Generales de Aduanas, hacienda.gob.es),
"Registration in" (REBECA guide, transportes.gob.es) and "Canarias". Run
20261103-004818-e80c7c, Martinique: Bayonne from a mer.gouv.fr table of contents.
Offline: the geocode cache is a stub, no HTTP.
"""
import asyncio

import pytest

from app.core import geo
from app.core.extract import extract_structured_ports
from app.services import poe_pipeline as poe

CANARY = {"mrgid": 8364, "name": "Canary Islands", "iso2": None, "sov_iso2": "ES",
          "geometry": {"type": "Polygon", "coordinates": [[
              [-18.6, 26.8], [-12.9, 26.8], [-12.9, 29.8], [-18.6, 29.8], [-18.6, 26.8]]]}}
MARTINIQUE = {"mrgid": 33178, "name": "Martinique", "iso2": "MQ", "sov_iso2": "FR"}
SAINT_LUCIA = {"mrgid": 8416, "name": "Saint Lucia", "iso2": "LC", "sov_iso2": "LC"}

# Verbatim (markdown as fetched): puertoscanarios.es/normativa-y-legales/.
PUERTOS_CANARIOS = (
    "Instrucciones internas de contratación para los contratos no sujetos a regulación "
    "armonizada de la entidad Puertos Canarios\n\n# Puertos Canarios\n\n"
    "![Puerto de Arguineguín](https://puertoscanarios.es/wp-content/uploads/2016/07/Arguineguín-510x382.jpg)\n\n"
    "### Puerto de Arguineguín\n\n19/07/2016\n\n"
    "![Puerto de Las Nieves](https://puertoscanarios.es/wp-content/uploads/2016/07/agaete6.jpg)\n\n"
    "### Puerto de Las Nieves\n\n12/07/2016\n\n")
# 1948 Ordenanzas Generales de Aduanas (hacienda.gob.es/DocLeyes/aduanas/22989.htm).
ORDENANZAS = (
    "para el embarco y desembarco de pasajeros de la línea regular de hidroaviones "
    "Marsella-Argel, con escala discrecional en el puerto de Alcudia. Provincia de Valencia. "
    "Sagunto: Para la importación de material para la construcción del ferrocarril y puerto "
    "mientras aquélla dure, con intervención de la Aduana de Valencia. "
    "Formentera): Para el embarque de los productos naturales agrícolas de la isla y los "
    "elaborados por su industria, siempre que se destinen al puerto de Ibiza y se conduzcan "
    "en embarcaciones menores.")
# REBECA guidelines (cdn.transportes.gob.es), verbatim.
REBECA = (
    "SPECIAL REGISTRY OF SHIPS AND SHIPPING COMPANIES OF THE CANARY ISLANDS GENERAL "
    "DIRECTORATE OF THE MERCHANT MARINE English version: 18.01 Guidelines Page 6 The Deputy "
    "Director of Safety, Marine Pollution and Maritime Inspection, authorized on December 14, "
    "2011, changing the port of Registration in the Special Registry thereby responding the "
    "needs of the maritime sector.")
# mer.gouv.fr ports annex, table of contents (run e80c7c), verbatim.
MER_ANNEXE = (
    "13. Arrêté du 28 mai 2021 portant désignation du préfet chargé de la sûreté portuaire "
    "sur l'emprise du port de Bayonne.......140\n"
    "14. Arrêté du 28 mai 2021 portant désignation du préfet chargé de la sûreté portuaire")


def _ctx(*blocks):
    return "\n\n".join(f"[SOURCE: {u}]\n{t}" for u, t in blocks)


def _names(text, zone):
    return [p["name"] for p in poe.zone_catalog(text, zone)[0]]


def test_canary_catalogue_keeps_the_territory_s_own_ports_only():
    text = _ctx(("https://puertoscanarios.es/normativa-y-legales/", PUERTOS_CANARIOS),
                ("https://www.hacienda.gob.es/DocLeyes/aduanas/22989.htm", ORDENANZAS),
                ("https://cdn.transportes.gob.es/portal-web-sede/documentos/guidelinesrebeca.pdf", REBECA))
    names = _names(text, CANARY)
    assert names == ["Arguineguín", "Las Nieves"]


@pytest.mark.parametrize("phrase", ["el puerto de Santander", "el puerto de Barcelona",
                                    "el puerto de Santa María de Motril"])
def test_other_provinces_ports_on_a_national_decree_are_dropped(phrase):
    text = _ctx(("https://www.hacienda.gob.es/DocLeyes/aduanas/22989.htm",
                 f"Habilitadas para la importación las Aduanas de Alicante y {phrase}. "
                 "Provincia de Vizcaya."))
    assert _names(text, CANARY) == []


def test_national_decree_section_naming_the_territory_is_kept():
    # Constructed on Article 203's wording: the territory named just before.
    text = _ctx(("https://www.hacienda.gob.es/DocLeyes/aduanas/22989.htm",
                 "Artículo 203. Los puertos habilitados en las islas Canarias para verificar "
                 "operaciones de carga y descarga serán los siguientes: el puerto de Arrecife, "
                 "el puerto de Puerto del Rosario."))
    assert "Arrecife" in _names(text, CANARY)


def test_bayonne_is_not_martinique():
    text = _ctx(("https://www.mer.gouv.fr/sites/default/files/2024-04/25042024_ports_maritimes_annexe.pdf",
                 MER_ANNEXE))
    assert _names(text, MARTINIQUE) == []
    # The territory's own publisher (prefecture subdomain) is not a national document.
    local = _ctx(("https://www.martinique.gouv.fr/x.pdf",
                  "Arrêté relatif à la sûreté du port de Fort-de-France, du Marin et de la Trinité."))
    assert "Fort-de-France" in _names(local, MARTINIQUE)


def test_zones_that_are_their_own_sovereign_are_not_scoped():
    text = _ctx(("https://www.govt.lc/legislation/x.pdf",
                 "Vessels shall clear at the port of Castries, the port of Vieux Fort or "
                 "Rodney Bay."))
    assert "Castries" in _names(text, SAINT_LUCIA)


def test_registration_in_is_not_a_port():
    assert all(p["name"] != "Registration in" for p in extract_structured_ports(REBECA))


@pytest.mark.parametrize("name, expected", [
    ("Canarias", True), ("Islas Canarias", True), ("Martinique", True), ("Guadeloupe", True),
    ("Arguineguín", False), ("Las Palmas", False), ("Gibraltar", False), ("Saint Lucia", False),
])
def test_territory_names(name, expected):
    assert poe.is_territory_name(name) is expected


def _cache(monkeypatch, rows_by_name):
    async def fake_get(provider, query, country_code, limit):
        for name, row in rows_by_name.items():
            if name.lower() in query.lower():
                return [dict(row)]
        return None

    async def no_http(*a, **k):
        raise AssertionError("cache only: no Nominatim call")

    monkeypatch.setattr(geo, "_geocode_cache_get", fake_get)
    monkeypatch.setattr(geo, "_nominatim_rows", no_http)


SANTANDER = {"lat": "43.4623", "lon": "-3.8100", "display_name": "Santander, Cantabria, España",
             "class": "place", "type": "city"}
ARGUINEGUIN = {"lat": "27.7614", "lon": "-15.6815", "display_name": "Arguineguín, Gran Canaria",
               "class": "place", "type": "village"}


def _geom(zone):
    from shapely.geometry import shape
    from shapely.prepared import prep
    g = shape(zone["geometry"])
    return g, prep(g)


def test_deferred_name_with_a_cached_point_outside_is_dropped(monkeypatch):
    _cache(monkeypatch, {"Santander": SANTANDER, "Arguineguín": ARGUINEGUIN})
    g, p = _geom(CANARY)
    out = lambda n: asyncio.run(poe.deferred_outside_zone({"name": n}, {"name": n}, CANARY, g, p))
    assert out("Santander") == "géocode en cache hors du polygone"
    assert out("Arguineguín") is None          # cached point in the polygon
    assert out("Garachico") is None            # nothing cached: kept, deferred as before
    assert out("Canarias") == "nom de territoire"


def test_deferred_catalogue_drops_outside_names_keeps_the_rest(monkeypatch):
    _cache(monkeypatch, {"Santander": SANTANDER, "Arguineguín": ARGUINEGUIN})
    names = ["Arguineguín", "Las Nieves", "Garachico", "Playa San Juan", "Vueltas",
             "La Restinga", "Tazacorte", "Santander", "Canarias"]

    async def fake_extract(context, zone, log, rec=None, catalog_text=None, settings=None):
        return [{"name": n, "extraction_engine": "catalog"} for n in names]

    monkeypatch.setattr(poe, "extract_ports_llm", fake_extract)
    monkeypatch.setattr(poe, "_hmem_recall", lambda docs, run_id: [[] for _ in docs])
    monkeypatch.setattr(poe, "_hmem_remember", lambda docs, run_id: {})
    from app.services import listing_ref
    monkeypatch.setattr(listing_ref, "project_listing", lambda: {"ports": []})
    logs = []
    docs = asyncio.run(poe._extract_and_geocode(
        dict(CANARY), "ctx", [{"url": "https://puertoscanarios.es/x", "official": True}],
        logs.append, whitelist=["puertoscanarios.es"]))
    kept = [d["name"] for d in docs]
    assert "Santander" not in kept and "Canarias" not in kept
    assert kept and set(kept) <= set(names) - {"Santander", "Canarias"}
    assert all(d.get("geocode_arbitration") == "geocode_deferred" for d in docs)
    assert any("Santander" in m and "hors de ce polygone" in m for m in logs)
