"""Fetch cap keeps a slot per authority role (whitelist_sources.json), offline.

Canaries pilot (run 20261102-224042-f9be8d): 15 official candidates, the 8 fetches
went to transport-ministry and tax pages; Puertos del Estado, Puertos Canarios and
Puertos de Tenerife were never fetched.
"""
import asyncio

import pytest

from app.core.run_rules import bind_rules, catalog_default, reset_rules, snapshot_for_run
from app.services import authority_roles as ar
from app.services import poe_pipeline as poe

CANARIES = {"name": "Canary Islands", "mrgid": 8364, "iso2": "ES", "sov_iso2": "ES"}

# Current ranking order: ministry and tax pages first, the port authorities last.
RANKED = [
    "https://www.transportes.gob.es/marina-mercante",
    "https://www.transportes.gob.es/marina-mercante/nautica-de-recreo",
    "https://www.transportes.gob.es/marina-mercante/titulaciones",
    "https://www.mitma.gob.es/el-ministerio/normativa",
    "https://www.agenciatributaria.es/AEAT.internet/Aduanas.shtml",
    "https://www.agenciatributaria.es/AEAT.internet/Inicio/Aduanas/Viajeros.shtml",
    "https://sede.agenciatributaria.gob.es/Sede/aduanas.html",
    "https://www.gobiernodecanarias.org/hacienda/tributos/",
    "https://www.gobiernodecanarias.org/hacienda/aiem/",
    "https://www.policia.es/_es/extranjeria_fronteras.php",
    "https://www.salvamentomaritimo.es/conocenos",
    "https://www.puertos.es/en/puertos-del-estado",
    "https://www.palmasport.es/en/who-are-we/",
    "https://www.puertosdetenerife.org/en/port-authority-of-santa-cruz-de-tenerife/",
    "https://puertoscanarios.es/en/presentacion-en/",
]
PORT_AUTHORITIES = ("puertos.es", "palmasport.es", "puertosdetenerife.org", "puertoscanarios.es")


def _official(ranked=RANKED):
    return [{"url": u, "domain": poe.domain_of(u), "official": True} for u in ranked]


def _run(monkeypatch, zone, overrides=None, fail=None, ranked=RANKED):
    """``fail`` maps a host to how its fetch goes wrong: timeout, blocked or short."""
    calls: list[str] = []
    fail = fail or {}

    async def fake_cascade(url, min_chars=200, log=None):
        calls.append(url)
        kind = fail.get(poe.domain_of(url))
        if kind == "timeout":
            raise TimeoutError("read timeout")
        if kind == "blocked":
            return {"text": "Access denied", "html": "", "md5": url, "level": "N1",
                    "blocked": True}
        if kind == "short":
            return {"text": "Cookies.", "html": "", "md5": url, "level": "N1", "blocked": False}
        return {"text": "Información oficial. " * 40, "html": "", "md5": url,
                "level": "N1", "blocked": False}

    monkeypatch.setattr(poe, "extract_cascade", fake_cascade)
    logs: list[str] = []
    token = bind_rules(snapshot_for_run(mode="formalities", overrides=overrides or {}))
    try:
        _t, _h, used, _e = asyncio.run(poe._collect_texts(_official(ranked), logs.append,
                                                           zone=zone))
    finally:
        reset_rules(token)
    return calls, [u["url"] for u in used], logs


def _hosts(urls):
    return {poe.domain_of(u) for u in urls}


def test_cap_is_a_run_rule():
    assert catalog_default("formalities.max_fetch") == poe.MAX_FETCH == 8
    assert catalog_default("formalities.fetch_slots_per_role") == poe.FETCH_SLOTS_PER_ROLE == 1


def test_canaries_roles_come_from_whitelist_sources():
    auths = ar.zone_authorities(CANARIES)
    roles = {r for u in RANKED for r in ar.roles_of(u, auths)}
    assert roles == {"customs", "immigration", "maritime_authority", "port_authority"}
    assert ar.roles_of("https://www.puertos.es/en", auths) == {"port_authority"}
    assert ar.roles_of("https://www.salvamentomaritimo.es/x", auths) == {"maritime_authority"}
    assert ar.roles_of("https://www.transportes.gob.es/x", auths) == set()
    # sede.agenciatributaria.gob.es is another registrable domain: not the customs entry
    assert ar.roles_of("https://sede.agenciatributaria.gob.es/x", auths) == set()


def test_shared_gov_label_matches_the_authority_host_only():
    """Cape Verde customs lives on gov.cv: only mf.gov.cv is customs, not any ministry."""
    auths = ar.zone_authorities({"mrgid": 8362, "iso2": "CV"})
    assert ar.roles_of("https://www.mf.gov.cv/web/dnre/portos", auths) == {"customs"}
    assert ar.roles_of("https://www.turismo.gov.cv/", auths) == set()
    assert ar.roles_of("https://www.enapor.cv/portos/", auths) == {"port_authority"}


