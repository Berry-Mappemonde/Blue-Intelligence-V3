"""A port name variant matches whole words written as a name, never a common word.

Run 20261103-114750-f3993a: Porto Novo (Cape Verde) reached C2 on "os novos que venham a
surgir" in the WCO clearance-time study (mf.gov.cv): "Porto" is a facility word, so the
variant left was "novo", and the plural "s" matched "novos" next to "DESALFANDEGAMENTO".
Offline.
"""
import pytest

from app.adapters import evidence_store
from app.agents import registry_check as rc
from app.services import v3_certify

# Verbatim from the study as fetched in that run (evidence row "novo", mf.gov.cv).
STUDY = ("continua; Implementação das recomendações deste estudo para avaliar as melhorias "
         "alcançadas, bem como identificar os problemas que persistem e os novos que venham a "
         "surgir. 06 / ENSINAMENTOS DO ESTUDO CAPÍTULO SEIS ESTUDO DE TEMPO DE "
         "DESALFANDEGAMENTO. ALFÂNDEGA DA PRAIA 06. ENSINAMENTOS DO ESTUDO")
GOV = "https://www.mf.gov.cv/documents/54571/273413/Cabo%20Verde_Estudo%20de%20Tempo%20de%20Desalfandegamento_1.pdf"
WL = ["gov.cv", "enapor.cv"]


def _mention(name, text):
    return rc.entry_mention(text, rc.grounding_variants(name),
                            extra_strong=v3_certify.ENTRY_TERMS_STRONG,
                            extra_weak=v3_certify.ENTRY_TERMS_WEAK,
                            exclude=v3_certify.TARIFF_TERMS)


def test_porto_novo_is_not_os_novos():
    assert rc.entry_mention(STUDY, rc.grounding_variants("Porto Novo")) is None
    assert _mention("Porto Novo", STUDY) is None
    assert v3_certify.entry_mentions("Porto Novo", [STUDY]) == []
    assert v3_certify.all_mentions("Porto Novo", [STUDY]) == []
    # Judge evidence: no record for Porto Novo from this text.
    records = evidence_store.build_records(8362, ["Porto Novo"], [{"url": GOV, "text": STUDY}])
    assert records == []


def test_porto_novo_c2_falls_to_c1_on_that_text():
    port = {"name": "Porto Novo", "mrgid": 8362, "extraction_engine": "nightshift",
            "extraction_agreement": False, "source_urls": [GOV, "https://enapor.cv/x"],
            "judge": {"verdict": "unclear", "quoted_excerpt": "", "excerpt_verified": False},
            "lat": 17.019, "lon": -25.065, "spatial_kind": "in_eez",
            "geocode_arbitration": "single"}
    texts = [{"url": GOV, "text": STUDY}]
    assert v3_certify.level_from_v2(port, whitelist=WL, texts=texts) == "C1"


def test_praia_in_the_same_title_is_unchanged():
    # "ALFÂNDEGA DA PRAIA" is written as a name: this rule does not touch it.
    m = _mention("Praia", STUDY)
    assert m and m["matched_name"] == "praia" and m["entry_term"] == "desalfandegamento"
    assert _mention("Porto da Praia", STUDY)["matched_name"] == "praia"


@pytest.mark.parametrize("name, text", [
    ("Porto Novo", "O desalfandegamento das embarcações de recreio faz-se no Porto Novo."),
    ("Porto Novo", "PORTO NOVO: porto habilitado para embarcações de recreio."),
    ("Praia", "O desalfandegamento faz-se no porto da Praia."),
    ("Porto Inglês", "Porto Inglês é um porto habilitado."),
    ("Port of Spain", "Yachts clear customs at the port of entry of Spain's capital? No: "
                      "Port of Spain is a port of entry."),
    ("Las Palmas", "Son puertos habilitados el puerto de Las Palmas y el de Santa Cruz."),
    ("Mindelo", "O desalfandegamento das embarcações de recreio efetua-se no porto do Mindelo."),
])
def test_names_still_match(name, text):
    assert _mention(name, text), name


@pytest.mark.parametrize("name, text", [
    # Plural "s" on a one-word variant.
    ("Porto Novo", "Os navios novos fazem o desalfandegamento no porto."),
    # A one-word name written as a common word.
    ("Praia", "O desalfandegamento das embarcações faz-se na praia ou no porto."),
    # Stripped "ingles" (English) and the "ingle" fragment.
    ("Porto Inglês", "O formulário de desalfandegamento está disponível em inglês no porto."),
    # Capitalized at the start of a sentence, lower case elsewhere in the same text.
    ("Porto Novo", "Novo regulamento de desalfandegamento no porto. O regulamento novo entra em vigor."),
])
def test_common_words_do_not_match(name, text):
    assert _mention(name, text) is None, name
    assert v3_certify.entry_mentions(name, [text]) == []


def test_no_one_word_singular_fragment():
    assert [tuple(v) for v in rc.grounding_variants("Cannes")] == [("cannes",)]
    keys = [tuple(v) for v in rc.grounding_variants("Porto Inglês")]
    assert ("ingle",) not in keys and ("ingles",) in keys
    # Several words keep their singular form ("Las Palma" for "Las Palmas").
    assert ("las", "palma") in [tuple(v) for v in rc.grounding_variants("Las Palmas")]
