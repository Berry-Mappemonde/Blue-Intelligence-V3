"""Expected official sources (FR hexagon, MX, VE, NU, NZ, NC).

URLs are *discovery targets*, not the card display.
Seeds are indexed by polygon iso2 (Mayotte ≠ FR, Niue ≠ NZ).
"""
import asyncio

from app.core.extract import official_attachments, should_follow_attachments
from app.services import poe_pipeline as poe

EXAMPLE_ZONES = {
    5677: {
        "iso2": "FR", "sov_iso2": "FR", "name": "France", "sovereign": "France",
        "pol_type": "200NM", "mrgid": 5677,
    },
    8429: {
        "iso2": "MX", "sov_iso2": "MX", "name": "Mexico", "sovereign": "Mexico",
        "pol_type": "200NM", "mrgid": 8429,
    },
    8433: {
        "iso2": "VE", "sov_iso2": "VE", "name": "Venezuela", "sovereign": "Venezuela",
        "pol_type": "200NM", "mrgid": 8433,
    },
    8447: {
        "iso2": "NU", "sov_iso2": "NZ", "name": "Niue", "sovereign": "New Zealand",
        "pol_type": "200NM", "mrgid": 8447,
    },
    8455: {
        "iso2": "NZ", "sov_iso2": "NZ", "name": "New Zealand", "sovereign": "New Zealand",
        "pol_type": "200NM", "mrgid": 8455,
    },
    8312: {
        "iso2": "NC", "sov_iso2": "FR", "name": "New Caledonia", "sovereign": "France",
        "pol_type": "200NM", "mrgid": 8312,
    },
    21803: {
        "iso2": "SX", "sov_iso2": "NL", "name": "Sint-Maarten", "sovereign": "Netherlands",
        "pol_type": "200NM", "mrgid": 21803,
    },
    48944: {
        "iso2": "YT", "sov_iso2": "FR", "name": "Mayotte", "sovereign": "France",
        "pol_type": "200NM", "mrgid": 48944,
    },
    5696: {
        "iso2": "GB", "sov_iso2": "GB", "name": "United Kingdom", "sovereign": "United Kingdom",
        "pol_type": "200NM", "mrgid": 5696,
    },
    8490: {
        "iso2": "EG", "sov_iso2": "EG", "name": "Egypt", "sovereign": "Egypt",
        "pol_type": "200NM", "mrgid": 8490,
    },
    5670: {
        "iso2": "AL", "sov_iso2": "AL", "name": "Albania", "sovereign": "Albania",
        "pol_type": "200NM", "mrgid": 5670,
    },
}

PINNED_NEEDLES = {
    5677: ["vous-naviguez-en-provenance"],
    8429: ["puertos-y-terminales"],
    8433: ["Ley-de-Marinas-y-Actividades-Conexas.pdf",
           "CAPITANIAS-DE-PUERTO"],
    8447: ["niue_laws_vol4_part1"],
    8455: ["places-of-first-arrival-seaports", "sailing-to-new-zealand-this-small-craft-season"],
    8312: ["formalites-douanieres-pour-les-navires-de-plaisance"],
    21803: ["sintmaartengov.org", "Pages/Customs.aspx"],
    48944: ["JORFTEXT000030235682"],
    5696: ["submit-a-pleasure-craft-report"],
    8490: ["sis.gov.eg", "yacht-tourism"],
    5670: ["dogana.gov.al", "autorizim-per-perjashtimin", "akcizes", "peshkimit"],
}

FOREIGN_NEEDLES = {
    5677: ["mpi.govt.nz", "inea.gob.ve", "gouv.nc", "sintmaartengov", "sis.gov.eg",
           "dogana.gov.al"],
    8429: ["vous-naviguez", "mpi.govt.nz", "sintmaartengov", "sis.gov.eg"],
    8433: ["vous-naviguez", "mpi.govt.nz", "sintmaartengov", "sis.gov.eg"],
    8447: ["mpi.govt.nz", "customs.govt.nz", "vous-naviguez"],
    8455: ["niue_laws", "vous-naviguez", "gouv.nc", "sintmaartengov", "sis.gov.eg"],
    8312: ["vous-naviguez-en-provenance", "mpi.govt.nz", "sintmaartengov", "sis.gov.eg"],
    21803: ["vous-naviguez", "mpi.govt.nz", "inea.gob.ve", "gouv.nc"],
    48944: ["vous-naviguez-en-provenance", "Liste-ports-de-plaisance",
            "submit-a-pleasure-craft-report"],
    5696: ["vous-naviguez", "JORFTEXT000030235682", "inea.gob.ve"],
    8490: ["vous-naviguez", "mpi.govt.nz", "sintmaartengov", "inea.gob.ve"],
    5670: ["vous-naviguez", "mpi.govt.nz", "sis.gov.eg", "sintmaartengov"],
}


