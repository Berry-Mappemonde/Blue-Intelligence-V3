"""One definition of "official": the zone's PoE whitelist; and reg. 136's boarding stations
are entry wording.

Run 20261103-143422-608d85, Saint Lucia: stlucia.org is on the zone's whitelist (#39), was
read, and lists the "Entry & Clearance Ports", but evaluator.is_official_source (a gov-label
heuristic) said no, so evidence_store.build_records dropped it and the judge never saw it.
The Customs Regulations (Cap. 15.05, reg. 136) name the boarding stations, a wording no entry
term matched. Quotes are verbatim from the run's fetched texts. Offline.
"""
import pytest

from app.adapters import evidence_store
from app.agents import registry_check as rc
from app.agents.evaluator import is_official_source
from app.services import poe_pipeline as poe
from app.services import v3_certify

SAINT_LUCIA = {"mrgid": 8416, "name": "Saint Lucia", "iso2": "LC", "sov_iso2": "LC"}
WL = ["gov.lc", "govt.lc", "rslpf.com", "slaspa.com", "stlucia.org"]
STLUCIA_URL = "http://www.stlucia.org/en/plan-your-saint-lucia-trip/navigation-clearance"
ACT_URL = "https://www.govt.lc/media.govt.lc/www/resources/legislation/CustomsControlManagementAct.pdf"
# stlucia.org, navigation & clearance page.
STLUCIA = (
    "earance\n\nVessels arriving in Saint Lucia may remain for up to three months. Any extension "
    "must be requested in person at the main Immigration Office in Castries. Additional details "
    "on licensing, fees, customs procedures and ports of entry can be found below.\n\nEntry & "
    "Clearance Ports\n\n \n\n### RODNEY BAY MARINA\n\n#### Customs & Immigration\n\nShared Office: "
    "[+1-758-452-0235](tel:+17584520235)\n\nMain Office: [+1-758-452-0234](tel:+17584520234)\n\n"
    "Saint Lucia Air & Sea Ports Authority (SLASPA):\n\n[+1-758-452-0548](tel:+17584520548)\n\n"
    "### Castries\n\nPrimarily serves cruise liners and cargo vessels.\n\n")
STLUCIA_RODNEY = (
    "  Located on the island’s northern tip, IGY Rodney Bay Marina is Saint Lucia’s largest "
    "marina, with 253 slips and a full-service boatyard. An official port of entry, it offers "
    "customs and immigration, duty-free fuel, chandlery, provisioning, and repair facilities, and "
    "serves as the arrival point for the Atlantic Rally for Cruisers (ARC).\n")
# govt.lc, Customs Regulations reg. 136 (hours of attendance), in the run's kept window.
REG_136 = (
    "Airport in 3 shifts 6:00am – 12 midnight daily (b) at Hewanorra International Airport in 2 "
    "shifts 7:00am – 7:00pm daily h) at the boarding stations (a) Castries and Vieux-Fort (A) for "
    "the boarding and clearance of vessels 8:00am – 12 noon and 1:00pm – 4:00pm daily (B) for "
    "other business 8:00am – 12 noon and 1:00pm – 4:30pm daily (ii) Rodney Bay Marina (A) for the "
    "boarding and clearance of vessels 8:00am – 12 noon and 1:00pm – 4:30pm daily (B) for other "
    "business 8:00am – 12 noon and 1:00pm – 4:30pm daily (iii) Marigot Bay Marina (A) for the "
    "boarding and clearance of vessels 8:00am – 12 noon and 1:30pm – 4:00pm daily (B) for other "
    "business 8:00am – 12 noon and 1:00pm – 4:30pm daily (Substituted by S.I. 241/2000) Laws of "
    "Saint Lucia Customs Regulations Cap.15.05 185 Revision Date: 31 Dec 2005 137. RATES OF OVERT")


# --- 1. one definition of official ------------------------------------------------------

