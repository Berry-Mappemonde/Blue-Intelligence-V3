"""C2 needs designating context, and a zone's own name is never a port-name variant.

Run 20261103-143422-608d85, Martinique: "Fort-de-France (Martinique)" reached C2 on its
variant "martinique" (the zone's name) next to "dédouanement" in the octroi de mer
circular. Runs 608d85 and 20261103-114750-f3993a, Cape Verde: Porto da Praia and Praia
reached C2 on the WCO clearance-time study (Clément, 3 Nov 2026: a study describing its
scope, or a customs office's name, is not designation). Quotes are verbatim from the
runs' fetched texts. Offline.
"""
import pytest

from app.adapters import evidence_store
from app.agents import registry_check as rc
from app.services import v3_certify

# douane.gouv.fr, Circulaire octroi de mer 10/07/2025 (608d85).
OCTROI = ("r 1995, les régions de Guadeloupe et de Martinique constituent un territoire fiscal "
          "unique au regard de la TVA, des accises et de l’octroi de mer.\n[109] Les "
          "marchandises produites dans la région de Guadeloupe ou importées dans ce DROM et qui "
          "sont expédiées ou livrées dans la région de Martinique, et vice-versa, ne sont donc "
          "pas soumises à des formalités de dédouanement.\nLes expéditions s’entendent de "
          "l’envoi des biens depuis le DROM où les formalités d'")
OCTROI_URL = "https://www.douane.gouv.fr/sites/default/files/2025-07/16/Circulaire-octroi-de-mer10072025.pdf"
# mf.gov.cv, "Estudo de Tempo de Desalfandegamento" (WCO).
STUDY_SCOPE = ("iente; > Enapor; > Guarda Fiscal; > Despachantes Oficiais e Caixeiros Despachantes; "
               "> Agências Marítimas Na fase inicial, o grupo de trabalho determinou o âmbito do "
               "estudo, tendo decidido analisar todas as fases do processo de desalfandegamento "
               "das mercadorias, iniciando com a chegada do navio, até a saída efetiva da "
               "mercadoria no Porto da Praia, um dos principais portos de Cabo Verde, "
               "desempenhando o duplo papel de afirmação no mu")
STUDY_TITLE = (" : Estudo de Tempo de Desalfandegamento Alfândega da Praia ORGANISATION MONDIALE "
               "DES DOUANES WORLD CUSTOMS ORGANIZATION 2 ESTUDO DE TEMPO DE DESALFANDEGAMENTO. "
               "ALFÂNDEGA DA PRAIA SUMÁRIO A PREFÁCIO 04 B SUMÁRIO EXECUTIVO 06 C ")
STUDY_URL = "https://www.mf.gov.cv/documents/54571/273413/Cabo%20Verde_Estudo%20de%20Tempo%20de%20Desalfandegamento_1.pdf"
PRAIA_EXCERPT = ("tendo decidido analisar todas as fases do processo de desalfandegamento das "
                 "mercadorias, iniciando com a chegada do navio, até a saída efetiva da mercadoria "
                 "no Porto da Praia, um dos principais portos de Cabo Verde")
# douane.gouv.fr, liste des points de clearance agréés (608d85): rows of an official list.
CLEARANCE_LIST = (
    "ontact@portetangzabricots.fr Accueil Permanence 7h-17h 6h-18h Sea Services ShipChandler "
    "109, rue Ernest Deproge + 596 596 702 669  seaservices972@orange.fr lun-ven "
    "sam dim 8h30-17h 8h30-12h30 fermé Le François Capitainerie Marina - Pointe Bateau "
    "+ 596 596 542 954  glennjeanjoseph@yahoo.fr tous les jours 8h-18h Le Marin "
    "Port de plaisance Capitainerie du port (nouvelle extension) +596 596 748 383  "
    "contact@marina-martinique.fr lun-sam dim et jours fériés 8h-17h8h-12h30 Station-service "
    "CAP Ponton N°3 + 596 596 765 161 lun-dim et jours fériés 7h30-13h/14h-17h Point de "
    "clearance ouvert tous les jours jusqu’à 16h30 Prestataire Adresse Contact Horaires "
    "d’ouverture Saint Anne Snack BOUBOU 32 Rue Saffache (+)596 596 762 846 "
    "(+)596 696 728 818  cyber277@orange.fr Saint-Pierre Office municipal du "
    "tourisme Villa Sainte-Anne Rue Victor Hugo (+)596 596 738 405 (+)596 596 789 "
    "519  omt.saint-pierre@orange.fr L’alsace en Kay 75 rue Gabriel Peri (+)596 596 "
    "675 365 (+)596 596 776867  phm.glaciere@hotmail.fr Trois-ilets Régie Port de "
    "Plaisance Marina de la pointe du bout (+)596 596 660 774 (+)596 596 600 50 "
    " marina.3ilets@marina3ilets.fr Trinité Brigade de surveillance des douanes Cité des do")