def _pinned(zone):
    return poe.seed_url_candidates(zone) + poe.landing_url_candidates(zone)


def test_example_zones_pin_expected_pages_only():
    for mrgid, zone in EXAMPLE_ZONES.items():
        blob = " ".join(c["url"] for c in _pinned(zone))
        for needle in PINNED_NEEDLES[mrgid]:
            assert needle in blob, (mrgid, needle, blob)
        for foreign in FOREIGN_NEEDLES[mrgid]:
            assert foreign not in blob, (mrgid, foreign, blob)


def test_france_hexagon_not_mayotte_and_not_caledonia():
    hexagon = _pinned(EXAMPLE_ZONES[5677])
    mayotte = poe.seed_url_candidates({
        "iso2": "YT", "sov_iso2": "FR", "name": "Mayotte", "mrgid": 48944,
    })
    nc = _pinned(EXAMPLE_ZONES[8312])
    h = " ".join(c["url"] for c in hexagon)
    assert "vous-naviguez" in h
    assert not any("vous-naviguez" in c["url"] for c in mayotte)
    assert any("JORFTEXT000030235682" in c["url"] for c in mayotte)
    assert not any("JORFTEXT000030235682" in c["url"] for c in hexagon)
    assert not any("vous-naviguez-en-provenance" in c["url"] for c in nc)
    assert any("formalites-douanieres" in c["url"] for c in nc)


def test_france_landing_still_yields_current_list_and_ppf():
    html = """
    <a href="/sites/default/files/2022-07/11/carte-ppf-maritimes.pdf">old</a>
    <a href="https://www.douane.gouv.fr/sites/default/files/uploads/files/carte-PPF-maritimes.pdf">PPF</a>
    <a href="/sites/default/files/2022-07/11/formulaire-immigration.pdf">form</a>
    <a href="/sites/default/files/2026-07/06/Liste-ports-de-plaisance-eligibles.pdf">liste</a>
    """
    base = ("https://www.douane.gouv.fr/particuliers/vous-naviguez/"
            "vous-naviguez-en-provenance-ou-destination-dun-pays-non-membre-de")
    atts = official_attachments(html, base)
    blob = " ".join(atts)
    assert "Liste-ports-de-plaisance-eligibles.pdf" in blob
    assert "uploads/files/carte-PPF-maritimes.pdf" in blob
    assert should_follow_attachments(html, base)


def test_venezuela_reglamento_ocr_yields_capitanias():
    from app.core.extract import extract_structured_ports, looks_like_port_catalog
    text = """
    Artículo 4:—Las Capitanias de Puerto de la República, son las siguientes:
    1) Capitanía de Puerto de Maracaibo
    2) Capitanía de Puerto de Las Piedras
    3) Capitanía de Puerto de La Vela de Coro
    4) Capitanía de Puerto de Puerto Cabello
    5) Capitanía de Puerto de La Guaira
    6) Capitanía de Puerto de Guanta-Puerto La Cruz
    7) Capitanía de Puerto de Puerto Sucre
    8) Capitanía de Puerto de Carúpano
    9) Capitanía de Puerto de Pampatar
    10) Capitanía de Puerto de Güiria
    11) Capitanía de Puerto de Caripito
    12) Capitanía de Puerto de Ciudad Guayana
    13) Capitanía de Puerto de Ciudad Bolívar
    14) Capitanía de Puerto de Amazonas
    15) Capitanía de Puerto de Apure
    Delegaciones:
    —La Salina (Cabimas)
    —Puerto Miranda
    """
    ports = extract_structured_ports(text)
    names = " ".join(p["name"] for p in ports)
    assert looks_like_port_catalog(text)
    assert "Maracaibo" in names and "La Guaira" in names
    assert "Pampatar" in names and "Güiria" in names
    assert len(ports) >= 15
    ocr = extract_structured_ports("10) Capitanía de Puerto de Gúlria")
    assert any(p["name"] == "Güiria" for p in ocr)