def test_the_zone_whitelist_decides_when_given():
    assert is_official_source([STLUCIA_URL], WL)
    assert not is_official_source(["https://www.gov.uk/x"], WL)     # another country's gov
    assert not is_official_source([STLUCIA_URL], [])
    # No zone known: the naming heuristic, unchanged.
    assert not is_official_source([STLUCIA_URL])
    assert is_official_source([ACT_URL])


@pytest.mark.parametrize("url", [STLUCIA_URL, ACT_URL, "https://storage.slaspa.com/x",
                                 "https://www.gov.uk/x", "https://example.com/x"])
def test_every_official_check_agrees(url):
    expected = is_official_source([url], WL)
    assert rc._official_url(url, WL) is expected
    assert v3_certify.is_official(url, WL) is expected
    assert poe.url_allowed(url, WL) is expected


def test_registry_reads_the_zone_whitelist():
    assert set(poe.registry_whitelist(SAINT_LUCIA)) == set(poe.zone_whitelist(SAINT_LUCIA))
    assert "stlucia.org" in poe.registry_whitelist(SAINT_LUCIA)
    assert poe.registry_whitelist({"mrgid": None}) is None


def test_stlucia_org_reaches_the_evidence_store_and_the_judge():
    docs = [{"url": STLUCIA_URL, "text": STLUCIA_RODNEY}]
    assert evidence_store.build_records(8416, ["Rodney Bay Marina"], docs) == []   # heuristic
    records = evidence_store.build_records(8416, ["Rodney Bay Marina"], docs, whitelist=WL)
    assert [r["domain"] for r in records] == ["stlucia.org"]
    assert "official port of entry" in records[0]["snippet"]
    raw = f"[SOURCE: {STLUCIA_URL}]\n{STLUCIA}{STLUCIA_RODNEY}"
    used = [{"url": STLUCIA_URL}]
    assert not poe.has_official_text(raw, used)
    assert poe.has_official_text(raw, used, WL)
    texts = poe.judge_texts(SAINT_LUCIA, [{"name": "Rodney Bay Marina"}], raw, used, WL)
    assert texts and all(t["url"] == STLUCIA_URL for t in texts)


# --- 2. reg. 136 boarding stations ------------------------------------------------------

@pytest.mark.parametrize("name, term", [
    ("Castries", "boarding stations"),
    ("Vieux-Fort", "boarding and clearance of vessels"),
    ("Rodney Bay Marina", "boarding and clearance of vessels"),
    ("Marigot Bay Marina", "boarding and clearance of vessels"),
])
def test_reg_136_makes_evidence(name, term):
    m = rc.entry_mention(REG_136, rc.grounding_variants(name))
    assert m and m["entry_term"] == term and m["strength"] == "strong"
    records = evidence_store.build_records(8416, [name], [{"url": ACT_URL, "text": REG_136}],
                                           whitelist=WL)
    assert [r["domain"] for r in records] == ["govt.lc"]


@pytest.mark.parametrize("text", [
    "The pilot boarding station for Castries is 1 nm west of the harbour entrance.",
    "Pilots board at the pilot boarding stations off Castries and Vieux-Fort.",
    "Vessels require clearance at Castries before departure.",
    "Clearance of vessels for Castries is handled by the agent.",
])
def test_generic_clearance_text_is_not_evidence(text):
    assert rc.entry_mention(text, rc.grounding_variants("Castries")) is None
    assert evidence_store.build_records(8416, ["Castries"], [{"url": ACT_URL, "text": text}],
                                        whitelist=WL) == []


def test_a_pilot_boarding_station_gives_no_c2():
    text = "The pilot boarding station for Castries is 1 nm west of the harbour entrance."
    port = {"name": "Castries", "mrgid": 8416, "extraction_engine": "nightshift",
            "extraction_agreement": False, "source_urls": [ACT_URL, STLUCIA_URL],
            "lat": 14.01, "lon": -60.99, "spatial_kind": "in_eez", "geocode_arbitration": "single"}
    assert v3_certify.level_from_v2(port, whitelist=WL, zone=SAINT_LUCIA,
                                    texts=[{"url": ACT_URL, "text": text}]) == "C1"
