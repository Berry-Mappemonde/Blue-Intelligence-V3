"""Every query family speaks the polygon's language, and the shared entry terms cover
Portuguese and Spanish wording.

Run 20261102-224042-f9be8d: Cape Verde (pt) searched in Portuguese once (the first-round
template); its hints, site: lookups, Serper shot and lessons round were English. Offline.
"""
import pytest

from app.adapters import evidence_store
from app.agents import registry_check as rc
from app.services import poe_pipeline as poe
from app.services import v3_certify

FIELDS = {"hints", "site_pdf", "site_page", "serper", "lessons"}
# Polygons with their own branches in the query builders (they come before the families).
COUNTRY_BRANCHES = {"FR", "MX", "VE", "NZ", "NC", "NU", "SX", "YT", "GB", "EG", "AL"}
# One zone per language: the first ISO2 LANG_BY_ISO2 lists for it outside those branches.
ZONE_BY_LANG = {}
for _iso, _lang in poe.LANG_BY_ISO2.items():
    if _iso not in COUNTRY_BRANCHES:
        ZONE_BY_LANG.setdefault(_lang, {"mrgid": 990000 + len(ZONE_BY_LANG),
                                        "name": f"Testland {_iso}", "iso2": _iso, "sov_iso2": _iso})
LANGS = sorted(poe.QUERY_TEMPLATES)


def test_every_first_round_language_has_the_other_families():
    assert set(poe.LOCAL_SEARCH_TERMS) == set(poe.QUERY_TEMPLATES)
    assert {"fr", "es", "pt", "nl"} <= set(poe.LOCAL_SEARCH_TERMS)
    for lang, terms in poe.LOCAL_SEARCH_TERMS.items():
        assert set(terms) == FIELDS, lang
        assert len(terms["hints"]) == 2 and all("{place}" in h for h in terms["hints"]), lang
        assert all(isinstance(terms[k], str) and terms[k].strip() for k in FIELDS - {"hints"}), lang


@pytest.mark.parametrize("lang", LANGS)
def test_each_family_uses_the_zone_language(lang):
    zone = ZONE_BY_LANG[lang]
    terms = poe.LOCAL_SEARCH_TERMS[lang]
    assert poe.zone_search_lang(zone) == lang
    place = poe.serp_place_name(zone)
    hints = poe.default_search_hints(zone)
    assert hints[:2] == [h.format(place=place) for h in terms["hints"]]
    assert "customs act designated ports of entry" in hints[2]  # the EN legal hint stays
    assert poe.site_list_pdf_query("gov.example", zone).endswith(terms["site_pdf"])
    assert poe.site_list_page_query("gov.example", zone).endswith(terms["site_page"])
    shots = dict(poe.google_style_shots(zone))
    assert set(shots) == {"en", lang} and terms["serper"] in shots[lang]
    assert poe.lessons_learned_query(zone).endswith(terms["lessons"])


def test_cape_verde_searches_in_portuguese():
    cv = {"mrgid": 8362, "name": "Cape Verde", "iso2": "CV"}
    hints = " ".join(poe.default_search_hints(cv))
    assert "portos habilitados" in hints and "desalfandegamento" in hints
    assert "portos habilitados" in poe.site_list_pdf_query("enapor.cv", cv)
    assert ("pt", "Cape Verde portos de entrada oficiais lista iates .cv") in poe.google_style_shots(cv)
    assert "desalfandegamento" in poe.lessons_learned_query(cv)


def test_french_spanish_dutch_strings_are_the_ones_already_used():
    mq = {"mrgid": 33178, "name": "Martinique", "iso2": "MQ", "sov_iso2": "FR"}
    assert poe.default_search_hints(mq)[:2] == [
        "ports d'entrée officiels liste douane décret Martinique",
        "yacht marina plaisance liste officielle Martinique"]
    # + the French document-type name (fix/v3-sovereign-domains)
    assert poe.site_list_pdf_query("d.fr", mq) == (
        "site:d.fr filetype:pdf (liste ports d'entrée OR décret OR arrêté "
        "OR \"points de clearance agréés\")")
    assert poe.site_list_page_query("d.fr", mq) == (
        "site:d.fr (ports d'entrée OR liste douane OR \"points de clearance agréés\")")
    co = {"mrgid": 8426, "name": "Colombia", "iso2": "CO", "sov_iso2": "CO"}
    assert poe.default_search_hints(co)[:2] == [
        "Colombia puertos habilitados decreto lista oficial",
        "Colombia yates marinas turismo náutico lista oficial"]
    assert poe.site_list_pdf_query("d.co", co) == "site:d.co filetype:pdf (puertos habilitados OR decreto lista)"
    assert poe.site_list_page_query("d.co", co) == "site:d.co (puertos habilitados OR decreto)"
    assert ("es", "Colombia puertos habilitados lista oficial yates .co") in poe.google_style_shots(co)
    nl = {"mrgid": 5668, "name": "Netherlands", "iso2": "NL", "sov_iso2": "NL"}
    # local-language name in the local-language query (fix/v3-dual-domain-languages)
    assert ("nl", "Nederland officiële lijst toegangshavens pleziervaartuigen .nl") in poe.google_style_shots(nl)


def test_zones_without_a_language_keep_the_english_queries():
    for zone in ({"mrgid": 5673, "name": "Croatia", "iso2": "HR", "sov_iso2": "HR"},
                 {"mrgid": 8416, "name": "Saint Lucia", "iso2": "LC", "sov_iso2": "LC"}):
        place = poe.serp_place_name(zone)
        assert poe.default_search_hints(zone) == poe.family_default_hints(place)
        assert [lang for lang, _ in poe.google_style_shots(zone)] == ["en"]
        assert poe.lessons_learned_query(zone).endswith(
            "formalités plaisance sPCR boarding stations clearance stations")


@pytest.mark.parametrize("lang, name, text", [
    ("pt", "Palmeira", "Nos termos do decreto, são portos habilitados para embarcações de "
                       "recreio estrangeiras: Porto da Palmeira, Porto Grande e Praia."),
    ("pt", "Mindelo", "O desalfandegamento das embarcações de recreio efetua-se no porto do Mindelo."),
    ("es", "Arrecife", "El despacho de entrada de las embarcaciones de recreo se realiza en "
                       "el puerto de Arrecife."),
    ("es", "Las Palmas", "Son puertos habilitados el puerto de Las Palmas y el de Santa Cruz."),
])
def test_shared_entry_terms_make_judge_evidence(lang, name, text):
    # The evidence store (judge evidence) and the registry check read the shared list.
    assert rc.entry_mention(text, rc.grounding_variants(name))
    records = evidence_store.build_records(8362, [name], [{"url": "https://www.gov.cv/x", "text": text}])
    assert len(records) == 1 and records[0]["matched_name"]
    # #29's C2 entry context reads the same list (porto(s) habilitado(s) moved there).
    assert v3_certify.entry_mentions(name, [text])


def test_moved_terms_are_not_duplicated_in_certify():
    shared = set(rc.STRONG_ENTRY_TERMS)
    assert {"porto habilitado", "portos habilitados", "desalfandegamento", "despacho de entrada",
            "puertos habilitados"} <= shared
    assert not shared & set(v3_certify.ENTRY_TERMS_STRONG)