def test_france_plaisance_table_yields_ports():
    from app.core.extract import catalog_is_sufficient, extract_structured_ports
    text = """
    Liste des ports de plaisance éligibles
    Haut de France
    Calais
    Port de plaisance de Calais
    Calais
    PAF
    Haut de France
    Dunkerque
    Dunkerque Marina
    Dunkerque
    PAF
    Normandie
    Dieppe
    Port de plaisance de Dieppe
    Dieppe
    Douane
    Bretagne
    Saint-Malo
    Saint-Malo Plaisance
    Saint Malo
    PAF
    PACA
    Antibes
    Port Vauban
    Cannes
    Douane
    PACA
    Cap d'Ail
    Port de Cap d'Ail
    Monaco
    PAF
    Nouvelle Aquitaine
    La Rochelle
    Port de plaisance de La Rochelle
    La Rochelle La Pallice
    Douane
    Corse
    Ajaccio
    Tino Rossi
    Ajaccio
    PAF
    """
    ports = extract_structured_ports(text)
    names = " ".join(p["name"] for p in ports)
    assert "Calais" in names and "Dunkerque Marina" in names
    assert "Port Vauban" in names and "La Rochelle" in names
    assert catalog_is_sufficient(ports, text)


def test_venezuela_inea_attachments_keep_ley_and_reglamento():
    html = """
    <a href="/wp-content/uploads/2026/04/Ley-de-Marinas-y-Actividades-Conexas.pdf">ley</a>
    <a href="/wp-content/uploads/2026/04/REGLAMENTO-QUE-DETERMINA-LA-JURISDICCION-DE-LAS-CAPITANIAS-DE-PUERTO-DE-LA-REPUBLICA.pdf">reg</a>
    <a href="/wp-content/uploads/2024/01/logo-inea.pdf">logo</a>
    """
    base = "https://inea.gob.ve/"
    atts = official_attachments(html, base)
    blob = " ".join(atts)
    assert "Ley-de-Marinas-y-Actividades-Conexas.pdf" in blob
    assert "CAPITANIAS-DE-PUERTO" in blob
    assert should_follow_attachments(html, base)


def test_example_hints_stay_on_the_polygon():
    nz = " ".join(poe.search_hint_queries(EXAMPLE_ZONES[8455]))
    nu = " ".join(poe.search_hint_queries(EXAMPLE_ZONES[8447]))
    nc = " ".join(poe.search_hint_queries(EXAMPLE_ZONES[8312]))
    fr = " ".join(poe.search_hint_queries(EXAMPLE_ZONES[5677]))
    ve = " ".join(poe.search_hint_queries(EXAMPLE_ZONES[8433]))
    sx = " ".join(poe.search_hint_queries(EXAMPLE_ZONES[21803]))
    yt = " ".join(poe.search_hint_queries(EXAMPLE_ZONES[48944]))
    gb = " ".join(poe.search_hint_queries(EXAMPLE_ZONES[5696]))
    eg = " ".join(poe.search_hint_queries(EXAMPLE_ZONES[8490]))
    al = " ".join(poe.search_hint_queries(EXAMPLE_ZONES[5670]))
    assert "places of first arrival" in nz and "Niue" not in nz
    assert "Customs Act" in nu and "places of first arrival" not in nu
    assert "plaisance" in nc and "PPF" not in nc
    assert "PPF" in fr and "Mayotte" not in fr and "Calédonie" not in fr
    assert "Ley de Marinas" in ve and "capitanías" in ve
    assert "Sint Maarten customs" in sx
    assert "havens van binnenkomst" not in sx
    assert "annexe I" in yt and "PPF" not in yt
    assert "pleasure craft" in gb and "Mayotte" not in gb
    assert poe.zone_search_lang(EXAMPLE_ZONES[21803]) is None
    assert "legifrance.gouv.fr" in poe.build_whitelist("YT", "FR", mrgid=48944)
    assert "sintmaartengov.org" in poe.build_whitelist("SX", "NL")
    assert "government.nl" not in poe.build_whitelist("SX", "NL")
    qsx = poe.localized_query(EXAMPLE_ZONES[21803]) or ""
    assert "customs department" in qsx and "binnenkomst" not in qsx
    assert "yacht tourism" in eg and "Mayotte" not in eg
    qeg = poe.localized_query(EXAMPLE_ZONES[8490]) or ""
    assert "yacht tourism" in qeg
    assert "sis.gov.eg" in poe.build_whitelist("EG", "EG")
    assert poe.url_allowed(
        "https://sis.gov.eg/en/egypt/tourism/yacht-tourism/yacht-tourism/",
        poe.build_whitelist("EG", "EG"),
    )
    assert "akciz" in al and "peshkimit" in al and "Durres" not in al
    qal = poe.localized_query(EXAMPLE_ZONES[5670]) or ""
    assert "porteve detare" in qal
    al_url = ("https://www.dogana.gov.al/dokument/2251/"
              "autorizim-per-perjashtimin-nga-detyrimet-e-akcizes-se-karburantit"
              "-per-anijet-e-peshkimit")
    assert poe.url_allowed(al_url, poe.build_whitelist("AL", "AL"))
    assert "dogana.gov.al" in poe.build_whitelist("AL", "AL")
    assert poe.list_url_bonus(al_url) >= 0.3