CLEARANCE_URL = "https://www.douane.gouv.fr/sites/default/files/2026-01/30/liste-points-clearance-agrees-mise-a-jour.pdf"
# govt.lc, Customs (Control and Management) Act, list of subsidiary legislation.
SOUFRIERE_ORDER = ("/2000 in force 1 March 2000 Amended by S.I. 67/2001 in force 2 June 2001 Amended "
                   "by S.I. 53/2002 in force 2 June 2002 Amended by S.I.172/2005 in force 28 "
                   "December 2005 Amended by Act 25 of 2005 in force 21 November 2005 CUSTOMS (PORT "
                   "OF SOUFRIERE BAY) ORDER – Section 12 Statutory Instrument 60/1994 in force 1 "
                   "October 1994 CUSTOMS REGULATIONS – Sections 23, 33, 50, 52 and 141")
ACT_URL = "https://www.govt.lc/media.govt.lc/www/resources/legislation/CustomsControlManagementAct.pdf"

MARTINIQUE = {"mrgid": 33178, "name": "Martinique", "iso2": "MQ", "sov_iso2": "FR"}
CAPE_VERDE = {"mrgid": 8362, "name": "Cape Verde", "iso2": "CV"}
WL_MQ = ["douane.gouv.fr", "martinique.gouv.fr"]
WL_CV = ["gov.cv", "enapor.cv"]


def _port(name, mrgid, urls, judge=None, lat=14.59, lon=-61.0):
    return {"name": name, "mrgid": mrgid, "extraction_engine": "nightshift",
            "extraction_agreement": False, "source_urls": urls,
            "judge": judge or {"verdict": "unclear", "quoted_excerpt": "", "excerpt_verified": False},
            "lat": lat, "lon": lon, "spatial_kind": "in_eez", "geocode_arbitration": "single"}


# --- 1. a zone, territory or sovereign name is never a port-name variant ---------------

def test_fort_de_france_martinique_has_no_martinique_variant():
    assert rc.grounding_variants("Fort-de-France (Martinique)") == [("fort", "de", "france")]


def test_fort_de_france_martinique_is_not_c2_on_the_octroi_circular():
    assert v3_certify.entry_mentions("Fort-de-France (Martinique)", [OCTROI]) == []
    assert v3_certify.all_mentions("Fort-de-France (Martinique)", [OCTROI]) == []
    assert evidence_store.build_records(33178, ["Fort-de-France (Martinique)"],
                                        [{"url": OCTROI_URL, "text": OCTROI}]) == []
    port = _port("Fort-de-France (Martinique)", 33178,
                 [OCTROI_URL, "https://www.martinique.gouv.fr/x.pdf"])
    texts = [{"url": OCTROI_URL, "text": OCTROI}]
    assert v3_certify.level_from_v2(port, whitelist=WL_MQ, texts=texts, zone=MARTINIQUE) == "C1"


@pytest.mark.parametrize("name, dropped", [
    ("Fort-de-France (Martinique)", "martinique"),
    ("Puerto de Las Palmas (Canarias)", "canarias"),       # short form of Islas Canarias
    ("Puerto de Arrecife (Islas Canarias)", "islas canarias"),
    ("Port of Spain", "spain"),                             # stripped variant, a country
    ("Port Hercule (Monaco)", "monaco"),
    ("Mindelo (Cabo Verde)", "cabo verde"),                 # the sovereign in Portuguese
    ("Marigot (Saint-Martin)", "saint martin"),
])
def test_place_names_are_never_variants(name, dropped):
    assert dropped not in {" ".join(v) for v in rc.grounding_variants(name)}


def test_the_zone_s_own_names_from_the_caller():
    # "Cape Verde" (VLIZ) is not an ISO 3166 name: it comes with the zone.
    assert "cape verde" in {" ".join(v) for v in rc.grounding_variants("Porto Grande (Cape Verde)")}
    variants = rc.grounding_variants("Porto Grande (Cape Verde)",
                                     places=rc.zone_names(CAPE_VERDE))
    assert {" ".join(v) for v in variants} == {"porto grande", "grande"}
    assert "cape verde" in v3_certify._places({}, CAPE_VERDE)


