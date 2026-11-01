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
          8416: ("Saint Lucia", "LC", "LC"), 33178: ("Martinique", "MQ", "FR"),
          # French overseas zones on the route.
          33177: ("Guadeloupe", "GP", "FR"), 48952: ("Saint-Barthélemy", "BL", "FR"),
          8495: ("Collectivity of Saint Martin", "MF", "FR"), 8462: ("French Guiana", "GF", "FR"),
          8494: ("Saint-Pierre and Miquelon", "PM", "FR"), 8440: ("French Polynesia", "PF", "FR"),
          8454: ("Wallis and Futuna", "WF", "FR"), 8312: ("New Caledonia", "NC", "FR"),
          48944: ("Mayotte", "YT", "FR"), 8338: ("Réunion", "RE", "FR"),
          48946: ("Ile Tromelin", None, "FR"), 8341: ("Europa Island", "TF", "FR")}
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
        # A role with no authority in the zone (no port, no customs post) says why.
        none = audit.get("no_authority") or {}
        assert set(none) <= ROLES and not set(none) & roles
        assert all(isinstance(why, str) and why.strip() for why in none.values())
        # Customs, immigration and at least one of maritime / port authority.
        assert {"customs", "immigration"} <= roles | set(none)
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
    # MQ gets the customs and DM hosts, not all of gouv.fr.
    assert not url_allowed("https://www.education.gouv.fr/", mq)
    assert not url_allowed("https://dm.guadeloupe.developpement-durable.gouv.fr/", mq)
    assert url_allowed("https://www.palmasport.es/en/who-are-we/", es)
    assert not url_allowed("https://palmasport.es.fake.com/", es)
    # Overseas zones get the national service's host, not its whole parent domain.
    re_ = build_whitelist("RE", "FR", exceptions)
    assert url_allowed("https://www.police-nationale.interieur.gouv.fr/x", re_)
    assert not url_allowed("https://www.interieur.gouv.fr/", re_)
    assert not url_allowed("https://www.education.gouv.fr/", re_)
    bl = build_whitelist("BL", "FR", exceptions)
    assert url_allowed("https://portdegustavia.fr/port-plaisance-gustavia/formalites-port-plaisance", bl)
    assert not url_allowed("https://www.comstbarth.fr/", bl)  # unverifiable JS shell, not listed
    nc = build_whitelist("NC", "FR", exceptions)
    assert url_allowed("https://noumeaport.nc/la-capitainerie/la-presentation/", nc)
    assert not url_allowed("https://noumeaport.nc.example.com/", nc)
    # Tromelin uses France's list: taaf.fr is now official for mainland France too.
    assert url_allowed("https://taaf.fr/acceder-aux-territoires/demande-de-visa/",
                       build_whitelist(None, "FR", exceptions))


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