def test_google_style_query_matches_human_serp():
    eg = poe.google_style_query(EXAMPLE_ZONES[8490])
    assert "Egypt" in eg and "official" in eg and ".eg" in eg
    assert "Hurghada" not in eg
    al = poe.google_style_query(EXAMPLE_ZONES[5670])
    assert "Albania" in al and ".al" in al
    yt = poe.google_style_query(EXAMPLE_ZONES[48944], "fr")
    assert "Mayotte" in yt and ".yt" in yt and ".fr" in yt
    assert "France" not in yt
    sx = poe.google_style_query(EXAMPLE_ZONES[21803])
    assert "Sint Maarten" in sx and ".sx" in sx
    assert "Sint-Maarten" not in sx
    shots = poe.google_style_shots(EXAMPLE_ZONES[21803])
    assert shots == [("en", sx)]
    fr_shots = poe.google_style_shots(EXAMPLE_ZONES[5677])
    langs = [lang for lang, _ in fr_shots]
    assert langs == ["en", "fr"]
    hr = {"iso2": "HR", "sov_iso2": "HR", "name": "Croatia", "sovereign": "Croatia"}
    qhr = poe.google_style_query(hr)
    assert "Croatia" in qhr and ".hr" in qhr and "akciz" not in qhr


def test_witness_polygon_gets_family_hints_without_iso_switch():
    """Outside the 11: legal + pleasure + EN, not an if iso AL/FR."""
    hr = {"iso2": "HR", "sov_iso2": "HR", "name": "Croatia", "sovereign": "Croatia"}
    hints = " ".join(poe.default_search_hints(hr))
    assert "yacht" in hints and "first arrival" in hints
    assert "akcizës" not in hints and "PPF" not in hints
    loc = poe.localized_query(hr) or ""
    assert "Croatia" in loc
    pdf = poe.site_list_pdf_query("gov.hr", hr)
    assert "ports of entry" in pdf or "yacht" in pdf
    assert "JORFTEXT" not in pdf and "C1331" not in pdf
    q2 = poe.lessons_learned_query(hr)
    assert "yacht" in q2 and "akcizë" in q2 and "Croatia" in q2


def test_sint_maarten_gov_org_is_whitelisted_not_sx_cctld():
    """The SX state site is sintmaartengov.org, not a .sx host."""
    wl = poe.build_whitelist("SX", "NL")
    url = ("https://www.sintmaartengov.org/Ministries/Departments/"
           "Pages/Customs.aspx")
    assert "sintmaartengov.org" in wl
    assert poe.url_allowed(url, wl)
    assert poe.url_allowed("https://www.sintmaartengov.org/", wl)
    assert not poe.url_allowed("https://www.government.nl/", wl)
    assert not poe.is_foreign_gov_domain("sintmaartengov.org", {"sx"})
    assert poe.domain_of(url) == "sintmaartengov.org"


