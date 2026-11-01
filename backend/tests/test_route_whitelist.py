"""Route whitelist audit: pilot zones' official domains and the generated report.

Offline: reads the repo's GeoJSON and JSON data files only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))

import route_whitelist_report as report  # noqa: E402
from app.services.poe_pipeline import build_whitelist, load_exceptions, url_allowed  # noqa: E402

PILOTS = {8364: ("Canary Islands", None, "ES"), 8362: ("Cape Verde", "CV", "CV"),
          8416: ("Saint Lucia", "LC", "LC"), 33178: ("Martinique", "MQ", "FR")}
ROLES = {"customs", "immigration", "maritime_authority", "port_authority"}
# Never official, whatever the audit says (CAHIER_DES_CHARGES_POE: Noonsite is a signal only).
NOT_OFFICIAL = ("noonsite", "navily", "marinareservation", "superyacht", "tripadvisor",
                "wikipedia", "facebook", "blogspot", "wordpress", "yachtbooker", "marfret",
                "sailboat-cruising", "stlucia.org", "slucia.com")


@pytest.fixture(scope="module")
def sources():
    return report.load_sources()["zones"]


@pytest.fixture(scope="module")
def exceptions():
    return load_exceptions()


def _wl(mrgid, exceptions):
    _, iso2, sov2 = PILOTS[mrgid]
    return build_whitelist(iso2, sov2, exceptions)


def test_every_pilot_zone_is_audited(sources):
    assert {int(k) for k in sources} == set(PILOTS)
    for mrgid, audit in sources.items():
        assert audit["zone"] == PILOTS[int(mrgid)][0]
        roles = {a["role"] for a in audit["authorities"]}
        assert roles <= ROLES
        # Customs, immigration and at least one of maritime / port authority.
        assert {"customs", "immigration"} <= roles
        assert roles & {"maritime_authority", "port_authority"}


@pytest.mark.parametrize("mrgid", sorted(PILOTS))
def test_pilot_authorities_pass_url_allowed(mrgid, sources, exceptions):
    wl = _wl(mrgid, exceptions)
    for a in sources[str(mrgid)]["authorities"]:
        assert url_allowed(a["url"], wl), (a["url"], wl)
        # Subpages of the same host pass too.
        host = urlsplit(a["url"]).hostname
        assert url_allowed(f"https://{host}/any/page.pdf", wl)


@pytest.mark.parametrize("mrgid", sorted(PILOTS))
def test_added_domains_are_in_the_whitelist_data_and_were_missing(mrgid, sources, exceptions):
    audit = sources[str(mrgid)]
    manual = exceptions["manual"][audit["whitelist_key"]]
    before = build_whitelist(PILOTS[mrgid][1], PILOTS[mrgid][2],
                             report.without_added(exceptions, audit))
    for a in audit["authorities"]:
        host = urlsplit(a["url"]).hostname or ""
        assert host == a["domain"] or host.endswith("." + a["domain"]), a
        if a["added"]:
            assert a["domain"] in manual
            assert not url_allowed(a["url"], before), f"{a['domain']} was already covered"
        else:
            assert url_allowed(a["url"], before), f"{a['domain']} marked as already listed"


def test_every_entry_has_an_official_url_and_a_source(sources):
    for audit in sources.values():
        for a in audit["authorities"]:
            for key in ("name", "url", "domain", "source"):
                assert isinstance(a.get(key), str) and a[key].strip(), (key, a)
            for url in (a["url"], a["source"]):
                assert urlsplit(url).scheme in ("http", "https") and urlsplit(url).hostname
            for bad in NOT_OFFICIAL:
                assert bad not in a["domain"] and bad not in urlsplit(a["source"]).hostname


def test_new_entries_do_not_open_lookalike_hosts(exceptions):
    lc = build_whitelist("LC", "LC", exceptions)
    mq = build_whitelist("MQ", "FR", exceptions)
    es = build_whitelist(None, "ES", exceptions)
    assert url_allowed("https://storage.slaspa.com/contentPages/view/port-castries", lc)
    assert not url_allowed("https://slaspa.com.example.org/", lc)
    assert not url_allowed("https://notslaspa.com/", lc)
    assert not url_allowed("https://www.noonsite.com/place/st-lucia", lc)
    assert url_allowed("https://www.douane.gouv.fr/fiche/navigation-dans-les-departements-doutre-mer", mq)
    # MQ inherits France's State domains (gouv.fr), never lookalikes of them.
    assert url_allowed("https://www.dm.martinique.developpement-durable.gouv.fr/", mq)
    assert not url_allowed("https://gouv.fr.example.com/", mq)
    assert not url_allowed("https://notgouv.fr/", mq)
    assert url_allowed("https://www.palmasport.es/en/who-are-we/", es)
    assert not url_allowed("https://palmasport.es.fake.com/", es)


def test_route_crosses_the_pilot_zones():
    zones = {z["mrgid"]: z for z in report.route_zones()}
    assert len(zones) == 70
    for mrgid, (name, iso2, sov2) in PILOTS.items():
        assert zones[mrgid]["zone"] == name
        assert zones[mrgid]["iso2"] == iso2 and zones[mrgid]["sov_iso2"] == sov2


def test_report_marks_pilots_without_gap_and_is_up_to_date():
    table = report.rows()
    by_mrgid = {r["mrgid"]: r for r in table}
    for mrgid in PILOTS:
        assert by_mrgid[mrgid]["gap"] == "no", by_mrgid[mrgid]
    empty = {r["mrgid"] for r in table if not r["domains"]}
    assert empty and all(by_mrgid[m]["gap"] == "yes" for m in empty)
    assert report.DOC.read_text(encoding="utf-8") == report.render(table), (
        "docs/ROUTE-WHITELIST.md is stale: run backend/scripts/route_whitelist_report.py --write")



# --- territories inherit their sovereign's whitelist ---------------------------------

FR_CUSTOMS = "https://www.douane.gouv.fr/fiche/navigation-dans-les-departements-doutre-mer"
NL_CUSTOMS = "https://www.belastingdienst.nl/wps/wcm/connect/nl/douane/douane"


@pytest.mark.parametrize("iso2, sov2, url", [
    ("MQ", "FR", FR_CUSTOMS),   # Martinique
    ("GP", "FR", FR_CUSTOMS),   # Guadeloupe
    ("MF", "FR", FR_CUSTOMS),   # Collectivity of Saint Martin (was empty)
    ("BQ", "NL", NL_CUSTOMS),   # Bonaire (was empty)
])
def test_territories_accept_their_sovereigns_customs_site(iso2, sov2, url, exceptions):
    wl = build_whitelist(iso2, sov2, exceptions)
    assert url_allowed(url, wl), wl
    # The sovereign's own list is included as is.
    assert set(build_whitelist(sov2, sov2, exceptions)) <= set(wl)


def test_territory_keeps_its_own_domains_too(exceptions):
    wl = build_whitelist("MQ", "FR", exceptions)
    assert {"martinique.gouv.fr", "martinique.port.fr", "douane.gouv.fr"} <= set(wl)
    assert url_allowed("https://www.martinique.port.fr/autorite_portuaire.aspx", wl)


def test_western_sahara_gets_nothing(exceptions):
    from app.services.poe_pipeline import NO_SOVEREIGN_INHERITANCE, whitelist_countries
    assert "disputed" in NO_SOVEREIGN_INHERITANCE["EH"]
    for sov in ("EH", "MA", "ES", None):
        assert whitelist_countries("EH", sov) == ["EH"]
        assert build_whitelist("EH", sov, exceptions) == []
    row = next(r for r in report.rows() if r["mrgid"] == 8368)
    assert row["domains"] == [] and row["gap"] == "yes" and "disputed" in row["note"]


@pytest.mark.parametrize("iso2, sov2, foreign", [
    ("SX", "NL", "https://www.government.nl/"),
    ("AW", "NL", "https://www.government.nl/"),
    ("CW", "NL", "https://www.government.nl/"),
    ("NU", "NZ", "https://www.mpi.govt.nz/"),
    ("CK", "NZ", "https://www.mpi.govt.nz/"),
])
def test_self_governing_countries_do_not_inherit(iso2, sov2, foreign, exceptions):
    assert not url_allowed(foreign, build_whitelist(iso2, sov2, exceptions))


def test_inheritance_order_and_zones_without_iso2():
    from app.services.poe_pipeline import whitelist_countries
    assert whitelist_countries("MQ", "FR") == ["MQ", "FR"]
    assert whitelist_countries("mq", "fr") == ["MQ", "FR"]
    assert whitelist_countries("FR", "FR") == ["FR"]
    assert whitelist_countries(None, "ES") == ["ES"]   # Canary Islands
    assert whitelist_countries(None, None) == []
    assert whitelist_countries("SX", "NL") == ["SX"]
