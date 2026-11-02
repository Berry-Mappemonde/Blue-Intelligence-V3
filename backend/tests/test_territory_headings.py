"""Per-territory lists with markdown / all-caps headings ("### **GUYANE**"): a zone reads
its own section only, and a catalogue from other territories' sections never skips the LLM.

Fixture: the douane.gouv.fr octroi de mer page as Tavily Extract gave it in run
20261102-224042-f9be8d, where Martinique got Pointe-à-Pitre, Degrad, Saint-Denis-Gillot…
and the LLM was skipped. Offline: stubbed LLM and NER, no Mongo.
"""
import asyncio
from pathlib import Path

import pytest

from app.core.extract import extract_structured_ports, territory_rows
from app.services import poe_pipeline as poe

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "douane_octroi_de_mer_contacts_dom.txt"
MARTINIQUE = {"mrgid": 33178, "name": "Martinique", "iso2": "MQ"}
GUYANE = {"mrgid": 8462, "name": "French Guiana", "iso2": "GF"}
SPM = {"mrgid": 8494, "name": "Saint-Pierre and Miquelon", "iso2": "PM"}
OTHER_OFFICES = {"Pointe-à-Pitre", "Degrad", "Saint-Denis-Gillot"}


def _text() -> str:
    raw = FIXTURE.read_text(encoding="utf-8")
    return "".join(ln for ln in raw.splitlines(True) if not ln.startswith("# FIXTURE "))


def test_fixture_is_small_and_cites_its_source():
    assert FIXTURE.stat().st_size < 20_000
    assert any(ln.startswith("# FIXTURE Source: http")
               for ln in FIXTURE.read_text(encoding="utf-8").splitlines())


def test_markdown_all_caps_headings_are_territory_sections():
    heads = [h for h, _ in territory_rows(_text(), poe.zone_row_names(MARTINIQUE)[1])]
    assert {"GUADELOUPE", "MARTINIQUE", "MAYOTTE", "GUYANE", "LA RÉUNION"} <= set(heads)


def test_martinique_reads_its_own_section_only():
    ports, missed = poe.zone_catalog(_text(), MARTINIQUE)
    assert [p["name"] for p in ports] == ["Lamentin"] and not missed
    assert ports[0]["territory_row"] == "MARTINIQUE"
    assert not {p["name"] for p in ports} & OTHER_OFFICES


def test_each_department_gets_its_own_office():
    assert [p["name"] for p in poe.zone_catalog(_text(), GUYANE)[0]] == ["Degrad"]


def test_zone_without_a_section_gets_nothing():
    ports, missed = poe.zone_catalog(_text(), SPM)
    assert ports == [] and missed


def test_without_a_zone_the_parser_is_unchanged():
    # The legacy reader still takes every office (the leak the zone fixes).
    assert OTHER_OFFICES <= {p["name"] for p in extract_structured_ports(_text())}


@pytest.fixture
def stub_readers(monkeypatch):
    calls: list[str] = []

    async def fake_extract(context, zone, settings=None, log=None):
        calls.append(context)
        return []

    import app.core.ml as ml
    monkeypatch.setattr(poe, "extract_ports", fake_extract)
    monkeypatch.setattr(ml, "extract_entities", lambda text: [])
    return calls


def _compared(zone, raw, logs):
    return asyncio.run(poe._extract_ports_compared(raw, zone, logs.append, catalog_text=raw))


def test_other_territories_sections_never_skip_the_llm(stub_readers, monkeypatch):
    # Even a catalogue judged sufficient: SPM has no section on this page.
    monkeypatch.setattr(poe, "catalog_is_sufficient", lambda ports, text="": True)
    logs: list[str] = []
    ports = _compared(SPM, _text(), logs)
    assert stub_readers, logs
    assert not {p["name"] for p in ports} & (OTHER_OFFICES | {"Lamentin"})


def test_martinique_keeps_its_office_and_runs_the_llm(stub_readers):
    logs: list[str] = []
    ports = _compared(MARTINIQUE, _text(), logs)
    assert stub_readers, logs  # one name is not a sufficient catalogue
    names = {p["name"] for p in ports}
    assert "Lamentin" in names and not names & OTHER_OFFICES