def test_collect_follows_list_pdfs_hidden_in_html(monkeypatch):
    landing = (
        "https://www.douane.gouv.fr/particuliers/vous-naviguez/"
        "vous-naviguez-en-provenance-ou-destination-dun-pays-non-membre-de"
    )
    liste = ("https://www.douane.gouv.fr/sites/default/files/2026-07/06/"
             "Liste-ports-de-plaisance-eligibles.pdf")
    carte = ("https://www.douane.gouv.fr/sites/default/files/uploads/files/"
             "carte-PPF-maritimes.pdf")
    html = (
        f'<a href="{carte}">PPF</a>'
        f'<a href="{liste}">liste</a>'
    )
    prose = "Formalités pour les plaisanciers en provenance d'un pays tiers. " * 40

    async def fake_cascade(url, min_chars=200, log=None):
        if "carte-PPF" in url:
            return {"text": "Points de passage frontaliers maritimes",
                    "html": "", "md5": "c", "level": "N1-pymupdf", "blocked": False}
        if url.endswith(".pdf"):
            return {"text": "1.- Port Alpha latitude: 46.1 longitude: -1.1\n" * 8,
                    "html": "", "md5": "p", "level": "N1", "blocked": False}
        return {"text": prose, "html": html, "md5": "h", "level": "N1", "blocked": False}

    monkeypatch.setattr(poe, "extract_cascade", fake_cascade)
    _texts, _h, used, _ex = asyncio.run(poe._collect_texts(
        [{"url": landing, "domain": "douane.gouv.fr"}], lambda m: None))
    blob = " ".join(c["url"] for c in used)
    assert liste in blob
    assert carte in blob


def test_mexico_habilitados_table_yields_coords():
    from app.core.extract import catalog_is_sufficient, extract_structured_ports
    text = """
    PUERTOS Y TERMINALES HABILITADOS
    COMERCIAL PESQUERA TURÍSTICA
    1 Bahía Colonet
    Baja California
    Puerto
    07/08/2006
    30.96571843
    -116.2804389 https://www.dof.gob.mx/nota
    4 Ensenada
    Baja California
    Puerto
    31/05/1974
    31.8522146
    -116.625788 https://www.dof.gob.mx/nota
    34 Manzanillo
    Colima
    Puerto
    01/01/2000
    19.057546
    -104.313762
    [ACTIVIDAD_TURISTICA]
    4 Ensenada
    34 Manzanillo
    """
    ports = extract_structured_ports(text)
    by = {p["name"]: p for p in ports}
    assert "Ensenada" in by and "Manzanillo" in by
    assert "Bahía Colonet" not in by
    assert abs(by["Ensenada"]["lat"] - 31.8522146) < 1e-6
    assert by["Ensenada"]["city"] == "Baja California"
    assert catalog_is_sufficient(ports, text)


def test_mexico_jina_drops_non_turistica_activity():
    from app.core.extract import extract_structured_ports
    text = """
#### 1.- Bahía Colonet
**Entidad federativa:**Baja California
**Tipo de actividad:**Comercial
**Latitud:**30.96571843
**Longitud:**-116.2804389
#### 4.- Ensenada
**Entidad federativa:**Baja California
**Tipo de actividad:**Comercial Pesquera Turística
**Latitud:**31.8522146
**Longitud:**-116.625788
"""
    names = {p["name"] for p in extract_structured_ports(text)}
    assert "Ensenada" in names
    assert "Bahía Colonet" not in names


def test_mayotte_annexe_i_keeps_maritime_ppc_only():
    from app.core.extract import extract_structured_ports, looks_like_port_catalog
    text = """
    ANNEXE I
    1. Liste des points de passage contrôlés :
    | SITES | MODALITÉS D'OUVERTURE |
    | Frontières aériennes |
    | Dzaoudzi-Pamandzi | Permanent |
    Frontières maritimes
    Dzaoudzi
    Permanent
    2. Liste des documents sur lesquels il n'est pas apposé de cachet
    """
    ports = extract_structured_ports(text)
    names = {p["name"] for p in ports}
    assert looks_like_port_catalog(text)
    assert "Dzaoudzi" in names
    assert not any("Pamandzi" in n for n in names)