def test_without_zone_the_old_ranking_misses_every_port_authority(monkeypatch):
    calls, _used, _logs = _run(monkeypatch, zone=None)
    assert len(calls) == 8
    assert not _hosts(calls) & set(PORT_AUTHORITIES)
    assert "puertos.es" not in _hosts(calls)


def test_canaries_fetches_each_authority_role_within_the_cap(monkeypatch):
    calls, used, logs = _run(monkeypatch, zone=CANARIES)
    assert len(calls) == 8
    auths = ar.zone_authorities(CANARIES)
    fetched_roles = {r for u in calls for r in ar.roles_of(u, auths)}
    assert fetched_roles == {"customs", "immigration", "maritime_authority", "port_authority"}
    assert _hosts(calls) & set(PORT_AUTHORITIES)
    assert set(used) == set(calls)
    # the rest follows the current ranking (4 reserved + 1 spare slot for a failure)
    assert calls[:3] == RANKED[:3]
    assert any("créneau réservé port_authority" in m for m in logs)


def test_canaries_three_slots_per_role_reads_three_port_authorities(monkeypatch):
    calls, _used, _logs = _run(monkeypatch, zone=CANARIES,
                               overrides={"formalities.fetch_slots_per_role": 3})
    assert len(calls) == 8
    hosts = _hosts(calls)
    assert len(hosts & set(PORT_AUTHORITIES)) == 3
    assert {"puertos.es", "palmasport.es", "puertosdetenerife.org"} <= hosts
    assert {"salvamentomaritimo.es", "policia.es"} <= hosts


# Pilot order: Las Palmas ranked first among the port authorities.
PILOT = [u for u in RANKED if "puertos.es" not in u] + [
    u for u in RANKED if "puertos.es" in u]


@pytest.mark.parametrize("kind", ["timeout", "blocked", "short"])
def test_failed_fetch_does_not_serve_its_role(monkeypatch, kind):
    """palmasport.es fails: the port_authority slot is spent, the role stays in
    deficit, and the next port authority is reserved and read."""
    calls, used, _logs = _run(monkeypatch, zone=CANARIES, ranked=PILOT,
                              fail={"palmasport.es": kind})
    assert len(calls) == 8
    assert "palmasport.es" in _hosts(calls)
    assert "palmasport.es" not in _hosts(used)
    assert _hosts(used) & {"puertosdetenerife.org", "puertoscanarios.es", "puertos.es"}
    auths = ar.zone_authorities(CANARIES)
    assert {r for u in used for r in ar.roles_of(u, auths)} == {
        "customs", "immigration", "maritime_authority", "port_authority"}


def test_role_with_every_candidate_failing_gives_its_slots_back(monkeypatch):
    """No port authority can be read: one retry, then the role is given up, and the
    other roles are still served instead of four dead port-authority fetches."""
    fail = {h: "timeout" for h in PORT_AUTHORITIES}
    calls, used, logs = _run(monkeypatch, zone=CANARIES, fail=fail)
    assert len(calls) == 8
    pa_calls = [u for u in calls if poe.domain_of(u) in PORT_AUTHORITIES]
    assert len(pa_calls) == 2  # one retry, then given up
    assert len(_hosts(pa_calls)) == 2  # the retry went to another host
    auths = ar.zone_authorities(CANARIES)
    assert {r for u in used for r in ar.roles_of(u, auths)} == {
        "customs", "immigration", "maritime_authority"}


def test_cap_rule_changes_the_number_of_fetches(monkeypatch):
    calls, _used, _logs = _run(monkeypatch, zone=CANARIES,
                               overrides={"formalities.max_fetch": 15})
    assert calls == RANKED


def test_no_pressure_keeps_the_ranking_order(monkeypatch):
    """When the cap holds every candidate the ranked order is unchanged; a role the
    list does not cover (customs here) reads its audited URL after it, with a spare slot."""
    short = RANKED[9:]

    async def fake_cascade(url, min_chars=200, log=None):
        calls.append(url)
        return {"text": "x " * 200, "html": "", "md5": url, "level": "N1", "blocked": False}

    calls: list[str] = []
    monkeypatch.setattr(poe, "extract_cascade", fake_cascade)
    official = [{"url": u, "domain": poe.domain_of(u)} for u in short]
    asyncio.run(poe._collect_texts(official, lambda m: None, zone=CANARIES))
    assert calls[:len(short)] == short
    audited = {a["url"] for a in ar.zone_authorities(CANARIES)}
    assert set(calls[len(short):]) <= audited


def test_zone_without_audit_has_no_reservation(monkeypatch):
    calls, _used, logs = _run(monkeypatch, zone={"mrgid": 990001, "iso2": "ZZ"})
    assert calls == RANKED[:8]
    assert not any("créneaux réservés" in m for m in logs)


@pytest.mark.parametrize("cap,expected", [(2, {"port_authority": 1, "maritime_authority": 1}),
                                          (8, {"port_authority": 1, "maritime_authority": 1,
                                               "customs": 1, "immigration": 1})])
def test_quotas_follow_role_priority_under_a_small_cap(cap, expected):
    auths = ar.zone_authorities(CANARIES)
    assert ar.role_quotas(_official(), auths, cap) == expected
