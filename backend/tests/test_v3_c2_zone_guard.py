"""C2 guards (pilot: 1 of 19 C2 ports had correct per-port evidence).

a) zone attribution: the evidence row/section names this zone's territory, or the
   port geocodes in the zone (polygon / coastal buffer); else C1, "evidence not
   attributable to zone".
b) two official hosts need a per-port entry context; tariff schedules do not count.

Synthetic fixtures only. No network, no Mongo.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import v3_certify  # noqa: E402

WL = ["gouv.fr"]
GP = {"mrgid": 33177, "name": "Guadeloupe"}  # territories.json: guadeloupe
CV_WL = ["gov.cv"]

DECREE = "https://www.douane.gouv.fr/arrete-ports-entree.pdf"
PREF = "https://www.guadeloupe.gouv.fr/arrete-deshaies"
NATIONAL_DECREE = (
    "Arrêté du 3 mars 2025 portant désignation des ports d'entrée en outre-mer\n"
    "Article 1\n"
    "Martinique : Le Marin, port d'entrée désigné pour la plaisance.\n"
    "Martinique : Fort-de-France, port d'entrée désigné.\n"
    "Guadeloupe : Pointe-à-Pitre, port d'entrée désigné.\n"
)
CUSTOMS_ORDER = (
    "Préfecture de la Guadeloupe — Arrêté préfectoral n° 2025-114\n"
    "Le port de Deshaies est désigné port d'entrée pour les navires de plaisance "
    "en provenance de l'étranger ; les formalités sont accomplies au bureau de douane."
)


def _port(name, urls, **kw):
    port = {"name": name, "source_urls": urls, "extraction_engine": "llm",
            "mrgid": GP["mrgid"], "zone_name": GP["name"]}
    port.update(kw)
    return port


def test_tariff_list_of_ports_is_c1():
    tariff = (
        "Tarifário portuário 2025 — taxas de utilização\n"
        "Tipo II: Porto Novo, cais da alfândega — 1 200 ECV\n"
        "Tipo III: Tarrafal, porto de pesca — 800 ECV\n"
    )
    urls = ["https://www.portos.gov.cv/tarifario.pdf", "https://www.mtt.gov.cv/portos"]
    texts = [{"url": u, "text": tariff} for u in urls]
    port = {"name": "Porto Novo", "source_urls": urls, "extraction_engine": "llm",
            "zone_name": "Cabo Verde", "spatial_kind": "in_eez"}
    out = v3_certify.assess_port(port, whitelist=CV_WL, texts=texts)
    assert out["confidence_level"] == "C1"
    assert out["confidence_cap_reason"] == v3_certify.CAP_NO_ENTRY_CONTEXT
    assert v3_certify.entry_mentions("Porto Novo", [tariff]) == []
    # Same port, same hosts, an entry sentence instead of a price row: C2.
    entry = "O Porto Novo é porto de entrada designado; desembaraço aduaneiro no cais."
    ok = [{"url": urls[0], "text": entry}, {"url": urls[1], "text": tariff}]
    assert v3_certify.level_from_v2(port, whitelist=CV_WL, texts=ok) == "C2"


def test_quote_from_another_territorys_row_of_a_national_decree_is_c1():
    texts = [{"url": DECREE, "text": NATIONAL_DECREE}]
    urls = [DECREE, PREF]
    # Le Marin is a Martinique port, listed in the same national decree.
    port = _port("Le Marin", urls)
    out = v3_certify.assess_port(port, whitelist=WL, texts=texts, zone=GP)
    assert out["confidence_level"] == "C1"
    assert out["downgraded_from"] == "C2"
    assert out["confidence_cap_reason"] == "evidence not attributable to zone"
    # The judge quoting that row does not attribute it to Guadeloupe either.
    judged = _port("Le Marin", [DECREE], judge={
        "verdict": "poe", "excerpt_verified": True, "reason": "decree",
        "quoted_excerpt": "Le Marin, port d'entrée désigné pour la plaisance"})
    assert v3_certify.level_from_v2(judged, whitelist=WL, texts=texts, zone=GP) == "C1"
    # The Guadeloupe row of the same decree is attributable.
    ours = _port("Pointe-à-Pitre", urls)
    assert v3_certify.level_from_v2(ours, whitelist=WL, texts=texts, zone=GP) == "C2"
    # Geocoded inside this zone's polygon: attributable whatever the row says.
    geo = _port("Le Marin", urls, spatial_kind="in_eez")
    assert v3_certify.level_from_v2(geo, whitelist=WL, texts=texts, zone=GP) == "C2"


def test_customs_order_designating_the_port_keeps_c2():
    texts = [{"url": PREF, "text": CUSTOMS_ORDER}]
    port = _port("Deshaies", [PREF, DECREE])
    out = v3_certify.assess_port(port, whitelist=WL, texts=texts, zone=GP)
    assert out["confidence_level"] == "C2"
    assert "confidence_cap_reason" not in out
    # attach_confidence_level writes the reason only when the guard fires.
    v3_certify.attach_confidence_level(port, whitelist=WL, texts=texts, zone=GP)
    assert port["confidence_level"] == "C2" and "confidence_cap_reason" not in port
    capped = _port("Le Marin", [DECREE, PREF])
    v3_certify.attach_confidence_level(capped, whitelist=WL, zone=GP,
                                       texts=[{"url": DECREE, "text": NATIONAL_DECREE}])
    assert capped["confidence_cap_reason"] == "evidence not attributable to zone"
    assert "confidence_cap_reason" in v3_certify.LEVEL_FIELDS


def test_non_official_text_is_not_entry_context():
    texts = [{"url": "https://navily.com/deshaies", "text": CUSTOMS_ORDER}]
    port = _port("Deshaies", [PREF, DECREE])
    assert v3_certify.level_from_v2(port, whitelist=WL, texts=texts, zone=GP) == "C1"


def test_entry_mention_defaults_unchanged():
    """registry_check.entry_mention keeps its behaviour without the extra arguments."""
    from app.agents.registry_check import entry_mention, grounding_variants

    tariff = "Tarifa: port of entry Porto Novo, tipo II."
    assert entry_mention(tariff, grounding_variants("Porto Novo"))
    assert entry_mention(tariff, grounding_variants("Porto Novo"),
                         exclude=v3_certify.TARIFF_TERMS) is None
    # "ouvert au trafic international" is an entry term for certification only.
    text = "Le port de Deshaies est ouvert au trafic international."
    assert entry_mention(text, grounding_variants("Deshaies")) is None
    assert v3_certify.entry_mentions("Deshaies", [text])


def test_pipeline_passes_zone_texts_to_the_certifier():
    import inspect

    from app.services import poe_pipeline

    src = inspect.getsource(poe_pipeline)
    calls = [line for line in src.splitlines() if "attach_confidence_level(" in line
             and "import" not in line]
    assert calls
    for line in calls:
        start = src.index(line)
        call = src[start:src.index(")", src.index("attach_confidence_level(", start)) + 1]
        assert "texts=" in call or "texts=" in src[start:start + 300], line
    texts = poe_pipeline.certify_texts(
        "[SOURCE: https://www.guadeloupe.gouv.fr/a]\nLe port de Deshaies est désigné port d'entrée.",
        [{"url": "https://www.guadeloupe.gouv.fr/a"}])
    assert texts and texts[0]["url"] == "https://www.guadeloupe.gouv.fr/a"