def test_uk_pleasure_list_keeps_every_named_port():
    from app.core.extract import extract_structured_ports
    text = """
    ## Pleasure craft ports
    - Falmouth
    - Plymouth
    - Cowes
    - Ramsgate
    All UK pleasure craft ports are Ports of Entry.
    """
    names = {p["name"] for p in extract_structured_ports(text)}
    assert {"Falmouth", "Plymouth", "Cowes", "Ramsgate"} <= names


def test_venezuela_ley_prose_is_not_a_capitania_list():
    from app.core.extract import extract_structured_ports, looks_like_port_catalog
    text = """
    Artículo 12. La Capitanía de Puerto estará a cargo de un funcionario
    denominado Capitán de Puerto. Serán atribuciones del Capitán de Puerto
    ordenar la inspección. Capitanía de Puerto el permiso de zarpe, dentro
    de las doce horas. En cada circunscripción acuática.
    """
    ports = extract_structured_ports(text)
    names = " ".join(p["name"] for p in ports).casefold()
    assert "estará" not in names
    assert "permiso" not in names
    assert "zarpe" not in names
    assert "circunscrip" not in names
    assert looks_like_port_catalog(text) is False


def test_france_landing_prose_is_not_a_port_list():
    from app.core.extract import extract_structured_ports
    text = (
        "Si vous arrivez dans des ports de plaisance de français depuis un "
        "port situé en dehors de l’espace Schengen, ou dans des ports de "
        "plaisance de qui ne sont pas des points de passage frontaliers, "
        "la liste des ports de plaisance de non PPF est reprise sur la "
        "liste des 53 ports bénéficiant d'une procédure simplifiée."
    )
    ports = extract_structured_ports(text)
    names = " ".join(p["name"] for p in ports).casefold()
    assert "français" not in names
    assert "non ppf" not in names
    assert not any("qui ne sont" in (p["name"] or "").casefold() for p in ports)


def test_nz_pofa_tables_yield_seaports_not_arrival():
    from app.core.extract import catalog_is_sufficient, extract_structured_ports
    text = """
    ## Approved ports
    | Opua Marine Park |
    | --- |
    | Approved vessels | Private recreational vessels |
    | Port of Auckland Limited |
    | --- |
    | Approved vessels | Commercial vessels |
    | Port of Tauranga |
    | --- |
    | Approved vessels | Cargo |
    | Wellington Harbour |
    | --- |
    | Approved vessels | Ferry |
    | Lyttelton Harbour |
    | --- |
    | Approved vessels | Cargo and cruise |
    | Timaru Port |
    | --- |
    | Approved vessels | Cargo |
    | Port Chalmers |
    | --- |
    | Approved vessels | Cargo |
    | South Port, Bluff |
    | --- |
    | Approved vessels | Cargo |
    Unlike the port of arrival or the port of Christchurch airport.
    """
    ports = extract_structured_ports(text)
    names = {p["name"] for p in ports}
    assert "Opua Marine Park" in names
    assert "Lyttelton Harbour" in names
    assert "Port of Auckland Limited" in names
    assert "arrival" not in {n.casefold() for n in names}
    assert "Christchurch" not in names
    assert catalog_is_sufficient(ports, text)


def test_nz_pofa_tinyfish_table_rows():
    from app.core.extract import catalog_is_sufficient, extract_structured_ports
    from app.services.poe_seed_enrich import harvest_bu_catalog
    text = """
    Places of first arrival – seaports
    ## Approved ports
    ## Northland
    | Port contact (website) | Bay of Islands Marina |
    | Northport | |
    ## Auckland
    | Port of Auckland Limited | |
    | Chelsea Port, Birkenhead | |
    ## Tauranga
    | Port of Tauranga | |
    ## Napier
    | Napier Port | |
    ## Dunedin (Port Chalmers)
    | Port Chalmers | |
    ## Invercargill (Bluff and Tiwai Point)
    | South Port, Bluff | |
    """
    ports = extract_structured_ports(text)
    names = {p["name"] for p in ports}
    assert "Port of Tauranga" in names
    assert "Northport" in names
    assert "Bay of Islands Marina" in names
    assert catalog_is_sufficient(ports, text)
    h = harvest_bu_catalog(
        {"https://www.mpi.govt.nz/pofa": {"text": text, "blocked": False}},
        ["https://www.mpi.govt.nz/pofa"])
    assert h["sufficient"] is True