@pytest.mark.parametrize("name", ["Gibraltar", "Monaco", "Martinique", "Singapore"])
def test_a_port_named_like_its_zone_keeps_its_whole_name(name):
    assert rc.grounding_variants(name) == [tuple(rc.name_key(name).split())]


# --- 2. C2 needs designating context: not a study --------------------------------------

def test_porto_da_praia_on_the_study_scope_is_not_entry_context():
    assert v3_certify.entry_mentions("Porto da Praia", [STUDY_SCOPE]) == []
    # Without the study rule the same text gave the entry context.
    assert v3_certify.entry_mentions("Porto da Praia", [STUDY_SCOPE],
                                     exclude=v3_certify.TARIFF_TERMS)


def test_praia_in_the_study_title_is_not_entry_context():
    assert v3_certify.entry_mentions("Praia", [STUDY_TITLE]) == []
    port = _port("Praia", 8362, [STUDY_URL, "https://enapor.cv/x"], lat=15.126, lon=-23.528)
    result = v3_certify.attach_confidence_level(
        dict(port), whitelist=WL_CV, texts=[{"url": STUDY_URL, "text": STUDY_TITLE}], zone=CAPE_VERDE)
    assert result["confidence_level"] == "C1"
    assert result["confidence_cap_reason"] == v3_certify.CAP_NOT_DESIGNATING


def test_a_judge_poe_on_the_study_excerpt_is_not_c2():
    # 608d85: the judge said "poe" on this excerpt, verified in the study.
    judge = {"verdict": "poe", "quoted_excerpt": PRAIA_EXCERPT, "excerpt_verified": True}
    port = _port("Porto da Praia", 8362, [STUDY_URL, "https://enapor.cv/x"], judge,
                 lat=14.912, lon=-23.501)
    texts = [{"url": STUDY_URL, "text": STUDY_SCOPE}]
    assert v3_certify.judge_excerpt_in_study(port, texts, WL_CV)
    result = v3_certify.attach_confidence_level(dict(port), whitelist=WL_CV, texts=texts,
                                                zone=CAPE_VERDE)
    assert result["confidence_level"] == "C1"
    assert result["confidence_cap_reason"] == v3_certify.CAP_NOT_DESIGNATING


# --- every current correct C2 stays ------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Capitainerie Marina - Pointe Bateau",
    "Port de plaisance Capitainerie du port (nouvelle extension)",
    "Régie Port de Plaisance Marina de la pointe du bout",
])
def test_rows_of_the_douane_clearance_list_stay_c2(name):
    assert v3_certify.entry_mentions(name, [CLEARANCE_LIST])
    port = _port(name, 33178, [CLEARANCE_URL, "https://www.martinique.gouv.fr/x.pdf"])
    assert v3_certify.level_from_v2(port, whitelist=WL_MQ, zone=MARTINIQUE,
                                    texts=[{"url": CLEARANCE_URL, "text": CLEARANCE_LIST}]) == "C2"


def test_the_soufriere_bay_order_stays_c2():
    assert v3_certify.entry_mentions("Soufriere Bay", [SOUFRIERE_ORDER])
    judge = {"verdict": "poe", "quoted_excerpt": "CUSTOMS (PORT OF SOUFRIERE BAY) ORDER – Section 12",
             "excerpt_verified": True}
    port = _port("Soufriere Bay", 8416, [ACT_URL, "https://www.customs.gov.lc/x"], judge,
                 lat=13.852, lon=-61.067)
    texts = [{"url": ACT_URL, "text": SOUFRIERE_ORDER}]
    assert not v3_certify.judge_excerpt_in_study(port, texts, ["govt.lc", "gov.lc"])
    assert v3_certify.level_from_v2(port, whitelist=["govt.lc", "gov.lc"], texts=texts,
                                    zone={"mrgid": 8416, "name": "Saint Lucia"}) == "C2"


@pytest.mark.parametrize("text", [
    # "report" is a customs act's verb, never a study.
    "The master of every vessel shall report on arrival at Castries, a port of entry.",
    # A study named far from the mention, on another line, does not count.
    "Ver o estudo anexo." + " " * 300 + "\nO porto da Praia é um porto de entrada.",
])
def test_designating_text_near_other_words_still_counts(text):
    name = "Castries" if "Castries" in text else "Porto da Praia"
    assert v3_certify.entry_mentions(name, [text]), name