def test_nc_clearance_bureau_and_sx_generic_marina():
    from app.core.extract import extract_structured_ports
    nc = extract_structured_ports(
        "se présenter au bureau de douane de Nouméa Port pour une clearance."
    )
    assert any("Nouméa" in (p["name"] or "") for p in nc)
    sx = extract_structured_ports("Port de plaisance de Marina\nGreat Bay.")
    assert not any(p["name"].casefold() in {"marina", "port de plaisance de marina"}
                   for p in sx)


def test_albania_dogana_kartela_yields_four_seaports():
    from app.core.extract import catalog_is_sufficient, extract_structured_ports
    # PDF layout: “Porti detar” then the toponym on the next line.
    text = (
        "6. Ku mund të aplikoj?\n"
        "Degët doganore mbikëqyrëse (pranë porteve detare)\n"
        "Dega Doganore Durrës\nDega Doganore Lezhë\n"
        "Dega Doganore Vlorë\nDega Doganore Sarandë\n"
        "1. Durrës - Porti detar\nDurrës\n"
        "2. Lezhë - Porti detar\nShëngjin\n"
        "3. Vlorë - Porti detar Vlorë\n"
        "4. Sarandë - Porti detar\nSarandë\n"
        "Autorizim për përjashtimin nga detyrimet e akcizës për anijet e peshkimit."
    )
    ports = extract_structured_ports(text)
    names = {p["name"] for p in ports}
    assert names == {
        "Porti detar Durrës",
        "Porti detar Shëngjin",
        "Porti detar Vlorë",
        "Porti detar Sarandë",
    }
    assert "Lezhë" not in names and "Porti detar Lezhë" not in names
    assert catalog_is_sufficient(ports, text)
    flat = extract_structured_ports(
        "1. Durrës - Porti detar Durrës 2. Lezhë - Porti detar Shëngjin "
        "3. Vlorë - Porti detar Vlorë 4. Sarandë - Porti detar Sarandë. "
        "Degët doganore mbikëqyrëse pranë porteve detare, anijet e peshkimit."
    )
    assert {p["name"] for p in flat} == names


def test_marina_including_headings_work_without_country_name():
    """Generalized EG form: “X Marina:” titles after including."""
    from app.core.extract import catalog_is_sufficient, extract_structured_ports
    text = (
        "The state has established specialized marinas on its beaches, including:\n"
        "Alpha Bay Marina:\nBerths for 80 yachts.\n"
        "Beta Cove Marina:\nMediterranean quay.\n"
        "Gamma Roads Marina:\nReceives foreign yachts.\n"
        "Delta Point Marina:\nNorth coast.\n"
        "The State Sets a Legislative Framework For Yacht Tourism:\n"
        "Prime Minister's decision No. 1 of 2020. Sharm El Sheikh, Nuweiba."
    )
    ports = extract_structured_ports(text)
    names = {p["name"] for p in ports}
    assert names == {
        "Alpha Bay Marina", "Beta Cove Marina",
        "Gamma Roads Marina", "Delta Point Marina",
    }
    assert "Sharm El Sheikh" not in names
    assert catalog_is_sufficient(ports, text)


def test_egypt_sis_yacht_headings_yield_five_marinas():
    from app.core.extract import catalog_is_sufficient, extract_structured_ports
    text = (
        "Egypt has been keen to encourage yachting tourism and establish "
        "specialized marinas on its beaches, including:\n"
        "Hurghada Marina:\n"
        "Located in the heart of Hurghada city next to the Grand Mina Mosque.\n"
        "Marassi Yacht Marina:\n"
        "A marina for yachts in the yacht port of Marassi on the Mediterranean.\n"
        "Taba Heights Marina:\n"
        "The marina is located in Moqbela, south of Taba.\n"
        "Abu Teeg Marina (El Gouna Marina):\n"
        "Located within the El Gouna Resort area, 21 km north of Hurghada.\n"
        "Porto Marina North Coast:\n"
        "Located in Alamein city on the North Coast.\n"
        "The State Sets a Legislative Framework For Yacht Tourism:\n"
        "Prime Minister's decision No. 2721 of 2022. "
        "Yachts storing at Ismailia Yacht Marina for more than 90 days. "
        "Cities such as: Sharm El Sheikh, Nuweiba, Dahab and Taba."
    )
    ports = extract_structured_ports(text)
    names = {p["name"] for p in ports}
    assert names == {
        "Hurghada Marina",
        "Marassi Yacht Marina",
        "Taba Heights Marina",
        "Abu Teeg Marina (El Gouna Marina)",
        "Porto Marina North Coast",
    }
    assert "Marassi on" not in names
    assert "Sharm El Sheikh" not in names
    assert "Ismailia Yacht Marina" not in names
    assert catalog_is_sufficient(ports, text)
    flat = extract_structured_ports(text.replace("\n", " "))
    assert {p["name"] for p in flat} == names


def test_sint_maarten_customs_examples_yield_simpson_and_great_bay():
    from app.core.extract import catalog_is_sufficient, extract_structured_ports
    # Texte officiel Customs.aspx — SharePoint encode « : » en &#58;.
    text = (
        "Where Do Customs Officers Perform Their Duties? "
        "Sint Maarten is a place where the Customs Officer can exercise his "
        "authority. Some examples include&#58; Princess Juliana International "
        "Airport, Simpsonbay Marina, Port de Plaisance Marina, Il de Sol "
        "Marina, Cupecoy Marina, Captain Olivers Marina, Greatbay harbor, "
        "including the Cruise Terminal, and the Post Office and the entire "
        "coastline. Customs Officers have access to these areas at all times."
    )
    ports = extract_structured_ports(text)
    names = {p["name"] for p in ports}
    assert names == {
        "Simpsonbay Marina",
        "Port de Plaisance Marina",
        "Il de Sol Marina",
        "Cupecoy Marina",
        "Captain Olivers Marina",
        "Greatbay harbor",
        "Cruise Terminal",
    }
    assert not any("Juliana" in n or "Airport" in n or "Post" in n
                   or "coastline" in n.casefold() for n in names)
    assert catalog_is_sufficient(ports, text)
    short = extract_structured_ports(
        "authorized ports in Sint Maarten. Princess Juliana International Airport."
    )
    assert short == []


def test_attachments_beat_remaining_serp_queue(monkeypatch):
    landing = (
        "https://www.douane.gouv.fr/particuliers/vous-naviguez/"
        "vous-naviguez-en-provenance-ou-destination-dun-pays-non-membre-de"
    )
    liste = ("https://www.douane.gouv.fr/sites/default/files/2026-07/06/"
             "Liste-ports-de-plaisance-eligibles.pdf")
    carte = ("https://www.douane.gouv.fr/sites/default/files/uploads/files/"
             "carte-PPF-maritimes.pdf")
    html = f'<a href="{carte}">PPF</a><a href="{liste}">liste</a>'
    junk = [
        "https://www.douane.gouv.fr/french-customs-information-available-english",
        "https://www.douane.gouv.fr/sites/default/files/2018-11/10-questions-before-exporting-en.pdf",
        "https://www.service-public.fr/particuliers/vosdroits/F1234",
    ]

    async def fake_cascade(url, min_chars=200, log=None):
        if url in (liste, carte):
            return {"text": "1.- Port Alpha latitude: 46.1 longitude: -1.1\n" * 8,
                    "html": "", "md5": "p", "level": "N1", "blocked": False}
        if url == landing:
            return {"text": "Formalités plaisance. " * 40, "html": html,
                    "md5": "h", "level": "N1", "blocked": False}
        return {"text": "page d'accueil douanes " * 40, "html": "",
                "md5": "j", "level": "N1", "blocked": False}

    monkeypatch.setattr(poe, "extract_cascade", fake_cascade)
    official = ([{"url": landing, "domain": "douane.gouv.fr"}]
                + [{"url": u, "domain": "douane.gouv.fr"} for u in junk])
    _t, _h, used, _e = asyncio.run(poe._collect_texts(official, lambda m: None, max_fetch=5))
    blob = " ".join(c["url"] for c in used)
    assert liste in blob
    assert carte in blob
